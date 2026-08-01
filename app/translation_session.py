"""Bridges an audio source (mic or file) to the Palabra S2S API and an output sink.

Callers own the lifecycle of the source/sink (MicStream/FileStream, OutputSink from
audio_io.py) — this module only reads chunks() from the source and calls play() on
the sink, so it stays agnostic of actual device handling.
"""

from __future__ import annotations

import asyncio
import contextlib
import threading
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum, auto
from typing import Protocol

from palabra_ai import Audio, Palabra, ServerWarning, Transcript
from palabra_ai.exc import PalabraError


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
    ):
        self._palabra = Palabra(api_key=api_key, region=region)
        self._source_lang = source_lang
        self._target_lang = target_lang
        self._source = source
        self._sink = sink
        self._voice_id = voice_id
        self._voice_cloning = voice_cloning
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

    async def run(self) -> None:
        self._on_state(SessionState.CONNECTING)
        try:
            async with self._palabra.translation(
                source=self._source_lang,
                targets=[self._target_lang],
                voice_id=self._voice_id,
                voice_cloning=self._voice_cloning,
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
