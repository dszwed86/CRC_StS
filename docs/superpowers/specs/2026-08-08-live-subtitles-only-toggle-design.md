# Live toggle for "Tylko napisy" (subtitles-only)

## Context

"Tylko napisy (bez dźwięku)" already exists (`app/gui.py`, `app/translation_session.py`) as a local audio mute: `TranslationRunner._mute_output` skips `self._sink.play(event.pcm)` for incoming `Audio` events, while transcripts keep flowing normally. The checkbox that controls it (`subtitles_only_check`) is currently in `MainWindow._config_widgets`, so it's locked for the entire duration of a running session — it can only be set before clicking Start.

This mirrors a pattern the app already supports for other settings that don't touch the server session: `mic_gain_slider`/`mic_gate_slider` are live-tunable via `SessionWorker.set_mic_gain()`/`set_gate_threshold()`, and `mic_combo` stays enabled mid-session for live device switching. Subtitles-only should get the same treatment, since muting/unmuting playback is purely local (no server-side state, no billing change — confirmed in an earlier investigation that Palabra has no server-side "skip speech generation" option).

## Goal

Let the user toggle "Tylko napisy" while a session (Mikrofon, Plik, or Mikrofon + Plik mode) is running, instead of only before Start.

## Design

**`TranslationRunner` (`app/translation_session.py`)**
- Add `set_mute_output(muted: bool) -> None`: sets `self._mute_output = muted`. No asyncio scheduling needed — it's a plain flag read once per `Audio` event inside the existing receive loop, and Python's GIL makes a plain bool assignment/read from another thread safe enough for this (same reasoning already applied to `mic_gain`/`gate_threshold`, which are read from the audio device's own callback thread).

**`SessionWorker` (`app/gui.py`)**
- Add `set_subtitles_only(muted: bool) -> None`: if `self._runner` exists, call `self._runner.set_mute_output(muted)`; if `muted` is `True`, also call `self._sink.clear()` (already exists, used after seek) so any audio still sitting in `OutputSink`'s playback queue (up to ~1.2s per the recent backlog cap) is cut immediately instead of trailing off.

**GUI (`app/gui.py`)**
- Remove `subtitles_only_check` from `_config_widgets` so it stays enabled during a session (matching `mic_combo`'s precedent).
- Connect `subtitles_only_check.toggled` to a new `_on_subtitles_only_toggled(checked: bool)`:
  - If a worker is running, `checked is False` (i.e. the user is turning audio back ON), the mode has an active mic (`mode_combo.currentIndex() in (0, 2)`), and the current output device doesn't look like a virtual cable (`not is_virtual_cable_name(...)`) — show the same feedback-loop warning dialog Start already shows, with Yes/No. If the user picks No, revert the checkbox (block signals, set back to checked, return) without calling the worker.
  - Otherwise, call `self._worker.set_subtitles_only(checked)` if a worker exists (no-op before Start — the checkbox's own state is still what `_on_start_stop` reads when constructing a new `SessionWorker`).

No changes needed to `TranslationRunner.run()` itself, `SessionState`, or the Start-time validation path — both are already correct/independent of this change.

## Testing

- Unit test on `TranslationRunner.set_mute_output()`: toggle mid-stream, confirm `Audio` events are/aren't forwarded to a fake sink accordingly.
- GUI test: checkbox stays enabled during a session; toggling off (enabling audio) under risky conditions (mic active, non-cable output) shows the warning and reverts on "No"; toggling on (muting) calls `sink.clear()`.
- Real-hardware/live sanity check with a running session (matching the standard established for this app), given the prior investigation already caught a real bug that only real-hardware/live testing surfaced.

## Out of scope

- No change to the Start-time validation or warning logic.
- No change to how `mute_output` interacts with the loop-repeat auto-pause detector (that already operates on transcript text, independent of audio muting).
