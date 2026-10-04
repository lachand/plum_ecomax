"""Button platform for the Plum EcoMAX integration.

Provides a save/restore mechanism for the "advanced" (EntityCategory.CONFIG)
number parameters -- heating curves, base temperature, night setback,
anti-legionella settings, forced buffer loading -- so a value changed by
mistake (or while experimenting) can be reverted to a known-good snapshot
instead of hunting down the original value by hand.
"""

import logging

from homeassistant.components.button import ButtonEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import PlumConfigEntry
from .const import CONF_ACTIVE_CIRCUITS, DOMAIN
from .coordinator import PlumDataUpdateCoordinator
from .device import boiler_device_info
from .number import active_number_slugs, is_config_category_slug

_LOGGER = logging.getLogger(__name__)

STORAGE_VERSION = 1


def _snapshot_store(hass: HomeAssistant, entry_id: str) -> Store:
    """The on-disk store holding the saved reference values, one per
    config entry (so two boilers never share a snapshot).
    """
    return Store(hass, STORAGE_VERSION, f"{DOMAIN}_{entry_id}_number_defaults")


async def async_setup_entry(
    hass: HomeAssistant, entry: PlumConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Sets up the save/restore-defaults buttons."""
    coordinator = entry.runtime_data
    async_add_entities(
        [
            PlumSaveDefaultsButton(coordinator, entry),
            PlumRestoreDefaultsButton(coordinator, entry),
        ]
    )


class _PlumDefaultsButtonBase(CoordinatorEntity[PlumDataUpdateCoordinator], ButtonEntity):
    """Shared plumbing for the save/restore buttons."""

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator: PlumDataUpdateCoordinator, entry: PlumConfigEntry):
        super().__init__(coordinator)
        self._entry = entry
        self._entry_id = entry.entry_id

    @property
    def device_info(self) -> DeviceInfo:
        return boiler_device_info(self._entry_id, self.coordinator.data.get("uid"))

    def _eligible_slugs(self) -> list:
        """CONFIG-category number slugs currently exposed as entities --
        present on this boiler, and belonging to an active circuit.
        """
        selected_circuits = self._entry.data.get(CONF_ACTIVE_CIRCUITS, [])
        return [
            slug
            for slug in active_number_slugs(self.coordinator.device.params_map, selected_circuits)
            if is_config_category_slug(slug)
        ]


class PlumSaveDefaultsButton(_PlumDefaultsButtonBase):
    """Snapshots the current value of every CONFIG-category number entity
    (heating curves, base temp, night setback, anti-legionella, forced
    buffer loading) as the reference to restore later.
    """

    _attr_translation_key = "save_number_defaults"

    def __init__(self, coordinator: PlumDataUpdateCoordinator, entry: PlumConfigEntry):
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{DOMAIN}_{self._entry_id}_save_number_defaults"

    async def async_press(self) -> None:
        snapshot = {
            slug: self.coordinator.data[slug]
            for slug in self._eligible_slugs()
            if self.coordinator.data.get(slug) is not None
        }
        await _snapshot_store(self.hass, self._entry_id).async_save(snapshot)
        _LOGGER.info(
            "Saved %d parameter(s) as reference values: %s", len(snapshot), sorted(snapshot)
        )


class PlumRestoreDefaultsButton(_PlumDefaultsButtonBase):
    """Writes back every value from the saved snapshot, using the normal
    (now write-confirmed) parameter write path.
    """

    _attr_translation_key = "restore_number_defaults"

    def __init__(self, coordinator: PlumDataUpdateCoordinator, entry: PlumConfigEntry):
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{DOMAIN}_{self._entry_id}_restore_number_defaults"

    async def async_press(self) -> None:
        snapshot = await _snapshot_store(self.hass, self._entry_id).async_load()
        if not snapshot:
            _LOGGER.warning("No reference values have been saved yet -- nothing to restore")
            return

        eligible = set(self._eligible_slugs())
        restored = 0
        for slug, value in snapshot.items():
            if slug not in eligible:
                # e.g. a circuit that was deselected since the snapshot was taken
                continue
            await self.coordinator.async_set_value(slug, value)
            restored += 1
        _LOGGER.info("Restored %d parameter(s) from reference values", restored)
