"""Manual-mode-backed DHW-pump forcing: the plum_ecomax.solar_to_buffer
service and the machinery the "DHW pump -> Solar buffer" switch reuses.

Bus capture (IMPROVEMENT_PLAN_ARCHIVE.md section N) showed the ecoSTER "manual
control" service screen does just two writes to force the DHW transfer
pump -- and the boiler ignores the force unless the first one is in place:

    operating mode (pid 161) = 2   -> manual control
    hdwpumpforce   (pid 172) = 512 -> force the DHW pump

reversed (161 = 1, 172 = 0) on exit. Both the service (timed) and the
switch (held until turned off) replay that here, and both share the
*guaranteed* return to automatic -- on the timer, on a replacing call, on
integration unload, and on HA shutdown. Manual mode disables the boiler's
automatic regulation while active, so a stuck manual mode means no heating
control until someone notices; hence the belt-and-braces restore and the
`manual_mode_stuck` repair issue if it ever can't get back.

If the boiler is *already* in manual mode when we start (the user flipped
the Manual mode switch, or the physical panel), we force the pump but leave
manual mode alone on the way out -- we only undo what we did.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from datetime import timedelta

import voluptuous as vol
from homeassistant.components import persistent_notification
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.util import dt as dt_util

from .const import (
    AUTO_BUFFER_CEILING,
    AUTO_DT_BALANCE_FACTOR,
    AUTO_DT_STOP,
    AUTO_MIN_REST_SECONDS,
    AUTO_MIN_RUN_SECONDS,
    AUTO_TICK_SECONDS,
    DOMAIN,
    MANUAL_MODE_BIT,
    MANUAL_MODE_SLUG,
    OPERATING_MODE_AUTO,
    OPERATING_MODE_MANUAL,
    OPERATING_MODE_SLUG,
    SOLAR_DUMP_FORCE_SLUG,
    SOLAR_DUMP_FORCE_VALUE,
    SOLAR_DUMP_MAX_MINUTES,
    SOLAR_DUMP_TEMP_MAX,
    SOLAR_DUMP_TEMP_MIN,
)
from .issues import clear_issue, raise_issue

_LOGGER = logging.getLogger(__name__)

SERVICE_SOLAR_TO_BUFFER = "solar_to_buffer"
STUCK_ISSUE_ID = "manual_mode_stuck"
DHW_TEMP_SLUG = "tempcwu"
BUFFER_TEMP_SLUGS = ("tempbuforup", "tempbufordown", "tempclutch")
STOP_CHECK_INTERVAL = 30  # seconds between DHW-temperature checks while running

_TEMP_OVERRIDE = vol.All(
    vol.Coerce(float), vol.Range(min=SOLAR_DUMP_TEMP_MIN, max=SOLAR_DUMP_TEMP_MAX)
)
SOLAR_TO_BUFFER_SCHEMA = vol.Schema(
    {
        # No upper bound here on purpose: a longer request is clamped to
        # SOLAR_DUMP_MAX_MINUTES in the handler rather than rejected.
        vol.Required("duration"): vol.All(vol.Coerce(int), vol.Range(min=1)),
        # Optional per-call overrides of the number entities' thresholds.
        vol.Optional("start_temp"): _TEMP_OVERRIDE,
        vol.Optional("stop_temp"): _TEMP_OVERRIDE,
    }
)

# entry_id -> the running lifecycle task. One manual-mode session per boiler,
# shared by the service, the switch and the auto controller (last start wins).
_RUNNING: dict[str, asyncio.Task] = {}
# entry_id -> who started the current session: "manual" | "service" | "auto".
# The auto controller only ever stops a session it owns.
_OWNER: dict[str, str] = {}
_STOP_UNSUB = None


def _optimistic(coordinator, **values) -> None:
    """Nudge the coordinator's cache so entities reflect a write immediately
    instead of waiting for the next poll -- the same in-place update +
    listener notify that coordinator.async_set_value does. Safe to call any
    time, including during teardown."""
    with contextlib.suppress(Exception):
        data = coordinator.data
        if isinstance(data, dict):
            data.update(values)
            coordinator.async_set_updated_data(data)


def _threshold(coordinator, attr: str, override=None) -> float | None:
    """The DHW-temperature threshold to use: a per-call override if given,
    else the coordinator attribute the number entity keeps up to date.
    Coerced to float, or None if neither is a usable number."""
    for candidate in (override, getattr(coordinator, attr, None)):
        if isinstance(candidate, (int, float)) and not isinstance(candidate, bool):
            return float(candidate)
        try:
            if candidate is not None:
                return float(candidate)
        except (TypeError, ValueError):
            pass
    return None


async def _confirmed_write(dev, slug: str, value: int, tries: int = 3) -> bool:
    """Write and wait for the boiler's own confirmation, retrying."""
    for attempt in range(1, tries + 1):
        if await dev.set_value(slug, value):
            return True
        _LOGGER.debug("solar_to_buffer: write %s=%s not confirmed (try %d)", slug, value, attempt)
        await asyncio.sleep(1.0)
    return False


async def _restore(hass: HomeAssistant, coordinator, entry_id: str, *, exit_manual: bool) -> None:
    """Undo what we did: always clear the pump force; leave manual mode only
    if we were the ones who entered it. Idempotent, loud on failure."""
    dev = coordinator.device
    pump_ok = await _confirmed_write(dev, SOLAR_DUMP_FORCE_SLUG, 0, tries=6)
    _optimistic(coordinator, **{SOLAR_DUMP_FORCE_SLUG: 0})

    if not exit_manual:
        _LOGGER.info("solar_to_buffer: pump force cleared (manual mode left as it was)")
        if pump_ok:
            clear_issue(hass, STUCK_ISSUE_ID)
        return

    mode_ok = await _confirmed_write(dev, OPERATING_MODE_SLUG, OPERATING_MODE_AUTO, tries=6)
    read_back = None
    with contextlib.suppress(Exception):
        read_back = await dev.get_value(OPERATING_MODE_SLUG, retries=4)
    back_to_auto = read_back is not None and int(read_back) == OPERATING_MODE_AUTO
    if back_to_auto:
        _optimistic(coordinator, **{OPERATING_MODE_SLUG: OPERATING_MODE_AUTO})

    if mode_ok and back_to_auto:
        _LOGGER.info("solar_to_buffer: boiler restored to automatic")
        clear_issue(hass, STUCK_ISSUE_ID)
    else:
        _LOGGER.error(
            "solar_to_buffer: FAILED to return the boiler to automatic "
            "(pump_cleared=%s mode_write=%s mode_read=%s) -- set the operating "
            "mode back to automatic on the panel / with the Manual mode switch",
            pump_ok,
            mode_ok,
            read_back,
        )
        raise_issue(hass, STUCK_ISSUE_ID, STUCK_ISSUE_ID, severity=ir.IssueSeverity.ERROR)


async def _hold(dev, hold_s: int | None, stop_temp: float | None) -> None:
    """Wait out the forced-pump period. `hold_s` None = until cancelled.
    If `stop_temp` is set, poll the DHW tank temperature and return early
    once it drops to that threshold (so the tank isn't drained too far)."""
    if stop_temp is None:
        if hold_s is None:
            await asyncio.Event().wait()
        else:
            await asyncio.sleep(hold_s)
        return

    remaining = hold_s
    while remaining is None or remaining > 0:
        nap = STOP_CHECK_INTERVAL if remaining is None else min(STOP_CHECK_INTERVAL, remaining)
        await asyncio.sleep(nap)
        if remaining is not None:
            remaining -= nap
        temp = await dev.get_value(DHW_TEMP_SLUG, retries=2)
        if temp is not None and float(temp) <= stop_temp:
            _LOGGER.info(
                "solar_to_buffer: DHW at %.1f C <= stop threshold %.1f C -- stopping",
                float(temp),
                stop_temp,
            )
            return


async def _dump_lifecycle(
    hass: HomeAssistant,
    coordinator,
    entry_id: str,
    hold_s: int | None,
    start_temp_override: float | None = None,
    stop_temp_override: float | None = None,
) -> None:
    """Enter manual mode (unless already in it), force the DHW pump, hold for
    `hold_s` seconds (or until cancelled if None) / until the DHW tank hits
    the stop threshold, then restore. Won't start below the start threshold."""
    dev = coordinator.device
    forcing = False
    entered_manual = False
    try:
        start_temp = _threshold(coordinator, "solar_dump_start_temp", start_temp_override)
        stop_temp = _threshold(coordinator, "solar_dump_stop_temp", stop_temp_override)
        if start_temp is not None and stop_temp is not None and stop_temp >= start_temp:
            _LOGGER.warning(
                "solar_to_buffer: stop threshold %.1f C >= start threshold %.1f C "
                "-- a dump would stop as soon as it starts",
                stop_temp,
                start_temp,
            )

        # Start gate -- before touching the boiler, so an abort here leaves
        # forcing/entered_manual False and the finally only needs the poke.
        dhw_temp = await dev.get_value(DHW_TEMP_SLUG, retries=2)
        if start_temp is not None and dhw_temp is not None and float(dhw_temp) < start_temp:
            _LOGGER.info(
                "solar_to_buffer: DHW at %.1f C is below the %.1f C start threshold -- not starting",
                float(dhw_temp),
                start_temp,
            )
            persistent_notification.async_create(
                hass,
                f"Solar dump not started: the DHW tank is at {float(dhw_temp):.1f} °C, "
                f"below the {start_temp:.0f} °C start threshold.",
                title="Plum EcoMAX — solar dump",
                notification_id=f"{DOMAIN}_solar_dump_too_cold_{entry_id}",
            )
            return

        mode = await dev.get_value(OPERATING_MODE_SLUG, retries=4)
        if mode is None:
            _LOGGER.error("solar_to_buffer: cannot read the operating mode -- aborting")
            return

        entered_manual = int(mode) != OPERATING_MODE_MANUAL
        if entered_manual:
            if not await _confirmed_write(dev, OPERATING_MODE_SLUG, OPERATING_MODE_MANUAL):
                _LOGGER.error("solar_to_buffer: could not enter manual mode -- aborting")
                return
            _optimistic(coordinator, **{OPERATING_MODE_SLUG: OPERATING_MODE_MANUAL})
            await asyncio.sleep(3)
            state = await dev.get_value(MANUAL_MODE_SLUG, retries=4)
            if state is None or not (int(state) & MANUAL_MODE_BIT):
                _LOGGER.error(
                    "solar_to_buffer: manual mode not confirmed by telemetry (%s=%s) -- aborting",
                    MANUAL_MODE_SLUG,
                    state,
                )
                return

        if not await _confirmed_write(dev, SOLAR_DUMP_FORCE_SLUG, SOLAR_DUMP_FORCE_VALUE):
            _LOGGER.error("solar_to_buffer: could not force the DHW pump -- aborting")
            return

        forcing = True
        _optimistic(coordinator, **{SOLAR_DUMP_FORCE_SLUG: SOLAR_DUMP_FORCE_VALUE})
        if hold_s is None:
            _LOGGER.info("solar_to_buffer: DHW pump forced (held until turned off)")
        else:
            _LOGGER.info("solar_to_buffer: DHW pump forced for %d min", max(hold_s // 60, 1))
        await _hold(dev, hold_s, stop_temp)
        _LOGGER.info("solar_to_buffer: done")
    except asyncio.CancelledError:
        _LOGGER.info("solar_to_buffer: interrupted -- restoring")
        raise
    finally:
        _RUNNING.pop(entry_id, None)
        if forcing or entered_manual:
            # Awaits in a finally run to completion even while this task is
            # being cancelled (unload / HA stop / a replacing call), so the
            # restore always goes through; whoever cancelled us awaits this
            # task, so they wait for the restore too.
            await _restore(hass, coordinator, entry_id, exit_manual=entered_manual)
        else:
            # Nothing was actually done (start gate / unreadable mode /
            # failed manual-mode write) -- undo the optimistic ON the switch
            # showed when async_start_hold poked it.
            _optimistic(coordinator, **{SOLAR_DUMP_FORCE_SLUG: 0})


async def _replace_run(hass: HomeAssistant, entry_id: str, coro, name: str, owner: str) -> None:
    existing = _RUNNING.pop(entry_id, None)
    if existing and not existing.done():
        _LOGGER.info("solar_to_buffer: a run is active for %s -- replacing it", entry_id)
        existing.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await existing
    _OWNER[entry_id] = owner
    _RUNNING[entry_id] = hass.async_create_task(coro, name=name)


async def async_start_hold(
    hass: HomeAssistant,
    coordinator,
    entry_id: str,
    *,
    owner: str = "manual",
    start_temp: float | None = None,
    stop_temp: float | None = None,
) -> None:
    """Start manual mode + forced DHW pump, held until async_stop_for_entry.

    The "DHW pump -> Solar buffer" switch calls this with the defaults; the
    auto controller passes owner="auto" and its own thresholds (start_temp=0
    disables the start gate -- the tick already decided -- while stop_temp is
    the auto floor, kept as an in-burst safety net).
    """
    _optimistic(coordinator, **{SOLAR_DUMP_FORCE_SLUG: SOLAR_DUMP_FORCE_VALUE})
    await _replace_run(
        hass,
        entry_id,
        _dump_lifecycle(hass, coordinator, entry_id, None, start_temp, stop_temp),
        f"{DOMAIN} solar_to_buffer hold {entry_id}",
        owner,
    )


async def _handle_solar_to_buffer(hass: HomeAssistant, call: ServiceCall) -> None:
    requested = int(call.data["duration"])
    hold_min = min(requested, SOLAR_DUMP_MAX_MINUTES)
    if hold_min != requested:
        _LOGGER.warning(
            "solar_to_buffer: duration %d min clamped to the %d min cap",
            requested,
            SOLAR_DUMP_MAX_MINUTES,
        )
    hold_s = hold_min * 60
    start_override = call.data.get("start_temp")
    stop_override = call.data.get("stop_temp")
    coordinators = {
        e.entry_id: e.runtime_data for e in hass.config_entries.async_loaded_entries(DOMAIN)
    }
    if not coordinators:
        _LOGGER.warning("solar_to_buffer called but no Plum EcoMAX config entry is loaded")
        return

    for entry_id, coordinator in coordinators.items():
        _optimistic(coordinator, **{SOLAR_DUMP_FORCE_SLUG: SOLAR_DUMP_FORCE_VALUE})
        await _replace_run(
            hass,
            entry_id,
            _dump_lifecycle(hass, coordinator, entry_id, hold_s, start_override, stop_override),
            f"{DOMAIN} solar_to_buffer {entry_id}",
            "service",
        )


async def async_stop_for_entry(hass: HomeAssistant, entry_id: str) -> None:
    """Cancel a running manual-mode session for one entry and wait for its
    restore. Used by the switch's turn_off and by async_unload_entry (called
    BEFORE the device socket is closed, so the restore writes still go out).
    """
    _OWNER.pop(entry_id, None)
    task = _RUNNING.pop(entry_id, None)
    if task and not task.done():
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task


async def _async_stop_all(hass: HomeAssistant) -> None:
    for entry_id in list(_RUNNING):
        await async_stop_for_entry(hass, entry_id)


# --------------------------------------------------------------------------
# Automatic mode -- a differential-temperature (dT) controller that runs the
# transfer in bursts. See IMPROVEMENT_PLAN_ARCHIVE.md section O / the plan file.
# --------------------------------------------------------------------------

# entry_id -> {unsub, running, last_start, last_stop, runtime_today, day}
_AUTO: dict[str, dict] = {}


def _num(coordinator, attr: str, default: float) -> float:
    v = getattr(coordinator, attr, None)
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _buffer_temp(coordinator) -> float | None:
    """Best available buffer temperature. tempbuforup reads 999 (fault) on
    this boiler, so in practice this is tempbufordown."""
    for slug in BUFFER_TEMP_SLUGS:
        v = coordinator.data.get(slug)
        if isinstance(v, (int, float)) and 0 < float(v) < 200:
            return float(v)
    return None


def _in_legionella_hour(coordinator) -> bool:
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


def auto_runtime_minutes(entry_id: str) -> float:
    """Circulator minutes run today by the auto controller (incl. a burst in
    progress). Read by the runtime sensor."""
    st = _AUTO.get(entry_id)
    if not st:
        return 0.0
    total = st["runtime_today"]
    if st["running"] and st["last_start"] is not None:
        total += (time.monotonic() - st["last_start"]) / 60
    return round(total, 1)


def auto_seed_runtime(entry_id: str, minutes: float) -> None:
    """Restore today's accumulated minutes from the sensor's stored state."""
    st = _AUTO.setdefault(entry_id, _fresh_auto_state())
    st["runtime_today"] = max(st["runtime_today"], float(minutes))


def _fresh_auto_state() -> dict:
    return {
        "unsub": None,
        "running": False,
        "last_start": None,
        "last_stop": 0.0,
        "runtime_today": 0.0,
        "day": dt_util.now().date(),
    }


async def async_auto_enable(hass: HomeAssistant, coordinator, entry_id: str) -> None:
    """Arm the auto controller: tick now, then every AUTO_TICK_SECONDS."""
    st = _AUTO.setdefault(entry_id, _fresh_auto_state())
    if st["unsub"] is not None:
        return

    async def _tick(_now) -> None:
        with contextlib.suppress(Exception):
            await _auto_tick(hass, coordinator, entry_id)

    st["unsub"] = async_track_time_interval(hass, _tick, timedelta(seconds=AUTO_TICK_SECONDS))
    _LOGGER.info("solar dump auto: enabled for %s", entry_id)
    await _tick(None)


async def async_auto_disable(hass: HomeAssistant, entry_id: str) -> None:
    """Disarm the auto controller and stop a burst it owns."""
    st = _AUTO.get(entry_id)
    if st and st["unsub"] is not None:
        st["unsub"]()
        st["unsub"] = None
    if _OWNER.get(entry_id) == "auto":
        await async_stop_for_entry(hass, entry_id)
    if st:
        st["running"] = False
    _LOGGER.info("solar dump auto: disabled for %s", entry_id)


async def async_stop_auto(hass: HomeAssistant, entry_id: str) -> None:
    """Full teardown for async_unload_entry: disarm + drop state."""
    await async_auto_disable(hass, entry_id)
    _AUTO.pop(entry_id, None)


async def _auto_tick(hass: HomeAssistant, coordinator, entry_id: str) -> None:
    st = _AUTO.setdefault(entry_id, _fresh_auto_state())
    today = dt_util.now().date()
    if today != st["day"]:
        st["day"] = today
        st["runtime_today"] = 0.0

    # Reconcile: derive "running" from the shared session, so a burst ended
    # by the in-burst safety net or by an unload is accounted for here too.
    owns = _OWNER.get(entry_id) == "auto"
    task = _RUNNING.get(entry_id)
    running = owns and task is not None and not task.done()
    if st["running"] and not running and st["last_start"] is not None:
        st["runtime_today"] += (time.monotonic() - st["last_start"]) / 60
        st["last_stop"] = time.monotonic()
    st["running"] = running

    # Hands off while a manual switch / the service is driving.
    if _OWNER.get(entry_id) not in (None, "auto"):
        return

    if _in_legionella_hour(coordinator):
        if running:
            await _auto_stop(hass, st, entry_id, "anti-legionella hour")
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
            await _auto_stop(hass, st, entry_id, hard_off or f"dT exhausted ({dt:.1f})")


async def _auto_stop(hass: HomeAssistant, st: dict, entry_id: str, reason: str) -> None:
    await async_stop_for_entry(hass, entry_id)
    if st["running"] and st["last_start"] is not None:
        st["runtime_today"] += (time.monotonic() - st["last_start"]) / 60
    st["running"] = False
    st["last_stop"] = time.monotonic()
    _LOGGER.info(
        "solar dump auto: burst stop -- %s, %.0f min run today",
        reason,
        st["runtime_today"],
    )


async def async_register_services(hass: HomeAssistant) -> None:
    """Register plum_ecomax.solar_to_buffer once (idempotent)."""
    global _STOP_UNSUB
    if not hass.services.has_service(DOMAIN, SERVICE_SOLAR_TO_BUFFER):

        async def _service(call: ServiceCall) -> None:
            await _handle_solar_to_buffer(hass, call)

        hass.services.async_register(
            DOMAIN, SERVICE_SOLAR_TO_BUFFER, _service, schema=SOLAR_TO_BUFFER_SCHEMA
        )

    if _STOP_UNSUB is None:

        async def _on_stop(_event) -> None:
            await _async_stop_all(hass)

        _STOP_UNSUB = hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, _on_stop)


async def async_unregister_services(hass: HomeAssistant) -> None:
    """Drop the service when the last config entry unloads."""
    global _STOP_UNSUB
    if not hass.config_entries.async_loaded_entries(DOMAIN):
        hass.services.async_remove(DOMAIN, SERVICE_SOLAR_TO_BUFFER)
        if _STOP_UNSUB is not None:
            _STOP_UNSUB()
            _STOP_UNSUB = None
