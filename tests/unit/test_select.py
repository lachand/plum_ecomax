"""Unit tests for select.py.

Covers async_setup_entry filtering (only creates entities for slugs present
in the device map), option mapping in both directions (raw int <-> HA
option string), the write path, and unique_id scoping by entry_id.
"""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.plum_ecomax.const import DOMAIN, SELECT_TYPES
from custom_components.plum_ecomax.select import PlumEconetSelect, async_setup_entry


def _make_coordinator(params_map: dict, data: dict):
    coordinator = MagicMock()
    coordinator.device.params_map = params_map
    coordinator.data = data
    coordinator.async_set_value = AsyncMock(return_value=True)
    return coordinator


@pytest.mark.asyncio
async def test_async_setup_entry_only_creates_entities_present_in_device_map():
    coordinator = _make_coordinator({"hdwusermode": {"id": 10}}, {})
    hass = MagicMock()
    entry = MagicMock()
    entry.entry_id = "entry123"
    entry.runtime_data = coordinator

    added = []
    await async_setup_entry(hass, entry, lambda entities: added.extend(entities))

    slugs = {e._slug for e in added}
    assert slugs == {"hdwusermode"}


@pytest.mark.asyncio
async def test_async_setup_entry_skips_slugs_not_in_device_map():
    coordinator = _make_coordinator({}, {})
    hass = MagicMock()
    entry = MagicMock()
    entry.entry_id = "entry123"
    entry.runtime_data = coordinator

    added = []
    await async_setup_entry(hass, entry, lambda entities: added.extend(entities))

    assert added == []


def _make_select(coordinator, entry_id="entry123", slug="hdwusermode"):
    name, map_to_ha, map_to_plum = SELECT_TYPES[slug]
    return PlumEconetSelect(coordinator, entry_id, slug, name, map_to_ha, map_to_plum)


class TestOptions:
    def test_attr_options_lists_all_ha_option_strings(self):
        coordinator = _make_coordinator({}, {})
        select = _make_select(coordinator)

        assert set(select._attr_options) == {"off", "manual", "auto"}

    def test_current_option_maps_raw_value_to_ha_string(self):
        coordinator = _make_coordinator({}, {"hdwusermode": 2})
        select = _make_select(coordinator)

        assert select.current_option == "auto"

    def test_current_option_none_when_value_missing(self):
        coordinator = _make_coordinator({}, {})
        select = _make_select(coordinator)

        assert select.current_option is None

    def test_current_option_none_for_unmapped_raw_value(self):
        coordinator = _make_coordinator({}, {"hdwusermode": 99})
        select = _make_select(coordinator)

        assert select.current_option is None

    def test_current_option_none_for_non_numeric_value(self):
        coordinator = _make_coordinator({}, {"hdwusermode": "garbage"})
        select = _make_select(coordinator)

        assert select.current_option is None


class TestWritePath:
    @pytest.mark.asyncio
    async def test_select_option_writes_mapped_raw_value(self):
        coordinator = _make_coordinator({}, {})
        select = _make_select(coordinator)

        await select.async_select_option("auto")

        coordinator.async_set_value.assert_awaited_once_with("hdwusermode", 2)

    @pytest.mark.asyncio
    async def test_select_invalid_option_does_not_write(self, caplog):
        coordinator = _make_coordinator({}, {})
        select = _make_select(coordinator)

        with caplog.at_level(logging.ERROR):
            await select.async_select_option("not-a-real-option")

        coordinator.async_set_value.assert_not_awaited()
        assert "Invalid option" in caplog.text


def test_device_info_uses_hdw_device_scoped_by_entry():
    coordinator = _make_coordinator({}, {})
    select = _make_select(coordinator, entry_id="entry123")

    info = select.device_info

    assert (DOMAIN, "entry123_hdw") in info["identifiers"]


def test_unique_id_is_scoped_by_entry_id():
    coordinator = _make_coordinator({}, {})
    select_a = _make_select(coordinator, entry_id="entryA")
    select_b = _make_select(coordinator, entry_id="entryB")

    assert select_a.unique_id != select_b.unique_id
    assert "entryA" in select_a.unique_id
    assert "entryB" in select_b.unique_id
