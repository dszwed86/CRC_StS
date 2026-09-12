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
GLOSSARY_PATH = CONFIG_DIR / "glossary.json"

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
    "mic_channel": (int, type(None)),
    "output_device_name": str,
    "mic_gain": (int, float),
    "file_gain": (int, float),
    "mic_muted": bool,
    "mic_gate": (int, float),
    "subtitles_only": bool,
    "source_lang": str,
    "target_lang": str,
    "voice_kind": str,
    "voice_id": (str, type(None)),
    "voice_custom_text": str,
    "log_filter": str,
    "language": str,
    "window_width": (int, float),
    "window_height": (int, float),
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

# Voice preset MainWindow selects by default on a fresh install (no saved
# voice_kind yet -- see _rebuild_voice_combo). Measured via a real A/B test
# against the live API: EngMale2 speaks ~10% slower/less compressed than the
# "auto" default (which resolves to the same server voice as the
# "default_low" preset) at no extra latency -- a more natural-sounding male
# default with zero cost. (EngFem1 measured even better, ~19% slower, but
# the user wants a male voice by default.) Only affects new installs;
# anyone who already saved a voice choice keeps it.
DEFAULT_VOICE_ID = next(v["voice_id"] for v in DEFAULT_SAVED_VOICES if v["name"] == "EngMale2")


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


def _glossary_key(source_lang: str, target_lang: str, kind: str = "custom") -> str:
    # A Palabra "translation"-type glossary is tied to one specific
    # (source_lang, target_lang) pair (see app/glossary.py) -- keyed the
    # same way here so each language pair the user actually translates
    # keeps its own independent word list and remote glossary_id. kind
    # distinguishes the user's own term glossary ("custom") from the
    # profanity-filter word list ("banned") -- same mechanism, same file
    # format, entirely independent lists/remote glossaries so toggling one
    # never touches the other.
    suffix = "" if kind == "custom" else f":{kind}"
    return f"{source_lang}->{target_lang}{suffix}"


def glossary_txt_path(source_lang: str, target_lang: str, kind: str = "custom") -> Path:
    """The human-editable word-pair file for one language pair/kind -- can be
    opened and edited directly in any text editor (one "source => target"
    pair per line, blank lines and lines starting with # ignored), or via the
    app's own Glosariusz/Filtr przekleństw dialogs, which read and write this
    exact file. This is the single source of truth for pairs; glossary_id and
    synced_pairs (see load_glossary_entries) are app-internal bookkeeping and
    stay in GLOSSARY_PATH instead, since hand-editing those would only risk
    desyncing the local state from what's actually live on Palabra."""
    suffix = "" if kind == "custom" else f"_{kind}"
    return CONFIG_DIR / f"glossary_{source_lang}-{target_lang}{suffix}.txt"


def _parse_pairs(raw_pairs: Any) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    if isinstance(raw_pairs, list):
        for p in raw_pairs:
            if isinstance(p, list) and len(p) == 2 and all(isinstance(x, str) for x in p):
                pairs.append((p[0], p[1]))
    return pairs


def _parse_txt_pairs(text: str) -> list[tuple[str, str]]:
    """Parses the human-editable "source => target" txt format -- one pair
    per line, "=>" as the separator (chosen over "=" or ":" since neither of
    those can appear in a real word/phrase as unambiguously). Blank lines and
    lines starting with # (comments) are skipped; a line without "=>" is
    skipped rather than raising, since this file is meant to be hand-edited
    and a typo shouldn't block loading every other valid line."""
    pairs: list[tuple[str, str]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=>" not in line:
            continue
        src, _, tgt = line.partition("=>")
        src, tgt = src.strip(), tgt.strip()
        if src and tgt:
            pairs.append((src, tgt))
    return pairs


def _format_txt_pairs(pairs: list[tuple[str, str]]) -> str:
    return "".join(f"{src} => {tgt}\n" for src, tgt in pairs)


def _read_glossary_txt(path: Path) -> list[tuple[str, str]]:
    if not path.exists():
        return []
    try:
        return _parse_txt_pairs(path.read_text(encoding="utf-8"))
    except OSError:
        return []


def _write_glossary_txt(path: Path, pairs: list[tuple[str, str]]) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    path.write_text(_format_txt_pairs(pairs), encoding="utf-8")


# Sentinel for save_glossary_entries' synced_pairs param: distinguishes "leave
# whatever was last recorded as synced untouched" (the default -- used by plain
# local edits) from "explicitly set it" (None or a real list -- used after an
# actual sync attempt), since None alone already means "synced list is empty".
_UNSET = object()


# Shipped with the app (not user data) so a fresh install's profanity filter
# has a sensible starting point instead of an empty list -- seeded into the
# real .txt file the first time it's loaded for a Polish-source pair (see
# load_glossary_entries below), then left entirely alone: editing or deleting
# entries afterward is never overwritten back to this default. Common
# vulgarities plus their most frequent inflected forms, both lowercase and
# Capitalized (sentence-initial) forms -- not exhaustive, just a starting
# point; the .txt file is freely editable afterward (see glossary_txt_path).
DEFAULT_BANNED_WORDS_PL: list[tuple[str, str]] = [
    (w, "*****")
    for w in (
        "kurwa", "Kurwa", "kurwy", "kurwo", "kurwę", "kurwie", "kurwami", "kurwach",
        "chuj", "Chuj", "chuja", "chuju", "chujowi", "chujem", "chuje", "chujów",
        "chujowy", "chujowa", "chujowe",
        "huj", "Huj", "huja", "hujem",
        "pierdolić", "pierdole", "pierdolę", "pierdolisz", "pierdoli", "pierdolimy",
        "pierdolicie", "pierdolą", "pierdolony", "pierdolona", "pierdolone",
        "spierdalaj", "Spierdalaj", "spierdalać", "wypierdalaj", "Wypierdalaj",
        "popierdolone", "popierdolony", "popierdolona",
        "jebać", "jebię", "jebę", "jebiesz", "jebie", "jebią", "jebany", "jebana", "jebane",
        "pojebany", "pojebana", "pojebane", "zjebać", "zjebany", "zjebana", "zjebane", "wyjebane",
        "pizda", "Pizda", "pizdę", "pizdy", "pizdo",
        "cipa", "Cipa", "cipy", "cipę",
        "dziwka", "Dziwka", "dziwki",
        "skurwysyn", "Skurwysyn", "skurwysyny", "skurwysynu",
        "gówno", "Gówno", "gówna", "gównem",
        "gnojek", "gnoju", "Gnoju",
        "sukinsyn", "Sukinsyn",
    )
]


def load_glossary_entries(
    source_lang: str, target_lang: str, kind: str = "custom"
) -> tuple[list[tuple[str, str]], str | None, list[tuple[str, str]]]:
    """Reads the word-pair list (from its .txt file -- see
    glossary_txt_path), last-synced glossary_id, and the pair list that was
    actually last pushed to Palabra (synced_pairs, still kept in
    GLOSSARY_PATH) for one language pair/kind -- the caller (GlossaryDialog)
    compares the pairs against synced_pairs to know whether it's showing
    unsaved changes, instead of always assuming "just saved" on open.
    Returns ([], None, []) if nothing saved yet (except kind="banned" with a
    Polish source -- see DEFAULT_BANNED_WORDS_PL above).

    One-time migration: an existing install's "custom" pairs were
    previously stored inline in GLOSSARY_PATH's JSON instead of a .txt file
    -- if the .txt file doesn't exist yet but that old JSON field does,
    those pairs are exported to the .txt file (and kept in the JSON too,
    harmlessly ignored from here on) so upgrading doesn't silently lose a
    glossary someone already built.
    """
    txt_path = glossary_txt_path(source_lang, target_lang, kind)
    glossary_id: str | None = None
    synced_pairs: list[tuple[str, str]] = []
    if GLOSSARY_PATH.exists():
        try:
            data = json.loads(GLOSSARY_PATH.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            data = None
        if isinstance(data, dict):
            entry = data.get(_glossary_key(source_lang, target_lang, kind))
            if isinstance(entry, dict):
                raw_id = entry.get("glossary_id")
                glossary_id = raw_id if isinstance(raw_id, str) else None
                synced_pairs = _parse_pairs(entry.get("synced_pairs"))
                if kind == "custom" and not txt_path.exists():
                    legacy_pairs = _parse_pairs(entry.get("pairs"))
                    if legacy_pairs:
                        _write_glossary_txt(txt_path, legacy_pairs)
    if kind == "banned" and source_lang == "pl" and not txt_path.exists():
        _write_glossary_txt(txt_path, DEFAULT_BANNED_WORDS_PL)
    pairs = _read_glossary_txt(txt_path)
    return pairs, glossary_id, synced_pairs


def save_glossary_entries(
    source_lang: str,
    target_lang: str,
    pairs: list[tuple[str, str]],
    glossary_id: str | None,
    synced_pairs: list[tuple[str, str]] | None = _UNSET,  # type: ignore[assignment]
    kind: str = "custom",
) -> None:
    """Writes the word-pair list to its .txt file (glossary_txt_path) and the
    current remote glossary_id (or None, if nothing has been successfully
    synced to Palabra yet / it was cleared) to GLOSSARY_PATH, leaving every
    other pair/kind's own entry untouched. synced_pairs records what was
    actually last pushed to Palabra -- pass it explicitly only after a real
    sync attempt (success -> the pairs just sent; the field is otherwise
    left as whatever was previously recorded, since a plain local edit
    doesn't change what's live on Palabra)."""
    _write_glossary_txt(glossary_txt_path(source_lang, target_lang, kind), pairs)
    if GLOSSARY_PATH.exists():
        try:
            data = json.loads(GLOSSARY_PATH.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                data = {}
        except (json.JSONDecodeError, OSError):
            data = {}
    else:
        data = {}
    key = _glossary_key(source_lang, target_lang, kind)
    if synced_pairs is _UNSET:
        existing_entry = data.get(key)
        synced_pairs = _parse_pairs(existing_entry.get("synced_pairs")) if isinstance(existing_entry, dict) else []
    data[key] = {
        "glossary_id": glossary_id,
        "synced_pairs": [list(p) for p in (synced_pairs or [])],
    }
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    GLOSSARY_PATH.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def clear_glossary_id_if_matches(glossary_id: str) -> None:
    """Called after deleting a glossary directly (e.g. from the account-wide
    manager view) -- if any local language pair still points at that now-gone
    glossary_id, clears it (and the synced_pairs that went with it) so the
    next time that pair's GlossaryDialog opens it correctly shows "not
    synced" instead of a stale, now-false "Aktywny w Palabra."."""
    if not GLOSSARY_PATH.exists():
        return
    try:
        data = json.loads(GLOSSARY_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return
    if not isinstance(data, dict):
        return
    changed = False
    for entry in data.values():
        if isinstance(entry, dict) and entry.get("glossary_id") == glossary_id:
            entry["glossary_id"] = None
            entry["synced_pairs"] = []
            changed = True
    if changed:
        GLOSSARY_PATH.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
