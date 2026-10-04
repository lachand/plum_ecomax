"""Entity names must not repeat the name of the device they sit on.

With _attr_has_entity_name = True Home Assistant shows "<device name> <entity
name>" (and builds the entity_id the same way), so an entity on the device
"Circuit 1" called "Circuit 1 Base Temperature" ends up as "Circuit 1 Circuit 1
Base Temperature". Which device an entity lands on follows the same rules as
each platform's device_info (number.py / sensor.py / switch.py / select.py):
circuit and mixer slugs -> "Circuit N", hdw*/circulation* numbers, the three
HDW switches and the DHW mode select -> "DHW"; everything else -> the boiler.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

COMPONENT = Path(__file__).resolve().parents[2] / "custom_components" / "plum_ecomax"

# Same set as switch.py's HDW_SWITCHES.
HDW_SWITCHES = {"hdwstartoneloading", "hdwpumpforce", "hdwstartlegion"}


def _names(path: Path):
    entity = json.loads(path.read_text(encoding="utf-8"))["entity"]
    for platform, entities in entity.items():
        for key, value in entities.items():
            if "name" in value:
                yield platform, key, value["name"]


def _on_circuit_device(platform: str, key: str) -> bool:
    if platform == "number":
        return re.match(r"^circuit\d", key) is not None
    if platform == "sensor":
        return re.search(r"(circuit|mixer)\d", key) is not None
    return False


def _on_dhw_device(platform: str, key: str) -> bool:
    return (
        (platform == "number" and key.startswith(("hdw", "circulation")))
        or (platform == "switch" and key in HDW_SWITCHES)
        or platform == "select"
    )


def test_no_entity_on_a_circuit_device_repeats_the_circuit_name():
    for lang in (COMPONENT / "strings.json", COMPONENT / "translations" / "fr.json"):
        for platform, key, name in _names(lang):
            if _on_circuit_device(platform, key):
                assert not re.search(r"\bCircuit \d+\b", name), (lang.name, platform, key, name)


def test_no_entity_on_the_dhw_device_repeats_the_dhw_name():
    en = COMPONENT / "strings.json"
    fr = COMPONENT / "translations" / "fr.json"
    for platform, key, name in _names(en):
        if _on_dhw_device(platform, key):
            assert not re.match(r"(?i)^dhw\b", name), (en.name, platform, key, name)
    for platform, key, name in _names(fr):
        if _on_dhw_device(platform, key):
            assert not re.search(r"\bECS\b", name), (fr.name, platform, key, name)
