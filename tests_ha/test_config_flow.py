"""Config flow (user + reconfigure) driven through the real flow manager."""

from __future__ import annotations

from homeassistant import config_entries
from homeassistant.config_entries import ConfigEntryState
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from tests_ha.fakes import DOMAIN, ENTRY_DATA, IP, SERIAL, FakePlumDevice

USER_INPUT = dict(ENTRY_DATA)


async def _start(hass, source=config_entries.SOURCE_USER, **kwargs):
    return await hass.config_entries.flow.async_init(DOMAIN, context={"source": source}, **kwargs)


async def test_user_flow_creates_an_entry_keyed_by_the_serial_number(hass):
    result = await _start(hass)
    assert result["type"] is FlowResultType.FORM and result["step_id"] == "user"

    result = await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    entry = result["result"]
    assert entry.unique_id == SERIAL
    assert entry.data["ip_address"] == IP
    assert entry.state is ConfigEntryState.LOADED


async def test_unreachable_boiler_shows_cannot_connect(hass):
    FakePlumDevice.link_up = False
    result = await _start(hass)

    result = await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "cannot_connect"}


async def test_same_boiler_cannot_be_added_twice(hass):
    MockConfigEntry(domain=DOMAIN, data=ENTRY_DATA, unique_id=SERIAL).add_to_hass(hass)
    result = await _start(hass)

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {**USER_INPUT, "ip_address": "192.0.2.99"},  # new IP, same serial
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_an_ip_already_used_by_a_legacy_entry_is_refused(hass):
    # Entries created before the serial-number unique_id were keyed by the IP.
    MockConfigEntry(domain=DOMAIN, data=ENTRY_DATA, unique_id=IP).add_to_hass(hass)
    result = await _start(hass)

    result = await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_reconfigure_updates_the_address_and_reloads(hass):
    entry = MockConfigEntry(domain=DOMAIN, data=ENTRY_DATA, unique_id=SERIAL)
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    result = await entry.start_reconfigure_flow(hass)
    assert result["type"] is FlowResultType.FORM and result["step_id"] == "reconfigure"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {**USER_INPUT, "ip_address": "192.0.2.77"}
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert entry.data["ip_address"] == "192.0.2.77"
    assert entry.unique_id == SERIAL
    assert entry.state is ConfigEntryState.LOADED


async def test_reconfigure_refuses_a_different_boiler(hass):
    entry = MockConfigEntry(domain=DOMAIN, data=ENTRY_DATA, unique_id=SERIAL)
    entry.add_to_hass(hass)
    FakePlumDevice.values = {"uid": "ANOTHERBOILER"}

    result = await entry.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {**USER_INPUT, "ip_address": "192.0.2.77"}
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "unique_id_mismatch"
    assert entry.data["ip_address"] == IP  # unchanged


async def test_reconfigure_keeps_the_form_open_on_a_connection_error(hass):
    entry = MockConfigEntry(domain=DOMAIN, data=ENTRY_DATA, unique_id=SERIAL)
    entry.add_to_hass(hass)
    FakePlumDevice.link_up = False

    result = await entry.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "cannot_connect"}
