"""Unit test for number.py's async_setup_entry circuit filtering.

Regression for a real bug reported by the user: number.py was the only
platform (sensor.py/climate.py/calendar.py already did this) that ignored
CONF_ACTIVE_CIRCUITS entirely, creating heating-curve entities for every
circuit 1-7 in the device map regardless of which ones are actually wired
up in the user's installation (they only use circuit 2).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from homeassistant.const import EntityCategory

from custom_components.plum_ecomax.const import (
    CONF_ACTIVE_CIRCUITS,
    DOMAIN,
    SOLAR_DUMP_NUMBERS,
    SOLAR_DUMP_START_TEMP_DEFAULT,
    SOLAR_DUMP_STOP_TEMP_DEFAULT,
)
from custom_components.plum_ecomax.number import (
    PlumEcomaxNumber,
    PlumSolarDumpNumber,
    async_setup_entry,
)


def _make_hass_and_entry(params_map: dict, active_circuits: list[str]):
    coordinator = MagicMock()
    coordinator.device.params_map = params_map
    coordinator.data = {}

    hass = MagicMock()
    entry = MagicMock()
    entry.entry_id = "entry123"
    entry.data = {CONF_ACTIVE_CIRCUITS: active_circuits}
    entry.runtime_data = coordinator
    return hass, entry


@pytest.mark.asyncio
async def test_only_active_circuit_numbers_are_created():
    params_map = {
        "circuit1curvefloor": {"id": 1},
        "circuit1basetemp": {"id": 2},
        "circuit2curvefloor": {"id": 3},
        "circuit2basetemp": {"id": 4},
        "circuit3curvefloor": {"id": 5},
        "buforlongloadtime": {"id": 6},  # not circuit-prefixed, always eligible
    }
    hass, entry = _make_hass_and_entry(params_map, active_circuits=["2"])

    added = []
    await async_setup_entry(hass, entry, lambda entities: added.extend(entities))

    slugs = {e._slug for e in added if isinstance(e, PlumEcomaxNumber)}
    assert slugs == {"circuit2curvefloor", "circuit2basetemp", "buforlongloadtime"}
    assert "circuit1curvefloor" not in slugs
    assert "circuit3curvefloor" not in slugs


@pytest.mark.asyncio
async def test_no_active_circuits_creates_no_circuit_entities():
    params_map = {
        "circuit2curvefloor": {"id": 3},
        "buforlongloadtime": {"id": 6},
    }
    hass, entry = _make_hass_and_entry(params_map, active_circuits=[])

    added = []
    await async_setup_entry(hass, entry, lambda entities: added.extend(entities))

    slugs = {e._slug for e in added if isinstance(e, PlumEcomaxNumber)}
    assert slugs == {"buforlongloadtime"}


@pytest.mark.asyncio
async def test_async_set_native_value_does_not_truncate_floats():
    """Regression: async_set_native_value used to int()-cast the value,
    silently rounding FLOAT parameters (heating curves, step 0.1) so setting
    a curve to 1.4 wrote 1.0 to the boiler. The value must reach the
    coordinator unchanged -- PlumDevice._encode casts per wire type.
    """
    coordinator = MagicMock()
    coordinator.async_set_value = AsyncMock(return_value=True)
    entry = MagicMock()
    entry.entry_id = "e1"

    number = PlumEcomaxNumber(
        coordinator, entry, "circuit2curvefloor", (0.1, 4.0, 0.1, "mdi:chart-bell-curve")
    )
    await number.async_set_native_value(1.4)

    coordinator.async_set_value.assert_awaited_once_with("circuit2curvefloor", 1.4)


def _number(slug):
    coordinator = MagicMock()
    coordinator.data = {}
    entry = MagicMock()
    entry.entry_id = "e1"
    return PlumEcomaxNumber(coordinator, entry, slug, (0, 60, 1, "mdi:timer"))


class TestNewNumberRouting:
    def test_circulation_slugs_route_to_dhw_device_as_config(self):
        n = _number("circulationtimework")
        assert n.entity_category == EntityCategory.CONFIG
        assert n.device_info["identifiers"] == {(DOMAIN, "e1_hdw")}

    def test_cooling_setpoints_route_to_their_circuit_device(self):
        n = _number("circuit2maxsetpointcooling")
        assert n.entity_category == EntityCategory.CONFIG
        assert n.device_info["identifiers"] == {(DOMAIN, "e1_circuit_2")}

    @pytest.mark.asyncio
    async def test_cooling_setpoints_are_gated_by_active_circuits(self):
        params_map = {
            "circuit2maxsetpointcooling": {"id": 1},
            "circuit3maxsetpointcooling": {"id": 2},
        }
        hass, entry = _make_hass_and_entry(params_map, active_circuits=["2"])
        added = []
        await async_setup_entry(hass, entry, lambda e: added.extend(e))
        slugs = {e._slug for e in added if isinstance(e, PlumEcomaxNumber)}
        assert slugs == {"circuit2maxsetpointcooling"}


class TestSolarDumpNumbers:
    """The HA-local solar-dump settings (guard rails + auto-mode knobs),
    all built from const.SOLAR_DUMP_NUMBERS."""

    def _mk(self, key):
        coordinator = MagicMock()
        coordinator.data = {}
        for _d, _mn, _mx, _s, _u, _i, attr in SOLAR_DUMP_NUMBERS.values():
            setattr(coordinator, attr, None)
        entry = MagicMock()
        entry.entry_id = "e1"
        return coordinator, PlumSolarDumpNumber(coordinator, entry, key)

    @pytest.mark.asyncio
    async def test_setup_creates_every_number(self):
        hass, entry = _make_hass_and_entry({}, active_circuits=[])
        added = []
        await async_setup_entry(hass, entry, lambda e: added.extend(e))
        keys = {e._key for e in added if isinstance(e, PlumSolarDumpNumber)}
        assert keys == set(SOLAR_DUMP_NUMBERS)

    def test_defaults_config_category_unique_ids(self):
        _c1, start = self._mk("start_temp")
        _c2, stop = self._mk("stop_temp")
        _c3, budget = self._mk("daily_budget")
        assert start.native_value == SOLAR_DUMP_START_TEMP_DEFAULT
        assert stop.native_value == SOLAR_DUMP_STOP_TEMP_DEFAULT
        assert start.entity_category == EntityCategory.CONFIG
        assert len({start.unique_id, stop.unique_id, budget.unique_id}) == 3
        # unchanged from the two-threshold version -> no entity-registry churn
        assert start.unique_id.endswith("_number_solar_dump_start_temp")

    @pytest.mark.asyncio
    async def test_set_value_pushes_to_coordinator(self):
        coordinator, entity = self._mk("buffer_target")
        entity.async_write_ha_state = MagicMock()
        await entity.async_set_native_value(58)
        assert entity.native_value == 58.0
        assert coordinator.solar_dump_buffer_target == 58.0

    @pytest.mark.asyncio
    async def test_restore_pushes_last_value_to_coordinator(self):
        coordinator, entity = self._mk("dt_start")
        last = MagicMock()
        last.native_value = 11.0
        entity.async_get_last_number_data = AsyncMock(return_value=last)
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                "homeassistant.helpers.restore_state.RestoreEntity.async_added_to_hass",
                AsyncMock(),
            )
            await entity.async_added_to_hass()
        assert entity.native_value == 11.0
        assert coordinator.solar_dump_dt_start == 11.0
