"""parameters.py: validating readings and choosing which parameters to probe (pure functions)."""

from __future__ import annotations

import pytest

from custom_components.plum_ecomax.const import (
    ALARM_BITMASK_SLUGS,
    DEVICE_INFO_PARAMS,
    MANUAL_MODE_SLUG,
    SENSOR_TYPES,
    SWITCH_TYPES,
)
from custom_components.plum_ecomax.parameters import (
    MAX_DELTA_REJECTIONS,
    detection_candidates,
    validate_value,
)

PARAMS = {
    "bounded": {"min": 10, "max": 90},
    "smoothed": {"min": 0, "max": 100, "max_delta": 0.5},
    "tempprobe": {},  # no JSON bounds: the generic "temp" range applies
    "pressure_bar": {},
    "free": {},
}


def _validate(slug, raw, cached=None, counts=None):
    return validate_value(PARAMS, slug, raw, cached, {} if counts is None else counts)


class TestValidateValue:
    def test_none_is_rejected(self):
        assert _validate("free", None) == (False, None)

    @pytest.mark.parametrize("code", [999, 999.0])
    def test_the_sensor_fault_code_is_rejected(self, code):
        assert _validate("free", code) == (False, None)

    def test_json_bounds_accept_inside_and_reject_outside(self):
        assert _validate("bounded", 50) == (True, 50)
        assert _validate("bounded", 5) == (False, None)
        assert _validate("bounded", 95) == (False, None)

    def test_a_non_numeric_value_skips_the_bounds(self):
        assert _validate("bounded", "text") == (True, "text")

    def test_generic_ranges_apply_by_keyword_when_there_are_no_json_bounds(self):
        assert _validate("tempprobe", 21.5) == (True, 21.5)
        assert _validate("tempprobe", 250) == (False, None)
        assert _validate("pressure_bar", 6.0) == (False, None)

    def test_a_slug_with_no_rule_passes(self):
        assert _validate("free", 12345) == (True, 12345)

    def test_an_unknown_slug_falls_back_to_the_generic_ranges(self):
        assert validate_value({}, "tempx", 21, None, {}) == (True, 21)
        assert validate_value({}, "tempx", 500, None, {}) == (False, None)

    def test_max_delta_rejects_a_jump_until_the_escape_hatch(self):
        counts: dict[str, int] = {}
        # A jump of 5 against a 0.5 limit is rejected MAX_DELTA_REJECTIONS - 1 times ...
        for expected in range(1, MAX_DELTA_REJECTIONS):
            assert _validate("smoothed", 20.0, cached=15.0, counts=counts) == (False, None)
            assert counts["smoothed"] == expected
        # ... then trusted (the value probably really moved), clearing the counter.
        assert _validate("smoothed", 20.0, cached=15.0, counts=counts) == (True, 20.0)
        assert "smoothed" not in counts

    def test_a_small_step_is_accepted_and_clears_the_counter(self):
        counts = {"smoothed": 2}
        assert _validate("smoothed", 15.3, cached=15.0, counts=counts) == (True, 15.3)
        assert "smoothed" not in counts

    def test_max_delta_needs_a_cached_value(self):
        assert _validate("smoothed", 50.0, cached=None) == (True, 50.0)

    def test_json_bounds_are_never_given_the_max_delta_escape_hatch(self):
        counts = {"smoothed": MAX_DELTA_REJECTIONS}
        assert _validate("smoothed", 500.0, cached=15.0, counts=counts) == (False, None)


class TestDetectionCandidates:
    def test_only_slugs_present_in_the_map_are_returned(self):
        wanted = [*DEVICE_INFO_PARAMS, MANUAL_MODE_SLUG]
        result = detection_candidates({slug: {} for slug in wanted})
        assert set(result) == set(wanted)

    def test_everything_an_entity_reads_is_a_candidate(self):
        everything = {
            *SENSOR_TYPES,
            *SWITCH_TYPES,
            *ALARM_BITMASK_SLUGS,
            *DEVICE_INFO_PARAMS,
            MANUAL_MODE_SLUG,
        }
        result = detection_candidates({slug: {} for slug in everything})
        assert everything <= set(result)

    def test_the_result_has_no_duplicates_and_keeps_order(self):
        result = detection_candidates({slug: {} for slug in [*SENSOR_TYPES, *SWITCH_TYPES]})
        assert len(result) == len(set(result))
        assert result == list(dict.fromkeys(result))

    def test_an_empty_map_gives_no_candidates(self):
        assert detection_candidates({}) == []
