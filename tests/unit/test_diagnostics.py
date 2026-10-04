"""Unit tests for diagnostics.py -- redaction + the link-health fields
added so a bug report carries what's actually needed to triage it.
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock

import pytest
from homeassistant.const import CONF_IP_ADDRESS, CONF_PASSWORD

from custom_components.plum_ecomax.diagnostics import async_get_config_entry_diagnostics


def _setup():
    device = MagicMock()
    device.ip = "192.168.1.38"
    device.port = 8899
    device.map_file = "/config/custom_components/plum_ecomax/device_map_ecomax360i.json"
    device.params_map = {"a": {}, "b": {}}
    device._sock = object()
    device.consecutive_failures = 2
    device.last_write_error = 0x7D

    coordinator = MagicMock()
    coordinator.device = device
    coordinator.last_update_success = True
    coordinator.last_exception = None
    coordinator.update_interval = "0:00:30"
    coordinator.available_slugs = ["b", "a"]
    coordinator.data = {"a": 1, "b": 2}
    coordinator._timestamps = {"a": time.time() - 12, "b": time.time() - 400}
    coordinator._delta_rejection_counts = {"tempwthr": 1}

    hass = MagicMock()
    entry = MagicMock()
    entry.entry_id = "e1"
    entry.data = {CONF_IP_ADDRESS: "192.168.1.38", CONF_PASSWORD: "0000", "port": 8899}
    entry.options = {}
    entry.runtime_data = coordinator
    return hass, entry


@pytest.mark.asyncio
async def test_credentials_and_ip_are_redacted():
    hass, entry = _setup()
    diag = await async_get_config_entry_diagnostics(hass, entry)

    assert diag["entry"]["data"][CONF_PASSWORD] == "**REDACTED**"
    assert diag["entry"]["data"][CONF_IP_ADDRESS] == "**REDACTED**"
    assert diag["device"]["ip"] == "**REDACTED**"


@pytest.mark.asyncio
async def test_link_health_fields_present():
    hass, entry = _setup()
    diag = await async_get_config_entry_diagnostics(hass, entry)

    assert diag["device"]["consecutive_failures"] == 2
    assert diag["device"]["last_write_error"] == "0x7D"
    assert diag["device"]["connected"] is True
    assert diag["coordinator"]["delta_rejection_counts"] == {"tempwthr": 1}
    # cache ages are exposed per slug, roughly matching the seeded timestamps
    ages = diag["coordinator"]["cache_age_seconds"]
    assert ages["a"] < 60 < ages["b"]
