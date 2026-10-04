"""Unit tests for switch.py.

Covers async_setup_entry filtering (only creates entities for slugs present
in the device map), on/off state derivation from arbitrary on_value/
off_value pairs (not just 1/0 -- e.g. hdwpumpforce uses 512/0), the write
path, and device routing (HDW_SWITCHES -> DHW device, everything else ->
boiler device), plus unique_id scoping by entry_id -- the pattern that was
missing here before device.py existed (IMPROVEMENT_PLAN_ARCHIVE.md section C).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.plum_ecomax.const import DOMAIN, SWITCH_TYPES
from custom_components.plum_ecomax.switch import (
    HDW_SWITCHES,
    PlumEconetSwitch,
    PlumSolarDumpAutoSwitch,
    async_setup_entry,
)


def _param_switch_slugs(added):
    return {e._slug for e in added if isinstance(e, PlumEconetSwitch)}


def _make_coordinator(params_map: dict, data: dict):
    coordinator = MagicMock()
    coordinator.device.params_map = params_map
    coordinator.data = data
    coordinator.async_set_value = AsyncMock(return_value=True)
    return coordinator


@pytest.mark.asyncio
async def test_async_setup_entry_only_creates_entities_present_in_device_map():
    coordinator = _make_coordinator({"hdwpumpforce": {"id": 172}}, {})
    hass = MagicMock()
    entry = MagicMock()
    entry.entry_id = "entry123"
    entry.runtime_data = coordinator

    added = []
    await async_setup_entry(hass, entry, lambda entities: added.extend(entities))

    assert _param_switch_slugs(added) == {"hdwpumpforce"}


@pytest.mark.asyncio
async def test_async_setup_entry_skips_slugs_not_in_device_map():
    coordinator = _make_coordinator({}, {})
    hass = MagicMock()
    entry = MagicMock()
    entry.entry_id = "entry123"
    entry.runtime_data = coordinator

    added = []
    await async_setup_entry(hass, entry, lambda entities: added.extend(entities))

    assert _param_switch_slugs(added) == set()
    # the auto-mode switch is always created (no boiler param behind it)
    assert any(isinstance(e, PlumSolarDumpAutoSwitch) for e in added)


class TestIsOn:
    def test_is_on_true_for_nonstandard_on_value(self):
        # hdwpumpforce's "on" is 512, not 1 -- a naive bool(int(val)) check
        # would also report on for e.g. 2, so this pins the exact-match
        # comparison against on_value.
        name, on_value, off_value = SWITCH_TYPES["hdwpumpforce"]
        coordinator = _make_coordinator({}, {"hdwpumpforce": 512})
        switch = PlumEconetSwitch(
            coordinator, "entry123", "hdwpumpforce", name, on_value, off_value
        )

        assert switch.is_on is True

    def test_is_on_false_for_off_value(self):
        name, on_value, off_value = SWITCH_TYPES["hdwpumpforce"]
        coordinator = _make_coordinator({}, {"hdwpumpforce": 0})
        switch = PlumEconetSwitch(
            coordinator, "entry123", "hdwpumpforce", name, on_value, off_value
        )

        assert switch.is_on is False

    def test_is_on_false_when_value_missing(self):
        name, on_value, off_value = SWITCH_TYPES["hdwpumpforce"]
        coordinator = _make_coordinator({}, {})
        switch = PlumEconetSwitch(
            coordinator, "entry123", "hdwpumpforce", name, on_value, off_value
        )

        assert switch.is_on is False

    def test_is_on_false_for_non_numeric_value(self):
        name, on_value, off_value = SWITCH_TYPES["hdwpumpforce"]
        coordinator = _make_coordinator({}, {"hdwpumpforce": "not-a-number"})
        switch = PlumEconetSwitch(
            coordinator, "entry123", "hdwpumpforce", name, on_value, off_value
        )

        assert switch.is_on is False


class TestWritePath:
    @pytest.mark.asyncio
    async def test_turn_on_writes_on_value(self):
        # hdwstartoneloading is a plain parameter switch (hdwpumpforce is not
        # anymore -- see TestManualModeBackedSwitch).
        name, on_value, off_value = SWITCH_TYPES["hdwstartoneloading"]
        coordinator = _make_coordinator({}, {})
        switch = PlumEconetSwitch(
            coordinator, "entry123", "hdwstartoneloading", name, on_value, off_value
        )

        await switch.async_turn_on()

        coordinator.async_set_value.assert_awaited_once_with("hdwstartoneloading", 1)

    @pytest.mark.asyncio
    async def test_turn_off_writes_off_value(self):
        name, on_value, off_value = SWITCH_TYPES["hdwstartoneloading"]
        coordinator = _make_coordinator({}, {})
        switch = PlumEconetSwitch(
            coordinator, "entry123", "hdwstartoneloading", name, on_value, off_value
        )

        await switch.async_turn_off()

        coordinator.async_set_value.assert_awaited_once_with("hdwstartoneloading", 0)


class TestManualModeBackedSwitch:
    """The hdwpumpforce switch doesn't just write the parameter -- it routes
    through solar_dump so it enters/leaves manual mode (the boiler ignores
    the force otherwise). See IMPROVEMENT_PLAN_ARCHIVE.md section N."""

    @pytest.mark.asyncio
    async def test_turn_on_calls_start_hold(self):
        name, on_value, off_value = SWITCH_TYPES["hdwpumpforce"]
        coordinator = _make_coordinator({}, {})
        coordinator.hass = MagicMock()
        switch = PlumEconetSwitch(coordinator, "entryX", "hdwpumpforce", name, on_value, off_value)

        with patch("custom_components.plum_ecomax.switch.async_start_hold") as start:
            await switch.async_turn_on()

        start.assert_awaited_once_with(coordinator.hass, coordinator, "entryX")
        coordinator.async_set_value.assert_not_called()

    @pytest.mark.asyncio
    async def test_turn_off_calls_stop_for_entry(self):
        name, on_value, off_value = SWITCH_TYPES["hdwpumpforce"]
        coordinator = _make_coordinator({}, {})
        coordinator.hass = MagicMock()
        switch = PlumEconetSwitch(coordinator, "entryX", "hdwpumpforce", name, on_value, off_value)

        with patch("custom_components.plum_ecomax.switch.async_stop_for_entry") as stop:
            await switch.async_turn_off()

        stop.assert_awaited_once_with(coordinator.hass, "entryX")
        coordinator.async_set_value.assert_not_called()


class TestDeviceRouting:
    def test_hdw_switches_route_to_dhw_device(self):
        coordinator = _make_coordinator({}, {})
        for slug in HDW_SWITCHES:
            name, on_value, off_value = SWITCH_TYPES[slug]
            switch = PlumEconetSwitch(coordinator, "entry123", slug, name, on_value, off_value)
            info = switch.device_info
            assert (DOMAIN, "entry123_hdw") in info["identifiers"]

    def test_non_hdw_switch_routes_to_boiler_device(self):
        coordinator = _make_coordinator({}, {"uid": "SN123"})
        switch = PlumEconetSwitch(coordinator, "entry123", "some_other_slug", "Other", 1, 0)

        info = switch.device_info

        assert (DOMAIN, "entry123") in info["identifiers"]
        assert info["serial_number"] == "SN123"


class TestOperatingModeSwitch:
    """pid 161 exposed as a switch: on=2 (manual), off=1 (automatic),
    Configuration category (IMPROVEMENT_PLAN_ARCHIVE.md section N)."""

    def _switch(self, data, entry="entry123"):
        from custom_components.plum_ecomax.const import OPERATING_MODE_AUTO, OPERATING_MODE_MANUAL

        assert SWITCH_TYPES["operatingmode"] == (
            "Manual mode",
            OPERATING_MODE_MANUAL,
            OPERATING_MODE_AUTO,
        )
        name, on_value, off_value = SWITCH_TYPES["operatingmode"]
        return PlumEconetSwitch(
            _make_coordinator({}, data), entry, "operatingmode", name, on_value, off_value
        )

    def test_is_on_when_manual(self):
        assert self._switch({"operatingmode": 2}).is_on is True

    def test_is_off_when_automatic(self):
        assert self._switch({"operatingmode": 1}).is_on is False

    def test_entity_category_is_config(self):
        from homeassistant.const import EntityCategory

        assert self._switch({}).entity_category == EntityCategory.CONFIG

    def test_hdwpumpforce_has_no_entity_category(self):
        name, on_value, off_value = SWITCH_TYPES["hdwpumpforce"]
        switch = PlumEconetSwitch(
            _make_coordinator({}, {}), "e", "hdwpumpforce", name, on_value, off_value
        )
        assert switch.entity_category is None

    @pytest.mark.asyncio
    async def test_turn_on_writes_manual(self):
        switch = self._switch({})
        await switch.async_turn_on()
        switch.coordinator.async_set_value.assert_awaited_once_with("operatingmode", 2)

    @pytest.mark.asyncio
    async def test_turn_off_writes_automatic(self):
        switch = self._switch({})
        await switch.async_turn_off()
        switch.coordinator.async_set_value.assert_awaited_once_with("operatingmode", 1)

    def test_routes_to_boiler_device(self):
        info = self._switch({"uid": "SN"}).device_info
        assert (DOMAIN, "entry123") in info["identifiers"]


def test_unique_id_is_scoped_by_entry_id():
    coordinator = _make_coordinator({}, {})
    name, on_value, off_value = SWITCH_TYPES["hdwpumpforce"]
    switch_a = PlumEconetSwitch(coordinator, "entryA", "hdwpumpforce", name, on_value, off_value)
    switch_b = PlumEconetSwitch(coordinator, "entryB", "hdwpumpforce", name, on_value, off_value)

    assert switch_a.unique_id != switch_b.unique_id
    assert "entryA" in switch_a.unique_id
    assert "entryB" in switch_b.unique_id
