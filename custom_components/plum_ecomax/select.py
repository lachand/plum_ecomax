import logging
from typing import Any, Dict
from homeassistant.components.select import SelectEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, SELECT_TYPES

_LOGGER = logging.getLogger(__name__)

async def async_setup_entry(
    hass: HomeAssistant,
    entry: Any,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """
    @brief Sets up Plum select entities (Dropdowns).
    """
    coordinator = hass.data[DOMAIN][entry.entry_id]
    entities = []

    # Config format: "slug": ("Name", Map_To_HA, Map_To_Plum)
    for slug, (name, map_to_ha, map_to_plum) in SELECT_TYPES.items():
        if slug in coordinator.device.params_map:
            entities.append(PlumEconetSelect(coordinator, slug, name, map_to_ha, map_to_plum))
        else:
            _LOGGER.debug(f"Select '{slug}' not found in device map, skipping.")

    async_add_entities(entities)

class PlumEconetSelect(CoordinatorEntity, SelectEntity):
    """
    @class PlumEconetSelect
    @brief Representation of a multi-choice parameter (Enum).
    """

    def __init__(self, coordinator, slug: str, name: str, map_to_ha: Dict[int, str], map_to_plum: Dict[str, int]):
        """
        @brief Constructor.
        @param map_to_ha Dictionary mapping Integer (Plum) -> String (Home Assistant).
        @param map_to_plum Dictionary mapping String (Home Assistant) -> Integer (Plum).
        """
        super().__init__(coordinator)
        self._slug = slug
        self._attr_name = name
        self._attr_unique_id = f"{DOMAIN}_{slug}"
        self._attr_has_entity_name = False
        
        self._map_to_ha = map_to_ha
        self._map_to_plum = map_to_plum
        
        # Define available options based on the mapping keys
        self._attr_options = list(map_to_plum.keys())

    @property
    def current_option(self) -> str | None:
        """
        @brief Return the selected entity option to represent the entity state.
        @details Converts the raw integer from device to a readable string.
        """
        raw_val = self.coordinator.data.get(self._slug)
        try:
            raw_int = int(raw_val)
            return self._map_to_ha.get(raw_int)
        except (ValueError, TypeError):
            return None

    async def async_select_option(self, option: str) -> None:
        """
        @brief Change the selected option.
        @details Converts the string back to integer and writes to device.
        """
        target_val = self._map_to_plum.get(option)
        
        if target_val is not None:
            _LOGGER.info(f"Setting {self._attr_name} to {option} (Raw: {target_val})")
            await self.coordinator.async_set_value(self._slug, target_val)
        else:
            _LOGGER.error(f"Invalid option '{option}' for {self._slug}")