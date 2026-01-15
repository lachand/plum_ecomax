"""Global fixtures for Plum EcoMAX integration tests."""
import pytest
import sys
import os

# This adds the root folder to the Python path, allowing imports from custom_components
sys.path.append(os.getcwd())

@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """
    @brief Automatically enables loading of custom integrations.
    @details This fixture comes from 'pytest-homeassistant-custom-component'.
    It is mandatory to allow Home Assistant logic to find your folder 'plum_ecomax'.
    """
    yield

# You can add global mocks here if needed later
# Example: mocking the network connection for all tests
