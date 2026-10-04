"""The brand images Home Assistant shows for the integration (local brand folder):
icon square, logo with a shorter side of 128-256 px, @2x exactly double, dark logo
the same size as the light one. Sizes are read from the PNG headers (no Pillow)."""

from __future__ import annotations

import struct
from pathlib import Path

BRAND = Path(__file__).resolve().parents[2] / "custom_components" / "plum_ecomax" / "brand"


def _size(name: str) -> tuple[int, int]:
    data = (BRAND / name).read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n", f"{name} is not a PNG"
    return struct.unpack(">II", data[16:24])


def test_icon_is_square_with_a_2x_version():
    width, height = _size("icon.png")
    assert width == height == 256
    assert _size("icon@2x.png") == (512, 512)


def test_logo_has_a_valid_shorter_side_and_an_exact_2x():
    width, height = _size("logo.png")
    assert 128 <= min(width, height) <= 256
    assert _size("logo@2x.png") == (width * 2, height * 2)


def test_dark_logo_matches_the_light_logo_size():
    assert _size("dark_logo.png") == _size("logo.png")
    assert _size("dark_logo@2x.png") == _size("logo@2x.png")
