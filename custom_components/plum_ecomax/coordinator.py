"""Data Update Coordinator for Plum EcoMAX.

This module provides the central data management logic for the integration.
It handles polling, caching, validation, and a robust "fire-and-forget"
write strategy to ensure commands reach the device despite network latency.
"""

import asyncio
import logging
import time
from datetime import timedelta

# Conditional import for typing only
from typing import TYPE_CHECKING, Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

if TYPE_CHECKING:
    from .plum_device import PlumDevice

from .const import (
    ALARM_BITMASK_SLUGS,
    CLIMATE_TYPES,
    DEVICE_INFO_PARAMS,
    DOMAIN,
    MANUAL_MODE_SLUG,
    NUMBER_TYPES,
    SCHEDULE_TYPES,
    SELECT_TYPES,
    SENSOR_TYPES,
    SOLAR_DUMP_AUTO_FLOOR_DEFAULT,
    SOLAR_DUMP_BUFFER_TARGET_DEFAULT,
    SOLAR_DUMP_DAILY_BUDGET_DEFAULT,
    SOLAR_DUMP_DT_START_DEFAULT,
    SOLAR_DUMP_START_TEMP_DEFAULT,
    SOLAR_DUMP_STOP_TEMP_DEFAULT,
    STATIC_SLUGS,
    SWITCH_TYPES,
    UPDATE_INTERVAL,
    WATER_HEATER_TYPES,
)
from .issues import clear_issue, raise_issue

_LOGGER = logging.getLogger(__name__)

DEFAULT_TTL = 300

# _async_update_data only actually attempts a transaction for slugs whose
# per-slug cache entry has gone stale (see the fetch-splitting logic
# below) -- with DEFAULT_TTL=300s vs. the 30s default poll interval, a
# quiet cycle can go by without any transaction being attempted at all, so
# "3 consecutive failures" can take a few minutes of wall-clock time to
# reach in the worst case, not 3 poll cycles. Acceptable for a heating
# comfort integration (not a safety-critical one) but worth being honest
# about instead of implying near-instant detection.
CONNECTION_LOST_THRESHOLD = 3

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


class PlumDataUpdateCoordinator(DataUpdateCoordinator):
    """Centralized data management with Robust Data Validation.

    Implements caching, write-through strategies, and data sanitization
    to prevent outliers from polluting the state machine.
    """

    # True while the boiler is unreachable -- lets the outage be logged once
    # on the way down and once on recovery instead of every cycle.
    _link_down: bool = False

    def __init__(
        self,
        hass: HomeAssistant,
        device: "PlumDevice",
        config_entry: ConfigEntry,
        update_interval: int = UPDATE_INTERVAL,
    ):
        """Initializes the coordinator.

        Args:
            hass: Home Assistant core instance.
            device: The low-level PlumDevice instance.
            config_entry: The config entry this coordinator belongs to.
                Passed explicitly to DataUpdateCoordinator (rather than
                relying on the current_entry ContextVar fallback), and its
                entry_id scopes repair issue ids so two boilers never
                collide (same reasoning as device.py's device_info helpers).
            update_interval: Polling interval in seconds. Defaults to
                UPDATE_INTERVAL, overridable per config entry (see
                CONF_UPDATE_INTERVAL in const.py). Also the effective
                freshness of live telemetry -- see STATIC_SLUGS / the
                per-slug TTL in _async_update_data.
        """
        self.device = device
        self.entry_id = config_entry.entry_id
        self.available_slugs: list[str] = []

        # Solar-dump settings for solar_dump.py. Seeded with the defaults so
        # a dump/burst started before the number entities restore still has
        # real values; PlumSolarDumpNumber (number.py) overwrites these on
        # restore and on every user change.
        self.solar_dump_start_temp: float = SOLAR_DUMP_START_TEMP_DEFAULT
        self.solar_dump_stop_temp: float = SOLAR_DUMP_STOP_TEMP_DEFAULT
        self.solar_dump_auto_ecs_floor: float = SOLAR_DUMP_AUTO_FLOOR_DEFAULT
        self.solar_dump_buffer_target: float = SOLAR_DUMP_BUFFER_TARGET_DEFAULT
        self.solar_dump_dt_start: float = SOLAR_DUMP_DT_START_DEFAULT
        self.solar_dump_daily_budget: float = SOLAR_DUMP_DAILY_BUDGET_DEFAULT

        # Cache System
        self._cache: dict[str, Any] = {}
        self._timestamps: dict[str, float] = {}
        self._cache_lock = asyncio.Lock()
        # TTL for slugs in STATIC_SLUGS (setpoints, curves, schedules,
        # names): long, they don't change on their own. Live telemetry
        # (everything else) is re-read every cycle -- see _async_update_data.
        self.ttl = DEFAULT_TTL
        # Consecutive max_delta rejections per slug -- see
        # MAX_DELTA_REJECTIONS above for why this exists.
        self._delta_rejection_counts: dict[str, int] = {}

        super().__init__(
            hass,
            _LOGGER,
            config_entry=config_entry,
            name=DOMAIN,
            update_interval=timedelta(seconds=update_interval),
        )

    async def _async_update_data(self) -> dict[str, Any]:
        """Main update loop with Validation and Fallback.

        Returns:
            dict: The validated data.
        """
        data = {}
        now = time.time()

        if not self.available_slugs:
            await self._detect_available_parameters()

        # 1. Split into cache hits vs. slugs that need a fresh read.
        # STATIC_SLUGS (setpoints, curves, schedules, names) get the long
        # TTL; everything else is live telemetry and re-read every cycle
        # (TTL 0), so the configured polling interval is what actually
        # bounds how stale a temperature can get.
        to_fetch = []
        async with self._cache_lock:
            for slug in self.available_slugs:
                ttl = self.ttl if slug in STATIC_SLUGS else 0
                last_update = self._timestamps.get(slug, 0)
                cached_val = self._cache.get(slug)
                if (now - last_update) < ttl and cached_val is not None:
                    data[slug] = cached_val
                else:
                    to_fetch.append(slug)

        # 2. Batch-fetch everything stale in as few frames as possible
        # (spec 1.5.3.12 allows several parameter blocks per request), instead
        # of one TCP connection per parameter.
        raw_values: dict[str, Any] = {}
        read_error: Exception | None = None
        if to_fetch:
            try:
                raw_values = await self.device.get_values(to_fetch, retries=2)
            except Exception as e:
                read_error = e

            # Nothing usable came back at all: the link is down, not just a
            # few implausible readings. Surface it so entities go
            # unavailable (and first refresh raises ConfigEntryNotReady)
            # rather than serving stale cache indefinitely.
            if read_error is not None or not any(v is not None for v in raw_values.values()):
                self._update_connection_issue()
                if not self._link_down:
                    _LOGGER.warning(
                        "Lost communication with the boiler (%d parameters unread): %s",
                        len(to_fetch),
                        read_error or "no valid response",
                    )
                self._link_down = True
                raise UpdateFailed("Boiler unreachable") from read_error
        if self._link_down:
            _LOGGER.info("Communication with the boiler restored")
            self._link_down = False

        # 3. Validate & fall back per-slug
        for slug in to_fetch:
            async with self._cache_lock:
                cached_val = self._cache.get(slug)

            raw_val = raw_values.get(slug)
            is_valid, final_val = self._validate_value(slug, raw_val, cached_val)

            if is_valid:
                # Valid new data: Update cache
                async with self._cache_lock:
                    self._cache[slug] = final_val
                    self._timestamps[slug] = time.time()
                data[slug] = final_val
            else:
                # Invalid data: Use fallback (Hold Last State)
                if cached_val is not None:
                    data[slug] = cached_val

        self._update_connection_issue()
        return data

    def _update_connection_issue(self) -> None:
        """Raises/clears the "connection lost" repair issue based on
        PlumDevice.consecutive_failures. Runs unconditionally every cycle
        (not gated on whether this cycle actually attempted a transaction)
        so a past outage's issue stays open until the count actually
        recovers, and a healthy device never has a stale issue lingering.
        """
        issue_id = f"connection_lost_{self.entry_id}"
        if self.device.consecutive_failures >= CONNECTION_LOST_THRESHOLD:
            raise_issue(
                self.hass,
                issue_id,
                "connection_lost",
                severity=ir.IssueSeverity.ERROR,
                translation_placeholders={"count": str(self.device.consecutive_failures)},
            )
        else:
            clear_issue(self.hass, issue_id)

    def _validate_value(self, slug: str, raw_val: Any, cached_val: Any) -> tuple[bool, Any]:
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

        param_def = self.device.params_map.get(slug, {})
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
                rejections = self._delta_rejection_counts.get(slug, 0) + 1
                if rejections < MAX_DELTA_REJECTIONS:
                    self._delta_rejection_counts[slug] = rejections
                    return False, None
                # Escape hatch: this many consecutive rejections means the
                # value probably genuinely moved, not noise -- see
                # MAX_DELTA_REJECTIONS. Falls through to the accepted
                # return below, which also clears the counter.

            self._delta_rejection_counts.pop(slug, None)
            return True, raw_val

        # C. Generic bounds check (Fallback)
        if isinstance(raw_val, (int, float)):
            for keyword, (min_v, max_v) in VALIDATION_RANGES.items():
                if keyword in slug:
                    if not (min_v <= raw_val <= max_v):
                        return False, None
                    break

        return True, raw_val

    async def async_set_value(self, slug: str, value: Any) -> bool:
        """Writes a value using Optimistic UI + Repeated Background Sends.

        1. Updates the internal cache immediately so the UI is responsive.
        2. Launches a background task to send the command (up to 5 attempts)
           and reconcile the optimistic cache with what the device actually
           confirmed once it answers.

        Args:
            slug: The parameter identifier.
            value: The value to write.

        Returns:
            bool: Always True (Optimistic). Use the entity's later state
            (or the logs) to learn whether the device actually confirmed it.
        """
        # 1. Optimistic Cache Update (Immediate)
        async with self._cache_lock:
            previous_val = self._cache.get(slug)
            self._cache[slug] = value
            self._timestamps[slug] = time.time()

        # Notify Home Assistant immediately
        self.async_set_updated_data(self._cache)
        _LOGGER.info("Optimistic set for %s=%s. Launching background sends.", slug, value)

        # 2. Repeated sending on a background task tied to the config entry,
        # so an in-flight write (up to 5 attempts x 2s) is cancelled on
        # unload/reload instead of running on against a closed socket and
        # calling into hass after teardown.
        self.config_entry.async_create_background_task(
            self.hass,
            self._perform_repeated_write(slug, value, previous_val),
            name=f"{DOMAIN} write {slug}",
        )

        return True

    async def _perform_repeated_write(
        self, slug: str, value: Any, previous_val: Any = None
    ) -> None:
        """Background task to send the write command and reconcile state.

        Sends the command up to 5 times, 2 seconds apart, stopping as soon
        as `device.set_value()` reports a confirmed write (the boiler's own
        0xE5 result code, validated in plum_device._write_value_once -- not
        just "a response arrived"). If none of the 5 attempts are
        confirmed, the optimistic cache entry is reverted so the UI doesn't
        keep showing a value the boiler never actually applied. Either way,
        the slug's cache entry is marked stale so the next poll cycle
        re-reads the real hardware state instead of trusting the optimistic
        value for the full cache TTL.

        Args:
            slug: Parameter slug.
            value: Value to write.
            previous_val: The cached value before this optimistic write, to
                restore if the device never confirms.
        """
        confirmed = False
        # Snapshotted right after our own device.set_value() call, before
        # anything else can touch device.last_write_error: that attribute
        # is device-wide (not per-slug), and the _io_lock that protects it
        # is released during the 2s gap between our own attempts below, so
        # a different slug's write (or a poll read) could run in that gap
        # and overwrite it. Reading it immediately after our own call --
        # not once at the end of the loop -- keeps this specific to our
        # own write.
        last_rejection_code: int | None = None
        for i in range(1, 6):  # up to 5 attempts
            _LOGGER.debug("Sending %s=%s (attempt %d/5)", slug, value, i)
            if await self.device.set_value(slug, value):
                confirmed = True
                break
            last_rejection_code = self.device.last_write_error

            # Wait 2 seconds between sends, but not after the last one
            if i < 5:
                await asyncio.sleep(2.0)

        async with self._cache_lock:
            if confirmed:
                # Force a real read at the next poll instead of trusting the
                # optimistic value for the full cache TTL.
                self._timestamps[slug] = 0
            else:
                _LOGGER.warning(
                    "Write %s=%s was never confirmed by the device after 5 attempts; "
                    "reverting optimistic state to %s.",
                    slug,
                    value,
                    previous_val,
                )
                if previous_val is not None:
                    self._cache[slug] = previous_val
                else:
                    self._cache.pop(slug, None)
                self._timestamps[slug] = 0

        issue_id = f"write_rejected_{self.entry_id}_{slug}"
        if confirmed:
            clear_issue(self.hass, issue_id)
        elif last_rejection_code is not None:
            # The device explicitly rejected the write (e.g. 0x7D auth
            # error) at least once -- distinct from simply never
            # answering, which is already covered by the warning log
            # above and doesn't get its own repair issue (nothing
            # specific to tell the user beyond "check the connection",
            # which connection_lost already covers if it's persistent).
            raise_issue(
                self.hass,
                issue_id,
                "write_rejected",
                translation_placeholders={"slug": slug, "code": f"0x{last_rejection_code:02X}"},
            )

        self.async_set_updated_data(self._cache)

    async def _detect_available_parameters(self) -> None:
        """Initial scan to filter out unsupported parameters."""
        _LOGGER.info("Initial scan of available parameters...")

        targets = []
        targets.extend(list(SENSOR_TYPES.keys()))
        for conf in CLIMATE_TYPES.values():
            targets.extend(conf)
        targets.extend(list(NUMBER_TYPES.keys()))
        for conf in WATER_HEATER_TYPES.values():
            targets.extend(conf)
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

        # Dedupe while preserving order, then read everything in as few
        # batched frames as possible instead of one connection per slug.
        candidates = [slug for slug in dict.fromkeys(targets) if slug in self.device.params_map]
        values = dict(await self.device.get_values(candidates, retries=5))

        # This boiler is known to fail an entire batch when it contains one
        # PID it doesn't recognise (IMPROVEMENT_PLAN_ARCHIVE.md section F,
        # tools/scan_device_map.py) -- and the detection candidate list is
        # exactly where unrecognised-but-catalogued PIDs turn up (all 7
        # circuits' curves, every mixer, ...). A poisoned batch would drop
        # valid slugs sharing it from available_slugs -> missing entities.
        # If the batched read got *some* answers but not all, re-read the
        # rest one-by-one (a genuinely-absent parameter still returns None
        # individually and stays out; a valid one only lost to poisoning is
        # recovered). One-time cost at startup.
        if values:
            missing = [slug for slug in candidates if slug not in values]
            if missing:
                _LOGGER.debug(
                    "Batched scan returned %d/%d; re-probing %d slug(s) individually",
                    len(values),
                    len(candidates),
                    len(missing),
                )
                for slug in missing:
                    val = await self.device.get_value(slug, retries=2)
                    if val is not None:
                        values[slug] = val

        # Filter invalid values (999.0 often indicates a disconnected probe)
        valid = {slug: val for slug, val in values.items() if val is not None and val != 999.0}

        self.available_slugs = list(valid)

        # Seed the cache from the scan so the first _async_update_data cycle
        # doesn't immediately re-read everything it just read here. Live
        # slugs (TTL 0) get re-read next cycle anyway; this mainly spares
        # the STATIC_SLUGS a redundant round-trip right at startup.
        seed_now = time.time()
        async with self._cache_lock:
            for slug, val in valid.items():
                self._cache.setdefault(slug, val)
                self._timestamps.setdefault(slug, seed_now)

        _LOGGER.info("%d active parameters retained.", len(self.available_slugs))
