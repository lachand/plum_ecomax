"""device.py: devices this entry no longer creates are detected and removed
(circuits that are no longer active, devices with no entity left), and the
main boiler device is never touched.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from custom_components.plum_ecomax import async_remove_config_entry_device
from custom_components.plum_ecomax import device as device_module
from custom_components.plum_ecomax.const import CONF_ACTIVE_CIRCUITS, DOMAIN

ENTRY_ID = "entryA"


def _entry(active=("2",)):
    entry = MagicMock()
    entry.entry_id = ENTRY_ID
    entry.data = {CONF_ACTIVE_CIRCUITS: list(active)}
    return entry


def _device(ident, name="dev"):
    dev = MagicMock()
    dev.id = f"id-{ident}"
    dev.name = name
    dev.identifiers = {(DOMAIN, ident)}
    return dev


def _with_entities(has_entities):
    return patch.multiple(
        device_module.er,
        async_get=MagicMock(),
        async_entries_for_device=MagicMock(return_value=[object()] if has_entities else []),
    )


def test_main_boiler_device_is_never_stale():
    with _with_entities(False):
        assert not device_module.is_stale_device(MagicMock(), _entry(), _device(ENTRY_ID))


def test_inactive_circuit_is_stale_even_if_entities_remain():
    with _with_entities(True):
        assert device_module.is_stale_device(
            MagicMock(), _entry(active=("2",)), _device(f"{ENTRY_ID}_circuit_3")
        )


def test_active_circuit_is_kept():
    with _with_entities(True):
        assert not device_module.is_stale_device(
            MagicMock(), _entry(active=("2",)), _device(f"{ENTRY_ID}_circuit_2")
        )


def test_other_device_without_entities_is_stale_and_with_entities_is_kept():
    hdw = _device(f"{ENTRY_ID}_hdw")
    with _with_entities(False):
        assert device_module.is_stale_device(MagicMock(), _entry(), hdw)
    with _with_entities(True):
        assert not device_module.is_stale_device(MagicMock(), _entry(), hdw)


def test_circuit_of_another_entry_is_not_mistaken_for_this_ones():
    # "entryAB_circuit_3" must not match entry "entryA"'s circuit pattern.
    with _with_entities(True):
        assert not device_module.is_stale_device(
            MagicMock(), _entry(active=("2",)), _device("entryAB_circuit_3")
        )


def test_remove_stale_devices_only_detaches_the_stale_ones():
    entry = _entry(active=("2",))
    main = _device(ENTRY_ID, "boiler")
    live = _device(f"{ENTRY_ID}_circuit_2", "Circuit 2")
    old = _device(f"{ENTRY_ID}_circuit_5", "Circuit 5")
    registry = MagicMock()

    with (
        _with_entities(True),
        patch.object(device_module.dr, "async_get", return_value=registry),
        patch.object(
            device_module.dr, "async_entries_for_config_entry", return_value=[main, live, old]
        ),
    ):
        device_module.async_remove_stale_devices(MagicMock(), entry)

    registry.async_update_device.assert_called_once_with(old.id, remove_config_entry_id=ENTRY_ID)


async def test_ui_delete_is_allowed_only_for_stale_devices():
    entry = _entry(active=("2",))
    with _with_entities(True):
        assert not await async_remove_config_entry_device(
            MagicMock(), entry, _device(f"{ENTRY_ID}_circuit_2")
        )
        assert not await async_remove_config_entry_device(MagicMock(), entry, _device(ENTRY_ID))
        assert await async_remove_config_entry_device(
            MagicMock(), entry, _device(f"{ENTRY_ID}_circuit_6")
        )
