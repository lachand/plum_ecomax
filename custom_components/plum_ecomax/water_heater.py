"""Water Heater platform for the Plum EcoMAX integration.

This module handles the Domestic Hot Water (DHW/ECS) tank control.
It allows setting the target temperature, viewing the current temperature,
and switching between operation modes (Off, Manual/Performance, Auto/Eco).
"""

import logging
import math
from typing import Any

from homeassistant.components.water_heater import (
    STATE_ECO,
    STATE_PERFORMANCE,
    WaterHeaterEntity,
    WaterHeaterEntityFeature,
)
from homeassistant.const import ATTR_TEMPERATURE, PRECISION_WHOLE, STATE_OFF, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, HA_TO_PLUM_WATER_HEATER, PLUM_TO_HA_WATER_HEATER, WATER_HEATER_TYPES
from .coordinator import PlumDataUpdateCoordinator
from .device import hdw_device_info

_LOGGER = logging.getLogger(__name__)

# Commands go to a single boiler over one connection: serialize them (the driver
# already orders I/O; this stops Home Assistant from piling up concurrent calls).
PARALLEL_UPDATES = 1


async def async_setup_entry(
    hass: HomeAssistant,
    entry: Any,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Sets up the water heater entity based on the configuration.

    It iterates through the defined `WATER_HEATER_TYPES` and checks if
    the required parameters exist in the device map before creating the entity.

    Args:
        hass: The Home Assistant instance.
        entry: The config entry.
        async_add_entities: Callback to add entities to Home Assistant.
    """
    coordinator = entry.runtime_data
    entities = []

    _LOGGER.info("Starting water heater setup...")

    for key, slugs in WATER_HEATER_TYPES.items():
        current_temp, target_temp, min_temp, max_temp, mode_slug = slugs

        has_current = current_temp in coordinator.device.params_map
        has_target = target_temp in coordinator.device.params_map

        if has_current and has_target:
            _LOGGER.info("Creating Water Heater '%s' (parameters found).", key)
            entities.append(
                PlumEcomaxWaterHeater(
                    coordinator,
                    entry.entry_id,
                    key,
                    current_temp,
                    target_temp,
                    min_temp,
                    max_temp,
                    mode_slug,
                )
            )
        else:
            _LOGGER.error(
                "Failed to create Water Heater '%s'. Missing parameters in "
                "device_map.json: Temp='%s' (present=%s), Target='%s' (present=%s)",
                key,
                current_temp,
                has_current,
                target_temp,
                has_target,
            )

    async_add_entities(entities)


class PlumEcomaxWaterHeater(CoordinatorEntity[PlumDataUpdateCoordinator], WaterHeaterEntity):
    """Representation of the Domestic Hot Water (DHW) tank.

    This entity controls the hot water production. It reads dynamic limits
    (min/max supported temperature) directly from the device controller and
    supports distinct operation modes.
    """

    _attr_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_precision = PRECISION_WHOLE

    # Supported features: Target Temperature and Operation Mode
    _attr_supported_features = (
        WaterHeaterEntityFeature.TARGET_TEMPERATURE | WaterHeaterEntityFeature.OPERATION_MODE
    )

    # Supported modes (Off, Performance=Manual, Eco=Auto)
    _attr_operation_list = [STATE_OFF, STATE_PERFORMANCE, STATE_ECO]

    def __init__(
        self,
        coordinator: PlumDataUpdateCoordinator,
        entry_id: str,
        translation_key: str,
        current_slug: str,
        target_slug: str,
        min_slug: str,
        max_slug: str,
        mode_slug: str,
    ) -> None:
        """Initializes the water heater entity.

        Args:
            coordinator: The data update coordinator.
            entry_id: The config entry ID, used to scope unique_id/device_info.
            translation_key: The translation key (used as name).
            current_slug: Slug for current temperature sensor.
            target_slug: Slug for target temperature parameter.
            min_slug: Slug for minimum allowed temperature.
            max_slug: Slug for maximum allowed temperature.
            mode_slug: Slug for operation mode.
        """
        super().__init__(coordinator)
        self._entry_id = entry_id
        self._attr_translation_key = translation_key
        self._current_slug = current_slug
        self._target_slug = target_slug
        self._min_slug = min_slug
        self._max_slug = max_slug
        self._mode_slug = mode_slug

        self._attr_unique_id = f"{DOMAIN}_{entry_id}_{translation_key}"
        self._attr_has_entity_name = True

    @property
    def device_info(self) -> DeviceInfo:
        """Links this entity to the dedicated DHW device.

        Returns:
            DeviceInfo: The device info dictionary.
        """
        return hdw_device_info(self._entry_id)

    @property
    def current_temperature(self) -> float | None:
        """Returns the current water temperature.

        Includes checks for NaN (Not a Number) to prevent errors.

        Returns:
            float | None: Current temperature or None if invalid.
        """
        val = self.coordinator.data.get(self._current_slug)
        if val is None:
            return None
        try:
            f_val = float(val)
            if math.isnan(f_val):
                return None
            return f_val
        except (ValueError, TypeError):
            return None

    @property
    def target_temperature(self) -> float | None:
        """Returns the current target temperature setpoint.

        Returns:
            float | None: The setpoint.
        """
        val = self.coordinator.data.get(self._target_slug)
        if val is None:
            return None
        try:
            return float(val)
        except (ValueError, TypeError):
            return None

    @property
    def min_temp(self) -> float:
        """Returns the minimum supported temperature.

        Fetches the limit dynamically from the device. Falls back to 20.0
        if the value is unavailable or NaN.

        Returns:
            float: The limit value.
        """
        val: Any = self.coordinator.data.get(self._min_slug)
        try:
            f = float(val)
            if math.isnan(f):
                return 20.0
            return f
        except (ValueError, TypeError):
            return 20.0

    @property
    def max_temp(self) -> float:
        """Returns the maximum supported temperature.

        Fetches the limit dynamically from the device. Falls back to 60.0
        if the value is unavailable or NaN.

        Returns:
            float: The limit value.
        """
        val: Any = self.coordinator.data.get(self._max_slug)
        try:
            f = float(val)
            if math.isnan(f):
                return 60.0
            return f
        except (ValueError, TypeError):
            return 60.0

    @property
    def current_operation(self) -> str | None:
        """Returns the current operation mode.

        Maps internal Plum codes to Home Assistant states:
        - 0 -> Off
        - 1 -> Performance (Manual)
        - 2 -> Eco (Auto/Schedule)

        Returns:
            str: The current mode (STATE_OFF, STATE_PERFORMANCE, or STATE_ECO).
        """
        raw_mode = self.coordinator.data.get(self._mode_slug)
        # If raw_mode is None (startup), return Off for safety
        if raw_mode is None:
            return STATE_OFF

        return PLUM_TO_HA_WATER_HEATER.get(raw_mode, STATE_OFF)

    async def async_set_temperature(self, **kwargs: Any) -> None:
        """Sets the water target temperature.

        Args:
            **kwargs: Arguments containing ATTR_TEMPERATURE.
        """
        temp = kwargs.get(ATTR_TEMPERATURE)
        if temp is None:
            return

        _LOGGER.info("Setting DHW target: %s", temp)
        # Convert to int as Plum usually expects integers for setpoints
        await self.coordinator.async_set_value(self._target_slug, int(temp))

    async def async_set_operation_mode(self, operation_mode: str) -> None:
        """Sets the operation mode.

        Args:
            operation_mode: The desired mode (e.g., "performance", "eco").
        """
        target_val = HA_TO_PLUM_WATER_HEATER.get(operation_mode)

        if target_val is not None:
            _LOGGER.info("Setting DHW mode: %s -> %s", operation_mode, target_val)
            await self.coordinator.async_set_value(self._mode_slug, target_val)
        else:
            _LOGGER.error("Unknown or unsupported DHW mode: %s", operation_mode)
