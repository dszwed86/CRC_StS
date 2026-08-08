# Tech-debt cleanup: pacing dedup, mode enum, SessionConfig, file validation

## Context

A full-app code review (`code-review` skill, mattpocock's two-axis Standards+Spec reviewer, diffed against the repo's first commit) surfaced several Fowler-baseline code smells and two spec gaps. The user selected five of them to fix now (the rest — a "Divergent Change" observation about `MainWindow`, and adding WebSocket-reconnect logic — are explicitly deferred; reconnect logic is a real feature addition, not a cleanup, and needs its own design later).

## Goal

Fix, in one pass: three duplication/primitive-obsession smells (pacing loop, mic stream construction, mode index checks), one data-clump smell (`SessionWorker`'s 9-parameter constructor), and one spec gap (file validation happens only at Start, not at selection).

## Design

### 1. `RealtimePacer` (new class in `app/audio_io.py`)

The `anchor`/`anchor_ms` real-time pacing pattern (advance a target wall-clock time by a fixed step each tick, sleep until it, resync instead of racing to catch up if already behind) is duplicated in `MicStream.chunks()` (`app/audio_io.py:238-298`), `FileStream.chunks()` (`app/audio_io.py:357-388`), and `MixedSource.chunks()` (`app/audio_io.py:498-518`). Extract:

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

Placement: module level in `app/audio_io.py`, after the module constants (`CHUNK_MS` etc.) and before `MicStream`.

Each of the three call sites replaces its own `anchor`/`anchor_ms` local variables with one `pacer = RealtimePacer(CHUNK_MS)`, replaces every `anchor = time.monotonic(); anchor_ms = 0.0` resync pair with `pacer.resync()`, and replaces every `anchor_ms += CHUNK_MS; delay = ...; if delay > 0: sleep else: resync` block with `await pacer.tick()`. No behavioral change — this is a pure extraction; the resync-on-behind branch that used to be inlined at each tick site now lives inside `tick()` itself.

### 2. `MicStream._open_stream()` (`app/audio_io.py`)

`__init__` (`app/audio_io.py:133-140`) and `switch_device()` (`app/audio_io.py:178-185`) build an identical `sd.RawInputStream(samplerate=RATE, channels=CHANNELS, dtype="int16", device=..., callback=self._on_audio, extra_settings=_wasapi_extra_settings())`. Extract:

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

`__init__` becomes `self._stream = self._open_stream(device)`; `switch_device` becomes `new_stream = self._open_stream(new_device)` (then unchanged: `new_stream.start()`, swap, close old). This preserves the documented "open-before-close" safety property in `switch_device`'s docstring, since both call sites now build the stream identically by construction rather than by convention.

### 3. `SessionMode` enum (`app/gui.py`)

`mode_combo.currentIndex()` (0=Mikrofon, 1=Plik, 2=Mikrofon+Plik) is checked with raw `in (0, 2)` / `in (1, 2)` / `== 2` at five sites: `_on_mode_changed` (`app/gui.py:836-837`), `_on_mic_selection_changed` (`:850`), `_confirm_feedback_loop_risk` (`:916-918`), `_on_start_stop` (`:950-952`), `_on_pause_resume` (`:1068`), and one mic-mode-only check in the transcript handler (`:1116`). Add:

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

Placement: module level in `app/gui.py`, near the top (with the other module-level constants like `REGION`/`DASHBOARD_URL`). Requires `from enum import Enum` added to the imports.

Add a small `MainWindow` helper: `def _current_mode(self) -> SessionMode: return SessionMode(self.mode_combo.currentIndex())`. Replace each of the six sites' raw index math with `self._current_mode()` and `.has_mic`/`.has_file` (or `== SessionMode.MIC_AND_FILE` / `== SessionMode.MIC` for the two exact-mode checks at `:1068` and `:1116`). No behavior change — `SessionMode(0/1/2)` maps 1:1 to the combo's existing item order, which is not itself changing.

### 4. `SessionConfig` dataclass (`app/gui.py`)

`SessionWorker.__init__` (`app/gui.py:278-292`) takes 9 parameters that all travel together from one call site (`_on_start_stop`, `app/gui.py:985-995`). Add:

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

Placement: module level in `app/gui.py`, right before `class SessionWorker`. Requires `from dataclasses import dataclass` added to the imports.

`SessionWorker.__init__` changes from 9 separate parameters to one `config: SessionConfig`, and its body unpacks `self._api_key = config.api_key` etc. (identical attribute names, so **no other line in `SessionWorker` changes** — every existing `self._api_key`/`self._mic_device`/etc. reference elsewhere in the class stays exactly as it is). The call site becomes `SessionWorker(SessionConfig(api_key=..., source_lang=..., ...))`, same keyword arguments as today, now on the `SessionConfig(...)` constructor instead of `SessionWorker(...)` directly.

### 5. `probe_audio_file()` (`app/audio_io.py`) + `_choose_file` (`app/gui.py`)

Spec gap: file validation currently only happens at Start (`SessionWorker.start()`'s broad `except Exception`), not at selection (`_choose_file`, `app/gui.py:856-865`), as the original plan required ("walidacja przy wyborze ..., czytelny błąd zamiast crasha"). Add a lightweight probe — opens/demuxes without fully decoding, so it stays fast even for a large file:

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

Placement: `app/audio_io.py`, near `load_pcm`'s import site (module level, after the existing imports; needs `import wave` added — `av` is already imported lazily elsewhere in the codebase the same way).

`_choose_file` (`app/gui.py`) changes from:

```python
def _choose_file(self) -> None:
    path, _ = QFileDialog.getOpenFileName(...)
    if path:
        self._selected_file = path
        self.file_label.setText(path)
```

to:

```python
def _choose_file(self) -> None:
    path, _ = QFileDialog.getOpenFileName(...)
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

`probe_audio_file` needs adding to the existing `from .audio_io import (...)` block in `app/gui.py`.

## Testing

Per this project's convention (standalone scripts under the scratchpad directory, `assert` + `print`, no pytest):
- `RealtimePacer`: unit test — `tick()` paces correctly (timing within tolerance), `resync()` resets the anchor so a subsequent `tick()` doesn't try to "catch up".
- `MicStream._open_stream`: covered indirectly by the existing mic-switch tests (real-hardware + mocked) already in the suite — re-run them to confirm no regression, no new test needed for the extraction itself.
- `SessionMode`: unit test — `.has_mic`/`.has_file` correct for all three values; GUI test — re-run the existing mode-related GUI tests (mixed-mode, mic-switch, subtitles-only-toggle) to confirm no behavior change after the refactor.
- `SessionConfig`: covered by re-running the existing `_on_start_stop`-driven GUI tests (they already construct a `SessionWorker` via the real code path) — confirms the new call shape still produces an identical, working `SessionWorker`.
- `probe_audio_file`: unit test — a valid WAV passes; a corrupt/truncated file raises `ValueError` with a readable message; a non-audio file (e.g. a text file renamed `.mp4`) raises `ValueError`; `_choose_file`'s GUI-level catch shows the warning dialog and does NOT set `self._selected_file`.
- Full regression suite (all existing scratchpad tests) must stay green, since none of this changes external behavior.

## Out of scope

- `MainWindow`'s "Divergent Change" (many responsibilities) — observation only, no action.
- WebSocket reconnect logic / "Rozłączono" state — deferred, needs its own design (it's a new feature, not a cleanup).
