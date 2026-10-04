"""Persistent TCP connection to the ecoNET module.

One connection, kept open across transactions (reconnecting costs about a
second on this module), with request/response transactions bounded by
asyncio.timeout(). The module streams unsolicited frames and duplicates of its
own answers on the same connection, so a transaction takes an `accept`
predicate that picks the expected response out of that stream. Frame building
and parsing live in protocol.py (pure functions); what the frames mean
(parameters, retries, ordering of reads and writes) lives in plum_device.py.
"""

import asyncio
import logging
import time
from collections.abc import Callable

from .protocol import START_BYTE, Frame, pop_valid_frame

logger = logging.getLogger(__name__)

# Seconds allowed to open the TCP connection to the module.
CONNECT_TIMEOUT = 5.0

# Upper bound on the bytes kept while waiting for the expected response: the
# module streams unsolicited frames on the same connection, so a never-matching
# stream must not grow the buffer without limit.
MAX_RESPONSE_BUFFER = 64 * 1024
# Bytes kept when trimming: more than the largest frame (l_val <= 4096).
_KEEP_TAIL = 8192


class PlumTransport:
    """The persistent connection to one module, with its failure bookkeeping."""

    def __init__(self, ip: str, port: int) -> None:
        self.ip = ip
        self.port = port
        # Persistent connection: reused across transactions instead of a
        # fresh connect/close per request (see transaction). None means "not
        # currently connected".
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        # Consecutive failed transactions (connect error, no matching answer
        # in time) -- coordinator.py surfaces a "connection lost" repair issue
        # once this crosses a threshold; any successful transaction resets it.
        self.consecutive_failures = 0
        # Wall-clock time of the last transaction that got a valid response
        # (None until the first one). Surfaced as a diagnostic sensor.
        self.last_success_ts: float | None = None

    async def ensure_connection(self) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        """Returns the persistent connection, opening a fresh one if needed."""
        if self._reader is None or self._writer is None:
            async with asyncio.timeout(CONNECT_TIMEOUT):
                self._reader, self._writer = await asyncio.open_connection(self.ip, self.port)
        return self._reader, self._writer

    def close(self) -> None:
        """Tears down the persistent connection, if one is open.

        Never blocks: StreamWriter.close() only schedules the transport
        shutdown on the event loop. Called on integration unload/reload, after
        a one-shot config_flow probe and on any anomaly, so a stale connection
        never lingers. Must run on the event loop.
        """
        writer, self._reader, self._writer = self._writer, None, None
        if writer is not None:
            try:
                writer.close()
            except OSError:
                pass

    async def async_close(self) -> None:
        """Like close(), but also waits for the transport to finish closing."""
        writer = self._writer
        self.close()
        if writer is not None:
            try:
                async with asyncio.timeout(CONNECT_TIMEOUT):
                    await writer.wait_closed()
            except (OSError, TimeoutError):
                pass

    async def transaction(
        self,
        frame: bytes,
        timeout: float = 2.0,
        accept: Callable[[Frame], bool] | None = None,
    ) -> Frame | None:
        """Executes one request/response transaction over a persistent
        connection, reusing it across calls instead of reconnecting for
        every single transaction (a full TCP handshake per read/write adds
        real overhead when polling every few seconds).

        A hard socket error (dead/reset connection) closes the connection and
        retries once against a freshly reconnected one within this same call,
        since that's the case most likely to just be a transient drop (e.g.
        the boiler closed our idle connection) rather than the boiler being
        genuinely unreachable. A plain timeout (no matching answer in time)
        does NOT retry inline -- callers already retry with backoff
        (get_value/get_values/set_value) -- and keeps the connection the first
        time: late answers are skipped by session id, so it isn't desynced,
        and reconnecting costs about a second. A second consecutive failure
        closes it.

        The module streams unsolicited frames (func 0xC0, in bursts) and
        duplicates of its own answers on the same connection. `accept` picks
        the expected response out of that stream: frames it rejects are
        skipped instead of being taken for a desync, so they no longer force a
        reconnection (which costs about a second on this module). Bytes read
        past the accepted frame are dropped with the call-local buffer; a
        duplicate arriving later is skipped by the next transaction's `accept`
        (same command but an older session id).

        The whole transaction is bounded by asyncio.timeout(), so a stalled
        peer can never hold the event loop's I/O lock open indefinitely.

        Args:
            frame: The binary frame to send.
            timeout: Time budget for sending and receiving, in seconds.
            accept: Predicate selecting the response among the received
                frames; defaults to accepting the first valid frame.

        Returns:
            Frame | None: the accepted response frame, or None on
            timeout/error.
        """
        for attempt in (1, 2):
            try:
                reader, writer = await self.ensure_connection()
            except OSError as e:  # includes TimeoutError
                logger.debug("Connect failed (attempt %d/2): %s", attempt, e)
                self.close()
                self.consecutive_failures += 1
                if attempt == 2:
                    return None
                continue

            try:
                async with asyncio.timeout(timeout):
                    writer.write(frame)
                    await writer.drain()
                    result = await self._read_response(reader, accept)
            except asyncio.CancelledError:
                # Cancelled mid-exchange (e.g. an integration unload): the
                # boiler's answer may still arrive and would be misread as
                # the reply to the next request, so drop the connection.
                self.close()
                raise
            except TimeoutError:
                # No answer in time. A late answer can't be mistaken for the
                # next one (accept() matches the session id), so the stream is
                # not desynced and the connection is kept: reconnecting costs
                # about a second on this module. A second consecutive failure
                # means the connection itself is suspect (half-open): drop it.
                # No inline retry either (see docstring).
                self.consecutive_failures += 1
                if self.consecutive_failures >= 2:
                    self.close()
                return None
            except OSError as e:
                logger.debug("Transaction failed (attempt %d/2): %s", attempt, e)
                self.close()
                self.consecutive_failures += 1
                if attempt == 2:
                    return None
                continue

            self.consecutive_failures = 0
            self.last_success_ts = time.time()
            return result
        return None

    async def _read_response(
        self,
        reader: asyncio.StreamReader,
        accept: Callable[[Frame], bool] | None = None,
    ) -> Frame:
        """Reads until a valid frame that `accept` selects arrives.

        Has no deadline of its own: the caller wraps it in asyncio.timeout(),
        which cancels the pending read when the budget runs out. Valid frames
        that `accept` rejects (unsolicited broadcasts, stale duplicates) are
        discarded and the read goes on.

        Args:
            reader: The connected stream to read from.
            accept: Predicate for the wanted frame; None accepts the first
                valid frame.

        Returns:
            Frame: the first accepted frame.

        Raises:
            OSError: If the peer closes the connection (read() returns no
                data) -- treated as a hard failure by the caller, which
                closes and retries once against a fresh connection, same
                as any other connection error.
        """
        buffer = bytearray()
        skipped = 0
        while True:
            chunk = await reader.read(1024)
            if not chunk:
                raise OSError("Connection closed by peer")
            buffer.extend(chunk)

            while (frame := pop_valid_frame(buffer)) is not None:
                if accept is None or accept(frame):
                    if skipped:
                        logger.debug("Skipped %d unrelated frame(s) before the response", skipped)
                    return frame
                skipped += 1

            if START_BYTE not in buffer:
                buffer.clear()  # no frame can start in what's left
            elif len(buffer) > MAX_RESPONSE_BUFFER:
                del buffer[:-_KEEP_TAIL]  # only noise: keep the tail
