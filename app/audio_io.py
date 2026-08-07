"""Audio device enumeration plus microphone capture / playback streaming.

PCM format throughout matches what the Palabra API expects: 16-bit signed
little-endian, mono, 24 kHz (see palabra_ai.audio.OUTPUT_SAMPLE_RATE).
"""

from __future__ import annotations

import asyncio
import queue
import threading
import time
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
        self._stream = sd.RawInputStream(
            samplerate=RATE,
            channels=CHANNELS,
            dtype="int16",
            device=device,
            callback=self._on_audio,
            extra_settings=_wasapi_extra_settings(),
        )

    def _on_audio(self, indata, frames, time_info, status) -> None:
        try:
            self._q.put_nowait(bytes(indata))
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
        anchor = time.monotonic()
        anchor_ms = 0.0
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
                anchor = time.monotonic()  # resync -- don't try to "catch up" on paused time
                anchor_ms = 0.0
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
                anchor = time.monotonic()  # we just dropped a backlog -- resync instead of pacing off a stale anchor
                anchor_ms = 0.0
            while len(pending) >= CHUNK_BYTES:
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
        pcm = await asyncio.to_thread(load_pcm, self._path, sample_rate=RATE, channels=CHANNELS)
        self.total_ms = len(pcm) / BYTES_PER_MS
        # Real recordings usually trail off into silence, which is what lets the
        # server detect the end of the last phrase and finalize/translate it
        # within eos_timeout. A file that cuts off cold (e.g. synthesized audio,
        # or a hard trim) doesn't -- so append silence to guarantee that gap.
        # position_ms/seek() are unaffected: both stay clamped to total_ms.
        pcm += bytes(int(BYTES_PER_MS * TRAILING_SILENCE_MS))
        pos = 0
        anchor = time.monotonic()
        anchor_ms = 0.0
        while True:
            if self._seek_to_ms is not None:
                pos = int(self._seek_to_ms * BYTES_PER_MS)
                pos -= pos % 2  # keep 16-bit sample alignment
                pos = max(0, min(pos, len(pcm)))
                self._seek_to_ms = None
                anchor = time.monotonic()
                anchor_ms = 0.0
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
            anchor_ms += CHUNK_MS
            delay = anchor + anchor_ms / 1000 - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
            else:
                # fell behind real time (paused, seeked, or a scheduling hiccup) —
                # resync instead of bursting out all the chunks we "owe"
                anchor = time.monotonic()
                anchor_ms = 0.0
        self.position_ms = self.total_ms


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

    def clear(self) -> None:
        """Drops any buffered-but-not-yet-played audio (e.g. right after a seek).

        Just raises a flag: the actual clearing happens inside _on_playback, on
        the realtime callback thread, so self._buffer is never written from two
        threads at once (see __init__).
        """
        self._clear_requested.set()
