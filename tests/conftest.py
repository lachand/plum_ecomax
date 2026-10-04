"""Global fixtures for Plum EcoMAX integration tests."""

import os
import sys

import pytest

# CRITICAL: This adds the root directory to the Python path.
# It allows tests to do: "from custom_components.plum_ecomax import ..."
sys.path.append(os.getcwd())

# We removed 'auto_enable_custom_integrations' because we are using Mocks.
# We don't need to load the full Home Assistant component logic for unit tests.


@pytest.fixture
def hass():
    """Mock the Home Assistant object for easier testing."""
    from unittest.mock import MagicMock

    hass = MagicMock()
    hass.data = {}
    return hass


def set_loaded_entries(hass, coordinators: dict) -> None:
    """Expose {entry_id: coordinator} the way services see them:
    hass.config_entries.async_loaded_entries(DOMAIN) -> entries carrying
    their coordinator as runtime_data.
    """
    from unittest.mock import MagicMock

    entries = []
    for entry_id, coordinator in coordinators.items():
        entry = MagicMock()
        entry.entry_id = entry_id
        entry.runtime_data = coordinator
        entries.append(entry)
    hass.config_entries.async_loaded_entries.return_value = entries
