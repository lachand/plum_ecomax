"""Pure wire-protocol codec for Plum ecoNET modules (no I/O).

Frame layout (RS-485 over TCP): ``0x68 | L(2) | dest(2) | src(2) | func | data |
CRC16(2) | 0x16``, with the CRC covering L through data. This module builds
and validates frames and converts values to and from their wire encodings
(spec 1.4.2); the connection, retries and I/O ordering live in plum_device.py.

Attributes:
    DEST_ID (int): The default destination address for the boiler (1).
    SOURCE_ID (int): The source address for the integration (100).
    CMD_READ_VAL (int): Command ID to read a parameter (0x43).
    CMD_WRITE_FORCE (int): Command ID to write a parameter (0x29).
"""

import struct
from typing import Any, NamedTuple, NotRequired, TypedDict

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


class ParamDef(TypedDict):
    """One entry of the bundled parameter map (device_map_ecomax360i.json).

    Typing only: the JSON is loaded as-is, nothing is converted at runtime.
    """

    id: int
    type: str  # BYTE / SHORT_INT / WORD / INT / DWORD / LONG_INT / FLOAT / RAW
    exponent: int
    unit: str
    name_orig: str
    # Plausibility bounds / rate limit, only on the parameters that need them
    # (see PlumDataUpdateCoordinator._validate_value) and enum labels.
    min: NotRequired[int | float]
    max: NotRequired[int | float]
    max_delta: NotRequired[int | float]
    enum: NotRequired[list]


class Frame(NamedTuple):
    """A structurally valid response frame: command byte and its data field."""

    func: int
    payload: bytes


def build_frame(cmd, payload):
    """Constructs the full binary frame (Header + Body + CRC)."""
    l_val = 5 + len(payload)
    header = struct.pack("<HHHB", l_val, DEST_ID, SOURCE_ID, cmd)
    body = header + payload
    chk = crc16(body)
    return b"\x68" + body + struct.pack(">H", chk) + b"\x16"


def crc16(data: bytes) -> int:
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


def extract_valid_frame(buffer: bytearray) -> Frame | None:
    """Scans a buffer for the first structurally valid response frame.

    Rejects candidates with an inconsistent length, a bad CRC, a wrong
    stop byte, or a source address other than the boiler, instead of
    trusting the first '0x68 ... 0x16' shape found in the buffer.

    Args:
        buffer: The accumulated bytes read from the connection so far.

    Returns:
        Frame | None: the frame, or None if no complete valid frame is
        present yet (more data may still arrive).
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

        if candidate[-1] != STOP_BYTE or crc16(body) != received_crc:
            i += 1
            continue

        src = struct.unpack("<H", candidate[5:7])[0]
        if src != DEST_ID:
            i += frame_len
            continue

        return Frame(candidate[7], candidate[8:-3])

    return None


def encode_value(value: Any, param_def: ParamDef) -> bytes | None:
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


def decode_value(data: bytes, param_def: ParamDef) -> Any:
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
