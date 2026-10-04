"""__init__._adopt_serial_unique_id: IP-keyed entries move to the boiler's
serial number once the first refresh has read it.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from custom_components.plum_ecomax import _adopt_serial_unique_id


def _setup(entry_uid, data, others=()):
    hass = MagicMock()
    entry = MagicMock(unique_id=entry_uid)
    hass.config_entries.async_entries.return_value = [entry, *others]
    coordinator = MagicMock(data=data)
    return hass, entry, coordinator


def test_ip_keyed_entry_adopts_serial():
    hass, entry, coord = _setup("1.2.3.4", {"uid": "ABC123"})
    _adopt_serial_unique_id(hass, entry, coord)
    hass.config_entries.async_update_entry.assert_called_once_with(entry, unique_id="ABC123")


def test_already_serial_keyed_is_left_alone():
    hass, entry, coord = _setup("ABC123", {"uid": "ABC123"})
    _adopt_serial_unique_id(hass, entry, coord)
    hass.config_entries.async_update_entry.assert_not_called()


def test_unreadable_serial_is_left_alone():
    hass, entry, coord = _setup("1.2.3.4", {})
    _adopt_serial_unique_id(hass, entry, coord)
    hass.config_entries.async_update_entry.assert_not_called()


def test_serial_owned_by_another_entry_is_not_stolen():
    other = MagicMock(unique_id="ABC123")
    hass, entry, coord = _setup("1.2.3.4", {"uid": "ABC123"}, others=[other])
    _adopt_serial_unique_id(hass, entry, coord)
    hass.config_entries.async_update_entry.assert_not_called()
