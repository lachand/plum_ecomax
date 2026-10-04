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

from .const import CONF_UPDATE_INTERVAL, DEFAULT_PORT, DOMAIN, UPDATE_INTERVAL
from .coordinator import PlumDataUpdateCoordinator
from .plum_device import PlumDevice
from .schedule import async_register_services as async_register_schedule_service
from .schedule import async_unregister_services as async_unregister_schedule_service
from .solar_dump import async_register_services as async_register_solar_dump_service
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


async def async_setup(hass: HomeAssistant, config: dict):
    """Set up the Plum EcoMAX component.

    Args:
        hass: The Home Assistant instance.
        config: The configuration dictionary.

    Returns:
        bool: Always True (configuration is handled via Config Flow).
    """
    return True


async def async_setup_entry(hass: HomeAssistant, entry: PlumConfigEntry):
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

    filename = "device_map_ecomax360i.json"
    json_path = hass.config.path(f"custom_components/{DOMAIN}/{filename}")

    device = PlumDevice(ip, port=port, password=password, map_file=json_path)

    try:
        await asyncio.to_thread(device.load_map)
    except Exception as err:
        raise ConfigEntryNotReady(f"Could not load parameter map {json_path}: {err}") from err

    update_interval = entry.data.get(CONF_UPDATE_INTERVAL, UPDATE_INTERVAL)
    coordinator = PlumDataUpdateCoordinator(hass, device, entry, update_interval=update_interval)

    await coordinator.async_config_entry_first_refresh()

    entry.runtime_data = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    await async_register_schedule_service(hass)
    await async_register_solar_dump_service(hass)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: PlumConfigEntry):
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
        await async_stop_solar_dump_auto(hass, entry.entry_id)
        await async_stop_solar_dump(hass, entry.entry_id)
        coordinator = entry.runtime_data
        # PlumDevice now keeps its TCP connection open across transactions
        # (persistent connection) instead of closing it after every one --
        # tear it down explicitly here so a reload/removal doesn't leak an
        # open socket until garbage collection gets around to it.
        await asyncio.to_thread(coordinator.device.close)
        await async_unregister_schedule_service(hass)
        await async_unregister_solar_dump_service(hass)
    return unload_ok
