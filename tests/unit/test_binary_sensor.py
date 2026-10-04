"""Unit tests for binary_sensor.py.

Covers:
* async_setup_entry filtering (manual-mode + alarm entities only created
  for slugs present in the device map).
* PlumManualModeBinarySensor -- bitmask extraction of MANUAL_MODE_BIT from
  heatsourcemainpumpstate (see IMPROVEMENT_PLAN_ARCHIVE.md section H for how this
  bit was empirically confirmed against the real boiler).
* PlumAlarmBinarySensor -- coarse "some bit is set" reading of the alarm
  registers, plus the repair-issue mirroring (create while on, delete while
  off/removed) so an active alarm surfaces in Settings -> Repairs too.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from custom_components.plum_ecomax.binary_sensor import (
    PlumAlarmBinarySensor,
    PlumManualModeBinarySensor,
    async_setup_entry,
)
from custom_components.plum_ecomax.const import (
    ALARM_BITMASK_SLUGS,
    DOMAIN,
    MANUAL_MODE_SLUG,
)


def _make_coordinator(params_map: dict, data: dict):
    coordinator = MagicMock()
    coordinator.device.params_map = params_map
    coordinator.data = data
    return coordinator


class TestAsyncSetupEntry:
    @pytest.mark.asyncio
    async def test_creates_manual_mode_and_alarm_entities_present_in_device_map(self):
        params_map = {MANUAL_MODE_SLUG: {"id": 463}, "alarmbits_1": {"id": 1042}}
        coordinator = _make_coordinator(params_map, {})
        hass = MagicMock()
        entry = MagicMock()
        entry.entry_id = "entry123"
        entry.runtime_data = coordinator

        added = []
        await async_setup_entry(hass, entry, lambda entities: added.extend(entities))

        manual = [e for e in added if isinstance(e, PlumManualModeBinarySensor)]
        alarms = [e for e in added if isinstance(e, PlumAlarmBinarySensor)]
        assert len(manual) == 1
        assert {e._slug for e in alarms} == {"alarmbits_1"}

    @pytest.mark.asyncio
    async def test_skips_slugs_not_in_device_map(self):
        coordinator = _make_coordinator({}, {})
        hass = MagicMock()
        entry = MagicMock()
        entry.entry_id = "entry123"
        entry.runtime_data = coordinator

        added = []
        await async_setup_entry(hass, entry, lambda entities: added.extend(entities))

        assert added == []

    @pytest.mark.asyncio
    async def test_creates_one_alarm_entity_per_alarm_slug(self):
        params_map = {slug: {"id": i} for i, slug in enumerate(ALARM_BITMASK_SLUGS)}
        coordinator = _make_coordinator(params_map, {})
        hass = MagicMock()
        entry = MagicMock()
        entry.entry_id = "entry123"
        entry.runtime_data = coordinator

        added = []
        await async_setup_entry(hass, entry, lambda entities: added.extend(entities))

        alarm_slugs = {e._slug for e in added if isinstance(e, PlumAlarmBinarySensor)}
        assert alarm_slugs == set(ALARM_BITMASK_SLUGS)


class TestManualModeBinarySensor:
    def test_is_on_true_when_bit_set(self):
        # 64 = bit 6 alone; 64 | 1 keeps bit 6 set alongside unrelated bits,
        # proving this checks the bit, not exact equality to 64.
        coordinator = _make_coordinator({}, {MANUAL_MODE_SLUG: 64 | 1})
        sensor = PlumManualModeBinarySensor(coordinator, "entry123")

        assert sensor.is_on is True

    def test_is_on_false_when_bit_clear(self):
        coordinator = _make_coordinator({}, {MANUAL_MODE_SLUG: 0})
        sensor = PlumManualModeBinarySensor(coordinator, "entry123")

        assert sensor.is_on is False

    def test_is_on_none_when_value_missing(self):
        coordinator = _make_coordinator({}, {})
        sensor = PlumManualModeBinarySensor(coordinator, "entry123")

        assert sensor.is_on is None

    def test_is_on_none_for_non_numeric_value(self):
        coordinator = _make_coordinator({}, {MANUAL_MODE_SLUG: "garbage"})
        sensor = PlumManualModeBinarySensor(coordinator, "entry123")

        assert sensor.is_on is None

    def test_device_info_uses_boiler_device_scoped_by_entry(self):
        coordinator = _make_coordinator({}, {"uid": "SN123"})
        sensor = PlumManualModeBinarySensor(coordinator, "entry123")

        info = sensor.device_info

        assert (DOMAIN, "entry123") in info["identifiers"]
        assert info["serial_number"] == "SN123"

    def test_unique_id_is_scoped_by_entry_id(self):
        coordinator = _make_coordinator({}, {})
        sensor_a = PlumManualModeBinarySensor(coordinator, "entryA")
        sensor_b = PlumManualModeBinarySensor(coordinator, "entryB")

        assert sensor_a.unique_id != sensor_b.unique_id
        assert "entryA" in sensor_a.unique_id
        assert "entryB" in sensor_b.unique_id


class TestAlarmBinarySensor:
    def test_is_on_true_for_nonzero_value(self):
        coordinator = _make_coordinator({}, {"alarmbits_1": 8})
        sensor = PlumAlarmBinarySensor(coordinator, "entry123", "alarmbits_1")

        assert sensor.is_on is True

    def test_is_on_false_for_zero_value(self):
        coordinator = _make_coordinator({}, {"alarmbits_1": 0})
        sensor = PlumAlarmBinarySensor(coordinator, "entry123", "alarmbits_1")

        assert sensor.is_on is False

    def test_is_on_none_when_value_missing(self):
        coordinator = _make_coordinator({}, {})
        sensor = PlumAlarmBinarySensor(coordinator, "entry123", "alarmbits_1")

        assert sensor.is_on is None

    def test_unique_id_includes_slug_and_entry(self):
        coordinator = _make_coordinator({}, {})
        sensor = PlumAlarmBinarySensor(coordinator, "entry123", "alarmbits_2")

        assert "entry123" in sensor.unique_id
        assert "alarmbits_2" in sensor.unique_id

    def test_handle_coordinator_update_creates_issue_while_alarm_active(self):
        coordinator = _make_coordinator({}, {"alarmbits_1": 8})
        sensor = PlumAlarmBinarySensor(coordinator, "entry123", "alarmbits_1")
        sensor.hass = MagicMock()
        sensor.async_write_ha_state = MagicMock()

        with (
            patch("custom_components.plum_ecomax.binary_sensor.raise_issue") as mock_raise,
            patch("custom_components.plum_ecomax.binary_sensor.clear_issue") as mock_clear,
        ):
            sensor._handle_coordinator_update()

        mock_raise.assert_called_once()
        args, kwargs = mock_raise.call_args
        assert args[0] is sensor.hass
        assert args[1] == sensor._issue_id
        assert args[2] == "alarm_active"
        assert kwargs["translation_placeholders"] == {"slug": "alarmbits_1"}
        mock_clear.assert_not_called()
        sensor.async_write_ha_state.assert_called_once()

    def test_handle_coordinator_update_deletes_issue_once_alarm_clears(self):
        coordinator = _make_coordinator({}, {"alarmbits_1": 0})
        sensor = PlumAlarmBinarySensor(coordinator, "entry123", "alarmbits_1")
        sensor.hass = MagicMock()
        sensor.async_write_ha_state = MagicMock()

        with (
            patch("custom_components.plum_ecomax.binary_sensor.raise_issue") as mock_raise,
            patch("custom_components.plum_ecomax.binary_sensor.clear_issue") as mock_clear,
        ):
            sensor._handle_coordinator_update()

        mock_clear.assert_called_once_with(sensor.hass, sensor._issue_id)
        mock_raise.assert_not_called()

    @pytest.mark.asyncio
    async def test_will_remove_from_hass_clears_open_issue(self):
        coordinator = _make_coordinator({}, {"alarmbits_1": 8})
        sensor = PlumAlarmBinarySensor(coordinator, "entry123", "alarmbits_1")
        sensor.hass = MagicMock()

        with patch("custom_components.plum_ecomax.binary_sensor.clear_issue") as mock_clear:
            await sensor.async_will_remove_from_hass()

        mock_clear.assert_called_once_with(sensor.hass, sensor._issue_id)
