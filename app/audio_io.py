"""Audio device enumeration plus microphone capture / playback streaming.

PCM format throughout matches what the Palabra API expects: 16-bit signed
little-endian, mono, 24 kHz (see palabra_ai.audio.OUTPUT_SAMPLE_RATE).
"""

from __future__ import annotations

import asyncio
import contextlib
import queue
import threading
import time
import wave
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import sounddevice as sd
from palabra_ai import load_pcm

RATE = 24000
CHANNELS = 1
CHUNK_MS = 320
CHUNK_SAMPLES = int(RATE * CHUNK_MS / 1000)
CHUNK_BYTES = CHUNK_SAMPLES * 2  # int16 = 2 bytes/sample
BYTES_PER_MS = RATE * CHANNELS * 2 / 1000
TRAILING_SILENCE_MS = 2000  # appended so the server can always finalize the last segment
MAX_MIC_BACKLOG_BYTES = CHUNK_BYTES * 2  # ~640ms -- see MicStream.chunks()
MAX_OUTPUT_BACKLOG_SAMPLES = int(RATE * 1.2)  # ~1.2s -- see OutputSink.play()


def probe_audio_file(path: str | Path) -> None:
    """Quick validity check for a file picked as a translation source --
    opens/demuxes it without decoding, so a corrupt or non-audio file is
    caught immediately at selection time instead of only surfacing once
    Start tries to fully decode it (see load_pcm). Raises ValueError with a
    readable Polish message on failure; returns normally if the file looks
    decodable.

    The .wav checks below (suffix special-case + 16-bit constraint) exist
    specifically to mirror palabra_ai.audio.load_pcm's own read_wav
    constraints -- don't "simplify" this into a single av.open() call
    without re-checking that parity still holds.
    """
    path = Path(path)
    if path.suffix.lower() == ".wav":
        try:
            with wave.open(str(path), "rb") as w:
                if w.getsampwidth() != 2:
                    raise ValueError(
                        f"{path.name}: obsługiwany jest tylko 16-bitowy WAV (plik ma {w.getsampwidth() * 8} bitów)."
                    )
                if w.getnframes() == 0:
                    raise ValueError(f"{path.name}: plik WAV nie zawiera dźwięku.")
        except ValueError:
            raise
        except (wave.Error, OSError) as e:
            raise ValueError(f"{path.name}: nie można otworzyć pliku ({e}).") from e
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


@dataclass
class DeviceInfo:
    index: int
    name: str
    max_input_channels: int
    max_output_channels: int


def rescan_devices() -> None:
    """Forces PortAudio to re-scan hardware (e.g. a mic plugged in after startup).

    sounddevice/PortAudio snapshot the device list at initialization; re-running
    init is the standard workaround to pick up hardware changes. Safe to call
    anytime no stream is open.
    """
    sd._terminate()
    sd._initialize()


def _preferred_hostapi_index() -> int | None:
    """On Windows, PortAudio reports the same physical device once per host
    API (MME, DirectSound, WASAPI, WDM-KS) -- the exact same microphone can
    show up 3-4 times in the device list. WASAPI is the modern API and
    already covers every real device (including virtual cables), so when
    it's available only its devices are listed. Platforms with just one
    host API (e.g. macOS Core Audio) are unaffected -- this returns None
    and no filtering happens.
    """
    for i, api in enumerate(sd.query_hostapis()):
        if api["name"] == "Windows WASAPI":
            return i
    return None


def _wasapi_extra_settings() -> "sd.WasapiSettings | None":
    """auto_convert lets WASAPI insert its own sample-rate/channel converter
    for devices whose native format doesn't match our fixed 24 kHz mono PCM.
    MME used to handle this silently, but since device listing now only
    shows WASAPI devices (see _preferred_hostapi_index), opening a device
    whose mixer isn't already at 24 kHz would otherwise fail outright with
    "Invalid sample rate" -- confirmed with this app's own real microphone,
    which opened fine under MME but not under WASAPI without this.
    """
    if _preferred_hostapi_index() is not None:  # WASAPI available -- i.e. Windows
        return sd.WasapiSettings(auto_convert=True)
    return None


def list_input_devices() -> list[DeviceInfo]:
    preferred = _preferred_hostapi_index()
    return [
        DeviceInfo(i, d["name"], d["max_input_channels"], d["max_output_channels"])
        for i, d in enumerate(sd.query_devices())
        if d["max_input_channels"] > 0 and (preferred is None or d["hostapi"] == preferred)
    ]


def list_output_devices() -> list[DeviceInfo]:
    preferred = _preferred_hostapi_index()
    return [
        DeviceInfo(i, d["name"], d["max_input_channels"], d["max_output_channels"])
        for i, d in enumerate(sd.query_devices())
        if d["max_output_channels"] > 0 and (preferred is None or d["hostapi"] == preferred)
    ]


_VIRTUAL_CABLE_MARKERS = ("cable", "blackhole")


def is_virtual_cable_name(device_name: str) -> bool:
    """True if a device name looks like a virtual audio cable (VB-Cable on
    Windows, BlackHole on macOS) rather than a real, audible output (e.g.
    speakers/headphones) a microphone could pick back up."""
    lowered = device_name.lower()
    return any(marker in lowered for marker in _VIRTUAL_CABLE_MARKERS)


def find_virtual_cable(devices: list[DeviceInfo]) -> DeviceInfo | None:
    """Finds a likely virtual-audio-cable output (VB-Cable on Windows, BlackHole on macOS)."""
    for d in devices:
        if is_virtual_cable_name(d.name):
            return d
    return None


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


class MicStream:
    """Captures a microphone as an async stream of 320 ms PCM chunks.

    Supports pause()/resume() (stops feeding the session, e.g. to pause billing
    without losing the device), set_gain() (0.0-1.0, for a live volume/mute
    slider that doesn't touch the session at all), and set_gate_threshold()
    (0.0-1.0, a noise gate: chunks whose peak amplitude falls below the
    threshold are replaced with silence instead of being sent as-is).
    """

    def __init__(self, device: int | None = None):
        self._q: queue.Queue[bytes] = queue.Queue(maxsize=100)
        self._paused = threading.Event()
        self._gain = 1.0
        self._gate_threshold = 0.0
        # Live input level (0.0-1.0, peak amplitude), for a GUI meter that
        # confirms the mic is actually picking up sound. Computed from the
        # RAW incoming samples -- before gain/gate are applied -- so it
        # answers "is audio reaching the app at all", not "what's actually
        # being sent". Updated on the audio callback's own thread; a plain
        # float write/read is safe enough here (single writer, no torn
        # reads under the GIL), same reasoning as total_ms/position_ms
        # elsewhere in this file.
        self.level: float = 0.0
        self._stream = self._open_stream(device)

    def _open_stream(self, device: int | None) -> sd.RawInputStream:
        return sd.RawInputStream(
            samplerate=RATE,
            channels=CHANNELS,
            dtype="int16",
            device=device,
            callback=self._on_audio,
            extra_settings=_wasapi_extra_settings(),
        )

    def _on_audio(self, indata, frames, time_info, status) -> None:
        data = bytes(indata)
        if data:
            self.level = min(1.0, int(np.abs(np.frombuffer(data, dtype=np.int16)).max()) / 32767)
        else:
            self.level = 0.0
        try:
            self._q.put_nowait(data)
        except queue.Full:
            pass  # drop audio rather than block the audio-driver thread

    def __enter__(self) -> "MicStream":
        try:
            self._stream.start()
        except Exception:
            self._stream.close()
            raise
        return self

    def __exit__(self, *exc_info) -> None:
        self._stream.stop()
        self._stream.close()

    def switch_device(self, new_device: int | None) -> None:
        """Swaps to a different physical input device without disturbing
        chunks()'s already-running pacing loop: it only ever reads from
        self._q, filled by the same self._on_audio callback regardless of
        which sd.RawInputStream is calling it, so nothing about the async
        generator or its pacing state needs to change -- only which stream
        object is open.

        The new stream is opened and started BEFORE the old one is
        stopped/closed, and self._stream is only reassigned once that
        succeeds -- so a failure here (e.g. the new device doesn't support
        our fixed sample rate) leaves the working old stream untouched
        instead of leaving the session without any mic at all.

        Must be called from the thread that originally opened this
        MicStream (SessionWorker's background thread, which has COM
        initialized on Windows for WASAPI -- see SessionWorker.start()).
        """
        new_stream = self._open_stream(new_device)
        new_stream.start()
        old_stream = self._stream
        self._stream = new_stream
        old_stream.stop()
        old_stream.close()

    def pause(self) -> None:
        self._paused.set()

    def resume(self) -> None:
        self._paused.clear()

    def set_gain(self, gain: float) -> None:
        """0.0 (silent) .. 1.0 (full volume). Thread-safe; applied to the next chunks."""
        self._gain = max(0.0, min(1.0, gain))

    def set_gate_threshold(self, threshold: float) -> None:
        """0.0 (off -- every chunk passes through) .. 1.0 (only near-full-scale
        peaks pass). Thread-safe; applied to the next chunks, before gain, so
        its meaning doesn't shift depending on the gain slider's position.

        Meant to filter out quiet background sound the mic shouldn't be
        picking up as speech at all (in particular, a live translation's own
        output leaking back in through speakers) without also quietening
        down actual, closer speech the way turning down gain would.
        """
        self._gate_threshold = max(0.0, min(1.0, threshold))

    async def chunks(self) -> AsyncIterator[bytes]:
        """Yields fixed-size 320 ms PCM chunks, paced to real time."""
        # The audio callback starts filling self._q as soon as __enter__() runs
        # (i.e. as soon as the device opens), which is well before this generator
        # is first iterated -- that only happens once the session has finished
        # connecting and feed() starts consuming. Anything already queued at that
        # point was recorded during the "Łączenie..." wait, so draining it here
        # (once) prevents it from being dumped out all at once with no real-time
        # pacing, which the server flags as "arriving faster than real-time".
        while True:
            try:
                self._q.get_nowait()
            except queue.Empty:
                break
        pending = b""
        # Explicit real-time pacing (like FileStream), not just "trust the mic
        # callback's own rate": relying on the callback alone wasn't enough --
        # WASAPI's auto_convert resampling (needed since our fixed 24 kHz
        # doesn't match most devices' native rate) can hand over audio very
        # slightly faster than true real-time, which adds up over a session
        # into a persistent "arriving faster than real-time" warning, not just
        # a one-off burst after a stall (that's the backlog cap below, a
        # separate concern: a stall makes chunks late, a fast callback makes
        # them early -- both are handled here, independently).
        pacer = RealtimePacer(CHUNK_MS)
        while True:
            if self._paused.is_set():
                # Drain and discard everything queued so far, not just one item:
                # the mic callback keeps pushing in real time regardless of
                # pause, so removing only one item per 50ms tick fell behind
                # and let a backlog build up (up to maxsize=100) -- which then
                # burst out faster than real-time on resume, triggering the
                # server's "audio arriving faster than real-time" warning.
                while True:
                    try:
                        self._q.get_nowait()
                    except queue.Empty:
                        break
                pending = b""
                await asyncio.sleep(0.05)
                pacer.resync()  # don't try to "catch up" on paused time
                continue
            try:
                pending += self._q.get_nowait()
            except queue.Empty:
                await asyncio.sleep(0.005)
                continue
            # Drain whatever else is already queued in this same pass, then
            # cap the result: something can briefly stall consumption even
            # outside of an explicit pause (a network hiccup, or set_task()
            # sharing the same websocket send when the voice is changed
            # mid-session) while the mic callback keeps pushing in real
            # time. Draining one item per loop iteration would still replay
            # that backlog as a burst of near-instant yields once caught up
            # -- capping it here means at most one chunk's worth of stale
            # audio gets sent late, instead of everything piled up during
            # the stall.
            while True:
                try:
                    pending += self._q.get_nowait()
                except queue.Empty:
                    break
            if len(pending) > MAX_MIC_BACKLOG_BYTES:
                pending = pending[-CHUNK_BYTES:]
                pacer.resync()  # we just dropped a backlog -- resync instead of pacing off a stale anchor
            while len(pending) >= CHUNK_BYTES:
                await pacer.tick()
                yield self._apply_gain(pending[:CHUNK_BYTES])
                pending = pending[CHUNK_BYTES:]

    def _apply_gain(self, chunk: bytes) -> bytes:
        if self._gate_threshold > 0.0:
            peak = int(np.abs(np.frombuffer(chunk, dtype=np.int16)).max())
            if peak < self._gate_threshold * 32767:
                return bytes(len(chunk))  # below the sensitivity threshold -- treat as silence
        gain = self._gain
        if gain >= 1.0:
            return chunk
        if gain <= 0.0:
            return bytes(len(chunk))
        arr = np.frombuffer(chunk, dtype=np.int16).astype(np.float32) * gain
        return arr.astype(np.int16).tobytes()


class FileStream:
    """Decodes an audio/video file and yields it as 320 ms PCM chunks, paced to
    real time — same interface as MicStream.chunks(), so a file behaves like a
    live source feeding the translation session.

    Supports pause()/resume() and seek() while streaming: position_ms/total_ms
    (in milliseconds) are updated live for a GUI to show a scrubber.
    """

    def __init__(self, path: str | Path):
        self._path = Path(path)
        self._paused = threading.Event()
        self._seek_to_ms: float | None = None
        self.position_ms: float = 0.0
        self.total_ms: float = 0.0
        # Decode starts immediately at construction time instead of waiting
        # for chunks() to be iterated -- which today only happens once the
        # Palabra server connection completes -- so total_ms/seek() and the
        # GUI's position slider become usable as soon as possible, regardless
        # of server-connect latency or whether the file is still paused (a
        # file starts paused by design, see SessionWorker.start()/
        # TranslationRunner._do_set_file, but should still decode while
        # paused). A plain thread, not asyncio: this runs off the event loop
        # entirely so it can't be starved by loop scheduling, and both
        # FileStream() call sites already have a loop running by construction
        # time anyway -- the point is decoupling decode from chunks() being
        # pumped, not from asyncio itself.
        self._decode_done = threading.Event()
        self._pcm: bytes | None = None
        self._decode_error: Exception | None = None
        threading.Thread(target=self._decode, daemon=True).start()

    def _decode(self) -> None:
        try:
            pcm = load_pcm(self._path, sample_rate=RATE, channels=CHANNELS)
            self._pcm = pcm
            self.total_ms = len(pcm) / BYTES_PER_MS
        except Exception as e:  # decode failure (corrupt file, codec issue, ...) -- surfaced later by chunks()
            self._decode_error = e
        finally:
            self._decode_done.set()

    def __enter__(self) -> "FileStream":
        return self

    def __exit__(self, *exc_info) -> None:
        pass

    def pause(self) -> None:
        self._paused.set()

    def resume(self) -> None:
        self._paused.clear()

    def seek(self, position_ms: float) -> None:
        self._seek_to_ms = max(0.0, min(position_ms, self.total_ms))

    async def chunks(self) -> AsyncIterator[bytes]:
        await asyncio.to_thread(self._decode_done.wait)
        if self._decode_error is not None:
            raise self._decode_error
        pcm = self._pcm
        # Real recordings usually trail off into silence, which is what lets the
        # server detect the end of the last phrase and finalize/translate it
        # within eos_timeout. A file that cuts off cold (e.g. synthesized audio,
        # or a hard trim) doesn't -- so append silence to guarantee that gap.
        # position_ms/seek() are unaffected: both stay clamped to total_ms.
        pcm += bytes(int(BYTES_PER_MS * TRAILING_SILENCE_MS))
        pos = 0
        pacer = RealtimePacer(CHUNK_MS)
        while True:
            if self._seek_to_ms is not None:
                pos = int(self._seek_to_ms * BYTES_PER_MS)
                pos -= pos % 2  # keep 16-bit sample alignment
                pos = max(0, min(pos, len(pcm)))
                self._seek_to_ms = None
                pacer.resync()
            if pos >= len(pcm):
                # checked after the seek above so a seek landing exactly at EOF
                # (e.g. right as the last chunk was sent) still takes effect
                # instead of being silently dropped by an early loop exit
                break
            if self._paused.is_set():
                await asyncio.sleep(0.05)
                continue
            chunk = pcm[pos : pos + CHUNK_BYTES]
            pos += len(chunk)
            self.position_ms = min(pos / BYTES_PER_MS, self.total_ms)
            yield chunk
            await pacer.tick()
        self.position_ms = self.total_ms


def _mix_pcm(a: bytes, b: bytes) -> bytes:
    """Sums two equal-length int16 PCM buffers sample-by-sample, clipping to
    the valid range instead of wrapping around on overflow. The standard,
    simple way to combine two audio sources into one; if both are loud at
    the same instant the result can clip (no per-source gain reduction is
    applied) -- acceptable for combining a live mic with a file, not
    intended as a mastering-quality mixer."""
    arr_a = np.frombuffer(a, dtype=np.int16).astype(np.int32)
    arr_b = np.frombuffer(b, dtype=np.int16).astype(np.int32)
    return np.clip(arr_a + arr_b, -32768, 32767).astype(np.int16).tobytes()


class MixedSource:
    """Combines a live MicStream and a FileStream into one mixed audio
    stream for a single translation session -- e.g. a live host talking
    over a pre-recorded narration file, translated together as one
    conversation instead of two separate sessions.

    Supports two independent pause mechanisms:

    - Whole-session pause()/resume(): stops both mic and file from being fed
      to the session at all (paced silence otherwise). Used by
      TranslationRunner's normal request_pause()/request_resume() -- i.e.
      the session-level "Pauza" button and the feedback-loop auto-pause
      safety feature -- and pauses/resumes server-side billing along with
      it. Unlike a standalone paused FileStream, this does NOT freeze the
      file's own internal timeline -- the file's pump task keeps decoding
      and advancing position_ms in real time underneath, its output just
      gets discarded here instead of sent. A whole-session pause therefore
      skips that stretch of the file rather than preserving it -- only
      pause_file() (below) preserves position.
    - File-only pause_file()/resume_file(): stops only the file's
      contribution while the mic keeps flowing, used by the separate
      "Pauza pliku" button. This never touches the session at all.

    These two are fully independent: pausing/resuming one never reads or
    changes the other's state. A file paused via pause_file() stays paused
    across a whole-session pause()/resume() cycle, and vice versa.
    """

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
        self._file_lock = asyncio.Lock()
        # Whole-session pause -- separate from self._file's own self._paused
        # (touched by pause_file/resume_file below), see class docstring.
        self._paused = threading.Event()

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

    def pause(self) -> None:
        """Whole-session pause: stops feeding both mic and file to the
        session. Independent of pause_file()'s own flag -- see class
        docstring."""
        self._paused.set()

    def resume(self) -> None:
        """Whole-session resume. Does NOT touch self._file's own
        pause_file()/resume_file() state -- see class docstring."""
        self._paused.clear()

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
    def mic_level(self) -> float:
        return self._mic.level

    @property
    def position_ms(self) -> float:
        return self._file.position_ms if self._file is not None else 0.0

    @property
    def total_ms(self) -> float:
        return self._file.total_ms if self._file is not None else 0.0

    async def _pump(self, source: MicStream | FileStream, q: asyncio.Queue[bytes]) -> None:
        async for chunk in source.chunks():
            if len(chunk) < CHUNK_BYTES:
                # FileStream's very last chunk before EOF can be shorter than
                # CHUNK_BYTES (whatever's left in the file) -- pad it so
                # _mix_pcm always sees two equal-length buffers, same as
                # every other chunk. MicStream itself never yields a short
                # chunk, but padding here rather than assuming that keeps
                # this pump correct regardless of the source.
                chunk = chunk + bytes(CHUNK_BYTES - len(chunk))
            try:
                q.put_nowait(chunk)
            except asyncio.QueueFull:
                # Drop the oldest rather than the newest to stay close to
                # real time if something briefly falls behind -- matches
                # MicStream's own backlog-capping rationale.
                with contextlib.suppress(asyncio.QueueEmpty):
                    q.get_nowait()
                q.put_nowait(chunk)

    async def chunks(self) -> AsyncIterator[bytes]:
        """Yields fixed-size 320 ms PCM chunks mixed from both sub-sources,
        paced to real time by this method itself (not by draining the
        sub-sources' own chunks() directly): each sub-source is pumped into
        its own queue by a background task, and this loop takes whatever is
        currently available from each queue every tick -- silence if
        nothing is (the file is paused, has finished, or is momentarily
        behind) -- instead of waiting on both together. That's essential:
        if this waited for both queues every tick, a paused (or finished)
        file would stall the live mic side too, which is exactly what
        pause_file() must NOT do.
        """
        mic_task = asyncio.create_task(self._pump(self._mic, self._mic_q))
        if self._file is not None and self._file_task is None:
            # set_file() may have already started a pump task before chunks()
            # was ever iterated (e.g. a file picked while the session was
            # still connecting) -- don't overwrite it and orphan it.
            self._file_task = asyncio.create_task(self._pump(self._file, self._file_q))
        silence = bytes(CHUNK_BYTES)
        pacer = RealtimePacer(CHUNK_MS)
        try:
            while True:
                if self._paused.is_set():
                    # Whole-session pause: drain and discard everything queued
                    # so far from both sub-sources (same backlog-draining
                    # reasoning as MicStream.chunks()), then block here
                    # without yielding until resumed -- feed()'s `async for`
                    # simply stalls, exactly like a paused MicStream/FileStream.
                    while True:
                        try:
                            self._mic_q.get_nowait()
                        except asyncio.QueueEmpty:
                            break
                    while True:
                        try:
                            self._file_q.get_nowait()
                        except asyncio.QueueEmpty:
                            break
                    await asyncio.sleep(0.05)
                    pacer.resync()  # don't try to "catch up" on paused time
                    continue
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
            with contextlib.suppress(asyncio.CancelledError):
                await mic_task
            # Holds self._file_lock so this can't race a concurrent set_file()
            # call: without it, a set_file() suspended awaiting the old
            # task's cancellation could install a brand new self._file_task
            # right after this teardown already ran, orphaning it (nothing
            # would ever cancel it again, since chunks() has already exited).
            async with self._file_lock:
                if self._file_task is not None:
                    self._file_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await self._file_task
                    self._file_task = None

    async def set_file(self, file: FileStream | None) -> None:
        """Live-swaps the file source: cancels and awaits any existing file
        pump task, drops any file audio still queued, closes the old
        FileStream (if any), then -- if given a new one -- enters it and
        starts a fresh pump task for it.

        Guarded by self._file_lock: this awaits the old pump task's
        cancellation, which yields control back to the event loop, so two
        overlapping calls (e.g. the user swaps files twice in quick
        succession) could otherwise race to set self._file_task and leak
        whichever one loses. The lock makes overlapping calls serialize
        instead of interleaving.
        """
        async with self._file_lock:
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


class OutputSink:
    """Plays received PCM chunks to a chosen output device (e.g. a virtual cable)."""

    def __init__(self, device: int | None = None):
        self._q: queue.Queue[np.ndarray] = queue.Queue(maxsize=100)
        self._buffer = np.zeros(0, dtype=np.int16)
        # Set from another thread by clear() (after a seek); only ever read/acted
        # on inside _on_playback, so self._buffer itself is written exclusively by
        # the realtime callback thread -- no lock needed. A lock here previously
        # caused an intermittent native crash: blocking a PortAudio callback on a
        # Python lock can collide with the stream's own stop()/close() teardown.
        self._clear_requested = threading.Event()
        self._stream = sd.OutputStream(
            samplerate=RATE,
            channels=CHANNELS,
            dtype="int16",
            device=device,
            callback=self._on_playback,
            extra_settings=_wasapi_extra_settings(),
        )

    def _on_playback(self, outdata, frames, time_info, status) -> None:
        if self._clear_requested.is_set():
            self._clear_requested.clear()
            self._buffer = np.zeros(0, dtype=np.int16)
            with self._q.mutex:
                self._q.queue.clear()
        while len(self._buffer) < frames:
            try:
                self._buffer = np.concatenate([self._buffer, self._q.get_nowait()])
            except queue.Empty:
                break
        if len(self._buffer) >= frames:
            outdata[:] = self._buffer[:frames].reshape(-1, 1)
            self._buffer = self._buffer[frames:]
        else:
            outdata.fill(0)

    def __enter__(self) -> "OutputSink":
        try:
            self._stream.start()
        except Exception:
            self._stream.close()
            raise
        return self

    def __exit__(self, *exc_info) -> None:
        self._stream.stop()
        self._stream.close()

    def play(self, pcm: bytes) -> None:
        try:
            self._q.put_nowait(np.frombuffer(pcm, dtype=np.int16))
        except queue.Full:
            pass  # drop rather than build unbounded latency
        self._trim_backlog()

    def _trim_backlog(self) -> None:
        """Caps how far playback can fall behind the live translation.

        Measured live: the server can deliver translated audio for a segment
        slightly FASTER than that segment's own playback duration (observed
        ~0.8-0.9x real time). With nothing bounding it, received-but-not-yet-
        played audio piles up in self._q over the course of a session --
        text stays live while the voice drifts further and further behind it
        (the exact "whole sentence, even the next one, is already showing
        before he's even started speaking it" symptom this was written to
        fix). queue.Queue's own lock is already safely used from both this
        thread and the realtime callback thread elsewhere in this class (see
        _on_playback's get_nowait()), so briefly holding it here to drop the
        OLDEST queued audio down to MAX_OUTPUT_BACKLOG_SAMPLES is safe --
        unlike the custom Python lock previously removed from __init__ (see
        _clear_requested), this doesn't hold a lock across the callback's own
        blocking work, just a quick internal deque trim.
        """
        with self._q.mutex:
            backlog = sum(len(item) for item in self._q.queue)
            while backlog > MAX_OUTPUT_BACKLOG_SAMPLES and self._q.queue:
                backlog -= len(self._q.queue.popleft())

    def clear(self) -> None:
        """Drops any buffered-but-not-yet-played audio (e.g. right after a seek).

        Just raises a flag: the actual clearing happens inside _on_playback, on
        the realtime callback thread, so self._buffer is never written from two
        threads at once (see __init__).
        """
        self._clear_requested.set()
