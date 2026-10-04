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

import pytest

from custom_components.plum_ecomax import plum_device as plum_device_module
from custom_components.plum_ecomax.plum_device import PlumDevice
from custom_components.plum_ecomax.protocol import (
    CMD_WRITE_RESP,
    DEST_ID,
    SOURCE_ID,
    crc16,
)
from tests.unit.fake_streams import (
    _BROADCAST,
    _PARAM,
    _PID16_VALUE,
    _ConcurrencyTrackingStream,
    _connections,
    _FakeStream,
    _install_open_connection,
    _MultiTransactionStream,
    _noop,
    _patch_counting_socket,
    _patch_socket,
    _RaisingOnWriteStream,
    _ScriptedStream,
)
from tests.unit.wire_fixtures import (
    SPEC_READ_RESPONSE,
    SPEC_WRITE_OK_RESPONSE,
    response_frame,
    with_session,
)


def _make_device(**overrides) -> PlumDevice:
    device = PlumDevice("192.0.2.1", **overrides)
    return device


# ---------------------------------------------------------------------------
# get_value / set_value via a fake socket (no real network)
# ---------------------------------------------------------------------------


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
        assert device._transport._writer is not None
        device.close()
        assert device._transport._writer is None

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


class TestConnectionAfterTimeouts:
    async def test_a_success_between_timeouts_keeps_the_connection(self, monkeypatch):
        device = _make_device()
        _connections(
            monkeypatch,
            lambda: _ScriptedStream(lambda s: [with_session(SPEC_READ_RESPONSE, s)]),
        )
        device.consecutive_failures = 1  # one earlier timeout

        assert await device._read_value_once(16, _PARAM) == _PID16_VALUE

        assert device.consecutive_failures == 0 and device._transport._writer is not None


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
        assert opened[0].closed and device._transport._writer is None

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


class TestSetValueDeadLink:
    async def test_a_dead_link_stops_the_write_retries_early(self, monkeypatch):
        device = _make_device()
        device.params_map = {"w": {"id": 9, "type": "WORD", "exponent": 0}}
        attempts = []

        async def _fail(pid, payload):
            attempts.append(pid)
            device.consecutive_failures += 2  # a failed transaction: 2 connect attempts
            return False

        async def _no_sleep(_s):
            return None

        monkeypatch.setattr(device, "_write_value_once", _fail)
        monkeypatch.setattr(plum_device_module.asyncio, "sleep", _no_sleep)

        assert await device.set_value("w", 5) is False
        assert len(attempts) == 2  # 4 failures = LINK_DOWN_FAILURES: no third attempt
