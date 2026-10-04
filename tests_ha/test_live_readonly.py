"""Read-only smoke test of the whole integration against the REAL boiler.

Opt-in: skipped unless PLUM_LIVE_IP is set, so CI and ordinary runs ignore it.
Every write is forbidden for the duration (a write attempt fails the test), so
it is safe to run against a boiler in service.

    PLUM_LIVE_IP=192.168.1.38 pytest tests_ha/test_live_readonly.py \
        -p pytest_homeassistant_custom_component -s

The plugin blocks sockets by default; this test re-enables them for the boiler's
address only.
"""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.plum_ecomax.plum_device import PlumDevice
from tests_ha.fakes import DOMAIN

LIVE_IP = os.environ.get("PLUM_LIVE_IP")

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(not LIVE_IP, reason="set PLUM_LIVE_IP to run against the real boiler"),
]


@pytest.fixture
def real_sockets(socket_enabled):
    import pytest_socket

    pytest_socket.enable_socket()
    pytest_socket.socket_allow_hosts([LIVE_IP, "127.0.0.1"], allow_unix_socket=True)
    yield


async def test_integration_runs_against_the_real_boiler_without_writing(hass, real_sockets):
    async def _no_writes(self, slug, value, *args, **kwargs):
        raise AssertionError(f"write attempted on a live boiler: {slug}={value}")

    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=LIVE_IP,  # legacy IP key: also exercises the serial-number adoption
        data={
            "ip_address": LIVE_IP,
            "port": 8899,
            "username": "admin",
            "password": "0000",
            "active_circuits": ["2"],
            "update_interval": 30,
        },
    )
    entry.add_to_hass(hass)

    with patch.object(PlumDevice, "set_value", _no_writes):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        assert entry.state is ConfigEntryState.LOADED
        assert entry.unique_id != LIVE_IP, "the serial number should replace the IP"
        coordinator = entry.runtime_data
        assert coordinator.available_slugs, "the initial scan detected nothing"

        entities = er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)
        assert len(entities) > 30
        devices = {
            d.name for d in dr.async_entries_for_config_entry(dr.async_get(hass), entry.entry_id)
        }
        assert "Plum EcoMAX Boiler" in devices

        await coordinator.async_refresh()  # a live re-read through the real link
        assert coordinator.last_update_success
        assert coordinator.device.consecutive_failures == 0

        assert await hass.config_entries.async_unload(entry.entry_id)
