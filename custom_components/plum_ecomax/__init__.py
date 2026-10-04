"""The main entry point for the Plum EcoMAX integration.

This module handles the setup, configuration, and unloading of the
integration through Home Assistant's Config Flow.
"""

import asyncio
import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_IP_ADDRESS, CONF_PASSWORD, CONF_PORT
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.typing import ConfigType

from .const import CONF_UPDATE_INTERVAL, DEFAULT_PORT, DOMAIN, SERIAL_SLUG, UPDATE_INTERVAL
from .coordinator import PlumDataUpdateCoordinator
from .device import async_remove_stale_devices, is_stale_device, normalise_serial
from .plum_device import DEVICE_MAP_PATH, PlumDevice
from .schedule import async_register_services as async_register_schedule_service
from .schedule import async_unregister_services as async_unregister_schedule_service
from .solar_dump import async_register_services as async_register_solar_dump_service
from .solar_dump import async_register_stop_listener as async_register_solar_dump_stop
from .solar_dump import async_stop_auto as async_stop_solar_dump_auto
from .solar_dump import async_stop_for_entry as async_stop_solar_dump
from .solar_dump import async_unregister_services as async_unregister_solar_dump_service

_LOGGER = logging.getLogger(__name__)

type PlumConfigEntry = ConfigEntry[PlumDataUpdateCoordinator]

PLATFORMS = [
    "climate",
    "sensor",
    "number",
    "switch",
    "select",
    "water_heater",
    "calendar",
    "button",
    "binary_sensor",
]

# This integration is only ever set up from a config entry (Settings ->
# Devices & Services -> Add Integration), never from configuration.yaml --
# tells hassfest/HA there's deliberately no YAML schema to validate.
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


def _adopt_serial_unique_id(
    hass: HomeAssistant, entry: PlumConfigEntry, coordinator: PlumDataUpdateCoordinator
) -> None:
    """Move an IP-keyed entry's unique_id to the boiler's serial number.

    Entries created before the serial-based unique_id existed use the IP,
    which changes with DHCP. Done here, once the first refresh has read
    "uid", rather than in a migration step: a migration runs before the boiler
    is reachable and must not depend on it. Entity unique_ids are scoped by
    entry_id, not by this value, so nothing else is renamed.
    """
    serial = normalise_serial(coordinator.data.get(SERIAL_SLUG))
    if not serial or entry.unique_id == serial:
        return
    if any(e.unique_id == serial for e in hass.config_entries.async_entries(DOMAIN)):
        _LOGGER.debug("Serial %s already used by another entry, keeping unique_id", serial)
        return
    hass.config_entries.async_update_entry(entry, unique_id=serial)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up the Plum EcoMAX component.

    Args:
        hass: The Home Assistant instance.
        config: The configuration dictionary.

    Returns:
        bool: Always True (configuration is handled via Config Flow).
    """
    return True


async def async_setup_entry(hass: HomeAssistant, entry: PlumConfigEntry) -> bool:
    """Set up Plum EcoMAX from a config entry.

    This function initializes the connection to the boiler, loads the
    device parameter map, creates the data coordinator, and sets up
    the various platforms (sensor, climate, etc.).

    Args:
        hass: The Home Assistant instance.
        entry: The config entry containing connection details.

    Returns:
        bool: True if setup was successful.
    """
    ip = entry.data.get(CONF_IP_ADDRESS)
    port = entry.data.get(CONF_PORT, DEFAULT_PORT)
    password = entry.data.get(CONF_PASSWORD, "0000")

    json_path = str(DEVICE_MAP_PATH)

    device = PlumDevice(ip, port=port, password=password, map_file=json_path)

    try:
        await asyncio.to_thread(device.load_map)
    except (OSError, ValueError) as err:
        raise ConfigEntryNotReady(f"Could not load parameter map {json_path}: {err}") from err

    update_interval = entry.data.get(CONF_UPDATE_INTERVAL, UPDATE_INTERVAL)
    coordinator = PlumDataUpdateCoordinator(hass, device, entry, update_interval=update_interval)

    await coordinator.async_config_entry_first_refresh()

    entry.runtime_data = coordinator
    _adopt_serial_unique_id(hass, entry, coordinator)
    async_register_solar_dump_stop(hass, entry, coordinator)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    async_remove_stale_devices(hass, entry)
    await async_register_schedule_service(hass)
    await async_register_solar_dump_service(hass)
    return True


async def async_remove_config_entry_device(
    hass: HomeAssistant, entry: PlumConfigEntry, device_entry: dr.DeviceEntry
) -> bool:
    """Let the user delete a device from the UI only if it is no longer present.

    Enables the "Delete" button on stale devices (e.g. a circuit that is no
    longer active); live devices, and always the main boiler, stay protected.
    """
    return is_stale_device(hass, entry, device_entry)


async def async_unload_entry(hass: HomeAssistant, entry: PlumConfigEntry) -> bool:
    """Unload a config entry.

    Args:
        hass: The Home Assistant instance.
        entry: The config entry to unload.

    Returns:
        bool: True if the entry was successfully unloaded.
    """
    if unload_ok := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        # Disarm the auto controller, then stop any in-flight solar_to_buffer
        # run and let it write the boiler back to automatic BEFORE the socket
        # below is closed -- otherwise the boiler could be left in manual mode.
        await async_stop_solar_dump_auto(hass, entry.runtime_data)
        await async_stop_solar_dump(hass, entry.runtime_data)
        coordinator = entry.runtime_data
        # PlumDevice now keeps its TCP connection open across transactions
        # (persistent connection) instead of closing it after every one --
        # tear it down explicitly here so a reload/removal doesn't leak an
        # open socket until garbage collection gets around to it.
        await coordinator.device.async_close()
        await async_unregister_schedule_service(hass)
        await async_unregister_solar_dump_service(hass)
    return unload_ok
