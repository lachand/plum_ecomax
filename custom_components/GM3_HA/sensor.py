import logging
import re
from homeassistant.components.sensor import SensorEntity, SensorStateClass
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from .const import DOMAIN, SENSOR_TYPES, CONF_ACTIVE_CIRCUITS

_LOGGER = logging.getLogger(__name__)

async def async_setup_entry(hass, entry, async_add_entities):
    coordinator = hass.data[DOMAIN][entry.entry_id]
    selected_circuits = entry.data.get(CONF_ACTIVE_CIRCUITS, [])
    entities = []

    for slug, config in SENSOR_TYPES.items():
        if slug not in coordinator.device.params_map:
            continue

        target_circuit_id = None
        match = re.search(r'(circuit|mixer)(\d+)', slug)
        
        if match:
            found_id = match.group(2)
            if found_id in selected_circuits:
                target_circuit_id = found_id
            else:
                continue 

        entities.append(PlumEcomaxSensor(coordinator, entry, slug, config, target_circuit_id))

    if entities:
        async_add_entities(entities)

class PlumEcomaxSensor(CoordinatorEntity, SensorEntity):
    _attr_has_entity_name = True # Active la traduction

    def __init__(self, coordinator, entry, slug, config, circuit_id=None):
        super().__init__(coordinator)
        self._slug = slug
        
        # --- CHANGEMENT DES INDEX ICI ---
        # config[0] est maintenant l'unité (avant c'était config[1])
        self._unit = config[0]
        self._icon = config[1]
        self._device_class = config[2]
        
        self._entry_id = entry.entry_id
        self._circuit_id = circuit_id
        
        # On lie à la clé de traduction
        self._attr_translation_key = slug

    @property
    def unique_id(self):
        return f"{DOMAIN}_{self._entry_id}_{self._slug}"

    @property
    def native_value(self):
        return self.coordinator.data.get(self._slug)

    @property
    def native_unit_of_measurement(self):
        return self._unit

    @property
    def icon(self):
        return self._icon

    @property
    def device_class(self):
        return self._device_class

    @property
    def state_class(self):
        return SensorStateClass.MEASUREMENT

    @property
    def device_info(self):
        if self._circuit_id:
            return {
                "identifiers": {(DOMAIN, f"{self._entry_id}_circuit_{self._circuit_id}")},
                "name": f"Circuit {self._circuit_id}",
                "manufacturer": "Plum",
                "via_device": (DOMAIN, self._entry_id),
            }
        else:
            return {
                "identifiers": {(DOMAIN, self._entry_id)},
                "name": "Chaudière Plum EcoMAX",
                "manufacturer": "Plum",
            }