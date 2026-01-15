import logging
from homeassistant.components.climate import (
    ClimateEntity,
    ClimateEntityFeature,
    HVACMode,
)
from homeassistant.const import UnitOfTemperature, ATTR_TEMPERATURE
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, CONF_ACTIVE_CIRCUITS

_LOGGER = logging.getLogger(__name__)

async def async_setup_entry(hass, entry, async_add_entities):
    coordinator = hass.data[DOMAIN][entry.entry_id]
    selected_circuits = entry.data.get(CONF_ACTIVE_CIRCUITS, [])
    entities = []

    for circuit_id in selected_circuits:
        current_slug = f"circuit{circuit_id}thermostattemp"
        target_slug = f"circuit{circuit_id}comforttemp"
        active_slug = f"circuit{circuit_id}active"
        
        # Fallback sonde
        if current_slug not in coordinator.device.params_map:
             current_slug = f"tempcircuit{circuit_id}"

        if target_slug in coordinator.device.params_map:
             entities.append(PlumEcomaxClimate(
                 coordinator, entry, circuit_id, current_slug, target_slug, active_slug
            ))

    if entities:
        async_add_entities(entities)

class PlumEcomaxClimate(CoordinatorEntity, ClimateEntity):
    _attr_has_entity_name = True
    _attr_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_supported_features = ClimateEntityFeature.TARGET_TEMPERATURE
    _attr_hvac_modes = [HVACMode.OFF, HVACMode.HEAT]
    
    # CLÉ DE TRADUCTION
    _attr_translation_key = "thermostat"

    def __init__(self, coordinator, entry, circuit_id, current_slug, target_slug, active_slug):
        super().__init__(coordinator)
        self._circuit_id = circuit_id
        self._entry_id = entry.entry_id
        self._current_slug = current_slug
        self._target_slug = target_slug
        self._active_slug = active_slug

    @property
    def unique_id(self):
        return f"{DOMAIN}_{self._entry_id}_circuit_{self._circuit_id}_climate"

    @property
    def device_info(self):
        return {
            "identifiers": {(DOMAIN, f"{self._entry_id}_circuit_{self._circuit_id}")},
            "name": f"Circuit {self._circuit_id}",
            "manufacturer": "Plum",
            "model": "Module Chauffage",
            "via_device": (DOMAIN, self._entry_id),
        }

    # IMPORTANT: On ne définit PAS def name(self) pour laisser HA gérer la trad "Circuit X Thermostat"

    @property
    def min_temp(self): return 10.0
    @property
    def max_temp(self): return 30.0
    @property
    def target_temperature_step(self): return 0.5

    @property
    def current_temperature(self):
        val = self.coordinator.data.get(self._current_slug)
        return float(val) if val is not None else None

    @property
    def target_temperature(self):
        val = self.coordinator.data.get(self._target_slug)
        if val is None: return 20.0 
        return float(val)

    @property
    def hvac_mode(self):
        is_active = self.coordinator.data.get(self._active_slug)
        if is_active == 0: return HVACMode.OFF
        return HVACMode.HEAT

    async def async_set_hvac_mode(self, hvac_mode):
        value = 1 if hvac_mode == HVACMode.HEAT else 0
        if await self.coordinator.device.set_value(self._active_slug, value):
            self.coordinator.data[self._active_slug] = value
            self.async_write_ha_state()

    async def async_set_temperature(self, **kwargs):
        temp = kwargs.get(ATTR_TEMPERATURE)
        if temp is None: return
        if self.hvac_mode == HVACMode.OFF:
            await self.async_set_hvac_mode(HVACMode.HEAT)
        
        if await self.coordinator.device.set_value(self._target_slug, temp):
            self.coordinator.data[self._target_slug] = temp
            self.async_write_ha_state()
        else:
            await self.coordinator.async_request_refresh()