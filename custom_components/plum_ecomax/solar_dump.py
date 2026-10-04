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
from collections.abc import Coroutine
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import voluptuous as vol
from homeassistant.components import persistent_notification
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.core import Event, HomeAssistant, ServiceCall
from homeassistant.helpers import issue_registry as ir

from .const import (
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

if TYPE_CHECKING:  # annotations only: __init__ imports this module
    from . import PlumConfigEntry
    from .coordinator import PlumDataUpdateCoordinator
    from .plum_device import PlumDevice

_LOGGER = logging.getLogger(__name__)

SERVICE_SOLAR_TO_BUFFER = "solar_to_buffer"
STUCK_ISSUE_ID = "manual_mode_stuck"
DHW_TEMP_SLUG = "tempcwu"
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


@dataclass
class SolarDumpState:
    """Per-boiler solar-dump state, carried by that entry's coordinator.

    One manual-mode session per boiler, shared by the service, the switch and
    the auto controller (last start wins).
    """

    # The running lifecycle task, if any.
    task: asyncio.Task | None = None
    # Who started the current session: "manual" | "service" | "auto". The
    # auto controller only ever stops a session it owns.
    owner: str | None = None
    # Auto-controller bookkeeping ({unsub, running, last_start, last_stop,
    # runtime_today, day}), created on first use -- see _fresh_auto_state().
    auto: dict | None = None
    # True once the auto tick's unsubscribe has been handed to the entry's
    # async_on_unload, so toggling the switch doesn't register it again.
    unload_hooked: bool = field(default=False, repr=False)


def _state(coordinator: PlumDataUpdateCoordinator) -> SolarDumpState:
    """The coordinator's SolarDumpState, created on first access (lazily, so
    a coordinator built without going through __init__ still works)."""
    state: SolarDumpState | None = vars(coordinator).get("_solar_dump_state")
    if state is None:
        state = SolarDumpState()
        coordinator._solar_dump_state = state
    return state


def _optimistic(coordinator: PlumDataUpdateCoordinator, **values: Any) -> None:
    """Nudge the coordinator's cache so entities reflect a write immediately
    instead of waiting for the next poll -- the same in-place update +
    listener notify that coordinator.async_set_value does. Safe to call any
    time, including during teardown."""
    with contextlib.suppress(Exception):
        data = coordinator.data
        if isinstance(data, dict):
            data.update(values)
            coordinator.async_set_updated_data(data)


def _threshold(
    coordinator: PlumDataUpdateCoordinator, attr: str, override: float | None = None
) -> float | None:
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


async def _confirmed_write(dev: PlumDevice, slug: str, value: int, tries: int = 3) -> bool:
    """Write and wait for the boiler's own confirmation, retrying."""
    for attempt in range(1, tries + 1):
        if await dev.set_value(slug, value):
            return True
        _LOGGER.debug("solar_to_buffer: write %s=%s not confirmed (try %d)", slug, value, attempt)
        await asyncio.sleep(1.0)
    return False


async def _restore(
    hass: HomeAssistant, coordinator: PlumDataUpdateCoordinator, entry_id: str, *, exit_manual: bool
) -> None:
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


async def _hold(dev: PlumDevice, hold_s: int | None, stop_temp: float | None) -> None:
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
    coordinator: PlumDataUpdateCoordinator,
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
        state = _state(coordinator)
        if state.task is asyncio.current_task():
            state.task = None
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


async def _replace_run(
    hass: HomeAssistant,
    coordinator: PlumDataUpdateCoordinator,
    entry_id: str,
    coro: Coroutine[Any, Any, None],
    name: str,
    owner: str,
) -> None:
    state = _state(coordinator)
    existing, state.task = state.task, None
    if existing and not existing.done():
        _LOGGER.info("solar_to_buffer: a run is active for %s -- replacing it", entry_id)
        existing.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await existing
    state.owner = owner
    state.task = hass.async_create_task(coro, name=name)


async def async_start_hold(
    hass: HomeAssistant,
    coordinator: PlumDataUpdateCoordinator,
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
        coordinator,
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
            coordinator,
            entry_id,
            _dump_lifecycle(hass, coordinator, entry_id, hold_s, start_override, stop_override),
            f"{DOMAIN} solar_to_buffer {entry_id}",
            "service",
        )


async def async_stop_for_entry(hass: HomeAssistant, coordinator: PlumDataUpdateCoordinator) -> None:
    """Cancel a running manual-mode session for one entry and wait for its
    restore. Used by the switch's turn_off, by the HA-stop listener and by
    async_unload_entry (called BEFORE the device socket is closed, so the
    restore writes still go out).
    """
    state = _state(coordinator)
    state.owner = None
    task, state.task = state.task, None
    if task and not task.done():
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task


async def async_register_services(hass: HomeAssistant) -> None:
    """Register plum_ecomax.solar_to_buffer once (idempotent)."""
    if hass.services.has_service(DOMAIN, SERVICE_SOLAR_TO_BUFFER):
        return

    async def _service(call: ServiceCall) -> None:
        await _handle_solar_to_buffer(hass, call)

    hass.services.async_register(
        DOMAIN, SERVICE_SOLAR_TO_BUFFER, _service, schema=SOLAR_TO_BUFFER_SCHEMA
    )


def async_register_stop_listener(
    hass: HomeAssistant, entry: PlumConfigEntry, coordinator: PlumDataUpdateCoordinator
) -> None:
    """On HA shutdown, stop this entry's session so the boiler is written back
    to automatic. Unsubscribed with the entry (async_on_unload)."""

    async def _on_stop(_event: Event) -> None:
        await async_stop_for_entry(hass, coordinator)

    entry.async_on_unload(hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, _on_stop))


async def async_unregister_services(hass: HomeAssistant) -> None:
    """Drop the service when the last config entry unloads."""
    if not hass.config_entries.async_loaded_entries(DOMAIN):
        hass.services.async_remove(DOMAIN, SERVICE_SOLAR_TO_BUFFER)
