"""Unit tests for the PlumEconetCalendar entity."""
import pytest
from unittest.mock import MagicMock, patch
from datetime import datetime, timedelta, timezone

from custom_components.plum_ecomax.calendar import PlumEconetCalendar
from custom_components.plum_ecomax.const import DOMAIN

# --- FIX: Helper pour ajouter une timezone UTC aux datetimes naïfs ---
def mock_as_local(dt):
    """Simule dt_util.as_local en ajoutant UTC si manquant."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt

@pytest.fixture
def mock_coordinator():
    coord = MagicMock()
    coord.data = {}
    coord.device.params_map = {
        "circuit1mondayam": {"name": "Test"},
        "hdwmondayam": {"name": "Test DHW"}
    }
    return coord

@pytest.fixture
def mock_entry():
    entry = MagicMock()
    entry.entry_id = "test_entry_id"
    return entry

def test_calendar_init_circuit(mock_coordinator, mock_entry):
    calendar = PlumEconetCalendar(mock_coordinator, mock_entry, "circuit", 1)
    assert calendar.name == "Calendar Circuit 1"
    assert calendar.unique_id == f"{DOMAIN}_test_entry_id_calendar_circuit_1"

def test_calendar_init_hdw(mock_coordinator, mock_entry):
    calendar = PlumEconetCalendar(mock_coordinator, mock_entry, "hdw", 0)
    assert calendar.name == "DHW Calendar"
    assert calendar.unique_id == f"{DOMAIN}_test_entry_id_calendar_hdw"

@pytest.mark.asyncio
async def test_get_events_decoding_logic(mock_coordinator, mock_entry):
    calendar = PlumEconetCalendar(mock_coordinator, mock_entry, "circuit", 1)
    
    # 06:00 to 08:00 AM Comfort
    # Bits 12, 13, 14, 15 = 1 -> Value 61440
    mock_coordinator.data["circuit1mondayam"] = 61440
    mock_coordinator.data["circuit1mondaypm"] = 0
    
    # Dates de requête (timezone-aware pour être propre)
    start_date = datetime(2024, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
    end_date = datetime(2024, 1, 1, 23, 59, 59, tzinfo=timezone.utc)
    
    hass = MagicMock()
    
    # --- CORRECTION ICI : On utilise notre fonction mock_as_local ---
    with patch("custom_components.plum_ecomax.calendar.dt_util.as_local", side_effect=mock_as_local):
        events = await calendar.async_get_events(hass, start_date, end_date)
    
    assert len(events) == 3
    
    # On vérifie les dates avec timezone UTC
    # Event 1: Eco 00:00 -> 06:00
    assert events[0].summary == "Eco"
    assert events[0].start == datetime(2024, 1, 1, 0, 0, tzinfo=timezone.utc)
    assert events[0].end   == datetime(2024, 1, 1, 6, 0, tzinfo=timezone.utc)
    
    # Event 2: Confort 06:00 -> 08:00
    # NOTE: Vérifiez si votre code utilise "Confort" ou "Actif". 
    # J'utilise "Confort" ici comme dans votre dernière demande.
    assert events[1].summary == "Active" # ou "Confort" selon votre code calendar.py
    assert events[1].start == datetime(2024, 1, 1, 6, 0, tzinfo=timezone.utc)
    assert events[1].end   == datetime(2024, 1, 1, 8, 0, tzinfo=timezone.utc)

@pytest.mark.asyncio
async def test_get_events_hdw_mapping(mock_coordinator, mock_entry):
    calendar = PlumEconetCalendar(mock_coordinator, mock_entry, "hdw", 0)
    
    mock_coordinator.data["hdwmondayam"] = 16777215 # Full AM
    mock_coordinator.data["hdwmondaypm"] = 0
    
    start_date = datetime(2024, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
    end_date = datetime(2024, 1, 1, 23, 59, 59, tzinfo=timezone.utc)
    hass = MagicMock()

    with patch("custom_components.plum_ecomax.calendar.dt_util.as_local", side_effect=mock_as_local):
        events = await calendar.async_get_events(hass, start_date, end_date)
        
    assert len(events) == 2
    assert events[0].summary == "Active"
    assert events[0].end == datetime(2024, 1, 1, 12, 0, tzinfo=timezone.utc)