"""Fixtures for tests that run against a real Home Assistant (hass, registries,
config entries) via pytest-homeassistant-custom-component.

Run with the plugin loaded explicitly (it pins its own pytest/HA versions, so
it lives in requirements_test_ha.txt, not requirements_test.txt):

    pytest tests_ha -p pytest_homeassistant_custom_component

This folder is deliberately outside tests/: tests/conftest.py replaces `hass`
with a MagicMock for the unit tests, which would shadow the real fixture.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

try:
    import pytest_homeassistant_custom_component  # noqa: F401
except ImportError:  # plain `pytest` without the plugin: skip the whole folder
    collect_ignore_glob = ["test_*.py"]


@pytest.fixture(autouse=True)
def _enable_custom_integrations(enable_custom_integrations):
    """Let Home Assistant load custom_components/plum_ecomax from this repo."""
    yield


@pytest.fixture(autouse=True)
def fake_device():
    """Route both the entry setup and the config-flow probe to FakePlumDevice."""
    from unittest.mock import patch

    from tests_ha.fakes import FakePlumDevice

    FakePlumDevice.reset()
    with (
        patch("custom_components.plum_ecomax.PlumDevice", FakePlumDevice),
        patch("custom_components.plum_ecomax.config_flow.PlumDevice", FakePlumDevice),
    ):
        yield FakePlumDevice
    FakePlumDevice.reset()
