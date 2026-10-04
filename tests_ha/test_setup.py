"""Entry setup / unload against a real Home Assistant."""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import patch

from homeassistant.config_entries import ConfigEntryState
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.plum_ecomax import async_remove_config_entry_device
from tests_ha.fakes import DOMAIN, ENTRY_DATA, IP, SERIAL, FakePlumDevice


async def _setup(hass, **kwargs) -> MockConfigEntry:
    kwargs.setdefault("unique_id", SERIAL)
    entry = MockConfigEntry(domain=DOMAIN, data=kwargs.pop("data", ENTRY_DATA), **kwargs)
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


def _device_names(hass, entry) -> set[str]:
    registry = dr.async_get(hass)
    return {d.name for d in dr.async_entries_for_config_entry(registry, entry.entry_id)}


async def test_entry_loads_with_its_devices_and_entities(hass):
    entry = await _setup(hass)

    assert entry.state is ConfigEntryState.LOADED
    # Only the active circuit (2) gets a device; the others are never created.
    assert _device_names(hass, entry) == {"Plum EcoMAX Boiler", "DHW", "Circuit 2"}
    assert len(er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)) > 40


async def test_entity_ids_do_not_repeat_the_device_name(hass):
    entry = await _setup(hass)
    ids = {
        e.entity_id for e in er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)
    }

    assert "number.circuit_2_base_temperature" in ids
    assert "sensor.circuit_2_name" in ids
    assert not any("circuit_2_circuit_2" in i or "dhw_dhw" in i for i in ids)


async def test_unreachable_boiler_at_startup_retries_instead_of_loading_empty(hass):
    FakePlumDevice.link_up = False

    entry = await _setup(hass)

    assert entry.state is ConfigEntryState.SETUP_RETRY
    assert not er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)

    # Once the boiler answers, the next attempt sets everything up.
    FakePlumDevice.link_up = True
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED
    assert er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)


async def test_unload_closes_the_link_and_removes_the_services_with_the_last_entry(hass):
    entry = await _setup(hass)
    assert hass.services.has_service(DOMAIN, "solar_to_buffer")
    device = FakePlumDevice.instances[-1]

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.NOT_LOADED
    assert device.closed
    assert not hass.services.has_service(DOMAIN, "solar_to_buffer")
    assert not hass.services.has_service(DOMAIN, "set_schedule")


async def test_services_survive_unloading_one_of_two_entries(hass):
    first = await _setup(hass)
    second = await _setup(
        hass, unique_id="OTHERSERIAL", data={**ENTRY_DATA, "ip_address": "192.0.2.11"}
    )

    assert await hass.config_entries.async_unload(first.entry_id)
    await hass.async_block_till_done()

    assert second.state is ConfigEntryState.LOADED
    assert hass.services.has_service(DOMAIN, "solar_to_buffer")
    assert hass.services.has_service(DOMAIN, "set_schedule")


async def test_entry_keyed_by_ip_adopts_the_serial_number(hass):
    entry = await _setup(hass, unique_id=IP)

    assert entry.state is ConfigEntryState.LOADED
    assert entry.unique_id == SERIAL


async def test_options_flow_is_gone(hass):
    entry = await _setup(hass)
    assert not entry.supports_options


async def test_entities_go_unavailable_during_an_outage_and_recover(hass):
    entry = await _setup(hass)
    coordinator = entry.runtime_data
    state_id = "sensor.circuit_2_room_temperature"
    assert hass.states.get(state_id).state != "unavailable"
    base = coordinator.update_interval

    FakePlumDevice.link_up = False
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert hass.states.get(state_id).state == "unavailable"
    assert coordinator.update_interval == base * 2  # polling backs off during the outage

    FakePlumDevice.link_up = True
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert hass.states.get(state_id).state != "unavailable"
    assert coordinator.update_interval == timedelta(seconds=ENTRY_DATA["update_interval"])


async def test_stale_devices_are_removed_and_live_ones_kept(hass):
    entry = MockConfigEntry(domain=DOMAIN, data=ENTRY_DATA, unique_id=SERIAL)
    entry.add_to_hass(hass)
    registry = dr.async_get(hass)
    for ident in (f"{entry.entry_id}_circuit_5", f"{entry.entry_id}_ghost"):
        registry.async_get_or_create(
            config_entry_id=entry.entry_id, identifiers={(DOMAIN, ident)}, name=ident
        )

    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert _device_names(hass, entry) == {"Plum EcoMAX Boiler", "DHW", "Circuit 2"}


async def test_ui_delete_is_allowed_only_for_stale_devices(hass):
    entry = await _setup(hass)
    registry = dr.async_get(hass)
    stale = registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, f"{entry.entry_id}_circuit_6")},
        name="Circuit 6",
    )
    main = registry.async_get_device(identifiers={(DOMAIN, entry.entry_id)})
    live = registry.async_get_device(identifiers={(DOMAIN, f"{entry.entry_id}_circuit_2")})

    assert await async_remove_config_entry_device(hass, entry, stale)
    assert not await async_remove_config_entry_device(hass, entry, main)
    assert not await async_remove_config_entry_device(hass, entry, live)


async def test_unload_restores_automatic_mode_before_closing_the_link(hass):
    """A solar-dump session running at unload must write the boiler back to
    automatic while the connection is still open."""
    import asyncio

    from custom_components.plum_ecomax import solar_dump

    entry = await _setup(hass)
    device = FakePlumDevice.instances[-1]
    # Warm DHW so the start gate passes; boiler currently in automatic mode.
    FakePlumDevice.values = {"tempcwu": 60.0, "operatingmode": 1, "heatsourcemainpumpstate": 64}
    real_sleep = asyncio.sleep

    async def _quick(seconds, *a, **k):  # skip the 3 s manual-mode settle delay
        await real_sleep(0 if seconds >= 1 else seconds)

    with patch.object(solar_dump.asyncio, "sleep", _quick):
        await solar_dump.async_start_hold(hass, entry.runtime_data, entry.entry_id)
        for _ in range(200):
            if ("hdwpumpforce", 512) in device.writes:
                break
            await real_sleep(0.01)
        assert ("hdwpumpforce", 512) in device.writes

        closed_when_restoring: list[bool] = []
        original_set = device.set_value

        async def _spy(slug, value, *a, **k):
            closed_when_restoring.append(device.closed)
            return await original_set(slug, value, *a, **k)

        device.set_value = _spy
        assert await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()

    assert device.writes[-1] == ("operatingmode", 1)
    assert ("hdwpumpforce", 0) in device.writes
    assert closed_when_restoring and not any(closed_when_restoring)  # link still open
    assert device.closed
