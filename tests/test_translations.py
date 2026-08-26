"""strings.json is the canonical source; every translation must mirror its keys."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

_BASE = Path(__file__).parent.parent / "custom_components" / "hue_entertainment"
_STRINGS = json.loads((_BASE / "strings.json").read_text())


def _keys(obj, prefix=""):
    if isinstance(obj, dict):
        return {k for key, val in obj.items() for k in _keys(val, f"{prefix}{key}.")}
    return {prefix.rstrip(".")}


def test_en_translation_is_identical_to_strings():
    en = json.loads((_BASE / "translations" / "en.json").read_text())
    assert en == _STRINGS


@pytest.mark.parametrize(
    "path", sorted((_BASE / "translations").glob("*.json")), ids=lambda p: p.name
)
def test_translation_has_exactly_the_keys_of_strings(path: Path):
    translation = json.loads(path.read_text())
    assert _keys(translation) == _keys(_STRINGS), f"{path.name} keys differ from strings.json"
