import logging
import asyncio
import os
from homeassistant.core import HomeAssistant
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_IP_ADDRESS, CONF_PASSWORD, CONF_PORT
from .const import DOMAIN, DEFAULT_PORT
from .coordinator import PlumDataUpdateCoordinator
from .plum_device import PlumDevice

_LOGGER = logging.getLogger(__name__)
PLATFORMS = ["climate", "sensor", "number", "switch", "select", "water_heater"]

async def async_setup(hass: HomeAssistant, config: dict):
    return True

async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry):
    ip = entry.data.get(CONF_IP_ADDRESS)
    port = entry.data.get(CONF_PORT, DEFAULT_PORT)
    password = entry.data.get(CONF_PASSWORD, "0000")
    
    filename = "device_map_ecomax360i.json"
    json_path = hass.config.path(f"custom_components/{DOMAIN}/{filename}")
    
    device = PlumDevice(ip, port=port, password=password, map_file=json_path)
    
    # Chargement du JSON dans un thread pour ne pas bloquer
    await asyncio.to_thread(device.load_map)

    coordinator = PlumDataUpdateCoordinator(hass, device)
    
    # Premier rafraîchissement
    await coordinator.async_config_entry_first_refresh()

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True

async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry):
    if unload_ok := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        hass.data[DOMAIN].pop(entry.entry_id)
    return unload_ok