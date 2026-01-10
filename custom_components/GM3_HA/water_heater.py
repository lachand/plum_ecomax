import logging
from typing import Any

from homeassistant.components.water_heater import (
    WaterHeaterEntity,
    WaterHeaterEntityFeature,
    STATE_GAS,
    STATE_OFF,
)
from homeassistant.const import UnitOfTemperature, ATTR_TEMPERATURE
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, WATER_HEATER_CONFIG

_LOGGER = logging.getLogger(__name__)

async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Configuration du chauffe-eau."""
    coordinator = hass.data[DOMAIN][entry.entry_id]

    # Vérification : Les paramètres existent-ils dans le mapping ?
    conf = WATER_HEATER_CONFIG
    if (conf["current"] in coordinator.device.params_map and 
        conf["target"] in coordinator.device.params_map):
        
        # On vérifie si on reçoit des données
        if conf["current"] in coordinator.data:
            async_add_entities([PlumEcomaxWaterHeater(coordinator, conf)])
        else:
            _LOGGER.debug("Entité WaterHeater ignorée (pas de données).")

class PlumEcomaxWaterHeater(CoordinatorEntity, WaterHeaterEntity):
    """Contrôle de l'Eau Chaude Sanitaire (ECS/CWU)."""

    _attr_has_entity_name = True
    _attr_temperature_unit = UnitOfTemperature.CELSIUS
    # On déclare qu'on ne supporte que la consigne de température pour l'instant
    _attr_supported_features = WaterHeaterEntityFeature.TARGET_TEMPERATURE

    def __init__(self, coordinator, config):
        super().__init__(coordinator)
        self._config = config
        self._current_slug = config["current"]
        self._target_slug = config["target"]
        self._min_slug = config["min"]
        self._max_slug = config["max"]
        self._entry_id = coordinator.config_entry.entry_id

    @property
    def unique_id(self):
        return f"{DOMAIN}_{self._entry_id}_water_heater"

    @property
    def name(self):
        return self._config["name"]

    @property
    def current_temperature(self):
        """Température actuelle de l'eau."""
        return self.coordinator.data.get(self._current_slug)

    @property
    def target_temperature(self):
        """Consigne actuelle."""
        return self.coordinator.data.get(self._target_slug)

    @property
    def min_temp(self):
        """Limite basse dynamique (lue depuis la chaudière)."""
        val = self.coordinator.data.get(self._min_slug)
        return val if val is not None else 20.0

    @property
    def max_temp(self):
        """Limite haute dynamique (lue depuis la chaudière)."""
        val = self.coordinator.data.get(self._max_slug)
        return val if val is not None else 60.0

    @property
    def current_operation(self):
        """État de fonctionnement (Esthétique)."""
        # On pourrait utiliser 'hdwstate' pour savoir si ça chauffe vraiment.
        # Pour l'instant, si la cible est > 20°C, on considère que c'est actif.
        target = self.target_temperature
        if target and target > 20:
            return STATE_GAS # Affiche "Gaz" ou "Chauffe"
        return STATE_OFF

    async def async_set_temperature(self, **kwargs: Any) -> None:
        """Changement de la consigne ECS."""
        temp = kwargs.get(ATTR_TEMPERATURE)
        if temp is None:
            return

        _LOGGER.info(f"Changement consigne ECS -> {temp}")
        
        # 1. Envoi sécurisé
        success = await self.coordinator.device.set_value(self._target_slug, temp)

        if success:
            # 2. Mise à jour Optimiste (Reflet immédiat dans l'UI)
            self.coordinator.data[self._target_slug] = temp
            self.async_write_ha_state()
        else:
            _LOGGER.error("Échec changement consigne ECS")
            await self.coordinator.async_request_refresh()
