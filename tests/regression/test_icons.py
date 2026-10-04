"""Icons live in icons.json (HA's icon translations), keyed like the entities'
translation keys. const.py's tables still carry each icon next to its entity
definition; this keeps the two in step, and checks no entity sets an icon in
code any more.
"""

from __future__ import annotations

import json
from pathlib import Path

from custom_components.plum_ecomax import const

COMPONENT = Path(__file__).resolve().parents[2] / "custom_components" / "plum_ecomax"
ICONS = json.loads((COMPONENT / "icons.json").read_text(encoding="utf-8"))["entity"]
STRINGS = json.loads((COMPONENT / "strings.json").read_text(encoding="utf-8"))["entity"]


def test_every_sensor_and_number_icon_in_const_is_in_icons_json():
    for slug, cfg in const.SENSOR_TYPES.items():
        if cfg[1]:
            assert ICONS["sensor"][slug]["default"] == cfg[1], slug
    for slug, cfg in const.NUMBER_TYPES.items():
        if cfg[3]:
            assert ICONS["number"][slug]["default"] == cfg[3], slug
    for key, cfg in const.SOLAR_DUMP_NUMBERS.items():
        if cfg[5]:
            assert ICONS["number"][f"solar_dump_{key}"]["default"] == cfg[5], key


def test_every_icon_key_is_a_real_entity_translation_key():
    for platform, icons in ICONS.items():
        unknown = [key for key in icons if key not in STRINGS.get(platform, {})]
        assert not unknown, (platform, unknown)


def test_the_manual_mode_switch_icon_follows_its_state():
    icon = ICONS["switch"]["operatingmode"]
    assert icon["default"] == "mdi:cog-play"
    assert icon["state"]["on"] == "mdi:hand-back-right"


def test_no_entity_sets_an_icon_in_code():
    for path in COMPONENT.glob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert "_attr_icon" not in text and "def icon(" not in text, path.name
