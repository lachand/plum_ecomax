import logging
import datetime
from typing import Any, List

from homeassistant.components.calendar import CalendarEntity, CalendarEvent
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .const import DOMAIN, WEEKDAY_TO_SLUGS, CONF_ACTIVE_CIRCUITS

_LOGGER = logging.getLogger(__name__)

async def async_setup_entry(
    hass: HomeAssistant,
    entry: Any,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """
    @brief Configure les entités Calendrier.
    """
    coordinator = hass.data[DOMAIN][entry.entry_id]
    selected_circuits = entry.data.get(CONF_ACTIVE_CIRCUITS, [])
    entities = []

    _LOGGER.info(f"Configuration des calendriers pour les circuits : {selected_circuits}")

    for circuit_id in selected_circuits:
        # On vérifie si le paramètre "mondayam" existe pour ce circuit
        # C'est un bon indicateur pour savoir si le circuit supporte les plannings
        test_slug = f"circuit{circuit_id}mondayam"
        
        if test_slug in coordinator.device.params_map:
            entities.append(PlumEconetCalendar(coordinator, entry, circuit_id))
        else:
            _LOGGER.debug(f"Pas de paramètres calendrier détectés pour le Circuit {circuit_id}")

    async_add_entities(entities)


class PlumEconetCalendar(CoordinatorEntity, CalendarEntity):
    """
    @class PlumEconetCalendar
    @brief Convertit les registres binaires Plum en événements de calendrier HA.
    """

    def __init__(self, coordinator, entry, circuit_id):
        super().__init__(coordinator)
        self._entry_id = entry.entry_id
        self._circuit_id = circuit_id
        self._attr_name = f"Calendrier Circuit {circuit_id}"
        self._attr_unique_id = f"{DOMAIN}_{entry.entry_id}_calendar_circuit_{circuit_id}"
        self._event = None

    @property
    def event(self) -> CalendarEvent | None:
        """Retourne le prochain événement (non implémenté pour l'instant)."""
        return None

    async def async_get_events(
        self, hass: HomeAssistant, start_date: datetime.datetime, end_date: datetime.datetime
    ) -> List[CalendarEvent]:
        """
        @brief Génère la liste des événements pour la vue Calendrier.
        """
        events = []
        current_day = start_date
        
        # On boucle jour par jour sur la plage demandée par l'interface
        while current_day <= end_date:
            weekday = current_day.weekday() # 0=Lundi
            
            suffix_am, suffix_pm = WEEKDAY_TO_SLUGS.get(weekday)
            slug_am = f"circuit{self._circuit_id}{suffix_am}"
            slug_pm = f"circuit{self._circuit_id}{suffix_pm}"
            
            val_am = self.coordinator.data.get(slug_am)
            val_pm = self.coordinator.data.get(slug_pm)
            
            if val_am is not None and val_pm is not None:
                try:
                    # Conversion en Entier (au cas où ce serait du float/string)
                    day_events = self._decode_day(current_day, int(val_am), int(val_pm))
                    events.extend(day_events)
                except (ValueError, TypeError):
                    pass

            current_day += datetime.timedelta(days=1)
            
        return events

    def _decode_day(self, date_base: datetime.datetime, val_am: int, val_pm: int) -> List[CalendarEvent]:
        """
        @brief Décode 48 bits (24 AM + 24 PM) en plages horaires.
        """
        events = []
        
        # Helper pour lire le bit N
        def is_bit_set(val, pos):
            return (val >> pos) & 1 == 1

        # Construction de la grille de 48 demi-heures (True = Confort, False = Eco)
        slots = []
        for i in range(24): slots.append(is_bit_set(val_am, i)) # 00h00 - 12h00
        for i in range(24): slots.append(is_bit_set(val_pm, i)) # 12h00 - 00h00
        
        # Fusion des créneaux contigus
        start_slot = None
        
        for i, is_active in enumerate(slots):
            if is_active:
                if start_slot is None:
                    start_slot = i # Début d'une plage Confort
            else:
                if start_slot is not None:
                    # Fin d'une plage Confort -> On crée l'événement
                    events.append(self._create_event(date_base, start_slot, i))
                    start_slot = None
        
        # Cas où la plage se termine à minuit pile
        if start_slot is not None:
             events.append(self._create_event(date_base, start_slot, 48))

        return events

    def _create_event(self, date_base, start_slot, end_slot) -> CalendarEvent:
        """Crée un objet CalendarEvent Home Assistant."""
        # Slot 0 = 00:00, Slot 1 = 00:30, Slot 2 = 01:00...
        start_h = start_slot // 2
        start_m = (start_slot % 2) * 30
        
        end_h = end_slot // 2
        end_m = (end_slot % 2) * 30
        
        # Construction des dates locales
        dt_start = dt_util.as_local(date_base.replace(hour=int(start_h), minute=int(start_m), second=0, microsecond=0))
        
        if end_h == 24:
            dt_end = dt_util.as_local(date_base.replace(hour=0, minute=0, second=0, microsecond=0) + datetime.timedelta(days=1))
        else:
            dt_end = dt_util.as_local(date_base.replace(hour=int(end_h), minute=int(end_m), second=0, microsecond=0))

        return CalendarEvent(
            summary="Confort",
            start=dt_start,
            end=dt_end,
            description="Chauffage en mode Confort (Jour)"
        )
    
    @property
    def device_info(self):
        return {
            "identifiers": {(DOMAIN, f"{self._entry_id}_circuit_{self._circuit_id}")},
            "name": f"Circuit {self._circuit_id}",
            "manufacturer": "Plum",
            "via_device": (DOMAIN, self._entry_id),
        }