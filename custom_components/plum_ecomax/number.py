"""Number platform for the Plum EcoMAX integration.

This module provides number entities for configurable numerical parameters
of the boiler, such as hysteresis, target temperatures (if not covered by climate),
or other adjustable settings defined in `NUMBER_TYPES`.
"""

import logging
import re

from homeassistant.components.number import NumberEntity, NumberMode, RestoreNumber
from homeassistant.const import EntityCategory, UnitOfTemperature, UnitOfTime
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_ACTIVE_CIRCUITS, DOMAIN, NUMBER_TYPES, SOLAR_DUMP_NUMBERS
from .device import boiler_device_info, circuit_device_info, hdw_device_info, mixers_device_info

_SOLAR_DUMP_UNITS = {
    "C": UnitOfTemperature.CELSIUS,
    "K": UnitOfTemperature.KELVIN,
    "min": UnitOfTime.MINUTES,
}

_LOGGER = logging.getLogger(__name__)

_CIRCUIT_SLUG_RE = re.compile(r"^circuit(\d+)")
# Mixer N is physically tied to circuit N on this boiler, so mixer entities
# (none exist in NUMBER_TYPES yet, but see DP_INVENTORY.md) are gated by
# the same active-circuits setting as circuit entities -- see sensor.py's
# async_setup_entry for the same fix, prompted by "mixers_ouverture_vanne_3"
# showing up for a circuit that was never configured or wired up.
_CIRCUIT_OR_MIXER_SLUG_RE = re.compile(r"^(?:circuit|mixer)(\d+)")

# Advanced/rarely-touched settings (heating curves, anti-legionella cycle,
# forced buffer loading, DHW circulation timing): moved out of the main
# "Controls" card into the device's "Configuration" section, so they aren't
# sitting next to everyday controls where they could be nudged by accident.
_ADVANCED_NUMBER_PREFIXES = ("hdwlegion", "buforlongloadtime", "circulation")


def is_config_category_slug(slug: str) -> bool:
    """Whether a NUMBER_TYPES slug is marked EntityCategory.CONFIG.

    Shared with button.py's save/restore-defaults buttons, which operate
    on exactly this set of "advanced" parameters.
    """
    return bool(_CIRCUIT_SLUG_RE.match(slug)) or slug.startswith(_ADVANCED_NUMBER_PREFIXES)


def active_number_slugs(params_map: dict, selected_circuits: list) -> list:
    """NUMBER_TYPES slugs that would actually get an entity: present on
    this boiler, and (for circuit/mixer-numbered ones) belonging to an
    active circuit. Shared with button.py so the save/restore buttons only
    ever touch parameters that are really in use.
    """
    slugs = []
    for slug in NUMBER_TYPES:
        if slug not in params_map:
            continue
        gating_match = _CIRCUIT_OR_MIXER_SLUG_RE.match(slug)
        if gating_match and gating_match.group(1) not in selected_circuits:
            continue
        slugs.append(slug)
    return slugs


async def async_setup_entry(hass, entry, async_add_entities):
    """Sets up Plum EcoMAX number entities.

    Iterates through the `NUMBER_TYPES` configuration and creates an entity
    if the corresponding parameter exists on the device. Per-circuit
    entities (heating curves etc.) are skipped for circuits not selected in
    CONF_ACTIVE_CIRCUITS -- matching sensor.py/climate.py/calendar.py, which
    already do this. Previously this platform ignored the setting entirely
    and created every circuit 1-7's entities regardless of which ones are
    actually wired up.

    Args:
        hass: The Home Assistant instance.
        entry: The configuration entry.
        async_add_entities: Callback to add entities to Home Assistant.
    """
    coordinator = entry.runtime_data
    selected_circuits = entry.data.get(CONF_ACTIVE_CIRCUITS, [])
    entities: list[NumberEntity] = [
        PlumEcomaxNumber(coordinator, entry, slug, NUMBER_TYPES[slug])
        for slug in active_number_slugs(coordinator.device.params_map, selected_circuits)
    ]
    entities.extend(PlumSolarDumpNumber(coordinator, entry, key) for key in SOLAR_DUMP_NUMBERS)
    async_add_entities(entities)


class PlumEcomaxNumber(CoordinatorEntity, NumberEntity):
    """Representation of a Plum EcoMAX numerical parameter.

    This entity allows the user to adjust a numerical value (e.g., setpoint,
    hysteresis) within a defined range (min, max, step). It reflects changes
    immediately in the UI via the coordinator's optimistic update logic.
    """

    _attr_has_entity_name = True

    def __init__(self, coordinator, entry, slug, config):
        """Initializes the number entity.

        Args:
            coordinator: The data update coordinator.
            entry: The config entry.
            slug: The parameter identifier string.
            config: A tuple containing (min, max, step, icon).
        """
        super().__init__(coordinator)
        self._slug = slug

        # --- INDEX CHANGE ---
        self._min_val = config[0]
        self._max_val = config[1]
        self._step_val = config[2]
        self._icon_val = config[3]

        self._entry_id = entry.entry_id
        self._attr_translation_key = slug

        self._circuit_match = _CIRCUIT_SLUG_RE.match(slug)
        if is_config_category_slug(slug):
            self._attr_entity_category = EntityCategory.CONFIG

    @property
    def device_info(self) -> dict:
        """Links the entity to its Circuit, DHW, Mixers, or the main Boiler device.

        Previously every non-"mixer" slug fell through to the generic
        boiler device -- including per-circuit heating curves and DHW
        settings -- which is why they all showed up crammed onto "Plum
        EcoMAX Boiler" instead of their own Circuit N / DHW device.
        """
        if self._circuit_match:
            return circuit_device_info(self._entry_id, int(self._circuit_match.group(1)))
        if self._slug.startswith("mixer"):
            return mixers_device_info(self._entry_id)
        if self._slug.startswith(("hdw", "circulation")):
            return hdw_device_info(self._entry_id)
        return boiler_device_info(self._entry_id, self.coordinator.data.get("uid"))

    @property
    def unique_id(self) -> str:
        """Returns the unique ID of the entity.

        Returns:
            str: The unique identifier.
        """
        return f"{DOMAIN}_{self._entry_id}_number_{self._slug}"

    @property
    def native_value(self):
        """Returns the current value of the number.

        Returns:
            float | None: The current value or None if unavailable.
        """
        val = self.coordinator.data.get(self._slug)
        return float(val) if val is not None else None

    @property
    def native_min_value(self) -> float:
        """Returns the minimum allowed value."""
        return self._min_val

    @property
    def native_max_value(self) -> float:
        """Returns the maximum allowed value."""
        return self._max_val

    @property
    def native_step(self) -> float:
        """Returns the step increment."""
        return self._step_val

    @property
    def icon(self) -> str:
        """Returns the icon for the entity."""
        return self._icon_val

    async def async_set_native_value(self, value: float) -> None:
        """Sets a new value for the entity.

        The value is passed through unchanged: PlumDevice._encode already
        casts per the parameter's wire type (int() for SHORT_INT/WORD/...,
        struct.pack("<f") for FLOAT). Casting to int() here silently
        truncated FLOAT parameters -- notably the per-circuit heating
        curves (circuitNcurvefloor/curveradiator, step 0.1), so setting a
        curve to 1.4 wrote 1.0 to the boiler.

        Args:
            value: The new value to set.
        """
        await self.coordinator.async_set_value(self._slug, value)


class PlumSolarDumpNumber(RestoreNumber):
    """A solar-dump setting (see solar_dump.py) -- NOT a boiler parameter,
    a Home Assistant-local value. Persisted by RestoreNumber across
    restarts and mirrored onto a coordinator attribute so solar_dump.py
    can read it without a fragile entity-id lookup.

    All of them (the two DHW guard rails plus the automatic-mode settings)
    are built from the SOLAR_DUMP_NUMBERS table in const.py.
    """

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.CONFIG
    _attr_mode = NumberMode.BOX

    def __init__(self, coordinator, entry, key: str):
        default, minv, maxv, step, unit, icon, coord_attr = SOLAR_DUMP_NUMBERS[key]
        self._coordinator = coordinator
        self._entry_id = entry.entry_id
        self._key = key
        self._coord_attr = coord_attr
        self._attr_translation_key = f"solar_dump_{key}"
        self._attr_unique_id = f"{DOMAIN}_{entry.entry_id}_number_solar_dump_{key}"
        self._attr_icon = icon
        self._attr_native_unit_of_measurement = _SOLAR_DUMP_UNITS[unit]
        self._attr_native_min_value = minv
        self._attr_native_max_value = maxv
        self._attr_native_step = step
        self._attr_native_value = float(default)

    @property
    def device_info(self) -> dict:
        return boiler_device_info(self._entry_id, self._coordinator.data.get("uid"))

    def _push(self) -> None:
        setattr(self._coordinator, self._coord_attr, self._attr_native_value)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_number_data()
        if last is not None and last.native_value is not None:
            self._attr_native_value = float(last.native_value)
        self._push()

    async def async_set_native_value(self, value: float) -> None:
        self._attr_native_value = float(value)
        self._push()
        self.async_write_ha_state()
