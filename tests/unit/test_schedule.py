"""Unit tests for schedule.py: bitmask <-> slots encoding and the
plum_ecomax.set_schedule service handler.
"""

from __future__ import annotations

from datetime import time
from unittest.mock import AsyncMock, MagicMock

import pytest
from homeassistant.exceptions import ServiceValidationError

from custom_components.plum_ecomax.const import CONF_ACTIVE_CIRCUITS
from custom_components.plum_ecomax.schedule import (
    _handle_set_schedule,
    am_pm_to_slots,
    slots_to_am_pm,
    time_range_to_slots,
)
from tests.conftest import set_loaded_entries


class TestEncoding:
    def test_am_pm_roundtrips_through_slots(self):
        am, pm = 0b1010_0000_1111_0000_0000_0011, 0b0000_1111
        slots = am_pm_to_slots(am, pm)
        comfort = {i for i, on in enumerate(slots) if on}
        assert slots_to_am_pm(comfort) == (am, pm)

    def test_time_range_maps_to_half_hour_slots(self):
        # 06:00 (slot 12) .. 08:00 (slot 16, exclusive) -> {12,13,14,15}
        assert time_range_to_slots(time(6, 0), time(8, 0)) == {12, 13, 14, 15}

    def test_partial_end_slot_is_included_via_ceil(self):
        # 06:00 .. 06:15 still covers the 06:00-06:30 slot
        assert time_range_to_slots(time(6, 0), time(6, 15)) == {12}

    def test_end_before_start_means_through_midnight(self):
        assert max(time_range_to_slots(time(22, 0), time(0, 0))) == 47

    def test_full_day_block_sets_every_bit(self):
        comfort = time_range_to_slots(time(0, 0), time(0, 0))
        assert slots_to_am_pm(comfort) == (0xFFFFFF, 0xFFFFFF)


class TestSetScheduleService:
    def _hass_with_coordinator(self, params_map, active=("2",)):
        coordinator = MagicMock()
        coordinator.device.params_map = params_map
        coordinator.config_entry.data = {CONF_ACTIVE_CIRCUITS: list(active)}
        coordinator.async_set_value = AsyncMock(return_value=True)
        hass = MagicMock()
        set_loaded_entries(hass, {"e1": coordinator})
        return hass, coordinator

    @pytest.mark.asyncio
    async def test_writes_am_and_pm_for_each_named_day(self):
        params = {f"circuit2{d}{h}": {} for d in ("monday", "tuesday") for h in ("am", "pm")}
        hass, coordinator = self._hass_with_coordinator(params)
        call = MagicMock()
        call.data = {
            "circuit": 2,
            "days": ["monday", "tuesday"],
            "comfort_blocks": [{"from": time(6, 0), "to": time(8, 0)}],
        }

        await _handle_set_schedule(hass, call)

        writes = dict(c.args for c in coordinator.async_set_value.await_args_list)
        assert writes["circuit2mondayam"] == 0b1111_0000_0000_0000
        assert writes["circuit2mondaypm"] == 0
        assert writes["circuit2tuesdayam"] == 0b1111_0000_0000_0000

    @pytest.mark.asyncio
    async def test_dhw_target_when_no_circuit_given(self):
        params = {"hdwmondayam": {}, "hdwmondaypm": {}}
        hass, coordinator = self._hass_with_coordinator(params)
        call = MagicMock()
        call.data = {"days": ["monday"], "comfort_blocks": []}

        await _handle_set_schedule(hass, call)

        writes = dict(c.args for c in coordinator.async_set_value.await_args_list)
        assert writes == {"hdwmondayam": 0, "hdwmondaypm": 0}

    @pytest.mark.asyncio
    async def test_a_circuit_that_is_not_active_is_refused_and_nothing_is_written(self):
        params = {"circuit3mondayam": {}, "circuit3mondaypm": {}}
        hass, coordinator = self._hass_with_coordinator(params, active=("2",))
        call = MagicMock()
        call.data = {"circuit": 3, "days": ["monday"], "comfort_blocks": []}

        with pytest.raises(ServiceValidationError) as err:
            await _handle_set_schedule(hass, call)

        assert err.value.translation_key == "circuit_not_active"
        assert err.value.translation_placeholders == {"circuit": "3"}
        coordinator.async_set_value.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_target_the_boiler_does_not_expose_is_refused(self):
        hass, coordinator = self._hass_with_coordinator({}, active=("2",))  # empty map
        call = MagicMock()
        call.data = {"circuit": 2, "days": ["monday"], "comfort_blocks": []}

        with pytest.raises(ServiceValidationError) as err:
            await _handle_set_schedule(hass, call)

        assert err.value.translation_key == "schedule_not_supported"
        coordinator.async_set_value.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_no_loaded_entry_is_refused(self):
        hass = MagicMock()
        hass.config_entries.async_loaded_entries.return_value = []
        call = MagicMock()
        call.data = {"days": ["monday"], "comfort_blocks": []}

        with pytest.raises(ServiceValidationError) as err:
            await _handle_set_schedule(hass, call)

        assert err.value.translation_key == "no_entry_loaded"

    @pytest.mark.asyncio
    async def test_everyday_expands_to_all_seven_days(self):
        params = {
            f"circuit2{d}{h}": {}
            for d in (
                "monday",
                "tuesday",
                "wednesday",
                "thursday",
                "friday",
                "saturday",
                "sunday",
            )
            for h in ("am", "pm")
        }
        hass, coordinator = self._hass_with_coordinator(params)
        call = MagicMock()
        call.data = {"circuit": 2, "days": ["everyday"], "comfort_blocks": []}

        await _handle_set_schedule(hass, call)

        assert coordinator.async_set_value.await_count == 14
