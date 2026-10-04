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
from typing import TYPE_CHECKING, Any, NoReturn

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

if TYPE_CHECKING:
    from .plum_device import PlumDevice
    from .solar_dump import SolarDumpState

from .const import (
    DOMAIN,
    MAX_UPDATE_INTERVAL,
    SOLAR_DUMP_AUTO_FLOOR_DEFAULT,
    SOLAR_DUMP_BUFFER_TARGET_DEFAULT,
    SOLAR_DUMP_DAILY_BUDGET_DEFAULT,
    SOLAR_DUMP_DT_START_DEFAULT,
    SOLAR_DUMP_START_TEMP_DEFAULT,
    SOLAR_DUMP_STOP_TEMP_DEFAULT,
    STATIC_SLUGS,
    UPDATE_INTERVAL,
)
from .issues import clear_issue, raise_issue
from .parameters import detection_candidates, validate_value

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

# A write is sent up to WRITE_ATTEMPTS times, WRITE_RETRY_DELAY seconds apart (and
# each PlumDevice.set_value() retries on its own too). The caller waits for the
# outcome, so this is bounded to keep a failing action from hanging.
WRITE_ATTEMPTS = 2
WRITE_RETRY_DELAY = 2.0

# coordinator.data: slug -> decoded value. The keys are the parameter slugs of
# the device map (dynamic), so a TypedDict can't describe them.
PlumData = dict[str, Any]


class PlumDataUpdateCoordinator(DataUpdateCoordinator[PlumData]):
    # Always created with its config entry (see __init__): narrows the base
    # class's Optional so the entry's background-task API can be called.
    config_entry: ConfigEntry
    """Centralized data management with Robust Data Validation.

    Implements caching, write-through strategies, and data sanitization
    to prevent outliers from polluting the state machine.
    """

    # True while the boiler is unreachable -- lets the outage be logged once
    # on the way down and once on recovery instead of every cycle.
    _link_down: bool = False
    # The configured polling interval, remembered while update_interval is
    # stretched during an outage (see _stretch_interval).
    _base_interval: timedelta | None = None
    # Per-boiler solar-dump state, created lazily by solar_dump._state().
    # Quoted: SolarDumpState is imported only for type checking (Python 3.13
    # evaluates class-body annotations eagerly).
    _solar_dump_state: "SolarDumpState | None"

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

    async def _async_update_data(self) -> PlumData:
        """Main update loop with Validation and Fallback.

        Returns:
            dict: The validated data.
        """
        data = {}
        now = time.time()

        if not self.available_slugs:
            detect_error: Exception | None = None
            try:
                await self._detect_available_parameters()
            except Exception as e:
                detect_error = e
            # Nothing detected means nothing could be read: the boiler is
            # unreachable. Raising here (rather than carrying on with an empty
            # parameter list) makes the first refresh fail with
            # ConfigEntryNotReady, so the platforms are set up once the boiler
            # answers instead of loading with no entities at all.
            if not self.available_slugs:
                self._link_lost(0, detect_error)

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
                self._link_lost(len(to_fetch), read_error)
        if self._link_down:
            _LOGGER.info("Communication with the boiler restored")
            self._link_down = False
        self._restore_interval()

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

    def _link_lost(self, unread: int, cause: Exception | None) -> NoReturn:
        """Record an outage (repair issue, one warning per outage, slower polling)
        and abort the cycle with UpdateFailed."""
        self._update_connection_issue()
        if not self._link_down:
            _LOGGER.warning(
                "Lost communication with the boiler (%d parameters unread): %s",
                unread,
                cause or "no valid response",
            )
        self._link_down = True
        self._stretch_interval()
        raise UpdateFailed("Boiler unreachable") from cause

    def _stretch_interval(self) -> None:
        """Poll less often while the boiler is unreachable (doubling, capped at
        MAX_UPDATE_INTERVAL): a dead link makes each cycle wait on timeouts, and
        hammering it every few seconds helps nobody."""
        if self.update_interval is None:
            return
        if self._base_interval is None:
            self._base_interval = self.update_interval
        cap = timedelta(seconds=MAX_UPDATE_INTERVAL)
        self.update_interval = max(self._base_interval, min(self.update_interval * 2, cap))

    def _restore_interval(self) -> None:
        """Back to the configured polling interval after a successful cycle."""
        if self._base_interval is not None:
            self.update_interval = self._base_interval
            self._base_interval = None

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
        """Sanitizes a raw reading (see parameters.validate_value).

        Returns:
            Tuple[bool, Any]: (IsValid, SafeValue).
        """
        return validate_value(
            self.device.params_map, slug, raw_val, cached_val, self._delta_rejection_counts
        )

    async def async_set_value(self, slug: str, value: Any) -> bool:
        """Writes a value: optimistic UI, then wait for the boiler's confirmation.

        1. Updates the internal cache immediately so the UI is responsive.
        2. Sends the command (see _perform_repeated_write) and waits for the
           boiler's own confirmation. If it never confirms, the optimistic
           value is reverted and the call raises, so the action that asked for
           the write reports the failure instead of silently doing nothing.

        Args:
            slug: The parameter identifier.
            value: The value to write.

        Returns:
            bool: True once the boiler confirmed the write.

        Raises:
            HomeAssistantError: The boiler did not confirm the write
                ("write_failed"), or explicitly rejected it ("write_rejected").
        """
        # 1. Optimistic Cache Update (Immediate)
        async with self._cache_lock:
            previous_val = self._cache.get(slug)
            self._cache[slug] = value
            self._timestamps[slug] = time.time()

        # Notify Home Assistant immediately
        self.async_set_updated_data(self._cache)
        _LOGGER.info("Optimistic set for %s=%s. Sending.", slug, value)

        try:
            await self._perform_repeated_write(slug, value, previous_val)
        except asyncio.CancelledError:
            # The caller went away mid-write (e.g. the entry is unloading): don't
            # leave a value the boiler may never have applied in the cache.
            self._revert_optimistic(slug, previous_val)
            raise
        return True

    def _revert_optimistic(self, slug: str, previous_val: Any) -> None:
        """Put back what was cached before an optimistic write, and mark the slug
        stale so the next poll re-reads the real hardware state."""
        if previous_val is not None:
            self._cache[slug] = previous_val
        else:
            self._cache.pop(slug, None)
        self._timestamps[slug] = 0

    async def _perform_repeated_write(
        self, slug: str, value: Any, previous_val: Any = None
    ) -> None:
        """Send the write command, reconcile the cache, raise if never confirmed.

        Sends the command up to WRITE_ATTEMPTS times, WRITE_RETRY_DELAY seconds
        apart (each `device.set_value()` call retries on its own too), stopping
        as soon as it reports a confirmed write (the boiler's own 0xE5 result
        code, validated in plum_device._write_value_once -- not just "a
        response arrived"). If no attempt is confirmed, the optimistic cache
        entry is reverted so the UI doesn't keep showing a value the boiler
        never applied, a repair issue is raised when the boiler explicitly
        rejected the write, and HomeAssistantError is raised so the calling
        action fails visibly. Either way, the slug's cache entry is marked
        stale so the next poll cycle re-reads the real hardware state.

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
        for i in range(1, WRITE_ATTEMPTS + 1):
            _LOGGER.debug("Sending %s=%s (attempt %d/%d)", slug, value, i, WRITE_ATTEMPTS)
            if await self.device.set_value(slug, value):
                confirmed = True
                break
            last_rejection_code = self.device.last_write_error

            # Wait between sends, but not after the last one
            if i < WRITE_ATTEMPTS:
                await asyncio.sleep(WRITE_RETRY_DELAY)

        async with self._cache_lock:
            if confirmed:
                # Force a real read at the next poll instead of trusting the
                # optimistic value for the full cache TTL.
                self._timestamps[slug] = 0
            else:
                _LOGGER.warning(
                    "Write %s=%s was never confirmed by the device after %d attempts; "
                    "reverting optimistic state to %s.",
                    slug,
                    value,
                    WRITE_ATTEMPTS,
                    previous_val,
                )
                self._revert_optimistic(slug, previous_val)

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

        if not confirmed:
            if last_rejection_code is not None:
                raise HomeAssistantError(
                    translation_domain=DOMAIN,
                    translation_key="write_rejected",
                    translation_placeholders={"slug": slug, "code": f"0x{last_rejection_code:02X}"},
                )
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="write_failed",
                translation_placeholders={"slug": slug},
            )

    async def _detect_available_parameters(self) -> None:
        """Initial scan to filter out unsupported parameters."""
        _LOGGER.info("Initial scan of available parameters...")

        # Read everything in as few batched frames as possible instead of one
        # connection per slug.
        candidates = detection_candidates(self.device.params_map)
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
