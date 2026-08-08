# Remove the source-mode selector; live add/change/remove file; two independent pause controls

## Context

The "Źródło" dropdown (`mode_combo`: Mikrofon / Plik / Mikrofon + Plik) no longer reflects how the app is actually used — the user wants the microphone always active and the file treated as a fully optional, always-available add-on, editable at any time (before Start or mid-session), not gated behind a mode choice. Two related requests came out of discussion:

1. A freshly loaded/changed file must never start playing automatically — it always starts paused, whether that happens at Start or while a session is already running.
2. The file can be added, changed, or removed while a session is already running, not just before Start.

This builds directly on today's earlier tech-debt cleanup, which added `SessionMode` (enum) and `SessionConfig` (dataclass) — both get revisited here since the mode concept itself is being removed.

## Design

### A. Always-visible layout, no mode selector

- `mode_combo` and `SessionMode` (enum, `_current_mode()`) are removed entirely.
- The microphone device combo, gain slider, and gate slider are unconditionally visible (mic is always part of every session now).
- The file row is unconditionally visible, showing `(nie wybrano pliku)` when nothing is selected.
- Layout: two separate `QFormLayout` rows instead of the current combined "Wejście:" row — **"Mikrofon:"** (device combo + "Odśwież urządzenia") and **"Plik (opcjonalnie):"** (path label + "Wybierz plik..." + a new small remove button, see below).

### B. Removing a selected file

A new small button (e.g. "✕") next to the file path label, enabled only when a file is currently selected. Clicking it clears `self._selected_file`, resets the label to `(nie wybrano pliku)`, and — if a session is running — live-detaches the file from the mix (mic keeps going uninterrupted; see D).

### C. A file never autoplays

Whenever a file becomes the active file — at Start (one was pre-selected) or live mid-session (added or changed via "Wybierz plik...") — it starts in a **paused** state. The user must explicitly resume it (see E). This is enforced at the source level: the `FileStream` is paused immediately after construction, before any of its audio can reach the mix, so there's no window where a frame could leak through.

### D. `MixedSource` — file becomes optional and live-replaceable

`app/audio_io.py`. Today `MixedSource(mic, file)` takes both permanently at construction. New:

- `MixedSource.__init__(self, mic: MicStream, file: FileStream | None = None)` — file is optional from the start.
- `pause_file()` / `resume_file()` / `seek()` / `position_ms` / `total_ms` become no-ops / `0.0` when `self._file is None`.
- `__enter__`/`__exit__` skip the file's own enter/exit when there isn't one.
- `chunks()` only starts a file pump task if a file is present at the time it starts running; the file side of the mix is silence whenever there's no file pump task (already the existing fallback for "queue momentarily empty" — now also covers "no file at all").
- **New: `async def set_file(self, file: FileStream | None) -> None`** — live swap. Cancels and awaits any existing file pump task, drains any audio still sitting in the file queue, closes the old `FileStream` (if any), then — if a new file was given — enters it and starts a fresh pump task for it. Runs on `MixedSource`'s own asyncio loop (the session's loop), so no locking is needed: `chunks()`'s outer tick loop and `set_file()` never run concurrently on different threads, only interleaved on the same event loop, the same reasoning the rest of this class already relies on.

Every session — even one with no file at Start — now constructs a `MixedSource(mic, file=None)` instead of sometimes constructing a bare `MicStream`. This is what makes "add a file to a running mic-only session" possible at all: there has to be a `MixedSource` already in place to plug a file into.

### E. Two independent pause controls (not one button with shifting meaning)

- **"Pauza"/"Wznów" (existing button)** — always controls the whole session (server-side pause/resume, same as today's mic-only behavior), regardless of whether a file is selected. Fully reverts to its original, simple, single-purpose form — no more mode-dependent branching.
- **New "Pauza pliku"/"Wznów plik" button** — visible whenever a file is currently selected (before or during a session), enabled only while a session is running. Controls only `pause_file()`/`resume_file()` — purely local, mirrors today's Mikrofon+Plik-mode pause behavior, but as its own control instead of overloading the main button.

This sidesteps the double-pause ambiguity entirely (session paused with no file, then a file gets added — a real, if rare, sequence): each button tracks and displays its own independent state, no hierarchy or two-step resume needed. A freshly added/changed file always starts in its "Wznów plik" state per C.

### F. `TranslationRunner` / `SessionWorker` — live file control

`app/translation_session.py`: `request_set_file(path: str | None) -> None`, following the same shape as `request_change_mic_device` — no-ops if the source isn't a `MixedSource` (i.e. doesn't have `set_file`), otherwise schedules an async `_do_set_file(path)` that constructs a `FileStream(path)` (or `None`), pauses it before handing it to `self._source.set_file(...)` (enforcing C), and reports a readable error via `on_error` if construction/validation fails.

`app/gui.py`: `SessionWorker.set_file(path: str | None) -> None` — thin `_call_on_loop("request_set_file", path)` passthrough, same pattern as `change_mic_device`.

`SessionWorker.start()` simplifies to always build `MixedSource(mic, file)` (file possibly `None`) — the three-way branch (`include_mic and file_path` / `include_mic` / file-only) collapses to two cases (mic-only-at-Start vs. mic-with-file-at-Start), both producing the same `MixedSource` wrapper. `position_ms`/`total_ms` properties delegate to `self._mixed_source` (always set) instead of a separately-tracked `self._file_source`, so they stay correct after a live file swap instead of pointing at a stale, no-longer-active `FileStream`.

`SessionConfig` drops the now-always-true `include_mic` field.

### G. Loop-protection auto-pause always active

`_check_loop_repeat`'s call site drops its mode check (`event.is_final and self._current_mode() == SessionMode.MIC` → `event.is_final`) — since the mic is now always part of every session, the feedback-loop safeguard should always be armed, not only when no file is selected. This closes a real gap in the current app: today, a live mic picking up its own translated output while a file is *also* mixed in doesn't trigger auto-pause at all.

### H. Locking

`file_btn` and the new remove button join `mic_combo`/`subtitles_only_check` as **not** in `_config_widgets` — they stay live/editable for the whole session, matching this design's whole point.

## Testing

Per this project's convention (standalone scratchpad scripts, `assert` + `print`, no pytest):
- `MixedSource`: file-less construction/mixing (mic-only, silence on the file side); `set_file()` live add (mic-only → mixed, real device pump lifecycle); `set_file()` live change (swap to a second file mid-stream, old one's task actually cancelled — no leaked task, no cross-talk); `set_file(None)` live remove (mixed → mic-only, matching the existing "file exhausted" mic-only-continues behavior); a freshly `set_file()`'d file is paused (no audio from it until `resume_file()`).
- GUI: two-row layout renders both mic and file settings unconditionally; remove button enabled only when a file is selected; "Pauza" always session-level regardless of file presence; "Pauza pliku" visible only with a file, independent state from "Pauza"; adding/changing/removing a file mid-session calls `SessionWorker.set_file()` and updates `file_pause_btn`'s state to "Wznów plik"; loop-repeat check fires regardless of file presence.
- Real end-to-end session test (real API, real devices, matching this project's established practice) covering: Start mic-only → add a file mid-session → confirm it's silent until "Wznów plik" → change to a second file → remove the file → mic keeps flowing the whole time without interruption.
- Full regression suite must stay green.

## Out of scope

- Multiple files queued/playlisted — only one file active at a time, same as today.
- Any change to how a file is decoded, sought, or mixed once active — only its *lifecycle* (when it exists, when it's replaced) changes.
