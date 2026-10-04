import datetime
import logging
from typing import Any

from homeassistant.components.calendar import CalendarEntity, CalendarEvent
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .const import CONF_ACTIVE_CIRCUITS, DOMAIN, WEEKDAY_TO_SLUGS
from .device import circuit_device_info, hdw_device_info
from .schedule import am_pm_to_slots

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: Any,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Sets up Calendar entities for Circuits and DHW.

    Args:
        hass: The Home Assistant instance.
        entry: The config entry containing the configuration.
        async_add_entities: Callback to add entities to Home Assistant.
    """
    coordinator = entry.runtime_data
    selected_circuits = entry.data.get(CONF_ACTIVE_CIRCUITS, [])
    entities = []

    # 1. Circuit's calendar
    for circuit_id in selected_circuits:
        if f"circuit{circuit_id}mondayam" in coordinator.device.params_map:
            entities.append(PlumEconetCalendar(coordinator, entry, "circuit", circuit_id))

    # 2. HDW's calendar
    if "hdwmondayam" in coordinator.device.params_map:
        entities.append(PlumEconetCalendar(coordinator, entry, "hdw", 0))

    async_add_entities(entities)


class PlumEconetCalendar(CoordinatorEntity, CalendarEntity):
    """Representation of a Plum EcoMAX Calendar.

    This entity reads binary registers (AM/PM bitmasks) and converts them
    into readable Home Assistant Calendar events. It supports both
    heating circuits and domestic hot water (DHW).
    """

    _attr_has_entity_name = True
    _attr_translation_key = "schedule"

    def __init__(self, coordinator, entry, system_type: str, index: int):
        """Initializes the calendar entity.

        Args:
            coordinator: The data update coordinator.
            entry: The config entry.
            system_type: The type of system ('circuit' or 'hdw').
            index: The circuit index (1-7) or 0 for HDW.
        """
        super().__init__(coordinator)
        self._entry_id = entry.entry_id
        self._system_type = system_type  # 'circuit' or 'hdw'
        self._index = index
        self._event = None

        if self._system_type == "circuit":
            self._attr_unique_id = f"{DOMAIN}_{entry.entry_id}_calendar_circuit_{index}"
        else:
            self._attr_unique_id = f"{DOMAIN}_{entry.entry_id}_calendar_hdw"

    @property
    def event(self) -> CalendarEvent | None:
        """The comfort block currently running, or the next one within a week."""
        now = dt_util.now()
        for event in self._events_between(
            now - datetime.timedelta(days=1), now + datetime.timedelta(days=8)
        ):
            if event.end > now:
                return event
        return None

    async def async_get_events(
        self, hass: HomeAssistant, start_date: datetime.datetime, end_date: datetime.datetime
    ) -> list[CalendarEvent]:
        """Comfort blocks between start_date and end_date (eco is the gap)."""
        return [
            e
            for e in self._events_between(start_date, end_date)
            if e.end > start_date and e.start < end_date
        ]

    def _slug_pair(self, weekday: int) -> tuple[str, str]:
        suffix_am, suffix_pm = WEEKDAY_TO_SLUGS[weekday]
        prefix = f"circuit{self._index}" if self._system_type == "circuit" else "hdw"
        return f"{prefix}{suffix_am}", f"{prefix}{suffix_pm}"

    def _events_between(
        self, start_date: datetime.datetime, end_date: datetime.datetime
    ) -> list[CalendarEvent]:
        events: list[CalendarEvent] = []
        day = dt_util.as_local(start_date).replace(hour=0, minute=0, second=0, microsecond=0)
        end = dt_util.as_local(end_date)
        while day <= end:
            slug_am, slug_pm = self._slug_pair(day.weekday())
            val_am = self.coordinator.data.get(slug_am)
            val_pm = self.coordinator.data.get(slug_pm)
            if val_am is not None and val_pm is not None:
                try:
                    events.extend(self._decode_day(day, int(val_am), int(val_pm)))
                except (ValueError, TypeError):
                    pass
            day += datetime.timedelta(days=1)
        return events

    def _decode_day(
        self, date_base: datetime.datetime, val_am: int, val_pm: int
    ) -> list[CalendarEvent]:
        """One CalendarEvent per contiguous run of comfort slots that day.
        Eco time is simply the gaps -- no event.
        """
        slots = am_pm_to_slots(val_am, val_pm)
        events: list[CalendarEvent] = []
        run_start: int | None = None
        for i in range(len(slots) + 1):
            comfort = i < len(slots) and slots[i]
            if comfort and run_start is None:
                run_start = i
            elif not comfort and run_start is not None:
                events.append(self._create_event(date_base, run_start, i))
                run_start = None
        return events

    def _create_event(self, date_base, start_slot, end_slot) -> CalendarEvent:
        """A comfort CalendarEvent spanning [start_slot, end_slot) 30-min slots."""
        midnight = dt_util.as_local(date_base.replace(hour=0, minute=0, second=0, microsecond=0))
        dt_start = midnight + datetime.timedelta(minutes=30 * start_slot)
        dt_end = midnight + datetime.timedelta(minutes=30 * end_slot)
        return CalendarEvent(
            summary="Comfort",
            start=dt_start,
            end=dt_end,
            description="Heating/DHW comfort period",
        )

    @property
    def device_info(self) -> DeviceInfo:
        """Links the calendar to the correct device registry entry.

        Returns:
            DeviceInfo: Configuration to link this entity to a circuit or HDW device.
        """
        if self._system_type == "circuit":
            return circuit_device_info(self._entry_id, self._index)
        return hdw_device_info(self._entry_id)
