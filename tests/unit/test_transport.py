"""PlumTransport: the persistent connection and its transactions (timeouts,
cancellation, unsolicited frames, buffer bounds, closing), against fake streams."""

from __future__ import annotations

import asyncio
import struct
import time

import pytest

from custom_components.plum_ecomax import transport as transport_module
from custom_components.plum_ecomax.plum_device import PlumDevice
from custom_components.plum_ecomax.protocol import CMD_READ_RESP, CMD_READ_VAL, build_frame
from custom_components.plum_ecomax.transport import PlumTransport
from tests.unit.fake_streams import (
    _BROADCAST,
    _connections,
    _HangingStream,
    _install_open_connection,
    _ScriptedStream,
)
from tests.unit.wire_fixtures import SPEC_READ_RESPONSE, with_session


def _make_transport() -> PlumTransport:
    return PlumTransport("192.0.2.1", 8899)


class TestAsyncTimeouts:
    async def test_stalled_peer_times_out_and_keeps_the_connection_the_first_time(
        self, monkeypatch
    ):
        device = _make_transport()
        stream = _HangingStream()
        _install_open_connection(monkeypatch, lambda: stream)

        started = time.monotonic()
        result = await device.transaction(b"frame", timeout=0.05)

        assert result is None
        assert time.monotonic() - started < 1.0  # bounded by asyncio.timeout, not by 30s
        assert device.consecutive_failures == 1
        # A late answer can't be mistaken for the next one (matched by session),
        # so the connection is kept rather than paying a ~1 s reconnection.
        assert not stream.closed and device._writer is not None

    async def test_a_second_consecutive_timeout_drops_the_connection(self, monkeypatch):
        device = _make_transport()
        stream = _HangingStream()
        _install_open_connection(monkeypatch, lambda: stream)

        await device.transaction(b"frame", timeout=0.05)
        await device.transaction(b"frame", timeout=0.05)

        assert device.consecutive_failures == 2
        assert stream.closed and device._writer is None

    async def test_cancelled_transaction_closes_the_connection(self, monkeypatch):
        # A late answer to an abandoned request would be misread as the
        # reply to the next one, so a cancelled exchange must not leave the
        # connection open.
        device = _make_transport()
        stream = _HangingStream()
        _install_open_connection(monkeypatch, lambda: stream)

        task = asyncio.ensure_future(device.transaction(b"frame", timeout=30))
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert stream.closed and device._writer is None

    async def test_connect_timeout_counts_as_failure_and_retries_once(self, monkeypatch):
        device = _make_transport()
        monkeypatch.setattr(transport_module, "CONNECT_TIMEOUT", 0.05)
        attempts = {"n": 0}

        async def _never_connects(*_a, **_k):
            attempts["n"] += 1
            await asyncio.sleep(30)

        monkeypatch.setattr(asyncio, "open_connection", _never_connects)

        assert await device.transaction(b"frame") is None
        assert attempts["n"] == 2
        assert device.consecutive_failures == 2


class TestAsyncClose:
    async def test_without_a_connection_it_is_a_no_op(self):
        device = _make_transport()
        await device.async_close()
        assert device._writer is None

    async def test_it_closes_and_waits_for_the_transport(self, monkeypatch):
        device = _make_transport()
        waited = []

        class _Stream(_HangingStream):
            async def wait_closed(self):
                waited.append(True)

        stream = _Stream()
        _install_open_connection(monkeypatch, lambda: stream)
        await device.ensure_connection()

        await device.async_close()

        assert stream.closed and waited and device._writer is None

    async def test_a_transport_error_while_closing_is_swallowed(self, monkeypatch):
        device = _make_transport()

        class _Stream(_HangingStream):
            async def wait_closed(self):
                raise OSError("already reset")

        _install_open_connection(monkeypatch, lambda: _Stream())
        await device.ensure_connection()

        await device.async_close()  # must not raise

        assert device._writer is None


class TestUnrelatedFrames:
    async def test_only_unrelated_frames_time_out_without_dropping_the_connection(
        self, monkeypatch
    ):
        device = _make_transport()
        opened = _connections(
            monkeypatch, lambda: _ScriptedStream(lambda s: [_BROADCAST, _BROADCAST], hang=True)
        )

        result = await device.transaction(
            b"frame\x00\x00\x00\x00\x00\x00\x00\x00\x00",
            timeout=0.05,
            accept=PlumDevice._read_answer_to(42),
        )

        assert result is None
        assert not opened[0].closed and device._writer is not None  # first failure: kept
        assert device.consecutive_failures == 1

    @pytest.mark.parametrize("noise_byte", [0x00, 0x68])  # no start byte / only false starts
    async def test_a_long_stream_of_noise_does_not_grow_the_buffer_without_bound(
        self, monkeypatch, noise_byte
    ):
        monkeypatch.setattr(transport_module, "MAX_RESPONSE_BUFFER", 16384)
        device = _make_transport()
        noise = bytes([noise_byte]) * 1024
        sent = {"n": 0}
        longest = {"n": 0}
        original = transport_module.pop_valid_frame

        def _spy(buffer):
            longest["n"] = max(longest["n"], len(buffer))
            return original(buffer)

        monkeypatch.setattr(transport_module, "pop_valid_frame", _spy)

        class _Noisy(_ScriptedStream):
            async def read(self, n):
                if sent["n"] < 64:  # 64 KiB of noise before the real answer
                    sent["n"] += 1
                    return noise
                return await super().read(n)

        _connections(monkeypatch, lambda: _Noisy(lambda s: [with_session(SPEC_READ_RESPONSE, s)]))

        request = build_frame(CMD_READ_VAL, struct.pack("<HB BH", 7, 1, 1, 16))
        result = await device.transaction(request, accept=PlumDevice._read_answer_to(7))

        assert result is not None and result.func == CMD_READ_RESP
        assert longest["n"] <= 16384 + 1024  # trimmed once it passed the cap
