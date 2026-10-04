"""On-disk snapshot of the reference values (the save/restore buttons).

One store per config entry, so two boilers never share a snapshot. Lives in its
own module so the integration's entry removal can delete it without importing
a platform.
"""

from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .const import DOMAIN

STORAGE_VERSION = 1


def snapshot_store(hass: HomeAssistant, entry_id: str) -> Store[dict[str, Any]]:
    """The store holding the saved reference values of one config entry."""
    return Store(hass, STORAGE_VERSION, f"{DOMAIN}_{entry_id}_number_defaults")


async def async_remove_snapshot(hass: HomeAssistant, entry_id: str) -> None:
    """Delete the saved reference values of a removed config entry."""
    await snapshot_store(hass, entry_id).async_remove()
