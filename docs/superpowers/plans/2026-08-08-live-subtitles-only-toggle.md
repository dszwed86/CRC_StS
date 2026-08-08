# Live Subtitles-Only Toggle Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let the user toggle "Tylko napisy (bez dźwięku)" while a translation session is already running, instead of only before clicking Start.

**Architecture:** `TranslationRunner` gains a plain-attribute setter (`set_mute_output`) that also clears any already-queued audio when muting; `SessionWorker` exposes a thin passthrough (`set_subtitles_only`); the GUI unlocks the checkbox during a session and reuses the existing Start-time feedback-loop warning logic (extracted into a shared helper) when the user turns audio back on mid-session under risky conditions.

**Tech Stack:** Python, PySide6 (Qt), palabra-ai SDK. No new dependencies.

## Global Constraints

- Match this project's existing test convention: standalone scripts under the scratchpad directory (`C:\Users\dszwe\AppData\Local\Temp\claude\e--STS\c6cd0ce0-2621-4fa4-9540-d709192a0f82\scratchpad\`), run directly with `.venv/Scripts/python.exe <script>.py`, using plain `assert` + `print("OK ...")` / `print("ALL TESTS PASSED")` — this codebase does not use pytest as a project test suite.
- No changes to `TranslationRunner.run()`'s event loop, `SessionState`, or any server-side/session behavior — muting is purely local.
- Preserve existing Polish-language UI strings and warning dialog text verbatim where reused.
- Spec: `docs/superpowers/specs/2026-08-08-live-subtitles-only-toggle-design.md`.

---

### Task 1: `TranslationRunner.set_mute_output()`

**Files:**
- Modify: `app/translation_session.py` (add method after `request_change_mic_device`, i.e. after line 160, before `def request_seek` at line 162)
- Test: `<scratchpad>/test_live_mute_toggle.py`

**Interfaces:**
- Consumes: `TranslationRunner.__init__`'s existing `self._sink: AudioSink` and `self._mute_output: bool` (already present, `app/translation_session.py:77` and `:86`).
- Produces: `TranslationRunner.set_mute_output(muted: bool) -> None` — later tasks (Task 2) call this exact method name/signature.

- [ ] **Step 1: Write the failing test**

Create `<scratchpad>/test_live_mute_toggle.py`:

```python
"""TranslationRunner.set_mute_output(): toggling mid-stream stops/resumes
forwarding Audio events to the sink, and muting clears any audio already
queued in the sink (if the sink supports clear())."""
import sys

sys.path.insert(0, r"E:\STS")

from app.translation_session import TranslationRunner


class FakeSink:
    def __init__(self):
        self.played = []
        self.clear_calls = 0

    def play(self, pcm: bytes) -> None:
        self.played.append(pcm)

    def clear(self) -> None:
        self.clear_calls += 1


class FakeSource:
    async def chunks(self):
        return
        yield  # pragma: no cover -- never reached, makes this an async generator


sink = FakeSink()
runner = TranslationRunner(
    api_key="fake",
    region="eu",
    source_lang="pl",
    target_lang="en",
    source=FakeSource(),
    sink=sink,
)

# T1: default (mute_output=False) -- set_mute_output(True) mutes and clears.
assert runner._mute_output is False, "T1 FAILED: expected unmuted by default"
runner.set_mute_output(True)
assert runner._mute_output is True, "T1 FAILED: set_mute_output(True) didn't set the flag"
assert sink.clear_calls == 1, f"T1 FAILED: expected clear() called once, got {sink.clear_calls}"
print("OK T1: set_mute_output(True) mutes and clears the sink")

# T2: set_mute_output(False) unmutes and does NOT call clear() (nothing to
# cut short when turning audio back on).
runner.set_mute_output(False)
assert runner._mute_output is False, "T2 FAILED: set_mute_output(False) didn't clear the flag"
assert sink.clear_calls == 1, f"T2 FAILED: unmuting should not call clear(), calls={sink.clear_calls}"
print("OK T2: set_mute_output(False) unmutes without clearing the sink")

# T3: a sink without clear() (matches the AudioSink Protocol, which only
# requires play()) must not raise when muting.
class MinimalSink:
    def play(self, pcm: bytes) -> None:
        pass


runner2 = TranslationRunner(
    api_key="fake",
    region="eu",
    source_lang="pl",
    target_lang="en",
    source=FakeSource(),
    sink=MinimalSink(),
)
runner2.set_mute_output(True)  # must not raise
assert runner2._mute_output is True
print("OK T3: set_mute_output(True) on a sink without clear() doesn't raise")

print("ALL TESTS PASSED")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python.exe "<scratchpad>/test_live_mute_toggle.py"`
Expected: `AttributeError: 'TranslationRunner' object has no attribute 'set_mute_output'`

- [ ] **Step 3: Write minimal implementation**

In `app/translation_session.py`, insert immediately after `request_change_mic_device` (after the closing of the method at line 160, i.e. right before the blank line preceding `def request_seek` at line 162):

```python
    def set_mute_output(self, muted: bool) -> None:
        """Live-toggles subtitles-only mode (see __init__'s mute_output for why
        this never touches the server/billing -- it only routes/withholds
        already-generated audio locally). Same "plain attribute write, safe
        without loop marshaling" reasoning as set_mic_gain/set_gate_threshold
        on MicStream: this is read once per Audio event in run()'s receive
        loop, no asyncio scheduling needed.

        Muting also clears the sink's own playback queue (like request_seek
        does) so already-queued audio is cut immediately instead of trailing
        off for up to the sink's own backlog cap.
        """
        self._mute_output = muted
        if muted and hasattr(self._sink, "clear"):
            self._sink.clear()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/Scripts/python.exe "<scratchpad>/test_live_mute_toggle.py"`
Expected: `ALL TESTS PASSED`

- [ ] **Step 5: Commit**

```bash
git add app/translation_session.py
git commit -m "Add TranslationRunner.set_mute_output() for live subtitles-only toggling"
```

---

### Task 2: `SessionWorker.set_subtitles_only()`

**Files:**
- Modify: `app/gui.py` (add method right after `set_gate_threshold`, i.e. after line 461, before the blank lines preceding `_STATE_LABELS` at line 464)
- Test: `<scratchpad>/test_session_worker_subtitles_only.py`

**Interfaces:**
- Consumes: `TranslationRunner.set_mute_output(muted: bool) -> None` (Task 1) via `self._runner`.
- Produces: `SessionWorker.set_subtitles_only(muted: bool) -> None` — Task 3's GUI wiring calls this exact method name/signature.

- [ ] **Step 1: Write the failing test**

Create `<scratchpad>/test_session_worker_subtitles_only.py`:

```python
"""SessionWorker.set_subtitles_only(): forwards to the active runner's
set_mute_output() when a runner exists, safe no-op before Start (no runner
yet)."""
import sys

sys.path.insert(0, r"E:\STS")

import app.gui as guimod


class FakeRunner:
    def __init__(self):
        self.mute_calls = []

    def set_mute_output(self, muted: bool) -> None:
        self.mute_calls.append(muted)


# T1: no runner yet (before Start) -- must not raise.
worker = guimod.SessionWorker.__new__(guimod.SessionWorker)
worker._runner = None
worker.set_subtitles_only(True)  # must not raise
print("OK T1: set_subtitles_only() before Start is a safe no-op")

# T2: with an active runner, forwards the value unchanged.
worker._runner = FakeRunner()
worker.set_subtitles_only(True)
assert worker._runner.mute_calls == [True], f"T2 FAILED: {worker._runner.mute_calls}"
worker.set_subtitles_only(False)
assert worker._runner.mute_calls == [True, False], f"T2 FAILED: {worker._runner.mute_calls}"
print("OK T2: set_subtitles_only() forwards to the active runner's set_mute_output()")

print("ALL TESTS PASSED")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python.exe "<scratchpad>/test_session_worker_subtitles_only.py"`
Expected: `AttributeError: 'SessionWorker' object has no attribute 'set_subtitles_only'`

- [ ] **Step 3: Write minimal implementation**

In `app/gui.py`, insert immediately after `set_gate_threshold` (after line 461, before the two blank lines that precede `_STATE_LABELS` at line 464):

```python
    def set_subtitles_only(self, muted: bool) -> None:
        # Plain passthrough -- TranslationRunner.set_mute_output() is itself
        # a thread-safe direct call (see its docstring), no _call_on_loop
        # marshaling needed, same as set_mic_gain/set_gate_threshold above.
        if self._runner is not None:
            self._runner.set_mute_output(muted)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/Scripts/python.exe "<scratchpad>/test_session_worker_subtitles_only.py"`
Expected: `ALL TESTS PASSED`

- [ ] **Step 5: Commit**

```bash
git add app/gui.py
git commit -m "Add SessionWorker.set_subtitles_only() passthrough"
```

---

### Task 3: GUI wiring — unlock checkbox, live toggle handler, shared warning helper

**Files:**
- Modify: `app/gui.py`:
  - Remove `self.subtitles_only_check,` from `_config_widgets` (line 744)
  - Add `self.subtitles_only_check.toggled.connect(self._on_subtitles_only_toggled)` near the checkbox's construction (after line 618, where `form.addRow("", self.subtitles_only_check)` is)
  - Extract the feedback-loop warning (currently inline in `_on_start_stop`, lines 916-939) into a new method `_confirm_feedback_loop_risk(self) -> bool`, and call it from both `_on_start_stop` and the new `_on_subtitles_only_toggled`
- Test: `<scratchpad>/test_gui_live_subtitles_toggle.py`

**Interfaces:**
- Consumes: `SessionWorker.set_subtitles_only(muted: bool) -> None` (Task 2); `is_virtual_cable_name` (already imported in `app/gui.py` from `.audio_io`).
- Produces: `MainWindow._confirm_feedback_loop_risk(self) -> bool` and `MainWindow._on_subtitles_only_toggled(self, checked: bool) -> None` — no later task depends on these, but they must exist with these exact names for the test in this task.

- [ ] **Step 1: Write the failing test**

Create `<scratchpad>/test_gui_live_subtitles_toggle.py`:

```python
"""GUI wiring for the live "Tylko napisy" toggle: checkbox stays enabled
during a session, unchecking it (turning audio back ON) mid-session under
risky conditions (mic active, non-cable output) shows the feedback-loop
warning and reverts on "No", and toggling calls
SessionWorker.set_subtitles_only()."""
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock

sys.path.insert(0, r"E:\STS")

tmp_dir = Path(tempfile.mkdtemp())

import app.config as config
with patch.object(config, "CONFIG_DIR", tmp_dir), \
     patch.object(config, "OVERLAY_SETTINGS_PATH", tmp_dir / "overlay_settings.json"):

    from PySide6.QtWidgets import QApplication, QMessageBox
    app_qt = QApplication.instance() or QApplication([])

    import app.gui as guimod

    class FakeCreds:
        api_key = "fake-key"
        region = "eu"

    class FakeSessionWorker:
        def __init__(self, **kwargs):
            self.state_changed = MagicMock(connect=lambda *a, **k: None)
            self.transcript_received = MagicMock(connect=lambda *a, **k: None)
            self.error_occurred = MagicMock(connect=lambda *a, **k: None)
            self.finished = MagicMock(connect=lambda *a, **k: None)
            self.subtitles_only_calls = []

        def start(self):
            pass

        def stop(self):
            pass

        def set_subtitles_only(self, muted):
            self.subtitles_only_calls.append(muted)

    def make_window():
        w = guimod.MainWindow()
        w.mic_combo.clear()
        w.mic_combo.addItem("Mic A", 10)
        w.output_combo.clear()
        w.output_combo.addItem("Speakers (Realtek)", 0)  # NOT a virtual-cable name
        return w

    def start_session(w):
        with patch.object(config, "load_credentials", return_value=FakeCreds()), \
             patch.object(guimod, "SessionWorker", FakeSessionWorker), \
             patch("threading.Thread") as mock_thread_cls, \
             patch.object(guimod.QMessageBox, "warning", return_value=guimod.QMessageBox.StandardButton.Yes):
            mock_thread_cls.return_value = MagicMock(is_alive=lambda: False)
            w._on_start_stop()  # Start; the Start-time warning is accepted (Yes)

    # T1: checkbox stays enabled during a session (unlike source_lang_combo).
    w1 = make_window()
    start_session(w1)
    assert w1.subtitles_only_check.isEnabled(), "T1 FAILED: subtitles_only_check should stay enabled"
    assert not w1.source_lang_combo.isEnabled(), "T1 FAILED: source_lang_combo should still be locked"
    print("OK T1: subtitles_only_check stays enabled during a session")

    # T2: unchecking (enabling audio) mid-session under risky conditions
    # (mic active, non-cable output) shows the warning; answering No reverts
    # the checkbox and does NOT call set_subtitles_only().
    w1.subtitles_only_check.setChecked(True)  # start muted
    w1._worker.subtitles_only_calls.clear()
    with patch.object(guimod.QMessageBox, "warning", return_value=guimod.QMessageBox.StandardButton.No) as mock_warn:
        w1.subtitles_only_check.setChecked(False)  # try to turn audio ON
    assert mock_warn.called, "T2 FAILED: expected the feedback-loop warning to trigger"
    assert w1.subtitles_only_check.isChecked() is True, "T2 FAILED: checkbox should revert to checked after 'No'"
    assert w1._worker.subtitles_only_calls == [], f"T2 FAILED: {w1._worker.subtitles_only_calls}"
    print("OK T2: declining the warning reverts the checkbox and doesn't call the worker")

    # T3: answering Yes to the warning lets it through and calls the worker.
    with patch.object(guimod.QMessageBox, "warning", return_value=guimod.QMessageBox.StandardButton.Yes) as mock_warn:
        w1.subtitles_only_check.setChecked(False)
    assert mock_warn.called, "T3 FAILED: expected the warning to trigger again"
    assert w1.subtitles_only_check.isChecked() is False, "T3 FAILED: checkbox should stay unchecked after 'Yes'"
    assert w1._worker.subtitles_only_calls == [False], f"T3 FAILED: {w1._worker.subtitles_only_calls}"
    print("OK T3: accepting the warning lets the toggle through and calls set_subtitles_only(False)")

    # T4: muting (checking the box) never shows the warning, regardless of
    # output device, and always calls the worker.
    with patch.object(guimod.QMessageBox, "warning") as mock_warn:
        w1.subtitles_only_check.setChecked(True)
    assert not mock_warn.called, "T4 FAILED: muting should never show the feedback-loop warning"
    assert w1._worker.subtitles_only_calls == [False, True], f"T4 FAILED: {w1._worker.subtitles_only_calls}"
    print("OK T4: muting mid-session never warns and always forwards to the worker")

    # T5: with a virtual-cable-named output, unchecking never warns.
    w5 = make_window()
    w5.output_combo.clear()
    w5.output_combo.addItem("CABLE Input (VB-Audio Virtual Cable)", 0)
    start_session(w5)
    w5.subtitles_only_check.setChecked(True)
    w5._worker.subtitles_only_calls.clear()
    with patch.object(guimod.QMessageBox, "warning") as mock_warn:
        w5.subtitles_only_check.setChecked(False)
    assert not mock_warn.called, "T5 FAILED: a virtual-cable output should never trigger the warning"
    assert w5._worker.subtitles_only_calls == [False], f"T5 FAILED: {w5._worker.subtitles_only_calls}"
    print("OK T5: a virtual-cable output never triggers the warning")

    # T6: toggling before Start (no worker yet) is a safe no-op, no warning.
    w6 = make_window()
    with patch.object(guimod.QMessageBox, "warning") as mock_warn:
        w6.subtitles_only_check.setChecked(True)
        w6.subtitles_only_check.setChecked(False)
    assert not mock_warn.called, "T6 FAILED: toggling before Start should never warn"
    print("OK T6: toggling before Start is a safe no-op")

print("ALL TESTS PASSED")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python.exe "<scratchpad>/test_gui_live_subtitles_toggle.py"`
Expected: FAIL at T1 (`AssertionError: T1 FAILED: subtitles_only_check should stay enabled`), since the checkbox is still in `_config_widgets` and gets disabled by `_set_config_enabled(False)`.

- [ ] **Step 3: Write minimal implementation**

In `app/gui.py`:

1. Remove the checkbox from `_config_widgets` — change (around line 739-751):

```python
        self._config_widgets = [
            self.settings_btn,
            self.mode_combo,
            self.file_btn,
            self.output_combo,
            self.subtitles_only_check,
            self.source_lang_combo,
            self.target_lang_combo,
            self.voice_combo,
            self.voice_custom_edit,
            self.manage_voices_btn,
            self.refresh_devices_btn,
        ]
```

to:

```python
        # subtitles_only_check is deliberately NOT in this list -- like
        # mic_combo, it stays live-toggleable during a running session (see
        # _on_subtitles_only_toggled): muting/unmuting playback is purely
        # local, never touches the server session.
        self._config_widgets = [
            self.settings_btn,
            self.mode_combo,
            self.file_btn,
            self.output_combo,
            self.source_lang_combo,
            self.target_lang_combo,
            self.voice_combo,
            self.voice_custom_edit,
            self.manage_voices_btn,
            self.refresh_devices_btn,
        ]
```

2. Connect the toggle signal right after the checkbox is added to the form (after line 618, `form.addRow("", self.subtitles_only_check)`):

```python
        self.subtitles_only_check.toggled.connect(self._on_subtitles_only_toggled)
```

3. Replace the inline warning block inside `_on_start_stop` (lines 916-939):

```python
        # Loop-protection: a live mic can physically pick this app's own
        # output back up (e.g. it's routed to real speakers instead of a
        # virtual cable) and re-translate it endlessly -- confirmed live:
        # the exact same sentence repeating in the log over and over until
        # Stop was clicked. File-only mode never opens a mic (see
        # SessionWorker.start()), so it can't feed back this way -- and
        # neither can subtitles-only mode, since no audio is ever played.
        if mic_active and not self.subtitles_only_check.isChecked():
            output_name = self.output_combo.currentText()
            if not is_virtual_cable_name(output_name):
                answer = QMessageBox.warning(
                    self,
                    "Wybrane wyjście może spowodować pętlę sprzężenia",
                    f'Wybrane urządzenie wyjściowe ("{output_name}") nie wygląda na wirtualny '
                    "kabel audio (VB-Cable / BlackHole).\n\n"
                    "Jeśli mikrofon może usłyszeć ten dźwięk (np. przez głośniki), tłumaczenie "
                    "może wpaść w pętlę sprzężenia zwrotnego — to samo zdanie tłumaczone w kółko, "
                    "aż do ręcznego zatrzymania.\n\n"
                    "Kontynuować mimo to?",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    QMessageBox.StandardButton.No,
                )
                if answer == QMessageBox.StandardButton.No:
                    return
```

with:

```python
        # Loop-protection: see _confirm_feedback_loop_risk's docstring.
        if not self._confirm_feedback_loop_risk():
            return
```

4. Add the two new methods. Place `_confirm_feedback_loop_risk` right before `_on_start_stop` (i.e. right before the `def _on_start_stop(self) -> None:` line), and `_on_subtitles_only_toggled` right after `_on_start_stop` ends (i.e. right before `def _on_state`):

```python
    def _confirm_feedback_loop_risk(self) -> bool:
        """A live mic can physically pick this app's own output back up
        (e.g. it's routed to real speakers instead of a virtual cable) and
        re-translate it endlessly -- confirmed live: the exact same sentence
        repeating in the log over and over until Stop was clicked. Shared
        between the Start button and the live "Tylko napisy" toggle (turning
        audio back on mid-session carries the same risk). Returns True if
        it's safe to proceed (not risky, or the user chose to continue
        anyway), False if the user declined.
        """
        mode_idx = self.mode_combo.currentIndex()
        mic_active = mode_idx in (0, 2)
        if not mic_active or self.subtitles_only_check.isChecked():
            return True
        output_name = self.output_combo.currentText()
        if is_virtual_cable_name(output_name):
            return True
        answer = QMessageBox.warning(
            self,
            "Wybrane wyjście może spowodować pętlę sprzężenia",
            f'Wybrane urządzenie wyjściowe ("{output_name}") nie wygląda na wirtualny '
            "kabel audio (VB-Cable / BlackHole).\n\n"
            "Jeśli mikrofon może usłyszeć ten dźwięk (np. przez głośniki), tłumaczenie "
            "może wpaść w pętlę sprzężenia zwrotnego — to samo zdanie tłumaczone w kółko, "
            "aż do ręcznego zatrzymania.\n\n"
            "Kontynuować mimo to?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        return answer == QMessageBox.StandardButton.Yes
```

```python
    def _on_subtitles_only_toggled(self, checked: bool) -> None:
        # Turning audio back ON (unchecking) mid-session under risky
        # conditions needs the same confirmation Start already requires --
        # but only mid-session: before Start, _on_start_stop's own call to
        # _confirm_feedback_loop_risk() already covers it, so warning here
        # too (with no worker yet) would double up on the same check.
        if self._worker is not None and not checked and not self._confirm_feedback_loop_risk():
            self.subtitles_only_check.blockSignals(True)
            self.subtitles_only_check.setChecked(True)
            self.subtitles_only_check.blockSignals(False)
            return
        if self._worker is not None:
            self._worker.set_subtitles_only(checked)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/Scripts/python.exe "<scratchpad>/test_gui_live_subtitles_toggle.py"`
Expected: `ALL TESTS PASSED`

- [ ] **Step 5: Commit**

```bash
git add app/gui.py
git commit -m "Unlock \"Tylko napisy\" for live toggling mid-session"
```

---

### Task 4: Full regression + real-hardware/live sanity check

**Files:** none (verification only)

**Interfaces:**
- Consumes: everything from Tasks 1-3.
- Produces: nothing (terminal task).

- [ ] **Step 1: Run the existing scratchpad regression suite**

Run every `test_*.py` in `<scratchpad>` except the known slow/hardware-only ones (`test_2h_file_decode_memory.py`, `test_2h_gui_memory_growth.py`, `test_real_mic_switch_hardware.py`), plus the three new tests from Tasks 1-3. Expected: only the two known, pre-existing, unrelated failures (`test_change_voice.py` T4, `test_new_features.py` console-encoding) — everything else passes.

- [ ] **Step 2: Real-hardware/live sanity check**

Using a real API key and a real input/output device (mirrors the standard this app already holds itself to — a prior investigation into audio latency only caught a real bug via a real device, not mocks):
1. Start a Mikrofon session with a real mic and a non-virtual-cable output device, subtitles-only unchecked.
2. Speak a short sentence; confirm audio plays.
3. While the session is running, check "Tylko napisy" — confirm audio stops immediately (no trailing audio) and transcripts keep appearing.
4. Uncheck "Tylko napisy" — confirm the feedback-loop warning dialog appears; click "Nie" and confirm the checkbox reverts to checked and audio stays off.
5. Uncheck it again and click "Tak" — confirm audio resumes.
6. Switch the output device to a virtual cable (or rename via a fake match) and repeat step 4 — confirm no warning appears this time.

- [ ] **Step 3: Update README**

Add a short note under the existing "Tylko napisy" documentation (`README.md`) that it can now be toggled during a running session (mirroring how the live-mic-switching feature is documented), including the mid-session warning behavior.

- [ ] **Step 4: Commit**

```bash
git add README.md
git commit -m "Document live \"Tylko napisy\" toggling in README"
```

---

## Self-Review Notes

- **Spec coverage:** All three spec components (`TranslationRunner.set_mute_output`, `SessionWorker.set_subtitles_only`, GUI wiring incl. warning reuse and `clear()` behavior) map to Tasks 1-3. Testing section maps to Task 4. Out-of-scope items (no `run()` changes, no interaction with loop-repeat auto-pause) are respected — no task touches either.
- **Placeholder scan:** No TBD/TODO; every step has literal code or a concrete, enumerable manual procedure (Task 4 Step 2).
- **Type consistency:** `set_mute_output(muted: bool) -> None` (Task 1) is called identically by `set_subtitles_only(muted: bool) -> None` (Task 2), which is called identically by `_on_subtitles_only_toggled` (Task 3) — same parameter name and type throughout.
