"""water_heater.py: readings with bad values, limits, operation modes, writes and setup."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from homeassistant.components.water_heater import STATE_ECO, STATE_OFF, STATE_PERFORMANCE
from homeassistant.const import ATTR_TEMPERATURE

from custom_components.plum_ecomax import water_heater as wh
from custom_components.plum_ecomax.const import WATER_HEATER_TYPES
from custom_components.plum_ecomax.water_heater import PlumEcomaxWaterHeater


@pytest.fixture
def entity():
    coordinator = MagicMock()
    coordinator.data = {}
    coordinator.async_set_value = AsyncMock(return_value=True)
    return PlumEcomaxWaterHeater(coordinator, "entry1", "hdw", "cur", "tgt", "tmin", "tmax", "mode")


class TestReadings:
    @pytest.mark.parametrize(
        "raw,expected", [(None, None), ("n/a", None), (float("nan"), None), (44, 44.0)]
    )
    def test_current_temperature(self, entity, raw, expected):
        entity.coordinator.data["cur"] = raw
        assert entity.current_temperature == expected

    @pytest.mark.parametrize("raw,expected", [(None, None), ("n/a", None), (50, 50.0)])
    def test_target_temperature(self, entity, raw, expected):
        entity.coordinator.data["tgt"] = raw
        assert entity.target_temperature == expected

    @pytest.mark.parametrize(
        "raw,expected", [(None, 20.0), ("x", 20.0), (float("nan"), 20.0), (35, 35.0)]
    )
    def test_min_temp_falls_back_to_20(self, entity, raw, expected):
        entity.coordinator.data["tmin"] = raw
        assert entity.min_temp == expected

    @pytest.mark.parametrize(
        "raw,expected", [(None, 60.0), ("x", 60.0), (float("nan"), 60.0), (65, 65.0)]
    )
    def test_max_temp_falls_back_to_60(self, entity, raw, expected):
        entity.coordinator.data["tmax"] = raw
        assert entity.max_temp == expected

    @pytest.mark.parametrize(
        "raw,expected",
        [
            (None, STATE_OFF),
            (0, STATE_OFF),
            (1, STATE_PERFORMANCE),
            (2, STATE_ECO),
            (99, STATE_OFF),
        ],
    )
    def test_current_operation(self, entity, raw, expected):
        entity.coordinator.data["mode"] = raw
        assert entity.current_operation == expected


class TestWrites:
    async def test_set_temperature_writes_an_integer_setpoint(self, entity):
        await entity.async_set_temperature(**{ATTR_TEMPERATURE: 52.7})
        entity.coordinator.async_set_value.assert_awaited_once_with("tgt", 52)

    async def test_set_temperature_without_a_value_writes_nothing(self, entity):
        await entity.async_set_temperature()
        entity.coordinator.async_set_value.assert_not_awaited()

    @pytest.mark.parametrize("mode,raw", [(STATE_OFF, 0), (STATE_PERFORMANCE, 1), (STATE_ECO, 2)])
    async def test_set_operation_mode_maps_to_the_boiler_code(self, entity, mode, raw):
        await entity.async_set_operation_mode(mode)
        entity.coordinator.async_set_value.assert_awaited_once_with("mode", raw)

    async def test_an_unknown_mode_is_logged_and_not_written(self, entity, caplog):
        await entity.async_set_operation_mode("turbo")
        entity.coordinator.async_set_value.assert_not_awaited()
        assert "Unknown or unsupported DHW mode" in caplog.text


class TestSetup:
    @staticmethod
    def _entry(present: set[str]):
        entry = MagicMock()
        entry.entry_id = "e1"
        coordinator = MagicMock()
        coordinator.device.params_map = {slug: {} for slug in present}
        coordinator.data = {}
        entry.runtime_data = coordinator
        return entry

    async def test_the_entity_is_created_when_the_parameters_exist(self):
        cur, tgt, *_ = WATER_HEATER_TYPES["hdw"]
        add = MagicMock()

        await wh.async_setup_entry(MagicMock(), self._entry({cur, tgt}), add)

        (entities,) = add.call_args.args
        assert len(entities) == 1 and isinstance(entities[0], PlumEcomaxWaterHeater)

    async def test_nothing_is_created_and_an_error_is_logged_when_they_are_missing(self, caplog):
        add = MagicMock()

        await wh.async_setup_entry(MagicMock(), self._entry(set()), add)

        assert add.call_args.args == ([],)
        assert "Failed to create Water Heater" in caplog.text
