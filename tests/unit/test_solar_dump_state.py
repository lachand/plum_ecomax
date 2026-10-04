"""solar_dump.py: session/auto state lives on each entry's coordinator (no
module-level globals), and its hooks are tied to the config entry's lifetime.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.const import EVENT_HOMEASSISTANT_STOP

from custom_components.plum_ecomax import solar_dump


def _coord(entry_id="e1"):
    c = MagicMock()
    c.entry_id = entry_id
    return c


def test_two_boilers_do_not_share_state():
    a, b = _coord("a"), _coord("b")
    solar_dump._state(a).owner = "auto"
    solar_dump._auto_state(a)["runtime_today"] = 42.0

    assert solar_dump._state(b).owner is None
    assert solar_dump.auto_runtime_minutes(b) == 0.0
    assert solar_dump.auto_runtime_minutes(a) == 42.0


def test_state_is_created_once_per_coordinator():
    a = _coord()
    assert solar_dump._state(a) is solar_dump._state(a)


async def test_stop_listener_stops_this_entrys_session_and_unsubscribes_with_it():
    hass, entry, coord = MagicMock(), MagicMock(), _coord()
    unsub = MagicMock()
    hass.bus.async_listen_once.return_value = unsub

    solar_dump.async_register_stop_listener(hass, entry, coord)

    assert hass.bus.async_listen_once.call_args.args[0] == EVENT_HOMEASSISTANT_STOP
    entry.async_on_unload.assert_called_once_with(unsub)

    handler = hass.bus.async_listen_once.call_args.args[1]
    with patch.object(solar_dump, "async_stop_for_entry", AsyncMock()) as stop:
        await handler(None)
    stop.assert_awaited_once_with(hass, coord)


async def test_auto_tick_unsub_is_hooked_to_the_entry_once_and_is_idempotent():
    hass, coord = MagicMock(), _coord()
    unsub = MagicMock()
    with (
        patch.object(solar_dump, "async_track_time_interval", return_value=unsub),
        patch.object(solar_dump, "_auto_tick", AsyncMock()),
    ):
        await solar_dump.async_auto_enable(hass, coord, "e1")
        await solar_dump.async_auto_disable(hass, coord)
        await solar_dump.async_auto_enable(hass, coord, "e1")

    # registered with the entry exactly once despite the enable/disable/enable
    assert coord.config_entry.async_on_unload.call_count == 1

    # the hooked callback is harmless after the tick was already dropped
    solar_dump._drop_auto_tick(coord)
    solar_dump._drop_auto_tick(coord)
    assert unsub.call_count == 2  # one per armed period, never a double cancel
