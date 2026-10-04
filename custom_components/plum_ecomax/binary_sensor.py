"""Binary sensor platform for the Plum EcoMAX integration.

Exposes two families of read-only boolean state, both derived from raw
registers that don't have a dedicated per-state parameter on the wire:

* "Manual mode active" (MANUAL_MODE_SLUG/MANUAL_MODE_BIT in const.py) --
  whether the physical control panel is currently in manual override mode.
  This is the one state that determines whether writes like the
  hdwpumpforce switch actually have any physical effect
  (IMPROVEMENT_PLAN_ARCHIVE.md section H): a switch turned on while the panel isn't
  in manual mode is accepted and held by the boiler, but does nothing.
* Alarm bitmask registers (ALARM_BITMASK_SLUGS in const.py) as
  BinarySensorDeviceClass.PROBLEM -- "some bit is set" rather than decoded
  individual bits, whose meaning isn't documented or empirically verified.
"""

import logging

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import ALARM_BITMASK_SLUGS, DOMAIN, MANUAL_MODE_BIT, MANUAL_MODE_SLUG
from .device import boiler_device_info
from .issues import clear_issue, raise_issue

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Sets up Plum binary sensor entities.

    Args:
        hass: The Home Assistant instance.
        entry: The configuration entry.
        async_add_entities: Callback to add entities to Home Assistant.
    """
    coordinator = entry.runtime_data
    entities = []

    if MANUAL_MODE_SLUG in coordinator.device.params_map:
        entities.append(PlumManualModeBinarySensor(coordinator, entry.entry_id))
    else:
        _LOGGER.debug(
            "'%s' not found in device map, skipping manual mode sensor.",
            MANUAL_MODE_SLUG,
        )

    for slug in ALARM_BITMASK_SLUGS:
        if slug in coordinator.device.params_map:
            entities.append(PlumAlarmBinarySensor(coordinator, entry.entry_id, slug))
        else:
            _LOGGER.debug("Alarm register '%s' not found in device map, skipping.", slug)

    if entities:
        async_add_entities(entities)


class PlumManualModeBinarySensor(CoordinatorEntity, BinarySensorEntity):
    """Whether the boiler's physical control panel is in manual override mode."""

    _attr_has_entity_name = True
    _attr_translation_key = "manual_mode_active"
    _attr_device_class = BinarySensorDeviceClass.RUNNING
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator, entry_id: str):
        super().__init__(coordinator)
        self._entry_id = entry_id
        self._attr_unique_id = f"{DOMAIN}_{entry_id}_binary_sensor_manual_mode_active"

    @property
    def device_info(self) -> DeviceInfo:
        return boiler_device_info(self._entry_id, self.coordinator.data.get("uid"))

    @property
    def is_on(self) -> bool | None:
        val = self.coordinator.data.get(MANUAL_MODE_SLUG)
        try:
            return (int(val) & MANUAL_MODE_BIT) != 0
        except (TypeError, ValueError):
            return None


class PlumAlarmBinarySensor(CoordinatorEntity, BinarySensorEntity):
    """Coarse "some alarm bit is set" indicator for one alarm register.

    Does not decode which specific bit is set -- individual bit meanings
    for these registers aren't documented and haven't been empirically
    verified against the real boiler (unlike MANUAL_MODE_BIT, which was).
    While on, also raises a Home Assistant repair issue so an active alarm
    is visible from Settings -> Repairs, not just an entity state that has
    to be watched.
    """

    _attr_has_entity_name = True
    _attr_device_class = BinarySensorDeviceClass.PROBLEM
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator, entry_id: str, slug: str):
        super().__init__(coordinator)
        self._entry_id = entry_id
        self._slug = slug
        self._attr_translation_key = slug
        self._attr_unique_id = f"{DOMAIN}_{entry_id}_binary_sensor_{slug}"

    @property
    def device_info(self) -> DeviceInfo:
        return boiler_device_info(self._entry_id, self.coordinator.data.get("uid"))

    @property
    def is_on(self) -> bool | None:
        val = self.coordinator.data.get(self._slug)
        try:
            return int(val) != 0
        except (TypeError, ValueError):
            return None

    @property
    def _issue_id(self) -> str:
        return f"alarm_{self._entry_id}_{self._slug}"

    @callback
    def _handle_coordinator_update(self) -> None:
        """Mirror alarm state into a repair issue on every coordinator refresh.

        Creating an already-open issue is a no-op update, and deleting one
        that doesn't exist is a no-op too, so this can run unconditionally
        on every poll without tracking prior state.
        """
        if self.is_on:
            raise_issue(
                self.hass,
                self._issue_id,
                "alarm_active",
                translation_placeholders={"slug": self._slug},
            )
        else:
            clear_issue(self.hass, self._issue_id)
        super()._handle_coordinator_update()

    async def async_will_remove_from_hass(self) -> None:
        """Clear any open repair issue so it doesn't outlive the entity."""
        clear_issue(self.hass, self._issue_id)
        await super().async_will_remove_from_hass()
