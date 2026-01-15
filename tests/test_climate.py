"""Unit tests for Climate entities."""
import pytest
from unittest.mock import MagicMock, AsyncMock

# Assurez-vous que l'import correspond au nom de la classe dans votre fichier climate.py
from custom_components.plum_ecomax.climate import PlumEcomaxClimate
from custom_components.plum_ecomax.const import (
    PRESET_COMFORT, PRESET_ECO, PRESET_AWAY,
    HVAC_MODE_HEAT, HVAC_MODE_AUTO, HVAC_MODE_OFF
)

@pytest.fixture
def climate_entity():
    """Create a climate entity with mocked coordinator."""
    coordinator = MagicMock()
    coordinator.data = {}
    coordinator.async_set_value = AsyncMock(return_value=True)
    
    mock_entry = MagicMock()
    mock_entry.entry_id = "12345"
    
    # --- CORRECTION ICI : Ajout de l'argument 'name' ("Test Climate") ---
    entity = PlumEcomaxClimate(
        coordinator, 
        mock_entry,
        "Test Climate",   # <--- Argument ajouté ici
        "temp_curr",      # temp_slug
        "target_comfort", # target_slug
        "target_eco",     # eco_slug
        "mode_slug"       # mode_slug
    )
    return entity

def test_hvac_mode_mapping(climate_entity):
    """Test conversion from Plum ID to HA HVAC Mode."""
    # 3 = Auto
    climate_entity.coordinator.data["mode_slug"] = 3
    assert climate_entity.hvac_mode == HVAC_MODE_AUTO
    
    # 1 = Comfort (Heat)
    climate_entity.coordinator.data["mode_slug"] = 1
    assert climate_entity.hvac_mode == HVAC_MODE_HEAT
    
    # 0 = Antifreeze (Heat, not Off in our logic)
    climate_entity.coordinator.data["mode_slug"] = 0
    assert climate_entity.hvac_mode == HVAC_MODE_HEAT

def test_target_temperature_dual_setpoint(climate_entity):
    """Test that the entity returns the correct target based on preset."""
    # Setup data
    climate_entity.coordinator.data["target_comfort"] = 21.0
    climate_entity.coordinator.data["target_eco"] = 19.0
    
    # Case 1: Comfort Mode -> Should return Comfort Target
    climate_entity.coordinator.data["mode_slug"] = 1 # Comfort
    assert climate_entity.target_temperature == 21.0
    
    # Case 2: Eco Mode -> Should return Eco Target
    climate_entity.coordinator.data["mode_slug"] = 2 # Eco
    assert climate_entity.target_temperature == 19.0

@pytest.mark.asyncio
async def test_set_temperature_routing(climate_entity):
    """Test that set_temperature writes to the correct slug."""
    
    # 1. While in Comfort Mode -> Write to target_comfort
    climate_entity.coordinator.data["mode_slug"] = 1
    await climate_entity.async_set_temperature(temperature=22)
    climate_entity.coordinator.async_set_value.assert_called_with("target_comfort", 22)
    
    # 2. While in Eco Mode -> Write to target_eco
    climate_entity.coordinator.data["mode_slug"] = 2
    await climate_entity.async_set_temperature(temperature=18)
    climate_entity.coordinator.async_set_value.assert_called_with("target_eco", 18)
