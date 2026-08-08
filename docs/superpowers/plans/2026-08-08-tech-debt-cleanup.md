# Tech-Debt Cleanup Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix five Fowler-baseline code smells / spec gaps found by a full-app code review: duplicated real-time-pacing logic, duplicated mic-stream construction, a raw mode-index primitive standing in for a domain concept, a 9-parameter data clump on `SessionWorker`, and missing file validation at selection time.

**Architecture:** Five independent, mechanical extractions/additions with no behavioral change to any of them except Task 5 (which adds new, previously-missing validation behavior). No task depends on another's runtime interface — they touch overlapping files (`app/audio_io.py`, `app/gui.py`) but not overlapping code regions.

**Tech Stack:** Python, PySide6 (Qt), sounddevice, av (PyAV), palabra-ai SDK. No new dependencies.

## Global Constraints

- Match this project's existing test convention: standalone scripts under the scratchpad directory (`C:\Users\dszwe\AppData\Local\Temp\claude\e--STS\c6cd0ce0-2621-4fa4-9540-d709192a0f82\scratchpad\`), run directly with `.venv/Scripts/python.exe <script>.py`, using plain `assert` + `print("OK ...")` / `print("ALL TESTS PASSED")` — this codebase does not use pytest.
- No behavioral change for Tasks 1-4 — these are pure extractions. Any test that exercises the refactored code's *external* behavior must produce identical results before and after.
- Preserve existing Polish-language UI strings and error-message conventions.
- Spec: `docs/superpowers/specs/2026-08-08-tech-debt-cleanup-design.md`.

---

### Task 1: `RealtimePacer` — extract and use in `FileStream`, `MicStream`, `MixedSource`

**Files:**
- Modify: `app/audio_io.py` (add `RealtimePacer` class after the module constants, before `class MicStream`; use it in `MicStream.chunks()`, `FileStream.chunks()`, `MixedSource.chunks()`)
- Test: `<scratchpad>/test_realtime_pacer.py`

**Interfaces:**
- Produces: `RealtimePacer(step_ms: float)` with `async def tick(self) -> None` and `def resync(self) -> None` — no other task consumes this, but it must exist under this exact name/signature for the test in this task.

- [ ] **Step 1: Write the failing test**

Create `<scratchpad>/test_realtime_pacer.py`:

```python
"""RealtimePacer: paces a fixed-tick loop to real time via tick(), and
resync() lets the next tick() proceed immediately instead of "catching up"
after a gap (matching the anchor/anchor_ms pattern this replaces)."""
import asyncio
import sys
import time

sys.path.insert(0, r"E:\STS")

from app.audio_io import RealtimePacer


async def main():
    # T1: N ticks of step_ms each take roughly N * step_ms of wall time.
    pacer = RealtimePacer(step_ms=50)
    t0 = time.monotonic()
    for _ in range(3):
        await pacer.tick()
    elapsed = time.monotonic() - t0
    assert 0.13 < elapsed < 0.20, f"T1 FAILED: expected ~150ms for 3 ticks of 50ms, got {elapsed * 1000:.0f}ms"
    print(f"OK T1: 3 ticks of 50ms paced to ~{elapsed * 1000:.0f}ms")

    # T2: resync() re-anchors so the very next tick() doesn't try to "catch
    # up" on time that already passed.
    pacer2 = RealtimePacer(step_ms=50)
    await asyncio.sleep(0.2)  # simulate falling behind before any tick
    pacer2.resync()
    t0 = time.monotonic()
    await pacer2.tick()
    elapsed = time.monotonic() - t0
    assert elapsed < 0.1, f"T2 FAILED: expected a near-immediate tick after resync, got {elapsed * 1000:.0f}ms"
    print(f"OK T2: resync() lets the next tick() proceed without catching up ({elapsed * 1000:.0f}ms)")

    # T3: tick() itself self-resyncs when it's already behind (the "delay <=
    # 0" branch) -- same behavior the inline anchor/anchor_ms code had.
    pacer3 = RealtimePacer(step_ms=50)
    await asyncio.sleep(0.2)
    t0 = time.monotonic()
    await pacer3.tick()  # already "behind" -- 200ms passed vs. a 50ms step
    elapsed = time.monotonic() - t0
    assert elapsed < 0.1, f"T3 FAILED: expected tick() to self-resync when behind, got {elapsed * 1000:.0f}ms"
    print(f"OK T3: tick() self-resyncs when already behind ({elapsed * 1000:.0f}ms)")


asyncio.run(main())
print("ALL TESTS PASSED")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python.exe "<scratchpad>/test_realtime_pacer.py"`
Expected: `ImportError: cannot import name 'RealtimePacer' from 'app.audio_io'`

- [ ] **Step 3: Add `RealtimePacer` and wire it into all three call sites**

In `app/audio_io.py`, add this class after the module-level constants (after `MAX_OUTPUT_BACKLOG_SAMPLES = ...`) and before `class MicStream:`:

```python
class RealtimePacer:
    """Paces a fixed-tick loop to real time. Call tick() once per loop
    iteration where the loop previously advanced anchor_ms and slept; call
    resync() wherever the loop previously reset anchor/anchor_ms directly
    (after a pause, a seek, or dropping a stale backlog) instead of waiting
    for the next tick() to notice it fell behind.
    """

    def __init__(self, step_ms: float):
        self._step_ms = step_ms
        self._anchor = time.monotonic()
        self._anchor_ms = 0.0

    async def tick(self) -> None:
        self._anchor_ms += self._step_ms
        delay = self._anchor + self._anchor_ms / 1000 - time.monotonic()
        if delay > 0:
            await asyncio.sleep(delay)
        else:
            self.resync()

    def resync(self) -> None:
        self._anchor = time.monotonic()
        self._anchor_ms = 0.0
```

Then, in `MicStream.chunks()`:
- Replace `anchor = time.monotonic()` / `anchor_ms = 0.0` (the pair right after the comment block starting "Explicit real-time pacing (like FileStream)...") with `pacer = RealtimePacer(CHUNK_MS)`.
- Replace the pause-branch's `anchor = time.monotonic()  # resync -- don't try to "catch up" on paused time` / `anchor_ms = 0.0` with `pacer.resync()  # don't try to "catch up" on paused time`.
- Replace the backlog-drop branch's `anchor = time.monotonic()  # we just dropped a backlog -- resync instead of pacing off a stale anchor` / `anchor_ms = 0.0` with `pacer.resync()  # we just dropped a backlog -- resync instead of pacing off a stale anchor`.
- Replace the whole tick block:
  ```python
  anchor_ms += CHUNK_MS
  delay = anchor + anchor_ms / 1000 - time.monotonic()
  if delay > 0:
      await asyncio.sleep(delay)
  else:
      # Fell behind real time (a scheduling hiccup, GC pause,
      # anything) -- resync instead of racing to catch up.
      # Without this, anchor/anchor_ms never recover: every
      # later chunk keeps computing delay <= 0 too (anchor_ms
      # keeps climbing while anchor stays fixed in the past),
      # so pacing silently stops for the rest of the session --
      # exactly the persistent, worsening "faster than
      # real-time" this pacing was meant to prevent. Same fix
      # FileStream already uses for the same reason.
      anchor = time.monotonic()
      anchor_ms = 0.0
  yield self._apply_gain(pending[:CHUNK_BYTES])
  pending = pending[CHUNK_BYTES:]
  ```
  with:
  ```python
  await pacer.tick()
  yield self._apply_gain(pending[:CHUNK_BYTES])
  pending = pending[CHUNK_BYTES:]
  ```

In `FileStream.chunks()`:
- Replace `anchor = time.monotonic()` / `anchor_ms = 0.0` (right after `pos = 0`) with `pacer = RealtimePacer(CHUNK_MS)`.
- Replace the seek branch's `anchor = time.monotonic()` / `anchor_ms = 0.0` with `pacer.resync()`.
- Replace the tick block:
  ```python
  anchor_ms += CHUNK_MS
  delay = anchor + anchor_ms / 1000 - time.monotonic()
  if delay > 0:
      await asyncio.sleep(delay)
  else:
      # fell behind real time (paused, seeked, or a scheduling hiccup) —
      # resync instead of bursting out all the chunks we "owe"
      anchor = time.monotonic()
      anchor_ms = 0.0
  ```
  with:
  ```python
  await pacer.tick()
  ```
  (this block already comes right after `yield chunk`, so no other reordering is needed).

In `MixedSource.chunks()`:
- Replace `anchor = time.monotonic()` / `anchor_ms = 0.0` (right after `silence = bytes(CHUNK_BYTES)`) with `pacer = RealtimePacer(CHUNK_MS)`.
- Replace the tick block:
  ```python
  anchor_ms += CHUNK_MS
  delay = anchor + anchor_ms / 1000 - time.monotonic()
  if delay > 0:
      await asyncio.sleep(delay)
  else:
      # Same resync-on-behind rationale as MicStream/FileStream.
      anchor = time.monotonic()
      anchor_ms = 0.0
  ```
  with:
  ```python
  await pacer.tick()
  ```
  (leave the `try/while True:`/`finally:` structure around it untouched — only this inner block changes).

- [ ] **Step 4: Run the new test to verify it passes**

Run: `.venv/Scripts/python.exe "<scratchpad>/test_realtime_pacer.py"`
Expected: `ALL TESTS PASSED`

- [ ] **Step 5: Re-run existing pacing-related regression tests to confirm no behavior change**

Run each of these (same scratchpad directory) and confirm each still prints `ALL TESTS PASSED` (or its prior success marker) exactly as before:
`.venv/Scripts/python.exe "<scratchpad>/test_mic_pacing_resync.py"`
`.venv/Scripts/python.exe "<scratchpad>/test_mic_realtime_pacing.py"`
`.venv/Scripts/python.exe "<scratchpad>/test_mixed_source.py"`
`.venv/Scripts/python.exe "<scratchpad>/test_file_seek_while_paused.py"`

- [ ] **Step 6: Commit**

```bash
git add app/audio_io.py
git commit -m "Extract RealtimePacer, dedup real-time pacing in MicStream/FileStream/MixedSource"
```

---

### Task 2: `MicStream._open_stream()` — dedup stream construction

**Files:**
- Modify: `app/audio_io.py` (`MicStream.__init__` and `MicStream.switch_device`)
- Test: `<scratchpad>/test_mic_open_stream.py`

**Interfaces:**
- Produces: `MicStream._open_stream(self, device: int | None) -> sd.RawInputStream` — no other task consumes this.

- [ ] **Step 1: Write the failing test**

Create `<scratchpad>/test_mic_open_stream.py`:

```python
"""MicStream._open_stream(): __init__ and switch_device() both build their
sd.RawInputStream through this one method, so they can never drift apart."""
import sys
from unittest.mock import patch, MagicMock

sys.path.insert(0, r"E:\STS")

import app.audio_io as audio_io

# T1: _open_stream exists and returns whatever sd.RawInputStream returns,
# called with the expected fixed kwargs plus the given device.
with patch.object(audio_io.sd, "RawInputStream") as mock_stream_cls:
    mock_stream_cls.return_value = MagicMock()
    mic = audio_io.MicStream.__new__(audio_io.MicStream)
    mic._q = None  # not touched by _open_stream itself
    result = mic._open_stream(42)
    mock_stream_cls.assert_called_once_with(
        samplerate=audio_io.RATE,
        channels=audio_io.CHANNELS,
        dtype="int16",
        device=42,
        callback=mic._on_audio,
        extra_settings=audio_io._wasapi_extra_settings(),
    )
    assert result is mock_stream_cls.return_value
    print("OK T1: _open_stream(42) builds the stream with the expected fixed kwargs")

# T2: __init__ uses _open_stream (not a second inline sd.RawInputStream call).
with patch.object(audio_io.sd, "RawInputStream") as mock_stream_cls:
    mock_stream_cls.return_value = MagicMock()
    mic = audio_io.MicStream(device=7)
    assert mock_stream_cls.call_count == 1, f"T2 FAILED: expected 1 call, got {mock_stream_cls.call_count}"
    assert mock_stream_cls.call_args.kwargs["device"] == 7
    print("OK T2: __init__ builds its stream via _open_stream")

# T3: switch_device uses _open_stream too, and still swaps correctly.
with patch.object(audio_io.sd, "RawInputStream") as mock_stream_cls:
    old_stream = MagicMock()
    new_stream = MagicMock()
    mock_stream_cls.side_effect = [old_stream, new_stream]
    mic = audio_io.MicStream(device=1)
    mic.switch_device(2)
    assert mock_stream_cls.call_count == 2, f"T3 FAILED: expected 2 calls total, got {mock_stream_cls.call_count}"
    assert mock_stream_cls.call_args.kwargs["device"] == 2
    new_stream.start.assert_called_once()
    old_stream.stop.assert_called_once()
    old_stream.close.assert_called_once()
    assert mic._stream is new_stream
    print("OK T3: switch_device builds the new stream via _open_stream and swaps correctly")

print("ALL TESTS PASSED")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python.exe "<scratchpad>/test_mic_open_stream.py"`
Expected: `AttributeError: 'MicStream' object has no attribute '_open_stream'`

- [ ] **Step 3: Write minimal implementation**

In `app/audio_io.py`, inside `class MicStream`, add right after `__init__` (before `_on_audio`):

```python
    def _open_stream(self, device: int | None) -> sd.RawInputStream:
        return sd.RawInputStream(
            samplerate=RATE,
            channels=CHANNELS,
            dtype="int16",
            device=device,
            callback=self._on_audio,
            extra_settings=_wasapi_extra_settings(),
        )
```

Change `__init__`'s stream construction from:

```python
        self._stream = sd.RawInputStream(
            samplerate=RATE,
            channels=CHANNELS,
            dtype="int16",
            device=device,
            callback=self._on_audio,
            extra_settings=_wasapi_extra_settings(),
        )
```

to:

```python
        self._stream = self._open_stream(device)
```

Change `switch_device`'s stream construction from:

```python
        new_stream = sd.RawInputStream(
            samplerate=RATE,
            channels=CHANNELS,
            dtype="int16",
            device=new_device,
            callback=self._on_audio,
            extra_settings=_wasapi_extra_settings(),
        )
```

to:

```python
        new_stream = self._open_stream(new_device)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/Scripts/python.exe "<scratchpad>/test_mic_open_stream.py"`
Expected: `ALL TESTS PASSED`

- [ ] **Step 5: Re-run existing mic-switch regression tests to confirm no behavior change**

`.venv/Scripts/python.exe "<scratchpad>/test_mic_switch_device.py"`
`.venv/Scripts/python.exe "<scratchpad>/test_mic_switch_device_pipeline.py"`

Both must still print `ALL TESTS PASSED`.

- [ ] **Step 6: Commit**

```bash
git add app/audio_io.py
git commit -m "Extract MicStream._open_stream(), dedup __init__/switch_device"
```

---

### Task 3: `SessionMode` enum

**Files:**
- Modify: `app/gui.py` (add `SessionMode` enum, add `MainWindow._current_mode()`, replace 6 raw-index sites)
- Test: `<scratchpad>/test_session_mode.py`

**Interfaces:**
- Produces: `SessionMode` enum (`MIC = 0`, `FILE = 1`, `MIC_AND_FILE = 2`, properties `.has_mic`, `.has_file`) and `MainWindow._current_mode(self) -> SessionMode` — no other task consumes these, but they must exist under these exact names for the test in this task.

- [ ] **Step 1: Write the failing test**

Create `<scratchpad>/test_session_mode.py`:

```python
"""SessionMode: has_mic/has_file correctly reflect each of the 3 modes, and
MainWindow._current_mode() reads mode_combo.currentIndex() through it."""
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, r"E:\STS")

# T1: enum values and properties, no GUI needed.
import app.gui as guimod

assert guimod.SessionMode.MIC.value == 0
assert guimod.SessionMode.FILE.value == 1
assert guimod.SessionMode.MIC_AND_FILE.value == 2
assert guimod.SessionMode.MIC.has_mic is True and guimod.SessionMode.MIC.has_file is False
assert guimod.SessionMode.FILE.has_mic is False and guimod.SessionMode.FILE.has_file is True
assert guimod.SessionMode.MIC_AND_FILE.has_mic is True and guimod.SessionMode.MIC_AND_FILE.has_file is True
print("OK T1: SessionMode.has_mic/has_file correct for all 3 modes")

# T2: MainWindow._current_mode() reads mode_combo through SessionMode.
tmp_dir = Path(tempfile.mkdtemp())
import app.config as config
with patch.object(config, "CONFIG_DIR", tmp_dir), \
     patch.object(config, "OVERLAY_SETTINGS_PATH", tmp_dir / "overlay_settings.json"):
    from PySide6.QtWidgets import QApplication
    app_qt = QApplication.instance() or QApplication([])

    w = guimod.MainWindow()
    w.mode_combo.setCurrentIndex(0)
    assert w._current_mode() is guimod.SessionMode.MIC
    w.mode_combo.setCurrentIndex(1)
    assert w._current_mode() is guimod.SessionMode.FILE
    w.mode_combo.setCurrentIndex(2)
    assert w._current_mode() is guimod.SessionMode.MIC_AND_FILE
    print("OK T2: MainWindow._current_mode() tracks mode_combo.currentIndex()")

print("ALL TESTS PASSED")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python.exe "<scratchpad>/test_session_mode.py"`
Expected: `AttributeError: module 'app.gui' has no attribute 'SessionMode'`

- [ ] **Step 3: Write minimal implementation**

In `app/gui.py`, add `from enum import Enum` to the imports (alongside the other stdlib imports at the top of the file, e.g. next to `import asyncio`/`import sys`).

Add this module-level, near the other module-level constants (`REGION`, `DASHBOARD_URL`):

```python
class SessionMode(Enum):
    MIC = 0
    FILE = 1
    MIC_AND_FILE = 2

    @property
    def has_mic(self) -> bool:
        return self in (SessionMode.MIC, SessionMode.MIC_AND_FILE)

    @property
    def has_file(self) -> bool:
        return self in (SessionMode.FILE, SessionMode.MIC_AND_FILE)
```

Add this method on `MainWindow` (place it near `_on_mode_changed`):

```python
    def _current_mode(self) -> SessionMode:
        return SessionMode(self.mode_combo.currentIndex())
```

Then replace the raw index checks at these six sites (search for them by content, not line number, since earlier tasks may have shifted lines slightly):

1. `_on_mode_changed(self, index: int) -> None`: replace
   ```python
   mic_active = index in (0, 2)
   file_active = index in (1, 2)
   ```
   with
   ```python
   mode = SessionMode(index)
   mic_active = mode.has_mic
   file_active = mode.has_file
   ```
   (leave everything below using `mic_active`/`file_active` unchanged).

2. `_on_mic_selection_changed`: replace
   ```python
   if self._worker is None or self.mode_combo.currentIndex() not in (0, 2):
   ```
   with
   ```python
   if self._worker is None or not self._current_mode().has_mic:
   ```

3. `_confirm_feedback_loop_risk`: replace
   ```python
   mode_idx = self.mode_combo.currentIndex()
   mic_active = mode_idx in (0, 2)
   if not mic_active or self.subtitles_only_check.isChecked():
   ```
   with
   ```python
   mic_active = self._current_mode().has_mic
   if not mic_active or self.subtitles_only_check.isChecked():
   ```

4. `_on_start_stop`: replace
   ```python
   mode_idx = self.mode_combo.currentIndex()
   mic_active = mode_idx in (0, 2)
   needs_file = mode_idx in (1, 2)
   ```
   with
   ```python
   mode = self._current_mode()
   mic_active = mode.has_mic
   needs_file = mode.has_file
   ```
   (leave everything below using `mic_active`/`needs_file` unchanged).

5. `_on_pause_resume`: replace
   ```python
   if self.mode_combo.currentIndex() == 2:
   ```
   with
   ```python
   if self._current_mode() == SessionMode.MIC_AND_FILE:
   ```

6. The transcript handler's mic-mode-only check: replace
   ```python
   if event.is_final and self.mode_combo.currentIndex() == 0:  # mic mode only
   ```
   with
   ```python
   if event.is_final and self._current_mode() == SessionMode.MIC:  # mic mode only
   ```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/Scripts/python.exe "<scratchpad>/test_session_mode.py"`
Expected: `ALL TESTS PASSED`

- [ ] **Step 5: Re-run existing mode-related regression tests to confirm no behavior change**

`.venv/Scripts/python.exe "<scratchpad>/test_gui_mixed_mode.py"`
`.venv/Scripts/python.exe "<scratchpad>/test_gui_mic_switch.py"`
`.venv/Scripts/python.exe "<scratchpad>/test_gui_subtitles_only.py"`
`.venv/Scripts/python.exe "<scratchpad>/test_gui_live_subtitles_toggle.py"`

All four must still print `ALL TESTS PASSED`.

- [ ] **Step 6: Commit**

```bash
git add app/gui.py
git commit -m "Add SessionMode enum, replace raw mode_combo index checks"
```

---

### Task 4: `SessionConfig` dataclass

**Files:**
- Modify: `app/gui.py` (`SessionWorker.__init__`, the call site in `_on_start_stop`)
- Test: `<scratchpad>/test_session_config.py`

**Interfaces:**
- Consumes: `SessionMode`/`_current_mode()` (Task 3) only insofar as the call site already uses `mic_active`/`needs_file`/etc. computed from them — no new dependency.
- Produces: `SessionConfig` dataclass (12 fields matching `SessionWorker.__init__`'s current parameters) and `SessionWorker(config: SessionConfig)` — no other task consumes these.

- [ ] **Step 1: Write the failing test**

Create `<scratchpad>/test_session_config.py`:

```python
"""SessionConfig: bundles SessionWorker's construction parameters into one
dataclass; SessionWorker(config) unpacks it into the same self._xxx
attributes as before (every other line in SessionWorker is unaffected)."""
import sys

sys.path.insert(0, r"E:\STS")

import app.gui as guimod

# T1: SessionConfig holds all 12 fields with the documented defaults.
config = guimod.SessionConfig(
    api_key="key",
    source_lang="pl",
    target_lang="en",
    mic_device=5,
    output_device=6,
    file_path=None,
)
assert config.include_mic is True
assert config.mic_gain == 1.0
assert config.mic_gate_threshold == 0.0
assert config.voice_id is None
assert config.voice_cloning is False
assert config.subtitles_only is False
print("OK T1: SessionConfig has the expected fields and defaults")

# T2: SessionWorker(config) unpacks every field into the matching self._xxx
# attribute, identically to the old 9-parameter constructor.
config2 = guimod.SessionConfig(
    api_key="k2",
    source_lang="pl",
    target_lang="en",
    mic_device=1,
    output_device=2,
    file_path="/tmp/x.wav",
    include_mic=False,
    mic_gain=0.5,
    mic_gate_threshold=0.2,
    voice_id="v1",
    voice_cloning=True,
    subtitles_only=True,
)
worker = guimod.SessionWorker(config2)
assert worker._api_key == "k2"
assert worker._source_lang == "pl"
assert worker._target_lang == "en"
assert worker._mic_device == 1
assert worker._output_device == 2
assert worker._file_path == "/tmp/x.wav"
assert worker._include_mic is False
assert worker._initial_mic_gain == 0.5
assert worker._initial_gate_threshold == 0.2
assert worker._voice_id == "v1"
assert worker._voice_cloning is True
assert worker._subtitles_only is True
print("OK T2: SessionWorker(config) unpacks every field to the matching self._xxx attribute")

print("ALL TESTS PASSED")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python.exe "<scratchpad>/test_session_config.py"`
Expected: `AttributeError: module 'app.gui' has no attribute 'SessionConfig'`

- [ ] **Step 3: Write minimal implementation**

In `app/gui.py`, add `from dataclasses import dataclass` to the imports (alongside the other stdlib imports).

Add this module-level, right before `class SessionWorker`:

```python
@dataclass
class SessionConfig:
    api_key: str
    source_lang: str
    target_lang: str
    mic_device: int | None
    output_device: int | None
    file_path: str | None
    include_mic: bool = True
    mic_gain: float = 1.0
    mic_gate_threshold: float = 0.0
    voice_id: str | None = None
    voice_cloning: bool = False
    subtitles_only: bool = False
```

Change `SessionWorker.__init__`'s signature from:

```python
    def __init__(
        self,
        api_key: str,
        source_lang: str,
        target_lang: str,
        mic_device: int | None,
        output_device: int | None,
        file_path: str | None,
        include_mic: bool = True,
        mic_gain: float = 1.0,
        mic_gate_threshold: float = 0.0,
        voice_id: str | None = None,
        voice_cloning: bool = False,
        subtitles_only: bool = False,
    ):
        super().__init__()
        self._api_key = api_key
        self._source_lang = source_lang
        self._target_lang = target_lang
        self._mic_device = mic_device
        self._output_device = output_device
        self._file_path = file_path
        # include_mic distinguishes "no mic at all" (file-only mode) from
        # "use the mic, and mic_device=None happens to mean the system
        # default" (mic-only or mixed mode) -- mic_device alone can't tell
        # those apart, since None is a valid device selection too.
        self._include_mic = include_mic
        self._initial_mic_gain = mic_gain
        self._initial_gate_threshold = mic_gate_threshold
        self._voice_id = voice_id
        self._voice_cloning = voice_cloning
        self._subtitles_only = subtitles_only
```

to:

```python
    def __init__(self, config: SessionConfig):
        super().__init__()
        self._api_key = config.api_key
        self._source_lang = config.source_lang
        self._target_lang = config.target_lang
        self._mic_device = config.mic_device
        self._output_device = config.output_device
        self._file_path = config.file_path
        # include_mic distinguishes "no mic at all" (file-only mode) from
        # "use the mic, and mic_device=None happens to mean the system
        # default" (mic-only or mixed mode) -- mic_device alone can't tell
        # those apart, since None is a valid device selection too.
        self._include_mic = config.include_mic
        self._initial_mic_gain = config.mic_gain
        self._initial_gate_threshold = config.mic_gate_threshold
        self._voice_id = config.voice_id
        self._voice_cloning = config.voice_cloning
        self._subtitles_only = config.subtitles_only
```

**Do not change anything else in `SessionWorker`** — every other `self._xxx` reference in the class (in `start()`, `pause_file()`, etc.) stays exactly as it is; only the constructor's parameter list and body change.

In `MainWindow._on_start_stop`, change the call site from:

```python
        worker = SessionWorker(
            api_key=creds.api_key,
            source_lang=source_lang,
            target_lang=target_lang,
            mic_device=mic_device,
            output_device=output_device,
            file_path=file_path,
            include_mic=mic_active,
            mic_gain=self.mic_gain_slider.value() / 100,
            mic_gate_threshold=self.mic_gate_slider.value() / 100,
            voice_id=voice_id,
            voice_cloning=voice_cloning,
            subtitles_only=self.subtitles_only_check.isChecked(),
        )
```

to:

```python
        worker = SessionWorker(SessionConfig(
            api_key=creds.api_key,
            source_lang=source_lang,
            target_lang=target_lang,
            mic_device=mic_device,
            output_device=output_device,
            file_path=file_path,
            include_mic=mic_active,
            mic_gain=self.mic_gain_slider.value() / 100,
            mic_gate_threshold=self.mic_gate_slider.value() / 100,
            voice_id=voice_id,
            voice_cloning=voice_cloning,
            subtitles_only=self.subtitles_only_check.isChecked(),
        ))
```

(Same keyword arguments, same values — only wrapped in `SessionConfig(...)` and passed as the single positional argument to `SessionWorker(...)`.)

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/Scripts/python.exe "<scratchpad>/test_session_config.py"`
Expected: `ALL TESTS PASSED`

- [ ] **Step 5: Re-run existing Start-flow regression tests to confirm no behavior change**

`.venv/Scripts/python.exe "<scratchpad>/test_gui_mixed_mode.py"`
`.venv/Scripts/python.exe "<scratchpad>/test_gui_subtitles_only.py"`
`.venv/Scripts/python.exe "<scratchpad>/test_gui_live_subtitles_toggle.py"`
`.venv/Scripts/python.exe "<scratchpad>/test_multi_start_stop_cycles.py"`

All four must still print `ALL TESTS PASSED`.

- [ ] **Step 6: Commit**

```bash
git add app/gui.py
git commit -m "Add SessionConfig dataclass, replace SessionWorker's 9-parameter constructor"
```

---

### Task 5: `probe_audio_file()` — validate the file at selection time

**Files:**
- Modify: `app/audio_io.py` (add `probe_audio_file`)
- Modify: `app/gui.py` (`_choose_file`, and the `from .audio_io import (...)` block)
- Test: `<scratchpad>/test_probe_audio_file.py`, `<scratchpad>/test_gui_choose_file_validation.py`

**Interfaces:**
- Produces: `probe_audio_file(path: str | Path) -> None` (raises `ValueError` on an invalid/corrupt file, `ImportError` if `av` isn't installed, returns `None` on a valid file) — consumed by `MainWindow._choose_file` in the same task.

- [ ] **Step 1: Write the failing test (probe function)**

Create `<scratchpad>/test_probe_audio_file.py`:

```python
"""probe_audio_file(): quick validity check for a file picked as a
translation source. Valid WAV passes; a corrupt/truncated file and a
non-audio file both raise ValueError with a readable message."""
import struct
import sys
import tempfile
import wave
from pathlib import Path

sys.path.insert(0, r"E:\STS")

from app.audio_io import probe_audio_file

tmp_dir = Path(tempfile.mkdtemp())

# T1: a valid WAV passes (returns None, raises nothing).
valid_wav = tmp_dir / "valid.wav"
with wave.open(str(valid_wav), "wb") as w:
    w.setnchannels(1)
    w.setsampwidth(2)
    w.setframerate(24000)
    w.writeframes(struct.pack("<100h", *([1000] * 100)))
probe_audio_file(str(valid_wav))  # must not raise
print("OK T1: a valid WAV passes probe_audio_file()")

# T2: a WAV with zero audio frames raises ValueError.
empty_wav = tmp_dir / "empty.wav"
with wave.open(str(empty_wav), "wb") as w:
    w.setnchannels(1)
    w.setsampwidth(2)
    w.setframerate(24000)
    w.writeframes(b"")
try:
    probe_audio_file(str(empty_wav))
    assert False, "T2 FAILED: expected ValueError for a WAV with no frames"
except ValueError as e:
    assert "empty.wav" in str(e), f"T2 FAILED: expected filename in message, got {e!r}"
    print(f"OK T2: an empty WAV raises ValueError -> {e}")

# T3: a corrupt/non-audio file (random bytes with a misleading .mp4 name)
# raises ValueError.
fake_mp4 = tmp_dir / "corrupt.mp4"
fake_mp4.write_bytes(b"this is not a real video/audio file, just text bytes")
try:
    probe_audio_file(str(fake_mp4))
    assert False, "T3 FAILED: expected ValueError for a non-audio file"
except ValueError as e:
    assert "corrupt.mp4" in str(e), f"T3 FAILED: expected filename in message, got {e!r}"
    print(f"OK T3: a corrupt/non-audio file raises ValueError -> {e}")

print("ALL TESTS PASSED")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python.exe "<scratchpad>/test_probe_audio_file.py"`
Expected: `ImportError: cannot import name 'probe_audio_file' from 'app.audio_io'`

- [ ] **Step 3: Write minimal implementation (probe function)**

In `app/audio_io.py`, add `import wave` to the imports (alongside `import queue`/`import threading`/etc.).

Add this function near `load_pcm`'s usage (module level, e.g. right after the `MAX_OUTPUT_BACKLOG_SAMPLES` constant and before `RealtimePacer`/`MicStream` — or right after the imports; either is fine as long as it's module-level and before first use):

```python
def probe_audio_file(path: str | Path) -> None:
    """Quick validity check for a file picked as a translation source --
    opens/demuxes it without decoding, so a corrupt or non-audio file is
    caught immediately at selection time instead of only surfacing once
    Start tries to fully decode it (see load_pcm). Raises ValueError with a
    readable Polish message on failure; returns normally if the file looks
    decodable.
    """
    path = Path(path)
    if path.suffix.lower() == ".wav":
        try:
            with wave.open(str(path), "rb") as w:
                if w.getnframes() == 0:
                    raise ValueError(f"{path.name}: plik WAV nie zawiera dźwięku.")
        except wave.Error as e:
            raise ValueError(f"{path.name}: nieprawidłowy plik WAV ({e}).") from e
        return
    try:
        import av
    except ImportError as e:
        raise ImportError(f"Sprawdzenie {path.name} wymaga pakietu av: uv add av") from e
    try:
        with av.open(str(path)) as container:
            if not container.streams.audio:
                raise ValueError(f"{path.name}: plik nie zawiera ścieżki audio.")
    except ValueError:
        raise
    except Exception as e:
        # av can raise several distinct FFmpeg-backed exception types for a
        # corrupt/unsupported/unreadable file -- catching broadly here is a
        # deliberate boundary (this function's whole job is "translate
        # whatever's wrong with this file into one readable message"),
        # matching the existing broad except in SessionWorker.start().
        raise ValueError(f"{path.name}: nie można otworzyć pliku ({e}).") from e
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/Scripts/python.exe "<scratchpad>/test_probe_audio_file.py"`
Expected: `ALL TESTS PASSED`

- [ ] **Step 5: Write the failing test (GUI wiring)**

Create `<scratchpad>/test_gui_choose_file_validation.py`:

```python
"""MainWindow._choose_file(): validates the picked file via probe_audio_file()
before accepting it -- shows a warning and leaves _selected_file untouched on
an invalid file, accepts it as before on a valid one."""
import struct
import sys
import tempfile
import wave
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, r"E:\STS")

tmp_dir = Path(tempfile.mkdtemp())

import app.config as config
with patch.object(config, "CONFIG_DIR", tmp_dir), \
     patch.object(config, "OVERLAY_SETTINGS_PATH", tmp_dir / "overlay_settings.json"):

    from PySide6.QtWidgets import QApplication
    app_qt = QApplication.instance() or QApplication([])

    import app.gui as guimod

    w = guimod.MainWindow()

    # T1: an invalid file shows a warning and does NOT set _selected_file.
    bad_file = tmp_dir / "bad.mp4"
    bad_file.write_bytes(b"not a real media file")
    with patch.object(guimod.QFileDialog, "getOpenFileName", return_value=(str(bad_file), "")), \
         patch.object(guimod.QMessageBox, "warning") as mock_warn:
        w._choose_file()
    assert mock_warn.called, "T1 FAILED: expected a warning dialog for an invalid file"
    assert w._selected_file is None, f"T1 FAILED: _selected_file should stay unset, got {w._selected_file!r}"
    print("OK T1: an invalid file shows a warning and is not accepted")

    # T2: a valid file is accepted as before (no warning, _selected_file set,
    # label updated).
    good_file = tmp_dir / "good.wav"
    with wave.open(str(good_file), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(24000)
        wav.writeframes(struct.pack("<100h", *([1000] * 100)))
    with patch.object(guimod.QFileDialog, "getOpenFileName", return_value=(str(good_file), "")), \
         patch.object(guimod.QMessageBox, "warning") as mock_warn:
        w._choose_file()
    assert not mock_warn.called, "T2 FAILED: a valid file should not trigger a warning"
    assert w._selected_file == str(good_file), f"T2 FAILED: {w._selected_file!r}"
    assert w.file_label.text() == str(good_file)
    print("OK T2: a valid file is accepted, no warning, label updated")

print("ALL TESTS PASSED")
```

- [ ] **Step 6: Run test to verify it fails**

Run: `.venv/Scripts/python.exe "<scratchpad>/test_gui_choose_file_validation.py"`
Expected: FAIL at T1 (`AssertionError: T1 FAILED: expected a warning dialog for an invalid file`), since `_choose_file` doesn't call `probe_audio_file` yet.

- [ ] **Step 7: Write minimal implementation (GUI wiring)**

In `app/gui.py`, add `probe_audio_file` to the existing `from .audio_io import (...)` block (alphabetically among the existing names, e.g. next to `list_output_devices`/`rescan_devices`).

Change `_choose_file` from:

```python
    def _choose_file(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Wybierz plik audio/wideo",
            "",
            "Audio/Video (*.wav *.mp3 *.mp4 *.mov *.m4a *.flac *.ogg *.mkv *.avi *.webm *.wmv *.flv *.aac *.ts);;Wszystkie pliki (*)",
        )
        if path:
            self._selected_file = path
            self.file_label.setText(path)
```

to:

```python
    def _choose_file(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Wybierz plik audio/wideo",
            "",
            "Audio/Video (*.wav *.mp3 *.mp4 *.mov *.m4a *.flac *.ogg *.mkv *.avi *.webm *.wmv *.flv *.aac *.ts);;Wszystkie pliki (*)",
        )
        if not path:
            return
        try:
            probe_audio_file(path)
        except (ValueError, ImportError) as e:
            QMessageBox.warning(self, "Nieprawidłowy plik", str(e))
            return
        self._selected_file = path
        self.file_label.setText(path)
```

- [ ] **Step 8: Run test to verify it passes**

Run: `.venv/Scripts/python.exe "<scratchpad>/test_gui_choose_file_validation.py"`
Expected: `ALL TESTS PASSED`

- [ ] **Step 9: Commit**

```bash
git add app/audio_io.py app/gui.py
git commit -m "Validate the picked file at selection time (probe_audio_file)"
```

---

### Task 6: Full regression + README

**Files:** `README.md` (small addition), no other files.

**Interfaces:**
- Consumes: everything from Tasks 1-5.
- Produces: nothing (terminal task).

- [ ] **Step 1: Run the full existing regression suite**

Run every `test_*.py` in `<scratchpad>` except the known slow/hardware-only/real-API ones (`test_2h_file_decode_memory.py`, `test_2h_gui_memory_growth.py`, `test_real_mic_switch_hardware.py`, and any script whose name contains `real_hardware`, `real_session`, `audio_text_latency`, `audio_text_timeline`, `audio_delivery_span`), plus all new tests from Tasks 1-5. Expected: only the two known, pre-existing, unrelated failures (`test_change_voice.py` T4, `test_new_features.py` console-encoding) — everything else passes.

- [ ] **Step 2: Update README**

Add a short bullet under the existing file-selection documentation in `README.md` (near where `_choose_file`'s behavior is described, or near the "Znane ograniczenie API" / file-mode section) noting that an invalid or corrupt file is now caught immediately when you pick it, with a clear error message, instead of only failing later at Start.

- [ ] **Step 3: Commit**

```bash
git add README.md
git commit -m "Document file validation at selection time in README"
```

---

## Self-Review Notes

- **Spec coverage:** All five spec sections (RealtimePacer, _open_stream, SessionMode, SessionConfig, probe_audio_file) map to Tasks 1-5. Testing section's full-regression requirement maps to Task 6. Out-of-scope items (MainWindow Divergent Change, reconnect logic) are respected — no task touches either.
- **Placeholder scan:** No TBD/TODO; every step has literal code or a concrete, runnable command.
- **Type consistency:** `RealtimePacer(step_ms: float)` / `.tick()` / `.resync()` used identically across all three Task 1 call sites. `SessionMode` values (0/1/2) and `.has_mic`/`.has_file` used identically across Task 3's six sites. `SessionConfig`'s 12 fields match `SessionWorker.__init__`'s prior 12 parameters name-for-name. `probe_audio_file(path: str | Path) -> None` used identically in Task 5's test and its `_choose_file` call site.
