"""Loads and persists the Palabra API credentials (key + region) and the
overlay window's appearance settings.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dotenv import dotenv_values, set_key

CONFIG_DIR = Path.home() / ".sts_bridge"
ENV_PATH = CONFIG_DIR / ".env"
OVERLAY_SETTINGS_PATH = CONFIG_DIR / "overlay_settings.json"
SAVED_VOICES_PATH = CONFIG_DIR / "saved_voices.json"

DEFAULT_REGION = "eu"


@dataclass
class Credentials:
    api_key: str | None
    region: str


def load_credentials() -> Credentials:
    """Reads PALABRA_API_KEY / PALABRA_REGION from ~/.sts_bridge/.env (if present)."""
    values = dotenv_values(ENV_PATH) if ENV_PATH.exists() else {}
    api_key = values.get("PALABRA_API_KEY") or None
    region = values.get("PALABRA_REGION") or DEFAULT_REGION
    return Credentials(api_key=api_key, region=region)


def save_credentials(api_key: str, region: str = DEFAULT_REGION) -> None:
    """Writes the API key/region to ~/.sts_bridge/.env, creating the file if needed."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    if not ENV_PATH.exists():
        ENV_PATH.touch()
    set_key(str(ENV_PATH), "PALABRA_API_KEY", api_key)
    set_key(str(ENV_PATH), "PALABRA_REGION", region)


_OVERLAY_SETTINGS_TYPES: dict[str, type | tuple[type, ...]] = {
    "filter_mode": str,
    "font_family": str,
    "font_size": (int, float),
    "font_color": str,
    "bg_color": str,
    "opacity_percent": (int, float),
    "shadow_enabled": bool,
    "always_on_top": bool,
    "pos_x": (int, float),
    "pos_y": (int, float),
    "width": (int, float),
    "height": (int, float),
}


def load_overlay_settings() -> dict[str, Any]:
    """Reads saved overlay appearance settings from disk, or {} if none/corrupt yet."""
    if not OVERLAY_SETTINGS_PATH.exists():
        return {}
    try:
        data = json.loads(OVERLAY_SETTINGS_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    if not isinstance(data, dict):
        return {}
    # A field with the wrong type (e.g. a hand-edited file) would otherwise
    # crash OverlayWindow.__init__ (e.g. QFont(family, "not-a-number")) --
    # drop just that field so the app falls back to its default instead.
    return {
        k: v
        for k, v in data.items()
        if k not in _OVERLAY_SETTINGS_TYPES or isinstance(v, _OVERLAY_SETTINGS_TYPES[k])
    }


def save_overlay_settings(settings: dict[str, Any]) -> None:
    """Writes the overlay appearance settings to disk as JSON."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    OVERLAY_SETTINGS_PATH.write_text(json.dumps(settings, indent=2), encoding="utf-8")


# Shipped with the app so a fresh install already has a useful starting
# list instead of an empty one -- only used the very first time (before the
# user's own ~/.sts_bridge/saved_voices.json exists); any add/remove from
# then on is stored in that local file like normal, independently per machine.
DEFAULT_SAVED_VOICES: list[dict[str, str]] = [
    {"name": "EngFem1", "voice_id": "36B6DAF6-4792-4106-A0AD-0069F582E7F8"},
    {"name": "EngMale1", "voice_id": "7fa1dafd-84e4-4e30-8e5b-7295edbc152e"},
    {"name": "EngMale2", "voice_id": "671e3dd0-cb38-448d-99f2-63300e9c5a67"},
    {"name": "EngMale3", "voice_id": "f1d62423-085e-4694-91cd-4d04085a2fc5"},
    {"name": "EngMale4", "voice_id": "fb4529b8-dd11-48f2-a10b-50b1e349ef4c"},
    {"name": "PolMale", "voice_id": "0c3adf42-f1f6-430b-a18c-7d83e141abd6"},
    {"name": "PolFemale", "voice_id": "d319b3e8-ab60-4146-af21-d66f5faa5a24"},
]


def load_saved_voices() -> list[dict[str, str]]:
    """Reads the user's named voice_id presets (Palabra has no API to list
    voices, so IDs are copied by hand from app.palabra.ai/voices and saved
    here under a friendly name so they don't need re-pasting every time)."""
    if not SAVED_VOICES_PATH.exists():
        return [dict(v) for v in DEFAULT_SAVED_VOICES]
    try:
        data = json.loads(SAVED_VOICES_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    if not isinstance(data, list):
        return []
    # Every entry must have "name"/"voice_id" strings -- this list is read on
    # every app startup (MainWindow.__init__ builds the voice picker from it),
    # so a malformed entry here must be skipped rather than raising, or the
    # whole app would fail to launch until the file is deleted by hand.
    return [
        v
        for v in data
        if isinstance(v, dict) and isinstance(v.get("name"), str) and isinstance(v.get("voice_id"), str)
    ]


def save_saved_voices(voices: list[dict[str, str]]) -> None:
    """Writes the named voice_id presets to disk as JSON."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    SAVED_VOICES_PATH.write_text(json.dumps(voices, indent=2), encoding="utf-8")
