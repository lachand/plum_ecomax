"""Weekly comfort/eco schedule encoding + the plum_ecomax.set_schedule service.

The boiler stores each day's schedule as two 24-bit registers (AM = 00:00
to 12:00, PM = 12:00 to 24:00), one bit per 30-minute slot, set = comfort.
`circuitN{day}am` / `circuitN{day}pm` for circuits, `hdw{day}am/pm` for DHW,
`circulation{day}am/pm` for the DHW circulation pump.

The calendar entity reads these; this service writes them, which is a more
predictable editing surface for a *weekly repeating* pattern than mapping
individual dated calendar events onto it.
"""

from __future__ import annotations

import logging
from datetime import time

import voluptuous as vol
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers import config_validation as cv

from .const import CONF_ACTIVE_CIRCUITS, DOMAIN, WEEKDAY_TO_SLUGS

_LOGGER = logging.getLogger(__name__)

SERVICE_SET_SCHEDULE = "set_schedule"

SLOT_MINUTES = 30
SLOTS_PER_DAY = 48
SLOTS_PER_HALF = 24

# Service field -> weekday index (matches datetime.weekday(): Monday = 0).
_DAY_NAMES = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
}

_BLOCK_SCHEMA = vol.Schema(
    {
        vol.Required("from"): cv.time,
        vol.Required("to"): cv.time,
    }
)

SET_SCHEDULE_SCHEMA = vol.Schema(
    {
        vol.Optional("circuit"): vol.All(vol.Coerce(int), vol.Range(min=1, max=7)),
        vol.Required("days"): vol.All(
            cv.ensure_list, [vol.In([*_DAY_NAMES, "everyday", "weekday", "weekend"])]
        ),
        vol.Optional("comfort_blocks", default=list): vol.All(cv.ensure_list, [_BLOCK_SCHEMA]),
    }
)


def am_pm_to_slots(am: int, pm: int) -> list[bool]:
    """Two 24-bit registers -> 48 booleans (slot i is comfort)."""
    slots = [bool((am >> i) & 1) for i in range(SLOTS_PER_HALF)]
    slots += [bool((pm >> i) & 1) for i in range(SLOTS_PER_HALF)]
    return slots


def slots_to_am_pm(comfort_slots: set[int]) -> tuple[int, int]:
    """Inverse of am_pm_to_slots: a set of comfort slot indices (0-47) ->
    (am, pm) register values.
    """
    am = sum(1 << i for i in comfort_slots if 0 <= i < SLOTS_PER_HALF)
    pm = sum(
        1 << (i - SLOTS_PER_HALF) for i in comfort_slots if SLOTS_PER_HALF <= i < SLOTS_PER_DAY
    )
    return am, pm


def time_range_to_slots(start: time, end: time) -> set[int]:
    """Half-open [start, end) as 30-minute slot indices. end == 00:00 (or
    anything <= start) means "to midnight".
    """
    start_min = start.hour * 60 + start.minute
    end_min = end.hour * 60 + end.minute
    start_slot = start_min // SLOT_MINUTES
    # ceil for the end so e.g. 06:15 still covers the 06:00-06:30 slot;
    # end <= start means "through midnight".
    end_slot = SLOTS_PER_DAY if end_min <= start_min else -(-end_min // SLOT_MINUTES)
    return set(range(max(start_slot, 0), min(end_slot, SLOTS_PER_DAY)))


def _resolve_days(names: list[str]) -> set[int]:
    out: set[int] = set()
    for name in names:
        if name == "everyday":
            out |= set(range(7))
        elif name == "weekday":
            out |= {0, 1, 2, 3, 4}
        elif name == "weekend":
            out |= {5, 6}
        else:
            out.add(_DAY_NAMES[name])
    return out


def _prefix(call: ServiceCall) -> str:
    circuit = call.data.get("circuit")
    return f"circuit{circuit}" if circuit is not None else "hdw"


async def _handle_set_schedule(hass: HomeAssistant, call: ServiceCall) -> None:
    prefix = _prefix(call)
    days = _resolve_days(call.data["days"])

    comfort_slots: set[int] = set()
    for block in call.data["comfort_blocks"]:
        comfort_slots |= time_range_to_slots(block["from"], block["to"])
    am, pm = slots_to_am_pm(comfort_slots)

    coordinators = [e.runtime_data for e in hass.config_entries.async_loaded_entries(DOMAIN)]
    if not coordinators:
        _LOGGER.warning("set_schedule called but no Plum EcoMAX config entry is loaded")
        return

    for coordinator in coordinators:
        selected = coordinator.config_entry.data.get(CONF_ACTIVE_CIRCUITS, [])
        circuit = call.data.get("circuit")
        if circuit is not None and str(circuit) not in selected:
            continue
        for day_idx in days:
            suffix_am, suffix_pm = WEEKDAY_TO_SLUGS[day_idx]
            slug_am, slug_pm = f"{prefix}{suffix_am}", f"{prefix}{suffix_pm}"
            if slug_am not in coordinator.device.params_map:
                continue
            _LOGGER.info("set_schedule: %s day=%d -> am=0x%06X pm=0x%06X", prefix, day_idx, am, pm)
            await coordinator.async_set_value(slug_am, am)
            await coordinator.async_set_value(slug_pm, pm)


async def async_register_services(hass: HomeAssistant) -> None:
    """Register plum_ecomax.set_schedule once (idempotent)."""
    if hass.services.has_service(DOMAIN, SERVICE_SET_SCHEDULE):
        return

    async def _service(call: ServiceCall) -> None:
        await _handle_set_schedule(hass, call)

    hass.services.async_register(DOMAIN, SERVICE_SET_SCHEDULE, _service, schema=SET_SCHEDULE_SCHEMA)


async def async_unregister_services(hass: HomeAssistant) -> None:
    """Drop the service when the last config entry unloads."""
    if not hass.config_entries.async_loaded_entries(DOMAIN):
        hass.services.async_remove(DOMAIN, SERVICE_SET_SCHEDULE)
