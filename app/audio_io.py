"""Audio device enumeration plus microphone capture / playback streaming.

PCM format throughout is 16-bit signed little-endian, mono, matching what
the Palabra API expects -- but at two different rates (see RATE/INPUT_RATE
below): audio sent TO the server (mic/file) is captured/resampled to
INPUT_RATE, while audio received FROM the server (TTS output) is always
RATE, the server's fixed output rate (palabra_ai.audio.OUTPUT_SAMPLE_RATE).
"""

from __future__ import annotations

import asyncio
import contextlib
import queue
import threading
import time
import wave
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TypeVar

import numpy as np
import sounddevice as sd
from palabra_ai import load_pcm

from .i18n import tr

RATE = 24000  # output/playback rate -- fixed server-side (palabra_ai.audio.OUTPUT_SAMPLE_RATE)
# Input (mic/file capture, sent to the server) rate. Lower than RATE:
# measured via a real A/B test against the live API -- confirmed both on
# synthetic speech and, since real mic input carries room noise/reverb/
# sibilants a synthetic clip doesn't, on a real 2-minute recording (a
# genuine sermon, not TTS) -- that 16kHz cuts ASR/confirmation latency with
# the transcribed text staying byte-identical to 24kHz's, so there's no
# accuracy cost to sending less data per chunk. 48kHz measured no better
# than 24kHz either, so only lowering helps. Independent from RATE:
# whatever we capture at gets resampled to this before being sent, exactly
# like a device whose native rate doesn't match (see _needs_resampling).
INPUT_RATE = 16000
CHANNELS = 1
# Ceiling for the mic/file gain sliders' boost range (1.0 = original level,
# above that amplifies a source that's too quiet even at 100%). 4.0 = +12dB --
# enough headroom for a genuinely quiet recording without inviting a user to
# push a merely-normal source into constant clipping.
MAX_GAIN_BOOST = 4.0
CHUNK_MS = 320
CHUNK_SAMPLES = int(INPUT_RATE * CHUNK_MS / 1000)
CHUNK_BYTES = CHUNK_SAMPLES * 2  # int16 = 2 bytes/sample
BYTES_PER_MS = INPUT_RATE * CHANNELS * 2 / 1000
TRAILING_SILENCE_MS = 2000  # appended so the server can always finalize the last segment
MAX_MIC_BACKLOG_BYTES = CHUNK_BYTES * 2  # ~640ms -- see MicStream.chunks()
STREAM_OPEN_RETRY_ATTEMPTS = 5
STREAM_OPEN_RETRY_DELAY_SECONDS = 1.0
# See MicStream.level/OutputSink.level: a live PortAudio callback fires
# continuously (every ~10-20ms for these buffer sizes) for as long as its
# stream is genuinely alive, silence included -- if the device dies (a
# laptop wakes from sleep before WASAPI has recovered, a USB
# mic/interface/virtual-cable driver resets, a device is unplugged), the
# callback simply stops firing. This threshold is generous slack above
# the normal callback interval, so a real gap of this length reliably
# means the stream is dead, not just briefly scheduled late.
STREAM_STALE_SECONDS = 0.5

_T = TypeVar("_T")


def _open_with_retry(factory: Callable[[], _T]) -> _T:
    """Retries opening a PortAudio stream a few times with a short delay on
    sd.PortAudioError -- in particular paInternalError ([PaErrorCode -9986]),
    which real-world logs from this app showed happening repeatedly (up to
    6 times across ~30s in one session) and then resolving on its own (a
    user manually clicking Start again eventually worked). This is a
    well-known transient failure on Windows WASAPI -- e.g. another app
    briefly holding exclusive access to the same device, or the Windows
    audio engine mid-reinitializing a device -- not something a single
    failed attempt should treat as fatal. The original budget here (3
    attempts, 0.5s apart, ~1s total) turned out too short for some of those
    real cases -- widened to 5 attempts / 1s apart (~4s total) so a single
    Start click has a real chance to ride out the conflict instead of
    requiring the user to keep re-clicking.

    Other exception types (a bad device index, invalid parameters) are not
    retried -- those fail identically every time, so retrying would only
    add delay before reporting the same error.
    """
    last_error: sd.PortAudioError | None = None
    for attempt in range(STREAM_OPEN_RETRY_ATTEMPTS):
        try:
            return factory()
        except sd.PortAudioError as e:
            last_error = e
            if attempt < STREAM_OPEN_RETRY_ATTEMPTS - 1:
                time.sleep(STREAM_OPEN_RETRY_DELAY_SECONDS)
    raise last_error


async def _open_with_retry_async(factory: Callable[[], _T]) -> _T:
    """Same retry/backoff logic as _open_with_retry(), but yields to the
    event loop between attempts (await asyncio.sleep) instead of blocking
    it with time.sleep(). For use from within an already-running asyncio
    loop -- MicStream.switch_device(), called mid-session while run()'s
    receive loop and feed() need to keep progressing on the same thread --
    unlike the plain synchronous version, which is fine for the initial
    device open in SessionWorker.start() (that happens before the loop
    starts pumping any coroutines at all, so blocking there is harmless).
    A real production trace showed this matters: a flaky device hitting
    all 5 retries would otherwise stall the whole session (including the
    receive loop processing server events) for the full ~4s budget.
    """
    last_error: sd.PortAudioError | None = None
    for attempt in range(STREAM_OPEN_RETRY_ATTEMPTS):
        try:
            return factory()
        except sd.PortAudioError as e:
            last_error = e
            if attempt < STREAM_OPEN_RETRY_ATTEMPTS - 1:
                await asyncio.sleep(STREAM_OPEN_RETRY_DELAY_SECONDS)
    raise last_error


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
                        f"{path.name}: {tr('obsługiwany jest tylko 16-bitowy WAV (plik ma')} "
                        f"{w.getsampwidth() * 8} {tr('bitów)')}."
                    )
                if w.getnframes() == 0:
                    raise ValueError(f"{path.name}: {tr('plik WAV nie zawiera dźwięku.')}")
        except ValueError:
            raise
        except (wave.Error, OSError) as e:
            raise ValueError(f"{path.name}: {tr('nie można otworzyć pliku')} ({e}).") from e
        return
    try:
        import av
    except ImportError as e:
        raise ImportError(f"{tr('Sprawdzenie')} {path.name} {tr('wymaga pakietu av: uv add av')}") from e
    try:
        with av.open(str(path)) as container:
            if not container.streams.audio:
                raise ValueError(f"{path.name}: {tr('plik nie zawiera ścieżki audio.')}")
    except ValueError:
        raise
    except Exception as e:
        # av can raise several distinct FFmpeg-backed exception types for a
        # corrupt/unsupported/unreadable file -- catching broadly here is a
        # deliberate boundary (this function's whole job is "translate
        # whatever's wrong with this file into one readable message"),
        # matching the existing broad except in SessionWorker.start().
        raise ValueError(f"{path.name}: {tr('nie można otworzyć pliku')} ({e}).") from e


# Populated by preload_file(), consumed (and popped -- single use, see
# FileStream._decode()) once a real FileStream is constructed for the same
# path. Lets a file picked before a session/event loop exists (see
# SessionWorker.start()'s own comment on why FileStream can only be built
# once a loop is running) start decoding right away instead of only once
# Start is actually clicked -- the wait a user felt clicking "Start pliku"
# was this decode only beginning at that exact moment, for a file that
# could've been decoding for however long it sat selected first.
_preload_events: dict[str, threading.Event] = {}
_preload_results: dict[str, bytes | None] = {}
# Events superseded by a later preload_file() call for a DIFFERENT path while
# their own _work() was still decoding -- see preload_file()'s own comment.
# A plain set keyed by object identity (Event has no custom __eq__/__hash__,
# so this is exact and holding a real reference keeps it safe from Python
# reusing the same id() for something unrelated later).
_preload_stale_events: set[threading.Event] = set()
_preload_lock = threading.Lock()


def preload_file(path: str | Path) -> None:
    """Starts decoding path in the background ahead of time (e.g. right after
    it's picked in the file dialog). Safe to call repeatedly for the same
    path -- a call while an earlier one is still decoding is a no-op."""
    key = str(Path(path))
    with _preload_lock:
        if key in _preload_events:
            return
        # Only one file is ever "selected" in the GUI at a time -- drop any
        # previous entry rather than accumulating one per file ever picked.
        # Without this, browsing several candidate files before starting a
        # session (a normal thing to do) pinned each one's full decoded PCM
        # in memory for the rest of the process's life. Popping/clearing the
        # dicts here isn't enough on its own though: the superseded path's
        # _work() thread (started by an EARLIER preload_file() call, still
        # running) doesn't know it's been dropped and would otherwise still
        # write its result into _preload_results once it finishes decoding,
        # re-leaking the exact thing this was meant to fix -- so any event(s)
        # still sitting here (not yet claimed by a real FileStream, see
        # below) get flagged in _preload_stale_events for _work() to check.
        # An event a FileStream._decode() has ALREADY popped out (genuinely
        # being consumed right now) is no longer in this dict, so it's never
        # flagged here -- exactly the case that must be left alone.
        for stale_event in _preload_events.values():
            _preload_stale_events.add(stale_event)
        _preload_events.clear()
        _preload_results.clear()
        event = threading.Event()
        _preload_events[key] = event

    def _work() -> None:
        try:
            pcm = load_pcm(path, sample_rate=INPUT_RATE, channels=CHANNELS)
        except Exception:
            pcm = None  # decode failed -- FileStream._decode() retries synchronously and reports the real error
        with _preload_lock:
            if event in _preload_stale_events:
                _preload_stale_events.discard(event)
            else:
                _preload_results[key] = pcm
        event.set()

    threading.Thread(target=_work, daemon=True).start()


@dataclass
class DeviceInfo:
    index: int
    name: str
    max_input_channels: int
    max_output_channels: int


def rescan_devices() -> None:
    """Forces PortAudio to re-scan hardware (e.g. a mic plugged in after startup).

    sounddevice/PortAudio snapshot the device list at initialization; re-running
    init is the standard workaround to pick up hardware changes. Only safe
    while no PortAudio stream is open -- callers are expected to have closed
    any MicStream/OutputSink of their own first (see e.g. MainWindow's
    _on_refresh_devices, which closes/reopens its mic level-meter preview
    around this call).
    """
    # play_test_tone()'s sd.play() is a separate, unmanaged global stream this
    # module doesn't otherwise track -- stop() it first (a no-op if nothing is
    # playing) so a still-running test tone can't be caught mid-playback when
    # _terminate() tears down the whole PortAudio session under it.
    sd.stop()
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


def _needs_resampling(device: int | None, target_rate: int) -> int | None:
    """Returns a device's native sample rate if it doesn't match target_rate
    AND this platform lacks WASAPI's auto_convert -- meaning PortAudio won't
    reliably convert for us and MicStream/OutputSink must resample
    themselves (see _LinearResampler). None otherwise: either WASAPI will
    handle it, or the device's own rate already matches target_rate, so the
    caller should just open at target_rate directly, today's untouched default.

    Used for BOTH capture (target_rate=INPUT_RATE) and playback
    (target_rate=RATE) -- confirmed via chunks()'s own long-standing comment
    that even WASAPI's auto_convert isn't perfectly real-time-accurate, so a
    platform with NO conversion at all (macOS) plausibly has the same class
    of problem on input as the one a real user report confirmed on output
    (screechy/crackling playback); nothing here assumes input is fine just
    because nobody happened to report it.
    """
    if _wasapi_extra_settings() is not None:
        return None
    try:
        native_rate = int(round(sd.query_devices(device)["default_samplerate"]))
    except Exception:
        return None
    if native_rate and native_rate != target_rate:
        return native_rate
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


def play_test_tone(device: int | None, duration_s: float = 1.0, freq: float = 440.0) -> None:
    """Plays a short sine-wave test tone on the given output device, without
    touching the Palabra API at all -- lets the user confirm OBS is actually
    picking up sound from this device before starting a real (paid) session.
    Non-blocking: sd.play() manages its own playback stream in the
    background, so this returns immediately.
    """
    t = np.linspace(0, duration_s, int(RATE * duration_s), endpoint=False)
    tone = (0.3 * np.sin(2 * np.pi * freq * t)).astype(np.float32)
    # Same as every other stream this module opens (MicStream, OutputSink):
    # without WASAPI's auto_convert, opening at the fixed 24kHz RATE fails
    # outright with "Invalid sample rate" on any device whose native rate
    # differs (in practice, nearly every real device) -- confirmed via a
    # real QA pass where this button silently never played anything on any
    # output device tried.
    sd.play(tone, samplerate=RATE, device=device, extra_settings=_wasapi_extra_settings())


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

    def __init__(self, device: int | None = None, channel: int | None = None):
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
        # elsewhere in this file. Exposed through the level property below,
        # not read directly -- see its docstring for why.
        self._level: float = 0.0
        self._last_callback_at: float = time.monotonic()
        # Which raw hardware input channel to capture, for a multi-channel
        # device (e.g. a 2-in audio interface like a Behringer UMC202HD,
        # with two different microphones on channels 1 and 2). None keeps
        # today's plain behavior (opens with channels=CHANNELS, i.e. mono)
        # completely untouched -- the vast majority of devices/users never
        # need this. An explicit index instead opens the stream with enough
        # channels to reach it and de-interleaves down to just that one
        # channel in _make_callback(), so everything downstream of this
        # class (chunks(), the rest of the pipeline) still only ever sees
        # plain mono PCM, exactly as before.
        self._channel = channel
        self._stream = self._open_stream(device)

    @property
    def level(self) -> float:
        """Reports 0.0 if the audio callback hasn't fired in a while
        (STREAM_STALE_SECONDS) instead of the last real reading -- a dead
        stream (device unplugged, driver reset, laptop just woke from
        sleep) otherwise leaves this frozen at whatever it last was,
        making the GUI's meter look alive when nothing is actually being
        captured."""
        if time.monotonic() - self._last_callback_at > STREAM_STALE_SECONDS:
            return 0.0
        return self._level

    def _open_stream(self, device: int | None) -> sd.RawInputStream:
        channel = self._channel
        # See _needs_resampling(): the same reasoning that produced a real,
        # reported "screechy/crackling" bug on OutputSink's playback path
        # (WASAPI's auto_convert covers this on Windows; nothing did on
        # macOS) applies just as much here -- a mismatched-rate capture
        # device would hand back pitch-shifted, crackling audio to
        # Palabra, just less obviously than a speaker (garbled speech
        # reads as "the ASR/translation is having a bad day", not as an
        # audio bug). Resample capture to INPUT_RATE ourselves in that case,
        # same as OutputSink does (to RATE) for playback.
        native_rate = _needs_resampling(device, INPUT_RATE)
        stream_rate = native_rate or INPUT_RATE
        resampler = _LinearResampler(native_rate, INPUT_RATE) if native_rate else None

        def _open(rate: int, resampler_for_rate: "_LinearResampler | None") -> sd.RawInputStream:
            return _open_with_retry(lambda: sd.RawInputStream(
                samplerate=rate,
                channels=(channel + 1) if channel is not None else CHANNELS,
                dtype="int16",
                device=device,
                callback=self._make_callback(channel, resampler_for_rate),
                extra_settings=_wasapi_extra_settings(),
            ))

        try:
            return _open(stream_rate, resampler)
        except Exception:
            if stream_rate == INPUT_RATE:
                raise
            # Same fallback reasoning as OutputSink: the device rejected
            # its own reported native rate -- fall back to plain INPUT_RATE
            # rather than failing to open the mic at all.
            return _open(INPUT_RATE, None)

    def _make_callback(self, channel: int | None, resampler: "_LinearResampler | None") -> Callable[..., None]:
        # A fresh closure per open (not a single self._on_audio bound method
        # reading self._channel): switch_device() below can change BOTH the
        # device and the channel together, but the OLD stream keeps calling
        # back on its own thread for a few more frames after the new one is
        # opened, right up until old_stream.stop() -- if the callback read
        # self._channel live, updating that attribute for the new stream
        # would retroactively (and incorrectly) change how the OLD stream's
        # still-in-flight callbacks try to de-interleave THEIR data, which
        # was captured at the OLD channel count. Capturing `channel` (and,
        # for the same reason, `resampler`) here instead ties each stream
        # to the settings it was actually opened with, for its whole
        # lifetime.
        def _on_audio(indata, frames, time_info, status) -> None:
            self._last_callback_at = time.monotonic()
            data = bytes(indata)
            if channel is not None and data:
                arr = np.frombuffer(data, dtype=np.int16).reshape(-1, channel + 1)
                data = arr[:, channel].tobytes()
            if resampler is not None and data:
                data = resampler.push(np.frombuffer(data, dtype=np.int16)).tobytes()
            if data:
                self._level = min(1.0, int(np.abs(np.frombuffer(data, dtype=np.int16)).max()) / 32767)
            else:
                self._level = 0.0
            try:
                self._q.put_nowait(data)
            except queue.Full:
                pass  # drop audio rather than block the audio-driver thread

        return _on_audio

    def __enter__(self) -> "MicStream":
        try:
            self._stream.start()
        except Exception:
            self._stream.close()
            raise
        return self

    def __exit__(self, *exc_info) -> None:
        # try/finally: this now runs far more often than "once per session at
        # clean shutdown" (its original, low-risk use) -- the mic level-meter
        # preview (see MainWindow._stop_mic_preview) calls it on every device/
        # channel change while idle, every few seconds via the auto device
        # refresh, and around every session start/stop. A stop() failure
        # (e.g. the device was physically unplugged) must not skip close()
        # and leak the underlying stream handle.
        try:
            self._stream.stop()
        finally:
            self._stream.close()

    async def switch_device(self, new_device: int | None, channel: int | None) -> None:
        """Swaps to a different physical input device (and/or which of its
        channels to capture, see __init__'s channel comment) without
        disturbing chunks()'s already-running pacing loop: it only ever
        reads from self._q, filled by whichever callback is currently
        active regardless of which sd.RawInputStream is calling it, so
        nothing about the async generator or its pacing state needs to
        change -- only which stream object is open.

        The new stream is opened and started BEFORE the old one is
        stopped/closed, and self._stream/self._channel are only reassigned
        once that succeeds -- so a failure here (e.g. the new device
        doesn't support our fixed sample rate) leaves the working old
        stream untouched instead of leaving the session without any mic
        at all.

        Async (unlike _open_stream(), used for the initial device open):
        this runs while TranslationRunner.run()'s event loop is already
        pumping other coroutines (the receive loop, feed()), so retrying a
        flaky open must yield control between attempts (see
        _open_with_retry_async) instead of blocking the whole loop with
        time.sleep() for up to ~4s -- a real gap a production trace showed
        (the receive loop and feed() both stalling for the full retry
        budget on every mic switch that hit a transient PortAudio error).

        Must be called from the thread that originally opened this
        MicStream (SessionWorker's background thread, which has COM
        initialized on Windows for WASAPI -- see SessionWorker.start()) --
        awaiting this coroutine keeps it on that same thread, since it's
        driven by that thread's own event loop, not offloaded to a
        different one (which would need its own COM initialization).
        """
        native_rate = _needs_resampling(new_device, INPUT_RATE)
        stream_rate = native_rate or INPUT_RATE
        resampler = _LinearResampler(native_rate, INPUT_RATE) if native_rate else None

        async def _open(rate: int, resampler_for_rate: "_LinearResampler | None") -> sd.RawInputStream:
            new_stream = await _open_with_retry_async(lambda: sd.RawInputStream(
                samplerate=rate,
                channels=(channel + 1) if channel is not None else CHANNELS,
                dtype="int16",
                device=new_device,
                callback=self._make_callback(channel, resampler_for_rate),
                extra_settings=_wasapi_extra_settings(),
            ))
            new_stream.start()
            return new_stream

        try:
            new_stream = await _open(stream_rate, resampler)
        except Exception:
            if stream_rate == INPUT_RATE:
                raise
            new_stream = await _open(INPUT_RATE, None)  # see _open_stream's matching fallback
        old_stream = self._stream
        self._stream = new_stream
        self._channel = channel
        old_stream.stop()
        old_stream.close()

    def pause(self) -> None:
        self._paused.set()

    def resume(self) -> None:
        self._paused.clear()

    def set_gain(self, gain: float) -> None:
        """0.0 (silent) .. 1.0 (original level) .. MAX_GAIN_BOOST (boosted, for
        a source that's simply too quiet at 100%). Thread-safe; applied to the
        next chunks."""
        self._gain = max(0.0, min(MAX_GAIN_BOOST, gain))

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
        if gain == 1.0:
            return chunk
        if gain <= 0.0:
            return bytes(len(chunk))
        # clip (not wrap) on overflow -- gain > 1.0 boosts a source that's too
        # quiet even at its own original level, so pushing past int16 range is
        # expected on loud peaks; wrapping around would be far more audible
        # (harsh digital noise) than clipping.
        arr = np.frombuffer(chunk, dtype=np.int16).astype(np.float32) * gain
        return np.clip(arr, -32768, 32767).astype(np.int16).tobytes()


class FileStream:
    """Decodes an audio/video file and yields it as 320 ms PCM chunks, paced to
    real time — same interface as MicStream.chunks(), so a file behaves like a
    live source feeding the translation session.

    Supports pause()/resume() and seek() while streaming: position_ms/total_ms
    (in milliseconds) are updated live for a GUI to show a scrubber.
    """

    def __init__(self, path: str | Path, loop: asyncio.AbstractEventLoop | None = None):
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
        # entirely so it can't be starved by loop scheduling -- the point is
        # decoupling decode from chunks() being pumped, not from asyncio
        # itself.
        #
        # Signaled via an asyncio.Event (set cross-thread with
        # call_soon_threadsafe), not a threading.Event awaited through
        # asyncio.to_thread(): that used to pin one of the default
        # executor's worker threads, blocked in Event.wait(), for the WHOLE
        # remaining decode duration whenever chunks() was cancelled early
        # (e.g. a fast set_file() swap discarding this FileStream before its
        # decode finished) -- since nothing else in this app uses
        # asyncio.to_thread, that thread wasn't doing anything useful once
        # abandoned, but stayed pinned regardless. An asyncio.Event's wait()
        # is a plain, cheaply-cancellable coroutine await instead.
        self._decode_done = asyncio.Event()
        # loop= lets a caller that already holds the loop object (see
        # SessionWorker.start(), a plain synchronous method) pass it in
        # directly -- asyncio.get_running_loop() (the fallback here, for
        # TranslationRunner._do_set_file's case, a real async method)
        # requires a loop to be actively RUNNING at the call site, not just
        # set as current-for-this-thread via asyncio.set_event_loop(). At
        # session start, a file picked before Start is constructed from
        # SessionWorker.start() AFTER set_event_loop() but BEFORE
        # run_until_complete() -- the loop exists and is the right one, it
        # just isn't running yet -- so get_running_loop() unconditionally
        # raised "RuntimeError: no running event loop" there, confirmed
        # reproducible and matching a real user report ("Błąd urządzenia
        # audio: no running event loop" on Start with a file selected).
        self._loop = loop if loop is not None else asyncio.get_running_loop()
        self._pcm: bytes | None = None
        self._decode_error: Exception | None = None
        self._gain = 1.0
        threading.Thread(target=self._decode, daemon=True).start()

    def set_gain(self, gain: float) -> None:
        """0.0 (silent) .. 1.0 (original level) .. MAX_GAIN_BOOST (boosted, for
        a source file that's too quiet even at 100%). Thread-safe; applied to
        the next chunks -- see MicStream.set_gain(), same semantics."""
        self._gain = max(0.0, min(MAX_GAIN_BOOST, gain))

    def _apply_gain(self, chunk: bytes) -> bytes:
        gain = self._gain
        if gain == 1.0:
            return chunk
        if gain <= 0.0:
            return bytes(len(chunk))
        arr = np.frombuffer(chunk, dtype=np.int16).astype(np.float32) * gain
        return np.clip(arr, -32768, 32767).astype(np.int16).tobytes()

    def _decode(self) -> None:
        try:
            key = str(self._path)
            with _preload_lock:
                event = _preload_events.pop(key, None)
            if event is not None:
                # preload_file() already started (or finished) decoding this
                # exact path -- wait for that instead of decoding it twice.
                event.wait()
                with _preload_lock:
                    pcm = _preload_results.pop(key, None)
                if pcm is None:  # the preload failed -- decode here for real, so a genuine error still surfaces
                    pcm = load_pcm(self._path, sample_rate=INPUT_RATE, channels=CHANNELS)
            else:
                pcm = load_pcm(self._path, sample_rate=INPUT_RATE, channels=CHANNELS)
            self._pcm = pcm
            self.total_ms = len(pcm) / BYTES_PER_MS
        except Exception as e:  # decode failure (corrupt file, codec issue, ...) -- surfaced later by chunks()
            self._decode_error = e
        finally:
            self._loop.call_soon_threadsafe(self._decode_done.set)

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
        await self._decode_done.wait()
        if self._decode_error is not None:
            raise self._decode_error
        pcm = self._pcm
        # Real recordings usually trail off into silence, which is what lets the
        # server detect the end of the last phrase and finalize/translate it
        # within eos_timeout. A file that cuts off cold (e.g. synthesized audio,
        # or a hard trim) doesn't -- so append silence to guarantee that gap.
        # position_ms/seek() are unaffected: both stay clamped to total_ms.
        pcm += bytes(int(BYTES_PER_MS * TRAILING_SILENCE_MS))
        # Resume from wherever a PRIOR chunks() call left off, not always 0:
        # this method gets called fresh again -- restarting its local `pos`
        # from scratch -- every time TranslationRunner.run() reconnects
        # (feed() is a brand-new closure each reconnect, so is this
        # generator). self.position_ms is the one piece of state that
        # survives across those separate calls; without seeding `pos` from
        # it, a mixed-in file would silently rewind to the very beginning
        # and replay from scratch after any mid-session reconnect.
        pos = int(self.position_ms * BYTES_PER_MS)
        pos -= pos % 2  # keep 16-bit sample alignment
        pos = max(0, min(pos, len(pcm)))
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
            yield self._apply_gain(chunk)
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

    def __init__(self, mic: MicStream, file: FileStream | None = None, on_error: Callable[[str], None] = lambda e: None):
        self._mic = mic
        self._file = file
        self._on_error = on_error
        # Lives here, not on any one FileStream: a file gain the user set is a
        # standing preference for "this mixer's file input", meant to survive
        # swapping in a different file mid-session (see set_file() below,
        # which re-applies it to whatever FileStream it's handed) -- a fresh
        # FileStream on its own always starts back at its own default of 1.0.
        self._file_gain = 1.0
        if file is not None:
            file.set_gain(self._file_gain)
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

    def set_file_gain(self, gain: float) -> None:
        """Thread-safe, like MicStream.set_gain() -- stored here (not just
        forwarded to the current FileStream) so it survives a later
        set_file() swap; see __init__."""
        self._file_gain = max(0.0, min(MAX_GAIN_BOOST, gain))
        if self._file is not None:
            self._file.set_gain(self._file_gain)

    def resume_file(self) -> None:
        if self._file is not None:
            self._file.resume()

    def seek(self, position_ms: float) -> None:
        if self._file is not None:
            self._file.seek(position_ms)

    async def switch_device(self, new_device: int | None, channel: int | None) -> None:
        await self._mic.switch_device(new_device, channel)

    @property
    def mic_level(self) -> float:
        return self._mic.level

    @property
    def position_ms(self) -> float:
        return self._file.position_ms if self._file is not None else 0.0

    @property
    def total_ms(self) -> float:
        return self._file.total_ms if self._file is not None else 0.0

    async def _pump(self, source: MicStream | FileStream, q: asyncio.Queue[bytes], *, is_file: bool) -> None:
        try:
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
        except asyncio.CancelledError:
            raise  # normal teardown path (chunks()'s finally / set_file()) -- not a real failure
        except Exception as e:
            # Without this, a real failure here (confirmed reachable:
            # FileStream.chunks() re-raises self._decode_error if load_pcm()
            # fails on a file that passed the lighter demux-only probe_audio_file()
            # check but can't actually be fully decoded) was a task exception
            # NEVER retrieved by anyone -- chunks()'s own consumer loop only
            # ever does q.get_nowait()/falls back to silence, it never checks
            # whether the pump feeding that queue is even still alive. The
            # mixed session would just go silent for that source with no
            # error shown, indistinguishable from genuine silence.
            message = tr("Błąd odczytu pliku") if is_file else tr("Błąd odczytu mikrofonu")
            self._on_error(f"{message}: {e}")
            if is_file:
                # Drop the broken file from the mix (same cleanup set_file()
                # does) so the rest of the session -- the mic -- keeps going
                # instead of silently losing the file's contribution for the
                # rest of the session with no way to recover short of Stop.
                # Safe against set_file()/chunks()'s own cleanup racing this:
                # both already serialize through self._file_lock, and this
                # only runs on a genuine failure (re-raised CancelledError
                # above means an in-progress .cancel() never reaches here).
                async with self._file_lock:
                    if self._file is not None:
                        self._file.__exit__(None, None, None)
                        self._file = None
                    self._file_task = None

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
        mic_task = asyncio.create_task(self._pump(self._mic, self._mic_q, is_file=False))
        if self._file is not None and self._file_task is None:
            # set_file() may have already started a pump task before chunks()
            # was ever iterated (e.g. a file picked while the session was
            # still connecting) -- don't overwrite it and orphan it.
            self._file_task = asyncio.create_task(self._pump(self._file, self._file_q, is_file=True))
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
                file.set_gain(self._file_gain)
                file.__enter__()
                self._file_task = asyncio.create_task(self._pump(self._file, self._file_q, is_file=True))


class _LinearResampler:
    """Streaming linear-interpolation resampler for mono int16 PCM.

    Continuous across successive push() calls: carries the trailing
    fractional source position and a couple of un-consumed source samples
    forward internally, so back-to-back chunks resample as one continuous
    stream instead of each being interpolated in isolation -- the latter
    would leave an audible click at every chunk boundary (every server
    Audio event, not on any fixed schedule).
    """

    def __init__(self, from_rate: int, to_rate: int):
        self._step = from_rate / to_rate  # source samples per output sample
        self._buf = np.zeros(0, dtype=np.float64)
        self._pos = 0.0  # next output sample's source index, relative to self._buf[0]

    def push(self, pcm: np.ndarray) -> np.ndarray:
        self._buf = np.concatenate([self._buf, pcm.astype(np.float64)])
        out = []
        while self._pos + 1.0 < len(self._buf):
            i = int(self._pos)
            frac = self._pos - i
            out.append(self._buf[i] * (1 - frac) + self._buf[i + 1] * frac)
            self._pos += self._step
        consumed = int(self._pos)
        if consumed > 0:
            self._buf = self._buf[consumed:]
            self._pos -= consumed
        if not out:
            return np.zeros(0, dtype=np.int16)
        return np.clip(np.array(out), -32768, 32767).astype(np.int16)


class OutputSink:
    """Plays received PCM chunks to a chosen output device (e.g. a virtual cable)."""

    def __init__(self, device: int | None = None):
        # Unbounded: a maxsize/backlog cap here used to silently discard
        # translated audio once the queue filled up, to keep spoken audio
        # from drifting far behind the live subtitle text when the server
        # generates TTS faster than real-time. Confirmed via a real user
        # report to sometimes chew into the MIDDLE of a sentence that had
        # already started playing (its early chunks played, a burst of
        # its later chunks got dropped, only its last chunk survived) --
        # heard as "plays the start of a sentence, then jumps straight to
        # its end". Told explicitly this is unacceptable: never drop
        # translated audio, even at the cost of the voice occasionally
        # lagging behind the displayed text during a burst -- it self-
        # corrects during the next pause, since nothing new arrives while
        # the queue keeps draining at real-time rate.
        self._q: queue.Queue[np.ndarray] = queue.Queue()
        self._buffer = np.zeros(0, dtype=np.int16)
        # Live output level (0.0-1.0, peak amplitude), for a GUI meter that
        # confirms translated audio is actually reaching the device -- not
        # just that it was received (a stalled/empty playback buffer still
        # counts as "not reaching the device"). Computed from outdata, the
        # ACTUAL samples handed to the audio driver each callback (silence
        # included), same "single writer, plain float" reasoning as
        # MicStream.level. Exposed through the level property below, not
        # read directly -- see its docstring for why.
        self._level: float = 0.0
        self._last_callback_at: float = time.monotonic()
        # Set from another thread by clear() (after a seek); only ever read/acted
        # on inside _on_playback, so self._buffer itself is written exclusively by
        # the realtime callback thread -- no lock needed. A lock here previously
        # caused an intermittent native crash: blocking a PortAudio callback on a
        # Python lock can collide with the stream's own stop()/close() teardown.
        self._clear_requested = threading.Event()
        # See _needs_resampling(): WASAPI's auto_convert handles a
        # mismatched device rate on Windows; without an equivalent,
        # CoreAudio (macOS) doesn't reliably resample a stream opened at a
        # rate the device doesn't natively support -- confirmed as the
        # cause of a real report (some Mac speakers/headphones sounding
        # "screechy"/"crackly"). Resample our fixed-rate PCM to the
        # device's own native rate ourselves in that case.
        native_rate = _needs_resampling(device, RATE)
        output_rate = native_rate or RATE
        self._resampler = _LinearResampler(RATE, native_rate) if native_rate else None

        def _open(rate: int) -> sd.OutputStream:
            return _open_with_retry(lambda: sd.OutputStream(
                samplerate=rate,
                channels=CHANNELS,
                dtype="int16",
                device=device,
                callback=self._on_playback,
                extra_settings=_wasapi_extra_settings(),
            ))

        try:
            self._stream = _open(output_rate)
        except Exception:
            if output_rate == RATE:
                raise
            # The device rejected its own reported native rate (a stale or
            # wrong query result, or the rate changed underneath us --
            # e.g. a Bluetooth device switching profiles). Fall back to
            # the plain RATE that always worked before this resampling
            # was added, rather than failing to start the session at all
            # -- the original screechy-audio bug this exists to fix beats
            # no audio whatsoever.
            output_rate = RATE
            self._resampler = None
            self._stream = _open(RATE)

    @property
    def level(self) -> float:
        """Same staleness handling as MicStream.level -- see its docstring."""
        if time.monotonic() - self._last_callback_at > STREAM_STALE_SECONDS:
            return 0.0
        return self._level

    def _on_playback(self, outdata, frames, time_info, status) -> None:
        self._last_callback_at = time.monotonic()
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
        self._level = min(1.0, int(np.abs(outdata).max()) / 32767) if outdata.size else 0.0

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
        arr = np.frombuffer(pcm, dtype=np.int16)
        if self._resampler is not None:
            arr = self._resampler.push(arr)
            if arr.size == 0:
                return
        self._q.put_nowait(arr)  # unbounded -- see __init__, never drop translated audio

    def clear(self) -> None:
        """Drops any buffered-but-not-yet-played audio (e.g. right after a seek).

        Just raises a flag: the actual clearing happens inside _on_playback, on
        the realtime callback thread, so self._buffer is never written from two
        threads at once (see __init__).
        """
        self._clear_requested.set()
