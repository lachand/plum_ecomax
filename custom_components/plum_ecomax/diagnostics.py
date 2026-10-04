"""Diagnostics support for the Plum EcoMAX integration.

Lets a user download a redacted snapshot (config entry data with
credentials stripped, coordinator cache, detected parameters) straight
from the Home Assistant UI (Settings > Devices & Services > Plum EcoMAX >
Download diagnostics) instead of having to reproduce state by hand for a
bug report.
"""

from __future__ import annotations

import time
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_IP_ADDRESS, CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant

TO_REDACT = {CONF_PASSWORD, CONF_USERNAME, CONF_IP_ADDRESS, "ip"}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    coordinator = entry.runtime_data
    device = coordinator.device

    now = time.time()
    cache_ages = {
        slug: round(now - ts, 1)
        for slug, ts in sorted(getattr(coordinator, "_timestamps", {}).items())
    }

    return {
        "entry": {
            "data": async_redact_data(dict(entry.data), TO_REDACT),
            "options": async_redact_data(dict(entry.options), TO_REDACT),
        },
        "device": async_redact_data(
            {
                "ip": device.ip,
                "port": device.port,
                "map_file": device.map_file,
                "params_map_size": len(device.params_map),
                "connected": device._sock is not None,
                "consecutive_failures": device.consecutive_failures,
                "last_write_error": (
                    f"0x{device.last_write_error:02X}"
                    if device.last_write_error is not None
                    else None
                ),
            },
            TO_REDACT,
        ),
        "coordinator": {
            "last_update_success": coordinator.last_update_success,
            "last_exception": repr(coordinator.last_exception)
            if coordinator.last_exception
            else None,
            "update_interval": str(coordinator.update_interval),
            "available_slugs": sorted(coordinator.available_slugs),
            "delta_rejection_counts": dict(getattr(coordinator, "_delta_rejection_counts", {})),
            "cache_age_seconds": cache_ages,
            "cached_data": dict(coordinator.data) if coordinator.data else {},
        },
    }
