"""Low-level communication layer for Plum EcoMAX devices.

This module handles the asyncio TCP connection: the persistent link, request/
response transactions, retries and I/O ordering. It acts as the driver that
talks directly to the ecoMax module. Frame building, CRC and value encoding
live in protocol.py (pure functions, no I/O).
"""

import asyncio
import json
import logging
import struct
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .protocol import (
    CMD_READ_RESP,
    CMD_READ_VAL,
    CMD_WRITE_FORCE,
    CMD_WRITE_RESP,
    START_BYTE,
    VALUE_BYTE_LEN,
    WRITE_RESULT_OK,
    Frame,
    ParamDef,
    build_frame,
    decode_value,
    encode_value,
    pop_valid_frame,
)

logger = logging.getLogger(__name__)

DEFAULT_BATCH_SIZE = 16

# Upper bound on the bytes kept while waiting for the expected response: the
# module streams unsolicited frames on the same connection, so a never-matching
# stream must not grow the buffer without limit.
MAX_RESPONSE_BUFFER = 64 * 1024
# Bytes kept when trimming: more than the largest frame (l_val <= 4096).
_KEEP_TAIL = 8192

# Failed transactions (connect error / timeout, not an invalid answer) within one
# get_values() cycle after which the link is considered down and the remaining
# batches are skipped: each failed attempt can cost up to CONNECT_TIMEOUT, so
# without this a cycle over many batches would take minutes to give up.
LINK_DOWN_FAILURES = 4

# Seconds allowed to open the TCP connection to the module.
CONNECT_TIMEOUT = 5.0
# Time budget for an answer. Measured on a real boiler: answered requests take
# ~40 ms (max ~0.5 s over 200 requests) and a request that isn't answered at all
# is never answered later, so waiting several seconds only delays the retry.
READ_TIMEOUT = 1.5
BATCH_TIMEOUT = 2.0


# Bundled parameter map, resolved from this file rather than from the HA config dir.
DEVICE_MAP_PATH = Path(__file__).parent / "device_map_ecomax360i.json"


class PlumDevice:
    """Handles low-level communication with the Plum EcoMAX boiler.

    This class manages the TCP connection lifecycle, frame encapsulation,
    checksum verification, and parameter mapping.
    """

    def __init__(
        self,
        ip: str,
        port: int = 8899,
        password: str = "0000",
        user: str = "admin",
        map_file: str = "device_map.json",
    ) -> None:
        """Initializes the PlumDevice driver.

        Args:
            ip: The IP address of the ecoNET module.
            port: The TCP port (default 8899).
            password: The device password (default "0000").
            user: The username for authentication (default "admin").
            map_file: Path to the JSON file containing parameter definitions.
        """
        self.ip = ip
        self.port = port
        self.password = password
        self.user = user
        self.map_file = map_file
        self.params_map: dict[str, ParamDef] = {}
        self.session_id = 10
        self._data_cache: dict[str, Any] = {}
        # Serializes all transactions so a background write and a
        # polling read never open concurrent TCP connections to the boiler.
        self._io_lock = asyncio.Lock()

        # Persistent connection: reused across transactions instead of a
        # fresh connect/close per request (see _transaction). None
        # means "not currently connected".
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        # Consecutive fully-failed transactions (both reconnect attempts
        # exhausted, or no valid frame within timeout) -- coordinator.py
        # surfaces a "connection lost" repair issue once this crosses a
        # threshold, and resets it to 0 on any successful transaction.
        self.consecutive_failures = 0
        # Wall-clock time of the last transaction that got a valid response
        # (None until the first one). Surfaced as a diagnostic sensor.
        self.last_success_ts: float | None = None
        # Raw result code (e.g. 0x7D auth error, 0x7F generic error) from
        # the most recent write the boiler explicitly rejected -- distinct
        # from "never got a response at all". Cleared on the next
        # successful write. coordinator.py uses this to decide whether a
        # write that was never confirmed deserves a specific "rejected"
        # repair issue instead of just the generic warning log.
        self.last_write_error: int | None = None

    def load_map(self) -> None:
        """Loads the parameter definition map from the JSON file.

        The map file defines the ID, type, and exponent for each parameter slug.

        Raises:
            OSError: If the map file can't be read.
            ValueError: If the map file isn't valid JSON.
        """
        try:
            with open(self.map_file) as f:
                self.params_map = json.load(f)
        except (OSError, ValueError) as e:
            logger.error("Error loading map from %s: %s", self.map_file, e)
            raise

    # --- API ---
    async def get_value(self, slug: str, retries: int = 3) -> Any:
        """Asynchronously fetches a parameter value.

        Wraps `_read_value_once` with a retry mechanism and a caching strategy.

        Args:
            slug: The string identifier of the parameter.
            retries: Number of attempts before returning the cached value.

        Returns:
            Any: The current value, or the last cached value if communication fails.
        """
        param = self.params_map.get(slug)
        if not param:
            return None
        pid = param["id"]

        async with self._io_lock:
            for attempt in range(1, retries + 1):
                val = await self._read_value_once(pid, param)
                if val is not None:
                    self._data_cache[slug] = val  # Caching
                    return val
                await asyncio.sleep(0.2 * attempt)

        # Returns the last known value on failure
        return self._data_cache.get(slug)

    async def get_values(
        self, slugs: list, retries: int = 2, batch_size: int = DEFAULT_BATCH_SIZE
    ) -> dict[str, Any]:
        """Asynchronously fetches several parameter values in as few frames as possible.

        Spec 1.5.3.12 (cmd 0x43) allows multiple parameter blocks in a
        single request/response, so this avoids opening one TCP connection
        per parameter for a whole polling cycle.

        Args:
            slugs: Parameter slugs to fetch.
            retries: Attempts per batch before giving up on the missing ones.
            batch_size: Max parameters requested in a single frame.

        Returns:
            Dict[str, Any]: Decoded value per slug that was successfully
            read. Slugs missing from the result mean the read failed for
            that parameter — callers should fall back to their own cache,
            same as a failed get_value() call.
        """
        items = []
        raw_slugs = []
        slug_by_pid: dict[int, str] = {}
        for slug in slugs:
            param = self.params_map.get(slug)
            if not param:
                continue
            # RAW (spec's STRING type) is variable-length with no size
            # prefix on the wire -- _read_value_onces_batch can't know where
            # it ends without decoding it, which would break walking any
            # block that follows it in the same multi-block response. Read
            # these individually instead of batching them.
            if param.get("type") == "RAW":
                raw_slugs.append(slug)
                continue
            pid = param["id"]
            items.append((pid, param))
            slug_by_pid[pid] = slug

        results: dict[str, Any] = {}
        async with self._io_lock:
            failures_at_start = self.consecutive_failures
            for i in range(0, len(items), batch_size):
                if self._link_down_since(failures_at_start):
                    return results
                chunk = items[i : i + batch_size]
                for attempt in range(1, retries + 1):
                    values = await self._read_values_batch(chunk)
                    for pid, val in values.items():
                        if val is None:
                            continue
                        slug = slug_by_pid.get(pid)
                        if slug:
                            results[slug] = val
                            self._data_cache[slug] = val
                    if values:
                        # A valid but short answer (the module stops at a pid it
                        # doesn't know) won't change on a retry: callers re-probe
                        # the missing ones individually. Only an empty result
                        # (no answer at all) is worth another attempt.
                        break
                    if self._link_down_since(failures_at_start):
                        break  # no point retrying a dead link; the loop above returns
                    await asyncio.sleep(0.2 * attempt)

            for slug in raw_slugs:
                if self._link_down_since(failures_at_start):
                    return results
                param = self.params_map[slug]
                pid = param["id"]
                for attempt in range(1, retries + 1):
                    val = await self._read_value_once(pid, param)
                    if val is not None:
                        results[slug] = val
                        self._data_cache[slug] = val
                        break
                    await asyncio.sleep(0.2 * attempt)

        return results

    def _link_down_since(self, failures_at_start: int) -> bool:
        """True once LINK_DOWN_FAILURES transactions failed since the baseline.

        consecutive_failures counts connect errors and timeouts only (an
        invalid or mismatched answer doesn't increment it), so a batch
        failing because the boiler dislikes one pid can't trip this.
        """
        if self.consecutive_failures - failures_at_start >= LINK_DOWN_FAILURES:
            logger.debug("Link down: skipping the rest of this read cycle")
            return True
        return False

    async def set_value(
        self, slug: str, value: Any, password: str | None = None, user: str | None = None
    ) -> bool:
        """Asynchronously writes a parameter value.

        Args:
            slug: The string identifier of the parameter.
            value: The new value to set.
            password: Optional password override.
            user: Optional username override.

        Returns:
            bool: True if the write operation was confirmed by the device.
        """
        param = self.params_map.get(slug)
        if not param:
            return False

        # Use stored credentials if not provided
        target_pass = password if password is not None else self.password
        target_user = user if user is not None else self.user

        pid = param["id"]
        encoded = encode_value(value, param)
        if not encoded:
            return False

        user_bytes = (target_user.encode("utf-8") + b"\x00") if target_user else b"\x00"
        pass_bytes = (target_pass.encode("utf-8") + b"\x00") if target_pass else b"\x00"
        full_payload = user_bytes + pass_bytes + b"\x01" + struct.pack("<H", pid) + encoded

        async with self._io_lock:
            for _attempt in range(1, 4):
                if await self._write_value_once(pid, full_payload):
                    return True
                await asyncio.sleep(1.0)
        return False

    # --- TRANSACTION WORKERS ---
    async def _read_value_once(self, pid: int, param: ParamDef) -> Any:
        """Fetches a single value in one transaction (no retry)."""
        self.session_id = (self.session_id + 1) % 65000
        payload = struct.pack("<HB BH", self.session_id, 1, 1, pid)
        frame = build_frame(CMD_READ_VAL, payload)
        result = await self._transaction(
            frame, timeout=READ_TIMEOUT, accept=self._read_answer_to(self.session_id)
        )
        if result is None:
            return None

        # accept() already guaranteed the command and the echoed session id.
        resp = result.payload
        if len(resp) < 7:
            logger.debug("Too-short read response for pid=%s: len=%d", pid, len(resp))
            # A truncated payload isn't a valid answer to this request: drop the
            # connection so the next transaction starts clean.
            self._close_connection()
            return None

        # Data layout (spec 1.5.3.12): session(2) nblocks(1) nparams(1) pid(2) status(1) value(n)
        resp_pid = struct.unpack("<H", resp[4:6])[0]
        if resp_pid != pid:
            logger.debug("PID mismatch for read: requested %s, device answered %s", pid, resp_pid)
            self._close_connection()
            return None

        return decode_value(resp[7:], param)

    async def _write_value_once(self, pid: int, payload: bytes) -> bool:
        """Writes a value in one transaction (no retry)."""
        self.session_id = (self.session_id + 1) % 65000
        frame = build_frame(CMD_WRITE_FORCE, payload)
        # The write acknowledgement carries no session id, so only its command
        # byte can be matched.
        result = await self._transaction(frame, accept=lambda f: f.func == CMD_WRITE_RESP)
        if result is None:
            return False

        # accept() already guaranteed this is a write acknowledgement.
        resp = result.payload

        # Data layout per spec 1.5.3.10: a single result code (0xE5=OK /
        # 0x7D=auth error / 0x7F=error). Confirmed against real hardware
        # (2026-08-07) that this firmware instead ACKs a successful write
        # with an *empty* data field (l_val=5, no code byte at all) rather
        # than an explicit 0xE5 -- treat that as success too. Only reject
        # when a code byte is actually present and isn't 0xE5, so firmware
        # that does send explicit error codes is still caught.
        if len(resp) >= 1 and resp[0] != WRITE_RESULT_OK:
            logger.warning("Write rejected by device for pid=%s: result code 0x%02X", pid, resp[0])
            # An explicit rejection is a legitimate, well-formed answer
            # (func matches) -- the connection itself is healthy, no
            # reason to reconnect. Just remember the code so the
            # coordinator can raise a specific "write rejected" repair
            # issue instead of only the generic "never confirmed" one.
            self.last_write_error = resp[0]
            return False

        self.last_write_error = None
        return True

    async def _read_values_batch(self, items: list[tuple[int, ParamDef]]) -> dict[int, Any]:
        """Fetches several values in a single frame.

        Builds one block per requested pid (spec 1.5.3.12), each holding
        exactly one parameter, so the response mirrors the request
        block-for-block and each value's byte width can be looked up from
        our own params_map instead of relying on a length prefix on the wire.

        Args:
            items: List of (pid, param_def) tuples, in request order.

        Returns:
            Dict[int, Any]: Decoded value per pid that was successfully
            parsed. Missing pids mean a parse/response failure for that
            parameter specifically, or the whole batch failed.
        """
        if not items:
            return {}

        self.session_id = (self.session_id + 1) % 65000
        header = struct.pack("<HB", self.session_id, len(items))
        blocks = b"".join(struct.pack("<BH", 1, pid) for pid, _ in items)
        frame = build_frame(CMD_READ_VAL, header + blocks)

        result = await self._transaction(
            frame, timeout=BATCH_TIMEOUT, accept=self._read_answer_to(self.session_id)
        )
        if result is None:
            return {}

        # accept() already guaranteed the command and the echoed session id.
        resp = result.payload
        if len(resp) < 3:
            logger.debug("Too-short batch read response: len=%d", len(resp))
            self._close_connection()
            return {}

        n_blocks = resp[2]
        values: dict[int, Any] = {}
        offset = 3
        for _ in range(n_blocks):
            if offset + 3 > len(resp):
                break  # truncated response, stop trusting what's left
            n_params = resp[offset]
            first_pid = struct.unpack("<H", resp[offset + 1 : offset + 3])[0]
            offset += 3

            param_def = next((p for pid, p in items if pid == first_pid), None)
            if n_params != 1 or param_def is None:
                # We only ever request one param per block; anything else
                # means we can't reliably know this block's value width.
                logger.debug(
                    "Unexpected block shape (n_params=%s, pid=%s) in batch read",
                    n_params,
                    first_pid,
                )
                break

            value_len = VALUE_BYTE_LEN.get(param_def["type"], 4)
            if offset + 1 + value_len > len(resp):
                break  # truncated mid-block, stop here with what we have

            # resp[offset] is the per-block status byte (unused); value follows it.
            raw = resp[offset + 1 : offset + 1 + value_len]
            offset += 1 + value_len
            values[first_pid] = decode_value(raw, param_def)

        return values

    @staticmethod
    def _read_answer_to(session_id: int) -> Callable[[Frame], bool]:
        """Accept only the answer to the read request that carried `session_id`.

        The module interleaves unsolicited frames (func 0xC0) and duplicates of
        its previous answers with the real response: matching the command and
        the echoed session id picks the right one out of the stream.
        """
        session = struct.pack("<H", session_id)
        return lambda frame: frame.func == CMD_READ_RESP and frame.payload[:2] == session

    async def _ensure_connection(self) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        """Returns the persistent connection, opening a fresh one if needed."""
        if self._reader is None or self._writer is None:
            async with asyncio.timeout(CONNECT_TIMEOUT):
                self._reader, self._writer = await asyncio.open_connection(self.ip, self.port)
        return self._reader, self._writer

    def _close_connection(self) -> None:
        """Tears down the persistent connection, if one is open.

        Never blocks: StreamWriter.close() only schedules the transport
        shutdown on the event loop.
        """
        writer, self._reader, self._writer = self._writer, None, None
        if writer is not None:
            try:
                writer.close()
            except OSError:
                pass

    def close(self) -> None:
        """Public, explicit teardown of the persistent connection.

        Called on integration unload/reload and after a one-shot
        config_flow connection probe, so a stale connection doesn't linger
        until garbage collection. Must run on the event loop.
        """
        self._close_connection()

    async def async_close(self) -> None:
        """Like close(), but also waits for the transport to finish closing."""
        writer = self._writer
        self._close_connection()
        if writer is not None:
            try:
                async with asyncio.timeout(CONNECT_TIMEOUT):
                    await writer.wait_closed()
            except (OSError, TimeoutError):
                pass

    async def _transaction(
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
                reader, writer = await self._ensure_connection()
            except OSError as e:  # includes TimeoutError
                logger.debug("Connect failed (attempt %d/2): %s", attempt, e)
                self._close_connection()
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
                self._close_connection()
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
                    self._close_connection()
                return None
            except OSError as e:
                logger.debug("Transaction failed (attempt %d/2): %s", attempt, e)
                self._close_connection()
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
