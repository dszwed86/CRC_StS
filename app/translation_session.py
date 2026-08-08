"""Bridges an audio source (mic or file) to the Palabra S2S API and an output sink.

Callers own the lifecycle of the source/sink (MicStream/FileStream, OutputSink from
audio_io.py) — this module only reads chunks() from the source and calls play() on
the sink, so it stays agnostic of actual device handling.
"""

from __future__ import annotations

import asyncio
import contextlib
import copy
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Protocol

from palabra_ai import Audio, Palabra, ServerWarning, Transcript
from palabra_ai.exc import PalabraError

from .audio_io import FileStream


class AudioSource(Protocol):
    def chunks(self): ...  # async generator[bytes]


class AudioSink(Protocol):
    def play(self, pcm: bytes) -> None: ...


class SessionState(Enum):
    CONNECTING = auto()
    RUNNING = auto()
    PAUSED = auto()
    STOPPED = auto()
    ERROR = auto()


@dataclass
class TranscriptEvent:
    text: str
    language: str
    is_translation: bool
    is_final: bool
    # Real creation time (process-monotonic), not wall clock. Lets the overlay's
    # paragraph-merging use the ORIGINAL timing between sentences even when
    # events are replayed later (e.g. backfilling an overlay opened mid-session)
    # rather than the replay time, which would otherwise make unrelated
    # sentences spoken minutes apart look like they happened back-to-back.
    timestamp: float = field(default_factory=time.monotonic)


class TranslationRunner:
    """Runs one live S2S translation session until the source is exhausted or stop() is called."""

    def __init__(
        self,
        api_key: str,
        region: str,
        source_lang: str,
        target_lang: str,
        source: AudioSource,
        sink: AudioSink,
        on_state: Callable[[SessionState], None] = lambda s: None,
        on_transcript: Callable[[TranscriptEvent], None] = lambda t: None,
        on_error: Callable[[str], None] = lambda e: None,
        stop_event: threading.Event | None = None,
        voice_id: str | None = None,
        voice_cloning: bool = False,
        mute_output: bool = False,
    ):
        self._palabra = Palabra(api_key=api_key, region=region)
        self._source_lang = source_lang
        self._target_lang = target_lang
        self._source = source
        self._sink = sink
        self._voice_id = voice_id
        self._voice_cloning = voice_cloning
        # Palabra has no server-side option to skip speech generation (it
        # only lets you configure HOW the voice sounds, not whether it's
        # produced at all -- confirmed against the docs), so translated
        # audio is still generated and billed the same either way. This
        # just stops the app from routing/playing it locally, for a
        # subtitles-only workflow.
        self._mute_output = mute_output
        self._on_state = on_state
        self._on_transcript = on_transcript
        self._on_error = on_error
        # threading.Event (not asyncio.Event): stop() must be safe to call from any
        # thread at any time, including before run()'s event loop even starts.
        self._stop = stop_event if stop_event is not None else threading.Event()
        self._session = None  # set once the session is entered; used by pause/resume/seek

    def stop(self) -> None:
        """Requests a graceful stop: feeding ends, session.end() flushes the translation tail."""
        if hasattr(self._source, "resume"):
            self._source.resume()  # unblock a paused source so feed() can observe the stop
        self._stop.set()

    def request_pause(self) -> None:
        """Pauses a pausable source (e.g. FileStream) and the server-side task (stops billing too).

        Must be called from the loop's own thread (e.g. via call_soon_threadsafe).
        """
        if self._session is None or not hasattr(self._source, "pause"):
            return
        asyncio.create_task(self._do_pause())

    async def _do_pause(self) -> None:
        # stop() may have already run by the time this (queued via
        # call_soon_threadsafe from the GUI thread) actually executes -- e.g.
        # the user clicked Pauza then Stop in quick succession. stop() always
        # resumes the source before setting self._stop precisely so a paused
        # source can't block feed() forever, but if we pause it AFTER that,
        # nothing ever resumes it again and feed() hangs (a paused source's
        # chunks() never yields, which is the only place feed() re-checks
        # self._stop). Bail out here instead of undoing stop()'s resume().
        if self._stop.is_set():
            return
        try:
            self._source.pause()
            await self._session.pause()
            self._on_state(SessionState.PAUSED)
        except PalabraError as e:
            self._on_error(f"Błąd: {e}")
        except Exception as e:
            self._on_error(f"Nieoczekiwany błąd: {e}")

    def request_resume(self) -> None:
        """Resumes the server-side task and a paused source. Same threading rule as request_pause."""
        if self._session is None:
            return
        asyncio.create_task(self._do_resume())

    async def _do_resume(self) -> None:
        try:
            await self._session.resume()
            if hasattr(self._source, "resume"):
                self._source.resume()
            self._on_state(SessionState.RUNNING)
        except PalabraError as e:
            self._on_error(f"Błąd: {e}")
        except Exception as e:
            self._on_error(f"Nieoczekiwany błąd: {e}")

    def request_change_mic_device(self, device_index: int) -> None:
        """Swaps the physical input device a live MicStream reads from.

        Unlike request_change_voice (which needs set_task() -- a real
        server-side call), this is purely local device I/O: the Palabra
        session never sees which physical microphone produced the PCM
        bytes it receives, so this never touches the session at all. Same
        threading rule as request_pause/request_seek.
        """
        if hasattr(self._source, "switch_device"):
            try:
                self._source.switch_device(device_index)
            except Exception as e:
                self._on_error(f"Nie udało się przełączyć mikrofonu: {e}")

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

    def request_seek(self, position_ms: float) -> None:
        """Jumps a seekable source to position_ms and drops in-flight audio for a clean cut."""
        if not hasattr(self._source, "seek"):
            return
        self._source.seek(position_ms)
        if hasattr(self._sink, "clear"):
            self._sink.clear()
        if self._session is not None:
            asyncio.create_task(self._do_flush())

    async def _do_flush(self) -> None:
        try:
            await self._session.flush()
        except PalabraError as e:
            self._on_error(f"Błąd: {e}")
        except Exception as e:
            self._on_error(f"Nieoczekiwany błąd: {e}")

    def request_change_voice(self, voice_id: str | None, voice_cloning: bool) -> None:
        """Switches the TTS voice for the rest of the session via set_task()
        ("update settings on the fly" per the SDK) -- no reconnect needed.
        Same threading rule as request_pause/request_seek.
        """
        if self._session is None:
            return
        asyncio.create_task(self._do_change_voice(voice_id, voice_cloning))

    async def _do_change_voice(self, voice_id: str | None, voice_cloning: bool) -> None:
        # Briefly pause the source (not the reported SessionState -- the UI
        # doesn't need to show "Wstrzymano" for this) around the set_task()
        # call: the server likely reinitializes its speech-generation
        # pipeline for the new voice, and continuing to stream audio while
        # that happens showed up as a spurious "arriving faster than
        # real-time" warning even though our own send pacing measured
        # correctly throughout. Pausing/resuming the source also makes it
        # resync its own pacing anchor afterwards (already the case for both
        # MicStream and FileStream), instead of racing to catch up.
        pausable = hasattr(self._source, "pause") and hasattr(self._source, "resume")
        if pausable:
            self._source.pause()
        try:
            task = copy.deepcopy(self._session.task)
            speech_gen: dict[str, object] = {}
            if voice_cloning:
                speech_gen["voice_cloning"] = True
            elif voice_id is not None:
                speech_gen["voice_id"] = voice_id
            task["pipeline"]["translations"][0]["speech_generation"] = speech_gen
            await self._session.set_task(task)
        except PalabraError as e:
            self._on_error(f"Błąd: {e}")
        except Exception as e:
            self._on_error(f"Nieoczekiwany błąd: {e}")
        finally:
            if pausable:
                self._source.resume()

    async def run(self) -> None:
        self._on_state(SessionState.CONNECTING)
        try:
            async with self._palabra.translation(
                source=self._source_lang,
                targets=[self._target_lang],
                voice_id=self._voice_id,
                voice_cloning=self._voice_cloning,
                # Lower perceived latency: translate partial (still-forming)
                # transcriptions instead of waiting for each segment to be
                # fully confirmed, and confirm a segment after a shorter
                # silence gap (server default 0.7s). Trade-off: an earlier
                # translation can occasionally get revised once the full
                # segment is heard, and a speaker who pauses mid-sentence
                # more than ~0.5s may see it split a bit eagerly.
                translate_partials=True,
                silence_threshold=0.5,
            ) as session:
                self._session = session
                self._on_state(SessionState.RUNNING)

                async def feed() -> None:
                    async for chunk in self._source.chunks():
                        if self._stop.is_set():
                            break
                        await session.send_audio(chunk)
                    await session.end(eos_timeout=4)

                feeder = asyncio.create_task(feed())
                # If feed() raises (e.g. the source's chunks() blows up immediately --
                # a corrupt/unsupported file, or the mic disappearing mid-session) with
                # no audio ever having been sent, the receive loop below has nothing to
                # do but wait for a server event that will now never arrive: it would
                # otherwise hang forever, with Stop unable to help since nothing here
                # ever checks self._stop between iterations of `async for event in
                # session`. This callback cancels the still-running receive loop (i.e.
                # this coroutine's own task) as soon as feed() fails, so the `finally`
                # below can pick up feed()'s real exception via `await feeder` and
                # report it properly instead of hanging.
                receiving_task = asyncio.current_task()

                def _abort_receive_on_feed_failure(t: asyncio.Task) -> None:
                    if not t.cancelled() and t.exception() is not None and receiving_task is not None:
                        receiving_task.cancel()

                feeder.add_done_callback(_abort_receive_on_feed_failure)
                try:
                    async for event in session:
                        if isinstance(event, Transcript):
                            self._on_transcript(
                                TranscriptEvent(
                                    text=event.text,
                                    language=event.language,
                                    is_translation=event.is_translation,
                                    is_final=event.is_eos,
                                )
                            )
                        elif isinstance(event, Audio):
                            if not self._mute_output:
                                self._sink.play(event.pcm)
                        elif isinstance(event, ServerWarning):
                            self._on_error(f"Ostrzeżenie: {event.message}")
                finally:
                    feeder.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await feeder
            self._on_state(SessionState.STOPPED)
        except PalabraError as e:
            self._on_error(f"Błąd: {e}")
            self._on_state(SessionState.ERROR)
        except Exception as e:  # unexpected (network, device, ...) — surface, don't crash silently
            self._on_error(f"Nieoczekiwany błąd: {e}")
            self._on_state(SessionState.ERROR)
