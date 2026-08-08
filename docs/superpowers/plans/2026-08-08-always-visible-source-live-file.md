# Always-Visible Source + Live File Add/Change/Remove Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove the "Źródło" mode selector (mic is always active; the file is a fully optional, always-editable add-on, before Start or mid-session), a freshly set file never autoplays, and pause control splits into two independent buttons (whole-session vs. file-only).

**Architecture:** `MixedSource` (app/audio_io.py) gains an optional, live-replaceable file source with a new `set_file()` coroutine that tears down and rebuilds its file pump task. Every session — even mic-only — now constructs a `MixedSource`, which is what makes live file add/change/remove possible at all. `TranslationRunner`/`SessionWorker` get a `request_set_file`/`set_file` pair following the existing `change_mic_device` pattern. The GUI drops `mode_combo`/`SessionMode` entirely, always shows mic + file controls, and replaces the old mode-dependent single Pauza button with two independent ones.

**Tech Stack:** Python, PySide6 (Qt), asyncio, sounddevice, palabra-ai SDK. No new dependencies.

## Global Constraints

- Match this project's existing test convention: standalone scripts under the scratchpad directory (`C:\Users\dszwe\AppData\Local\Temp\claude\e--STS\c6cd0ce0-2621-4fa4-9540-d709192a0f82\scratchpad\`), run directly with `.venv/Scripts/python.exe <script>.py`, using plain `assert` + `print("OK ...")` / `print("ALL TESTS PASSED")` — this codebase does not use pytest.
- Preserve existing Polish-language UI strings and conventions.
- A newly active file (at Start, or added/changed mid-session) always starts paused — no exceptions.
- The two pause controls never share state: "Pauza" is always whole-session; "Pauza pliku" is always file-only.
- Spec: `docs/superpowers/specs/2026-08-08-always-visible-source-live-file-design.md`.

---

### Task 1: `MixedSource` — file becomes optional

**Files:**
- Modify: `app/audio_io.py` (`MixedSource.__init__`, `__enter__`, `__exit__`, `pause_file`, `resume_file`, `seek`, `position_ms`, `total_ms`, `chunks`)
- Test: `<scratchpad>/test_mixed_source_optional_file.py`

**Interfaces:**
- Produces: `MixedSource(mic: MicStream, file: FileStream | None = None)` — Task 2 builds on this constructor accepting `file=None`.

- [ ] **Step 1: Write the failing test**

Create `<scratchpad>/test_mixed_source_optional_file.py`:

```python
"""MixedSource with file=None: mixes real mic audio with silence (no file
side), and every file-related method/property is a safe no-op/zero."""
import asyncio
import struct
import sys
import threading

sys.path.insert(0, r"E:\STS")

import numpy as np
import app.audio_io as audio_io


class FakeMicStream:
    def __init__(self):
        self._value = 1000
        self._stop = threading.Event()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self._stop.set()

    def switch_device(self, new_device):
        pass

    async def chunks(self):
        n = audio_io.CHUNK_SAMPLES
        while not self._stop.is_set():
            yield struct.pack(f"<{n}h", *([self._value] * n))
            await asyncio.sleep(audio_io.CHUNK_MS / 1000)


async def main():
    mic = FakeMicStream()
    mixed = audio_io.MixedSource(mic, file=None)

    # T1: no-op file methods don't raise with no file.
    mixed.pause_file()
    mixed.resume_file()
    mixed.seek(1000.0)
    assert mixed.position_ms == 0.0, f"T1 FAILED: {mixed.position_ms}"
    assert mixed.total_ms == 0.0, f"T1 FAILED: {mixed.total_ms}"
    print("OK T1: file-related no-ops don't raise, position/total are 0.0 with no file")

    # T2: __enter__/__exit__ work with no file to enter/exit.
    with mixed:
        gen = mixed.chunks()
        chunk = await asyncio.wait_for(gen.__anext__(), timeout=3.0)
        val = np.frombuffer(chunk, dtype=np.int16)[0]
        assert val == 1000, f"T2 FAILED: expected mic-only (1000) with no file, got {val}"
        print(f"OK T2: mixing with file=None yields mic-only audio -> {val}")

    print("ALL TESTS PASSED")


asyncio.run(main())
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python.exe "<scratchpad>/test_mixed_source_optional_file.py"`
Expected: `TypeError: MixedSource.__init__() missing 1 required positional argument: 'file'`

- [ ] **Step 3: Write minimal implementation**

In `app/audio_io.py`, change `MixedSource`:

```python
    def __init__(self, mic: MicStream, file: FileStream | None = None):
        self._mic = mic
        self._file = file
        # Small bound: both sub-sources already self-pace to ~1 chunk per
        # CHUNK_MS, so these stay near-empty in steady state -- this is just
        # a safety cap against unbounded growth if either stalls, not a
        # buffer this design relies on filling up.
        self._mic_q: asyncio.Queue[bytes] = asyncio.Queue(maxsize=4)
        self._file_q: asyncio.Queue[bytes] = asyncio.Queue(maxsize=4)
        self._file_task: asyncio.Task | None = None

    def __enter__(self) -> "MixedSource":
        self._mic.__enter__()
        if self._file is not None:
            try:
                self._file.__enter__()
            except Exception:
                self._mic.__exit__(None, None, None)
                raise
        return self

    def __exit__(self, *exc_info) -> None:
        if self._file is not None:
            self._file.__exit__(*exc_info)
        self._mic.__exit__(*exc_info)

    def pause_file(self) -> None:
        if self._file is not None:
            self._file.pause()

    def resume_file(self) -> None:
        if self._file is not None:
            self._file.resume()

    def seek(self, position_ms: float) -> None:
        if self._file is not None:
            self._file.seek(position_ms)

    def switch_device(self, new_device: int | None) -> None:
        self._mic.switch_device(new_device)

    @property
    def position_ms(self) -> float:
        return self._file.position_ms if self._file is not None else 0.0

    @property
    def total_ms(self) -> float:
        return self._file.total_ms if self._file is not None else 0.0
```

Change `chunks()`'s task setup and teardown from:

```python
        mic_task = asyncio.create_task(self._pump(self._mic, self._mic_q))
        file_task = asyncio.create_task(self._pump(self._file, self._file_q))
        silence = bytes(CHUNK_BYTES)
        pacer = RealtimePacer(CHUNK_MS)
        try:
            while True:
                await pacer.tick()
                try:
                    mic_chunk = self._mic_q.get_nowait()
                except asyncio.QueueEmpty:
                    mic_chunk = silence
                try:
                    file_chunk = self._file_q.get_nowait()
                except asyncio.QueueEmpty:
                    file_chunk = silence
                yield _mix_pcm(mic_chunk, file_chunk)
        finally:
            mic_task.cancel()
            file_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await mic_task
            with contextlib.suppress(asyncio.CancelledError):
                await file_task
```

to:

```python
        mic_task = asyncio.create_task(self._pump(self._mic, self._mic_q))
        if self._file is not None:
            self._file_task = asyncio.create_task(self._pump(self._file, self._file_q))
        silence = bytes(CHUNK_BYTES)
        pacer = RealtimePacer(CHUNK_MS)
        try:
            while True:
                await pacer.tick()
                try:
                    mic_chunk = self._mic_q.get_nowait()
                except asyncio.QueueEmpty:
                    mic_chunk = silence
                try:
                    file_chunk = self._file_q.get_nowait()
                except asyncio.QueueEmpty:
                    file_chunk = silence
                yield _mix_pcm(mic_chunk, file_chunk)
        finally:
            mic_task.cancel()
            if self._file_task is not None:
                self._file_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await mic_task
            if self._file_task is not None:
                with contextlib.suppress(asyncio.CancelledError):
                    await self._file_task
```

(`_pump` and `_mix_pcm` are unchanged.)

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/Scripts/python.exe "<scratchpad>/test_mixed_source_optional_file.py"`
Expected: `ALL TESTS PASSED`

- [ ] **Step 5: Re-run existing MixedSource regression tests to confirm no behavior change with a file present**

`.venv/Scripts/python.exe "<scratchpad>/test_mixed_source.py"`
`.venv/Scripts/python.exe "<scratchpad>/test_mixed_source_real_hardware.py"` (if a real non-cable input device is available in this environment; skip only if it fails purely for lack of hardware, not for any other reason)

Both must still print `ALL TESTS PASSED`.

- [ ] **Step 6: Commit**

```bash
git add app/audio_io.py
git commit -m "Make MixedSource's file source optional"
```

---

### Task 2: `MixedSource.set_file()` — live add/change/remove

**Files:**
- Modify: `app/audio_io.py` (`MixedSource`)
- Test: `<scratchpad>/test_mixed_source_set_file.py`

**Interfaces:**
- Consumes: `MixedSource(mic, file=None)` (Task 1).
- Produces: `MixedSource.set_file(self, file: FileStream | None) -> None` (async) — Task 4 (`TranslationRunner.request_set_file`) calls this exact method.

- [ ] **Step 1: Write the failing test**

Create `<scratchpad>/test_mixed_source_set_file.py`:

```python
"""MixedSource.set_file(): live add (None -> file), change (file -> a
different file, old one's pump task actually cancelled), and remove
(file -> None), all while chunks() keeps running and the mic side never
stalls."""
import asyncio
import struct
import sys
import threading
import wave
from pathlib import Path

sys.path.insert(0, r"E:\STS")

import numpy as np
import app.audio_io as audio_io


class FakeMicStream:
    def __init__(self):
        self._value = 1000
        self._stop = threading.Event()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self._stop.set()

    def switch_device(self, new_device):
        pass

    async def chunks(self):
        n = audio_io.CHUNK_SAMPLES
        while not self._stop.is_set():
            yield struct.pack(f"<{n}h", *([self._value] * n))
            await asyncio.sleep(audio_io.CHUNK_MS / 1000)


def make_wav(path: Path, value: int, seconds: float = 2.0) -> None:
    rate = audio_io.RATE
    n_samples = int(rate * seconds)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(struct.pack(f"<{n_samples}h", *([value] * n_samples)))


async def main():
    import tempfile

    tmp_dir = Path(tempfile.mkdtemp())
    wav_a = tmp_dir / "a.wav"
    wav_b = tmp_dir / "b.wav"
    make_wav(wav_a, 5000)
    make_wav(wav_b, 9000)

    mic = FakeMicStream()
    mixed = audio_io.MixedSource(mic, file=None)

    with mixed:
        gen = mixed.chunks()

        # T1: mic-only baseline.
        chunk = await asyncio.wait_for(gen.__anext__(), timeout=3.0)
        val = np.frombuffer(chunk, dtype=np.int16)[0]
        assert val == 1000, f"T1 FAILED: expected mic-only 1000, got {val}"
        print(f"OK T1: mic-only baseline -> {val}")

        # T2: live add -- set_file(file_a) while running; file starts
        # UNPAUSED here (this test exercises the mixing mechanism itself;
        # the "always starts paused" policy is enforced one layer up, in
        # TranslationRunner._do_set_file -- see Task 4).
        file_a = audio_io.FileStream(str(wav_a))
        await mixed.set_file(file_a)
        got_mixed = False
        for _ in range(30):
            chunk = await asyncio.wait_for(gen.__anext__(), timeout=3.0)
            val = np.frombuffer(chunk, dtype=np.int16)[0]
            if val == 6000:  # mic(1000) + file_a(5000)
                got_mixed = True
                break
        assert got_mixed, "T2 FAILED: expected mic+file_a (6000) after live add"
        print("OK T2: live set_file() adds a file to an already-running mic-only mix")

        # T3: live change -- set_file(file_b) replaces file_a; only file_b's
        # value should appear from now on, never file_a's again.
        file_b = audio_io.FileStream(str(wav_b))
        await mixed.set_file(file_b)
        got_new = False
        for _ in range(30):
            chunk = await asyncio.wait_for(gen.__anext__(), timeout=3.0)
            val = np.frombuffer(chunk, dtype=np.int16)[0]
            assert val != 6000, "T3 FAILED: old file (file_a) still contributing after change"
            if val == 10000:  # mic(1000) + file_b(9000)
                got_new = True
                break
        assert got_new, "T3 FAILED: expected mic+file_b (10000) after live change"
        print("OK T3: live set_file() swaps to a new file, old one stops contributing")

        # T4: live remove -- set_file(None) drops back to mic-only, mic never
        # stalled through any of the above.
        await mixed.set_file(None)
        got_mic_only = False
        for _ in range(30):
            chunk = await asyncio.wait_for(gen.__anext__(), timeout=3.0)
            val = np.frombuffer(chunk, dtype=np.int16)[0]
            if val == 1000:
                got_mic_only = True
                break
        assert got_mic_only, "T4 FAILED: expected mic-only (1000) after live remove"
        print("OK T4: live set_file(None) removes the file, mic-only mixing continues")

    print("ALL TESTS PASSED")


asyncio.run(main())
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python.exe "<scratchpad>/test_mixed_source_set_file.py"`
Expected: `AttributeError: 'MixedSource' object has no attribute 'set_file'`

- [ ] **Step 3: Write minimal implementation**

In `app/audio_io.py`, add this method to `MixedSource` (place it right after `chunks()`):

```python
    async def set_file(self, file: FileStream | None) -> None:
        """Live-swaps the file source: cancels and awaits any existing file
        pump task, drops any file audio still queued, closes the old
        FileStream (if any), then -- if given a new one -- enters it and
        starts a fresh pump task for it. Must run on this source's own
        asyncio loop; chunks() keeps running concurrently on the same loop,
        so no locking is needed (same reasoning the rest of this class
        already relies on for its single-event-loop cooperative model).
        """
        if self._file_task is not None:
            self._file_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._file_task
            self._file_task = None
        while True:
            try:
                self._file_q.get_nowait()
            except asyncio.QueueEmpty:
                break
        if self._file is not None:
            self._file.__exit__(None, None, None)
        self._file = file
        if file is not None:
            file.__enter__()
            self._file_task = asyncio.create_task(self._pump(self._file, self._file_q))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/Scripts/python.exe "<scratchpad>/test_mixed_source_set_file.py"`
Expected: `ALL TESTS PASSED`

- [ ] **Step 5: Commit**

```bash
git add app/audio_io.py
git commit -m "Add MixedSource.set_file() for live file add/change/remove"
```

---

### Task 3: `SessionWorker.start()` always builds `MixedSource`; drop `include_mic`

**Files:**
- Modify: `app/gui.py` (`SessionConfig`, `SessionWorker.__init__`, `SessionWorker.start()`, `SessionWorker.position_ms`/`total_ms`)
- Test: `<scratchpad>/test_session_worker_always_mixed.py`

**Interfaces:**
- Consumes: `MixedSource(mic, file=None)` (Task 1).
- Produces: `SessionWorker._mixed_source` is always a `MixedSource` once `start()` has begun (never `None` after that point, and `self._file_source`/the three-way branch are gone) — Task 4's `SessionWorker.set_file()` relies on `self._mixed_source` always being the right kind of object to forward to.

- [ ] **Step 1: Write the failing test**

Create `<scratchpad>/test_session_worker_always_mixed.py`:

```python
"""SessionConfig no longer has include_mic; SessionWorker.__init__ doesn't
choke on its absence."""
import sys

sys.path.insert(0, r"E:\STS")

import app.gui as guimod

# T1: SessionConfig has no include_mic field.
config = guimod.SessionConfig(
    api_key="k",
    source_lang="pl",
    target_lang="en",
    mic_device=None,
    output_device=None,
    file_path=None,
)
assert not hasattr(config, "include_mic"), "T1 FAILED: include_mic should be gone from SessionConfig"
print("OK T1: SessionConfig has no include_mic field")

# T2: SessionWorker construction still works with the trimmed config.
worker = guimod.SessionWorker(config)
assert worker._file_path is None
print("OK T2: SessionWorker(config) still constructs fine without include_mic")

print("ALL TESTS PASSED")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python.exe "<scratchpad>/test_session_worker_always_mixed.py"`
Expected: T1 fails — `AssertionError: T1 FAILED: include_mic should be gone from SessionConfig` (it's still there).

- [ ] **Step 3: Write minimal implementation**

In `app/gui.py`, remove the `include_mic: bool = True` field from `SessionConfig`:

```python
@dataclass
class SessionConfig:
    api_key: str
    source_lang: str
    target_lang: str
    mic_device: int | None
    output_device: int | None
    file_path: str | None
    mic_gain: float = 1.0
    mic_gate_threshold: float = 0.0
    voice_id: str | None = None
    voice_cloning: bool = False
    subtitles_only: bool = False
```

In `SessionWorker.__init__`, remove the `self._include_mic = config.include_mic` line and its preceding comment block (the `# include_mic distinguishes ...` comment):

```python
        self._api_key = config.api_key
        self._source_lang = config.source_lang
        self._target_lang = config.target_lang
        self._mic_device = config.mic_device
        self._output_device = config.output_device
        self._file_path = config.file_path
        self._initial_mic_gain = config.mic_gain
        self._initial_gate_threshold = config.mic_gate_threshold
        self._voice_id = config.voice_id
        self._voice_cloning = config.voice_cloning
        self._subtitles_only = config.subtitles_only
```

Remove `self._file_source: FileStream | None = None` from `__init__` (no longer needed — see the property change below). Keep `self._mic_source: MicStream | None = None` and `self._mixed_source: MixedSource | None = None`.

Change the `position_ms`/`total_ms` properties from:

```python
    @property
    def position_ms(self) -> float:
        return self._file_source.position_ms if self._file_source else 0.0

    @property
    def total_ms(self) -> float:
        return self._file_source.total_ms if self._file_source else 0.0
```

to:

```python
    @property
    def position_ms(self) -> float:
        return self._mixed_source.position_ms if self._mixed_source else 0.0

    @property
    def total_ms(self) -> float:
        return self._mixed_source.total_ms if self._mixed_source else 0.0
```

(`MixedSource.position_ms`/`total_ms` from Task 1 already return `0.0` when there's no file, so this stays correct in every case, including after a live file swap -- unlike the old `self._file_source` reference, which would go stale.)

Change the source-construction branch in `start()` from:

```python
            if self._include_mic and self._file_path is not None:
                mic = MicStream(device=self._mic_device)
                mic.set_gain(self._initial_mic_gain)
                mic.set_gate_threshold(self._initial_gate_threshold)
                self._mic_source = mic
                file = FileStream(self._file_path)
                self._file_source = file
                source_cm = MixedSource(mic, file)
                self._mixed_source = source_cm
            elif self._include_mic:
                source_cm = MicStream(device=self._mic_device)
                source_cm.set_gain(self._initial_mic_gain)
                source_cm.set_gate_threshold(self._initial_gate_threshold)
                self._mic_source = source_cm
            else:
                source_cm = FileStream(self._file_path)
                self._file_source = source_cm
```

to:

```python
            mic = MicStream(device=self._mic_device)
            mic.set_gain(self._initial_mic_gain)
            mic.set_gate_threshold(self._initial_gate_threshold)
            self._mic_source = mic
            file = None
            if self._file_path is not None:
                file = FileStream(self._file_path)
                file.pause()  # never autoplay a file that's active at Start
            source_cm = MixedSource(mic, file)
            self._mixed_source = source_cm
```

(Every session now goes through the same one path. `file.pause()` here is what makes Task 6's "never autoplay" requirement hold at Start; Task 4 handles the same requirement for a file added/changed mid-session.)

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/Scripts/python.exe "<scratchpad>/test_session_worker_always_mixed.py"`
Expected: `ALL TESTS PASSED`

- [ ] **Step 5: Re-run existing Start-flow regression tests to confirm no behavior change**

`.venv/Scripts/python.exe "<scratchpad>/test_gui_mixed_mode.py"`
`.venv/Scripts/python.exe "<scratchpad>/test_gui_subtitles_only.py"`
`.venv/Scripts/python.exe "<scratchpad>/test_gui_live_subtitles_toggle.py"`
`.venv/Scripts/python.exe "<scratchpad>/test_multi_start_stop_cycles.py"`
`.venv/Scripts/python.exe "<scratchpad>/test_device_open_failure.py"`

Note: some of these tests' `FakeSessionWorker`/direct `SessionConfig(...)` construction may reference `include_mic=` as a keyword argument -- if so, remove that keyword argument from the test's construction call (it's no longer a valid field) and re-run. Do not change any assertion values, only remove the now-invalid keyword.

All five must print `ALL TESTS PASSED` (or, for `test_device_open_failure.py`, its own success markers) with no other changes.

- [ ] **Step 6: Commit**

```bash
git add app/gui.py
git commit -m "Always build a MixedSource in SessionWorker.start(), drop include_mic"
```

---

### Task 4: `TranslationRunner.request_set_file()` / `SessionWorker.set_file()`

**Files:**
- Modify: `app/translation_session.py` (`TranslationRunner`)
- Modify: `app/gui.py` (`SessionWorker`)
- Test: `<scratchpad>/test_live_set_file_wiring.py`

**Interfaces:**
- Consumes: `MixedSource.set_file(file: FileStream | None) -> None` (Task 2); `SessionWorker._call_on_loop` (existing).
- Produces: `TranslationRunner.request_set_file(self, path: str | None) -> None` and `SessionWorker.set_file(self, path: str | None) -> None` — Task 7's GUI wiring calls `SessionWorker.set_file` with this exact name/signature.

- [ ] **Step 1: Write the failing test**

Create `<scratchpad>/test_live_set_file_wiring.py`:

```python
"""TranslationRunner.request_set_file(): no-op if the source doesn't
support set_file (e.g. a bare MicStream in some other context); on a
MixedSource-like source, constructs a FileStream, pauses it before handing
it over (never autoplay), and calls source.set_file() with it. path=None
removes the file the same way."""
import asyncio
import sys

sys.path.insert(0, r"E:\STS")

from app.translation_session import TranslationRunner


class FakeSource:
    def __init__(self, supports_set_file=True):
        self.set_file_calls = []
        self._supports = supports_set_file

    async def chunks(self):
        return
        yield  # pragma: no cover

    async def set_file(self, file):
        self.set_file_calls.append(file)


class NoSetFileSource:
    async def chunks(self):
        return
        yield  # pragma: no cover


class FakeSink:
    def play(self, pcm: bytes) -> None:
        pass


async def main():
    # T1: source without set_file -- request_set_file is a safe no-op.
    runner1 = TranslationRunner(
        api_key="k", region="eu", source_lang="pl", target_lang="en",
        source=NoSetFileSource(), sink=FakeSink(),
    )
    runner1.request_set_file(r"C:\fake.wav")  # must not raise
    print("OK T1: request_set_file() on a source without set_file is a safe no-op")

    # T2: source with set_file, but no active session yet (runner._session
    # is None) -- still schedules the async task since request_set_file only
    # checks hasattr(source, "set_file"), matching request_change_mic_device's
    # own no-session-required style (it's local source manipulation, not a
    # server call).
    source2 = FakeSource()
    runner2 = TranslationRunner(
        api_key="k", region="eu", source_lang="pl", target_lang="en",
        source=source2, sink=FakeSink(),
    )
    import tempfile
    import wave
    from pathlib import Path
    wav_path = Path(tempfile.mkdtemp()) / "real.wav"
    with wave.open(str(wav_path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(24000)
        w.writeframes(b"\x00\x00" * 100)
    runner2.request_set_file(str(wav_path))
    await asyncio.sleep(0.1)  # let the scheduled task run
    assert len(source2.set_file_calls) == 1, f"T2 FAILED: {source2.set_file_calls}"
    passed_file = source2.set_file_calls[0]
    assert passed_file is not None, "T2 FAILED: expected a FileStream, got None"
    assert passed_file._paused.is_set(), "T2 FAILED: the file must be pre-paused (never autoplay)"
    print("OK T2: request_set_file(path) constructs a paused FileStream and forwards it to source.set_file()")

    # T3: path=None removes the file.
    runner2.request_set_file(None)
    await asyncio.sleep(0.1)
    assert source2.set_file_calls[-1] is None, f"T3 FAILED: {source2.set_file_calls}"
    print("OK T3: request_set_file(None) forwards None to source.set_file() (remove)")

    print("ALL TESTS PASSED")


asyncio.run(main())
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python.exe "<scratchpad>/test_live_set_file_wiring.py"`
Expected: `AttributeError: 'TranslationRunner' object has no attribute 'request_set_file'`

- [ ] **Step 3: Write minimal implementation**

In `app/translation_session.py`, add these two methods to `TranslationRunner` (place `request_set_file` right after `request_change_mic_device`, and `_do_set_file` right after it):

```python
    def request_set_file(self, path: str | None) -> None:
        """Live add/change/remove of the mixed file source. No-op if the
        source doesn't support it (doesn't have set_file -- e.g. a plain
        MicStream, which no longer occurs in practice now that
        SessionWorker.start() always builds a MixedSource, but this stays
        a hasattr check for the same reason request_change_mic_device is
        one). Must be called from the loop's own thread (e.g. via
        call_soon_threadsafe).
        """
        if not hasattr(self._source, "set_file"):
            return
        asyncio.create_task(self._do_set_file(path))

    async def _do_set_file(self, path: str | None) -> None:
        try:
            file = FileStream(path) if path is not None else None
            if file is not None:
                file.pause()  # never autoplay a freshly added/changed file
            await self._source.set_file(file)
        except Exception as e:
            self._on_error(f"Nie udało się ustawić pliku: {e}")
```

Add the import for `FileStream` at the top of `app/translation_session.py` (check the existing imports first -- if `FileStream` isn't already imported there, add `from .audio_io import FileStream`).

In `app/gui.py`, add this method to `SessionWorker` (place it right after `change_mic_device`):

```python
    def set_file(self, path: str | None) -> None:
        self._call_on_loop("request_set_file", path)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/Scripts/python.exe "<scratchpad>/test_live_set_file_wiring.py"`
Expected: `ALL TESTS PASSED`

- [ ] **Step 5: Commit**

```bash
git add app/translation_session.py app/gui.py
git commit -m "Add TranslationRunner.request_set_file()/SessionWorker.set_file()"
```

---

### Task 5: GUI layout — remove `mode_combo`/`SessionMode`, always-visible mic + file rows, remove-file button

**Files:**
- Modify: `app/gui.py` (imports, widget construction, `_config_widgets`)
- Test: `<scratchpad>/test_gui_always_visible_layout.py`

**Interfaces:**
- Consumes: nothing new from earlier tasks (pure GUI structure).
- Produces: `MainWindow.file_clear_btn` (QPushButton) — Task 6 wires its `clicked` signal.

- [ ] **Step 1: Write the failing test**

Create `<scratchpad>/test_gui_always_visible_layout.py`:

```python
"""GUI layout: no mode selector exists; mic and file rows are always
visible regardless of any prior mode concept; file_clear_btn exists,
disabled until a file is chosen."""
import sys
import tempfile
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

    # T1: no mode_combo / SessionMode anywhere.
    assert not hasattr(guimod, "SessionMode"), "T1 FAILED: SessionMode should be removed"
    w = guimod.MainWindow()
    assert not hasattr(w, "mode_combo"), "T1 FAILED: mode_combo should be removed"
    print("OK T1: SessionMode and mode_combo are both gone")

    # T2: mic_combo and file_row are always visible (no hidden state at all).
    assert not w.mic_combo.isHidden(), "T2 FAILED: mic_combo should always be visible"
    assert not w.file_row.isHidden(), "T2 FAILED: file_row should always be visible"
    assert not w.mic_gain_row.isHidden(), "T2 FAILED: mic_gain_row should always be visible"
    print("OK T2: mic and file rows are unconditionally visible")

    # T3: file_clear_btn exists and starts disabled (no file chosen yet).
    assert hasattr(w, "file_clear_btn"), "T3 FAILED: file_clear_btn should exist"
    assert not w.file_clear_btn.isEnabled(), "T3 FAILED: file_clear_btn should start disabled"
    print("OK T3: file_clear_btn exists and starts disabled")

print("ALL TESTS PASSED")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python.exe "<scratchpad>/test_gui_always_visible_layout.py"`
Expected: FAIL at T1 (`AssertionError: T1 FAILED: SessionMode should be removed`) -- `SessionMode` still exists.

- [ ] **Step 3: Write minimal implementation**

In `app/gui.py`:

1. Remove `from enum import Enum` from the imports (no longer used anywhere in this file once `SessionMode` is removed).

2. Remove the `SessionMode` class entirely (the `class SessionMode(Enum): MIC = 0 ... has_mic ... has_file ...` block).

3. Remove `MainWindow._current_mode(self) -> SessionMode` entirely.

4. Remove `MainWindow._on_mode_changed(self, index: int) -> None` entirely.

5. Remove the `mode_combo` construction block:

```python
        self.mode_combo = QComboBox()
        self.mode_combo.addItems(["Mikrofon", "Plik", "Mikrofon + Plik"])
        self.mode_combo.setToolTip(
            "\"Mikrofon + Plik\" miksuje oba dźwięki w jedno wspólne tłumaczenie (np. lektor z "
            "pliku + osoba mówiąca na żywo) zamiast dwóch osobnych sesji. Pauza dotyczy wtedy "
            "tylko pliku -- mikrofon zostaje aktywny przez cały czas."
        )
        self.mode_combo.currentIndexChanged.connect(self._on_mode_changed)
        form.addRow("Źródło:", self.mode_combo)
```

6. Replace the combined "Wejście:" row (the `mic_combo`/`file_row` stacked in one `source_col`, plus the refresh button) with two separate rows. Find:

```python
        # A plain vertical stack of both rows (not a QStackedWidget) so
        # "Mikrofon + Plik" mode can show BOTH at once -- each row's own
        # visibility is toggled in _on_mode_changed instead of only one
        # being showable at a time.
        source_col = QVBoxLayout()
        source_col.setContentsMargins(0, 0, 0, 0)
        source_col.addWidget(self.mic_combo)
        source_col.addWidget(self.file_row)
        input_row = QHBoxLayout()
        input_row.addLayout(source_col, stretch=1)
        self.refresh_devices_btn = QPushButton("Odśwież urządzenia")
        self.refresh_devices_btn.clicked.connect(self._on_refresh_devices)
        input_row.addWidget(self.refresh_devices_btn)
        form.addRow("Wejście:", input_row)
        form.addRow("", self.mic_gain_row)
```

with:

```python
        # mic and file are both always part of every session now -- there is
        # no mode selector, so both rows are always visible; no toggling code
        # needed at all (contrast with the old _on_mode_changed).
        mic_row = QHBoxLayout()
        mic_row.addWidget(self.mic_combo, stretch=1)
        self.refresh_devices_btn = QPushButton("Odśwież urządzenia")
        self.refresh_devices_btn.clicked.connect(self._on_refresh_devices)
        mic_row.addWidget(self.refresh_devices_btn)
        form.addRow("Mikrofon:", mic_row)
        form.addRow("", self.mic_gain_row)
        form.addRow("Plik (opcjonalnie):", self.file_row)
```

7. Add a remove-file button to the `file_row` construction. Find:

```python
        self.file_row = QWidget()
        file_layout = QHBoxLayout(self.file_row)
        file_layout.setContentsMargins(0, 0, 0, 0)
        self.file_label = QLabel("(nie wybrano pliku)")
        self.file_btn = QPushButton("Wybierz plik...")
        self.file_btn.clicked.connect(self._choose_file)
        file_layout.addWidget(self.file_label, stretch=1)
        file_layout.addWidget(self.file_btn)
```

change to:

```python
        self.file_row = QWidget()
        file_layout = QHBoxLayout(self.file_row)
        file_layout.setContentsMargins(0, 0, 0, 0)
        self.file_label = QLabel("(nie wybrano pliku)")
        self.file_btn = QPushButton("Wybierz plik...")
        self.file_btn.clicked.connect(self._choose_file)
        self.file_clear_btn = QPushButton("✕")
        self.file_clear_btn.setToolTip("Usuń wybrany plik")
        self.file_clear_btn.setEnabled(False)
        self.file_clear_btn.clicked.connect(self._on_clear_file)
        file_layout.addWidget(self.file_label, stretch=1)
        file_layout.addWidget(self.file_btn)
        file_layout.addWidget(self.file_clear_btn)
```

(`_on_clear_file` is added in Task 6 -- this task only wires the button's `clicked` signal to it; the method doesn't need to exist yet for this task's own test, since T3 above only checks the button's existence/initial state, not its click behavior.)

8. Remove the now-invalid call `self._on_mode_changed(self.mode_combo.currentIndex())` (it was there to set initial visibility -- no longer needed since nothing is conditionally hidden anymore). Also remove the comment block right above it that explains why that call was needed. Find:

```python
        root.addWidget(self.playback_row)
        # playback_row itself (holding pause_btn) stays visible in all modes --
        # only the position slider/label (file-only, no meaning for a live mic)
        # are toggled in _on_mode_changed. mode_combo's currentIndexChanged
        # already fired once during addItems() above, before this method was
        # connected, so the initial mic_combo/file_row/mic_gain_row/position_*
        # visibility needs this explicit call instead of relying on the signal.
        self._on_mode_changed(self.mode_combo.currentIndex())
```

replace with:

```python
        root.addWidget(self.playback_row)
        # position_slider/position_label start hidden -- no file is selected
        # yet at construction time; _choose_file()/_on_clear_file() toggle
        # them from here on (see Task 6).
        self.position_slider.setVisible(False)
        self.position_label.setVisible(False)
```

9. Remove `self.mode_combo,` from `_config_widgets`.

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/Scripts/python.exe "<scratchpad>/test_gui_always_visible_layout.py"`
Expected: `ALL TESTS PASSED`

- [ ] **Step 5: Commit**

```bash
git add app/gui.py
git commit -m "Remove mode selector, always show mic+file rows, add file-clear button"
```

---

### Task 6: File never autoplays; two independent pause buttons

**Files:**
- Modify: `app/gui.py` (`MainWindow.__init__` widget construction, `_on_state`, `_on_pause_resume`, new `_on_file_pause_resume`, `_on_worker_finished`, `_on_start_stop`)
- Test: `<scratchpad>/test_gui_two_pause_buttons.py`

**Interfaces:**
- Consumes: `SessionWorker.pause_file()`/`resume_file()` (existing); Task 3's always-`MixedSource` `SessionWorker.start()`.
- Produces: `MainWindow.file_pause_btn` (QPushButton), `MainWindow._file_paused: bool`, `MainWindow._on_file_pause_resume(self) -> None` -- Task 7 references `self._file_paused`/`self.file_pause_btn` when wiring live file add/change/remove.

- [ ] **Step 1: Write the failing test**

Create `<scratchpad>/test_gui_two_pause_buttons.py`:

```python
"""Two independent pause controls: "Pauza" always controls the whole
session regardless of file presence; "Pauza pliku" is visible only with a
file selected, and a file active at Start always starts in its paused
("Wznów plik") state."""
import sys
import tempfile
import wave
import struct
from pathlib import Path
from unittest.mock import patch, MagicMock

sys.path.insert(0, r"E:\STS")

tmp_dir = Path(tempfile.mkdtemp())

import app.config as config
with patch.object(config, "CONFIG_DIR", tmp_dir), \
     patch.object(config, "OVERLAY_SETTINGS_PATH", tmp_dir / "overlay_settings.json"):

    from PySide6.QtWidgets import QApplication
    app_qt = QApplication.instance() or QApplication([])

    import app.gui as guimod

    class FakeCreds:
        api_key = "fake-key"
        region = "eu"

    class FakeSessionWorker:
        def __init__(self, config):
            self.state_changed = MagicMock(connect=lambda *a, **k: None)
            self.transcript_received = MagicMock(connect=lambda *a, **k: None)
            self.error_occurred = MagicMock(connect=lambda *a, **k: None)
            self.finished = MagicMock(connect=lambda *a, **k: None)
            self.pause_calls = 0
            self.resume_calls = 0
            self.pause_file_calls = 0
            self.resume_file_calls = 0

        def start(self):
            pass

        def stop(self):
            pass

        def pause(self):
            self.pause_calls += 1

        def resume(self):
            self.resume_calls += 1

        def pause_file(self):
            self.pause_file_calls += 1

        def resume_file(self):
            self.resume_file_calls += 1

    def make_window():
        w = guimod.MainWindow()
        w.mic_combo.clear()
        w.mic_combo.addItem("Mic A", 10)
        w.output_combo.clear()
        w.output_combo.addItem("CABLE Input (VB-Audio Virtual Cable)", 0)
        return w

    def start_session(w):
        with patch.object(config, "load_credentials", return_value=FakeCreds()), \
             patch.object(guimod, "SessionWorker", FakeSessionWorker), \
             patch("threading.Thread") as mock_thread_cls:
            mock_thread_cls.return_value = MagicMock(is_alive=lambda: False)
            w._on_start_stop()

    wav_path = Path(tempfile.mkdtemp()) / "real.wav"
    with wave.open(str(wav_path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(24000)
        wf.writeframes(struct.pack("<100h", *([1000] * 100)))

    # T1: no file selected -- "Pauza" controls the whole session; file_pause_btn
    # stays disabled/hidden the whole time.
    w1 = make_window()
    start_session(w1)
    assert not w1.file_pause_btn.isVisible() or not w1.file_pause_btn.isEnabled(), (
        "T1 FAILED: file_pause_btn should not be usable with no file"
    )
    w1._on_state(guimod.SessionState.RUNNING)
    w1._on_pause_resume()
    assert w1._worker.pause_calls == 1, f"T1 FAILED: {w1._worker.pause_calls}"
    assert w1._worker.pause_file_calls == 0, f"T1 FAILED: {w1._worker.pause_file_calls}"
    print("OK T1: with no file, Pauza controls the whole session; file_pause_btn unusable")

    # T2: a file selected at Start always starts in the "Wznów plik" (paused)
    # state, independent of the main Pauza button.
    w2 = make_window()
    w2._selected_file = str(wav_path)
    w2.file_label.setText(str(wav_path))
    w2.file_clear_btn.setEnabled(True)
    start_session(w2)
    assert w2._file_paused is True, "T2 FAILED: a file present at Start should start paused"
    assert w2.file_pause_btn.text() == "Wznów plik", f"T2 FAILED: {w2.file_pause_btn.text()!r}"
    print("OK T2: a file selected at Start starts in the paused/'Wznów plik' state")

    # T3: the two buttons are fully independent -- toggling one never touches
    # the other's call counts or displayed state.
    w2._on_state(guimod.SessionState.RUNNING)
    assert w2.pause_btn.text() == "Pauza", f"T3 FAILED: main pause_btn should read Pauza, got {w2.pause_btn.text()!r}"
    assert w2.file_pause_btn.text() == "Wznów plik", "T3 FAILED: file_pause_btn state should be untouched by session RUNNING"
    w2._on_pause_resume()  # whole-session pause
    assert w2._worker.pause_calls == 1
    assert w2._worker.pause_file_calls == 0
    assert w2.file_pause_btn.text() == "Wznów plik", "T3 FAILED: session pause must not affect file_pause_btn"
    w2._on_file_pause_resume()  # file resume
    assert w2._worker.resume_file_calls == 1, f"T3 FAILED: {w2._worker.resume_file_calls}"
    assert w2._worker.resume_calls == 0, "T3 FAILED: file resume must not touch the session"
    assert w2.file_pause_btn.text() == "Pauza pliku", f"T3 FAILED: {w2.file_pause_btn.text()!r}"
    assert w2.pause_btn.text() == "Pauza", "T3 FAILED: main pause_btn text should be unaffected by file resume"
    print("OK T3: Pauza and Pauza pliku are fully independent -- neither affects the other's state or calls")

print("ALL TESTS PASSED")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python.exe "<scratchpad>/test_gui_two_pause_buttons.py"`
Expected: `AttributeError: 'MainWindow' object has no attribute 'file_pause_btn'`

- [ ] **Step 3: Write minimal implementation**

In `app/gui.py`, in the `playback_row` construction, add the new button. Find:

```python
        self.playback_row = QWidget()
        playback_layout = QHBoxLayout(self.playback_row)
        playback_layout.setContentsMargins(0, 0, 0, 0)
        self.pause_btn = QPushButton("Pauza")
        self.pause_btn.setEnabled(False)
        self.pause_btn.clicked.connect(self._on_pause_resume)
        self.position_slider = QSlider(Qt.Orientation.Horizontal)
        self.position_slider.setEnabled(False)
        self.position_slider.sliderReleased.connect(self._on_seek)
        self.position_label = QLabel("00:00 / 00:00")
        playback_layout.addWidget(self.pause_btn)
        playback_layout.addWidget(self.position_slider, stretch=1)
        playback_layout.addWidget(self.position_label)
```

change to:

```python
        self.playback_row = QWidget()
        playback_layout = QHBoxLayout(self.playback_row)
        playback_layout.setContentsMargins(0, 0, 0, 0)
        self.pause_btn = QPushButton("Pauza")
        self.pause_btn.setToolTip("Wstrzymuje/wznawia całą sesję (mikrofon i plik, jeśli jest), niezależnie od stanu pliku.")
        self.pause_btn.setEnabled(False)
        self.pause_btn.clicked.connect(self._on_pause_resume)
        self.file_pause_btn = QPushButton("Pauza pliku")
        self.file_pause_btn.setToolTip("Wstrzymuje/wznawia tylko plik -- mikrofon i reszta sesji nie są tym dotknięte.")
        self.file_pause_btn.setVisible(False)
        self.file_pause_btn.setEnabled(False)
        self.file_pause_btn.clicked.connect(self._on_file_pause_resume)
        self.position_slider = QSlider(Qt.Orientation.Horizontal)
        self.position_slider.setEnabled(False)
        self.position_slider.sliderReleased.connect(self._on_seek)
        self.position_label = QLabel("00:00 / 00:00")
        playback_layout.addWidget(self.pause_btn)
        playback_layout.addWidget(self.file_pause_btn)
        playback_layout.addWidget(self.position_slider, stretch=1)
        playback_layout.addWidget(self.position_label)
```

Add `self._file_paused = False` right next to the existing `self._is_paused = False` in `__init__` (they now track two fully independent things -- `_is_paused` is whole-session-only from here on).

Change `_on_state` from:

```python
    def _on_state(self, state: SessionState) -> None:
        self._pause_request_pending = False
        self.status_label.setText(_STATE_LABELS.get(state, str(state)))
        if state == SessionState.PAUSED:
            self._is_paused = True
            self.pause_btn.setText("Wznów")
        elif state == SessionState.RUNNING:
            self._is_paused = False
            self.pause_btn.setText("Pauza")
            self.pause_btn.setEnabled(True)  # works for both mic and file mode now
```

to:

```python
    def _on_state(self, state: SessionState) -> None:
        self._pause_request_pending = False
        self.status_label.setText(_STATE_LABELS.get(state, str(state)))
        if state == SessionState.PAUSED:
            self._is_paused = True
            self.pause_btn.setText("Wznów")
        elif state == SessionState.RUNNING:
            self._is_paused = False
            self.pause_btn.setText("Pauza")
            self.pause_btn.setEnabled(True)
```

(Unchanged body, but the trailing comment "works for both mic and file mode now" is stale -- delete it; `_is_paused`/`pause_btn` are purely session-level again, same as before mixed mode existed.)

Change `_on_pause_resume` from (current mode-dependent version):

```python
    def _on_pause_resume(self) -> None:
        if self._worker is None:
            return
        if self._current_mode() == SessionMode.MIC_AND_FILE:
            # Mixed mode: Pauza/Wznów only pause the FILE locally -- the mic
            # and the underlying Palabra session keep running (still billed,
            # still translating whatever the mic picks up), so this is a
```

(and whatever follows in that branch) to the simple, original, single-purpose form:

```python
    def _on_pause_resume(self) -> None:
        if self._worker is None or self._pause_request_pending:
            return
        self._pause_request_pending = True
        if self._is_paused:
            self._worker.resume()
        else:
            self._worker.pause()
```

Add a new method right after it:

```python
    def _on_file_pause_resume(self) -> None:
        # Fully independent of _on_pause_resume/_is_paused: this only ever
        # touches the file locally (SessionWorker.pause_file()/resume_file()),
        # never the server-side session, so there's no _pause_request_pending
        # guard needed here either -- same reasoning as pause_file() itself.
        if self._worker is None or self._selected_file is None:
            return
        self._file_paused = not self._file_paused
        if self._file_paused:
            self._worker.pause_file()
            self.file_pause_btn.setText("Wznów plik")
        else:
            self._worker.resume_file()
            self.file_pause_btn.setText("Pauza pliku")
```

Change `_on_worker_finished` from:

```python
    def _on_worker_finished(self) -> None:
        self._worker = None
        self._thread = None
        self.start_stop_btn.setText("Start")
        self.start_stop_btn.setEnabled(True)
        self._set_config_enabled(True)
        self._position_timer.stop()
        self._is_paused = False
        self.pause_btn.setText("Pauza")
        self.pause_btn.setEnabled(False)
        self.position_slider.setEnabled(False)
        self.position_slider.setValue(0)
        self.position_label.setText("00:00 / 00:00")
```

to:

```python
    def _on_worker_finished(self) -> None:
        self._worker = None
        self._thread = None
        self.start_stop_btn.setText("Start")
        self.start_stop_btn.setEnabled(True)
        self._set_config_enabled(True)
        self._position_timer.stop()
        self._is_paused = False
        self.pause_btn.setText("Pauza")
        self.pause_btn.setEnabled(False)
        self._file_paused = False
        self.file_pause_btn.setText("Pauza pliku")
        self.file_pause_btn.setEnabled(False)
        self.position_slider.setEnabled(False)
        self.position_slider.setValue(0)
        self.position_label.setText("00:00 / 00:00")
```

(`file_pause_btn`'s *visibility* is left alone here -- like `position_slider`/`position_label`, it should stay visible after Stop if a file is still selected; only its enabled/text state resets. Visibility is driven by "is a file currently selected", set in Task 7's `_choose_file`/`_on_clear_file`.)

In `_on_start_stop`, right after the block that starts the thread and before/around the existing `if needs_file:`-style position-timer start (find the exact current text -- Task 3 already changed some of the surrounding lines, so match by content: the block containing `thread.start()`, `self.start_stop_btn.setText("Stop")`, `self._set_config_enabled(False)`), add the file-pause-state initialization. The block should end up as:

```python
        thread = threading.Thread(target=worker.start, daemon=True)
        self._worker = worker
        self._thread = thread
        thread.start()
        self.start_stop_btn.setText("Stop")
        self._set_config_enabled(False)
        if self._selected_file is not None:
            self._file_paused = True
            self.file_pause_btn.setText("Wznów plik")
            self.file_pause_btn.setEnabled(True)
            self._position_timer.start()
        else:
            self._file_paused = False
            self.file_pause_btn.setEnabled(False)
```

(This replaces whatever the current `if needs_file:` / `if file_path is not None:`-style conditional at the end of `_on_start_stop` looks like after Task 3's edits -- match by finding the `thread.start()` call and everything from there to the end of the method, and replace that trailing conditional with the block above. The `mic_device`/`output_device`/`SessionConfig(...)` construction earlier in the method is untouched by this task.)

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/Scripts/python.exe "<scratchpad>/test_gui_two_pause_buttons.py"`
Expected: `ALL TESTS PASSED`

- [ ] **Step 5: Commit**

```bash
git add app/gui.py
git commit -m "Split pause control into independent session-level and file-level buttons"
```

---

### Task 7: Live add/change/remove wiring; loop-protection always active; unlock file controls

**Files:**
- Modify: `app/gui.py` (`_choose_file`, new `_on_clear_file`, `_on_transcript`, `_config_widgets`, `_confirm_feedback_loop_risk`, `_on_mic_selection_changed`)
- Test: `<scratchpad>/test_gui_live_file_control.py`

**Interfaces:**
- Consumes: `SessionWorker.set_file(path: str | None)` (Task 4); `MainWindow.file_pause_btn`/`_file_paused` (Task 6); `MainWindow.file_clear_btn` (Task 5).
- Produces: `MainWindow._on_clear_file(self) -> None` -- no later task depends on it, but it must exist under this name for Task 5's button wiring (added in Task 5, calling this method which now gets its body) and this task's own test.

- [ ] **Step 1: Write the failing test**

Create `<scratchpad>/test_gui_live_file_control.py`:

```python
"""Live file control mid-session: choosing/changing a file while a session
is running calls SessionWorker.set_file() and resets file_pause_btn to its
paused state; clearing the file mid-session calls set_file(None) and hides
the file controls again, without touching the main session at all. The
feedback-loop check now fires regardless of whether a file is selected."""
import sys
import tempfile
import wave
import struct
from pathlib import Path
from unittest.mock import patch, MagicMock

sys.path.insert(0, r"E:\STS")

tmp_dir = Path(tempfile.mkdtemp())

import app.config as config
with patch.object(config, "CONFIG_DIR", tmp_dir), \
     patch.object(config, "OVERLAY_SETTINGS_PATH", tmp_dir / "overlay_settings.json"):

    from PySide6.QtWidgets import QApplication
    app_qt = QApplication.instance() or QApplication([])

    import app.gui as guimod
    from app.translation_session import TranscriptEvent

    class FakeCreds:
        api_key = "fake-key"
        region = "eu"

    class FakeSessionWorker:
        def __init__(self, config):
            self.state_changed = MagicMock(connect=lambda *a, **k: None)
            self.transcript_received = MagicMock(connect=lambda *a, **k: None)
            self.error_occurred = MagicMock(connect=lambda *a, **k: None)
            self.finished = MagicMock(connect=lambda *a, **k: None)
            self.set_file_calls = []

        def start(self):
            pass

        def stop(self):
            pass

        def set_file(self, path):
            self.set_file_calls.append(path)

    def make_window():
        w = guimod.MainWindow()
        w.mic_combo.clear()
        w.mic_combo.addItem("Mic A", 10)
        w.output_combo.clear()
        w.output_combo.addItem("CABLE Input (VB-Audio Virtual Cable)", 0)
        return w

    def start_session(w):
        with patch.object(config, "load_credentials", return_value=FakeCreds()), \
             patch.object(guimod, "SessionWorker", FakeSessionWorker), \
             patch("threading.Thread") as mock_thread_cls:
            mock_thread_cls.return_value = MagicMock(is_alive=lambda: False)
            w._on_start_stop()

    wav_path = Path(tempfile.mkdtemp()) / "real.wav"
    with wave.open(str(wav_path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(24000)
        wf.writeframes(struct.pack("<100h", *([1000] * 100)))

    # T1: file_btn and file_clear_btn are NOT locked during a session.
    w1 = make_window()
    start_session(w1)
    assert w1.file_btn.isEnabled(), "T1 FAILED: file_btn should stay enabled during a session"
    print("OK T1: file_btn stays live/enabled during a session")

    # T2: choosing a file mid-session calls SessionWorker.set_file() and
    # resets the file-pause state to paused.
    w1._file_paused = False
    w1.file_pause_btn.setText("Pauza pliku")
    with patch.object(guimod.QFileDialog, "getOpenFileName", return_value=(str(wav_path), "")):
        w1._choose_file()
    assert w1._worker.set_file_calls == [str(wav_path)], f"T2 FAILED: {w1._worker.set_file_calls}"
    assert w1._file_paused is True, "T2 FAILED: adding a file mid-session should start it paused"
    assert w1.file_pause_btn.text() == "Wznów plik", f"T2 FAILED: {w1.file_pause_btn.text()!r}"
    assert w1.file_pause_btn.isEnabled(), "T2 FAILED: file_pause_btn should become enabled"
    print("OK T2: choosing a file mid-session forwards to set_file() and starts it paused")

    # T3: clearing the file mid-session calls set_file(None), hides the
    # position controls, and does NOT touch the main session pause state.
    w1._on_clear_file()
    assert w1._worker.set_file_calls[-1] is None, f"T3 FAILED: {w1._worker.set_file_calls}"
    assert w1._selected_file is None
    assert not w1.file_clear_btn.isEnabled()
    assert not w1.file_pause_btn.isEnabled()
    print("OK T3: clearing the file mid-session forwards set_file(None) and disables file controls")

    # T4: feedback-loop check fires regardless of file presence -- with a
    # file selected, the loop-repeat detector still runs on final transcripts.
    w2 = make_window()
    w2._selected_file = str(wav_path)
    start_session(w2)
    calls = []
    w2._check_loop_repeat = lambda ev: calls.append(ev)
    ev = TranscriptEvent(text="powtarzam się", language="pl", is_translation=False, is_final=True)
    w2._on_transcript(ev)
    assert len(calls) == 1, "T4 FAILED: loop-repeat check should fire even with a file selected"
    print("OK T4: feedback-loop auto-pause check runs regardless of whether a file is selected")

print("ALL TESTS PASSED")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python.exe "<scratchpad>/test_gui_live_file_control.py"`
Expected: FAIL at T1 (`file_btn` still in `_config_widgets`, so disabled during a session).

- [ ] **Step 3: Write minimal implementation**

In `app/gui.py`:

1. Remove `self.file_btn,` from `_config_widgets`. Update the comment block above `_config_widgets` to add a note for `file_btn`/`file_clear_btn`, e.g. right after the existing `subtitles_only_check` explanation add:

```python
        # file_btn/file_clear_btn are deliberately NOT in this list either --
        # the file can be added, changed, or removed for the whole duration
        # of a session (see _choose_file/_on_clear_file), same live-editable
        # treatment as mic_combo and subtitles_only_check above.
```

2. Change `_choose_file` from:

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
        self.file_clear_btn.setEnabled(True)
        self.position_slider.setVisible(True)
        self.position_label.setVisible(True)
        self.file_pause_btn.setVisible(True)
        if self._worker is not None:
            # Live add/change mid-session: same "never autoplay" rule as a
            # file selected before Start -- see SessionWorker.set_file() /
            # TranslationRunner._do_set_file, which pauses it before it's
            # ever handed to MixedSource.
            self._file_paused = True
            self.file_pause_btn.setText("Wznów plik")
            self.file_pause_btn.setEnabled(True)
            self._position_timer.start()
            self._worker.set_file(path)
```

3. Add a new method right after `_choose_file`:

```python
    def _on_clear_file(self) -> None:
        self._selected_file = None
        self.file_label.setText("(nie wybrano pliku)")
        self.file_clear_btn.setEnabled(False)
        self.position_slider.setVisible(False)
        self.position_label.setVisible(False)
        self.position_slider.setEnabled(False)
        self.position_slider.setValue(0)
        self.position_label.setText("00:00 / 00:00")
        self.file_pause_btn.setVisible(False)
        self.file_pause_btn.setEnabled(False)
        self._file_paused = False
        self.file_pause_btn.setText("Pauza pliku")
        if self._worker is not None:
            self._worker.set_file(None)
```

4. Change `_confirm_feedback_loop_risk` from:

```python
        mic_active = self._current_mode().has_mic
        if not mic_active or self.subtitles_only_check.isChecked():
            return True
```

to:

```python
        if self.subtitles_only_check.isChecked():
            return True
```

(the mic is always active now, so the only thing left to check is subtitles-only.)

5. Change `_on_mic_selection_changed` from:

```python
    def _on_mic_selection_changed(self, _index: int) -> None:
        # Live device switching mid-session (see SessionWorker.change_mic_device)
        # -- a no-op before Start (no worker yet) or in file-only mode (mic_combo
        # exists but isn't the active source, and is hidden behind file_row
        # anyway; mode_combo itself stays locked during a session so this
        # can't actually flip mode mid-stream).
        if self._worker is None or not self._current_mode().has_mic:
            return
        device = self.mic_combo.currentData()
        if device is not None:
            self._worker.change_mic_device(device)
```

to:

```python
    def _on_mic_selection_changed(self, _index: int) -> None:
        # Live device switching mid-session (see SessionWorker.change_mic_device)
        # -- a no-op before Start (no worker yet). The mic is always the
        # active source now, so there's no mode check left to make.
        if self._worker is None:
            return
        device = self.mic_combo.currentData()
        if device is not None:
            self._worker.change_mic_device(device)
```

6. In `_on_transcript`, change:

```python
        if event.is_final and self._current_mode() == SessionMode.MIC:  # mic mode only
            self._check_loop_repeat(event)
```

to:

```python
        if event.is_final:
            self._check_loop_repeat(event)
```

Also update `_check_loop_repeat`'s docstring, which currently says "Feedback-loop detection: mic mode only (file mode can't feed back -- see SessionWorker.start(), which never opens a mic for it)..." -- this is no longer true (the mic is always open now). Change that opening sentence to:

```python
        """Feedback-loop detection: the mic is always live now, so this
        always runs (previously scoped to mic-only mode, which under-
        protected mixed mic+file sessions -- a live mic can feed back on
        itself regardless of whether a file is also playing). Confirmed
        via a real incident where a live mic picked up this app's own
```

(keep the rest of the docstring as-is from "translated output through speakers..." onward).

Also update the module-level comment above `LOOP_REPEAT_THRESHOLD`/`LOOP_REPEAT_WINDOW_SECONDS` if it says "(mic mode only ...)" -- change it to drop that qualifier the same way.

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/Scripts/python.exe "<scratchpad>/test_gui_live_file_control.py"`
Expected: `ALL TESTS PASSED`

- [ ] **Step 5: Commit**

```bash
git add app/gui.py
git commit -m "Wire live file add/change/remove; loop-protection always active"
```

---

### Task 8: Full regression + real end-to-end session test + README

**Files:** `README.md` (documentation update), no other files.

**Interfaces:**
- Consumes: everything from Tasks 1-7.
- Produces: nothing (terminal task).

- [ ] **Step 1: Run the full existing regression suite**

Run every `test_*.py` in `<scratchpad>` except the known slow/hardware-only/real-API-cost ones (`test_2h_file_decode_memory.py`, `test_2h_gui_memory_growth.py`, `test_real_mic_switch_hardware.py`, anything with `real_hardware`/`real_session`/`audio_text_latency`/`audio_text_timeline`/`audio_delivery_span` in its name), plus every new test from Tasks 1-7. Expected: only the two known, pre-existing, unrelated failures (`test_change_voice.py` T4, `test_new_features.py` console-encoding) -- everything else passes.

Note: several older tests reference the removed `mode_combo`/`SessionMode`/`include_mic` (e.g. anything that sets `w.mode_combo.setCurrentIndex(...)`, constructs `SessionConfig(..., include_mic=...)`, or imports `guimod.SessionMode`). Update each one directly: replace `w.mode_combo.setCurrentIndex(2)`-style mixed-mode setup with `w._selected_file = <path>` (mic is implicit now), remove `include_mic=` keyword arguments, and remove any `SessionMode` references. Do not change what each test is actually asserting -- only adapt the setup/construction calls to the new API shape, the same kind of mechanical fix already applied earlier this session when `SessionConfig` was introduced.

- [ ] **Step 2: Real-hardware/live sanity check**

Using a real API key and a real input/output device (this project's established practice -- prior investigations in this app found real bugs that only real hardware surfaced):
1. Start a session with no file selected. Confirm mic-only translation works, "Pauza" pauses/resumes the whole session, "Pauza pliku" is not visible.
2. While that session is running, click "Wybierz plik...", pick a real audio/video file. Confirm: it does NOT start playing automatically, "Pauza pliku" appears showing "Wznów plik", the mic keeps translating uninterrupted the whole time.
3. Click "Wznów plik" -- confirm the file's audio now mixes in.
4. Click "Wybierz plik..." again, pick a different file. Confirm the first file stops contributing immediately and the new one starts (paused, needs another "Wznów plik").
5. Click "✕" to remove the file. Confirm the mic keeps flowing uninterrupted, "Pauza pliku" disappears, the position slider hides.
6. Click "Pauza" (whole session). While paused, click "Wybierz plik..." to add a file. Confirm the file gets set (shows "Wznów plik") but nothing plays yet. Click "Pauza"/"Wznów" (whole session) -- confirm the mic resumes but the file still doesn't play. Click "Wznów plik" separately -- confirm the file now also plays.
7. Speak the same phrase 3+ times in a row (or otherwise trigger a repeat) with a file also selected/mixed in -- confirm the feedback-loop auto-pause still fires (it previously didn't in this scenario).

- [ ] **Step 3: Update README**

In `README.md`:
- Remove/rewrite the "Wybierz źródło: Mikrofon / Plik / Mikrofon + Plik" instructions (and the "Tryb Mikrofon + Plik" section) to describe the new model: the mic is always part of every session; the file is an optional add-on you can set, change, or remove at any time, including mid-session.
- Document that a newly set file (at Start or mid-session) always starts paused, and that there are now two independent pause controls: "Pauza" (whole session) and "Pauza pliku" (file only).
- Update the loop-protection section to remove the "only in mic mode" qualifier, since it now always applies.
- Update any screenshot-adjacent prose describing the old "Źródło:" dropdown or the old single-Pauza-button behavior.

- [ ] **Step 4: Commit**

```bash
git add README.md
git commit -m "Document always-on mic, optional live file control, and split pause buttons"
```

---

## Self-Review Notes

- **Spec coverage:** A (layout) -> Task 5. B (remove file button) -> Task 5. C (never autoplay) -> Tasks 3 (at Start) + 4 (mid-session, via `_do_set_file`'s `file.pause()`) + 7 (`_choose_file`'s mid-session branch sets `_file_paused=True`). D (`MixedSource` optional/live-replaceable) -> Tasks 1 + 2. E (two pause buttons) -> Task 6. F (`TranslationRunner`/`SessionWorker` wiring) -> Task 4 + Task 3 (construction side). G (loop-protection always active) -> Task 7. H (locking) -> Task 7 (file_btn) + Task 5 (mode_combo removed, so its own locking is moot). Testing section -> Task 8.
- **Placeholder scan:** no TBD/TODO; every step has literal code or a fully enumerated manual procedure (Task 8 Step 2).
- **Type consistency:** `MixedSource.set_file(file: FileStream | None) -> None` (Task 2) is called identically by `TranslationRunner._do_set_file` (Task 4), which is scheduled by `request_set_file(path: str | None)` (Task 4), called identically by `SessionWorker.set_file(path: str | None)` (Task 4), called identically by `MainWindow._choose_file`/`_on_clear_file` (Task 7) -- `path`/`file` naming and `str | None` / `FileStream | None` typing stay consistent at every hop. `_file_paused`/`file_pause_btn` (Task 6) are used identically in Task 7's live-wiring additions.
