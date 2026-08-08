"""Desktop GUI: pick a source (mic/file), languages and an output device, then
Start/Stop a live Palabra S2S translation session that plays its output onto the
chosen device (typically a virtual audio cable OBS picks up as a source).
"""

from __future__ import annotations

import asyncio
import contextlib
import sys
import threading

from PySide6.QtCore import QEventLoop, QObject, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QTextCursor
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSlider,
    QVBoxLayout,
    QWidget,
)
from palabra_ai import Palabra
from palabra_ai.exc import AuthError, PalabraError

from . import __version__, config
from .audio_io import (
    FileStream,
    MicStream,
    MixedSource,
    OutputSink,
    find_virtual_cable,
    is_virtual_cable_name,
    list_input_devices,
    list_output_devices,
    rescan_devices,
)
from .languages import DEFAULT_SOURCE, DEFAULT_TARGET, SOURCE_LANGUAGES, TARGET_LANGUAGES
from .overlay import OverlaySettingsDialog, OverlayWindow
from .translation_session import SessionState, TranscriptEvent, TranslationRunner

REGION = "eu"  # only region that currently serves the translation product
DASHBOARD_URL = "https://platform.palabra.ai/api-keys"  # account/keys dashboard (shows usage & balance)


class ApiKeyTester(QObject):
    """Checks whether an API key is accepted by Palabra, without starting a billed
    translation session: opens a Realtime STT connection (auth-only cost) and closes
    it immediately. Runs on its own plain thread so it never blocks the UI (see
    MainWindow._on_start_stop for why a plain threading.Thread is used instead of
    QThread).
    """

    finished = Signal(bool, str)

    def __init__(self, api_key: str, region: str):
        super().__init__()
        self._api_key = api_key
        self._region = region

    def run(self) -> None:
        try:
            asyncio.run(asyncio.wait_for(self._check(), timeout=10))
        except TimeoutError:
            self.finished.emit(False, "Przekroczono czas oczekiwania na odpowiedź serwera.")
            return
        except AuthError as e:
            self.finished.emit(False, f"Nieprawidłowy klucz API: {e}")
            return
        except PalabraError as e:
            self.finished.emit(False, f"Błąd połączenia: {e}")
            return
        except Exception as e:
            self.finished.emit(False, f"Nieoczekiwany błąd: {e}")
            return
        self.finished.emit(True, "Klucz API działa poprawnie.")

    async def _check(self) -> None:
        palabra = Palabra(api_key=self._api_key, region=self._region)
        async with palabra.stt():
            pass


class SettingsDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Ustawienia")
        creds = config.load_credentials()
        self._test_thread: threading.Thread | None = None
        self._test_worker: ApiKeyTester | None = None

        self.api_key_edit = QLineEdit(creds.api_key or "")
        self.api_key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.api_key_edit.setPlaceholderText("Klucz API z platform.palabra.ai/api-keys")

        form = QFormLayout()
        form.addRow("Klucz API Palabra:", self.api_key_edit)

        test_row = QHBoxLayout()
        self.test_btn = QPushButton("Testuj klucz")
        self.test_btn.clicked.connect(self._on_test_key)
        self.dashboard_btn = QPushButton("Otwórz panel Palabra (saldo, użycie)")
        self.dashboard_btn.clicked.connect(self._on_open_dashboard)
        test_row.addWidget(self.test_btn)
        test_row.addWidget(self.dashboard_btn)

        self.test_result_label = QLabel("")
        self.test_result_label.setWordWrap(True)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self._on_save)
        buttons.rejected.connect(self.reject)

        version_label = QLabel(f"wersja {__version__}")
        version_label.setStyleSheet("color: gray; font-size: 9pt;")
        version_label.setAlignment(Qt.AlignmentFlag.AlignRight)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addLayout(test_row)
        layout.addWidget(self.test_result_label)
        layout.addWidget(buttons)
        layout.addWidget(version_label)

    def _on_save(self) -> None:
        api_key = self.api_key_edit.text().strip()
        if not api_key:
            QMessageBox.warning(self, "Brak klucza", "Podaj klucz API przed zapisaniem.")
            return
        config.save_credentials(api_key, REGION)
        self.accept()

    def _on_open_dashboard(self) -> None:
        QDesktopServices.openUrl(QUrl(DASHBOARD_URL))

    def _on_test_key(self) -> None:
        api_key = self.api_key_edit.text().strip()
        if not api_key:
            QMessageBox.warning(self, "Brak klucza", "Wpisz klucz API przed testem.")
            return

        self.test_btn.setEnabled(False)
        self.test_result_label.setStyleSheet("")
        self.test_result_label.setText("Testowanie...")

        worker = ApiKeyTester(api_key, REGION)
        worker.finished.connect(self._on_test_finished, Qt.ConnectionType.QueuedConnection)

        thread = threading.Thread(target=worker.run, daemon=True)
        self._test_worker = worker
        self._test_thread = thread
        thread.start()

    def _on_test_finished(self, ok: bool, message: str) -> None:
        self.test_result_label.setStyleSheet("color: #2a7a2a;" if ok else "color: #b02a2a;")
        self.test_result_label.setText(("✓ " if ok else "✗ ") + message)
        self.test_btn.setEnabled(True)
        self._test_worker = None
        self._test_thread = None

    def closeEvent(self, event) -> None:
        if self._test_thread is not None:
            thread = self._test_thread
            wait_loop = QEventLoop()
            poll_timer = QTimer()
            poll_timer.timeout.connect(lambda: None if thread.is_alive() else wait_loop.quit())
            poll_timer.start(50)
            safety_timer = QTimer()
            safety_timer.setSingleShot(True)
            safety_timer.timeout.connect(wait_loop.quit)
            safety_timer.start(3000)
            wait_loop.exec()
        super().closeEvent(event)


class SavedVoicesDialog(QDialog):
    """Manages a small local library of named voice_id presets.

    Palabra doesn't expose an API to enumerate available voices -- IDs from
    the "Palabra Library" or a cloned voice are only visible in the
    account's own web portal (app.palabra.ai/voices), so they're copied by
    hand once and saved here under a friendly name to avoid re-pasting them
    on every session.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Zapisane głosy")
        self._voices = config.load_saved_voices()

        self.list_widget = QListWidget()
        self._refresh_list()

        hint = QLabel(
            "ID głosu skopiuj z portalu app.palabra.ai/voices (zakładka biblioteki głosów"
            " lub klonowanie)."
        )
        hint.setWordWrap(True)

        add_row = QHBoxLayout()
        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText("Nazwa (np. Lektor)")
        self.id_edit = QLineEdit()
        self.id_edit.setPlaceholderText("ID głosu z app.palabra.ai/voices")
        add_row.addWidget(self.name_edit)
        add_row.addWidget(self.id_edit)

        btn_row = QHBoxLayout()
        add_btn = QPushButton("Dodaj")
        add_btn.clicked.connect(self._on_add)
        remove_btn = QPushButton("Usuń zaznaczony")
        remove_btn.clicked.connect(self._on_remove)
        close_btn = QPushButton("Zamknij")
        close_btn.clicked.connect(self.close)
        btn_row.addWidget(add_btn)
        btn_row.addWidget(remove_btn)
        btn_row.addStretch()
        btn_row.addWidget(close_btn)

        layout = QVBoxLayout(self)
        layout.addWidget(hint)
        layout.addWidget(self.list_widget)
        layout.addLayout(add_row)
        layout.addLayout(btn_row)

    def _refresh_list(self) -> None:
        self.list_widget.clear()
        for v in self._voices:
            self.list_widget.addItem(f"{v['name']} — {v['voice_id']}")

    def _on_add(self) -> None:
        name = self.name_edit.text().strip()
        voice_id = self.id_edit.text().strip()
        if not name or not voice_id:
            QMessageBox.warning(self, "Brak danych", "Podaj nazwę i ID głosu.")
            return
        self._voices.append({"name": name, "voice_id": voice_id})
        config.save_saved_voices(self._voices)
        self.name_edit.clear()
        self.id_edit.clear()
        self._refresh_list()

    def _on_remove(self) -> None:
        row = self.list_widget.currentRow()
        if row < 0:
            return
        del self._voices[row]
        config.save_saved_voices(self._voices)
        self._refresh_list()


class SessionWorker(QObject):
    """Owns the audio devices and the asyncio loop for one translation run.

    start() runs on its own plain threading.Thread (not QThread -- see
    MainWindow._on_start_stop). state_changed/transcript_received/error_occurred/
    finished are emitted from that thread; callers must connect with
    Qt.ConnectionType.QueuedConnection to marshal them back to their own thread.
    """

    state_changed = Signal(object)
    transcript_received = Signal(object)
    error_occurred = Signal(str)
    finished = Signal()

    def __init__(
        self,
        api_key: str,
        source_lang: str,
        target_lang: str,
        mic_device: int | None,
        output_device: int | None,
        file_path: str | None,
        include_mic: bool = True,
        mic_gain: float = 1.0,
        mic_gate_threshold: float = 0.0,
        voice_id: str | None = None,
        voice_cloning: bool = False,
        subtitles_only: bool = False,
    ):
        super().__init__()
        self._api_key = api_key
        self._source_lang = source_lang
        self._target_lang = target_lang
        self._mic_device = mic_device
        self._output_device = output_device
        self._file_path = file_path
        # include_mic distinguishes "no mic at all" (file-only mode) from
        # "use the mic, and mic_device=None happens to mean the system
        # default" (mic-only or mixed mode) -- mic_device alone can't tell
        # those apart, since None is a valid device selection too.
        self._include_mic = include_mic
        self._initial_mic_gain = mic_gain
        self._initial_gate_threshold = mic_gate_threshold
        self._voice_id = voice_id
        self._voice_cloning = voice_cloning
        self._subtitles_only = subtitles_only
        self._loop: asyncio.AbstractEventLoop | None = None
        self._runner: TranslationRunner | None = None
        self._file_source: FileStream | None = None
        self._mic_source: MicStream | None = None
        self._mixed_source: MixedSource | None = None
        # Created here (not in start()) so stop() is safe to call the instant the
        # worker exists, even before the background thread has begun running —
        # otherwise a fast Start-then-Stop click could be silently lost.
        self._stop_event = threading.Event()

    @property
    def position_ms(self) -> float:
        return self._file_source.position_ms if self._file_source else 0.0

    @property
    def total_ms(self) -> float:
        return self._file_source.total_ms if self._file_source else 0.0

    def start(self) -> None:
        # WASAPI (the audio backend selected for every device on Windows --
        # see audio_io._preferred_hostapi_index) is COM-based and requires
        # COM to be initialized on whichever thread uses it. This thread
        # never did that, which previously didn't matter (MME devices don't
        # need COM), but once device listing switched to WASAPI-only, opening
        # *any* device here started failing with "Unanticipated host error
        # ... WdmSyncIoctl ... Windows WDM-KS error 0" -- confirmed by
        # reproducing it with a bare thread opening the same device, and
        # confirming CoInitializeEx() fixes it.
        com_initialized = False
        if sys.platform == "win32":
            import ctypes

            COINIT_APARTMENTTHREADED = 0x2
            hr = ctypes.windll.ole32.CoInitializeEx(None, COINIT_APARTMENTTHREADED)
            com_initialized = hr >= 0  # S_OK or S_FALSE (already initialized)

        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            # Constructing the source (opens the device) must stay inside this
            # try: MicStream(device=...) raises immediately if e.g. the mic was
            # unplugged after the device list was populated. If that exception
            # escaped uncaught, finished.emit() in the finally block below would
            # never run, leaving the GUI thinking a session is still active --
            # Start/Stop stuck forever until the app is restarted.
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
            with source_cm as source, OutputSink(device=self._output_device) as sink:
                self._runner = TranslationRunner(
                    api_key=self._api_key,
                    region=REGION,
                    source_lang=self._source_lang,
                    target_lang=self._target_lang,
                    source=source,
                    sink=sink,
                    on_state=self.state_changed.emit,
                    on_transcript=self.transcript_received.emit,
                    on_error=self.error_occurred.emit,
                    stop_event=self._stop_event,
                    voice_id=self._voice_id,
                    voice_cloning=self._voice_cloning,
                    mute_output=self._subtitles_only,
                )
                self._loop.run_until_complete(self._runner.run())
        except Exception as e:  # device open failure etc. — before/outside TranslationRunner's own handling
            self.error_occurred.emit(f"Błąd urządzenia audio: {e}")
            self.state_changed.emit(SessionState.ERROR)
        finally:
            # On Windows, asyncio's default ProactorEventLoop can crash the
            # process if it's closed immediately after closing an SSL/websocket
            # connection: pending IOCP completion callbacks for the just-closed
            # transport haven't run yet. Giving the loop one more brief idle
            # iteration first lets that cleanup finish before close().
            with contextlib.suppress(Exception):
                self._loop.run_until_complete(asyncio.sleep(0.2))
            self._loop.close()
            if com_initialized:
                ctypes.windll.ole32.CoUninitialize()
            self.finished.emit()

    def stop(self) -> None:
        # Delegate to the runner (which resumes a paused source before signaling
        # stop) whenever it exists; runner.stop() is plain thread-safe state
        # mutation, safe to call directly from any thread. Before the runner
        # exists yet, fall back to the shared event directly (see __init__).
        if self._runner is not None:
            self._runner.stop()
        else:
            self._stop_event.set()

    def _call_on_loop(self, method_name: str, *args) -> None:
        if self._loop is None or self._runner is None:
            return
        method = getattr(self._runner, method_name)
        try:
            self._loop.call_soon_threadsafe(method, *args)
        except RuntimeError:
            pass  # loop already closed -- the session ended just as we tried to act on it

    def pause(self) -> None:
        self._call_on_loop("request_pause")

    def resume(self) -> None:
        self._call_on_loop("request_resume")

    def seek(self, position_ms: float) -> None:
        self._call_on_loop("request_seek", position_ms)

    def change_voice(self, voice_id: str | None, voice_cloning: bool) -> None:
        self._call_on_loop("request_change_voice", voice_id, voice_cloning)

    def change_mic_device(self, device_index: int) -> None:
        self._call_on_loop("request_change_mic_device", device_index)

    def pause_file(self) -> None:
        # Deliberately does NOT go through request_pause() / the asyncio loop:
        # MixedSource.pause_file() is a plain thread-safe attribute write
        # (same as FileStream.pause() itself), and -- more importantly --
        # request_pause() would also pause the server-side session (stopping
        # the still-live mic from being translated at all). This only ever
        # touches the file half, directly, from whichever thread calls it.
        if self._mixed_source is not None:
            self._mixed_source.pause_file()

    def resume_file(self) -> None:
        if self._mixed_source is not None:
            self._mixed_source.resume_file()

    def set_mic_gain(self, gain: float) -> None:
        # MicStream.set_gain() is a plain thread-safe attribute write (no asyncio
        # involved), so this can be called directly -- no loop marshaling needed.
        if self._mic_source is not None:
            self._mic_source.set_gain(gain)

    def set_gate_threshold(self, threshold: float) -> None:
        if self._mic_source is not None:
            self._mic_source.set_gate_threshold(threshold)

    def set_subtitles_only(self, muted: bool) -> None:
        # Plain passthrough -- TranslationRunner.set_mute_output() is itself
        # a thread-safe direct call (see its docstring), no _call_on_loop
        # marshaling needed, same as set_mic_gain/set_gate_threshold above.
        if self._runner is not None:
            self._runner.set_mute_output(muted)


_STATE_LABELS = {
    SessionState.CONNECTING: "Łączenie...",
    SessionState.RUNNING: "Tłumaczę na żywo",
    SessionState.PAUSED: "Wstrzymano",
    SessionState.STOPPED: "Zatrzymano",
    SessionState.ERROR: "Błąd",
}

# Feedback-loop auto-pause (mic mode only -- see _on_transcript): the same
# final (source or translation) text repeating this many times in a row,
# within this many seconds of each other, is treated as a probable feedback
# loop (real speech essentially never repeats a whole sentence verbatim this
# many times back to back) rather than a coincidence.
LOOP_REPEAT_THRESHOLD = 3
LOOP_REPEAT_WINDOW_SECONDS = 15.0


def _fmt_ms(ms: float) -> str:
    total_seconds = int(ms // 1000)
    return f"{total_seconds // 60:02d}:{total_seconds % 60:02d}"


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("CRC Translator")
        self._thread: threading.Thread | None = None
        self._worker: SessionWorker | None = None
        self._selected_file: str | None = None
        self._overlay: OverlayWindow | None = None
        self._transcript_history: list[TranscriptEvent] = []
        # Feedback-loop auto-pause bookkeeping -- see _on_transcript. Keyed by
        # is_translation so a repeating source line and a repeating
        # translation line are tracked (and can each trigger) independently.
        self._loop_repeat_state: dict[bool, tuple[str | None, int, float]] = {
            True: (None, 0, 0.0),
            False: (None, 0, 0.0),
        }

        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)

        settings_row = QHBoxLayout()
        settings_row.addStretch()
        self.settings_btn = QPushButton("Ustawienia...")
        self.settings_btn.clicked.connect(self._open_settings)
        settings_row.addWidget(self.settings_btn)
        root.addLayout(settings_row)

        form = QFormLayout()

        self.mode_combo = QComboBox()
        self.mode_combo.addItems(["Mikrofon", "Plik", "Mikrofon + Plik"])
        self.mode_combo.setToolTip(
            "\"Mikrofon + Plik\" miksuje oba dźwięki w jedno wspólne tłumaczenie (np. lektor z "
            "pliku + osoba mówiąca na żywo) zamiast dwóch osobnych sesji. Pauza dotyczy wtedy "
            "tylko pliku -- mikrofon zostaje aktywny przez cały czas."
        )
        self.mode_combo.currentIndexChanged.connect(self._on_mode_changed)
        form.addRow("Źródło:", self.mode_combo)

        self.mic_combo = QComboBox()
        self._input_devices = list_input_devices()
        for d in self._input_devices:
            self.mic_combo.addItem(d.name, d.index)
        self.mic_combo.currentIndexChanged.connect(self._on_mic_selection_changed)

        self.mic_gain_row = QWidget()
        mic_gain_outer = QVBoxLayout(self.mic_gain_row)
        mic_gain_outer.setContentsMargins(0, 0, 0, 0)

        gain_row = QHBoxLayout()
        gain_row.addWidget(QLabel("Głośność mikrofonu:"))
        self.mic_gain_slider = QSlider(Qt.Orientation.Horizontal)
        self.mic_gain_slider.setRange(0, 100)
        self.mic_gain_slider.setValue(100)
        self.mic_gain_slider.valueChanged.connect(self._on_mic_gain_changed)
        self.mic_gain_label = QLabel("100%")
        gain_row.addWidget(self.mic_gain_slider, stretch=1)
        gain_row.addWidget(self.mic_gain_label)
        mic_gain_outer.addLayout(gain_row)

        gate_row = QHBoxLayout()
        gate_label_text = QLabel("Ignoruj ciszej niż:")
        gate_tooltip = (
            "Dźwięk cichszy niż ten poziom jest całkowicie pomijany (zamieniany na ciszę) "
            "zanim trafi do tłumaczenia — Twoja mowa musi być głośniejsza niż ustawiony próg, "
            "żeby się liczyła.\n\n"
            "0% (Wyłączony) = nic nie jest pomijane, wszystko przechodzi normalnie.\n"
            "Im wyżej, tym WIĘCEJ dźwięku jest odcinane (nie odwrotnie) — przy wysokiej "
            "wartości nawet Twoja własna, cichsza mowa może zostać ucięta.\n\n"
            "Przydatne głównie przeciw pętli sprzężenia zwrotnego (mikrofon łapiący własne "
            "tłumaczenie z głośnika) — zacznij od niskiej wartości (15-20%) i zwiększaj tylko "
            "jeśli to konieczne."
        )
        gate_label_text.setToolTip(gate_tooltip)
        gate_row.addWidget(gate_label_text)
        self.mic_gate_slider = QSlider(Qt.Orientation.Horizontal)
        self.mic_gate_slider.setRange(0, 100)
        self.mic_gate_slider.setValue(0)
        self.mic_gate_slider.setToolTip(gate_tooltip)
        self.mic_gate_slider.valueChanged.connect(self._on_mic_gate_changed)
        self.mic_gate_label = QLabel("Wyłączony")
        gate_row.addWidget(self.mic_gate_slider, stretch=1)
        gate_row.addWidget(self.mic_gate_label)
        mic_gain_outer.addLayout(gate_row)

        self.file_row = QWidget()
        file_layout = QHBoxLayout(self.file_row)
        file_layout.setContentsMargins(0, 0, 0, 0)
        self.file_label = QLabel("(nie wybrano pliku)")
        self.file_btn = QPushButton("Wybierz plik...")
        self.file_btn.clicked.connect(self._choose_file)
        file_layout.addWidget(self.file_label, stretch=1)
        file_layout.addWidget(self.file_btn)

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

        self.output_combo = QComboBox()
        self._output_devices = list_output_devices()
        cable = find_virtual_cable(self._output_devices)
        for d in self._output_devices:
            self.output_combo.addItem(d.name, d.index)
        self.output_hint = QLabel(
            "Nie wykryto wirtualnego kabla audio (VB-Cable / BlackHole)."
            " Zainstaluj go, aby OBS mógł odebrać tłumaczenie — patrz README."
        )
        self.output_hint.setStyleSheet("color: #b06a00;")
        if cable is not None:
            self.output_combo.setCurrentIndex(self._output_devices.index(cable))
            self.output_hint.setVisible(False)
        form.addRow("Wyjście (do OBS):", self.output_combo)

        self.subtitles_only_check = QCheckBox("Tylko napisy (bez dźwięku)")
        self.subtitles_only_check.setToolTip(
            "Odebrane przetłumaczone audio nie jest odtwarzane na wybrane wyjście -- zostaje "
            "tylko tekst (log/overlay). Palabra API nie oferuje trybu bez syntezy mowy, więc "
            "koszt sesji się nie zmienia -- to tylko wycisza odtwarzanie po stronie aplikacji."
        )
        form.addRow("", self.subtitles_only_check)

        self.source_lang_combo = QComboBox()
        for code, name in SOURCE_LANGUAGES:
            self.source_lang_combo.addItem(f"{name} ({code})", code)
        self.source_lang_combo.setCurrentIndex([c for c, _ in SOURCE_LANGUAGES].index(DEFAULT_SOURCE))
        form.addRow("Język źródłowy:", self.source_lang_combo)

        self.target_lang_combo = QComboBox()
        for code, name in TARGET_LANGUAGES:
            self.target_lang_combo.addItem(f"{name} ({code})", code)
        self.target_lang_combo.setCurrentIndex([c for c, _ in TARGET_LANGUAGES].index(DEFAULT_TARGET))
        form.addRow("Język docelowy:", self.target_lang_combo)

        self.voice_combo = QComboBox()
        self.voice_combo.currentIndexChanged.connect(self._on_voice_mode_changed)
        self.voice_combo.currentIndexChanged.connect(self._on_voice_selection_changed)
        self.voice_custom_edit = QLineEdit()
        self.voice_custom_edit.setPlaceholderText("ID głosu z app.palabra.ai/voices")
        self.voice_custom_edit.setVisible(False)
        self.voice_custom_edit.editingFinished.connect(self._on_voice_custom_edit_finished)
        self.manage_voices_btn = QPushButton("Zapisane głosy...")
        self.manage_voices_btn.clicked.connect(self._on_manage_voices)
        self._rebuild_voice_combo()
        voice_row = QHBoxLayout()
        voice_row.addWidget(self.voice_combo, stretch=1)
        voice_row.addWidget(self.voice_custom_edit, stretch=1)
        voice_row.addWidget(self.manage_voices_btn)
        form.addRow("Głos:", voice_row)

        root.addLayout(form)
        root.addWidget(self.output_hint)

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
        root.addWidget(self.playback_row)
        # playback_row itself (holding pause_btn) stays visible in all modes --
        # only the position slider/label (file-only, no meaning for a live mic)
        # are toggled in _on_mode_changed. mode_combo's currentIndexChanged
        # already fired once during addItems() above, before this method was
        # connected, so the initial mic_combo/file_row/mic_gain_row/position_*
        # visibility needs this explicit call instead of relying on the signal.
        self._on_mode_changed(self.mode_combo.currentIndex())

        self._position_timer = QTimer(self)
        self._position_timer.setInterval(250)
        self._position_timer.timeout.connect(self._update_position)
        self._is_paused = False
        self._pause_request_pending = False
        self._partial_line_active = False  # last log line is a growing, not-yet-final transcript
        self._show_lang_tags = True

        control_row = QHBoxLayout()
        self.status_label = QLabel("Gotowy")
        self.start_stop_btn = QPushButton("Start")
        self.start_stop_btn.clicked.connect(self._on_start_stop)
        control_row.addWidget(self.status_label, stretch=1)
        control_row.addWidget(self.start_stop_btn)
        root.addLayout(control_row)

        log_filter_row = QHBoxLayout()
        log_filter_row.addWidget(QLabel("Pokaż w logu:"))
        self.log_filter_combo = QComboBox()
        self.log_filter_combo.addItem("Źródłowy i tłumaczenie", "both")
        self.log_filter_combo.addItem("Tylko źródłowy", "source")
        self.log_filter_combo.addItem("Tylko tłumaczenie", "translation")
        self.log_filter_combo.currentIndexChanged.connect(self._rebuild_log)
        log_filter_row.addWidget(self.log_filter_combo)
        self.show_tags_check = QCheckBox("Pokaż tagi języka ([pl]/[en])")
        self.show_tags_check.setChecked(True)
        self.show_tags_check.toggled.connect(self._on_show_tags_toggled)
        log_filter_row.addWidget(self.show_tags_check)
        log_filter_row.addStretch()
        root.addLayout(log_filter_row)

        overlay_row = QHBoxLayout()
        overlay_row.addStretch()
        self.overlay_btn = QPushButton("Odczep okienko z tłumaczeniem")
        self.overlay_btn.clicked.connect(self._on_toggle_overlay)
        overlay_row.addWidget(self.overlay_btn)
        self.overlay_settings_btn = QPushButton("Ustawienia wyglądu overlay...")
        self.overlay_settings_btn.clicked.connect(self._on_open_overlay_settings)
        overlay_row.addWidget(self.overlay_settings_btn)
        self.save_transcript_btn = QPushButton("Zapisz transkrypcję...")
        self.save_transcript_btn.clicked.connect(self._on_save_transcript)
        overlay_row.addWidget(self.save_transcript_btn)
        self.clear_transcript_btn = QPushButton("Wyczyść transkrypcję")
        self.clear_transcript_btn.clicked.connect(self._on_clear_transcript)
        overlay_row.addWidget(self.clear_transcript_btn)
        root.addLayout(overlay_row)

        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        root.addWidget(self.log, stretch=1)

        self.resize(600, 500)

        # Voice pickers are locked during a session again: live-switching (via
        # SessionWorker.change_voice -> set_task()) is implemented and tested
        # correctly in isolation, but changing voice mid-stream (file mode)
        # kept triggering the server's "arriving faster than real-time"
        # warning despite several fix attempts, and the exact server-side
        # mechanism couldn't be confirmed. Disabled here rather than ripped
        # out -- remove voice_combo/voice_custom_edit from this list again to
        # re-enable live switching if that gets root-caused later.
        # mic_combo is deliberately NOT in this list -- unlike voice, switching
        # the input device mid-session never touches the Palabra session at
        # all (it's purely local device I/O, see MicStream.switch_device), so
        # it stays enabled and live-switchable during a running Mikrofon
        # session; see _on_mic_selection_changed.
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

    def _set_config_enabled(self, enabled: bool) -> None:
        for w in self._config_widgets:
            w.setEnabled(enabled)

    def _on_voice_mode_changed(self, _index: int) -> None:
        data = self.voice_combo.currentData()
        if data is None:
            return  # combo temporarily empty mid-rebuild
        kind, _ = data
        self.voice_custom_edit.setVisible(kind == "custom")

    def _resolve_selected_voice(self) -> tuple[str | None, bool] | None:
        """(voice_id, voice_cloning) for the current picker state, or None if
        "custom" is selected but the ID field is empty."""
        voice_kind, voice_preset_id = self.voice_combo.currentData()
        if voice_kind == "id":
            return voice_preset_id, False
        if voice_kind == "clone":
            return None, True
        if voice_kind == "custom":
            voice_id = self.voice_custom_edit.text().strip()
            return (voice_id, False) if voice_id else None
        return None, False  # "auto"

    def _on_voice_selection_changed(self, _index: int) -> None:
        # Live voice switching mid-session (see SessionWorker.change_voice) --
        # "custom" is handled by _on_voice_custom_edit_finished instead, since
        # there's no single ID to apply until the user finishes typing it.
        if self._worker is None:
            return
        kind, _ = self.voice_combo.currentData()
        if kind == "custom":
            return
        resolved = self._resolve_selected_voice()
        if resolved is not None:
            self._worker.change_voice(*resolved)

    def _on_voice_custom_edit_finished(self) -> None:
        if self._worker is None:
            return
        kind, _ = self.voice_combo.currentData()
        if kind != "custom":
            return
        resolved = self._resolve_selected_voice()
        if resolved is not None:
            self._worker.change_voice(*resolved)

    def _rebuild_voice_combo(self) -> None:
        current = self.voice_combo.currentData() if self.voice_combo.count() else None
        self.voice_combo.blockSignals(True)
        self.voice_combo.clear()
        self.voice_combo.addItem("Domyślny (auto)", ("auto", None))
        self.voice_combo.addItem("default_low", ("id", "default_low"))
        self.voice_combo.addItem("default_high", ("id", "default_high"))
        self.voice_combo.addItem("Klonowanie głosu mówcy (eksperymentalne)", ("clone", None))
        for v in config.load_saved_voices():
            self.voice_combo.addItem(v["name"], ("id", v["voice_id"]))
        self.voice_combo.addItem("Inny (ID z portalu Palabra)...", ("custom", None))
        if current is not None:
            idx = self.voice_combo.findData(current)
            self.voice_combo.setCurrentIndex(idx if idx >= 0 else 0)
        self.voice_combo.blockSignals(False)
        self._on_voice_mode_changed(self.voice_combo.currentIndex())

    def _on_manage_voices(self) -> None:
        SavedVoicesDialog(self).exec()
        self._rebuild_voice_combo()

    def _on_mode_changed(self, index: int) -> None:
        mic_active = index in (0, 2)
        file_active = index in (1, 2)
        self.mic_combo.setVisible(mic_active)
        self.file_row.setVisible(file_active)
        self.mic_gain_row.setVisible(mic_active)
        self.position_slider.setVisible(file_active)
        self.position_label.setVisible(file_active)

    def _on_mic_selection_changed(self, _index: int) -> None:
        # Live device switching mid-session (see SessionWorker.change_mic_device)
        # -- a no-op before Start (no worker yet) or in file-only mode (mic_combo
        # exists but isn't the active source, and is hidden behind file_row
        # anyway; mode_combo itself stays locked during a session so this
        # can't actually flip mode mid-stream).
        if self._worker is None or self.mode_combo.currentIndex() not in (0, 2):
            return
        device = self.mic_combo.currentData()
        if device is not None:
            self._worker.change_mic_device(device)

    def _choose_file(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Wybierz plik audio/wideo",
            "",
            "Audio/Video (*.wav *.mp3 *.mp4 *.mov *.m4a *.flac *.ogg *.mkv *.avi *.webm *.wmv *.flv *.aac *.ts);;Wszystkie pliki (*)",
        )
        if path:
            self._selected_file = path
            self.file_label.setText(path)

    def _open_settings(self) -> None:
        SettingsDialog(self).exec()

    def _on_refresh_devices(self) -> None:
        current_mic = self.mic_combo.currentData()
        current_output = self.output_combo.currentData()

        rescan_devices()

        self.mic_combo.clear()
        self._input_devices = list_input_devices()
        for d in self._input_devices:
            self.mic_combo.addItem(d.name, d.index)
        idx = self.mic_combo.findData(current_mic)
        self.mic_combo.setCurrentIndex(idx if idx >= 0 else 0)

        self.output_combo.clear()
        self._output_devices = list_output_devices()
        for d in self._output_devices:
            self.output_combo.addItem(d.name, d.index)
        idx = self.output_combo.findData(current_output)
        if idx >= 0:
            self.output_combo.setCurrentIndex(idx)
        else:
            cable = find_virtual_cable(self._output_devices)
            if cable is not None:
                self.output_combo.setCurrentIndex(self._output_devices.index(cable))
        self.output_hint.setVisible(find_virtual_cable(self._output_devices) is None)

    def _on_mic_gain_changed(self, value: int) -> None:
        self.mic_gain_label.setText(f"{value}%")
        if self._worker is not None:
            self._worker.set_mic_gain(value / 100)

    def _on_mic_gate_changed(self, value: int) -> None:
        self.mic_gate_label.setText("Wyłączony" if value == 0 else f"{value}%")
        if self._worker is not None:
            self._worker.set_gate_threshold(value / 100)

    def _on_start_stop(self) -> None:
        if self._worker is not None:
            self.start_stop_btn.setEnabled(False)
            self.status_label.setText("Zatrzymywanie...")
            self._worker.stop()
            return

        creds = config.load_credentials()
        if not creds.api_key:
            QMessageBox.warning(self, "Brak klucza API", "Ustaw klucz API w Ustawieniach przed rozpoczęciem.")
            self._open_settings()
            return

        mode_idx = self.mode_combo.currentIndex()
        mic_active = mode_idx in (0, 2)
        needs_file = mode_idx in (1, 2)
        file_path = self._selected_file if needs_file else None
        if needs_file and not file_path:
            QMessageBox.warning(self, "Brak pliku", "Wybierz plik audio/wideo do przetłumaczenia.")
            return
        if self.output_combo.count() == 0:
            QMessageBox.critical(self, "Brak urządzenia wyjściowego", "System nie zgłasza żadnego urządzenia audio wyjściowego.")
            return

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

        mic_device = self.mic_combo.currentData() if mic_active else None
        output_device = self.output_combo.currentData()
        source_lang = self.source_lang_combo.currentData()
        target_lang = self.target_lang_combo.currentData()

        resolved_voice = self._resolve_selected_voice()
        if resolved_voice is None:
            QMessageBox.warning(self, "Brak ID głosu", "Podaj ID głosu (z app.palabra.ai/voices) albo wybierz inną opcję.")
            return
        voice_id, voice_cloning = resolved_voice

        # Log/history/overlay deliberately survive across Start/Stop -- they
        # only reset via the explicit "Wyczyść transkrypcję" button now, so a
        # sequence of short takes doesn't wipe out everything said so far.
        # _partial_line_active is still reset: it's just bookkeeping for
        # whether the very next event should replace the log's last line or
        # start a new one, and a fresh session's first event should always
        # start a new line even if the previous one ended on a partial.
        self._partial_line_active = False
        self._loop_repeat_state = {True: (None, 0, 0.0), False: (None, 0, 0.0)}
        worker = SessionWorker(
            api_key=creds.api_key,
            source_lang=source_lang,
            target_lang=target_lang,
            mic_device=mic_device,
            output_device=output_device,
            file_path=file_path,
            include_mic=mic_active,
            mic_gain=self.mic_gain_slider.value() / 100,
            mic_gate_threshold=self.mic_gate_slider.value() / 100,
            voice_id=voice_id,
            voice_cloning=voice_cloning,
            subtitles_only=self.subtitles_only_check.isChecked(),
        )
        # A plain threading.Thread, not QThread: on Windows, running PortAudio
        # (WASAPI) device I/O on a QThread intermittently crashed the whole
        # process on session teardown (native crash, no Python traceback --
        # confirmed by bisecting against plain-thread and QThread-without-audio
        # variants, which never crashed). QObject signals are still safe to emit
        # from a non-Qt thread; QueuedConnection below forces proper marshaling
        # to the GUI thread regardless of this worker's own thread affinity.
        qc = Qt.ConnectionType.QueuedConnection
        worker.state_changed.connect(self._on_state, qc)
        worker.transcript_received.connect(self._on_transcript, qc)
        worker.error_occurred.connect(self._on_error, qc)
        worker.finished.connect(self._on_worker_finished, qc)

        thread = threading.Thread(target=worker.start, daemon=True)
        self._worker = worker
        self._thread = thread
        thread.start()
        self.start_stop_btn.setText("Stop")
        self._set_config_enabled(False)
        if needs_file:
            self._position_timer.start()

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
        # Re-enabling controls happens in _on_worker_finished(), NOT here:
        # state_changed(STOPPED/ERROR) is emitted from inside TranslationRunner.run(),
        # while the mic/output device is still being released by the `with` block
        # in SessionWorker.start(). Re-enabling Start on this signal would let the
        # user launch a new session before the old device handle is actually freed.

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

    def _on_pause_resume(self) -> None:
        if self._worker is None:
            return
        if self.mode_combo.currentIndex() == 2:
            # Mixed mode: Pauza/Wznów only pause the FILE locally -- the mic
            # and the underlying Palabra session keep running (still billed,
            # still translating whatever the mic picks up), so this is a
            # plain synchronous call on SessionWorker, not the server-side
            # pause/resume path below (which would stop the whole session).
            self._is_paused = not self._is_paused
            if self._is_paused:
                self._worker.pause_file()
                self.pause_btn.setText("Wznów")
            else:
                self._worker.resume_file()
                self.pause_btn.setText("Pauza")
            return
        if self._pause_request_pending:
            return
        self._pause_request_pending = True
        if self._is_paused:
            self._worker.resume()
        else:
            self._worker.pause()

    def _on_seek(self) -> None:
        if self._worker is not None:
            self._worker.seek(float(self.position_slider.value()))

    def _update_position(self) -> None:
        if self._worker is None:
            self._position_timer.stop()
            return
        total = self._worker.total_ms
        if total <= 0:
            return  # file not decoded yet
        if not self.position_slider.isEnabled():
            self.position_slider.setEnabled(True)
            self.position_slider.setRange(0, int(total))
        if not self.position_slider.isSliderDown():
            self.position_slider.setValue(int(self._worker.position_ms))
        self.position_label.setText(f"{_fmt_ms(self._worker.position_ms)} / {_fmt_ms(total)}")

    def _on_transcript(self, event: TranscriptEvent) -> None:
        # Kept so a newly-opened overlay can be backfilled (see _open_overlay)
        # instead of starting empty if it's opened mid-session, and so the log
        # filter/tag toggles can retroactively re-render already-shown lines
        # (see _rebuild_log). Capped since a long session could otherwise
        # accumulate an unbounded list.
        self._transcript_history.append(event)
        del self._transcript_history[:-300]
        if event.is_final and self.mode_combo.currentIndex() == 0:  # mic mode only
            self._check_loop_repeat(event)
        if self._overlay is not None:
            self._overlay.on_transcript(event)  # overlay applies its own, independent filter
        if not self._event_passes_log_filter(event):
            return  # filtered out entirely -- doesn't touch the log or the partial-line bookkeeping
        text = self._format_log_line(event)
        if self._partial_line_active:
            self._replace_last_log_line(text)
        else:
            self.log.appendPlainText(text)
        # A growing (non-final) line keeps getting replaced in place; once final,
        # the NEXT event (e.g. the translation) must start its own new line.
        self._partial_line_active = not event.is_final

    def _check_loop_repeat(self, event: TranscriptEvent) -> None:
        """Feedback-loop detection: mic mode only (file mode can't feed back --
        see SessionWorker.start(), which never opens a mic for it), confirmed
        via a real incident where a live mic picked up this app's own
        translated output through speakers and kept re-translating it,
        producing the exact same final text over and over. Real speech
        essentially never repeats a whole sentence verbatim LOOP_REPEAT_THRESHOLD
        times in a row within LOOP_REPEAT_WINDOW_SECONDS of each other, so
        that pattern is treated as a probable loop and auto-pauses the
        session -- see _trigger_loop_auto_pause for why NOT auto-resuming
        afterward is deliberate.
        """
        normalized = event.text.strip()
        if not normalized:
            return
        prev_text, prev_count, prev_time = self._loop_repeat_state[event.is_translation]
        if normalized == prev_text and (event.timestamp - prev_time) < LOOP_REPEAT_WINDOW_SECONDS:
            count = prev_count + 1
        else:
            count = 1
        self._loop_repeat_state[event.is_translation] = (normalized, count, event.timestamp)
        if count >= LOOP_REPEAT_THRESHOLD:
            # Reset immediately so this doesn't refire on every further repeat
            # while the pause request is still in flight.
            self._loop_repeat_state[event.is_translation] = (None, 0, event.timestamp)
            self._trigger_loop_auto_pause(normalized)

    def _trigger_loop_auto_pause(self, repeated_text: str) -> None:
        self._partial_line_active = False
        self.log.appendPlainText(
            f"⚠ Wykryto prawdopodobną pętlę sprzężenia zwrotnego (to samo tłumaczenie "
            f"powtórzone {LOOP_REPEAT_THRESHOLD}x z rzędu) — sesja wstrzymana automatycznie."
        )
        if self._worker is not None and not self._is_paused and not self._pause_request_pending:
            self._pause_request_pending = True
            self._worker.pause()
        QMessageBox.warning(
            self,
            "Wykryto pętlę sprzężenia zwrotnego",
            f'To samo tłumaczenie ("{repeated_text}") powtórzyło się {LOOP_REPEAT_THRESHOLD}x z rzędu — '
            "sesja została automatycznie wstrzymana (Pauza), żeby nie generować dalszych kosztów.\n\n"
            "Prawdopodobna przyczyna: mikrofon słyszy własne tłumaczenie z głośnika. Użyj słuchawek, "
            "zmień urządzenie wyjściowe, albo ustaw Próg czułości mikrofonu, a potem kliknij Wznów.",
        )

    def _event_passes_log_filter(self, event: TranscriptEvent) -> bool:
        filter_mode = self.log_filter_combo.currentData()
        if filter_mode == "source" and event.is_translation:
            return False
        if filter_mode == "translation" and not event.is_translation:
            return False
        return True

    def _format_log_line(self, event: TranscriptEvent) -> str:
        kind = "→" if event.is_translation else "•"
        suffix = "" if event.is_final else " …"
        if self._show_lang_tags:
            return f"{kind} [{event.language}] {event.text}{suffix}"
        return f"{kind} {event.text}{suffix}"

    def _rebuild_log(self) -> None:
        # Re-renders the whole log from history under the current filter/tag
        # settings, so toggling them re-filters what's already on screen
        # instead of only affecting transcripts that arrive afterward.
        self.log.clear()
        self._partial_line_active = False
        for event in self._transcript_history:
            if not self._event_passes_log_filter(event):
                continue
            text = self._format_log_line(event)
            if self._partial_line_active:
                self._replace_last_log_line(text)
            else:
                self.log.appendPlainText(text)
            self._partial_line_active = not event.is_final

    def _on_show_tags_toggled(self, checked: bool) -> None:
        self._show_lang_tags = checked
        self._rebuild_log()

    def _on_save_transcript(self) -> None:
        content = self.log.toPlainText()
        if not content.strip():
            QMessageBox.information(self, "Brak transkrypcji", "Nie ma jeszcze żadnej transkrypcji do zapisania.")
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "Zapisz transkrypcję", "transkrypcja.txt", "Pliki tekstowe (*.txt);;Wszystkie pliki (*)"
        )
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(content)
        except OSError as e:
            QMessageBox.critical(self, "Błąd zapisu", f"Nie udało się zapisać pliku: {e}")

    def _on_clear_transcript(self) -> None:
        self.log.clear()
        self._transcript_history = []
        self._partial_line_active = False
        if self._overlay is not None:
            self._overlay.clear()

    def _replace_last_log_line(self, text: str) -> None:
        cursor = self.log.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        cursor.select(QTextCursor.SelectionType.LineUnderCursor)
        cursor.insertText(text)

    def _on_error(self, message: str) -> None:
        self._pause_request_pending = False  # a failed pause/resume/seek won't produce a state change
        self._partial_line_active = False  # don't let a later partial transcript overwrite this line
        self.log.appendPlainText(message)

    def _on_toggle_overlay(self) -> None:
        if self._overlay is None:
            self._open_overlay()
        else:
            self._overlay.close()  # triggers closed -> _on_overlay_closed

    def _open_overlay(self) -> None:
        self._overlay = OverlayWindow()
        self._overlay.closed.connect(self._on_overlay_closed)
        # Backfill: replay whatever transcripts already happened this session so
        # opening the overlay late doesn't leave it empty. on_transcript() already
        # applies the overlay's own filter and trims to its line limit, so this
        # naturally ends up showing just the most recent relevant line(s) --
        # exactly as if the overlay had been open from the start.
        for event in self._transcript_history:
            self._overlay.on_transcript(event)
        self._overlay.show()
        self.overlay_btn.setText("Zamknij okienko z tłumaczeniem")

    def _on_overlay_closed(self) -> None:
        self._overlay = None
        self.overlay_btn.setText("Odczep okienko z tłumaczeniem")

    def _on_open_overlay_settings(self) -> None:
        # Opens the overlay on demand: settings need a live window for preview,
        # and this also means appearance can be adjusted (and is saved) even if
        # you haven't explicitly "detached" it yet.
        if self._overlay is None:
            self._open_overlay()
        dialog = OverlaySettingsDialog(self._overlay, self)
        dialog.show()

    def closeEvent(self, event) -> None:
        if self._worker is not None and self._thread is not None:
            thread = self._thread
            worker = self._worker
            self.status_label.setText("Zamykanie — kończę sesję...")
            QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
            worker.stop()
            # thread is a plain threading.Thread (see _on_start_stop): it finishes
            # on its own once worker.start() returns, with no dependency on this
            # (GUI) thread's event queue -- unlike the old QThread.wait() pattern,
            # so polling or join() are both safe here, not a permanent timeout burn.
            # A nested event loop is used only to keep the UI responsive while
            # waiting, not because completion detection needs it.
            wait_loop = QEventLoop()
            poll_timer = QTimer()
            poll_timer.timeout.connect(lambda: None if thread.is_alive() else wait_loop.quit())
            poll_timer.start(50)
            safety_timer = QTimer()
            safety_timer.setSingleShot(True)
            safety_timer.timeout.connect(wait_loop.quit)
            safety_timer.start(10000)
            wait_loop.exec()
            finished_in_time = not thread.is_alive()
            QApplication.restoreOverrideCursor()
            if not finished_in_time:
                answer = QMessageBox.warning(
                    self,
                    "Zamykanie trwa dłużej niż zwykle",
                    "Kończenie sesji (np. wolne połączenie) nie zdążyło się zakończyć.\n\n"
                    "Zamknięcie teraz może zostawić mikrofon/słuchawki zajęte, dopóki "
                    "aplikacja nie dokończy zwalniania urządzenia w tle — sprawdź Menedżera "
                    "zadań (python.exe), jeśli dźwięk przestanie działać.\n\n"
                    "Zamknąć mimo to?",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    QMessageBox.StandardButton.No,
                )
                if answer == QMessageBox.StandardButton.No:
                    self.status_label.setText("Zatrzymywanie...")
                    event.ignore()
                    return
        if self._overlay is not None:
            self._overlay.close()
        super().closeEvent(event)
