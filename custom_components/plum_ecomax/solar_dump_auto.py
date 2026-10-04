"""Automatic solar dump: a differential-temperature (dT) controller that runs
the DHW -> buffer transfer in bursts (see solar_dump.py for the session it
drives: manual mode + forced DHW pump, with the guaranteed return to automatic).

Every AUTO_TICK_SECONDS it compares the DHW tank (ECS) with the buffer: it starts
a burst when ECS is hotter than the buffer by dt_start (charge mode below the
buffer target, twice the gradient above it), stops below AUTO_DT_STOP, never
drains the DHW below the auto floor, skips the anti-legionella hour and respects
a daily circulator budget plus anti-short-cycle timers. It only ever stops a
session it owns (see SolarDumpState.owner).
"""

from __future__ import annotations

import contextlib
import logging
import time
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.util import dt as dt_util

from .const import (
    AUTO_BUFFER_CEILING,
    AUTO_DT_BALANCE_FACTOR,
    AUTO_DT_STOP,
    AUTO_MIN_REST_SECONDS,
    AUTO_MIN_RUN_SECONDS,
    AUTO_TICK_SECONDS,
)
from .solar_dump import DHW_TEMP_SLUG, _state, async_start_hold, async_stop_for_entry

if TYPE_CHECKING:  # annotations only
    from .coordinator import PlumDataUpdateCoordinator

_LOGGER = logging.getLogger(__name__)

BUFFER_TEMP_SLUGS = ("tempbuforup", "tempbufordown", "tempclutch")


def _num(coordinator: PlumDataUpdateCoordinator, attr: str, default: float) -> float:
    v: Any = getattr(coordinator, attr, None)
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _buffer_temp(coordinator: PlumDataUpdateCoordinator) -> float | None:
    """Best available buffer temperature. tempbuforup reads 999 (fault) on
    this boiler, so in practice this is tempbufordown."""
    for slug in BUFFER_TEMP_SLUGS:
        v = coordinator.data.get(slug)
        if isinstance(v, (int, float)) and 0 < float(v) < 200:
            return float(v)
    return None


def _in_legionella_hour(coordinator: PlumDataUpdateCoordinator) -> bool:
    """True during the boiler's weekly anti-legionella hour -- the boiler is
    driving the DHW tank up to ~70 C then, so a burst would fight it."""
    hour = coordinator.data.get("hdwlegionhour")
    day = coordinator.data.get("hdwlegionday")  # 0 = every day, 1..7 = Mon..Sun
    if not isinstance(hour, (int, float)):
        return False
    now = dt_util.now()
    if now.hour != int(hour):
        return False
    return not isinstance(day, (int, float)) or int(day) in (0, now.isoweekday())


def auto_runtime_minutes(coordinator: PlumDataUpdateCoordinator) -> float:
    """Circulator minutes run today by the auto controller (incl. a burst in
    progress). Read by the runtime sensor."""
    st = _state(coordinator).auto
    if not st:
        return 0.0
    total: float = st["runtime_today"]
    if st["running"] and st["last_start"] is not None:
        total += (time.monotonic() - st["last_start"]) / 60
    return round(total, 1)


def _auto_state(coordinator: PlumDataUpdateCoordinator) -> dict[str, Any]:
    state = _state(coordinator)
    if state.auto is None:
        state.auto = _fresh_auto_state()
    return state.auto


def auto_seed_runtime(coordinator: PlumDataUpdateCoordinator, minutes: float) -> None:
    """Restore today's accumulated minutes from the sensor's stored state."""
    st = _auto_state(coordinator)
    st["runtime_today"] = max(st["runtime_today"], float(minutes))


def _fresh_auto_state() -> dict[str, Any]:
    return {
        "unsub": None,
        "running": False,
        "last_start": None,
        "last_stop": 0.0,
        "runtime_today": 0.0,
        "day": dt_util.now().date(),
    }


async def async_auto_enable(
    hass: HomeAssistant, coordinator: PlumDataUpdateCoordinator, entry_id: str
) -> None:
    """Arm the auto controller: tick now, then every AUTO_TICK_SECONDS."""
    st = _auto_state(coordinator)
    if st["unsub"] is not None:
        return

    async def _tick(_now: datetime | None) -> None:
        with contextlib.suppress(Exception):
            await _auto_tick(hass, coordinator, entry_id)

    st["unsub"] = async_track_time_interval(hass, _tick, timedelta(seconds=AUTO_TICK_SECONDS))
    state = _state(coordinator)
    config_entry = getattr(coordinator, "config_entry", None)
    if not state.unload_hooked and config_entry is not None:
        # Safety net: if the entry goes away without async_stop_auto having
        # run, the periodic tick must not keep firing against it.
        state.unload_hooked = True
        config_entry.async_on_unload(lambda: _drop_auto_tick(coordinator))
    _LOGGER.info("solar dump auto: enabled for %s", entry_id)
    await _tick(None)


def _drop_auto_tick(coordinator: PlumDataUpdateCoordinator) -> None:
    """Cancel the periodic auto tick, if armed. Idempotent."""
    st = _state(coordinator).auto
    if st and st["unsub"] is not None:
        st["unsub"]()
        st["unsub"] = None


async def async_auto_disable(hass: HomeAssistant, coordinator: PlumDataUpdateCoordinator) -> None:
    """Disarm the auto controller and stop a burst it owns."""
    state = _state(coordinator)
    st = state.auto
    _drop_auto_tick(coordinator)
    if state.owner == "auto":
        await async_stop_for_entry(hass, coordinator)
    if st:
        st["running"] = False
    _LOGGER.info("solar dump auto: disabled for %s", coordinator.entry_id)


async def async_stop_auto(hass: HomeAssistant, coordinator: PlumDataUpdateCoordinator) -> None:
    """Full teardown for async_unload_entry: disarm + drop state."""
    await async_auto_disable(hass, coordinator)
    _state(coordinator).auto = None


async def _auto_tick(
    hass: HomeAssistant, coordinator: PlumDataUpdateCoordinator, entry_id: str
) -> None:
    st = _auto_state(coordinator)
    state = _state(coordinator)
    today = dt_util.now().date()
    if today != st["day"]:
        st["day"] = today
        st["runtime_today"] = 0.0

    # Reconcile: derive "running" from the shared session, so a burst ended
    # by the in-burst safety net or by an unload is accounted for here too.
    owns = state.owner == "auto"
    task = state.task
    running = owns and task is not None and not task.done()
    if st["running"] and not running and st["last_start"] is not None:
        st["runtime_today"] += (time.monotonic() - st["last_start"]) / 60
        st["last_stop"] = time.monotonic()
    st["running"] = running

    # Hands off while a manual switch / the service is driving.
    if state.owner not in (None, "auto"):
        return

    if _in_legionella_hour(coordinator):
        if running:
            await _auto_stop(hass, coordinator, st, entry_id, "anti-legionella hour")
        return

    ecs = coordinator.data.get(DHW_TEMP_SLUG)
    buf = _buffer_temp(coordinator)
    if not isinstance(ecs, (int, float)) or buf is None or float(ecs) >= 999:
        _LOGGER.debug("solar dump auto: tick skipped, sensors ecs=%s buf=%s", ecs, buf)
        return
    ecs = float(ecs)

    floor = _num(coordinator, "solar_dump_auto_ecs_floor", 45)
    target = _num(coordinator, "solar_dump_buffer_target", 55)
    dt_on = _num(coordinator, "solar_dump_dt_start", 8)
    budget = _num(coordinator, "solar_dump_daily_budget", 120)
    dt = ecs - buf

    hard_off = None
    if ecs <= floor:
        hard_off = f"DHW at floor ({ecs:.1f} <= {floor:.0f})"
    elif buf >= AUTO_BUFFER_CEILING:
        hard_off = f"buffer at ceiling ({buf:.1f})"
    elif budget and st["runtime_today"] >= budget:
        hard_off = f"daily budget reached ({st['runtime_today']:.0f}/{budget:.0f} min)"

    if hard_off:
        want = False
    elif buf < target:  # charge mode
        want = dt > AUTO_DT_STOP if running else dt >= dt_on
    else:  # balance / trickle mode
        want = dt > AUTO_DT_STOP if running else dt >= dt_on * AUTO_DT_BALANCE_FACTOR

    now = time.monotonic()
    if want and not running:
        if now - st["last_stop"] >= AUTO_MIN_REST_SECONDS:
            mode = "charge" if buf < target else "balance"
            _LOGGER.info(
                "solar dump auto: burst start -- %s mode, ECS %.1f, buffer %.1f, dT %.1f",
                mode,
                ecs,
                buf,
                dt,
            )
            await async_start_hold(
                hass, coordinator, entry_id, owner="auto", start_temp=0, stop_temp=floor
            )
            st["running"] = True
            st["last_start"] = now
    elif running and not want:
        min_run_ok = st["last_start"] is not None and now - st["last_start"] >= AUTO_MIN_RUN_SECONDS
        if hard_off or min_run_ok:
            await _auto_stop(
                hass, coordinator, st, entry_id, hard_off or f"dT exhausted ({dt:.1f})"
            )


async def _auto_stop(
    hass: HomeAssistant,
    coordinator: PlumDataUpdateCoordinator,
    st: dict[str, Any],
    entry_id: str,
    reason: str,
) -> None:
    await async_stop_for_entry(hass, coordinator)
    if st["running"] and st["last_start"] is not None:
        st["runtime_today"] += (time.monotonic() - st["last_start"]) / 60
    st["running"] = False
    st["last_stop"] = time.monotonic()
    _LOGGER.info(
        "solar dump auto: burst stop -- %s, %.0f min run today",
        reason,
        st["runtime_today"],
    )
