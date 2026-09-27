#!/usr/bin/env python3
"""Static contract test for icon slot registry, preferences and templates."""
import re
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from bravoric_stt_clipboard.config import ICON_SLOT_KEYS

prefs = (ROOT / "gnome-extension/bravoric-indicator@local/prefs.js").read_text()
rows = set(re.findall(r"slot: '([^']+)'", prefs))
assert rows == set(ICON_SLOT_KEYS), (set(ICON_SLOT_KEYS) - rows, rows - set(ICON_SLOT_KEYS))
for filename in ("config/config.example.toml", "config/config.example.it.toml"):
    with (ROOT / filename).open("rb") as stream:
        icons = tomllib.load(stream)["icons"]
    assert set(icons) == set(ICON_SLOT_KEYS), (filename, set(ICON_SLOT_KEYS) - set(icons), set(icons) - set(ICON_SLOT_KEYS))
    assert all(value == "" for value in icons.values()), filename
print(f"PASS: {len(ICON_SLOT_KEYS)} registry slots match prefs rows and both example TOMLs")
