import logging
from typing import Any
from homeassistant.components.switch import SwitchEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, SWITCH_TYPES

_LOGGER = logging.getLogger(__name__)

async def async_setup_entry(
    hass: HomeAssistant,
    entry: Any,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """
    @brief Sets up Plum switch entities.
    """
    coordinator = hass.data[DOMAIN][entry.entry_id]
    entities = []

    for slug, name in SWITCH_TYPES.items():
        # We only create the entity if the parameter exists on the device
        if slug in coordinator.device.params_map:
            entities.append(PlumEconetSwitch(coordinator, slug, name))
        else:
            _LOGGER.debug(f"Switch '{slug}' not found in device map, skipping.")

    async_add_entities(entities)

class PlumEconetSwitch(CoordinatorEntity, SwitchEntity):
    """
    @class PlumEconetSwitch
    @brief Representation of a binary switch (e.g., Force DHW Loading).
    """

    def __init__(self, coordinator, slug: str, name: str):
        """
        @brief Constructor.
        @param coordinator The data coordinator.
        @param slug The parameter slug (e.g., 'hdwstartoneloading').
        @param name The friendly name.
        """
        super().__init__(coordinator)
        self._slug = slug
        self._attr_name = name
        self._attr_unique_id = f"{DOMAIN}_{slug}"
        self._attr_has_entity_name = False

    @property
    def is_on(self) -> bool:
        """
        @brief Return true if the switch is on (value == 1).
        """
        val = self.coordinator.data.get(self._slug)
        try:
            return int(val) == 1
        except (ValueError, TypeError):
            return False

    async def async_turn_on(self, **kwargs: Any) -> None:
        """
        @brief Turn the switch on.
        @details Writes '1' to the device and updates cache immediately.
        """
        _LOGGER.info(f"Turning ON {self._attr_name} ({self._slug})")
        await self.coordinator.async_set_value(self._slug, 1)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """
        @brief Turn the switch off.
        @details Writes '0' to the device and updates cache immediately.
        """
        _LOGGER.info(f"Turning OFF {self._attr_name} ({self._slug})")
        await self.coordinator.async_set_value(self._slug, 0)