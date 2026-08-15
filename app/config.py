"""Loads and persists the Palabra API credentials (key + region) and the
overlay window's appearance settings.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from dotenv import dotenv_values, set_key

CONFIG_DIR = Path.home() / ".sts_bridge"
ENV_PATH = CONFIG_DIR / ".env"
OVERLAY_SETTINGS_PATH = CONFIG_DIR / "overlay_settings.json"
SAVED_VOICES_PATH = CONFIG_DIR / "saved_voices.json"
ERROR_LOG_PATH = CONFIG_DIR / "errors.log"
APP_SETTINGS_PATH = CONFIG_DIR / "app_settings.json"
BALANCE_PATH = CONFIG_DIR / "balance.json"
SESSION_HISTORY_PATH = CONFIG_DIR / "session_history.json"
MAX_SESSION_HISTORY_ENTRIES = 200  # avoid unbounded growth over months of use

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


def log_error(message: str) -> None:
    """Appends a timestamped error line to ~/.sts_bridge/errors.log, so a
    problem that scrolled out of the in-app log (or happened in a past
    session) can still be found afterward. Never raises -- a failure to
    write the log must not itself crash the app or interrupt error
    reporting to the GUI, which is the primary channel this supplements.
    """
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().isoformat(sep=" ", timespec="seconds")
        with open(ERROR_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(f"[{timestamp}] {message}\n")
    except OSError:
        pass


_APP_SETTINGS_TYPES: dict[str, type | tuple[type, ...]] = {
    "mic_device_name": str,
    "output_device_name": str,
    "mic_gain": (int, float),
    "mic_gate": (int, float),
    "subtitles_only": bool,
    "source_lang": str,
    "target_lang": str,
    "voice_kind": str,
    "voice_id": (str, type(None)),
    "voice_custom_text": str,
    "log_filter": str,
    "show_lang_tags": bool,
}


def load_app_settings() -> dict[str, Any]:
    """Reads saved main-window settings (device/language/voice/log filter
    preferences) from disk, or {} if none/corrupt yet."""
    if not APP_SETTINGS_PATH.exists():
        return {}
    try:
        data = json.loads(APP_SETTINGS_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {
        k: v
        for k, v in data.items()
        if k not in _APP_SETTINGS_TYPES or isinstance(v, _APP_SETTINGS_TYPES[k])
    }


def save_app_settings(settings: dict[str, Any]) -> None:
    """Writes the main-window settings to disk as JSON."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    APP_SETTINGS_PATH.write_text(json.dumps(settings, indent=2), encoding="utf-8")


def load_balance() -> float | None:
    """Reads the user's manually-entered Palabra balance estimate (USD), or
    None if never set. This is a rough, client-side ESTIMATE -- Palabra
    doesn't expose real balance via API (see the Settings dialog's "Otwórz
    panel Palabra" link) -- kept in sync by subtracting each session's
    estimated cost as it ends. Can drift from the real balance (e.g. if the
    same API key is also used elsewhere) and should be periodically
    re-entered from the real dashboard value.
    """
    if not BALANCE_PATH.exists():
        return None
    try:
        data = json.loads(BALANCE_PATH.read_text(encoding="utf-8"))
        return float(data["balance_usd"])
    except (json.JSONDecodeError, OSError, KeyError, ValueError, TypeError):
        return None


def save_balance(balance_usd: float) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    BALANCE_PATH.write_text(json.dumps({"balance_usd": balance_usd}), encoding="utf-8")


def load_session_history() -> list[dict[str, Any]]:
    """Reads past completed sessions (see append_session_history), most
    recent last. Palabra doesn't expose per-session usage history the app
    can read, so this is the only record of past sessions/spend available
    from within the app itself."""
    if not SESSION_HISTORY_PATH.exists():
        return []
    try:
        data = json.loads(SESSION_HISTORY_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    if not isinstance(data, list):
        return []
    return [
        e
        for e in data
        if isinstance(e, dict)
        and isinstance(e.get("started_at"), str)
        and isinstance(e.get("duration_seconds"), (int, float))
        and isinstance(e.get("cost_usd"), (int, float))
    ]


def append_session_history(started_at: str, duration_seconds: float, cost_usd: float) -> None:
    """Appends one completed session, trimmed to the most recent
    MAX_SESSION_HISTORY_ENTRIES."""
    history = load_session_history()
    history.append({"started_at": started_at, "duration_seconds": duration_seconds, "cost_usd": cost_usd})
    history = history[-MAX_SESSION_HISTORY_ENTRIES:]
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    SESSION_HISTORY_PATH.write_text(json.dumps(history, indent=2), encoding="utf-8")


def clear_session_history() -> None:
    if SESSION_HISTORY_PATH.exists():
        SESSION_HISTORY_PATH.unlink()


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
