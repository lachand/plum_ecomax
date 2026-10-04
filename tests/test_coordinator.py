"""Unit tests for the PlumDataUpdateCoordinator."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.plum_ecomax.coordinator import PlumDataUpdateCoordinator
from custom_components.plum_ecomax.parameters import MAX_DELTA_REJECTIONS


# Mock class to simulate the PlumDevice behavior
class MockDevice:
    def __init__(self):
        # We simulate a params_map with mixed configurations
        self.params_map = {
            "temp_strict_json": {"min": 10, "max": 50, "name": "Strict"},  # Has JSON limits
            "temp_generic": {"name": "Generic"},  # No JSON limits
            "pressure_bar": {"name": "Pressure"},  # Should use generic pressure limits
            # Mirrors the real device map's tempwthr entry (the only
            # parameter with max_delta set) -- see MAX_DELTA_REJECTIONS.
            "temp_noisy_outdoor": {"min": -20, "max": 50, "max_delta": 0.5, "name": "Outdoor"},
        }
        # Mock methods
        self.get_value = AsyncMock()
        self.set_value = AsyncMock()


@pytest.fixture
def coordinator(hass):
    """Fixture to create a coordinator instance with a mocked device.

    Uses the real constructor now that PlumDataUpdateCoordinator passes
    config_entry= explicitly to DataUpdateCoordinator (so the frame.report_usage
    ContextVar-fallback path -- which raised against this project's
    lightweight hass=MagicMock() -- is no longer taken).
    """
    entry = MagicMock()
    entry.entry_id = "entry_test"
    coord = PlumDataUpdateCoordinator(hass, MockDevice(), entry, update_interval=30)
    # Pre-fill cache to simulate previous state
    coord._cache = {"temp_strict_json": 20}
    return coord


def test_validate_value_protocol_errors(coordinator):
    """Test rejection of protocol specific error codes."""
    # Test None (No data)
    valid, val = coordinator._validate_value("temp_generic", None, 20)
    assert valid is False
    assert val is None

    # Test 999 (Sensor Error)
    valid, val = coordinator._validate_value("temp_generic", 999.0, 20)
    assert valid is False
    assert val is None


def test_validate_value_json_priority(coordinator):
    """Test that JSON limits defined in params_map take priority."""
    slug = "temp_strict_json"

    # 1. Value inside JSON limits [10, 50]
    valid, val = coordinator._validate_value(slug, 25, 20)
    assert valid is True
    assert val == 25

    # 2. Value outside JSON limits (e.g., 60)
    # Even if 60 is valid for generic temp (-20 to 100), JSON max is 50.
    valid, val = coordinator._validate_value(slug, 60, 20)
    assert valid is False
    assert val is None


def test_validate_value_generic_fallback(coordinator):
    """Test that generic VALIDATION_RANGES are used when no JSON limits exist."""
    slug = "temp_generic"  # Contains "temp", so uses (-20, 100) range

    # 1. Valid generic value
    valid, val = coordinator._validate_value(slug, 85, 20)
    assert valid is True
    assert val == 85

    # 2. Invalid generic value (out of -20..100)
    valid, val = coordinator._validate_value(slug, 150, 20)
    assert valid is False
    assert val is None


def test_validate_value_pressure(coordinator):
    """Test generic limits for pressure (0.0 to 4.0 bar)."""
    slug = "pressure_bar"

    # Valid
    valid, _ = coordinator._validate_value(slug, 1.5, 1.0)
    assert valid is True

    # Invalid (Safety valve open?)
    valid, _ = coordinator._validate_value(slug, 5.5, 1.0)
    assert valid is False


class TestMaxDeltaRecovery:
    """Regression for a real bug found on the live boiler (2026-08-13/14):
    tempwthr (the only device-map parameter with max_delta set) got stuck
    at a single value for 15+ hours. A rejected reading never updated the
    cached reference, so every later reading kept being compared against
    the same stale value -- once the real temperature drifted away
    overnight, it could never get back within max_delta of a value from
    hours earlier, freezing the entity indefinitely even though the
    connection/coordinator were healthy the whole time (confirmed via the
    live HA history: every other entity kept updating normally).
    """

    def test_single_jump_is_rejected_like_before(self, coordinator):
        slug = "temp_noisy_outdoor"
        valid, val = coordinator._validate_value(slug, 40.0, 36.8)  # jump of 3.2
        assert valid is False
        assert val is None

    def test_small_step_within_delta_is_always_accepted(self, coordinator):
        slug = "temp_noisy_outdoor"
        valid, val = coordinator._validate_value(slug, 37.1, 36.8)  # jump of 0.3
        assert valid is True
        assert val == 37.1

    def test_repeated_rejections_eventually_accept_the_new_value(self, coordinator):
        """The exact failure mode observed live: the real value moved away
        from the cached one and never came back -- must not stay stuck
        forever.
        """
        slug = "temp_noisy_outdoor"
        cached = 36.8

        for _ in range(MAX_DELTA_REJECTIONS - 1):
            valid, val = coordinator._validate_value(slug, 20.0, cached)
            assert valid is False
            assert val is None

        # One more rejection than the threshold allows -- must now accept.
        valid, val = coordinator._validate_value(slug, 20.0, cached)
        assert valid is True
        assert val == 20.0

    def test_rejection_count_resets_after_acceptance(self, coordinator):
        """After the escape hatch fires (or a normal acceptance), the next
        single big jump must go through the full rejection count again --
        not be immediately force-accepted because of leftover state.
        """
        slug = "temp_noisy_outdoor"

        for _ in range(MAX_DELTA_REJECTIONS - 1):
            coordinator._validate_value(slug, 20.0, 36.8)
        valid, _ = coordinator._validate_value(slug, 20.0, 36.8)
        assert valid is True  # escape hatch fired, counter reset

        # A fresh big jump right after must be rejected again, not
        # force-accepted from stale counter state.
        valid, val = coordinator._validate_value(slug, 5.0, 20.0)
        assert valid is False
        assert val is None

    def test_min_max_violations_are_never_force_accepted(self, coordinator):
        """The escape hatch is specific to max_delta smoothing -- physical
        implausibility (outside min/max) must keep being rejected no
        matter how many times it repeats.
        """
        slug = "temp_noisy_outdoor"
        for _ in range(MAX_DELTA_REJECTIONS + 5):
            valid, val = coordinator._validate_value(
                slug, 60.0, 36.8
            )  # above max=50, not the 999 sentinel
            assert valid is False
            assert val is None
