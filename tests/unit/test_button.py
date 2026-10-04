"""Unit tests for button.py's save/restore-defaults buttons.

Covers the feature requested by the user: a way to snapshot the current
value of every CONFIG-category number entity (heating curves, base temp,
night setback, anti-legionella, forced buffer loading) and restore them
later if a value gets changed by mistake.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.plum_ecomax.button import (
    PlumRestoreDefaultsButton,
    PlumSaveDefaultsButton,
    async_setup_entry,
)
from custom_components.plum_ecomax.const import CONF_ACTIVE_CIRCUITS, DOMAIN


def _make_coordinator(params_map: dict, data: dict):
    coordinator = MagicMock()
    coordinator.device.params_map = params_map
    coordinator.data = data
    coordinator.async_set_value = AsyncMock(return_value=True)
    return coordinator


def _make_entry(active_circuits: list[str]):
    entry = MagicMock()
    entry.entry_id = "entry123"
    entry.data = {CONF_ACTIVE_CIRCUITS: active_circuits}
    return entry


PARAMS_MAP = {
    "circuit2curvefloor": {"id": 1},
    "circuit2basetemp": {"id": 2},
    "circuit3curvefloor": {"id": 3},  # inactive circuit, excluded
    "hdwtsetpoint": {"id": 4},  # not CONFIG category, excluded
    "buforlongloadtime": {"id": 5},
}

DATA = {
    "circuit2curvefloor": 1.3,
    "circuit2basetemp": 22,
    "circuit3curvefloor": 1.1,
    "hdwtsetpoint": 48,
    "buforlongloadtime": 60,
}


@pytest.mark.asyncio
async def test_async_setup_entry_adds_both_buttons():
    coordinator = _make_coordinator(PARAMS_MAP, DATA)
    entry = _make_entry(["2"])
    hass = MagicMock()
    entry.runtime_data = coordinator

    added = []
    await async_setup_entry(hass, entry, lambda entities: added.extend(entities))

    assert len(added) == 2
    assert isinstance(added[0], PlumSaveDefaultsButton)
    assert isinstance(added[1], PlumRestoreDefaultsButton)


def test_eligible_slugs_excludes_inactive_circuit_and_non_config():
    coordinator = _make_coordinator(PARAMS_MAP, DATA)
    entry = _make_entry(["2"])
    button = PlumSaveDefaultsButton(coordinator, entry)

    eligible = set(button._eligible_slugs())

    assert eligible == {"circuit2curvefloor", "circuit2basetemp", "buforlongloadtime"}
    assert "circuit3curvefloor" not in eligible  # inactive circuit
    assert "hdwtsetpoint" not in eligible  # not CONFIG category


@pytest.mark.asyncio
async def test_save_button_stores_only_eligible_values():
    coordinator = _make_coordinator(PARAMS_MAP, DATA)
    entry = _make_entry(["2"])
    button = PlumSaveDefaultsButton(coordinator, entry)
    button.hass = MagicMock()

    fake_store = MagicMock()
    fake_store.async_save = AsyncMock()
    with patch("custom_components.plum_ecomax.button.Store", return_value=fake_store):
        await button.async_press()

    fake_store.async_save.assert_awaited_once()
    saved = fake_store.async_save.await_args[0][0]
    assert saved == {
        "circuit2curvefloor": 1.3,
        "circuit2basetemp": 22,
        "buforlongloadtime": 60,
    }


@pytest.mark.asyncio
async def test_restore_button_writes_back_saved_values():
    coordinator = _make_coordinator(PARAMS_MAP, DATA)
    entry = _make_entry(["2"])
    button = PlumRestoreDefaultsButton(coordinator, entry)
    button.hass = MagicMock()

    snapshot = {"circuit2curvefloor": 1.0, "circuit2basetemp": 20, "buforlongloadtime": 45}
    fake_store = MagicMock()
    fake_store.async_load = AsyncMock(return_value=snapshot)
    with patch("custom_components.plum_ecomax.button.Store", return_value=fake_store):
        await button.async_press()

    assert coordinator.async_set_value.await_count == 3
    written = {call.args[0]: call.args[1] for call in coordinator.async_set_value.await_args_list}
    assert written == snapshot


@pytest.mark.asyncio
async def test_restore_button_skips_slugs_no_longer_eligible():
    """A slug saved while its circuit was active, but the circuit was since
    deselected, must not be written back."""
    coordinator = _make_coordinator(PARAMS_MAP, DATA)
    entry = _make_entry(["2"])
    button = PlumRestoreDefaultsButton(coordinator, entry)
    button.hass = MagicMock()

    snapshot = {
        "circuit2curvefloor": 1.0,
        "circuit3curvefloor": 1.5,  # no longer an active circuit
    }
    fake_store = MagicMock()
    fake_store.async_load = AsyncMock(return_value=snapshot)
    with patch("custom_components.plum_ecomax.button.Store", return_value=fake_store):
        await button.async_press()

    coordinator.async_set_value.assert_awaited_once_with("circuit2curvefloor", 1.0)


@pytest.mark.asyncio
async def test_restore_button_does_nothing_when_no_snapshot_saved():
    coordinator = _make_coordinator(PARAMS_MAP, DATA)
    entry = _make_entry(["2"])
    button = PlumRestoreDefaultsButton(coordinator, entry)
    button.hass = MagicMock()

    fake_store = MagicMock()
    fake_store.async_load = AsyncMock(return_value=None)
    with patch("custom_components.plum_ecomax.button.Store", return_value=fake_store):
        await button.async_press()

    coordinator.async_set_value.assert_not_awaited()


def test_device_info_uses_boiler_device_scoped_by_entry():
    coordinator = _make_coordinator(PARAMS_MAP, DATA)
    entry = _make_entry(["2"])
    button = PlumSaveDefaultsButton(coordinator, entry)

    info = button.device_info

    assert (DOMAIN, "entry123") in info["identifiers"]


def test_unique_ids_are_distinct_and_entry_scoped():
    coordinator = _make_coordinator(PARAMS_MAP, DATA)
    entry = _make_entry(["2"])
    save_button = PlumSaveDefaultsButton(coordinator, entry)
    restore_button = PlumRestoreDefaultsButton(coordinator, entry)

    assert save_button.unique_id != restore_button.unique_id
    assert "entry123" in save_button.unique_id
    assert "entry123" in restore_button.unique_id
