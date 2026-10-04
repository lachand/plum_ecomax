"""Switch platform for the Plum EcoMAX integration.

This module handles binary switch entities. These are typically used for
boolean parameters or simple on/off commands on the boiler, such as
forcing the Domestic Hot Water (DHW) heating cycle.
"""

import logging
from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import STATE_ON, EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONFIG_SWITCHES, DOMAIN, SWITCH_TYPES
from .device import boiler_device_info, hdw_device_info
from .solar_dump import (
    async_auto_disable,
    async_auto_enable,
    async_start_hold,
    async_stop_for_entry,
)

HDW_SWITCHES = {"hdwstartoneloading", "hdwpumpforce", "hdwstartlegion"}

# This switch doesn't just write a parameter -- the boiler ignores the DHW
# pump force unless it's in manual mode. Turning it on enters manual mode
# then forces the pump; turning it off stops the pump and (if we entered it)
# leaves manual mode. Handled by solar_dump, which also guarantees the
# return to automatic on reload / HA shutdown.
MANUAL_MODE_BACKED = "hdwpumpforce"

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Sets up Plum switch entities.

    Iterates through the `SWITCH_TYPES` configuration and creates an entity
    if the corresponding parameter exists on the device.

    Args:
        hass: The Home Assistant instance.
        entry: The configuration entry.
        async_add_entities: Callback to add entities to Home Assistant.
    """
    coordinator = entry.runtime_data
    entities = []

    for slug, cfg in SWITCH_TYPES.items():
        name, on_value, off_value = cfg
        # We only create the entity if the parameter exists on the device
        if slug in coordinator.device.params_map:
            entities.append(
                PlumEconetSwitch(coordinator, entry.entry_id, slug, name, on_value, off_value)
            )
        else:
            _LOGGER.debug("Switch '%s' not found in device map, skipping.", slug)

    entities.append(PlumSolarDumpAutoSwitch(coordinator, entry.entry_id))
    async_add_entities(entities)


class PlumEconetSwitch(CoordinatorEntity, SwitchEntity):
    """Representation of a binary switch.

    This entity represents a writable boolean parameter on the device.
    It uses the data coordinator to read the current state and write
    changes back to the device (e.g., setting a value of 1 for On and 0 for Off).
    """

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator,
        entry_id: str,
        slug: str,
        name: str,
        on_value: int = 1,
        off_value: int = 0,
    ):
        super().__init__(coordinator)
        self._entry_id = entry_id
        self._slug = slug
        self._log_name = name
        self._on_value = on_value
        self._off_value = off_value
        self._attr_translation_key = slug
        self._attr_unique_id = f"{DOMAIN}_{entry_id}_switch_{slug}"

    @property
    def entity_category(self) -> EntityCategory | None:
        if self._slug in CONFIG_SWITCHES:
            return EntityCategory.CONFIG
        return None

    @property
    def icon(self) -> str | None:
        if self._slug == "operatingmode":
            return "mdi:hand-back-right" if self.is_on else "mdi:cog-play"
        return None

    @property
    def device_info(self) -> DeviceInfo | None:
        if self._slug in HDW_SWITCHES:
            return hdw_device_info(self._entry_id)
        return boiler_device_info(self._entry_id, self.coordinator.data.get("uid"))

    @property
    def is_on(self) -> bool:
        val = self.coordinator.data.get(self._slug)
        try:
            return int(val) == self._on_value
        except (ValueError, TypeError):
            return False

    async def async_turn_on(self, **kwargs: Any) -> None:
        _LOGGER.info("Turning ON %s (%s) -> %s", self._log_name, self._slug, self._on_value)
        if self._slug == MANUAL_MODE_BACKED:
            await async_start_hold(self.coordinator.hass, self.coordinator, self._entry_id)
            return
        await self.coordinator.async_set_value(self._slug, self._on_value)

    async def async_turn_off(self, **kwargs: Any) -> None:
        _LOGGER.info("Turning OFF %s (%s) -> %s", self._log_name, self._slug, self._off_value)
        if self._slug == MANUAL_MODE_BACKED:
            await async_stop_for_entry(self.coordinator.hass, self.coordinator)
            return
        await self.coordinator.async_set_value(self._slug, self._off_value)


class PlumSolarDumpAutoSwitch(CoordinatorEntity, RestoreEntity, SwitchEntity):
    """Automatic solar-dump mode: a dT controller that runs the transfer in
    bursts (see solar_dump.py). HA-local state, persisted across restarts.
    """

    _attr_has_entity_name = True
    _attr_translation_key = "solar_dump_auto"
    _attr_icon = "mdi:sun-clock"

    def __init__(self, coordinator, entry_id: str):
        super().__init__(coordinator)
        self._entry_id = entry_id
        self._is_on = False
        self._attr_unique_id = f"{DOMAIN}_{entry_id}_switch_solar_dump_auto"

    @property
    def device_info(self) -> DeviceInfo | None:
        return boiler_device_info(self._entry_id, self.coordinator.data.get("uid"))

    @property
    def is_on(self) -> bool:
        return self._is_on

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last is not None and last.state == STATE_ON:
            self._is_on = True
            await async_auto_enable(self.coordinator.hass, self.coordinator, self._entry_id)

    async def async_turn_on(self, **kwargs: Any) -> None:
        self._is_on = True
        self.async_write_ha_state()
        await async_auto_enable(self.coordinator.hass, self.coordinator, self._entry_id)

    async def async_turn_off(self, **kwargs: Any) -> None:
        self._is_on = False
        self.async_write_ha_state()
        await async_auto_disable(self.coordinator.hass, self.coordinator)
