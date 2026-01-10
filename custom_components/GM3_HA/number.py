import logging
from homeassistant.components.number import NumberEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from .const import DOMAIN, NUMBER_TYPES

_LOGGER = logging.getLogger(__name__)

async def async_setup_entry(hass, entry, async_add_entities):
    coordinator = hass.data[DOMAIN][entry.entry_id]
    entities = []
    for slug, config in NUMBER_TYPES.items():
        if slug in coordinator.device.params_map:
            entities.append(PlumEcomaxNumber(coordinator, entry, slug, config))
    if entities:
        async_add_entities(entities)

class PlumEcomaxNumber(CoordinatorEntity, NumberEntity):
    _attr_has_entity_name = True 

    def __init__(self, coordinator, entry, slug, config):
        super().__init__(coordinator)
        self._slug = slug
        
        # --- CHANGEMENT INDEX ---
        self._min_val = config[0]
        self._max_val = config[1]
        self._step_val = config[2]
        self._icon_val = config[3]
        
        self._entry_id = entry.entry_id
        self._attr_translation_key = slug

    @property
    def unique_id(self):
        return f"{DOMAIN}_{self._entry_id}_number_{self._slug}"

    @property
    def native_value(self):
        val = self.coordinator.data.get(self._slug)
        return float(val) if val is not None else None

    @property
    def native_min_value(self): return self._min_val
    @property
    def native_max_value(self): return self._max_val
    @property
    def native_step(self): return self._step_val
    @property
    def icon(self): return self._icon_val

    async def async_set_native_value(self, value: float) -> None:
        if await self.coordinator.device.set_value(self._slug, int(value)):
            self.coordinator.data[self._slug] = value
            self.async_write_ha_state()
        else:
            await self.coordinator.async_request_refresh()