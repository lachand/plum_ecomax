import logging
import math
from typing import Any, Optional

from homeassistant.components.water_heater import (
    WaterHeaterEntity,
    WaterHeaterEntityFeature,
    STATE_OFF,
    STATE_ECO,
    STATE_PERFORMANCE,
)
from homeassistant.const import ATTR_TEMPERATURE, UnitOfTemperature, PRECISION_WHOLE
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.helpers.entity import DeviceInfo

from .const import (
    DOMAIN, 
    WATER_HEATER_TYPES, 
    PLUM_TO_HA_WATER_HEATER, 
    HA_TO_PLUM_WATER_HEATER
)

_LOGGER = logging.getLogger(__name__)

async def async_setup_entry(
    hass: HomeAssistant,
    entry: Any,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """
    @brief Configure l'entité Chauffe-Eau (Water Heater).
    """
    coordinator = hass.data[DOMAIN][entry.entry_id]
    entities = []

    _LOGGER.info("🚀 Démarrage de la configuration Water Heater...")

    for key, slugs in WATER_HEATER_TYPES.items():
        current_temp, target_temp, min_temp, max_temp, mode_slug = slugs
        
        has_current = current_temp in coordinator.device.params_map
        has_target = target_temp in coordinator.device.params_map
        
        if has_current and has_target:
            _LOGGER.info(f"✅ Création du Water Heater '{name}' (Paramètres trouvés).")
            entities.append(
                PlumEcomaxWaterHeater(
                    coordinator, 
                    key, 
                    current_temp, target_temp, min_temp, max_temp, mode_slug
                )
            )
        else:
            # Log d'erreur explicite si l'entité n'est pas créée
            _LOGGER.error(
                f"❌ Échec création Water Heater '{name}'. "
                f"Paramètres manquants dans device_map.json : "
                f"Temp='{current_temp}' (Présent={has_current}), "
                f"Consigne='{target_temp}' (Présent={has_target})"
            )

    async_add_entities(entities)


class PlumEcomaxWaterHeater(CoordinatorEntity, WaterHeaterEntity):
    """
    @class PlumEcomaxWaterHeaterReprésente
    @brief  le ballon d'Eau Chaude Sanitaire (ECS).
    """

    _attr_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_precision = PRECISION_WHOLE
    
    # Capacités supportées : Changer la température cible et le mode
    _attr_supported_features = (
        WaterHeaterEntityFeature.TARGET_TEMPERATURE | 
        WaterHeaterEntityFeature.OPERATION_MODE
    )
    
    # Liste des modes supportés (Off, Performance=Manuel, Eco=Auto)
    _attr_operation_list = [STATE_OFF, STATE_PERFORMANCE, STATE_ECO]

    def __init__(self, coordinator, translation_key, current_slug, target_slug, min_slug, max_slug, mode_slug):
        super().__init__(coordinator)
        self._attr_translation_key = translation_key
        self._current_slug = current_slug
        self._target_slug = target_slug
        self._min_slug = min_slug
        self._max_slug = max_slug
        self._mode_slug = mode_slug
        
        self._attr_name = name
        self._attr_unique_id = f"{DOMAIN}_{translation_key}"
        self._attr_has_entity_name = True

    @property
    def device_info(self) -> DeviceInfo:
        """
        @brief Lie cette entité à l'appareil dédié 'Eau Chaude Sanitaire'.
        """
        return DeviceInfo(
            identifiers={(DOMAIN, "plum_hdw")},
            name="Eau Chaude Sanitaire",
            manufacturer="Plum",
            model="Gestionnaire ECS",
        )

    @property
    def current_temperature(self) -> Optional[float]:
        """Retourne la température actuelle avec protection NaN."""
        val = self.coordinator.data.get(self._current_slug)
        if val is None:
            return None
        try:
            f_val = float(val)
            if math.isnan(f_val): return None
            return f_val
        except (ValueError, TypeError):
            return None

    @property
    def target_temperature(self) -> Optional[float]:
        """Retourne la consigne actuelle."""
        val = self.coordinator.data.get(self._target_slug)
        if val is None: return None
        try: return float(val)
        except: return None

    @property
    def min_temp(self) -> float:
        """Récupère la borne Min dynamique (ou 20°C par défaut)."""
        val = self.coordinator.data.get(self._min_slug)
        try: 
            f = float(val)
            if math.isnan(f): return 20.0
            return f
        except: return 20.0

    @property
    def max_temp(self) -> float:
        """Récupère la borne Max dynamique (ou 60°C par défaut)."""
        val = self.coordinator.data.get(self._max_slug)
        try: 
            f = float(val)
            if math.isnan(f): return 60.0
            return f
        except: return 60.0

    @property
    def current_operation(self) -> Optional[str]:
        """Retourne le mode actuel (Off, Performance, Eco)."""
        raw_mode = self.coordinator.data.get(self._mode_slug)
        # Si raw_mode est None (démarrage), on renvoie Off par sécurité
        if raw_mode is None:
            return STATE_OFF
            
        return PLUM_TO_HA_WATER_HEATER.get(raw_mode, STATE_OFF)

    async def async_set_temperature(self, **kwargs) -> None:
        """Définit la nouvelle consigne."""
        temp = kwargs.get(ATTR_TEMPERATURE)
        if temp is None:
            return
        
        _LOGGER.info(f"Changement consigne ECS : {temp}")
        # On convertit en int car Plum attend souvent des entiers pour les consignes
        await self.coordinator.async_set_value(self._target_slug, int(temp))

    async def async_set_operation_mode(self, operation_mode: str) -> None:
        """Change le mode de fonctionnement."""
        target_val = HA_TO_PLUM_WATER_HEATER.get(operation_mode)
        
        if target_val is not None:
            _LOGGER.info(f"Changement mode ECS : {operation_mode} -> {target_val}")
            await self.coordinator.async_set_value(self._mode_slug, target_val)
        else:
            _LOGGER.error(f"Mode ECS inconnu ou non supporté : {operation_mode}")
