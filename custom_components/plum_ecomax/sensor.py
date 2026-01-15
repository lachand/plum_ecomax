import logging
import re
import math  # <--- CRITICAL: Import required for NaN checks
from homeassistant.components.sensor import SensorEntity, SensorStateClass
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from .const import DOMAIN, SENSOR_TYPES, CONF_ACTIVE_CIRCUITS

_LOGGER = logging.getLogger(__name__)

async def async_setup_entry(hass, entry, async_add_entities):
    """
    @brief Sets up sensor entities based on the configuration.
    """
    coordinator = hass.data[DOMAIN][entry.entry_id]
    selected_circuits = entry.data.get(CONF_ACTIVE_CIRCUITS, [])
    entities = []

    for slug, config in SENSOR_TYPES.items():
        # Skip if the parameter is not present on the device
        if slug not in coordinator.device.params_map:
            continue

        target_circuit_id = None
        # Automatic circuit detection via Regex (e.g., tempcircuit1 -> 1)
        match = re.search(r'(circuit|mixer)(\d+)', slug)
        
        if match:
            found_id = match.group(2)
            # If it's a circuit sensor, verify if this circuit is enabled in config
            if found_id in selected_circuits:
                target_circuit_id = found_id
            else:
                continue 

        entities.append(PlumEcomaxSensor(coordinator, entry, slug, config, target_circuit_id))

    if entities:
        async_add_entities(entities)

class PlumEcomaxSensor(CoordinatorEntity, SensorEntity):
    """
    @class PlumEcomaxSensor
    @brief Represents a Plum sensor entity with NaN protection.
    """
    
    # --- SAFETY NOTE: Translation key disabled to prevent UndefinedError ---
    # Uncomment these lines only if you have a valid strings.json file.
    # _attr_has_entity_name = True 
    # _attr_translation_key = slug

    def __init__(self, coordinator, entry, slug, config, circuit_id=None):
        """
        @brief Constructor.
        @param config Tuple containing (unit, icon, device_class).
        """
        super().__init__(coordinator)
        self._slug = slug
        
        # Unpack configuration from const.py
        self._unit = config[0]
        self._icon = config[1]
        self._device_class = config[2]
        
        self._entry_id = entry.entry_id
        self._circuit_id = circuit_id
        
        # Fallback name if translation is disabled
        # Uses the original name from device map or the slug
        original_name = coordinator.device.params_map.get(slug, {}).get("name", slug)
        self._attr_name = original_name

    @property
    def unique_id(self):
        return f"{DOMAIN}_{self._entry_id}_{self._slug}"

    @property
    def native_value(self):
        """
        @brief Returns the sensor value.
        @details CRITICAL FIX: Filters out 'NaN' (Not a Number) values to prevent HA crash.
        """
        val = self.coordinator.data.get(self._slug)
        
        if val is None:
            return None

        # If a Device Class or Unit is defined, we expect a number
        if self._device_class or self._unit:
            try:
                f_val = float(val)
                # Check if value is NaN or Infinite -> Return None (Unavailable)
                if math.isnan(f_val) or math.isinf(f_val):
                    return None
                return f_val
            except (ValueError, TypeError):
                # Conversion failed but a number was expected -> Return None
                return None
        
        # For text sensors, return the value as is
        return val

    @property
    def available(self) -> bool:
        """
        @brief checks availability.
        @details Returns False if data is missing or NaN.
        """
        val = self.coordinator.data.get(self._slug)
        if val is None:
            return False
        
        # If it's a number, check for NaN
        if isinstance(val, float) and math.isnan(val):
            return False
            
        return super().available

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
        # Only set state_class for numeric sensors
        if self._device_class or self._unit:
            return SensorStateClass.MEASUREMENT
        return None

    @property
    def device_info(self):
        """
        @brief Links the sensor to the correct device (Boiler or specific Circuit).
        """
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
                "name": "Plum EcoMAX Boiler",
                "manufacturer": "Plum",
            }