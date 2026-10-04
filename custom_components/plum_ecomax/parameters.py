"""Pure helpers behind the coordinator: validating readings and choosing which
parameters to probe. No Home Assistant, no I/O -- state is passed in."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from .const import (
    ALARM_BITMASK_SLUGS,
    CLIMATE_TYPES,
    DEVICE_INFO_PARAMS,
    MANUAL_MODE_SLUG,
    NUMBER_TYPES,
    SCHEDULE_TYPES,
    SELECT_TYPES,
    SENSOR_TYPES,
    SWITCH_TYPES,
    WATER_HEATER_TYPES,
)

_LOGGER = logging.getLogger(__name__)

# _validate_value's max_delta check (device map "max_delta", currently only
# set for tempwthr, 0.5) rejects a reading that jumps too far from the last
# *accepted* value in one step -- meant to smooth out a single noisy
# reading. Without an escape hatch, a reading that's rejected once is
# compared against that same stale reference forever: if the real value
# keeps moving further away (e.g. overnight cooling), it can never get
# back within max_delta of a value from hours ago, and the entity freezes
# indefinitely. Confirmed against real hardware (2026-08-13/14): tempwthr
# got stuck at a single value for 15+ hours this way, while every other
# entity kept updating normally (the coordinator/connection were fine).
# After this many consecutive rejections, trust the new reading instead of
# the frozen cache -- it's more likely a real change than 3+ cycles of
# noise on the same parameter.
MAX_DELTA_REJECTIONS = 3

# Definitions of physical limits for validation
VALIDATION_RANGES = {
    "temp": (-20, 100.0),
    "power": (0, 100),
    "fan": (0, 100),
    "valveposition": (0, 100),
    "pressure": (0.0, 4.0),
    "lambda": (0.0, 25.0),
}


def validate_value(
    params_map: Mapping[str, Mapping[str, Any]],
    slug: str,
    raw_val: Any,
    cached_val: Any,
    rejection_counts: dict[str, int],
) -> tuple[bool, Any]:
    """Sanitizes the raw value based on JSON limits or Generic constraints.

    Args:
        slug: The parameter identifier.
        raw_val: The raw value received.
        cached_val: The previous value (for delta checking).

    Returns:
        Tuple[bool, Any]: (IsValid, SafeValue).
    """
    # A. Basic protocol checks
    if raw_val is None:
        return False, None

    if isinstance(raw_val, (int, float)) and raw_val in (999, 999.0):
        _LOGGER.debug("Rejection: %s returned sensor error code %s", slug, raw_val)
        return False, None

    param_def: Mapping[str, Any] = params_map.get(slug) or {}
    json_min = param_def.get("min")
    json_max = param_def.get("max")
    json_max_delta = param_def.get("max_delta")

    # B. Specific bounds check (JSON)
    if (json_min is not None or json_max is not None) and isinstance(raw_val, (int, float)):
        # Min/max are physical-plausibility bounds (e.g. a temperature
        # probe can't read -50degC) -- a violation here is never given
        # the max_delta escape hatch below, no matter how many times
        # it repeats.
        if json_min is not None and raw_val < json_min:
            return False, None
        if json_max is not None and raw_val > json_max:
            return False, None

        if (
            json_max_delta is not None
            and cached_val is not None
            and abs(cached_val - raw_val) > json_max_delta
        ):
            rejections = rejection_counts.get(slug, 0) + 1
            if rejections < MAX_DELTA_REJECTIONS:
                rejection_counts[slug] = rejections
                return False, None
            # Escape hatch: this many consecutive rejections means the
            # value probably genuinely moved, not noise -- see
            # MAX_DELTA_REJECTIONS. Falls through to the accepted
            # return below, which also clears the counter.

        rejection_counts.pop(slug, None)
        return True, raw_val

    # C. Generic bounds check (Fallback)
    if isinstance(raw_val, (int, float)):
        for keyword, (min_v, max_v) in VALIDATION_RANGES.items():
            if keyword in slug:
                if not (min_v <= raw_val <= max_v):
                    return False, None
                break

    return True, raw_val


def detection_candidates(params_map: Mapping[str, Any]) -> list[str]:
    """Slugs worth probing at startup: everything an entity reads, present in the map.

    The coordinator only ever polls the slugs this scan detected, so anything an
    entity needs must be listed here (or it is never re-read after the first
    poll). Deduplicated, order preserved.
    """
    targets: list[str] = []
    targets.extend(list(SENSOR_TYPES.keys()))
    for conf in CLIMATE_TYPES.values():
        targets.extend(conf)
    targets.extend(list(NUMBER_TYPES.keys()))
    for wh_conf in WATER_HEATER_TYPES.values():
        targets.extend(wh_conf)
    # Added Schedule types to detection
    targets.extend(list(SCHEDULE_TYPES.keys()))
    # SWITCH_TYPES/SELECT_TYPES were missing here entirely: since
    # _async_update_data() only ever polls self.available_slugs, any
    # switch/select slug not detected here was never re-read after the
    # initial optimistic write, so its state reverted to unknown at the
    # very next poll cycle regardless of the real hardware state.
    targets.extend(list(SWITCH_TYPES.keys()))
    targets.extend(list(SELECT_TYPES.keys()))
    # binary_sensor.py's slugs: same reasoning as the SWITCH_TYPES/
    # SELECT_TYPES fix above -- anything read by an entity has to be in
    # this list or it's never in available_slugs, so _async_update_data
    # never re-polls it after the initial scan.
    targets.append(MANUAL_MODE_SLUG)
    targets.extend(ALARM_BITMASK_SLUGS)
    # Not an entity, but device_info properties read this from
    # coordinator.data (see const.py's DEVICE_INFO_PARAMS docstring).
    targets.extend(DEVICE_INFO_PARAMS)

    # Dedupe while preserving order.
    return [slug for slug in dict.fromkeys(targets) if slug in params_map]
