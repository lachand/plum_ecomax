"""Unit tests for protocol.py: the pure wire codec (frames, CRC, value
encodings). Ground-truth bytes come from Plum's "Standard Transmission
Protocols ed.15" PDF (spec 1.5.3.12 p.25, 1.4.2); they are shared with the
transport tests through wire_fixtures.py.
"""

from __future__ import annotations

import struct

import pytest

from custom_components.plum_ecomax.protocol import (
    CMD_READ_RESP,
    CMD_WRITE_RESP,
    DEST_ID,
    SOURCE_ID,
    Frame,
    build_frame,
    crc16,
    decode_value,
    encode_value,
    extract_valid_frame,
    pop_valid_frame,
)
from tests.unit.wire_fixtures import SPEC_READ_RESPONSE, SPEC_WRITE_OK_RESPONSE


class TestExtractValidFrame:
    def test_parses_spec_worked_example(self):
        func, payload = extract_valid_frame(bytearray(SPEC_READ_RESPONSE))

        assert func == CMD_READ_RESP
        session, n_blocks = struct.unpack("<HB", payload[0:3])
        assert session == 59
        assert n_blocks == 2
        # Block 0: pid 16, status 5, value 00 E0 2B 46 (value starts right
        # after the 1-byte status -- this is the offset that was wrong).
        resp_pid = struct.unpack("<H", payload[4:6])[0]
        assert resp_pid == 16
        assert payload[6] == 0x05  # status byte, must NOT be mistaken for value
        assert payload[7:11] == bytes.fromhex("00E02B46")

    def test_skips_noise_prefix_with_coincidental_start_byte(self):
        noisy = bytearray(b"\xab\xcd\x68\xff") + bytearray(SPEC_READ_RESPONSE)
        result = extract_valid_frame(noisy)
        assert result == extract_valid_frame(bytearray(SPEC_READ_RESPONSE))

    def test_rejects_corrupted_crc(self):
        corrupted = bytearray(SPEC_READ_RESPONSE)
        corrupted[-3] ^= 0xFF  # flip a CRC byte
        assert extract_valid_frame(corrupted) is None

    def test_rejects_wrong_stop_byte(self):
        corrupted = bytearray(SPEC_READ_RESPONSE)
        corrupted[-1] = 0x00
        assert extract_valid_frame(corrupted) is None

    def test_incomplete_frame_returns_none_without_crashing(self):
        truncated = bytearray(SPEC_READ_RESPONSE[:10])
        assert extract_valid_frame(truncated) is None

    def test_rejects_frame_from_wrong_source(self):
        wrong_src = bytearray(SPEC_READ_RESPONSE)
        # src field is at offset 5:7; DEST_ID is 1, so use 2 instead.
        wrong_src[5:7] = struct.pack("<H", 2)
        # Recompute CRC so only the source-address check can reject it.
        body = bytes(wrong_src[1 : 1 + 2 + struct.unpack("<H", bytes(wrong_src[1:3]))[0]])
        new_crc = crc16(body)
        wrong_src[-3:-1] = struct.pack(">H", new_crc)
        assert extract_valid_frame(wrong_src) is None

    def test_write_ok_response_parses_as_single_result_byte(self):
        func, payload = extract_valid_frame(bytearray(SPEC_WRITE_OK_RESPONSE))
        assert func == CMD_WRITE_RESP
        assert payload == b"\xe5"


# ---------------------------------------------------------------------------
# _encode / _decode: signed vs. unsigned per spec 1.4.2
# ---------------------------------------------------------------------------


class TestEncodeDecodeTypes:
    @pytest.mark.parametrize(
        "ptype,value,expected_hex",
        [
            ("BYTE", 255, "ff"),
            ("SHORT_INT", -5, "fb"),
            ("WORD", 40000, "409c"),  # > INT16 max, must not raise
            ("INT", -100, "9cff"),
            ("DWORD", 3_000_000_000, "005ed0b2"),  # > INT32 max, must not raise
            ("LONG_INT", -2000000000, "006cca88"),
            ("FLOAT", 20.5, None),  # checked separately (float roundtrip)
        ],
    )
    def test_encode_matches_wire_format(self, ptype, value, expected_hex):
        raw = encode_value(value, {"type": ptype, "exponent": 0})
        if expected_hex is not None:
            assert raw.hex() == expected_hex

    def test_encode_float_roundtrips_through_decode(self):
        raw = encode_value(20.5, {"type": "FLOAT", "exponent": 0})
        assert decode_value(raw, {"type": "FLOAT", "exponent": 0}) == 20.5

    def test_decode_short_int_is_signed(self):
        # 0xFB = 251 unsigned, -5 signed -- must decode as -5.
        assert decode_value(b"\xfb", {"type": "SHORT_INT", "exponent": 0}) == -5

    def test_decode_word_is_unsigned(self):
        raw = struct.pack("<H", 40000)
        assert decode_value(raw, {"type": "WORD", "exponent": 0}) == 40000

    def test_decode_dword_is_unsigned(self):
        raw = struct.pack("<I", 3_000_000_000)
        assert decode_value(raw, {"type": "DWORD", "exponent": 0}) == 3_000_000_000

    def test_encode_decode_round_trip_with_exponent(self):
        param_def = {"type": "SHORT_INT", "exponent": 1}
        raw = encode_value(20.5, param_def)  # 20.5 / 10**1 = 2 (rounded, int-truncated)
        assert decode_value(raw, param_def) == 20.0  # 2 * 10**1

    def test_encode_unknown_type_returns_none(self):
        assert encode_value(1, {"type": "NOPE", "exponent": 0}) is None

    def test_decode_too_short_returns_none(self):
        assert decode_value(b"", {"type": "FLOAT", "exponent": 0}) is None


class TestRawStringType:
    """RAW is the spec's STRING type (1.4.2): "a sequence of characters
    followed by byte 00". Confirmed against real hardware -- the 'uid'
    parameter's raw wire bytes decode to the boiler's actual serial number
    once this type is handled (previously _decode had no RAW branch at
    all, so every RAW-typed parameter silently decoded as None forever,
    indistinguishable from a genuinely absent parameter).
    """

    def test_decode_stops_at_null_terminator(self):
        raw = b"1M86DIP6H1GQE1H6P3KGIH5\x00\x00\x00"  # trailing padding after the string
        assert decode_value(raw, {"type": "RAW", "exponent": 0}) == "1M86DIP6H1GQE1H6P3KGIH5"

    def test_decode_without_trailing_null_still_works(self):
        assert decode_value(b"circuit1", {"type": "RAW", "exponent": 0}) == "circuit1"

    def test_encode_appends_null_terminator(self):
        raw = encode_value("circuit1", {"type": "RAW", "exponent": 0})
        assert raw == b"circuit1\x00"

    def test_exponent_is_not_applied_to_strings(self):
        # The exponent-scaling step in _decode/_encode guards on
        # isinstance(val, (int, float)); a non-zero exponent on a RAW
        # param must not raise or corrupt the string.
        assert decode_value(b"abc\x00", {"type": "RAW", "exponent": 2}) == "abc"


class TestFrameType:
    def test_extracted_frame_is_a_named_frame_and_still_unpacks_as_a_tuple(self):

        frame = extract_valid_frame(bytearray(SPEC_READ_RESPONSE))

        assert isinstance(frame, Frame)
        func, payload = frame  # existing call sites unpack it this way
        assert (func, payload) == (frame.func, frame.payload)
        assert func == CMD_READ_RESP


class TestBuildFrame:
    def test_frame_is_delimited_and_carries_a_valid_crc_over_length_to_data(self):
        frame = build_frame(0x43, b"\x01\x02\x03")

        assert frame[0] == 0x68 and frame[-1] == 0x16
        body = frame[1:-3]  # L(2) + dest + src + func + data
        assert struct.unpack(">H", frame[-3:-1])[0] == crc16(body)
        assert struct.unpack("<H", frame[1:3])[0] == 5 + 3  # l_val = 5 + len(payload)
        assert frame[5:7] == struct.pack("<H", SOURCE_ID)
        assert frame[3:5] == struct.pack("<H", DEST_ID)


class TestPopValidFrame:
    def test_consumes_exactly_one_frame_and_leaves_the_rest(self):
        buffer = bytearray(SPEC_READ_RESPONSE + SPEC_WRITE_OK_RESPONSE)

        first = pop_valid_frame(buffer)
        assert first.func == CMD_READ_RESP
        assert bytes(buffer) == SPEC_WRITE_OK_RESPONSE

        second = pop_valid_frame(buffer)
        assert second.func == CMD_WRITE_RESP
        assert not buffer
        assert pop_valid_frame(buffer) is None

    def test_drops_noise_before_the_frame(self):
        buffer = bytearray(b"\x00\x01\x68\x02" + SPEC_WRITE_OK_RESPONSE)

        assert pop_valid_frame(buffer).func == CMD_WRITE_RESP
        assert not buffer

    def test_keeps_an_incomplete_tail_for_the_next_read(self):
        partial = SPEC_READ_RESPONSE[:10]
        buffer = bytearray(SPEC_WRITE_OK_RESPONSE + partial)

        assert pop_valid_frame(buffer).func == CMD_WRITE_RESP
        assert pop_valid_frame(buffer) is None
        assert bytes(buffer) == partial  # untouched, waiting for more bytes

    def test_extract_valid_frame_does_not_consume(self):
        buffer = bytearray(SPEC_READ_RESPONSE)
        assert extract_valid_frame(buffer) is not None
        assert bytes(buffer) == SPEC_READ_RESPONSE
