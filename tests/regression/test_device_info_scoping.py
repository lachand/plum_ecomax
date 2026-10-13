"""Regression test: every platform's device_info must be scoped by the
config entry ID.

Before device.py existed, switch.py/calendar.py/water_heater.py hardcoded
the DHW device as `(DOMAIN, "plum_hdw")` -- a fixed string shared by
*every* config entry. A second boiler (second config entry) would collide
with the first on that device, silently merging their DHW entities in the
device registry. This instantiates each entity twice, under two different
config entries, and checks that every device identifier actually differs
between them -- the only way to prove nothing is hardcoded.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from custom_components.plum_ecomax.calendar import PlumEconetCalendar
from custom_components.plum_ecomax.climate import PlumEcomaxClimate
from custom_components.plum_ecomax.number import PlumEcomaxNumber
from custom_components.plum_ecomax.select import PlumEconetSelect
from custom_components.plum_ecomax.sensor import PlumEcomaxSensor
from custom_components.plum_ecomax.switch import PlumEconetSwitch
from custom_components.plum_ecomax.water_heater import PlumEcomaxWaterHeater

ENTRY_A = "entryA"
ENTRY_B = "entryB"


def _coordinator():
    return MagicMock()


def _identifiers(device_info) -> set:
    """device_info is a dict for some platforms and a DeviceInfo (also
    dict-like) for others -- both expose "identifiers" the same way.
    """
    return set(device_info["identifiers"])


def _assert_scoped_by_entry(build_entity, label: str):
    info_a = build_entity(ENTRY_A).device_info
    info_b = build_entity(ENTRY_B).device_info

    ids_a, ids_b = _identifiers(info_a), _identifiers(info_b)
    assert ids_a, f"{label}: device_info has no identifiers at all"
    assert ids_a.isdisjoint(ids_b), (
        f"{label}: device identifiers are shared between two different "
        f"config entries ({ids_a} vs {ids_b}) -- something is hardcoded "
        f"instead of scoped by entry_id, exactly like the old "
        f"(DOMAIN, 'plum_hdw') bug."
    )
    for _domain, identifier in ids_a:
        assert ENTRY_A in identifier, (
            f"{label}: identifier '{identifier}' doesn't embed the entry_id ({ENTRY_A})"
        )


class TestDeviceInfoScopedByEntry:
    def test_switch_hdw_variant(self):
        _assert_scoped_by_entry(
            lambda entry_id: PlumEconetSwitch(
                _coordinator(), entry_id, "hdwpumpforce", "Force pump", 512, 0
            ),
            "switch (HDW)",
        )

    def test_switch_boiler_variant(self):
        _assert_scoped_by_entry(
            lambda entry_id: PlumEconetSwitch(
                _coordinator(), entry_id, "hdwstartoneloading", "Reload", 1, 0
            ),
            "switch (boiler fallback)",
        )

    def test_select(self):
        _assert_scoped_by_entry(
            lambda entry_id: PlumEconetSelect(
                _coordinator(), entry_id, "hdwusermode", "DHW Mode", {0: "off"}, {"off": 0}
            ),
            "select",
        )

    def test_number_boiler_variant(self):
        # buforlongloadtime doesn't match the circuit/mixer/hdw prefixes,
        # so it's the one that actually stays on the generic boiler device.
        _assert_scoped_by_entry(
            lambda entry_id: PlumEcomaxNumber(
                _coordinator(),
                SimpleNamespace(entry_id=entry_id),
                "buforlongloadtime",
                (0, 180, 1, "mdi:timer"),
            ),
            "number (boiler fallback)",
        )

    def test_number_hdw_variant(self):
        _assert_scoped_by_entry(
            lambda entry_id: PlumEcomaxNumber(
                _coordinator(),
                SimpleNamespace(entry_id=entry_id),
                "hdwtsetpoint",
                (10, 60, 1, "mdi:thermometer"),
            ),
            "number (hdw)",
        )

    def test_number_circuit_variant(self):
        _assert_scoped_by_entry(
            lambda entry_id: PlumEcomaxNumber(
                _coordinator(),
                SimpleNamespace(entry_id=entry_id),
                "circuit2curvefloor",
                (0.1, 4.0, 0.1, "mdi:chart-bell-curve"),
            ),
            "number (circuit)",
        )

    def test_number_mixer_variant(self):
        _assert_scoped_by_entry(
            lambda entry_id: PlumEcomaxNumber(
                _coordinator(),
                SimpleNamespace(entry_id=entry_id),
                "mixer1valveopeningtime",
                (0, 100, 1, "mdi:valve"),
            ),
            "number (mixers)",
        )

    def test_water_heater(self):
        _assert_scoped_by_entry(
            lambda entry_id: PlumEcomaxWaterHeater(
                _coordinator(), entry_id, "hdw", "cur", "tgt", "min", "max", "mode"
            ),
            "water_heater",
        )

    def test_sensor_circuit_variant(self):
        _assert_scoped_by_entry(
            lambda entry_id: PlumEcomaxSensor(
                _coordinator(),
                SimpleNamespace(entry_id=entry_id),
                "tempcircuit1",
                ("°C", "mdi:thermometer", "temperature"),
                circuit_id=1,
            ),
            "sensor (circuit)",
        )

    def test_sensor_boiler_variant(self):
        _assert_scoped_by_entry(
            lambda entry_id: PlumEcomaxSensor(
                _coordinator(),
                SimpleNamespace(entry_id=entry_id),
                "tempcwu",
                ("°C", "mdi:thermometer", "temperature"),
            ),
            "sensor (boiler fallback)",
        )

    def test_climate(self):
        _assert_scoped_by_entry(
            lambda entry_id: PlumEcomaxClimate(
                _coordinator(), SimpleNamespace(entry_id=entry_id), 2, "cur", "tgt", "active"
            ),
            "climate",
        )

    def test_calendar_hdw_variant(self):
        _assert_scoped_by_entry(
            lambda entry_id: PlumEconetCalendar(
                _coordinator(), SimpleNamespace(entry_id=entry_id), "hdw", 0
            ),
            "calendar (HDW)",
        )

    def test_calendar_circuit_variant(self):
        _assert_scoped_by_entry(
            lambda entry_id: PlumEconetCalendar(
                _coordinator(), SimpleNamespace(entry_id=entry_id), "circuit", 2
            ),
            "calendar (circuit)",
        )


class TestNumberRoutedToCorrectDeviceCategory:
    """Regression for a real bug: number.py only special-cased "mixer*"
    slugs, so every circuit heating-curve and every "hdw*" setting fell
    through to the generic boiler device instead of its own Circuit N / DHW
    device -- entry-scoping alone (TestDeviceInfoScopedByEntry above)
    doesn't catch this, since a wrong-but-consistently-scoped device is
    still "scoped by entry". This checks the actual device identifier
    suffix matches the category the slug belongs to.
    """

    @staticmethod
    def _identifier_suffix(entity) -> str:
        ((_, identifier),) = entity.device_info["identifiers"]
        return identifier.removeprefix(ENTRY_A)

    def test_circuit_slug_goes_to_its_circuit_device(self):
        entity = PlumEcomaxNumber(
            _coordinator(),
            SimpleNamespace(entry_id=ENTRY_A),
            "circuit2curvefloor",
            (0.1, 4.0, 0.1, "mdi:x"),
        )
        assert self._identifier_suffix(entity) == "_circuit_2"

    def test_hdw_slug_goes_to_hdw_device(self):
        entity = PlumEcomaxNumber(
            _coordinator(), SimpleNamespace(entry_id=ENTRY_A), "hdwtsetpoint", (10, 60, 1, "mdi:x")
        )
        assert self._identifier_suffix(entity) == "_hdw"

    def test_mixer_slug_goes_to_its_circuits_device(self):
        """Mixer N is circuit N's valve; its settings sit with the circuit, like the
        valve-position sensor does."""
        entity = PlumEcomaxNumber(
            _coordinator(),
            SimpleNamespace(entry_id=ENTRY_A),
            "mixer2valveopeningtime",
            (0, 100, 1, "mdi:x"),
        )
        assert self._identifier_suffix(entity) == "_circuit_2"

    def test_unprefixed_slug_falls_back_to_boiler_device(self):
        entity = PlumEcomaxNumber(
            _coordinator(),
            SimpleNamespace(entry_id=ENTRY_A),
            "buforlongloadtime",
            (0, 180, 1, "mdi:x"),
        )
        assert self._identifier_suffix(entity) == ""  # bare entry_id, no suffix
