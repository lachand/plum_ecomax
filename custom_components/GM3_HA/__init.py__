import logging
import asyncio
from homeassistant.core import HomeAssistant
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PLATFORMS
from .const import DOMAIN, CONF_IP_ADDRESS
from .coordinator import PlumDataUpdateCoordinator
from .plum_device import PlumDevice

_LOGGER = logging.getLogger(__name__)

# On déclare les types d'entités qu'on va créer
PLATFORMS = ["sensor", "climate", "number", "water_heater"]

async def async_setup(hass: HomeAssistant, config: dict):
    """Configuration via YAML (obsolète mais supporté)."""
    return True

async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry):
    """Configuration via l'interface UI."""
    ip = entry.data.get(CONF_IP_ADDRESS, "192.168.1.38") # Fallback IP
    
    _LOGGER.info(f"Initialisation Plum EcoMAX sur {ip}")

    # 1. On initialise notre driver (votre fichier plum_device.py)
    # Assurez-vous que device_map.json est bien copié dans le dossier du composant
    device = PlumDevice(ip, map_file=hass.config.path("custom_components/plum_ecomax/device_map_ecomax360i.json"))
    
    # 2. On charge le mapping (IO bloquante -> dans un thread)
    await asyncio.to_thread(device.load_map)

    # 3. On crée le coordinateur qui va gérer les mises à jour
    coordinator = PlumDataUpdateCoordinator(hass, device)
    
    # 4. Première mise à jour immédiate
    await coordinator.async_config_entry_first_refresh()

    # 5. On stocke tout ça dans Home Assistant
    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = coordinator

    # 6. On lance la création des entités (Sensors, Climates...)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    return True

async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry):
    """Déchargement."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id)
    return unload_ok
