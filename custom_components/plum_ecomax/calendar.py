import logging
import datetime
from typing import Any, List, Optional

from homeassistant.components.calendar import CalendarEntity, CalendarEvent
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util
from homeassistant.helpers.entity import DeviceInfo

from .const import DOMAIN, WEEKDAY_TO_SLUGS, CONF_ACTIVE_CIRCUITS

_LOGGER = logging.getLogger(__name__)

async def async_setup_entry(
    hass: HomeAssistant,
    entry: Any,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """
    @brief Sets up Calendar entities for Circuits and DHW.
    """
    coordinator = hass.data[DOMAIN][entry.entry_id]
    selected_circuits = entry.data.get(CONF_ACTIVE_CIRCUITS, [])
    entities = []

    # 1. Calendriers des Circuits de Chauffage
    for circuit_id in selected_circuits:
        if f"circuit{circuit_id}mondayam" in coordinator.device.params_map:
            entities.append(PlumEconetCalendar(coordinator, entry, "circuit", circuit_id))

    # 2. Calendrier Eau Chaude Sanitaire (HDW)
    if "hdwmondayam" in coordinator.device.params_map:
        entities.append(PlumEconetCalendar(coordinator, entry, "hdw", 0))

    async_add_entities(entities)


class PlumEconetCalendar(CoordinatorEntity, CalendarEntity):
    """
    @class PlumEconetCalendar
    @brief Converts Plum binary registers to HA calendar events.
    @details Supports both Heating Circuits and DHW (HDW).
    """

    def __init__(self, coordinator, entry, system_type: str, index: int):
        """
        @param system_type: 'circuit' or 'hdw'
        @param index: Circuit ID (1..7) or 0 for HDW
        """
        super().__init__(coordinator)
        self._entry_id = entry.entry_id
        self._system_type = system_type # 'circuit' ou 'hdw'
        self._index = index
        self._event = None

        if self._system_type == "circuit":
            self._attr_name = f"Calendrier Circuit {index}"
            self._attr_unique_id = f"{DOMAIN}_{entry.entry_id}_calendar_circuit_{index}"
        else:
            self._attr_name = "Calendrier ECS"
            self._attr_unique_id = f"{DOMAIN}_{entry.entry_id}_calendar_hdw"

    @property
    def event(self) -> CalendarEvent | None:
        return None

    async def async_get_events(
        self, hass: HomeAssistant, start_date: datetime.datetime, end_date: datetime.datetime
    ) -> List[CalendarEvent]:
        """
        @brief Generates events based on the system type (Circuit/HDW).
        """
        events = []
        current_day = start_date
        
        while current_day <= end_date:
            weekday = current_day.weekday()
            slugs = WEEKDAY_TO_SLUGS.get(weekday)
            
            if not slugs: 
                current_day += datetime.timedelta(days=1)
                continue

            suffix_am, suffix_pm = slugs
            
            # Construction dynamique du slug selon le type
            if self._system_type == "circuit":
                slug_am = f"circuit{self._index}{suffix_am}"
                slug_pm = f"circuit{self._index}{suffix_pm}"
            else:
                slug_am = f"hdw{suffix_am}"
                slug_pm = f"hdw{suffix_pm}"
            
            val_am = self.coordinator.data.get(slug_am)
            val_pm = self.coordinator.data.get(slug_pm)
            
            if val_am is not None and val_pm is not None:
                try:
                    day_events = self._decode_day(current_day, int(val_am), int(val_pm))
                    events.extend(day_events)
                except (ValueError, TypeError):
                    pass

            current_day += datetime.timedelta(days=1)
            
        return events

    def _decode_day(self, date_base: datetime.datetime, val_am: int, val_pm: int) -> List[CalendarEvent]:
        events = []
        slots = []
        for i in range(24): slots.append((val_am >> i) & 1 == 1)
        for i in range(24): slots.append((val_pm >> i) & 1 == 1)
        
        if not slots: return []

        current_start_slot = 0
        current_state = slots[0]

        for i in range(1, 48):
            state = slots[i]
            if state != current_state:
                events.append(self._create_event(date_base, current_start_slot, i, current_state))
                current_state = state
                current_start_slot = i
        
        events.append(self._create_event(date_base, current_start_slot, 48, current_state))
        return events

    def _create_event(self, date_base, start_slot, end_slot, is_active) -> CalendarEvent:
        start_h = start_slot // 2
        start_m = (start_slot % 2) * 30
        end_h = end_slot // 2
        end_m = (end_slot % 2) * 30
        
        dt_start = dt_util.as_local(date_base.replace(hour=int(start_h), minute=int(start_m), second=0, microsecond=0))
        
        if end_h >= 24:
            dt_end = dt_util.as_local(date_base.replace(hour=0, minute=0, second=0, microsecond=0) + datetime.timedelta(days=1))
        else:
            dt_end = dt_util.as_local(date_base.replace(hour=int(end_h), minute=int(end_m), second=0, microsecond=0))

        if is_active:
            summary = "Actif"
            description = "Chauffage/ECS Actif (Jour)"
        else:
            summary = "Éco"
            description = "Chauffage/ECS Réduit (Nuit)"

        return CalendarEvent(
            summary=summary,
            start=dt_start,
            end=dt_end,
            description=description
        )
    
    @property
    def device_info(self) -> DeviceInfo:
        """
        @brief Links the calendar to the correct device (Circuit X or HDW).
        """
        if self._system_type == "circuit":
            return DeviceInfo(
                identifiers={(DOMAIN, f"{self._entry_id}_circuit_{self._index}")},
                name=f"Circuit {self._index}",
                manufacturer="Plum",
                via_device=(DOMAIN, self._entry_id),
            )
        else:
            # Lien vers l'appareil Eau Chaude Sanitaire créé dans water_heater.py
            return DeviceInfo(
                identifiers={(DOMAIN, "plum_hdw")},
                name="Eau Chaude Sanitaire",
                manufacturer="Plum",
                model="Gestionnaire ECS",
                via_device=(DOMAIN, self._entry_id),
            )