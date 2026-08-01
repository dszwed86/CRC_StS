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


def list_input_devices() -> list[DeviceInfo]:
    return [
        DeviceInfo(i, d["name"], d["max_input_channels"], d["max_output_channels"])
        for i, d in enumerate(sd.query_devices())
        if d["max_input_channels"] > 0
    ]


def list_output_devices() -> list[DeviceInfo]:
    return [
        DeviceInfo(i, d["name"], d["max_input_channels"], d["max_output_channels"])
        for i, d in enumerate(sd.query_devices())
        if d["max_output_channels"] > 0
    ]


_VIRTUAL_CABLE_MARKERS = ("cable", "blackhole")


def find_virtual_cable(devices: list[DeviceInfo]) -> DeviceInfo | None:
    """Finds a likely virtual-audio-cable output (VB-Cable on Windows, BlackHole on macOS)."""
    for d in devices:
        lowered = d.name.lower()
        if any(marker in lowered for marker in _VIRTUAL_CABLE_MARKERS):
            return d
    return None


class MicStream:
    """Captures a microphone as an async stream of 320 ms PCM chunks.

    Supports pause()/resume() (stops feeding the session, e.g. to pause billing
    without losing the device) and set_gain() (0.0-1.0, for a live volume/mute
    slider that doesn't touch the session at all).
    """

    def __init__(self, device: int | None = None):
        self._q: queue.Queue[bytes] = queue.Queue(maxsize=100)
        self._paused = threading.Event()
        self._gain = 1.0
        self._stream = sd.RawInputStream(
            samplerate=RATE,
            channels=CHANNELS,
            dtype="int16",
            device=device,
            callback=self._on_audio,
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

    async def chunks(self) -> AsyncIterator[bytes]:
        """Yields fixed-size 320 ms PCM chunks, waiting for the mic as needed."""
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
                continue
            try:
                pending += self._q.get_nowait()
            except queue.Empty:
                await asyncio.sleep(0.005)
                continue
            while len(pending) >= CHUNK_BYTES:
                yield self._apply_gain(pending[:CHUNK_BYTES])
                pending = pending[CHUNK_BYTES:]

    def _apply_gain(self, chunk: bytes) -> bytes:
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
