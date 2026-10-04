"""Fake asyncio streams shared by the driver and transport tests.

They stand in for the (reader, writer) pair returned by asyncio.open_connection:
scripted answers, answers that echo the request's session id (like the real
boiler), a peer that never answers, a dead connection, concurrency tracking.
"""

from __future__ import annotations

import asyncio
import struct

from custom_components.plum_ecomax.protocol import CMD_READ_VAL
from tests.unit.wire_fixtures import (
    SPEC_READ_RESPONSE,
    SPEC_WRITE_OK_RESPONSE,
    response_frame,
    session_of_request,
    with_session,
)


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


def _connections(monkeypatch, make_stream):
    opened = []

    def _factory():
        stream = make_stream()
        opened.append(stream)
        return stream

    _install_open_connection(monkeypatch, _factory)
    return opened


_BROADCAST = response_frame(0xC0, b"\x11" * 300)  # unsolicited stream frame, func 0xC0


_PID16_VALUE = struct.unpack("<I", bytes.fromhex("00E02B46"))[0]


_PARAM = {"type": "DWORD", "exponent": 0}


async def _noop():
    return None
