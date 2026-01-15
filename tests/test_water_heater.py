"""Unit tests for Water Heater entity."""
import pytest
from unittest.mock import MagicMock, AsyncMock
from custom_components.plum_ecomax.water_heater import PlumEcomaxWaterHeater
from homeassistant.components.water_heater import STATE_PERFORMANCE, STATE_ECO

@pytest.fixture
def dhw_entity():
    coordinator = MagicMock()
    coordinator.data = {}
    coordinator.async_set_value = AsyncMock(return_value=True)
    
    entity = PlumEcomaxWaterHeater(
        coordinator, "DHW", 
        "temp_curr", "temp_target", "temp_min", "temp_max", "mode_slug"
    )
    return entity

def test_dhw_temperature_nan(dhw_entity):
    """Test NaN protection for current temperature."""
    # Valid
    dhw_entity.coordinator.data["temp_curr"] = 45.0
    assert dhw_entity.current_temperature == 45.0
    
    # NaN
    dhw_entity.coordinator.data["temp_curr"] = float('nan')
    assert dhw_entity.current_temperature is None

def test_dhw_dynamic_limits(dhw_entity):
    """Test that min/max temp are fetched dynamically."""
    dhw_entity.coordinator.data["temp_min"] = 30
    dhw_entity.coordinator.data["temp_max"] = 55
    
    assert dhw_entity.min_temp == 30.0
    assert dhw_entity.max_temp == 55.0
    
    # Fallback if data missing
    dhw_entity.coordinator.data["temp_min"] = None
    assert dhw_entity.min_temp == 20.0 # Default

@pytest.mark.asyncio
async def test_set_operation_mode(dhw_entity):
    """Test mode setting mapping."""
    # HA 'Performance' -> Plum 1 (Manual)
    await dhw_entity.async_set_operation_mode(STATE_PERFORMANCE)
    dhw_entity.coordinator.async_set_value.assert_called_with("mode_slug", 1)
    
    # HA 'Eco' -> Plum 2 (Auto)
    await dhw_entity.async_set_operation_mode(STATE_ECO)
    dhw_entity.coordinator.async_set_value.assert_called_with("mode_slug", 2)
