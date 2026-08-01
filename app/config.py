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


def load_overlay_settings() -> dict[str, Any]:
    """Reads saved overlay appearance settings from disk, or {} if none/corrupt yet."""
    if not OVERLAY_SETTINGS_PATH.exists():
        return {}
    try:
        data = json.loads(OVERLAY_SETTINGS_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def save_overlay_settings(settings: dict[str, Any]) -> None:
    """Writes the overlay appearance settings to disk as JSON."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    OVERLAY_SETTINGS_PATH.write_text(json.dumps(settings, indent=2), encoding="utf-8")


def load_saved_voices() -> list[dict[str, str]]:
    """Reads the user's named voice_id presets (Palabra has no API to list
    voices, so IDs are copied by hand from app.palabra.ai/voices and saved
    here under a friendly name so they don't need re-pasting every time)."""
    if not SAVED_VOICES_PATH.exists():
        return []
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
