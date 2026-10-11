"""Unit tests for climate.py + the coordinator's parameter detection.

Regression for the "thermostat always reports HEAT" bug: climate.py reads
and writes `circuitNactive` for the HVAC on/off state, but CLIMATE_TYPES
(the only place the coordinator's initial scan learns which circuit slugs
to poll) listed `circuitNworkstate`/`circuitNecotemp` instead -- slugs no
entity reads. So `circuitNactive` was never in `available_slugs`, never
re-polled, and `coordinator.data.get(active_slug)` stayed None forever
(-> hvac_mode always HEAT, an OFF from HA reverting at the next poll).
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from homeassistant.components.climate import HVACAction, HVACMode

from custom_components.plum_ecomax.climate import PlumEcomaxClimate
from custom_components.plum_ecomax.const import CLIMATE_TYPES
from custom_components.plum_ecomax.coordinator import PlumDataUpdateCoordinator


def _make_climate(data: dict):
    coordinator = MagicMock()
    coordinator.data = data
    coordinator.async_set_value = AsyncMock(return_value=True)
    entry = MagicMock()
    entry.entry_id = "e1"
    return PlumEcomaxClimate(
        coordinator, entry, "2", "circuit2thermostattemp", "circuit2comforttemp", "circuit2active"
    )


class TestClimateTypesCoverage:
    def test_every_slug_climate_reads_is_in_climate_types(self):
        """CLIMATE_TYPES must contain every slug climate.py can touch at
        runtime, per circuit -- current temp (+ fallback), target, active.
        """
        for i in range(1, 8):
            conf = CLIMATE_TYPES[str(i)]
            assert f"circuit{i}active" in conf
            assert f"circuit{i}comforttemp" in conf
            assert f"circuit{i}thermostattemp" in conf
            assert f"tempcircuit{i}" in conf

    def test_dead_slugs_are_gone(self):
        flat = {s for conf in CLIMATE_TYPES.values() for s in conf}
        assert not any(s.endswith("workstate") for s in flat)
        assert not any(s.endswith("ecotemp") for s in flat)


class TestHvacMode:
    def test_off_when_active_is_zero(self):
        entity = _make_climate({"circuit2active": 0})
        assert entity.hvac_mode == HVACMode.OFF

    def test_heat_when_active_is_one(self):
        entity = _make_climate({"circuit2active": 1})
        assert entity.hvac_mode == HVACMode.HEAT

    @pytest.mark.asyncio
    async def test_set_hvac_off_writes_active_zero(self):
        entity = _make_climate({"circuit2active": 1})
        await entity.async_set_hvac_mode(HVACMode.OFF)
        entity.coordinator.async_set_value.assert_awaited_with("circuit2active", 0)


class TestTemperatureGuards:
    def test_current_temperature_none_when_missing(self):
        assert _make_climate({}).current_temperature is None

    def test_current_temperature_none_when_nan(self):
        assert _make_climate({"circuit2thermostattemp": float("nan")}).current_temperature is None

    def test_target_temperature_defaults_to_20(self):
        assert _make_climate({}).target_temperature == 20.0


@pytest.mark.asyncio
async def test_detection_targets_include_circuit_active_slugs():
    """The coordinator's initial scan must probe circuitNactive."""
    coord = object.__new__(PlumDataUpdateCoordinator)
    coord.available_slugs = []
    coord._cache = {}
    coord._timestamps = {}
    coord._cache_lock = asyncio.Lock()

    captured: dict = {}

    async def fake_get_values(candidates, retries=5):
        captured["candidates"] = list(candidates)
        return dict.fromkeys(candidates, 1)

    coord.device = MagicMock()
    coord.device.get_values = fake_get_values
    # Every slug the scan asks about "exists" on this fake boiler.
    coord.device.params_map = _AllKeysContainer()

    await coord._detect_available_parameters()

    for i in range(1, 8):
        assert f"circuit{i}active" in captured["candidates"]


class _AllKeysContainer:
    """dict-like where every `slug in params_map` is True (the scan filters
    candidates by membership; here we want all of them through)."""

    def __contains__(self, _key):
        return True


class TestHvacAction:
    def test_heating_while_the_mixing_valve_is_open(self):
        entity = _make_climate({"circuit2active": 1, "mixer2valveposition": 33.3})
        assert entity.hvac_action == HVACAction.HEATING

    def test_idle_when_the_valve_is_closed(self):
        entity = _make_climate({"circuit2active": 1, "mixer2valveposition": 0.0})
        assert entity.hvac_action == HVACAction.IDLE

    def test_off_when_the_circuit_is_off(self):
        entity = _make_climate({"circuit2active": 0, "mixer2valveposition": 50.0})
        assert entity.hvac_action == HVACAction.OFF

    def test_unknown_without_a_valve_position(self):
        assert _make_climate({"circuit2active": 1}).hvac_action is None
        assert (
            _make_climate({"circuit2active": 1, "mixer2valveposition": float("nan")}).hvac_action
            is None
        )
