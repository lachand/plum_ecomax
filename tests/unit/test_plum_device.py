"""Unit tests for plum_device.py (Family: wire protocol driver).

This is where the real bugs were (byte-offset error reading the value
after the status byte, no CRC/length/source validation, signed/unsigned
struct format mismatches -- see IMPROVEMENT_PLAN_ARCHIVE.md section 0). These
tests pin the fixed behavior against the worked examples from Plum's
"Standard Transmission Protocols ed.15" PDF so a regression here is
caught by CI instead of only by manually replaying capture logs against
the live boiler.

`_transaction` opens its connection with `asyncio.open_connection(...)`
itself rather than taking an injected transport, so tests that need to
control what "arrives on the wire" monkeypatch `asyncio.open_connection`
with `_FakeStream`, which scripts `read()` to return pre-built frames chunk
by chunk.
"""

from __future__ import annotations

import asyncio
import struct
import time

import pytest

from custom_components.plum_ecomax import plum_device as plum_device_module
from custom_components.plum_ecomax.plum_device import PlumDevice
from custom_components.plum_ecomax.protocol import (
    CMD_READ_VAL,
    CMD_WRITE_RESP,
    DEST_ID,
    SOURCE_ID,
    crc16,
)
from tests.unit.wire_fixtures import (
    SPEC_READ_RESPONSE,
    SPEC_WRITE_OK_RESPONSE,
    response_frame,
    session_of_request,
    with_session,
)


def _make_device(**overrides) -> PlumDevice:
    device = PlumDevice("192.0.2.1", **overrides)
    return device


# ---------------------------------------------------------------------------
# get_value / set_value via a fake socket (no real network)
# ---------------------------------------------------------------------------


class _FakeStream:
    """Stands in for an asyncio (reader, writer) pair: scripts one response
    per connection, delivered as pre-chunked bytes, and records what was
    written.
    """

    def __init__(self, response: bytes | None, chunk_size: int = 4096):
        self._response = response
        self._chunk_size = chunk_size
        self._pos = 0
        self.sent: list[bytes] = []
        self.closed = False

    def write(self, data: bytes):
        self.sent.append(data)
        # Like the real boiler, answer a read with the request's own session id.
        if self._response is not None and self._response[7] == 0xC3:
            self._response = with_session(self._response, session_of_request(data))

    async def drain(self):
        pass

    async def read(self, _n: int) -> bytes:
        if self._response is None or self._pos >= len(self._response):
            return b""
        chunk = self._response[self._pos : self._pos + self._chunk_size]
        self._pos += len(chunk)
        return chunk

    def close(self):
        self.closed = True

    async def wait_closed(self):
        pass


def _install_open_connection(monkeypatch, factory):
    """Route asyncio.open_connection to `factory() -> stream` (reader and
    writer being the same fake object)."""

    async def _open(*_a, **_k):
        stream = factory()
        return stream, stream

    monkeypatch.setattr(asyncio, "open_connection", _open)


def _patch_socket(monkeypatch, response: bytes | None, chunk_size: int = 4096):
    _install_open_connection(monkeypatch, lambda: _FakeStream(response, chunk_size=chunk_size))


def _patch_counting_socket(monkeypatch, response: bytes | None):
    """Like _patch_socket, but every connection attempt is counted and gets
    its own fresh _FakeStream instance -- used by the persistent-connection
    tests below to prove whether a connection was reused (factory called
    once) or reopened (factory called again).
    """
    calls = {"count": 0}

    def _factory():
        calls["count"] += 1
        return _FakeStream(response)

    _install_open_connection(monkeypatch, _factory)
    return calls


class TestReadValueOnce:
    async def test_successful_read(self, monkeypatch):
        device = _make_device()
        _patch_socket(monkeypatch, SPEC_READ_RESPONSE)
        val = await device._read_value_once(16, {"type": "DWORD", "exponent": 0})
        assert val == struct.unpack("<I", bytes.fromhex("00E02B46"))[0]

    async def test_pid_mismatch_returns_none(self, monkeypatch):
        device = _make_device()
        _patch_socket(monkeypatch, SPEC_READ_RESPONSE)
        # Response answers pid 16, not 999.
        assert await device._read_value_once(999, {"type": "DWORD", "exponent": 0}) is None

    async def test_no_response_returns_none(self, monkeypatch):
        device = _make_device()
        _patch_socket(monkeypatch, None)
        assert await device._read_value_once(16, {"type": "DWORD", "exponent": 0}) is None

    async def test_response_split_across_multiple_recv_calls(self, monkeypatch):
        device = _make_device()
        _patch_socket(monkeypatch, SPEC_READ_RESPONSE, chunk_size=5)
        val = await device._read_value_once(16, {"type": "DWORD", "exponent": 0})
        assert val == struct.unpack("<I", bytes.fromhex("00E02B46"))[0]


class TestSyncSetValue:
    async def test_confirmed_write_returns_true(self, monkeypatch):
        device = _make_device()
        _patch_socket(monkeypatch, SPEC_WRITE_OK_RESPONSE)
        assert await device._write_value_once(172, b"payload") is True

    async def test_auth_error_returns_false(self, monkeypatch):
        device = _make_device()
        # 68 06 00 00 00 01 00 a9 7d <crc> 16, code=0x7D (auth error)
        frame = bytearray(SPEC_WRITE_OK_RESPONSE)
        frame[8] = 0x7D
        body = bytes(frame[1 : 1 + 2 + struct.unpack("<H", bytes(frame[1:3]))[0]])
        new_crc = crc16(body)
        frame[-3:-1] = struct.pack(">H", new_crc)
        _patch_socket(monkeypatch, bytes(frame))
        assert await device._write_value_once(172, b"payload") is False

    async def test_no_response_returns_false(self, monkeypatch):
        device = _make_device()
        _patch_socket(monkeypatch, None)
        assert await device._write_value_once(172, b"payload") is False

    async def test_empty_payload_ack_counts_as_success(self, monkeypatch):
        """Confirmed against real hardware (2026-08-07, IMPROVEMENT_PLAN_ARCHIVE.md):
        this firmware ACKs a successful write with func=CMD_WRITE_RESP and
        an *empty* data field (l_val=5, no result code byte at all) instead
        of the explicit 0xE5 the spec's worked example shows. The write
        still has to be treated as confirmed, or every write against this
        hardware is reported as failed even though it actually took effect
        (verified by reading the value back after the write).
        """
        device = _make_device()
        header = struct.pack("<HHHB", 5, SOURCE_ID, DEST_ID, CMD_WRITE_RESP)
        body = header  # no payload at all
        crc = crc16(body)
        frame = b"\x68" + body + struct.pack(">H", crc) + b"\x16"
        _patch_socket(monkeypatch, frame)
        assert await device._write_value_once(172, b"payload") is True


class TestReadValueOncesBatch:
    async def test_batch_decodes_both_blocks(self, monkeypatch):
        device = _make_device()
        # _sync_get_values_batch increments session_id before using it, and
        # SPEC_READ_RESPONSE echoes session=59, so pre-set to 58.
        device.session_id = 58
        _patch_socket(monkeypatch, SPEC_READ_RESPONSE)

        items = [
            (16, {"type": "DWORD", "exponent": 0}),
            (147, {"type": "DWORD", "exponent": 0}),
        ]
        values = await device._read_values_batch(items)

        assert values[16] == struct.unpack("<I", bytes.fromhex("00E02B46"))[0]
        # Block 1 in the spec example actually holds 3 params, but we only
        # requested 1 starting at pid 147 -- n_params=3 in the response
        # doesn't match our request (n_params=1), so that block must be
        # rejected rather than guessed at.
        assert 147 not in values

    async def test_empty_items_returns_empty_without_network_call(self, monkeypatch):
        device = _make_device()
        _patch_socket(monkeypatch, None)
        assert await device._read_values_batch([]) == {}


class TestGetValuesRawRouting:
    """get_values() must keep RAW-type slugs out of the batched request
    and fall back to reading them one at a time (variable length, no size
    prefix on the wire -- see get_values()'s docstring).
    """

    @pytest.mark.asyncio
    async def test_raw_slugs_are_excluded_from_batching_and_read_individually(self, monkeypatch):
        device = _make_device()
        device.params_map = {
            "normal_slug": {"id": 16, "type": "DWORD", "exponent": 0},
            "raw_slug": {"id": 373, "type": "RAW", "exponent": 0},
        }

        batch_calls = []

        async def fake_batch(items):
            batch_calls.append(items)
            return {16: 42}

        single_calls = []

        async def fake_single(pid, param):
            single_calls.append((pid, param))
            return "1M86DIP6H1GQE1H6P3KGIH5"

        monkeypatch.setattr(device, "_read_values_batch", fake_batch)
        monkeypatch.setattr(device, "_read_value_once", fake_single)

        results = await device.get_values(["normal_slug", "raw_slug"])

        assert results == {"normal_slug": 42, "raw_slug": "1M86DIP6H1GQE1H6P3KGIH5"}
        assert len(batch_calls) == 1
        assert batch_calls[0] == [(16, device.params_map["normal_slug"])]
        assert single_calls == [(373, device.params_map["raw_slug"])]


# ---------------------------------------------------------------------------
# Persistent connection: reused across transactions, reconnected on any
# anomaly instead of the previous connect-send-recv-close-per-transaction
# pattern.
# ---------------------------------------------------------------------------


class _RaisingOnWriteStream:
    """Stands in for a persistent connection that has silently died (e.g.
    the boiler closed our idle connection): the write itself raises, as it
    would for a genuinely broken pipe/reset connection.
    """

    def write(self, _data):
        raise OSError("simulated dead connection")

    async def drain(self):
        pass

    async def read(self, _n):
        return b""

    def close(self):
        pass


class _MultiTransactionStream:
    """Delivers one complete response per write()->read() round, replayed
    fresh for each request. Unlike _FakeStream's single pre-scripted
    buffer, this doesn't pretend a second response is already sitting in
    the stream before the second request was even sent -- a real boiler
    can't answer request #2 before receiving it, so pre-concatenating two
    responses doesn't model a real persistent connection's timing.
    """

    def __init__(self, response: bytes):
        self._base = response
        self._response = response
        self._pos = 0

    def write(self, data):
        self._response = with_session(self._base, session_of_request(data))
        self._pos = 0  # a fresh, complete response becomes available

    async def drain(self):
        pass

    async def read(self, n):
        chunk = self._response[self._pos : self._pos + n]
        self._pos += len(chunk)
        return chunk

    def close(self):
        pass


class TestPersistentConnection:
    async def test_connection_is_reused_across_successful_transactions(self, monkeypatch):
        device = _make_device()
        calls = {"count": 0}

        def _factory():
            calls["count"] += 1
            return _MultiTransactionStream(SPEC_READ_RESPONSE)

        _install_open_connection(monkeypatch, _factory)

        first = await device._read_value_once(16, {"type": "DWORD", "exponent": 0})
        second = await device._read_value_once(16, {"type": "DWORD", "exponent": 0})

        assert first == second == struct.unpack("<I", bytes.fromhex("00E02B46"))[0]
        assert calls["count"] == 1  # one connect for both transactions

    async def test_hard_socket_error_reconnects_and_retries_within_one_call(self, monkeypatch):
        device = _make_device()
        instances = iter([_RaisingOnWriteStream(), _FakeStream(SPEC_READ_RESPONSE)])
        _install_open_connection(monkeypatch, lambda: next(instances))

        val = await device._read_value_once(16, {"type": "DWORD", "exponent": 0})

        # First (dead) connection's write() raised -- transparently recovered
        # by reconnecting and resending within the same _sync_get_value()
        # call, the caller never sees a failure.
        assert val == struct.unpack("<I", bytes.fromhex("00E02B46"))[0]

    async def test_pid_mismatch_closes_connection_so_next_call_reconnects(self, monkeypatch):
        device = _make_device()
        calls = _patch_counting_socket(monkeypatch, SPEC_READ_RESPONSE)

        # Response answers pid 16, not 999 -- a mismatch, which should
        # drop the connection (see plum_device.py's _sync_get_value).
        await device._read_value_once(999, {"type": "DWORD", "exponent": 0})
        await device._read_value_once(999, {"type": "DWORD", "exponent": 0})

        assert calls["count"] == 2  # second call had to reconnect

    async def test_close_tears_down_connection_and_next_call_reconnects(self, monkeypatch):
        device = _make_device()
        calls = _patch_counting_socket(monkeypatch, SPEC_READ_RESPONSE)

        await device._read_value_once(16, {"type": "DWORD", "exponent": 0})
        assert device._writer is not None
        device.close()
        assert device._writer is None

        await device._read_value_once(16, {"type": "DWORD", "exponent": 0})
        assert calls["count"] == 2

    async def test_consecutive_failures_increments_on_failure_and_resets_on_success(
        self, monkeypatch
    ):
        device = _make_device()
        _patch_socket(monkeypatch, None)  # every transaction fails

        await device._read_value_once(16, {"type": "DWORD", "exponent": 0})
        assert device.consecutive_failures > 0

        _patch_socket(monkeypatch, SPEC_READ_RESPONSE)
        await device._read_value_once(16, {"type": "DWORD", "exponent": 0})
        assert device.consecutive_failures == 0


class TestLastWriteError:
    async def test_rejection_sets_last_write_error_to_the_raw_code(self, monkeypatch):
        device = _make_device()
        frame = bytearray(SPEC_WRITE_OK_RESPONSE)
        frame[8] = 0x7D
        body = bytes(frame[1 : 1 + 2 + struct.unpack("<H", bytes(frame[1:3]))[0]])
        frame[-3:-1] = struct.pack(">H", crc16(body))
        _patch_socket(monkeypatch, bytes(frame))

        assert await device._write_value_once(172, b"payload") is False
        assert device.last_write_error == 0x7D

    async def test_success_clears_last_write_error(self, monkeypatch):
        device = _make_device()
        device.last_write_error = 0x7D  # left over from an earlier rejection
        _patch_socket(monkeypatch, SPEC_WRITE_OK_RESPONSE)

        assert await device._write_value_once(172, b"payload") is True
        assert device.last_write_error is None


# ---------------------------------------------------------------------------
# I/O serialization: a background write and a polling read must never open
# concurrent socket transactions (IMPROVEMENT_PLAN_ARCHIVE.md section A).
# ---------------------------------------------------------------------------


class _ConcurrencyTrackingStream:
    """Fake connection that answers a read or a write appropriately (based
    on the func byte it was sent) and records the peak number of
    simultaneously-open transactions.

    The "occupied" window is bracketed by write() -> read() (a transaction
    actually in flight on the wire), not by connect()/close(): the
    persistent-connection PlumDevice keeps a successful connection open
    across transactions instead of closing it every time, so close() is not
    a reliable "this transaction is done" signal.
    """

    active = 0
    max_concurrent = 0

    def __init__(self):
        self._sent_frame: bytes | None = None
        self._responded = False

    def write(self, data: bytes):
        self._sent_frame = data
        cls = _ConcurrencyTrackingStream
        cls.active += 1
        cls.max_concurrent = max(cls.max_concurrent, cls.active)

    async def drain(self):
        await asyncio.sleep(0.05)  # hold the "connection" open long enough to overlap if unlocked

    async def read(self, _n):
        # Decrement unconditionally: write() always increments once, and a
        # reused-but-already-answered instance (persistent connection
        # picked up stale, gets the "closed" b"" signal, see plum_device's
        # _read_response) still needs its matching decrement.
        _ConcurrencyTrackingStream.active -= 1
        if self._responded:
            return b""
        self._responded = True
        func = self._sent_frame[7]
        if func == CMD_READ_VAL:
            return with_session(SPEC_READ_RESPONSE, session_of_request(self._sent_frame))
        return SPEC_WRITE_OK_RESPONSE

    def close(self):
        pass


class TestIoSerialization:
    @pytest.mark.asyncio
    async def test_concurrent_get_and_set_never_overlap_on_the_wire(self, monkeypatch):
        _ConcurrencyTrackingStream.active = 0
        _ConcurrencyTrackingStream.max_concurrent = 0
        _install_open_connection(monkeypatch, _ConcurrencyTrackingStream)

        device = _make_device()
        device.params_map = {"pid16": {"id": 16, "type": "DWORD", "exponent": 0}}

        await asyncio.gather(
            device.get_value("pid16", retries=1),
            device.set_value("pid16", 5, password="0000", user="admin"),
            device.get_value("pid16", retries=1),
        )

        assert _ConcurrencyTrackingStream.max_concurrent == 1


class _HangingStream:
    """Accepts writes but never answers -- a stalled boiler."""

    closed = False

    def write(self, _data):
        pass

    async def drain(self):
        pass

    async def read(self, _n):
        await asyncio.sleep(30)
        return b""

    def close(self):
        self.closed = True


class TestAsyncTimeouts:
    async def test_stalled_peer_times_out_and_keeps_the_connection_the_first_time(
        self, monkeypatch
    ):
        device = _make_device()
        stream = _HangingStream()
        _install_open_connection(monkeypatch, lambda: stream)

        started = time.monotonic()
        result = await device._transaction(b"frame", timeout=0.05)

        assert result is None
        assert time.monotonic() - started < 1.0  # bounded by asyncio.timeout, not by 30s
        assert device.consecutive_failures == 1
        # A late answer can't be mistaken for the next one (matched by session),
        # so the connection is kept rather than paying a ~1 s reconnection.
        assert not stream.closed and device._writer is not None

    async def test_a_second_consecutive_timeout_drops_the_connection(self, monkeypatch):
        device = _make_device()
        stream = _HangingStream()
        _install_open_connection(monkeypatch, lambda: stream)

        await device._transaction(b"frame", timeout=0.05)
        await device._transaction(b"frame", timeout=0.05)

        assert device.consecutive_failures == 2
        assert stream.closed and device._writer is None

    async def test_a_success_between_timeouts_keeps_the_connection(self, monkeypatch):
        device = _make_device()
        _connections(
            monkeypatch,
            lambda: _ScriptedStream(lambda s: [with_session(SPEC_READ_RESPONSE, s)]),
        )
        device.consecutive_failures = 1  # one earlier timeout

        assert await device._read_value_once(16, _PARAM) == _PID16_VALUE

        assert device.consecutive_failures == 0 and device._writer is not None

    async def test_cancelled_transaction_closes_the_connection(self, monkeypatch):
        # A late answer to an abandoned request would be misread as the
        # reply to the next one, so a cancelled exchange must not leave the
        # connection open.
        device = _make_device()
        stream = _HangingStream()
        _install_open_connection(monkeypatch, lambda: stream)

        task = asyncio.ensure_future(device._transaction(b"frame", timeout=30))
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert stream.closed and device._writer is None

    async def test_connect_timeout_counts_as_failure_and_retries_once(self, monkeypatch):
        device = _make_device()
        monkeypatch.setattr(plum_device_module, "CONNECT_TIMEOUT", 0.05)
        attempts = {"n": 0}

        async def _never_connects(*_a, **_k):
            attempts["n"] += 1
            await asyncio.sleep(30)

        monkeypatch.setattr(asyncio, "open_connection", _never_connects)

        assert await device._transaction(b"frame") is None
        assert attempts["n"] == 2
        assert device.consecutive_failures == 2


class TestFailFastOnDeadLink:
    """get_values() gives up on the remaining batches once the link is clearly
    down, instead of paying CONNECT_TIMEOUT-sized waits for every batch."""

    @staticmethod
    def _device_with_many_params(n=64):
        device = _make_device()
        device.params_map = {
            f"p{i}": {"id": i, "type": "WORD", "exponent": 0, "unit": "", "name_orig": ""}
            for i in range(n)
        }
        return device

    async def test_dead_link_stops_after_the_failure_threshold(self, monkeypatch):
        device = self._device_with_many_params(64)  # 4 batches of 16
        attempts = {"n": 0}

        async def _refuse(*_a, **_k):
            attempts["n"] += 1
            raise ConnectionRefusedError("down")

        monkeypatch.setattr(asyncio, "open_connection", _refuse)
        monkeypatch.setattr(plum_device_module.asyncio, "sleep", lambda _s: _noop())

        results = await device.get_values(list(device.params_map), retries=2)

        assert results == {}
        # one batch = 2 attempts x 2 connection tries = 4 failures = the threshold;
        # without the fail-fast the other 3 batches would add 12 more attempts.
        assert attempts["n"] == plum_device_module.LINK_DOWN_FAILURES

    async def test_a_reachable_boiler_that_answers_garbage_is_not_cut_short(self, monkeypatch):
        # Invalid answers don't count as link failures, so every batch is still tried.
        device = self._device_with_many_params(32)  # 2 batches
        calls = {"n": 0}

        async def _empty_batch(items):
            calls["n"] += 1
            return {}

        monkeypatch.setattr(device, "_read_values_batch", _empty_batch)
        monkeypatch.setattr(plum_device_module.asyncio, "sleep", lambda _s: _noop())

        await device.get_values(list(device.params_map), retries=2)

        assert calls["n"] == 4  # 2 batches x 2 attempts, none skipped


async def _noop():
    return None


class _ScriptedStream:
    """Answers each request with `script(session) -> [frames]`, delivered in
    order; `hang` makes it go silent afterwards (a boiler that never answers)."""

    def __init__(self, script, hang=False):
        self._script = script
        self._hang = hang
        self._pending = b""
        self.closed = False

    def write(self, data):
        self._pending += b"".join(self._script(session_of_request(data)))

    async def drain(self):
        pass

    async def read(self, n):
        if self._pending:
            chunk, self._pending = self._pending[:n], self._pending[n:]
            return chunk
        if self._hang:
            await asyncio.sleep(30)
        return b""

    def close(self):
        self.closed = True


_BROADCAST = response_frame(0xC0, b"\x11" * 300)  # unsolicited stream frame, func 0xC0
_PID16_VALUE = struct.unpack("<I", bytes.fromhex("00E02B46"))[0]
_PARAM = {"type": "DWORD", "exponent": 0}


def _connections(monkeypatch, make_stream):
    opened = []

    def _factory():
        stream = make_stream()
        opened.append(stream)
        return stream

    _install_open_connection(monkeypatch, _factory)
    return opened


class TestUnsolicitedFrames:
    """The module interleaves 0xC0 broadcasts and duplicates of its answers with
    the real response: they must be skipped, not treated as a desync that closes
    the connection (a reconnection costs about a second on the real module)."""

    async def test_a_broadcast_burst_before_the_answer_is_skipped_without_reconnecting(
        self, monkeypatch
    ):
        device = _make_device()
        opened = _connections(
            monkeypatch,
            lambda: _ScriptedStream(
                lambda s: [_BROADCAST, _BROADCAST, _BROADCAST, with_session(SPEC_READ_RESPONSE, s)]
            ),
        )

        assert await device._read_value_once(16, _PARAM) == _PID16_VALUE
        assert await device._read_value_once(16, _PARAM) == _PID16_VALUE

        assert len(opened) == 1  # one connection for both reads
        assert not opened[0].closed

    async def test_a_stale_duplicate_of_the_previous_answer_is_skipped(self, monkeypatch):
        device = _make_device()
        previous = {"frame": None}

        def script(session):
            stale = [previous["frame"]] if previous["frame"] else []  # old session, same func
            fresh = with_session(SPEC_READ_RESPONSE, session)
            previous["frame"] = fresh
            return [*stale, fresh]

        opened = _connections(monkeypatch, lambda: _ScriptedStream(script))

        assert await device._read_value_once(16, _PARAM) == _PID16_VALUE
        assert await device._read_value_once(16, _PARAM) == _PID16_VALUE  # stale copy arrives first

        assert len(opened) == 1 and not opened[0].closed

    async def test_only_unrelated_frames_time_out_without_dropping_the_connection(
        self, monkeypatch
    ):
        device = _make_device()
        opened = _connections(
            monkeypatch, lambda: _ScriptedStream(lambda s: [_BROADCAST, _BROADCAST], hang=True)
        )
        device.session_id = 41

        result = await device._transaction(
            b"frame\x00\x00\x00\x00\x00\x00\x00\x00\x00",
            timeout=0.05,
            accept=device._read_answer_to(42),
        )

        assert result is None
        assert not opened[0].closed and device._writer is not None  # first failure: kept
        assert device.consecutive_failures == 1

    async def test_a_write_acknowledgement_is_picked_out_of_the_broadcasts(self, monkeypatch):
        device = _make_device()
        _connections(
            monkeypatch,
            lambda: _ScriptedStream(lambda s: [_BROADCAST, SPEC_WRITE_OK_RESPONSE, _BROADCAST]),
        )

        assert await device._write_value_once(172, b"payload") is True

    async def test_a_batch_answer_is_picked_out_of_broadcasts_and_stale_copies(self, monkeypatch):
        device = _make_device()
        stale = with_session(SPEC_READ_RESPONSE, 1)  # some earlier session
        _connections(
            monkeypatch,
            lambda: _ScriptedStream(
                lambda s: [_BROADCAST, stale, _BROADCAST, with_session(SPEC_READ_RESPONSE, s)]
            ),
        )
        items = [(16, {"type": "DWORD", "exponent": 0})]

        values = await device._read_values_batch(items)

        assert values == {16: _PID16_VALUE}

    @pytest.mark.parametrize("noise_byte", [0x00, 0x68])  # no start byte / only false starts
    async def test_a_long_stream_of_noise_does_not_grow_the_buffer_without_bound(
        self, monkeypatch, noise_byte
    ):
        monkeypatch.setattr(plum_device_module, "MAX_RESPONSE_BUFFER", 16384)
        device = _make_device()
        noise = bytes([noise_byte]) * 1024
        sent = {"n": 0}
        longest = {"n": 0}
        original = plum_device_module.pop_valid_frame

        def _spy(buffer):
            longest["n"] = max(longest["n"], len(buffer))
            return original(buffer)

        monkeypatch.setattr(plum_device_module, "pop_valid_frame", _spy)

        class _Noisy(_ScriptedStream):
            async def read(self, n):
                if sent["n"] < 64:  # 64 KiB of noise before the real answer
                    sent["n"] += 1
                    return noise
                return await super().read(n)

        _connections(monkeypatch, lambda: _Noisy(lambda s: [with_session(SPEC_READ_RESPONSE, s)]))

        assert await device._read_value_once(16, _PARAM) == _PID16_VALUE
        assert longest["n"] <= 16384 + 1024  # trimmed once it passed the cap


class TestPartialBatchIsNotRetried:
    @staticmethod
    def _device(n=16):
        device = _make_device()
        device.params_map = {
            f"p{i}": {"id": i, "type": "WORD", "exponent": 0, "unit": "", "name_orig": ""}
            for i in range(n)
        }
        return device

    async def test_a_valid_but_short_answer_is_requested_once(self, monkeypatch):
        device = self._device()
        calls = {"n": 0}

        async def _short(items):
            calls["n"] += 1
            return {pid: 1 for pid, _ in items[:5]}  # 5/16: the module stopped at an unknown pid

        monkeypatch.setattr(device, "_read_values_batch", _short)

        results = await device.get_values(list(device.params_map), retries=5)

        assert calls["n"] == 1
        assert len(results) == 5

    async def test_an_empty_answer_is_still_retried(self, monkeypatch):
        device = self._device()
        calls = {"n": 0}

        async def _none(items):
            calls["n"] += 1
            return {}

        async def _no_sleep(_s):
            return None

        monkeypatch.setattr(device, "_read_values_batch", _none)
        monkeypatch.setattr(plum_device_module.asyncio, "sleep", _no_sleep)

        await device.get_values(list(device.params_map), retries=3)

        assert calls["n"] == 3


class TestLoadMap:
    def test_a_missing_map_file_raises_and_is_logged(self, caplog, tmp_path):
        device = _make_device(map_file=str(tmp_path / "nope.json"))
        with pytest.raises(OSError):
            device.load_map()
        assert "Error loading map" in caplog.text

    def test_an_invalid_json_map_raises_value_error(self, tmp_path):
        bad = tmp_path / "bad.json"
        bad.write_text("{not json")
        device = _make_device(map_file=str(bad))
        with pytest.raises(ValueError):
            device.load_map()

    def test_a_valid_map_is_loaded(self, tmp_path):
        good = tmp_path / "map.json"
        good.write_text('{"x": {"id": 1, "type": "WORD", "exponent": 0}}')
        device = _make_device(map_file=str(good))
        device.load_map()
        assert device.params_map["x"]["id"] == 1


class TestGetValueFallbacks:
    @staticmethod
    def _device():
        device = _make_device()
        device.params_map = {"t": {"id": 5, "type": "WORD", "exponent": 0}}
        return device

    async def test_unknown_slug_returns_none(self):
        assert await self._device().get_value("nope") is None

    async def test_failed_reads_fall_back_to_the_last_known_value(self, monkeypatch):
        device = self._device()
        answers = iter([7, None, None])

        async def _read(pid, param):
            return next(answers)

        async def _no_sleep(_s):
            return None

        monkeypatch.setattr(device, "_read_value_once", _read)
        monkeypatch.setattr(plum_device_module.asyncio, "sleep", _no_sleep)

        assert await device.get_value("t", retries=1) == 7  # fresh read, cached
        assert await device.get_value("t", retries=2) == 7  # both attempts fail: cached value


class TestSetValueGuards:
    @staticmethod
    def _device():
        device = _make_device()
        device.params_map = {"w": {"id": 9, "type": "WORD", "exponent": 0}}
        return device

    async def test_unknown_slug_is_refused(self):
        assert await self._device().set_value("nope", 1) is False

    async def test_a_value_that_cannot_be_encoded_is_refused_without_any_io(self, monkeypatch):
        device = self._device()
        called = []

        async def _write(pid, payload):
            called.append(pid)
            return True

        monkeypatch.setattr(device, "_write_value_once", _write)

        assert await device.set_value("w", "not a number") is False
        assert not called

    async def test_three_unconfirmed_attempts_then_failure(self, monkeypatch):
        device = self._device()
        attempts = []

        async def _write(pid, payload):
            attempts.append(pid)
            return False

        async def _no_sleep(_s):
            return None

        monkeypatch.setattr(device, "_write_value_once", _write)
        monkeypatch.setattr(plum_device_module.asyncio, "sleep", _no_sleep)

        assert await device.set_value("w", 5) is False
        assert len(attempts) == 3

    async def test_credentials_are_sent_in_the_payload_and_can_be_overridden(self, monkeypatch):
        device = _make_device(user="admin", password="0000")
        device.params_map = {"w": {"id": 9, "type": "WORD", "exponent": 0}}
        seen = []

        async def _write(pid, payload):
            seen.append(payload)
            return True

        monkeypatch.setattr(device, "_write_value_once", _write)

        await device.set_value("w", 5)
        await device.set_value("w", 5, user="USER-1", password="9999")

        assert seen[0].startswith(b"admin\x00" + b"0000\x00")
        assert seen[1].startswith(b"USER-1\x00" + b"9999\x00")


def _answer(session, body=b""):
    """A read answer carrying `session` followed by `body` (batch layout)."""
    return response_frame(0xC3, struct.pack("<H", session) + body)


class TestMalformedAnswers:
    """accept() guarantees the command and session; what's left to check is the
    payload's own shape."""

    async def test_a_too_short_single_read_drops_the_connection(self, monkeypatch):
        device = _make_device()
        opened = _connections(monkeypatch, lambda: _ScriptedStream(lambda s: [_answer(s, b"\x01")]))

        assert await device._read_value_once(16, _PARAM) is None
        assert opened[0].closed and device._writer is None

    async def test_an_answer_for_another_pid_drops_the_connection(self, monkeypatch):
        device = _make_device()
        # session | nblocks=1 nparams=1 | pid=99 | status | value(4)
        body = b"\x01\x01" + struct.pack("<H", 99) + b"\x00" + b"\x01\x00\x00\x00"
        opened = _connections(monkeypatch, lambda: _ScriptedStream(lambda s: [_answer(s, body)]))

        assert await device._read_value_once(16, _PARAM) is None
        assert opened[0].closed

    async def test_a_too_short_batch_answer_drops_the_connection(self, monkeypatch):
        device = _make_device()
        opened = _connections(monkeypatch, lambda: _ScriptedStream(lambda s: [_answer(s)]))

        assert await device._read_values_batch([(16, _PARAM)]) == {}
        assert opened[0].closed

    @pytest.mark.parametrize(
        "body,expected",
        [
            # block header cut short after the first block
            (
                b"\x02" + b"\x01" + struct.pack("<H", 16) + b"\x00" + b"\x2a\x00\x00\x00" + b"\x01",
                {16: 42},
            ),
            # a block announcing 2 params: width unknowable, stop trusting the rest
            (b"\x01" + b"\x02" + struct.pack("<H", 16) + b"\x00" + b"\x2a\x00\x00\x00", {}),
            # a block for a pid we never asked about
            (b"\x01" + b"\x01" + struct.pack("<H", 77) + b"\x00" + b"\x2a\x00\x00\x00", {}),
            # value cut off mid-block
            (b"\x01" + b"\x01" + struct.pack("<H", 16) + b"\x00" + b"\x2a\x00", {}),
        ],
    )
    async def test_truncated_or_odd_batch_blocks_keep_what_was_parsed(
        self, monkeypatch, body, expected
    ):
        device = _make_device()
        _connections(monkeypatch, lambda: _ScriptedStream(lambda s: [_answer(s, body)]))

        assert await device._read_values_batch([(16, _PARAM)]) == expected

    async def test_a_batch_with_no_items_does_no_io(self, monkeypatch):
        device = _make_device()
        opened = _connections(monkeypatch, lambda: _ScriptedStream(lambda s: []))

        assert await device._read_values_batch([]) == {}
        assert not opened


class TestAsyncClose:
    async def test_without_a_connection_it_is_a_no_op(self):
        device = _make_device()
        await device.async_close()
        assert device._writer is None

    async def test_it_closes_and_waits_for_the_transport(self, monkeypatch):
        device = _make_device()
        waited = []

        class _Stream(_HangingStream):
            async def wait_closed(self):
                waited.append(True)

        stream = _Stream()
        _install_open_connection(monkeypatch, lambda: stream)
        await device._ensure_connection()

        await device.async_close()

        assert stream.closed and waited and device._writer is None

    async def test_a_transport_error_while_closing_is_swallowed(self, monkeypatch):
        device = _make_device()

        class _Stream(_HangingStream):
            async def wait_closed(self):
                raise OSError("already reset")

        _install_open_connection(monkeypatch, lambda: _Stream())
        await device._ensure_connection()

        await device.async_close()  # must not raise

        assert device._writer is None
