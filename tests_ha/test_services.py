"""Entity services and the set_schedule service, through the real service registry.

Writes are recorded by FakePlumDevice.writes as (slug, value).
"""

from __future__ import annotations

import datetime

from homeassistant.config_entries import ConfigEntryState
from pytest_homeassistant_custom_component.common import MockConfigEntry

from tests_ha.fakes import DOMAIN, ENTRY_DATA, SERIAL, FakePlumDevice


async def _loaded(hass) -> tuple[MockConfigEntry, FakePlumDevice]:
    entry = MockConfigEntry(domain=DOMAIN, data=ENTRY_DATA, unique_id=SERIAL)
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    return entry, FakePlumDevice.instances[-1]


async def _call(hass, domain, service, data, **kwargs):
    result = await hass.services.async_call(domain, service, data, blocking=True, **kwargs)
    await hass.async_block_till_done()
    return result


def _written(device, slug):
    return [value for s, value in device.writes if s == slug]


async def test_water_heater_setpoint_and_mode(hass):
    _, device = await _loaded(hass)
    target = "water_heater.dhw_domestic_hot_water"

    await _call(hass, "water_heater", "set_temperature", {"entity_id": target, "temperature": 52})
    await _call(
        hass, "water_heater", "set_operation_mode", {"entity_id": target, "operation_mode": "eco"}
    )

    assert _written(device, "hdwtsetpoint") == [52]
    assert _written(device, "hdwusermode") == [2]


async def test_climate_setpoint_and_hvac_mode(hass):
    _, device = await _loaded(hass)
    entity = "climate.circuit_2_thermostat"

    await _call(hass, "climate", "set_temperature", {"entity_id": entity, "temperature": 21.5})
    await _call(hass, "climate", "set_hvac_mode", {"entity_id": entity, "hvac_mode": "off"})
    await _call(hass, "climate", "set_hvac_mode", {"entity_id": entity, "hvac_mode": "heat"})

    assert _written(device, "circuit2comforttemp")  # the setpoint went to the circuit-2 register
    assert _written(device, "circuit2active") == [0, 1]


async def test_configuration_switches_write_their_on_and_off_values(hass):
    _, device = await _loaded(hass)

    await _call(hass, "switch", "turn_on", {"entity_id": "switch.dhw_anti_legionella_cycle"})
    await _call(hass, "switch", "turn_off", {"entity_id": "switch.dhw_anti_legionella_cycle"})
    await _call(hass, "switch", "turn_on", {"entity_id": "switch.plum_ecomax_boiler_manual_mode"})
    await _call(hass, "switch", "turn_off", {"entity_id": "switch.plum_ecomax_boiler_manual_mode"})

    legion = _written(device, "hdwstartlegion")
    assert len(legion) == 2 and legion[0] != legion[1]
    assert _written(device, "operatingmode") == [2, 1]  # manual, then back to automatic


async def test_dhw_mode_select(hass):
    _, device = await _loaded(hass)

    await _call(hass, "select", "select_option", {"entity_id": "select.dhw_mode", "option": "auto"})
    await _call(hass, "select", "select_option", {"entity_id": "select.dhw_mode", "option": "off"})

    assert _written(device, "hdwusermode") == [2, 0]


async def test_the_solar_dump_auto_switch_toggles_without_writing_the_boiler(hass):
    _, device = await _loaded(hass)
    entity = "switch.plum_ecomax_boiler_solar_dump_automatic_mode"

    await _call(hass, "switch", "turn_on", {"entity_id": entity})
    assert hass.states.get(entity).state == "on"
    await _call(hass, "switch", "turn_off", {"entity_id": entity})
    assert hass.states.get(entity).state == "off"

    # The controller only acts through bursts that need a warm tank and a dT; the
    # fake boiler reports neither, so no boiler register is touched.
    assert not _written(device, "hdwpumpforce")


async def test_set_schedule_service_writes_the_comfort_registers(hass):
    _, device = await _loaded(hass)

    await _call(
        hass,
        DOMAIN,
        "set_schedule",
        {
            "circuit": 2,
            "days": ["monday"],
            "comfort_blocks": [{"from": "06:00:00", "to": "08:00:00"}],
        },
    )

    # one AM and one PM register per target day, for circuit 2's weekly program
    assert len(device.writes) >= 2
    assert all(isinstance(value, int) for _, value in device.writes)


async def test_calendar_exposes_the_comfort_events(hass):
    await _loaded(hass)
    start = datetime.datetime.now(datetime.UTC)
    end = start + datetime.timedelta(days=2)

    result = await hass.services.async_call(
        "calendar",
        "get_events",
        {
            "entity_id": "calendar.circuit_2_schedule",
            "start_date_time": start,
            "end_date_time": end,
        },
        blocking=True,
        return_response=True,
    )

    assert "calendar.circuit_2_schedule" in result
    assert isinstance(result["calendar.circuit_2_schedule"]["events"], list)
