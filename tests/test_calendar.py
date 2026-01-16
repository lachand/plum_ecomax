"""Unit tests for the PlumEconetCalendar entity."""
import pytest
from unittest.mock import MagicMock, AsyncMock, patch
from datetime import datetime, timedelta

from custom_components.plum_ecomax.calendar import PlumEconetCalendar
from custom_components.plum_ecomax.const import DOMAIN

# We mock datetime to have a fixed reference time if needed, 
# but for calendars we usually test specific requested ranges.

@pytest.fixture
def mock_coordinator():
    """Create a mock coordinator with data storage."""
    coord = MagicMock()
    coord.data = {}
    coord.device.params_map = {
        "circuit1mondayam": {"name": "Test"},
        "hdwmondayam": {"name": "Test DHW"}
    }
    return coord

@pytest.fixture
def mock_entry():
    """Mock config entry."""
    entry = MagicMock()
    entry.entry_id = "test_entry_id"
    return entry

def test_calendar_init_circuit(mock_coordinator, mock_entry):
    """Test initialization for a Heating Circuit calendar."""
    # type="circuit", index=1
    calendar = PlumEconetCalendar(mock_coordinator, mock_entry, "circuit", 1)
    
    assert calendar.name == "Calendrier Circuit 1"
    assert calendar.unique_id == f"{DOMAIN}_test_entry_id_calendar_circuit_1"
    
    # Verify Device Info links to the Circuit device
    dev_info = calendar.device_info
    assert dev_info["name"] == "Circuit 1"
    assert "plum_hdw" not in str(dev_info["identifiers"])

def test_calendar_init_hdw(mock_coordinator, mock_entry):
    """Test initialization for the DHW (Water Heater) calendar."""
    # type="hdw", index=0
    calendar = PlumEconetCalendar(mock_coordinator, mock_entry, "hdw", 0)
    
    assert calendar.name == "Calendrier ECS"
    assert calendar.unique_id == f"{DOMAIN}_test_entry_id_calendar_hdw"
    
    # Verify Device Info links to the HDW device
    dev_info = calendar.device_info
    assert dev_info["name"] == "Eau Chaude Sanitaire"
    assert ("plum_ecomax", "plum_hdw") in dev_info["identifiers"]

@pytest.mark.asyncio
async def test_get_events_decoding_logic(mock_coordinator, mock_entry):
    """
    Test the core bitmask decoding logic.
    We simulate a specific schedule pattern for a Monday.
    """
    calendar = PlumEconetCalendar(mock_coordinator, mock_entry, "circuit", 1)
    
    # --- SCENARIO SETUP ---
    # We want Comfort mode from 06:00 to 08:00 AM.
    # Slots:
    # 06:00 is 6*2 = 12th slot (index 12)
    # 08:00 is 8*2 = 16th slot (index 16, excluded)
    # So bits 12, 13, 14, 15 must be 1.
    # Binary: ...001111000000000000 (starting at bit 0 on the right)
    # Value = 2^12 + 2^13 + 2^14 + 2^15 = 4096 + 8192 + 16384 + 32768 = 61440
    
    monday_am_value = 61440
    monday_pm_value = 0 # All Eco in the afternoon
    
    # Populate coordinator data
    mock_coordinator.data["circuit1mondayam"] = monday_am_value
    mock_coordinator.data["circuit1mondaypm"] = monday_pm_value
    
    # Define test range (Just one Monday)
    # Let's say Jan 1st 2024 was a Monday
    start_date = datetime(2024, 1, 1, 0, 0, 0)
    end_date = datetime(2024, 1, 1, 23, 59, 59)
    
    # We mock 'hass' because async_get_events uses it (though strictly not needed for logic)
    hass = MagicMock()
    
    # --- EXECUTION ---
    # We patch dt_util.as_local to avoid timezone complexity in unit tests
    # It will just return the datetime as-is
    with patch("custom_components.plum_ecomax.calendar.dt_util.as_local", side_effect=lambda x: x):
        events = await calendar.async_get_events(hass, start_date, end_date)
    
    # --- ASSERTIONS ---
    # We expect 3 events for this day:
    # 1. Eco    : 00:00 -> 06:00
    # 2. Comfort: 06:00 -> 08:00
    # 3. Eco    : 08:00 -> 00:00 (Next day)
    
    assert len(events) == 3
    
    # Event 1: Eco
    assert events[0].summary == "Éco"
    assert events[0].start == datetime(2024, 1, 1, 0, 0)
    assert events[0].end   == datetime(2024, 1, 1, 6, 0)
    
    # Event 2: Comfort (The one we programmed)
    assert events[1].summary == "Confort" # or "Actif" depending on your code
    assert events[1].start == datetime(2024, 1, 1, 6, 0)
    assert events[1].end   == datetime(2024, 1, 1, 8, 0)
    
    # Event 3: Eco (Afternoon)
    assert events[2].summary == "Éco"
    assert events[2].start == datetime(2024, 1, 1, 8, 0)
    # The last event goes to midnight of the next day
    assert events[2].end   == datetime(2024, 1, 2, 0, 0)

@pytest.mark.asyncio
async def test_get_events_hdw_mapping(mock_coordinator, mock_entry):
    """Test that HDW calendar reads the correct 'hdw...' slugs."""
    calendar = PlumEconetCalendar(mock_coordinator, mock_entry, "hdw", 0)
    
    # Set Monday AM to all 1s (Full Comfort morning) -> Value 16777215 (2^24 - 1)
    mock_coordinator.data["hdwmondayam"] = 16777215
    mock_coordinator.data["hdwmondaypm"] = 0
    
    start_date = datetime(2024, 1, 1, 0, 0, 0) # Monday
    end_date = datetime(2024, 1, 1, 23, 59, 59)
    hass = MagicMock()

    with patch("custom_components.plum_ecomax.calendar.dt_util.as_local", side_effect=lambda x: x):
        events = await calendar.async_get_events(hass, start_date, end_date)
        
    # Expect: 00:00-12:00 Comfort, 12:00-00:00 Eco
    assert len(events) == 2
    assert events[0].summary == "Actif" # HDW uses "Actif"
    assert events[0].end == datetime(2024, 1, 1, 12, 0)