"""Bridges an audio source (mic or file) to the Palabra S2S API and an output sink.

Callers own the lifecycle of the source/sink (MicStream/FileStream, OutputSink from
audio_io.py) — this module only reads chunks() from the source and calls play() on
the sink, so it stays agnostic of actual device handling.
"""

from __future__ import annotations

import asyncio
import contextlib
import copy
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Protocol

import websockets
from palabra_ai import Audio, Palabra, Raw, ServerError, ServerWarning, Transcript, build_task
from palabra_ai.exc import NotReadyError, PalabraError, SessionError

from .audio_io import INPUT_RATE, FileStream
from .i18n import tr

# Auto-reconnect (see TranslationRunner.run()): SessionError ("WebSocket
# connection or session failure"), NotReadyError (pipeline didn't confirm
# in time), and websockets.exceptions.ConnectionClosed are retried -- all
# three are plausibly transient network/timing blips. The last one matters
# because palabra_ai's own send-side calls (send_audio/end/pause/resume/
# flush/set_task, see client.py's _send()) call the raw websocket's .send()
# directly with NO try/except -- a drop discovered while WE are sending
# (as opposed to the receive loop silently swallowing it, see run()'s
# docstring) surfaces as a raw websockets exception, not a palabra_ai one.
# Real production logs showed this exact gap: "Nieoczekiwany błąd: no close
# frame received or sent" (ConnectionClosedError's own message) went
# straight to a terminal ERROR instead of triggering a reconnect.
# AuthError/TaskError are never retried: a bad key or a rejected command
# won't fix itself by reconnecting, so failing fast matches today's
# behavior for those.
#
# Retries are UNBOUNDED: run() keeps reconnecting on any of the three
# above until either a connection succeeds or the user presses Stop --
# per explicit product decision, a translator that gives up on its own
# after a few attempts is worse than one that keeps trying through a
# rough patch of network and lets the user decide when to abandon it.
RECONNECT_BACKOFF_SECONDS = (2.0, 5.0, 10.0)
# A connection must stay up this long before a later drop resets the retry
# counter back to the SHORT end of the backoff schedule -- otherwise a
# server that accepts the connection but drops it again almost immediately
# (flapping) would keep resetting to the short delay on every attempt
# instead of properly backing off further and further.
RECONNECT_STABLE_SECONDS = 10.0


def _is_auth_rejection(exc: BaseException) -> bool:
    """True if exc is a SessionError wrapping a WebSocket handshake that was
    rejected with HTTP 401/403 (see palabra_ai's client.py: any connect
    failure -- `websockets.connect()` raising -- is wrapped as
    `raise SessionError(...) from e`, so the original cause survives as
    `__cause__`). This is a bad/expired/revoked API key or an exhausted
    account balance, confirmed via a real test against the API -- none of
    which will ever fix itself by retrying, unlike a genuine dropped
    connection. The SDK's own AuthError is NOT raised for this case (only
    for a missing key entirely, see client.py) -- a rejected key surfaces
    exactly the same way a network blip would, which is why this needs an
    explicit check rather than relying on exception type alone."""
    cause = exc.__cause__
    return isinstance(cause, websockets.exceptions.InvalidStatus) and cause.response.status_code in (401, 403)


class AudioSource(Protocol):
    def chunks(self): ...  # async generator[bytes]


class AudioSink(Protocol):
    def play(self, pcm: bytes) -> None: ...


class SessionState(Enum):
    CONNECTING = auto()
    RECONNECTING = auto()
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
        church_style: bool = False,
    ):
        self._palabra = Palabra(api_key=api_key, region=region)
        self._source_lang = source_lang
        self._target_lang = target_lang
        self._source = source
        self._sink = sink
        self._voice_id = voice_id
        self._voice_cloning = voice_cloning
        # Nudges Palabra's MT output toward a sermon/religious register
        # (server-side style="church_catholic") -- measured via a real A/B
        # test against the live API: no added latency, and it fixes concrete
        # translation errors a plain/no-style translation made on real
        # sermon audio (wrong numbers, non-idiomatic biblical phrasing).
        # Rewords a large share of sentences stylistically though, so it's a
        # static per-session choice (like voice_id/source_lang), not
        # live-switchable -- switching it mid-session via set_task was never
        # tested.
        self._church_style = church_style
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
        # Tracks the user's own pause intent, independent of self._session
        # (which is torn down and recreated on every reconnect) -- see
        # run()'s use of this right after a reconnect completes.
        self._paused_by_user = False
        # Serializes the request_*() family (pause/resume/flush/change-voice/
        # set-file): each queues its _do_*() onto the loop via
        # asyncio.create_task() with no ordering guarantee of its own, so a
        # quick double-click (e.g. Pauza then Wznow) could otherwise let
        # whichever network round-trip resolves LAST win, not whichever was
        # clicked last. This makes them run strictly in the order they were
        # queued instead.
        self._request_lock = asyncio.Lock()

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
        async with self._request_lock:
            # stop() may have already run by the time this (queued via
            # call_soon_threadsafe from the GUI thread) actually executes --
            # e.g. the user clicked Pauza then Stop in quick succession.
            # stop() always resumes the source before setting self._stop
            # precisely so a paused source can't block feed() forever, but if
            # we pause it AFTER that, nothing ever resumes it again and
            # feed() hangs (a paused source's chunks() never yields, which is
            # the only place feed() re-checks self._stop). Bail out here
            # instead of undoing stop()'s resume().
            if self._stop.is_set():
                return
            try:
                self._source.pause()
                if self._session is None:
                    # self._session is torn down to None on EVERY reconnect
                    # (see run()'s `finally: self._session = None`),
                    # independent of self._stop -- and can go stale while
                    # THIS request sat queued behind _request_lock (e.g. a
                    # slow _do_change_voice() holding it while a drop
                    # happened underneath). There's no live session left to
                    # tell -- but the pause intent is still real: the source
                    # is already paused above, so just record the intent and
                    # let run()'s own reconnect-completion logic (which
                    # already checks _paused_by_user and re-applies pause +
                    # reports PAUSED once a fresh session connects) pick it
                    # up from here, instead of letting `await
                    # self._session.pause()` below raise AttributeError on
                    # None with nothing left to undo the pause() call above.
                    self._paused_by_user = True
                    return
                await self._session.pause()
                # stop() runs synchronously on the GUI thread and can land
                # at ANY point during the two awaits above -- including
                # strictly between this coroutine's own is_set() check
                # (above) and here. If that happens, stop()'s resume() call
                # already ran (and found nothing paused yet, so it did
                # nothing) before OUR pause() call above re-paused the
                # source -- nothing left in the system will ever call
                # resume() again, hanging feed() forever. Re-checking here,
                # after the pause work is done, and undoing it ourselves if
                # stop() won the race in the meantime, closes that window.
                if self._stop.is_set():
                    if hasattr(self._source, "resume"):
                        self._source.resume()
                    return
                self._paused_by_user = True
                self._on_state(SessionState.PAUSED)
            except PalabraError as e:
                self._on_error(f"{tr('Błąd')}: {e}")
            except Exception as e:
                self._on_error(f"{tr('Nieoczekiwany błąd')}: {e}")

    def request_resume(self) -> None:
        """Resumes the server-side task and a paused source. Same threading rule as request_pause."""
        if self._session is None:
            return
        asyncio.create_task(self._do_resume())

    async def _do_resume(self) -> None:
        async with self._request_lock:
            if self._stop.is_set():
                return
            try:
                if self._session is None:
                    # Same staleness as _do_pause() above -- mid-reconnect,
                    # no live session to tell yet, but the resume intent is
                    # real: unblock the source now (so feed() doesn't stay
                    # artificially paused once a fresh session connects) and
                    # clear the pause flag so run()'s reconnect-completion
                    # logic reports RUNNING on the new session instead of
                    # re-pausing it.
                    if hasattr(self._source, "resume"):
                        self._source.resume()
                    self._paused_by_user = False
                    return
                await self._session.resume()
                if hasattr(self._source, "resume"):
                    self._source.resume()
                self._paused_by_user = False
                self._on_state(SessionState.RUNNING)
            except PalabraError as e:
                self._on_error(f"{tr('Błąd')}: {e}")
            except Exception as e:
                self._on_error(f"{tr('Nieoczekiwany błąd')}: {e}")

    def request_change_mic_device(self, device_index: int, channel: int | None = None) -> None:
        """Swaps the physical input device (and/or which of its channels to
        capture, for a multi-channel device -- see MicStream's channel
        comment) a live MicStream reads from.

        Unlike request_change_voice (which needs set_task() -- a real
        server-side call), this is purely local device I/O: the Palabra
        session never sees which physical microphone produced the PCM
        bytes it receives, so this never touches the session at all -- no
        self._request_lock needed either, since it shares no mutable state
        with the session-level requests that lock serializes. Same
        threading rule as request_pause/request_seek.

        Queued via create_task() like the others (this used to call
        switch_device() directly, synchronously): opening the new device
        can hit the same transient PortAudio errors _open_with_retry is
        built to ride out, and switch_device() is now itself async
        specifically so those retries yield to the loop instead of
        blocking run()'s receive loop and feed() for the whole retry
        budget (~4s) on a flaky device.
        """
        if hasattr(self._source, "switch_device"):
            asyncio.create_task(self._do_change_mic_device(device_index, channel))

    async def _do_change_mic_device(self, device_index: int, channel: int | None) -> None:
        try:
            await self._source.switch_device(device_index, channel)
        except Exception as e:
            self._on_error(f"{tr('Nie udało się przełączyć mikrofonu')}: {e}")

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
        async with self._request_lock:
            if self._stop.is_set():
                return
            try:
                file = FileStream(path) if path is not None else None
                if file is not None:
                    file.pause()  # never autoplay a freshly added/changed file
                await self._source.set_file(file)
            except Exception as e:
                self._on_error(f"{tr('Nie udało się ustawić pliku')}: {e}")

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
        async with self._request_lock:
            if self._stop.is_set() or self._session is None:
                # self._session goes stale (None) on every reconnect,
                # independent of self._stop, and can flip while this request
                # sat queued behind _request_lock -- the seek itself already
                # happened synchronously in request_seek() regardless, so
                # there's nothing useful left to flush against a session
                # that no longer exists (and no source-side state to
                # unwind here, unlike _do_pause/_do_resume).
                return
            try:
                # palabra_ai renamed this SDK method flush() -> interrupt()
                # between 2.0.2 and 2.1.1, and the OLD name's underlying
                # wire message ("flush_task") is no longer accepted by the
                # live server at all -- confirmed via a real seek/scrub
                # call: every attempt failed with "Błąd serwera:
                # VALIDATION_ERROR -- Message should contain non-empty
                # message_type field" (a real user report), because
                # "flush_task" isn't in the server's own list of valid
                # message types anymore. Bumped the pinned SDK version
                # (requirements.txt) alongside this rename.
                await self._session.interrupt()
            except PalabraError as e:
                self._on_error(f"{tr('Błąd')}: {e}")
            except Exception as e:
                self._on_error(f"{tr('Nieoczekiwany błąd')}: {e}")

    def request_change_voice(self, voice_id: str | None, voice_cloning: bool) -> None:
        """Switches the TTS voice for the rest of the session via set_task()
        ("update settings on the fly" per the SDK) -- no reconnect needed.
        Same threading rule as request_pause/request_seek.
        """
        if self._session is None:
            return
        asyncio.create_task(self._do_change_voice(voice_id, voice_cloning))

    async def _do_change_voice(self, voice_id: str | None, voice_cloning: bool) -> None:
        async with self._request_lock:
            if self._stop.is_set():
                return
            # Briefly pause the source (not the reported SessionState -- the
            # UI doesn't need to show "Wstrzymano" for this) around the
            # set_task() call: the server likely reinitializes its speech-
            # generation pipeline for the new voice, and continuing to
            # stream audio while that happens showed up as a spurious
            # "arriving faster than real-time" warning even though our own
            # send pacing measured correctly throughout. Pausing/resuming
            # the source also makes it resync its own pacing anchor
            # afterwards (already the case for both MicStream and
            # FileStream), instead of racing to catch up.
            #
            # was_paused_by_user remembers whether the user had ALREADY
            # paused before this call started -- if so, the `finally` below
            # must NOT resume the source afterwards: doing so unconditionally
            # (as this used to) silently resumed real audio/billing behind a
            # GUI still showing "Wstrzymano", with request_resume() the only
            # (and now confusingly redundant) way to notice anything was off.
            pausable = hasattr(self._source, "pause") and hasattr(self._source, "resume")
            was_paused_by_user = self._paused_by_user
            if pausable:
                self._source.pause()
            try:
                if self._session is None:
                    # Mid-reconnect (self._session goes stale on every
                    # reconnect, independent of self._stop) -- nothing to
                    # change voice on right now. The `finally` below still
                    # correctly restores the source's pause state either
                    # way, this just skips a confusing "NoneType has no
                    # attribute 'task'" message for what is really just
                    # "try again once reconnected."
                    return
                task = copy.deepcopy(self._session.task)
                speech_gen: dict[str, object] = {}
                if voice_cloning:
                    speech_gen["voice_cloning"] = True
                elif voice_id is not None:
                    speech_gen["voice_id"] = voice_id
                task["pipeline"]["translations"][0]["speech_generation"] = speech_gen
                await self._session.set_task(task)
                # Without this, a reconnect after a live voice change (see
                # run()'s `self._palabra.translation(..., voice_id=self._voice_id,
                # voice_cloning=self._voice_cloning, ...)`, which always builds
                # the NEW session from these two fields, never from the old
                # session's own mutated task) would silently revert to
                # whichever voice was set at __init__ time.
                self._voice_id = voice_id
                self._voice_cloning = voice_cloning
                if was_paused_by_user:
                    # set_task() "also resumes after pause()" per the SDK's
                    # own docstring -- without re-pausing here, changing
                    # voice while paused silently resumed the server-side
                    # task (and its billing) behind a GUI still showing
                    # "Wstrzymano", with no way left to notice.
                    await self._session.pause()
            except PalabraError as e:
                self._on_error(f"{tr('Błąd')}: {e}")
            except Exception as e:
                self._on_error(f"{tr('Nieoczekiwany błąd')}: {e}")
            finally:
                if pausable and not was_paused_by_user:
                    self._source.resume()

    async def _connect_or_stop(self, ctx):
        """Awaits ctx.__aenter__(), racing it against self._stop.

        Without this, a Stop click during the connect handshake (websocket
        connect + up to the SDK's own READY_TIMEOUT=30s waiting for the
        pipeline to confirm the task) was silently ignored -- run()'s only
        other self._stop checks are inside feed()'s per-chunk loop and the
        backoff wait, both of which only run once a session already exists.

        Returns the connected session, or None if stop won the race. On
        that path the in-flight __aenter__() is cancelled; TranslationSession
        .__aenter__'s own `except BaseException: await self.close()` handles
        cleaning up the half-open socket/receive task from that
        cancellation, so nothing is leaked here.
        """
        enter_task = asyncio.create_task(ctx.__aenter__())
        while not enter_task.done():
            if self._stop.is_set():
                enter_task.cancel()
                with contextlib.suppress(BaseException):
                    await enter_task
                return None
            await asyncio.wait({enter_task}, timeout=0.2)
        return enter_task.result()

    async def run(self) -> None:
        """Runs the session, auto-reconnecting indefinitely (with growing
        backoff, see RECONNECT_BACKOFF_SECONDS) on a dropped connection until
        either a connection succeeds or the user presses Stop -- see
        RECONNECT_BACKOFF_SECONDS's comment for exactly which failures
        qualify. Audio spoken during a reconnect gap is NOT buffered/replayed: the
        source (mic/file) keeps advancing in real time regardless, so
        whatever was "said" during the gap is simply never sent -- picking
        the session back up live once reconnected, rather than risking a
        replayed backlog triggering the server's "arriving faster than
        real-time" warning (the same failure mode already hit once trying
        to change voice mid-stream).

        IMPORTANT, found by reading palabra_ai's own source (client.py):
        TranslationSession._receive_loop() catches websockets.ConnectionClosed
        and silently swallows it (does NOT set _recv_error) before putting an
        end-of-stream sentinel -- so `async for event in session:` ending on
        its own raises NOTHING, not even SessionError, whether the server
        closed cleanly OR the connection just dropped out from under us.
        Those two cases are indistinguishable from any exception we could
        catch. The only reliable signal this app has to tell them apart is
        self._stop: MixedSource is an infinite source (mic never runs dry),
        so feed() only ever calls session.end() because self._stop was set --
        there is no other legitimate "natural end" for this app. So: if the
        receive loop ends quietly and self._stop is NOT set, treat it exactly
        like a dropped connection and reconnect.
        """
        attempt = 0
        # See _is_auth_rejection()'s use below: requires the SAME rejection
        # to repeat before concluding it's a real bad key/exhausted balance
        # rather than failing fast on a single 401/403 -- a proxy/WAF/rate
        # limiter in front of the API could plausibly answer a legitimate
        # reconnect attempt (now unbounded, see RECONNECT_BACKOFF_SECONDS's
        # comment) with a 401/403 of its own during a rough patch of
        # network, and that single transient response looks identical to a
        # real auth failure with no way to tell them apart otherwise.
        consecutive_auth_rejections = 0
        while True:
            self._on_state(SessionState.RECONNECTING if attempt > 0 else SessionState.CONNECTING)
            connection_dropped = False
            drop_message = ""
            connected_at: float | None = None
            try:
                # A mapping (not a plain [target_lang] list) merges "style"
                # into this target's own translations[] entry -- build_task()
                # only supports per-target overrides that way (see its
                # docstring/source); a bare list has no per-target dict for
                # this to land in.
                targets = {self._target_lang: {"style": "church_catholic"}} if self._church_style else [self._target_lang]
                task = build_task(
                    self._source_lang,
                    targets,
                    voice_id=self._voice_id,
                    voice_cloning=self._voice_cloning,
                    # Send captured/file audio at 16kHz instead of build_task's
                    # own 24kHz default -- see audio_io.INPUT_RATE for the
                    # real-API A/B test (including on real, noisy speech) this
                    # is based on. MicStream/FileStream already do the actual
                    # resampling to this rate; this just tells the server what
                    # rate to expect.
                    input_sample_rate=INPUT_RATE,
                    # Lower perceived latency: translate partial (still-forming)
                    # transcriptions instead of waiting for each segment to be
                    # fully confirmed, and confirm a segment after a shorter
                    # silence gap (server default 0.7s; 0.3 is the server's
                    # enforced floor). Trade-off: an earlier translation can
                    # occasionally get revised once the full segment is heard,
                    # and a speaker who pauses mid-sentence may see it split a
                    # bit eagerly. Measured via a real A/B test against the
                    # live API (synthetic speech incl. a sample with 0.35-0.45s
                    # mid-sentence hesitations): confirmation latency drops by
                    # 0.26-1.4s depending on how continuous the speech is, with
                    # translated text byte-identical to 0.5's -- segmentation
                    # only got marginally more eager (8 -> 9 segments on the
                    # hesitation sample), so the extra split risk is real but
                    # small. translate_partials itself was also verified NOT
                    # to affect audio latency at all (audio only starts after
                    # the full translated_transcription anyway) -- it's purely
                    # what makes partial subtitle text show up early.
                    translate_partials=True,
                    # Raised from the earlier 0.3 back up towards a middle
                    # ground with the original 0.5 (see the A/B test measured
                    # in the comment above this) -- real usage (a speaker who
                    # pauses mid-sentence for emphasis, e.g. preaching) showed
                    # 0.3 split segments more eagerly than wanted; 0.4 keeps
                    # most of 0.3's latency win while giving pauses a bit more
                    # room before the server calls a segment done.
                    silence_threshold=0.4,
                    # REVERTED (see below) -- left here so the next person
                    # who considers disabling sentence_splitter again knows
                    # why it was tried and reverted, not just that it wasn't:
                    #
                    # The server's own sentence_splitter (on by default) was
                    # measured via a real A/B test to force-split long
                    # sentences mid-clause with no pause anywhere near the
                    # cut, occasionally producing a duplicated, ungrammatical
                    # seam or (once) a wrong-gender/-person translation on
                    # the subjectless second half. Disabling it
                    # (transcription={"sentence_splitter": {"enabled":
                    # False}}) removed both defects on that test -- but a
                    # real live user then hit a MUCH worse failure mode on a
                    # genuinely long, pause-free run of speech (several
                    # sentences spoken back-to-back with no real gap,
                    # sermon-style): with nothing left to close a segment
                    # except silence, the segment just kept growing --
                    # confirmed via a real A/B replay of the exact reported
                    # text: time-to-first-audio went from ~8s (splitter on)
                    # to 16.3s (splitter off), with 102 partial-text
                    # revisions flickering on screen the whole time and dead
                    # air on the actual translated audio. For a LIVE
                    # broadcast tool, that stall is a worse failure than an
                    # occasional awkward split -- reverted to the server
                    # default (leave sentence_splitter untouched) on that
                    # basis. If revisiting this, look for a length/duration
                    # cap on sentence_splitter itself (not just on/off) --
                    # none was documented as of this test.
                )
                # Widen the server's internal TTS output queue beyond
                # build_task()'s implicit (tight) default -- not exposed as
                # a build_task() kwarg, so set directly on the task dict.
                # Measured via a real A/B test: the tight default compresses/
                # speeds up generated speech to keep the queue shallow, and
                # produced ~40% more simulated playback-buffer starvation
                # than this wider target -- exactly the unstable, robotic
                # timing this app has been fighting. Costs a bit more
                # steady-state buffered latency in exchange. 2000/4000 is
                # the server-enforced minimum for desired/max; auto_tempo
                # left at its default since the same test found no measurable
                # effect from it at this queue depth.
                task["pipeline"]["translation_queue_configs"] = {
                    "global": {"desired_queue_level_ms": 5000, "max_queue_level_ms": 9000}
                }
                ctx = self._palabra.translation(task=task)
                session = await self._connect_or_stop(ctx)
                if session is None:
                    # self._stop was set while still connecting -- see
                    # _connect_or_stop's docstring.
                    self._on_state(SessionState.STOPPED)
                    return
                async with contextlib.AsyncExitStack() as stack:
                    # ctx.__aenter__() already ran (inside _connect_or_stop);
                    # this registers its matching __aexit__() to run on the
                    # way out, with the same exception-propagation contract
                    # a plain `async with ctx as session:` would have given
                    # -- we just needed the enter half to be cancellable.
                    stack.push_async_exit(ctx)
                    self._session = session
                    connected_at = time.monotonic()
                    consecutive_auth_rejections = 0  # the key just got accepted -- it's not the problem
                    async with self._request_lock:
                        # Re-check _paused_by_user only AFTER acquiring the
                        # lock, not before: a request_resume()/request_pause()
                        # queued via call_soon_threadsafe right as this
                        # reconnect landed could otherwise race this
                        # session.pause() call -- whichever finished last used
                        # to silently win over the user's actual last click.
                        # Every other place that touches
                        # session.pause()/.resume()/.set_task() already goes
                        # through this same lock (_do_pause/_do_resume/
                        # _do_change_voice); this direct call was the one
                        # left outside it.
                        if self._paused_by_user:
                            # A reconnect landed while the user had this
                            # session paused (see request_pause()/_do_pause()):
                            # the source itself is still correctly blocked
                            # (its own pause flag survives across reconnects,
                            # since self._source is never recreated), but the
                            # freshly (re)connected session doesn't know about
                            # that on its own -- without this, the code below
                            # would unconditionally report RUNNING, resetting
                            # the GUI's Pauza button to "unpaused" while the
                            # source stays silently, permanently stuck not
                            # feeding any audio, with no way left to un-stick
                            # it.
                            await session.pause()
                            self._on_state(SessionState.PAUSED)
                        else:
                            self._on_state(SessionState.RUNNING)

                    async def feed() -> None:
                        # Explicitly closes the generator (async with
                        # contextlib.aclosing) instead of just letting `break`/
                        # cancellation abandon it: the source's chunks()
                        # (MixedSource) spawns background pump tasks in its own
                        # `finally:`, cleaned up only when aclose() actually
                        # runs. Left implicit, that depended entirely on
                        # CPython's refcounting + asyncio's asyncgen-finalizer
                        # hook scheduling aclose() as a SEPARATE task with no
                        # guarantee it gets a turn before this coroutine (and
                        # the loop shutting down around it) moves on --
                        # confirmed reproducible as a genuine "Task was
                        # destroyed but it is pending!" leak when nothing else
                        # happens to yield the loop enough turns afterward.
                        async with contextlib.aclosing(self._source.chunks()) as gen:
                            async for chunk in gen:
                                if self._stop.is_set():
                                    break
                                await session.send_audio(chunk)
                        try:
                            await session.end(eos_timeout=4)
                        except TypeError:
                            # Defends against palabra_ai SDK drift: production
                            # logs caught "TranslationSession.end() got an
                            # unexpected keyword argument 'eos_timeout'" after
                            # an SDK version resolved at a different build
                            # didn't support this parameter yet. Losing the
                            # eos_timeout tail-wait is a much smaller problem
                            # than every single Stop click surfacing as an
                            # error.
                            await session.end()

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
                                self._on_error(f"{tr('Ostrzeżenie')}: {event.message}")
                            elif isinstance(event, ServerError):
                                # Distinct from ServerWarning: the SDK's own
                                # docs describe post-readiness ServerError as
                                # "recoverable errors" the stream survives on
                                # its own (e.g. a rejected set_task from
                                # request_change_voice) -- surfaced so the
                                # user has SOME diagnostic instead of a
                                # silently-ignored request.
                                self._on_error(f"{tr('Błąd serwera')}: {event.code} — {event.desc}")
                            elif isinstance(event, Raw):
                                # Whatever message_type isn't one of the
                                # cases above -- per the SDK's own docstring,
                                # possibly pipeline_timings/tts_buffer_stats,
                                # which this app doesn't parse or use for
                                # anything yet. Diagnostic-only: printed to
                                # stderr (invisible in the packaged
                                # --windowed build, so this never clutters a
                                # real user's log) rather than routed through
                                # self._on_error, purely so a session run
                                # from source can reveal whether the server
                                # actually sends these and what they contain.
                                print(f"[Raw event] type={event.type} data={event.data}", file=sys.stderr)
                    finally:
                        feeder.cancel()
                        with contextlib.suppress(asyncio.CancelledError):
                            await feeder
                # Reached only when the block above exits WITHOUT an
                # exception -- see this method's docstring: that alone
                # doesn't tell us whether this was a clean, requested end
                # or the connection dropping silently underneath us.
                if self._stop.is_set():
                    self._on_state(SessionState.STOPPED)
                    return
                connection_dropped = True
                drop_message = tr("połączenie zostało zerwane")
            except (SessionError, NotReadyError, websockets.exceptions.ConnectionClosed) as e:
                if isinstance(e, SessionError) and _is_auth_rejection(e):
                    consecutive_auth_rejections += 1
                    if consecutive_auth_rejections >= 2:
                        # Two in a row, no successful connect in between --
                        # not a transient blip. Retrying forever (as of the
                        # unbounded-retry change above) would spam "ponawiam
                        # próbę" indistinguishable from a rough patch of
                        # network, while a wrong/expired key or an exhausted
                        # balance will never recover on its own. Fail fast
                        # instead, same as AuthError/TaskError below.
                        self._on_error(f"{tr('Błąd uwierzytelniania')}: {e}")
                        self._on_state(SessionState.STOPPED if self._stop.is_set() else SessionState.ERROR)
                        return
                    # First occurrence -- could be a real bad key, or could
                    # be a proxy/rate-limiter hiccup (see the comment on
                    # consecutive_auth_rejections above). Treat it like an
                    # ordinary drop for now; a genuine bad key will simply
                    # produce the same rejection again next attempt.
                connection_dropped = True
                drop_message = str(e)
            except PalabraError as e:
                self._on_error(f"{tr('Błąd')}: {e}")
                # If the user already asked to stop, this exception almost
                # certainly happened while trying to say a clean goodbye on
                # an already-dying connection (e.g. session.end() failing) --
                # the stop itself still succeeded, so report it as such
                # rather than alarming the user with "Error" for something
                # that was, from their point of view, a successful Stop.
                self._on_state(SessionState.STOPPED if self._stop.is_set() else SessionState.ERROR)
                return
            except Exception as e:  # unexpected (network, device, ...) — surface, don't crash silently
                self._on_error(f"{tr('Nieoczekiwany błąd')}: {e}")
                self._on_state(SessionState.STOPPED if self._stop.is_set() else SessionState.ERROR)
                return
            finally:
                self._session = None

            assert connection_dropped  # every path above either returned or set this
            if connected_at is not None and time.monotonic() - connected_at >= RECONNECT_STABLE_SECONDS:
                # This connection held up for a real while before dropping
                # again -- treat it as a fresh, independent outage and
                # restart the backoff schedule from its short end, rather
                # than continuing on from wherever a much earlier (possibly
                # unrelated) blip had left the counter.
                attempt = 0
            if self._stop.is_set():
                # The drop was detected exactly while (or after) the user
                # asked to stop -- e.g. send_audio()/session.end() hit an
                # already-dying connection during shutdown. The stop itself
                # still succeeded; report it as such instead of ERROR.
                self._on_state(SessionState.STOPPED)
                return
            # No attempt cap: retries are unbounded (see RECONNECT_BACKOFF_
            # SECONDS's comment) -- this loop keeps going until either a
            # connection succeeds or self._stop is set (checked above and in
            # the backoff wait below), never giving up and reporting ERROR
            # on its own for a dropped connection.
            backoff = RECONNECT_BACKOFF_SECONDS[min(attempt, len(RECONNECT_BACKOFF_SECONDS) - 1)]
            attempt += 1
            # Fire RECONNECTING now, not just at the top of the next loop
            # iteration (which only happens AFTER this backoff wait) --
            # otherwise the GUI status label stays stuck on "Tłumaczę na
            # żywo" for the whole 2-10s wait, and _on_state's own billable-
            # time fold (see gui.py's _on_state) never runs until then
            # either, so cost/duration would keep accumulating through a
            # window Palabra isn't actually billing for.
            self._on_state(SessionState.RECONNECTING)
            self._on_error(
                f"{tr('Połączenie przerwane')} ({drop_message}) -- {tr('ponawiam próbę')} "
                f"{attempt} {tr('za')} {backoff:.0f}s..."
            )
            elapsed = 0.0
            while elapsed < backoff:
                if self._stop.is_set():
                    self._on_state(SessionState.STOPPED)
                    return
                await asyncio.sleep(0.2)
                elapsed += 0.2
