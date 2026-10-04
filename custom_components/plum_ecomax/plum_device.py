"""Low-level communication layer for Plum EcoMAX devices.

This module handles the TCP socket connections, frame construction (RS-485 over TCP),
CRC calculation, and data encoding/decoding. It acts as the driver that talks
directly to the ecoMax module.

Attributes:
    DEST_ID (int): The default destination address for the boiler (1).
    SOURCE_ID (int): The source address for the integration (100).
    CMD_READ_VAL (int): Command ID to read a parameter (0x43).
    CMD_WRITE_FORCE (int): Command ID to write a parameter (0x29).
"""

import asyncio
import json
import logging
import socket
import struct
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Protocol Constants
DEST_ID = 1
SOURCE_ID = 100
CMD_READ_VAL = 0x43
CMD_WRITE_FORCE = 0x29
CMD_READ_RESP = CMD_READ_VAL | 0x80
CMD_WRITE_RESP = CMD_WRITE_FORCE | 0x80
WRITE_RESULT_OK = 0xE5
START_BYTE = 0x68
STOP_BYTE = 0x16

# Wire size of a decoded value per type (spec 1.4.2), used to walk batched
# multi-parameter responses without a length prefix per value.
VALUE_BYTE_LEN = {
    "BYTE": 1,
    "SHORT_INT": 1,
    "WORD": 2,
    "INT": 2,
    "DWORD": 4,
    "LONG_INT": 4,
    "FLOAT": 4,
}
DEFAULT_BATCH_SIZE = 16


# Bundled parameter map, resolved from this file rather than from the HA config dir.
DEVICE_MAP_PATH = Path(__file__).parent / "device_map_ecomax360i.json"


class PlumDevice:
    """Handles low-level communication with the Plum EcoMAX boiler.

    This class manages the TCP connection lifecycle, frame encapsulation,
    checksum verification, and parameter mapping.
    """

    def __init__(self, ip, port=8899, password="0000", user="admin", map_file="device_map.json"):
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
        self.params_map: dict[str, Any] = {}
        self.session_id = 10
        self._data_cache = {}
        # Serializes all socket transactions so a background write and a
        # polling read never open concurrent TCP connections to the boiler.
        self._io_lock = asyncio.Lock()

        # Persistent connection: reused across transactions instead of a
        # fresh connect/close per request (see _socket_transaction). None
        # means "not currently connected".
        self._sock: socket.socket | None = None
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

    def load_map(self):
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

    # --- ENCODING / DECODING ---
    def _encode(self, value: Any, param_def: dict) -> bytes:
        """Encodes a Python value into raw bytes based on the parameter definition.

        Args:
            value: The value to encode.
            param_def: Dictionary containing 'type' and 'exponent'.

        Returns:
            bytes: The binary representation of the value, or None if encoding fails.
        """
        ptype = param_def["type"]
        exp = param_def["exponent"]

        # Exponent handling (e.g., 20.5 -> 205 if exponent=1)
        if ptype != "FLOAT" and isinstance(value, (int, float)) and exp != 0:
            value = round(value / (10**exp))

        try:
            # Struct formats per spec 1.4.2: BYTE/WORD/DWORD are unsigned,
            # SHORT_INT/INT/LONG_INT are signed. RAW is spec's STRING type
            # ("a sequence of characters followed by byte 00").
            if ptype == "FLOAT":
                return struct.pack("<f", float(value))
            elif ptype == "BYTE":
                return struct.pack("B", int(value))
            elif ptype == "SHORT_INT":
                return struct.pack("<b", int(value))
            elif ptype == "WORD":
                return struct.pack("<H", int(value))
            elif ptype == "INT":
                return struct.pack("<h", int(value))
            elif ptype == "DWORD":
                return struct.pack("<I", int(value))
            elif ptype == "LONG_INT":
                return struct.pack("<i", int(value))
            elif ptype == "RAW":
                return str(value).encode("utf-8") + b"\x00"
            return None
        except (ValueError, TypeError, OverflowError, struct.error):
            return None

    def _decode(self, data: bytes, param_def: dict) -> Any:
        """Decodes raw bytes into a Python value.

        Args:
            data: The raw binary data received from the device.
            param_def: Dictionary containing 'type' and 'exponent'.

        Returns:
            Any: The decoded value (float, int, or bool).
        """
        ptype = param_def["type"]
        exp = param_def["exponent"]
        try:
            val = None
            if ptype == "FLOAT" and len(data) >= 4:
                val = struct.unpack("<f", data[:4])[0]
                val = round(val, 2)
            elif ptype == "BYTE" and len(data) >= 1:
                val = data[0]
            elif ptype == "SHORT_INT" and len(data) >= 1:
                val = struct.unpack("<b", data[:1])[0]
            elif ptype == "WORD" and len(data) >= 2:
                val = struct.unpack("<H", data[:2])[0]
            elif ptype == "INT" and len(data) >= 2:
                val = struct.unpack("<h", data[:2])[0]
            elif ptype == "DWORD" and len(data) >= 4:
                val = struct.unpack("<I", data[:4])[0]
            elif ptype == "LONG_INT" and len(data) >= 4:
                val = struct.unpack("<i", data[:4])[0]
            elif ptype == "RAW":
                val = data.split(b"\x00", 1)[0].decode("utf-8", errors="replace")

            if val is not None and isinstance(val, (int, float)) and exp != 0:
                val = val * (10**exp)
                val = round(val, 2)
            return val
        except (ValueError, TypeError, OverflowError, struct.error):
            return None

    # --- API ---
    async def get_value(self, slug: str, retries: int = 3) -> Any:
        """Asynchronously fetches a parameter value.

        Wrapper around the synchronous `_sync_get_value` method, executed in a thread.
        It implements a retry mechanism and caching strategy.

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
                val = await asyncio.to_thread(self._sync_get_value, pid, param)
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
            # prefix on the wire -- _sync_get_values_batch can't know where
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
            for i in range(0, len(items), batch_size):
                chunk = items[i : i + batch_size]
                for attempt in range(1, retries + 1):
                    values = await asyncio.to_thread(self._sync_get_values_batch, chunk)
                    for pid, val in values.items():
                        if val is None:
                            continue
                        slug = slug_by_pid.get(pid)
                        if slug:
                            results[slug] = val
                            self._data_cache[slug] = val
                    if len(values) >= len(chunk):
                        break
                    await asyncio.sleep(0.2 * attempt)

            for slug in raw_slugs:
                param = self.params_map[slug]
                pid = param["id"]
                for attempt in range(1, retries + 1):
                    val = await asyncio.to_thread(self._sync_get_value, pid, param)
                    if val is not None:
                        results[slug] = val
                        self._data_cache[slug] = val
                        break
                    await asyncio.sleep(0.2 * attempt)

        return results

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
        encoded = self._encode(value, param)
        if not encoded:
            return False

        user_bytes = (target_user.encode("utf-8") + b"\x00") if target_user else b"\x00"
        pass_bytes = (target_pass.encode("utf-8") + b"\x00") if target_pass else b"\x00"
        full_payload = user_bytes + pass_bytes + b"\x01" + struct.pack("<H", pid) + encoded

        async with self._io_lock:
            for _attempt in range(1, 4):
                if await asyncio.to_thread(self._sync_set_value, pid, full_payload):
                    return True
                await asyncio.sleep(1.0)
        return False

    # --- SYNC WORKERS ---
    def _sync_get_value(self, pid: int, param: dict) -> Any:
        """Blocking worker to fetch a single value."""
        self.session_id = (self.session_id + 1) % 65000
        payload = struct.pack("<HB BH", self.session_id, 1, 1, pid)
        frame = self._build_frame(CMD_READ_VAL, payload)
        result = self._socket_transaction(frame)
        if result is None:
            return None

        func, resp = result
        if func != CMD_READ_RESP or len(resp) < 7:
            logger.debug(
                "Unexpected read response for pid=%s: func=0x%02X len=%d", pid, func, len(resp)
            )
            # Wrong func or a too-short payload isn't a valid answer to
            # this specific request -- on a persistent connection that
            # means the stream is desynced (e.g. a stale/duplicate frame
            # from an earlier request), not just "this pid had no data".
            # Drop the connection so the next transaction starts clean.
            self._close_connection()
            return None

        # Data layout (spec 1.5.3.12): session(2) nblocks(1) nparams(1) pid(2) status(1) value(n)
        resp_pid = struct.unpack("<H", resp[4:6])[0]
        if resp_pid != pid:
            logger.debug("PID mismatch for read: requested %s, device answered %s", pid, resp_pid)
            self._close_connection()
            return None

        return self._decode(resp[7:], param)

    def _sync_set_value(self, pid: int, payload: bytes) -> bool:
        """Blocking worker to write a value."""
        self.session_id = (self.session_id + 1) % 65000
        frame = self._build_frame(CMD_WRITE_FORCE, payload)
        result = self._socket_transaction(frame)
        if result is None:
            return False

        func, resp = result
        if func != CMD_WRITE_RESP:
            logger.warning("Unexpected write response for pid=%s: func=0x%02X", pid, func)
            # Not a valid answer to this write -- same reasoning as the
            # read-side mismatch handling above: drop the connection
            # rather than risk it being desynced for the next transaction.
            self._close_connection()
            return False

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

    def _sync_get_values_batch(self, items: list) -> dict[int, Any]:
        """Blocking worker to fetch several values in a single frame.

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
        frame = self._build_frame(CMD_READ_VAL, header + blocks)

        result = self._socket_transaction(frame, timeout=3.0)
        if result is None:
            return {}

        func, resp = result
        if func != CMD_READ_RESP or len(resp) < 3:
            logger.debug("Unexpected batch read response: func=0x%02X", func)
            self._close_connection()
            return {}

        resp_session = struct.unpack("<H", resp[0:2])[0]
        if resp_session != self.session_id:
            logger.debug("Batch session mismatch: sent %s, got %s", self.session_id, resp_session)
            # Unlike the block-shape check below, a session mismatch means
            # this frame isn't even an answer to our own request -- treat
            # it as a desynced stream, same as the func mismatch above.
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
            values[first_pid] = self._decode(raw, param_def)

        return values

    def _build_frame(self, cmd, payload):
        """Constructs the full binary frame (Header + Body + CRC)."""
        l_val = 5 + len(payload)
        header = struct.pack("<HHHB", l_val, DEST_ID, SOURCE_ID, cmd)
        body = header + payload
        chk = self._crc16(body)
        return b"\x68" + body + struct.pack(">H", chk) + b"\x16"

    def _crc16(self, data: bytes) -> int:
        """Calculates the CRC16 checksum for the frame."""
        crc = 0x0000
        poly = 0x1021
        for b in data:
            crc ^= b << 8
            for _ in range(8):
                if crc & 0x8000:
                    crc = (crc << 1) ^ poly
                else:
                    crc <<= 1
                crc &= 0xFFFF
        return crc

    def _ensure_connection(self) -> socket.socket:
        """Returns the persistent socket, opening a fresh one if needed."""
        if self._sock is None:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(5.0)
            sock.connect((self.ip, self.port))
            self._sock = sock
        return self._sock

    def _close_connection(self) -> None:
        """Tears down the persistent socket, if one is open."""
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None

    def close(self) -> None:
        """Public, explicit teardown of the persistent connection.

        Called on integration unload/reload and after a one-shot
        config_flow connection probe, so a stale socket doesn't linger
        until garbage collection.
        """
        self._close_connection()

    def _socket_transaction(self, frame: bytes, timeout: float = 2.0) -> tuple | None:
        """Executes one request/response transaction over a persistent
        connection, reusing it across calls instead of reconnecting for
        every single transaction (a full TCP handshake per read/write adds
        real overhead when polling every few seconds).

        Any anomaly closes the connection so the *next* transaction starts
        clean rather than risking a desynced stream: a hard socket error
        (dead/reset connection) retries once against a freshly reconnected
        socket within this same call, since that's the case most likely to
        just be a transient drop (e.g. the boiler closed our idle
        connection) rather than the boiler being genuinely unreachable. A
        plain timeout (nothing invalid, just no data in time) closes the
        connection too but does NOT retry inline -- callers already retry
        with backoff (get_value/get_values/set_value), and immediately
        doubling the wait on a possibly-slow-but-alive boiler isn't
        obviously better than letting that existing retry loop handle it.

        Known limitation: _read_response's buffer is local to one call, so
        any bytes received past the first valid frame in the same recv()
        chunk are discarded rather than carried over to the next
        transaction. Not implemented, because it isn't expected to matter
        here: this is a strict request/response protocol (the boiler only
        answers what we just asked), so a recv() spanning more than the
        current response would only happen for a stale/duplicate frame --
        exactly the desync case this method already handles by closing
        and letting the caller retry, not silently misreading data.

        Args:
            frame: The binary frame to send.
            timeout: Socket timeout, in seconds.

        Returns:
            Optional[tuple[int, bytes]]: (func, payload) of the first
            structurally valid response frame, or None on timeout/error.
        """
        for attempt in (1, 2):
            try:
                sock = self._ensure_connection()
            except OSError as e:
                logger.debug("Connect failed (attempt %d/2): %s", attempt, e)
                self.consecutive_failures += 1
                if attempt == 2:
                    return None
                continue

            try:
                sock.settimeout(timeout)
                sock.send(frame)
                result = self._read_response(sock, timeout)
            except OSError as e:
                logger.debug("Socket transaction failed (attempt %d/2): %s", attempt, e)
                self._close_connection()
                self.consecutive_failures += 1
                if attempt == 2:
                    return None
                continue

            if result is None:
                # Timed out without finding a valid frame -- don't trust
                # this connection for the next transaction, but don't
                # retry inline either (see docstring).
                self._close_connection()
                self.consecutive_failures += 1
                return None

            self.consecutive_failures = 0
            self.last_success_ts = time.time()
            return result
        return None

    def _read_response(self, sock: socket.socket, timeout: float) -> tuple | None:
        """Reads from an already-connected socket until a structurally
        valid frame is found or the deadline passes.

        Args:
            sock: The connected socket to read from.
            timeout: Total time budget, in seconds.

        Returns:
            Optional[tuple[int, bytes]]: (func, payload), or None if no
            valid frame arrived in time.

        Raises:
            OSError: If the peer closes the connection (recv() returns no
                data) -- treated as a hard failure by the caller, which
                closes and retries once against a fresh connection, same
                as any other socket error.
        """
        buffer = bytearray()
        deadline = time.time() + timeout
        while time.time() < deadline:
            sock.settimeout(max(deadline - time.time(), 0.01))
            try:
                chunk = sock.recv(1024)
            except TimeoutError:
                break
            if not chunk:
                raise OSError("Connection closed by peer")
            buffer.extend(chunk)

            result = self._extract_valid_frame(buffer)
            if result is not None:
                return result
        return None

    def _extract_valid_frame(self, buffer: bytearray) -> tuple | None:
        """Scans a buffer for the first structurally valid response frame.

        Rejects candidates with an inconsistent length, a bad CRC, a wrong
        stop byte, or a source address other than the boiler, instead of
        trusting the first '0x68 ... 0x16' shape found in the buffer.

        Args:
            buffer: The accumulated bytes read from the socket so far.

        Returns:
            Optional[tuple[int, bytes]]: (func, payload), or None if no
            complete valid frame is present yet (more data may still arrive).
        """
        i = 0
        while i < len(buffer):
            if buffer[i] != START_BYTE:
                i += 1
                continue
            if i + 3 > len(buffer):
                return None  # header incomplete, wait for more data

            l_val = struct.unpack("<H", bytes(buffer[i + 1 : i + 3]))[0]
            if l_val < 5 or l_val > 4096:
                i += 1
                continue

            frame_len = l_val + 6  # 0x68 + L(2) + body(l_val) + CRC(2) + 0x16
            if i + frame_len > len(buffer):
                return None  # frame incomplete, wait for more data

            candidate = bytes(buffer[i : i + frame_len])
            body = candidate[1 : 1 + 2 + l_val]  # CRC covers L + Dest + Src + Func + Data
            received_crc = struct.unpack(">H", candidate[-3:-1])[0]

            if candidate[-1] != STOP_BYTE or self._crc16(body) != received_crc:
                i += 1
                continue

            src = struct.unpack("<H", candidate[5:7])[0]
            if src != DEST_ID:
                i += frame_len
                continue

            return candidate[7], candidate[8:-3]  # (func, payload)

        return None
