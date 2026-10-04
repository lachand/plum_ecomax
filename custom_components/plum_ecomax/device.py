"""Shared device-registry helpers for the Plum EcoMAX integration.

Every platform (sensor, climate, calendar, number, switch, select,
water_heater) groups its entities under one of a few devices: the main
boiler, the DHW manager, a heating circuit, or the mixers group. This
module is the single place that builds those `DeviceInfo` objects so all
platforms agree on the same identifiers.

All identifiers are scoped by config entry ID. Before this module existed,
several platforms (switch.py, calendar.py, water_heater.py) hardcoded the
DHW device as `(DOMAIN, "plum_hdw")` -- a fixed string shared by *every*
config entry, so a second boiler (second config entry) would collide with
the first on the same DHW device. `tests/regression/test_device_info_scoping.py`
guards against that pattern coming back.
"""

import logging
import re
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity import DeviceInfo

from .const import CONF_ACTIVE_CIRCUITS, DOMAIN

_LOGGER = logging.getLogger(__name__)


def normalise_serial(raw: Any) -> str | None:
    """Boiler serial number as a stable string, or None if unreadable.

    The "uid" parameter is a RAW (zero-terminated string) value, so it may
    arrive as already-decoded text or as raw bytes.
    """
    if isinstance(raw, (bytes, bytearray)):
        raw = bytes(raw).split(b"\x00")[0].decode("ascii", errors="ignore")
    if raw is None:
        return None
    serial = str(raw).strip()
    return serial or None


def boiler_device_info(entry_id: str, serial_number: str | None = None) -> DeviceInfo:
    """The main boiler device.

    Args:
        entry_id: The config entry ID.
        serial_number: The boiler's serial number (device map slug "uid",
            RAW/STRING type), if the coordinator has read it yet. Shown in
            the device registry instead of as a separate sensor entity.
    """
    return DeviceInfo(
        identifiers={(DOMAIN, entry_id)},
        name="Plum EcoMAX Boiler",
        manufacturer="Plum",
        serial_number=serial_number,
    )


def hdw_device_info(entry_id: str) -> DeviceInfo:
    """The domestic hot water (DHW) device."""
    return DeviceInfo(
        identifiers={(DOMAIN, f"{entry_id}_hdw")},
        name="DHW",
        manufacturer="Plum",
        model="DHW Manager",
        via_device=(DOMAIN, entry_id),
    )


def circuit_device_info(entry_id: str, circuit_id: int | str) -> DeviceInfo:
    """A heating circuit device."""
    return DeviceInfo(
        identifiers={(DOMAIN, f"{entry_id}_circuit_{circuit_id}")},
        name=f"Circuit {circuit_id}",
        manufacturer="Plum",
        model="Heating controller",
        via_device=(DOMAIN, entry_id),
    )


def mixers_device_info(entry_id: str) -> DeviceInfo:
    """The mixing valves device."""
    return DeviceInfo(
        identifiers={(DOMAIN, f"{entry_id}_mixers")},
        name="Mixers",
        manufacturer="Plum",
        model="Mixing valves",
        via_device=(DOMAIN, entry_id),
    )


def is_stale_device(hass: HomeAssistant, entry: ConfigEntry, device_entry: dr.DeviceEntry) -> bool:
    """True if `device_entry` no longer corresponds to anything this entry creates.

    The main boiler device is never stale. A circuit device is stale once its
    circuit is no longer in the entry's active circuits (the platforms stop
    creating its entities). Any other device is stale when no entity at all
    (disabled ones included) is registered on it -- nothing creates it anymore.
    """
    identifiers = {ident for domain, ident in device_entry.identifiers if domain == DOMAIN}
    if entry.entry_id in identifiers:
        return False

    active_circuits = {str(c) for c in entry.data.get(CONF_ACTIVE_CIRCUITS, [])}
    for ident in identifiers:
        match = re.fullmatch(rf"{re.escape(entry.entry_id)}_circuit_(\d+)", ident)
        if match:
            return match.group(1) not in active_circuits

    entity_registry = er.async_get(hass)
    return not er.async_entries_for_device(
        entity_registry, device_entry.id, include_disabled_entities=True
    )


def async_remove_stale_devices(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Detach this entry from devices it no longer creates (see is_stale_device).

    Run after the platforms have been set up, so every live device already has
    its entities registered. Removing a device also drops its leftover entity
    registry entries.
    """
    device_registry = dr.async_get(hass)
    for device_entry in dr.async_entries_for_config_entry(device_registry, entry.entry_id):
        if is_stale_device(hass, entry, device_entry):
            _LOGGER.info("Removing device no longer present: %s", device_entry.name)
            device_registry.async_update_device(
                device_entry.id, remove_config_entry_id=entry.entry_id
            )
