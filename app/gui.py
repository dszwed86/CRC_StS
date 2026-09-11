"""Desktop GUI: pick a source (mic/file), languages and an output device, then
Start/Stop a live Palabra S2S translation session that plays its output onto the
chosen device (typically a virtual audio cable OBS picks up as a source).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import sys
import threading
import time
import urllib.request
from datetime import datetime
from dataclasses import dataclass

from PySide6.QtCore import QEventLoop, QObject, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QColor, QDesktopServices, QKeySequence, QShortcut, QTextBlock, QTextCharFormat, QTextCursor
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSlider,
    QVBoxLayout,
    QWidget,
)
from palabra_ai import Palabra
from palabra_ai.exc import AuthError, PalabraError

from . import __version__, config, glossary, i18n, theme
from .i18n import tr
from .audio_io import (
    FileStream,
    MicStream,
    MixedSource,
    OutputSink,
    find_virtual_cable,
    list_input_devices,
    list_output_devices,
    play_test_tone,
    probe_audio_file,
    rescan_devices,
)
from .languages import DEFAULT_SOURCE, DEFAULT_TARGET, SOURCE_LANGUAGES, TARGET_LANGUAGES
from .overlay import OverlayWindow
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
            self.finished.emit(False, tr("Przekroczono czas oczekiwania na odpowiedź serwera."))
            return
        except AuthError as e:
            self.finished.emit(False, f"{tr('Nieprawidłowy klucz API')}: {e}")
            return
        except PalabraError as e:
            self.finished.emit(False, f"{tr('Błąd połączenia')}: {e}")
            return
        except Exception as e:
            self.finished.emit(False, f"{tr('Nieoczekiwany błąd')}: {e}")
            return
        self.finished.emit(True, tr("Klucz API działa poprawnie."))

    async def _check(self) -> None:
        palabra = Palabra(api_key=self._api_key, region=self._region)
        async with palabra.stt():
            pass


class GlossarySaver(QObject):
    """Pushes a local word-pair list to Palabra's glossary REST API (see
    app/glossary.py) on its own plain thread, same reasoning as
    ApiKeyTester -- this is plain blocking HTTP (urllib), not the
    translation session's own asyncio loop, so it must not run on the GUI
    thread either.
    """

    finished = Signal(bool, str, object)  # ok, message, new_glossary_id (or None)

    def __init__(self, api_key: str, name: str, source_lang: str, target_lang: str,
                 pairs: list[tuple[str, str]], old_glossary_id: str | None):
        super().__init__()
        self._api_key = api_key
        self._name = name
        self._source_lang = source_lang
        self._target_lang = target_lang
        self._pairs = pairs
        self._old_glossary_id = old_glossary_id

    def run(self) -> None:
        try:
            new_id = glossary.sync_glossary(
                self._api_key, self._name, self._source_lang, self._target_lang,
                self._pairs, self._old_glossary_id,
            )
        except glossary.GlossaryError as e:
            self.finished.emit(False, f"{tr('Błąd')}: {e}", None)
            return
        except Exception as e:
            self.finished.emit(False, f"{tr('Nieoczekiwany błąd')}: {e}", None)
            return
        if new_id is None:
            self.finished.emit(True, tr("Lista jest pusta -- glosariusz wyłączony dla tej pary językowej."), None)
        else:
            self.finished.emit(True, tr("Zapisano w Palabra."), new_id)


class GlossaryLister(QObject):
    """Fetches every glossary on the account (see app/glossary.py's
    list_glossaries) on its own plain thread -- same reasoning as
    ApiKeyTester/GlossarySaver above.
    """

    finished = Signal(bool, str, object)  # ok, error_message, glossaries (or None)

    def __init__(self, api_key: str):
        super().__init__()
        self._api_key = api_key

    def run(self) -> None:
        try:
            items = glossary.list_glossaries(self._api_key)
        except glossary.GlossaryError as e:
            self.finished.emit(False, f"{tr('Błąd')}: {e}", None)
            return
        except Exception as e:
            self.finished.emit(False, f"{tr('Nieoczekiwany błąd')}: {e}", None)
            return
        self.finished.emit(True, "", items)


class GlossaryDeleter(QObject):
    """Deletes one glossary by id on its own plain thread (see GlossaryLister)."""

    finished = Signal(bool, str, str)  # ok, error_message, glossary_id

    def __init__(self, api_key: str, glossary_id: str):
        super().__init__()
        self._api_key = api_key
        self._glossary_id = glossary_id

    def run(self) -> None:
        try:
            glossary.delete_glossary(self._api_key, self._glossary_id)
        except glossary.GlossaryError as e:
            self.finished.emit(False, f"{tr('Błąd')}: {e}", self._glossary_id)
            return
        except Exception as e:
            self.finished.emit(False, f"{tr('Nieoczekiwany błąd')}: {e}", self._glossary_id)
            return
        self.finished.emit(True, "", self._glossary_id)


class SettingsDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle(tr("Ustawienia"))
        creds = config.load_credentials()
        self._test_thread: threading.Thread | None = None
        self._test_worker: ApiKeyTester | None = None
        self._initial_language = i18n.get_language()

        self.language_combo = QComboBox()
        self.language_combo.addItem("Polski", i18n.LANG_PL)
        self.language_combo.addItem("English", i18n.LANG_EN)
        idx = self.language_combo.findData(self._initial_language)
        if idx >= 0:
            self.language_combo.setCurrentIndex(idx)

        self.api_key_edit = QLineEdit(creds.api_key or "")
        self.api_key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.api_key_edit.setPlaceholderText(tr("Klucz API z platform.palabra.ai/api-keys"))

        saved_balance = config.load_balance()
        self.balance_edit = QLineEdit("" if saved_balance is None else f"{saved_balance:.2f}")
        self.balance_edit.setPlaceholderText(tr("np. 45.00 -- sprawdź w panelu Palabra"))
        self.balance_edit.setToolTip(
            tr(
                "Szacunkowe saldo w USD. Palabra nie udostępnia prawdziwego salda przez API, więc "
                "to tylko przybliżenie liczone przez aplikację (odejmuje szacowany koszt każdej "
                "sesji) -- może się rozjechać z rzeczywistością. Wpisz tu aktualną wartość z panelu "
                "Palabra, żeby zsynchronizować."
            )
        )

        form = QFormLayout()
        form.addRow(tr("Język aplikacji:"), self.language_combo)
        form.addRow(tr("Klucz API Palabra:"), self.api_key_edit)
        form.addRow(tr("Saldo Palabra (USD, orientacyjne):"), self.balance_edit)

        test_row = QHBoxLayout()
        self.test_btn = QPushButton(tr("Testuj klucz"))
        self.test_btn.clicked.connect(self._on_test_key)
        self.dashboard_btn = QPushButton(tr("Otwórz panel Palabra (saldo, użycie)"))
        self.dashboard_btn.clicked.connect(self._on_open_dashboard)
        test_row.addWidget(self.test_btn)
        test_row.addWidget(self.dashboard_btn)

        self.history_btn = QPushButton(tr("Historia sesji..."))
        self.history_btn.clicked.connect(self._on_open_history)

        self.test_result_label = QLabel("")
        self.test_result_label.setWordWrap(True)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self._on_save)
        buttons.rejected.connect(self.reject)

        version_label = QLabel(f"{tr('wersja')} {__version__} — CRC Poland")
        version_label.setStyleSheet("color: gray; font-size: 9pt;")
        version_label.setAlignment(Qt.AlignmentFlag.AlignRight)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addLayout(test_row)
        layout.addWidget(self.history_btn)
        layout.addWidget(self.test_result_label)
        layout.addWidget(buttons)
        layout.addWidget(version_label)

    def _on_save(self) -> None:
        api_key = self.api_key_edit.text().strip()
        if not api_key:
            QMessageBox.warning(self, tr("Brak klucza"), tr("Podaj klucz API przed zapisaniem."))
            return
        config.save_credentials(api_key, REGION)
        balance_text = self.balance_edit.text().strip()
        if balance_text:  # empty means "leave the stored balance untouched"
            try:
                config.save_balance(float(balance_text.replace(",", ".")))
            except ValueError:
                QMessageBox.warning(self, tr("Nieprawidłowe saldo"), tr("Saldo musi być liczbą, np. 45.00."))
                return
        new_language = self.language_combo.currentData()
        if new_language != self._initial_language:
            settings = config.load_app_settings()
            settings["language"] = new_language
            config.save_app_settings(settings)
            i18n.set_language(new_language)
            QMessageBox.information(
                self,
                tr("Zmieniono język"),
                tr("Zmiana języka aplikacji będzie widoczna po ponownym uruchomieniu."),
            )
        self.accept()

    def _on_open_history(self) -> None:
        SessionHistoryDialog(self).exec()

    def _on_open_dashboard(self) -> None:
        QDesktopServices.openUrl(QUrl(DASHBOARD_URL))

    def _on_test_key(self) -> None:
        api_key = self.api_key_edit.text().strip()
        if not api_key:
            QMessageBox.warning(self, tr("Brak klucza"), tr("Wpisz klucz API przed testem."))
            return

        self.test_btn.setEnabled(False)
        self.test_result_label.setStyleSheet("")
        self.test_result_label.setText(tr("Testowanie..."))

        worker = ApiKeyTester(api_key, REGION)
        worker.finished.connect(self._on_test_finished, Qt.ConnectionType.QueuedConnection)

        thread = threading.Thread(target=worker.run, daemon=True)
        self._test_worker = worker
        self._test_thread = thread
        thread.start()

    def _on_test_finished(self, ok: bool, message: str) -> None:
        self.test_result_label.setStyleSheet(f"color: {theme.SUCCESS};" if ok else f"color: {theme.DANGER};")
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


class SessionHistoryDialog(QDialog):
    """Read-only view of past completed sessions (date, duration, estimated
    cost) -- the only record of past spend available from within the app,
    since Palabra doesn't expose per-session usage history via API."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle(tr("Historia sesji"))
        self.resize(450, 350)

        self.list_widget = QListWidget()
        self.total_label = QLabel("")

        self.clear_btn = QPushButton(tr("Wyczyść historię"))
        self.clear_btn.clicked.connect(self._on_clear)
        close_btn = QPushButton(tr("Zamknij"))
        close_btn.clicked.connect(self.close)
        btn_row = QHBoxLayout()
        btn_row.addWidget(self.clear_btn)
        btn_row.addStretch()
        btn_row.addWidget(close_btn)

        layout = QVBoxLayout(self)
        layout.addWidget(self.list_widget)
        layout.addWidget(self.total_label)
        layout.addLayout(btn_row)

        self._refresh()

    def _refresh(self) -> None:
        history = config.load_session_history()
        self.list_widget.clear()
        for entry in reversed(history):  # most recent first
            try:
                started = datetime.fromisoformat(entry["started_at"]).strftime("%Y-%m-%d %H:%M")
            except ValueError:
                started = entry["started_at"]
            minutes, seconds = divmod(int(entry["duration_seconds"]), 60)
            self.list_widget.addItem(f"{started} — {minutes:02d}:{seconds:02d} — ~${entry['cost_usd']:.2f}")
        total_cost = sum(e["cost_usd"] for e in history)
        self.total_label.setText(f"{tr('Razem')}: ~${total_cost:.2f} ({len(history)} {tr('sesji')})")

    def _on_clear(self) -> None:
        answer = QMessageBox.question(
            self, tr("Wyczyścić historię?"), tr("Usunąć całą zapisaną historię sesji? Tego nie można cofnąć.")
        )
        if answer == QMessageBox.StandardButton.Yes:
            config.clear_session_history()
            self._refresh()


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
        self.setWindowTitle(tr("Zapisane głosy"))
        self._voices = config.load_saved_voices()

        self.list_widget = QListWidget()
        self._refresh_list()

        hint = QLabel(
            tr(
                "ID głosu skopiuj z portalu app.palabra.ai/voices (zakładka biblioteki głosów"
                " lub klonowanie)."
            )
        )
        hint.setWordWrap(True)

        add_row = QHBoxLayout()
        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText(tr("Nazwa (np. Lektor)"))
        self.id_edit = QLineEdit()
        self.id_edit.setPlaceholderText(tr("ID głosu z app.palabra.ai/voices"))
        add_row.addWidget(self.name_edit)
        add_row.addWidget(self.id_edit)

        btn_row = QHBoxLayout()
        add_btn = QPushButton(tr("Dodaj"))
        add_btn.clicked.connect(self._on_add)
        remove_btn = QPushButton(tr("Usuń zaznaczony"))
        remove_btn.clicked.connect(self._on_remove)
        close_btn = QPushButton(tr("Zamknij"))
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
            QMessageBox.warning(self, tr("Brak danych"), tr("Podaj nazwę i ID głosu."))
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


class GlossaryDialog(QDialog):
    """Manages a local word-pair list that forces specific source->target
    translations (e.g. proper names, terminology) for the language pair
    currently selected in the main window, and pushes it to Palabra's
    glossary REST API (see app/glossary.py).

    There's no server-side "edit" available for an existing glossary
    (confirmed against the real API -- see glossary.py's own docstring):
    every Save deletes whatever glossary previously represented this
    language pair (if any) and uploads a fresh one with the current list.
    Local edits (Dodaj/Usuń) are saved to disk immediately so they
    survive closing the dialog without Save, but only take effect in
    actual translations once pushed -- see the status label.
    """

    def __init__(self, source_lang: str, target_lang: str, source_lang_name: str, target_lang_name: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle(tr("Glosariusz"))
        self._source_lang = source_lang
        self._target_lang = target_lang
        self._pairs, self._glossary_id, synced_pairs = config.load_glossary_entries(source_lang, target_lang)
        # Dirty means "the local list differs from what was actually last
        # pushed to Palabra" -- computed from the persisted synced_pairs
        # rather than always assuming False on open, otherwise reopening the
        # dialog after closing without saving would falsely show a green
        # "Aktywny w Palabra." status for a list that was never actually sent.
        self._dirty = self._pairs != synced_pairs
        self._saver_worker: GlossarySaver | None = None
        self._saver_thread: threading.Thread | None = None

        pair_label = QLabel(f"{tr('Para językowa')}: {source_lang_name} → {target_lang_name}")
        pair_label.setStyleSheet("font-weight: bold;")

        hint = QLabel(
            tr(
                "Wymusza dokładne tłumaczenie podanych słów/fraz (np. imion biblijnych) zamiast"
                " tego, co Palabra przetłumaczyłaby sama. Dotyczy tylko powyższej pary językowej --"
                " dla innej pary trzeba otworzyć to okno ponownie po jej wybraniu."
            )
        )
        hint.setWordWrap(True)

        self.list_widget = QListWidget()
        self._refresh_list()

        add_row = QHBoxLayout()
        self.source_edit = QLineEdit()
        self.source_edit.setPlaceholderText(tr("Słowo źródłowe (np. Jehowa)"))
        self.target_edit = QLineEdit()
        self.target_edit.setPlaceholderText(tr("Tłumaczenie (np. Jehovah)"))
        add_row.addWidget(self.source_edit)
        add_row.addWidget(self.target_edit)

        btn_row = QHBoxLayout()
        self.add_btn = QPushButton(tr("Dodaj"))
        self.add_btn.clicked.connect(self._on_add)
        self.remove_btn = QPushButton(tr("Usuń zaznaczone"))
        self.remove_btn.clicked.connect(self._on_remove)
        btn_row.addWidget(self.add_btn)
        btn_row.addWidget(self.remove_btn)
        btn_row.addStretch()

        self.status_label = QLabel("")
        self.status_label.setWordWrap(True)
        # Defense-in-depth alongside _request()'s own truncation of non-JSON
        # error bodies: even a truncated HTML-ish message shouldn't be given
        # a chance to rich-text-render and distort this dialog's layout.
        self.status_label.setTextFormat(Qt.TextFormat.PlainText)

        manage_btn = QPushButton(tr("Wszystkie glosariusze na koncie..."))
        manage_btn.setToolTip(
            tr(
                "Pokazuje wszystkie glosariusze zapisane na koncie Palabra (nie tylko dla tej pary"
                " językowej) i pozwala usunąć dowolny z nich -- przydatne, jeśli jakiś pozostał"
                " aktywny mimo utraty lokalnego zapisu w tej aplikacji."
            )
        )
        manage_btn.clicked.connect(self._on_manage_all)

        save_row = QHBoxLayout()
        self.save_btn = QPushButton(tr("Zapisz w Palabra"))
        self.save_btn.clicked.connect(self._on_save)
        close_btn = QPushButton(tr("Zamknij"))
        close_btn.clicked.connect(self.close)
        save_row.addWidget(self.save_btn)
        save_row.addWidget(manage_btn)
        save_row.addStretch()
        save_row.addWidget(close_btn)

        layout = QVBoxLayout(self)
        layout.addWidget(pair_label)
        layout.addWidget(hint)
        layout.addWidget(self.list_widget)
        layout.addLayout(add_row)
        layout.addLayout(btn_row)
        layout.addWidget(self.status_label)
        layout.addLayout(save_row)

        self._update_status_label()

    def _refresh_list(self) -> None:
        self.list_widget.clear()
        for src, tgt in self._pairs:
            self.list_widget.addItem(f"{src} → {tgt}")

    def _update_status_label(self) -> None:
        if self._dirty:
            self.status_label.setStyleSheet(f"color: {theme.WARNING};")
            self.status_label.setText(
                tr('Niezapisane zmiany -- kliknij "Zapisz w Palabra", żeby zaczęły obowiązywać.')
            )
        elif self._glossary_id is not None:
            self.status_label.setStyleSheet(f"color: {theme.SUCCESS};")
            self.status_label.setText(tr("Aktywny w Palabra."))
        else:
            self.status_label.setStyleSheet("")
            self.status_label.setText(tr("Brak aktywnego glosariusza dla tej pary językowej."))

    def _on_add(self) -> None:
        src = self.source_edit.text().strip()
        tgt = self.target_edit.text().strip()
        if not src or not tgt:
            QMessageBox.warning(self, tr("Brak danych"), tr("Podaj słowo źródłowe i jego tłumaczenie."))
            return
        self._pairs.append((src, tgt))
        self._dirty = True
        config.save_glossary_entries(self._source_lang, self._target_lang, self._pairs, self._glossary_id)
        self.source_edit.clear()
        self.target_edit.clear()
        self._refresh_list()
        self._update_status_label()

    def _on_remove(self) -> None:
        row = self.list_widget.currentRow()
        if row < 0:
            return
        del self._pairs[row]
        self._dirty = True
        config.save_glossary_entries(self._source_lang, self._target_lang, self._pairs, self._glossary_id)
        self._refresh_list()
        self._update_status_label()

    def _on_save(self) -> None:
        creds = config.load_credentials()
        if not creds.api_key:
            QMessageBox.warning(
                self, tr("Brak klucza"), tr("Ustaw klucz API w Ustawieniach przed zapisem glosariusza.")
            )
            return
        self.save_btn.setEnabled(False)
        # Also block local edits for the duration of the save: GlossarySaver
        # captures a snapshot of self._pairs at dispatch time below, so an
        # edit made while the save is in flight would be silently lost --
        # the dialog would still end up reporting "✓ Zapisano w Palabra."
        # for a list that was never actually sent.
        self.add_btn.setEnabled(False)
        self.remove_btn.setEnabled(False)
        self.status_label.setStyleSheet("")
        self.status_label.setText(tr("Zapisywanie..."))

        sent_pairs = list(self._pairs)
        worker = GlossarySaver(
            creds.api_key, "CRC Translator", self._source_lang, self._target_lang,
            sent_pairs, self._glossary_id,
        )
        worker.finished.connect(
            lambda ok, message, new_id: self._on_save_finished(ok, message, new_id, sent_pairs),
            Qt.ConnectionType.QueuedConnection,
        )
        thread = threading.Thread(target=worker.run, daemon=True)
        self._saver_worker = worker
        self._saver_thread = thread
        thread.start()

    def _on_save_finished(self, ok: bool, message: str, new_glossary_id: object, sent_pairs: list[tuple[str, str]]) -> None:
        self.save_btn.setEnabled(True)
        self.add_btn.setEnabled(True)
        self.remove_btn.setEnabled(True)
        self._saver_worker = None
        self._saver_thread = None
        if ok:
            self._glossary_id = new_glossary_id
            # Dirty only if the list changed again after this save was sent
            # (impossible right now since edits were blocked above, but this
            # keeps the check correct instead of assuming "still False").
            self._dirty = self._pairs != sent_pairs
            config.save_glossary_entries(
                self._source_lang, self._target_lang, self._pairs, self._glossary_id, synced_pairs=sent_pairs
            )
            self.status_label.setStyleSheet(f"color: {theme.SUCCESS};")
            self.status_label.setText(f"✓ {message}")
        else:
            self.status_label.setStyleSheet(f"color: {theme.DANGER};")
            self.status_label.setText(f"✗ {message}")

    def _on_manage_all(self) -> None:
        GlossaryManagerDialog(self).exec()

    def closeEvent(self, event) -> None:
        # Mirrors SettingsDialog.closeEvent: waits for the save thread so
        # closing (or reopening) the dialog mid-save can't let a stale,
        # already-superseded glossary_id get written back to disk -- which
        # would silently orphan whichever glossary the in-flight save was
        # about to make active (see app/glossary.py's module docstring).
        if self._saver_thread is not None:
            thread = self._saver_thread
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


class GlossaryManagerDialog(QDialog):
    """Lists every glossary on the account (not just the one this app's
    local glossary.json currently knows about for the selected language
    pair) and lets the user delete any of them -- the only in-app way to
    find and remove an orphaned glossary: one whose glossary_id was lost
    locally (a partial save, editing the same account from another
    machine, or a bug) but is still is_enabled on Palabra's side, silently
    rewriting every future translation for its language pair with no
    other way to even see it (see app/glossary.py's list_glossaries
    docstring)."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle(tr("Wszystkie glosariusze na koncie"))
        self.resize(500, 350)
        self._list_thread: threading.Thread | None = None
        self._list_worker: GlossaryLister | None = None
        self._delete_thread: threading.Thread | None = None
        self._delete_worker: GlossaryDeleter | None = None
        self._glossaries: list[dict] = []

        self.list_widget = QListWidget()
        self.status_label = QLabel("")
        self.status_label.setWordWrap(True)
        self.status_label.setTextFormat(Qt.TextFormat.PlainText)

        btn_row = QHBoxLayout()
        self.refresh_btn = QPushButton(tr("Odśwież"))
        self.refresh_btn.clicked.connect(self._refresh)
        self.delete_btn = QPushButton(tr("Usuń zaznaczony"))
        self.delete_btn.clicked.connect(self._on_delete)
        close_btn = QPushButton(tr("Zamknij"))
        close_btn.clicked.connect(self.close)
        btn_row.addWidget(self.refresh_btn)
        btn_row.addWidget(self.delete_btn)
        btn_row.addStretch()
        btn_row.addWidget(close_btn)

        layout = QVBoxLayout(self)
        layout.addWidget(self.list_widget)
        layout.addWidget(self.status_label)
        layout.addLayout(btn_row)

        self._refresh()

    def _refresh(self) -> None:
        creds = config.load_credentials()
        if not creds.api_key:
            self.status_label.setStyleSheet(f"color: {theme.DANGER};")
            self.status_label.setText(tr("Ustaw klucz API w Ustawieniach przed zarządzaniem glosariuszami."))
            return
        self.refresh_btn.setEnabled(False)
        self.delete_btn.setEnabled(False)
        self.status_label.setStyleSheet("")
        self.status_label.setText(tr("Wczytywanie..."))

        worker = GlossaryLister(creds.api_key)
        worker.finished.connect(self._on_list_finished, Qt.ConnectionType.QueuedConnection)
        thread = threading.Thread(target=worker.run, daemon=True)
        self._list_worker = worker
        self._list_thread = thread
        thread.start()

    def _on_list_finished(self, ok: bool, message: str, items: object) -> None:
        self.refresh_btn.setEnabled(True)
        self.delete_btn.setEnabled(True)
        self._list_worker = None
        self._list_thread = None
        if not ok:
            self.status_label.setStyleSheet(f"color: {theme.DANGER};")
            self.status_label.setText(f"✗ {message}")
            return
        self._glossaries = items or []
        self.list_widget.clear()
        for g in self._glossaries:
            state = tr("włączony") if g.get("is_enabled") else tr("wyłączony")
            self.list_widget.addItem(
                f"{g.get('name', '?')} -- {g.get('source_lang', '?')} → {g.get('target_lang', '?')} ({state})"
            )
        self.status_label.setStyleSheet("")
        self.status_label.setText("" if self._glossaries else tr("Brak glosariuszy na koncie."))

    def _on_delete(self) -> None:
        row = self.list_widget.currentRow()
        if row < 0 or row >= len(self._glossaries):
            return
        target = self._glossaries[row]
        answer = QMessageBox.question(
            self,
            tr("Usunąć glosariusz?"),
            f"{tr('Usunąć')} \"{target.get('name', '?')}\" "
            f"({target.get('source_lang', '?')} → {target.get('target_lang', '?')})? "
            f"{tr('Tego nie można cofnąć.')}",
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        creds = config.load_credentials()
        if not creds.api_key:
            return
        self.refresh_btn.setEnabled(False)
        self.delete_btn.setEnabled(False)
        self.status_label.setStyleSheet("")
        self.status_label.setText(tr("Usuwanie..."))

        worker = GlossaryDeleter(creds.api_key, target["glossary_id"])
        worker.finished.connect(self._on_delete_finished, Qt.ConnectionType.QueuedConnection)
        thread = threading.Thread(target=worker.run, daemon=True)
        self._delete_worker = worker
        self._delete_thread = thread
        thread.start()

    def _on_delete_finished(self, ok: bool, message: str, glossary_id: str) -> None:
        self._delete_worker = None
        self._delete_thread = None
        if not ok:
            self.refresh_btn.setEnabled(True)
            self.delete_btn.setEnabled(True)
            self.status_label.setStyleSheet(f"color: {theme.DANGER};")
            self.status_label.setText(f"✗ {message}")
            return
        # Keeps any already-open GlossaryDialog for this language pair from
        # later writing back a glossary_id that no longer exists anywhere.
        config.clear_glossary_id_if_matches(glossary_id)
        self._refresh()  # re-enables buttons + re-fetches the now-updated list

    def closeEvent(self, event) -> None:
        for thread in (self._list_thread, self._delete_thread):
            if thread is None:
                continue
            wait_loop = QEventLoop()
            poll_timer = QTimer()
            poll_timer.timeout.connect(lambda t=thread: None if t.is_alive() else wait_loop.quit())
            poll_timer.start(50)
            safety_timer = QTimer()
            safety_timer.setSingleShot(True)
            safety_timer.timeout.connect(wait_loop.quit)
            safety_timer.start(3000)
            wait_loop.exec()
        super().closeEvent(event)


@dataclass
class SessionConfig:
    api_key: str
    source_lang: str
    target_lang: str
    mic_device: int | None
    output_device: int | None
    file_path: str | None
    mic_channel: int | None = None
    mic_gain: float = 1.0
    mic_gate_threshold: float = 0.0
    voice_id: str | None = None
    voice_cloning: bool = False
    subtitles_only: bool = False
    church_style: bool = False


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

    def __init__(self, config: SessionConfig):
        super().__init__()
        self._api_key = config.api_key
        self._source_lang = config.source_lang
        self._target_lang = config.target_lang
        self._mic_device = config.mic_device
        self._mic_channel = config.mic_channel
        self._output_device = config.output_device
        self._file_path = config.file_path
        self._initial_mic_gain = config.mic_gain
        self._initial_gate_threshold = config.mic_gate_threshold
        self._voice_id = config.voice_id
        self._voice_cloning = config.voice_cloning
        self._subtitles_only = config.subtitles_only
        self._church_style = config.church_style
        self._loop: asyncio.AbstractEventLoop | None = None
        self._runner: TranslationRunner | None = None
        self._mic_source: MicStream | None = None
        self._mixed_source: MixedSource | None = None
        self._sink: OutputSink | None = None
        # Created here (not in start()) so stop() is safe to call the instant the
        # worker exists, even before the background thread has begun running —
        # otherwise a fast Start-then-Stop click could be silently lost.
        self._stop_event = threading.Event()

    @property
    def position_ms(self) -> float:
        return self._mixed_source.position_ms if self._mixed_source else 0.0

    @property
    def total_ms(self) -> float:
        return self._mixed_source.total_ms if self._mixed_source else 0.0

    @property
    def mic_level(self) -> float:
        return self._mixed_source.mic_level if self._mixed_source else 0.0

    @property
    def output_level(self) -> float:
        return self._sink.level if self._sink else 0.0

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
            mic = MicStream(device=self._mic_device, channel=self._mic_channel)
            mic.set_gain(self._initial_mic_gain)
            mic.set_gate_threshold(self._initial_gate_threshold)
            self._mic_source = mic
            file = None
            if self._file_path is not None:
                # loop=self._loop explicitly: this runs before
                # self._loop.run_until_complete() below, so it isn't
                # RUNNING yet -- FileStream's own get_running_loop()
                # fallback needs an active loop, not just one that's been
                # set as current (see FileStream's constructor comment).
                file = FileStream(self._file_path, loop=self._loop)
                file.pause()  # never autoplay a file that's active at Start
            source_cm = MixedSource(mic, file, on_error=self.error_occurred.emit)
            self._mixed_source = source_cm
            with source_cm as source, OutputSink(device=self._output_device) as sink:
                self._sink = sink
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
                    church_style=self._church_style,
                )
                self._loop.run_until_complete(self._runner.run())
        except Exception as e:  # device open failure etc. — before/outside TranslationRunner's own handling
            self.error_occurred.emit(f"{tr('Błąd urządzenia audio')}: {e}")
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

    def change_mic_device(self, device_index: int, channel: int | None) -> None:
        self._call_on_loop("request_change_mic_device", device_index, channel)

    def set_file(self, path: str | None) -> None:
        self._call_on_loop("request_set_file", path)

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
        # Always update self._subtitles_only (also covers the pre-runner
        # "connecting" window: SessionWorker.start() reads it when it builds
        # the TranslationRunner, whereas before, a toggle received before
        # self._runner existed yet was silently dropped -- found in final
        # review, see the SDD ledger).
        self._subtitles_only = muted
        if self._runner is not None:
            self._runner.set_mute_output(muted)


_STATE_LABELS = {
    SessionState.CONNECTING: "Łączenie...",
    SessionState.RECONNECTING: "Rozłączono, ponawiam próbę...",
    SessionState.RUNNING: "Tłumaczę na żywo",
    SessionState.PAUSED: "Wstrzymano",
    SessionState.STOPPED: "Zatrzymano",
    SessionState.ERROR: "Błąd",
}

# Status banner colors per state (see MainWindow.control_row's status_frame/
# status_dot) -- amber for "working on it" (matches theme.ACCENT, the app's
# one accent color), the same red the RECONNECTING text already uses for
# trouble, and the same green as the mic level meter for "actually on air".
# A full tinted banner, not just a small dot, so the live/error state reads
# at a glance during an actual service -- exactly the kind of thing a studio
# tally light is for. Background tints are the dot color darkened toward
# theme.BG_PANEL, not the flat accent, so status_label's text stays legible
# on top of them.
_STATUS_DOT_IDLE_COLOR = theme.TEXT_DISABLED
_STATUS_BANNER_IDLE_BG = theme.BG_PANEL
_STATUS_DOT_COLORS = {
    SessionState.CONNECTING: theme.ACCENT,
    SessionState.RECONNECTING: theme.DANGER,
    SessionState.RUNNING: "#4caf50",
    SessionState.PAUSED: theme.ACCENT,
    SessionState.STOPPED: _STATUS_DOT_IDLE_COLOR,
    SessionState.ERROR: theme.DANGER,
}
_STATUS_BANNER_BG_COLORS = {
    SessionState.CONNECTING: "#332a16",
    SessionState.RECONNECTING: "#331a1a",
    SessionState.RUNNING: "#173319",
    SessionState.PAUSED: "#332a16",
    SessionState.STOPPED: _STATUS_BANNER_IDLE_BG,
    SessionState.ERROR: "#331a1a",
}

# Palabra S2S pricing (see README) -- used only for a rough, client-side
# running estimate next to the session timer. Palabra doesn't expose actual
# balance/usage via API (see the Settings dialog's "Otwórz panel Palabra"),
# so this is deliberately approximate, not authoritative.
PALABRA_COST_PER_MINUTE_USD = 0.04

# See _update_position()'s use of _file_swap_baseline_total_ms: how long to
# wait for total_ms to actually change (confirming a live file swap landed)
# before giving up and re-enabling the controls anyway. Bounded so a file
# that coincidentally has the exact same duration as the one it replaced
# can't leave the slider/skip buttons stuck disabled forever.
FILE_SWAP_TIMEOUT_SECONDS = 3.0

# See _on_transcript()'s use of _transcript_history: how many past
# transcript events (source + translation) are kept for the log
# filter/tag toggle to re-render from (see _rebuild_log) and for a
# newly-opened overlay to backfill from. A TranscriptEvent is tiny (a
# short string + a couple of floats/bools), so keeping a full heavy
# workday's worth of them (measured: ~2,000/hour of continuous speech)
# costs a few MB, not the hundreds of MB the log's own QPlainTextEdit
# widget uses per line -- generous on purpose, since the old cap of 300
# meant switching the log filter mid-session silently discarded almost
# all of a long session's transcript.
MAX_TRANSCRIPT_HISTORY_ENTRIES = 20_000

# MainWindow's default open height for the settings scroll area (see
# settings_scroll's construction): comfortably fits the collapsed default
# view (mic/output/language, nothing exotic open) without scrolling.
# Deliberately a constant here, not settings_scroll's own sizeHint()/
# minimumSizeHint() queried at the point of use -- those are measured
# before the window's first show() and unreliable for this. Measured with
# an isolated (no locally saved settings) fresh install, matching what a
# real first launch actually sees: collapsed content sizeHint() is ~402px;
# an earlier ~565px reading here turned out to be this dev machine's own
# leftover advanced_expanded=true test state, not the true collapsed size
# -- worth remembering since it's an easy way to re-fool this measurement
# again (see MainWindow's "how to verify" note, if one gets added).
_SETTINGS_SCROLL_DESIRED_HEIGHT = 460


def _estimated_cost(seconds: float) -> float:
    return (seconds / 60) * PALABRA_COST_PER_MINUTE_USD


def _fmt_ms(ms: float) -> str:
    total_seconds = int(ms // 1000)
    return f"{total_seconds // 60:02d}:{total_seconds % 60:02d}"


def _is_newer_version(candidate: str, current: str) -> bool:
    """Compares two dotted version strings numerically (1.10.0 > 1.9.0),
    not lexicographically. Malformed input (missing/non-numeric parts)
    is treated as "not newer" -- never blocks/crashes the check."""
    try:
        candidate_parts = tuple(int(p) for p in candidate.strip().split("."))
        current_parts = tuple(int(p) for p in current.strip().split("."))
    except ValueError:
        return False
    return candidate_parts > current_parts


class UpdateChecker(QObject):
    """Checks GitHub's "latest release" once in the background and reports
    back only if it's actually newer than this running build. Silent on any
    failure (offline, rate-limited, GitHub down, unexpected response shape)
    -- this is a courtesy notice, never allowed to interrupt or delay
    startup, so failures are swallowed rather than surfaced anywhere.
    """

    update_found = Signal(str, str)  # new_version, release_url

    def start(self) -> None:
        threading.Thread(target=self._check, daemon=True).start()

    def _check(self) -> None:
        try:
            request = urllib.request.Request(
                "https://api.github.com/repos/dszwed86/CRC_StS/releases/latest",
                headers={"Accept": "application/vnd.github+json"},
            )
            with urllib.request.urlopen(request, timeout=5) as response:
                data = json.loads(response.read().decode("utf-8"))
            latest_version = str(data.get("tag_name", "")).lstrip("v")
            release_url = str(data.get("html_url", ""))
            if latest_version and release_url and _is_newer_version(latest_version, __version__):
                self.update_found.emit(latest_version, release_url)
        except Exception:
            pass


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        # Must run before any tr()-wrapped widget text below is built: tr()
        # reads the language set here, and (see i18n.py) this app has no
        # QTranslator/retranslateUi wiring to re-apply it to already-built
        # widgets later -- a language switch in Settings only takes effect
        # on the next launch, which is exactly this code path again.
        i18n.set_language(config.load_app_settings().get("language", i18n.LANG_PL))
        self.setWindowTitle(tr("CRC Translator"))
        self._thread: threading.Thread | None = None
        self._worker: SessionWorker | None = None
        self._selected_file: str | None = None
        self._overlay: OverlayWindow | None = None
        self._transcript_history: list[TranscriptEvent] = []
        # Collapses consecutive identical *finalized* log lines of the same
        # kind (source vs. translation) into one "text xN" line instead of
        # one line per repeat -- keyed by is_translation since source and
        # translation repeats are independent and interleave in the default
        # "both" log filter view. _log_repeat_block holds a reference to the
        # QTextBlock currently showing that kind's last finalized line, so
        # it can be re-edited directly even after other lines were appended.
        self._log_repeat_state: dict[bool, tuple[str | None, int]] = {True: (None, 0), False: (None, 0)}
        self._log_repeat_block: dict[bool, QTextBlock | None] = {True: None, False: None}

        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)

        settings_row = QHBoxLayout()
        self.update_btn = QPushButton("")
        self.update_btn.setVisible(False)
        self.update_btn.setStyleSheet(f"QPushButton {{ color: {theme.LINK}; border: none; text-decoration: underline; }}")
        self.update_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.update_btn.clicked.connect(self._on_open_update_url)
        self._update_url: str | None = None
        settings_row.addWidget(self.update_btn)
        settings_row.addStretch()
        self.settings_btn = QPushButton(tr("Ustawienia..."))
        self.settings_btn.clicked.connect(self._open_settings)
        settings_row.addWidget(self.settings_btn)
        root.addLayout(settings_row)

        source_group = QGroupBox(tr("Źródło dźwięku"))
        form = QFormLayout(source_group)

        self.mic_combo = QComboBox()
        # Device names are arbitrary and sometimes very long (real examples
        # seen on this app's own dev/test machines: a ~70-character virtual
        # audio device name on Windows, similarly long Bluetooth/interface
        # names on macOS). QComboBox's default AdjustToContentsOnFirstShow
        # policy sizes the box (and therefore the whole window, since
        # nothing here scrolls -- see MainWindow's layout) to fit the
        # WIDEST item, which made the window unable to shrink below ~1500px
        # wide on a machine with one such device plugged in -- wider than
        # many laptop screens (confirmed: this is what "the window doesn't
        # fit on my Mac" traced back to). Cap the combo's own width instead;
        # the current selection still elides with "..." if too long to
        # display, and the full name remains visible via the tooltip below
        # and in the dropdown popup itself.
        self.mic_combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.mic_combo.setMinimumContentsLength(24)
        self._input_devices = list_input_devices()
        for d in self._input_devices:
            self.mic_combo.addItem(d.name, d.index)
        self.mic_combo.currentIndexChanged.connect(self._on_mic_selection_changed)
        self.mic_combo.currentIndexChanged.connect(
            lambda _i: self.mic_combo.setToolTip(self.mic_combo.currentText())
        )
        self.mic_combo.setToolTip(self.mic_combo.currentText())

        # Only shown for a device with more than one input channel (e.g. a
        # 2-in audio interface like a Behringer UMC202HD, with two
        # different microphones on channels 1 and 2) -- hidden for the
        # vast majority of ordinary single-source mics. See
        # _populate_mic_channel_combo() and MicStream's channel comment for
        # why the first item still maps to data=None (preserves today's
        # default capture path untouched) rather than an explicit 0.
        self.mic_channel_combo = QComboBox()
        self.mic_channel_combo.setToolTip(
            tr("Który kanał wejściowy tego urządzenia nagrywać (dla interfejsów z więcej niż jednym wejściem).")
        )
        self.mic_channel_combo.setVisible(False)
        self.mic_channel_combo.currentIndexChanged.connect(self._on_mic_channel_changed)
        self._populate_mic_channel_combo()

        self.mic_gain_row = QWidget()
        mic_gain_outer = QVBoxLayout(self.mic_gain_row)
        mic_gain_outer.setContentsMargins(0, 0, 0, 0)

        gain_row = QHBoxLayout()
        gain_row.addWidget(QLabel(tr("Głośność mikrofonu:")))
        self.mic_gain_slider = QSlider(Qt.Orientation.Horizontal)
        self.mic_gain_slider.setRange(0, 100)
        self.mic_gain_slider.setValue(100)
        self.mic_gain_slider.valueChanged.connect(self._on_mic_gain_changed)
        self.mic_gain_label = QLabel("100%")
        gain_row.addWidget(self.mic_gain_slider, stretch=1)
        gain_row.addWidget(self.mic_gain_label)
        self.mic_mute_check = QCheckBox(tr("Wycisz"))
        self.mic_mute_check.setToolTip(tr("Wycisza mikrofon bez zmiany ustawionej głośności (skrót: M)."))
        self.mic_mute_check.toggled.connect(self._on_mic_mute_toggled)
        gain_row.addWidget(self.mic_mute_check)
        mic_gain_outer.addLayout(gain_row)

        level_row = QHBoxLayout()
        level_row.addWidget(QLabel(tr("Poziom sygnału:")))
        self.mic_level_bar = QProgressBar()
        self.mic_level_bar.setRange(0, 100)
        self.mic_level_bar.setValue(0)
        self.mic_level_bar.setTextVisible(False)
        self.mic_level_bar.setFixedHeight(16)
        self.mic_level_bar.setToolTip(
            tr(
                "Poziom dźwięku odbieranego z mikrofonu na żywo, w trakcie trwającej sesji -- "
                "potwierdza, że mikrofon faktycznie łapie dźwięk, niezależnie od Głośności mikrofonu."
            )
        )
        self.mic_level_bar.setStyleSheet("QProgressBar::chunk { background-color: #4caf50; }")
        level_row.addWidget(self.mic_level_bar, stretch=1)
        mic_gain_outer.addLayout(level_row)

        # A separate widget (not folded into mic_gain_row above) so it can
        # live in the "Zaawansowane" section below instead of always-visible
        # real estate -- a noise gate is set up once and rarely revisited,
        # unlike gain/level right above it. Still fully live-adjustable
        # during a running session either way (see _config_widgets: this
        # slider was never in that list, so moving its container doesn't
        # change that).
        self.mic_gate_row = QWidget()
        gate_row = QHBoxLayout(self.mic_gate_row)
        gate_row.setContentsMargins(0, 0, 0, 0)
        gate_label_text = QLabel(tr("Ignoruj ciszej niż:"))
        gate_tooltip = tr(
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
        self.mic_gate_label = QLabel(tr("Wyłączony"))
        gate_row.addWidget(self.mic_gate_slider, stretch=1)
        gate_row.addWidget(self.mic_gate_label)

        self.file_row = QWidget()
        file_layout = QHBoxLayout(self.file_row)
        file_layout.setContentsMargins(0, 0, 0, 0)
        self.file_label = QLabel(tr("(nie wybrano pliku)"))
        self.file_btn = QPushButton(tr("Wybierz plik..."))
        self.file_btn.clicked.connect(self._choose_file)
        self.file_clear_btn = QPushButton("✕")
        self.file_clear_btn.setToolTip(tr("Usuń wybrany plik"))
        self.file_clear_btn.setEnabled(False)
        self.file_clear_btn.clicked.connect(self._on_clear_file)
        file_layout.addWidget(self.file_label, stretch=1)
        file_layout.addWidget(self.file_btn)
        file_layout.addWidget(self.file_clear_btn)

        self.file_playback_row = QWidget()
        file_playback_layout = QHBoxLayout(self.file_playback_row)
        file_playback_layout.setContentsMargins(0, 0, 0, 0)
        self.file_pause_btn = QPushButton(tr("Pauza pliku"))
        self.file_pause_btn.setToolTip(
            tr("Wstrzymuje/wznawia tylko plik -- mikrofon i reszta sesji nie są tym dotknięte.")
        )
        self.file_pause_btn.setVisible(False)
        self.file_pause_btn.setEnabled(False)
        self.file_pause_btn.clicked.connect(self._on_file_pause_resume)
        self.position_slider = QSlider(Qt.Orientation.Horizontal)
        self.position_slider.setEnabled(False)
        self.position_slider.sliderReleased.connect(self._on_seek)
        self.position_label = QLabel("00:00 / 00:00")
        # Skip buttons: rewind ones go left of the slider, fast-forward ones
        # right of it (common media-player layout) -- same enabled/visible
        # lifecycle as position_slider itself (see _set_skip_buttons_enabled/
        # _set_skip_buttons_visible and their call sites).
        self.skip_buttons: list[QPushButton] = []
        for offset_s in (-7, -3, -1):
            btn = QPushButton(f"{offset_s}s")
            btn.setFixedWidth(36)
            btn.setToolTip(f"{tr('Przewija plik o')} {offset_s}s")
            btn.clicked.connect(lambda checked=False, s=offset_s: self._on_skip_file(s))
            btn.setEnabled(False)
            btn.setVisible(False)
            self.skip_buttons.append(btn)
        file_playback_layout.addWidget(self.file_pause_btn)
        for btn in self.skip_buttons:
            file_playback_layout.addWidget(btn)
        file_playback_layout.addWidget(self.position_slider, stretch=1)
        for offset_s in (1, 3, 7):
            btn = QPushButton(f"+{offset_s}s")
            btn.setFixedWidth(36)
            btn.setToolTip(f"{tr('Przewija plik o')} +{offset_s}s")
            btn.clicked.connect(lambda checked=False, s=offset_s: self._on_skip_file(s))
            btn.setEnabled(False)
            btn.setVisible(False)
            self.skip_buttons.append(btn)
            file_playback_layout.addWidget(btn)
        file_playback_layout.addWidget(self.position_label)
        # Same visibility lifecycle as position_slider/position_label below --
        # no file selected yet at construction time.
        self.position_slider.setVisible(False)
        self.position_label.setVisible(False)

        # mic and file are both always part of every session now -- there is
        # no mode selector, so both rows are always visible; no toggling code
        # needed at all (contrast with the old _on_mode_changed).
        mic_row = QHBoxLayout()
        mic_row.addWidget(self.mic_combo, stretch=1)
        mic_row.addWidget(self.mic_channel_combo)
        self.refresh_devices_btn = QPushButton(tr("Odśwież urządzenia"))
        self.refresh_devices_btn.clicked.connect(self._on_refresh_devices)
        mic_row.addWidget(self.refresh_devices_btn)
        form.addRow(tr("Mikrofon:"), mic_row)
        form.addRow("", self.mic_gain_row)
        form.addRow(tr("Plik (opcjonalnie):"), self.file_row)
        form.addRow("", self.file_playback_row)

        output_group = QGroupBox(tr("Tłumaczenie"))
        form = QFormLayout(output_group)

        self.output_combo = QComboBox()
        # Same fix as mic_combo above (see its comment) -- device names can
        # be arbitrarily long and this combo hit the exact same
        # window-too-wide-to-fit-the-screen problem.
        self.output_combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.output_combo.setMinimumContentsLength(24)
        self._output_devices = list_output_devices()
        cable = find_virtual_cable(self._output_devices)
        for d in self._output_devices:
            self.output_combo.addItem(d.name, d.index)
        self.output_combo.currentIndexChanged.connect(
            lambda _i: self.output_combo.setToolTip(self.output_combo.currentText())
        )
        self.output_combo.setToolTip(self.output_combo.currentText())
        self.output_hint = QLabel(
            tr(
                "Nie wykryto wirtualnego kabla audio (VB-Cable / BlackHole)."
                " Zainstaluj go, aby OBS mógł odebrać tłumaczenie — patrz README."
            )
        )
        self.output_hint.setWordWrap(True)
        self.output_hint.setStyleSheet(f"color: {theme.WARNING};")
        if cable is not None:
            self.output_combo.setCurrentIndex(self._output_devices.index(cable))
            self.output_hint.setVisible(False)

        output_row = QHBoxLayout()
        output_row.addWidget(self.output_combo, stretch=1)
        self.test_output_btn = QPushButton(tr("Testuj wyjście"))
        self.test_output_btn.setToolTip(
            tr(
                "Odtwarza krótki dźwięk testowy na wybrane urządzenie wyjściowe -- "
                "bez uruchamiania sesji Palabra, więc bez kosztu -- przydatne do sprawdzenia, "
                "czy dźwięk faktycznie dociera do tego urządzenia."
            )
        )
        self.test_output_btn.clicked.connect(self._on_test_output)
        output_row.addWidget(self.test_output_btn)
        form.addRow(tr("Wyjście:"), output_row)

        output_level_row = QHBoxLayout()
        output_level_row.addWidget(QLabel(tr("Poziom wyjścia:")))
        self.output_level_bar = QProgressBar()
        self.output_level_bar.setRange(0, 100)
        self.output_level_bar.setValue(0)
        self.output_level_bar.setTextVisible(False)
        self.output_level_bar.setFixedHeight(16)
        self.output_level_bar.setToolTip(
            tr(
                "Poziom dźwięku faktycznie odtwarzanego na wybrane wyjście, na żywo, w trakcie "
                "trwającej sesji -- potwierdza, że przetłumaczone audio realnie dociera do "
                "urządzenia (np. wirtualnego kabla), a nie tylko że zostało odebrane."
            )
        )
        self.output_level_bar.setStyleSheet("QProgressBar::chunk { background-color: #2196f3; }")
        output_level_row.addWidget(self.output_level_bar, stretch=1)
        form.addRow("", output_level_row)

        self.subtitles_only_check = QCheckBox(tr("Tylko napisy (bez dźwięku)"))
        self.subtitles_only_check.setToolTip(
            tr(
                "Odebrane przetłumaczone audio nie jest odtwarzane na wybrane wyjście -- zostaje "
                "tylko tekst (log/overlay). Palabra API nie oferuje trybu bez syntezy mowy, więc "
                "koszt sesji się nie zmienia -- to tylko wycisza odtwarzanie po stronie aplikacji."
            )
        )
        # Not added to this form -- lives in the "Zaawansowane" section
        # below instead (see its construction further down). Still fully
        # live-toggleable during a running session either way (see
        # _config_widgets: this checkbox was never in that list).
        self.subtitles_only_check.toggled.connect(self._on_subtitles_only_toggled)

        self.source_lang_combo = QComboBox()
        for code, name in SOURCE_LANGUAGES:
            self.source_lang_combo.addItem(f"{name} ({code})", code)
        self.source_lang_combo.setCurrentIndex([c for c, _ in SOURCE_LANGUAGES].index(DEFAULT_SOURCE))
        form.addRow(tr("Język źródłowy:"), self.source_lang_combo)

        self.target_lang_combo = QComboBox()
        for code, name in TARGET_LANGUAGES:
            self.target_lang_combo.addItem(f"{name} ({code})", code)
        self.target_lang_combo.setCurrentIndex([c for c, _ in TARGET_LANGUAGES].index(DEFAULT_TARGET))
        form.addRow(tr("Język docelowy:"), self.target_lang_combo)

        self.manage_glossary_btn = QPushButton(tr("Glosariusz..."))
        self.manage_glossary_btn.setToolTip(
            tr(
                "Wymuś własne tłumaczenie konkretnych słów/imion (np. biblijnych) dla obecnie"
                " wybranej pary językowej -- zamiast tego, co Palabra przetłumaczyłaby sama."
            )
        )
        self.manage_glossary_btn.clicked.connect(self._on_manage_glossary)
        # Not added to this form -- see "Zaawansowane" below.

        self.church_style_check = QCheckBox(tr("Styl kościelny"))
        self.church_style_check.setChecked(True)
        self.church_style_check.setToolTip(
            tr(
                "Dostraja tłumaczenie pod rejestr kazań/treści religijnych (Palabra: style="
                "church_catholic) -- np. poprawnie oddaje idiomy biblijne i liczebniki, zamiast"
                " dosłownego tłumaczenia słowo w słowo. Zmierzone: bez dodatkowego opóźnienia."
                " Zmienia znaczną część zdań stylistycznie, więc wyłącz dla świeckich sesji."
            )
        )
        # Not added to this form -- see "Zaawansowane" below.

        self.voice_combo = QComboBox()
        self.voice_combo.currentIndexChanged.connect(self._on_voice_mode_changed)
        self.voice_combo.currentIndexChanged.connect(self._on_voice_selection_changed)
        self.voice_custom_edit = QLineEdit()
        self.voice_custom_edit.setPlaceholderText(tr("ID głosu z app.palabra.ai/voices"))
        self.voice_custom_edit.setVisible(False)
        self.voice_custom_edit.editingFinished.connect(self._on_voice_custom_edit_finished)
        self.manage_voices_btn = QPushButton(tr("Zapisane głosy..."))
        self.manage_voices_btn.clicked.connect(self._on_manage_voices)
        self._rebuild_voice_combo()
        voice_row = QHBoxLayout()
        voice_row.addWidget(self.voice_combo, stretch=1)
        voice_row.addWidget(self.voice_custom_edit, stretch=1)
        voice_row.addWidget(self.manage_voices_btn)
        # Not added to this form -- see "Zaawansowane" below.

        # Settings set up once and rarely revisited, collapsed by default so
        # the window opens showing only what's needed every session (mic,
        # output, language pair) -- confirmed by a real report that the full
        # uncollapsed form made the window too tall to fit/resize on a
        # smaller laptop screen. Expand/collapse state is remembered (see
        # _save_app_settings/_apply_saved_app_settings).
        self.advanced_toggle_btn = QPushButton(f"▸ {tr('Zaawansowane')}")
        self.advanced_toggle_btn.setObjectName("advancedToggle")
        self.advanced_toggle_btn.setCheckable(True)
        self.advanced_toggle_btn.toggled.connect(self._on_advanced_toggled)

        # A QFrame (styled as its own small card via objectName -- see
        # theme.py), not a bare QWidget: lying directly on the window
        # background (unlike source_group/output_group above, both real
        # QGroupBox panels) made this section read as visually unfinished,
        # its hierarchy disconnected from the two panels above it.
        self.advanced_panel = QFrame()
        self.advanced_panel.setObjectName("advancedPanel")
        advanced_form = QFormLayout(self.advanced_panel)
        advanced_form.addRow("", self.mic_gate_row)
        advanced_form.addRow("", self.subtitles_only_check)
        # Not just the bare button -- addStretch() keeps it at its natural
        # width instead of stretching across the whole row with its label
        # centered, which read as a text field rather than a button.
        glossary_btn_row = QHBoxLayout()
        glossary_btn_row.addWidget(self.manage_glossary_btn)
        glossary_btn_row.addStretch()
        advanced_form.addRow("", glossary_btn_row)
        advanced_form.addRow("", self.church_style_check)
        advanced_form.addRow(tr("Głos:"), voice_row)
        self.advanced_panel.setVisible(False)

        settings_container = QWidget()
        settings_layout = QVBoxLayout(settings_container)
        settings_layout.setContentsMargins(0, 0, 0, 0)
        settings_layout.addWidget(source_group)
        settings_layout.addWidget(output_group)
        settings_layout.addWidget(self.output_hint)
        settings_layout.addWidget(self.advanced_toggle_btn)
        settings_layout.addWidget(self.advanced_panel)
        # Docks the content to the top instead of the group boxes/panel
        # silently stretching to fill whatever extra room the scroll
        # viewport ends up with (see settings_scroll's own stretch comment
        # below for why that room should mostly go to the log instead).
        settings_layout.addStretch()

        # Scrolls internally instead of forcing the whole window taller than
        # the screen -- the same class of bug the mic/output combo width fix
        # addressed, but for height: this app's settings have grown several
        # times over (glossary, mic channel picker, church style...) and
        # native control sizing is taller on macOS than Windows, so a fixed
        # height here would only be one more feature away from breaking
        # again. Only this settings block scrolls -- status/Start/Stop and
        # the transcript log below always stay on-screen, never scrolled
        # away, since those matter for the whole duration of a live session.
        settings_scroll = QScrollArea()
        settings_scroll.setWidgetResizable(True)
        settings_scroll.setFrameShape(QFrame.Shape.NoFrame)
        # Never horizontal -- settings_container's own content never needs
        # to scroll sideways (that specific failure mode is exactly what
        # the mic/output combo width fix upstream prevents); leaving Qt's
        # default "auto" policy showed a horizontal scrollbar (with a
        # visible few pixels of real scroll range) at the default window
        # size purely because the target width computed below doesn't
        # reserve room for the vertical scrollbar's own width.
        settings_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        settings_scroll.setWidget(settings_container)
        self._settings_scroll = settings_scroll
        # A MAXIMUM only -- deliberately no setMinimumHeight here. A minimum
        # would be a hard floor the layout system enforces even when the
        # user (or a small screen, at open time -- see the final resize()
        # below) tries to shrink the window, which would silently
        # reintroduce the exact "can't resize small enough to fit the
        # screen" bug this scroll area exists to prevent, just with a
        # bigger floor than before. 900 bounds how tall this can grow
        # (an expanded "Zaawansowane" panel) without stopping it from
        # shrinking arbitrarily small with its own scrollbar -- the actual
        # DEFAULT open height is computed explicitly in the final resize()
        # below instead, via the _SETTINGS_SCROLL_DESIRED_HEIGHT constant,
        # not by querying this widget's own sizeHint() (see that resize()
        # call's comment for why that's unreliable pre-show).
        settings_scroll.setMaximumHeight(900)
        # A modest stretch factor, not 0 and not disproportionately high: the
        # final resize() call below already budgets an explicit amount of
        # height for this (_SETTINGS_SCROLL_DESIRED_HEIGHT) and for self.log
        # (log_reasonable_height) -- an earlier attempt at stretch=10 (vs.
        # log's 1) "won" that budget so thoroughly that it claimed most of
        # any leftover space up to its own maximumHeight cap above, leaving
        # ~250px of empty canvas inside its own viewport while self.log (the
        # most important thing to see during an actual live session) was
        # squeezed down to about 3 visible lines -- confirmed directly by
        # measuring both against a truly fresh install (no locally saved
        # settings skewing the numbers). A small ratio in self.log's favor
        # instead sends most of any surplus there, since it's the one that
        # actually benefits from more visible height session over session
        # (the settings area's own content doesn't grow past what it
        # already needs just because the window got taller). Both remain
        # free to shrink all the way down on a genuinely small screen either
        # way, since neither has a hard minimumHeight forcing it not to.
        root.addWidget(settings_scroll, stretch=3)

        self.pause_btn = QPushButton(tr("Pauza"))
        self.pause_btn.setToolTip(
            tr("Wstrzymuje/wznawia całą sesję (mikrofon i plik, jeśli jest), niezależnie od stanu pliku. (F6)")
        )
        self.pause_btn.setEnabled(False)
        self.pause_btn.clicked.connect(self._on_pause_resume)

        self._position_timer = QTimer(self)
        self._position_timer.setInterval(250)
        self._position_timer.timeout.connect(self._update_position)

        # Faster than _position_timer: a level meter reads choppy/laggy at
        # 250ms, needs a shorter interval to look live.
        self._level_timer = QTimer(self)
        self._level_timer.setInterval(80)
        self._level_timer.timeout.connect(self._update_level_meter)
        self._level_timer.timeout.connect(self._update_session_display)

        # Unlike the two timers above, this one runs continuously from
        # startup (not started/stopped around a session) -- it's the
        # periodic, silent version of the "Odśwież urządzenia" button
        # (see _auto_refresh_devices), picking up a plugged-in/unplugged
        # mic or headset while idle without the user needing to click
        # anything or restart the app.
        self._device_refresh_timer = QTimer(self)
        self._device_refresh_timer.setInterval(3000)
        self._device_refresh_timer.timeout.connect(self._auto_refresh_devices)
        self._device_refresh_timer.start()

        self._is_paused = False
        self._file_paused = False
        self._mic_muted = False
        self._pause_request_pending = False
        # Tracks the last state _on_state() received, so _on_error() can
        # tell a reconnect-attempt announcement (fired right after
        # RECONNECTING, see TranslationRunner.run()) apart from any other
        # error message, without depending on the message text itself.
        self._current_session_state: SessionState | None = None
        # A live file swap/clear mid-session (see _choose_file/_on_clear_file)
        # is fire-and-forget -- SessionWorker.set_file() only queues the
        # actual swap onto TranslationRunner's own loop, so self._worker.
        # total_ms/position_ms keep reporting the OLD file's numbers for a
        # little while after the request. _update_position()'s only signal
        # for "has the swap landed" was "total_ms > 0", true for the old
        # file too -- so a slow swap (e.g. _request_lock briefly held by a
        # concurrent seek/voice-change) could re-enable the slider/skip
        # buttons against stale data. These two remember what total_ms was
        # right before the swap was requested, so _update_position can wait
        # for it to actually change (with a bounded timeout so a genuine
        # same-duration coincidence can't leave the controls stuck disabled
        # forever -- see _update_position()).
        self._file_swap_baseline_total_ms: float | None = None
        self._file_swap_pending_since: float | None = None
        self._partial_line_active = False  # last log line is a growing, not-yet-final transcript
        # Billable session time (see _on_state): accumulates only while the
        # server-side session is actually RUNNING, not during Pauza -- matches
        # what Palabra is actually charging for, per the "Pauza also stops
        # billing" behavior already documented in the README.
        self._session_billable_seconds = 0.0
        self._session_running_since: float | None = None
        self._session_started_at: str | None = None
        self._balance_usd: float | None = config.load_balance()

        control_row = QHBoxLayout()
        # A tally-light banner, not just a text label -- a studio's own
        # on-air light is a full colored panel, not a small icon, because it
        # has to register at a glance from across the room. _on_state()
        # below recolors status_frame's background/border and status_dot's
        # fill together, from the matched pair of tables right below
        # MainWindow's class body (_STATUS_DOT_COLORS/_STATUS_BANNER_BG).
        self.status_frame = QFrame()
        self.status_frame.setObjectName("statusFrame")
        status_frame_layout = QHBoxLayout(self.status_frame)
        status_frame_layout.setContentsMargins(10, 4, 10, 4)
        self.status_dot = QLabel()
        self.status_dot.setFixedSize(10, 10)
        self.status_label = QLabel(tr("Gotowy"))
        status_frame_layout.addWidget(self.status_dot)
        status_frame_layout.addWidget(self.status_label, stretch=1)
        self._set_status_colors(_STATUS_DOT_IDLE_COLOR, _STATUS_BANNER_IDLE_BG)
        self.session_time_label = QLabel("")
        self.start_stop_btn = QPushButton(tr("Start"))
        self.start_stop_btn.setObjectName("primaryButton")
        self.start_stop_btn.clicked.connect(self._on_start_stop)
        control_row.addWidget(self.status_frame, stretch=1)
        control_row.addWidget(self.session_time_label)
        control_row.addWidget(self.pause_btn)
        control_row.addWidget(self.start_stop_btn)
        root.addLayout(control_row)

        # F-keys, not letter/Space combos: this window has several text-entry
        # widgets (voice_custom_edit, api_key_edit in Settings), and a
        # QShortcut fires regardless of which child widget has focus --
        # letters or Space would steal keystrokes while typing.
        self.start_stop_btn.setToolTip("F5")
        QShortcut(QKeySequence("F5"), self, activated=self._on_start_stop)
        QShortcut(QKeySequence("F6"), self, activated=self._on_pause_resume)

        log_filter_row = QHBoxLayout()
        log_filter_row.addWidget(QLabel(tr("Pokaż w logu:")))
        self.log_filter_combo = QComboBox()
        self.log_filter_combo.addItem(tr("Źródłowy i tłumaczenie"), "both")
        self.log_filter_combo.addItem(tr("Tylko źródłowy"), "source")
        self.log_filter_combo.addItem(tr("Tylko tłumaczenie"), "translation")
        self.log_filter_combo.currentIndexChanged.connect(self._rebuild_log)
        log_filter_row.addWidget(self.log_filter_combo)
        log_filter_row.addStretch()
        root.addLayout(log_filter_row)

        # Split across two rows, not one: five buttons with full Polish
        # labels side by side forced the window's minimum width to ~1500px
        # (each button's own minimumSizeHint refuses to shrink below its
        # label), which didn't fit on a smaller laptop screen (confirmed:
        # this and the two device-name combos above were what "the window
        # doesn't fit on my Mac" traced back to). Two shorter rows roughly
        # halves that.
        overlay_row = QHBoxLayout()
        overlay_row.addStretch()
        self.overlay_btn = QPushButton(tr("Odczep okienko z tłumaczeniem"))
        self.overlay_btn.clicked.connect(self._on_toggle_overlay)
        overlay_row.addWidget(self.overlay_btn)
        self.overlay_settings_btn = QPushButton(tr("Ustawienia wyglądu overlay..."))
        self.overlay_settings_btn.clicked.connect(self._on_open_overlay_settings)
        overlay_row.addWidget(self.overlay_settings_btn)
        root.addLayout(overlay_row)

        transcript_row = QHBoxLayout()
        transcript_row.addStretch()
        self.save_transcript_btn = QPushButton(tr("Zapisz transkrypcję..."))
        self.save_transcript_btn.clicked.connect(self._on_save_transcript)
        transcript_row.addWidget(self.save_transcript_btn)
        self.clear_transcript_btn = QPushButton(tr("Wyczyść transkrypcję"))
        self.clear_transcript_btn.clicked.connect(self._on_clear_transcript)
        transcript_row.addWidget(self.clear_transcript_btn)
        self.open_error_log_btn = QPushButton(tr("Otwórz log błędów"))
        self.open_error_log_btn.setToolTip(tr("Otwiera ~/.sts_bridge/errors.log w domyślnym edytorze tekstu."))
        self.open_error_log_btn.clicked.connect(self._on_open_error_log)
        transcript_row.addWidget(self.open_error_log_btn)
        root.addLayout(transcript_row)

        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        # stretch=2 vs. settings_scroll's 1 above -- see its comment for why
        # the ratio favors this getting most of any surplus window height.
        root.addWidget(self.log, stretch=2)

        # A fixed 600x500 (this call's value until now) badly undersized the
        # window once the settings area could scroll internally -- narrower
        # than the settings area's natural width, it forced an awkward
        # horizontal scrollbar in there too.
        #
        # Deliberately NOT self.sizeHint() for the height: measured (by
        # comparing settings_scroll.viewport().size() before/after several
        # different attempts) that root's layout, when it has to negotiate
        # space between a QScrollArea and self.log (stretch=1, and already
        # independently scrollable on its own as a QPlainTextEdit), starves
        # the scroll area well below what it actually needs -- self.
        # sizeHint() reflects THAT negotiated outcome, not the settings
        # area's real need, no matter how much extra padding gets added on
        # top of it. Summing every other top-level row's own sizeHint()
        # directly (each a plain QHBoxLayout, none of them flexible/
        # shrinkable like the scroll area or the log) sidesteps that
        # negotiation entirely: this is "how tall does everything BUT the
        # scroll area and the log need", to which
        # _SETTINGS_SCROLL_DESIRED_HEIGHT and a fixed reasonable log height
        # are added back explicitly.
        chrome_height = (
            settings_row.sizeHint().height()
            + control_row.sizeHint().height()
            + log_filter_row.sizeHint().height()
            + overlay_row.sizeHint().height()
            + transcript_row.sizeHint().height()
        )
        log_reasonable_height = 200
        target_width = self.sizeHint().width() + 20
        target_height = _SETTINGS_SCROLL_DESIRED_HEIGHT + chrome_height + log_reasonable_height + 80
        # Capped against the real screen's available space -- belt-and-
        # suspenders alongside settings_scroll's own internal scrollbar --
        # so a screen smaller than even this size still opens the window
        # fully on-screen instead of spilling off the edge.
        screen = QApplication.primaryScreen()
        if screen is not None:
            available = screen.availableGeometry()
            target_width = min(target_width, available.width() - 40)
            target_height = min(target_height, available.height() - 80)
        self.resize(target_width, target_height)

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
        # subtitles_only_check is deliberately NOT in this list -- like
        # mic_combo, it stays live-toggleable during a running session (see
        # _on_subtitles_only_toggled): muting/unmuting playback is purely
        # local, never touches the server session.
        # file_btn/file_clear_btn are deliberately NOT in this list either --
        # the file can be added, changed, or removed for the whole duration
        # of a session (see _choose_file/_on_clear_file), same live-editable
        # treatment as mic_combo and subtitles_only_check above.
        self._config_widgets = [
            self.settings_btn,
            self.output_combo,
            self.source_lang_combo,
            self.target_lang_combo,
            self.voice_combo,
            self.voice_custom_edit,
            self.manage_voices_btn,
            self.refresh_devices_btn,
            self.manage_glossary_btn,
            self.church_style_check,
        ]

        self._apply_saved_app_settings(config.load_app_settings())

        self._update_checker = UpdateChecker()
        self._update_checker.update_found.connect(self._on_update_found, Qt.ConnectionType.QueuedConnection)
        self._update_checker.start()

    def _on_update_found(self, version: str, url: str) -> None:
        self._update_url = url
        self.update_btn.setText(f"{tr('Dostępna nowa wersja')} v{version} -- {tr('kliknij, aby otworzyć')}")
        self.update_btn.setVisible(True)

    def _on_open_update_url(self) -> None:
        if self._update_url:
            QDesktopServices.openUrl(QUrl(self._update_url))

    def _apply_saved_app_settings(self, settings: dict) -> None:
        """Restores widget selections saved by _save_app_settings() on the
        previous run. Each field is applied independently and only if still
        valid (e.g. a saved device name that's no longer connected, or a
        saved language code that's no longer in the list, is silently
        skipped) -- one stale field must never block the rest from applying.
        """
        mic_name = settings.get("mic_device_name")
        mic_device_found = False
        if mic_name:
            idx = self.mic_combo.findText(mic_name)
            if idx >= 0:
                self.mic_combo.setCurrentIndex(idx)
                mic_device_found = True
        if mic_device_found and "mic_channel" in settings:
            # Only restored when the SAVED device was actually found above
            # -- otherwise mic_combo stayed on whatever device it already
            # defaulted to (e.g. the saved interface is unplugged), and
            # applying a channel meant for a DIFFERENT physical device
            # would silently pick the wrong input on it (e.g. its Kanał 2,
            # which could easily be dead silence) with no indication why.
            #
            # Restored AFTER mic_name above: selecting the device already
            # rebuilt this combo's items for it (see
            # _on_mic_selection_changed -> _populate_mic_channel_combo),
            # resetting to its default item -- this picks the saved channel
            # back out of that freshly-built list, or is silently skipped
            # if the device no longer offers that many channels.
            # A manual scan, not findData(): findData() unreliably fails to
            # match a plain None through Qt's QVariant wrapping (same
            # reasoning as voice_kind's restoration below).
            wanted_channel = settings["mic_channel"]
            for i in range(self.mic_channel_combo.count()):
                if self.mic_channel_combo.itemData(i) == wanted_channel:
                    self.mic_channel_combo.setCurrentIndex(i)
                    break
        output_name = settings.get("output_device_name")
        if output_name:
            idx = self.output_combo.findText(output_name)
            if idx >= 0:
                self.output_combo.setCurrentIndex(idx)
        if "mic_gain" in settings:
            self.mic_gain_slider.setValue(int(settings["mic_gain"]))
        if "mic_muted" in settings:
            self.mic_mute_check.setChecked(bool(settings["mic_muted"]))
        if "mic_gate" in settings:
            self.mic_gate_slider.setValue(int(settings["mic_gate"]))
        if "subtitles_only" in settings:
            self.subtitles_only_check.setChecked(bool(settings["subtitles_only"]))
        if "church_style" in settings:
            self.church_style_check.setChecked(bool(settings["church_style"]))
        if settings.get("advanced_expanded"):
            self.advanced_toggle_btn.setChecked(True)  # triggers _on_advanced_toggled via its toggled signal
        source_lang = settings.get("source_lang")
        if source_lang:
            idx = self.source_lang_combo.findData(source_lang)
            if idx >= 0:
                self.source_lang_combo.setCurrentIndex(idx)
        target_lang = settings.get("target_lang")
        if target_lang:
            idx = self.target_lang_combo.findData(target_lang)
            if idx >= 0:
                self.target_lang_combo.setCurrentIndex(idx)
        if "voice_kind" in settings:
            # Not findData(): QComboBox.findData() unreliably fails to match
            # a tuple containing None (e.g. ("custom", None)) through Qt's
            # QVariant wrapping -- a plain scan over itemData() compares the
            # actual Python tuples directly instead.
            wanted = (settings["voice_kind"], settings.get("voice_id"))
            for i in range(self.voice_combo.count()):
                if self.voice_combo.itemData(i) == wanted:
                    self.voice_combo.setCurrentIndex(i)
                    break
        if "voice_custom_text" in settings:
            self.voice_custom_edit.setText(settings["voice_custom_text"])
        log_filter = settings.get("log_filter")
        if log_filter:
            idx = self.log_filter_combo.findData(log_filter)
            if idx >= 0:
                self.log_filter_combo.setCurrentIndex(idx)

    def _save_app_settings(self) -> None:
        voice_kind, voice_id = self.voice_combo.currentData() if self.voice_combo.count() else ("auto", None)
        config.save_app_settings({
            "mic_device_name": self.mic_combo.currentText(),
            "mic_channel": self.mic_channel_combo.currentData() if self.mic_channel_combo.count() else None,
            "output_device_name": self.output_combo.currentText(),
            "mic_gain": self.mic_gain_slider.value(),
            "mic_muted": self.mic_mute_check.isChecked(),
            "mic_gate": self.mic_gate_slider.value(),
            "subtitles_only": self.subtitles_only_check.isChecked(),
            "church_style": self.church_style_check.isChecked(),
            "advanced_expanded": self.advanced_toggle_btn.isChecked(),
            "source_lang": self.source_lang_combo.currentData(),
            "target_lang": self.target_lang_combo.currentData(),
            "voice_kind": voice_kind,
            "voice_id": voice_id,
            "voice_custom_text": self.voice_custom_edit.text(),
            "log_filter": self.log_filter_combo.currentData(),
            "language": i18n.get_language(),
        })

    def _set_config_enabled(self, enabled: bool) -> None:
        for w in self._config_widgets:
            w.setEnabled(enabled)

    def _on_advanced_toggled(self, expanded: bool) -> None:
        self.advanced_panel.setVisible(expanded)
        arrow = "▾" if expanded else "▸"
        self.advanced_toggle_btn.setText(f"{arrow} {tr('Zaawansowane')}")

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
        self.voice_combo.addItem(tr("Domyślny (auto)"), ("auto", None))
        self.voice_combo.addItem("default_low", ("id", "default_low"))
        self.voice_combo.addItem("default_high", ("id", "default_high"))
        self.voice_combo.addItem(tr("Klonowanie głosu mówcy (eksperymentalne)"), ("clone", None))
        for v in config.load_saved_voices():
            self.voice_combo.addItem(v["name"], ("id", v["voice_id"]))
        self.voice_combo.addItem(tr("Inny (ID z portalu Palabra)..."), ("custom", None))
        if current is not None:
            # Not findData(): unreliably fails to match a tuple containing
            # None (e.g. ("clone", None), ("custom", None)) through Qt's
            # QVariant wrapping -- previously meant selecting "Klonowanie"
            # or "Inny..." and then rebuilding (e.g. via "Zapisane głosy...")
            # silently reset the selection back to "auto" (index 0).
            idx = 0
            for i in range(self.voice_combo.count()):
                if self.voice_combo.itemData(i) == current:
                    idx = i
                    break
            self.voice_combo.setCurrentIndex(idx)
        else:
            # current is None only on this combo's very first-ever build (see
            # above) -- before _apply_saved_app_settings() gets a chance to
            # restore a previously saved choice, so this only ends up being
            # the ACTUAL default on a fresh install (or if voice_kind was
            # never saved). Default to DEFAULT_VOICE_ID instead of leaving
            # Qt's implicit first item ("Domyślny (auto)") selected -- see
            # its definition in config.py for why.
            for i in range(self.voice_combo.count()):
                if self.voice_combo.itemData(i) == ("id", config.DEFAULT_VOICE_ID):
                    self.voice_combo.setCurrentIndex(i)
                    break
        self.voice_combo.blockSignals(False)
        self._on_voice_mode_changed(self.voice_combo.currentIndex())

    def _on_manage_voices(self) -> None:
        SavedVoicesDialog(self).exec()
        self._rebuild_voice_combo()

    def _on_manage_glossary(self) -> None:
        source_lang = self.source_lang_combo.currentData()
        target_lang = self.target_lang_combo.currentData()
        GlossaryDialog(
            source_lang, target_lang,
            self.source_lang_combo.currentText(), self.target_lang_combo.currentText(),
            self,
        ).exec()

    def _on_mic_selection_changed(self, _index: int) -> None:
        # Repopulating the channel combo for the newly selected device must
        # happen regardless of whether a session is running -- so it's
        # correctly populated by the time Start reads it too, not just for
        # live mid-session switches. currentIndexChanged fires again from
        # this (a fresh combo starts at index 0), which reaches
        # _on_mic_channel_changed below and does the actual live switch.
        self._populate_mic_channel_combo()

    def _populate_mic_channel_combo(self) -> None:
        device_index = self.mic_combo.currentData()
        channels = next(
            (d.max_input_channels for d in self._input_devices if d.index == device_index),
            1,
        )
        self.mic_channel_combo.blockSignals(True)
        self.mic_channel_combo.clear()
        # First item always maps to data=None (today's default capture
        # path, untouched -- see MicStream's channel comment), even though
        # it's labeled "Kanał 1": the None/explicit-0 distinction is an
        # internal safety detail the user doesn't need to know about.
        for i in range(max(1, channels)):
            self.mic_channel_combo.addItem(f"{tr('Kanał')} {i + 1}", None if i == 0 else i)
        self.mic_channel_combo.blockSignals(False)
        self.mic_channel_combo.setVisible(channels > 1)
        self._on_mic_channel_changed(self.mic_channel_combo.currentIndex())

    def _on_mic_channel_changed(self, _index: int) -> None:
        # Live device/channel switching mid-session (see
        # SessionWorker.change_mic_device) -- a no-op before Start (no
        # worker yet). The mic is always the active source now, so there's
        # no mode check left to make.
        if self._worker is None:
            return
        device = self.mic_combo.currentData()
        if device is not None:
            self._worker.change_mic_device(device, self.mic_channel_combo.currentData())

    def _choose_file(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            tr("Wybierz plik audio/wideo"),
            "",
            f"{tr('Audio/Video')} (*.wav *.mp3 *.mp4 *.mov *.m4a *.flac *.ogg *.mkv *.avi *.webm *.wmv *.flv *.aac *.ts)"
            f";;{tr('Wszystkie pliki')} (*)",
        )
        if not path:
            return
        try:
            probe_audio_file(path)
        except (ValueError, ImportError) as e:
            QMessageBox.warning(self, tr("Nieprawidłowy plik"), str(e))
            return
        self._selected_file = path
        self.file_label.setText(path)
        self.file_clear_btn.setEnabled(True)
        self.position_slider.setVisible(True)
        self.position_label.setVisible(True)
        self._set_skip_buttons_visible(True)
        self.file_pause_btn.setVisible(True)
        if self._worker is not None:
            # Live add/change mid-session: same "never autoplay" rule as a
            # file selected before Start -- see SessionWorker.set_file() /
            # TranslationRunner._do_set_file, which pauses it before it's
            # ever handed to MixedSource.
            self._file_paused = True
            self.file_pause_btn.setText(tr("Wznów plik"))
            self.file_pause_btn.setEnabled(True)
            self.position_slider.setEnabled(False)
            self._set_skip_buttons_enabled(False)
            self._file_swap_baseline_total_ms = self._worker.total_ms
            self._file_swap_pending_since = time.monotonic()
            self._position_timer.start()
            self._worker.set_file(path)

    def _on_clear_file(self) -> None:
        self._selected_file = None
        self.file_label.setText(tr("(nie wybrano pliku)"))
        self.file_clear_btn.setEnabled(False)
        self.position_slider.setVisible(False)
        self.position_label.setVisible(False)
        self._set_skip_buttons_visible(False)
        self.position_slider.setEnabled(False)
        self._set_skip_buttons_enabled(False)
        self.position_slider.setValue(0)
        self.position_label.setText("00:00 / 00:00")
        self._file_swap_baseline_total_ms = None
        self._file_swap_pending_since = None
        self._position_timer.stop()  # no file left to track -- avoid a stale-total_ms re-enable while hidden
        self.file_pause_btn.setVisible(False)
        self.file_pause_btn.setEnabled(False)
        self._file_paused = False
        self.file_pause_btn.setText(tr("Pauza pliku"))
        if self._worker is not None:
            self._worker.set_file(None)

    def _open_settings(self) -> None:
        SettingsDialog(self).exec()
        self._balance_usd = config.load_balance()  # may have been edited/synced just now

    def _on_refresh_devices(self) -> None:
        rescan_devices()

        current_mic = self.mic_combo.currentData()
        current_mic_channel = self.mic_channel_combo.currentData()
        new_input_devices = list_input_devices()
        if new_input_devices != self._input_devices:
            # Only actually touch the combo (clear + repopulate) when the
            # device list changed -- rebuilding it on every call, even when
            # nothing changed, is wasted work and a needless visual flicker
            # now that this also runs automatically every few seconds (see
            # _auto_refresh_devices), not just on an explicit button click.
            self.mic_combo.clear()
            self._input_devices = new_input_devices
            for d in self._input_devices:
                self.mic_combo.addItem(d.name, d.index)
            idx = self.mic_combo.findData(current_mic)
            self.mic_combo.setCurrentIndex(idx if idx >= 0 else 0)
            # Restoring the device above already rebuilt the channel combo
            # for it (see _on_mic_selection_changed ->
            # _populate_mic_channel_combo), resetting it to the default
            # item -- without this, ANY device-list change (a headset
            # plugged in elsewhere, Steam/Sonar adding a virtual device,
            # this timer firing every 3s -- see _auto_refresh_devices)
            # silently reset an explicitly chosen channel back to "Kanał 1"
            # for the same still-selected device.
            for i in range(self.mic_channel_combo.count()):
                if self.mic_channel_combo.itemData(i) == current_mic_channel:
                    self.mic_channel_combo.setCurrentIndex(i)
                    break

        current_output = self.output_combo.currentData()
        new_output_devices = list_output_devices()
        if new_output_devices != self._output_devices:
            self.output_combo.clear()
            self._output_devices = new_output_devices
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

    def _auto_refresh_devices(self) -> None:
        """Periodic, silent version of _on_refresh_devices() -- picks up a
        mic/headset plugged in or unplugged while idle, without the user
        needing to click "Odśwież urządzenia" or restart the app.

        Guarded to only run when no session is active: rescan_devices()
        itself is only safe to call while no PortAudio stream is open (see
        its own docstring) -- exactly why refresh_devices_btn is already
        disabled during a running session (see _config_widgets). A session
        starting or stopping mid-tick isn't a race worth guarding further:
        the check below runs on the same GUI thread as _on_start_stop, so
        there's no window for self._worker to change between the check and
        rescan_devices() actually running.
        """
        if self._worker is not None:
            return
        self._on_refresh_devices()

    def _on_mic_gain_changed(self, value: int) -> None:
        self.mic_gain_label.setText(f"{value}%")
        if self._worker is not None and not self._mic_muted:
            self._worker.set_mic_gain(value / 100)

    def _on_mic_mute_toggled(self, checked: bool) -> None:
        self._mic_muted = checked
        if self._worker is not None:
            self._worker.set_mic_gain(0.0 if checked else self.mic_gain_slider.value() / 100)

    def _on_mic_gate_changed(self, value: int) -> None:
        self.mic_gate_label.setText(tr("Wyłączony") if value == 0 else f"{value}%")
        if self._worker is not None:
            self._worker.set_gate_threshold(value / 100)

    def _on_test_output(self) -> None:
        if self.output_combo.count() == 0:
            QMessageBox.warning(
                self, tr("Brak urządzenia wyjściowego"), tr("System nie zgłasza żadnego urządzenia audio wyjściowego.")
            )
            return
        try:
            play_test_tone(self.output_combo.currentData())
        except Exception as e:
            QMessageBox.warning(self, tr("Błąd testu wyjścia"), f"{tr('Nie udało się odtworzyć dźwięku testowego')}: {e}")

    def _on_start_stop(self) -> None:
        if self._worker is not None:
            self.start_stop_btn.setEnabled(False)
            self.status_label.setText(tr("Zatrzymywanie..."))
            self._worker.stop()
            return

        creds = config.load_credentials()
        if not creds.api_key:
            QMessageBox.warning(self, tr("Brak klucza API"), tr("Ustaw klucz API w Ustawieniach przed rozpoczęciem."))
            self._open_settings()
            return

        mic_active = True
        needs_file = self._selected_file is not None
        file_path = self._selected_file if needs_file else None
        if needs_file and not file_path:
            QMessageBox.warning(self, tr("Brak pliku"), tr("Wybierz plik audio/wideo do przetłumaczenia."))
            return
        if self.output_combo.count() == 0:
            QMessageBox.critical(
                self, tr("Brak urządzenia wyjściowego"), tr("System nie zgłasza żadnego urządzenia audio wyjściowego.")
            )
            return

        mic_device = self.mic_combo.currentData() if mic_active else None
        output_device = self.output_combo.currentData()
        source_lang = self.source_lang_combo.currentData()
        target_lang = self.target_lang_combo.currentData()

        resolved_voice = self._resolve_selected_voice()
        if resolved_voice is None:
            QMessageBox.warning(
                self, tr("Brak ID głosu"), tr("Podaj ID głosu (z app.palabra.ai/voices) albo wybierz inną opcję.")
            )
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
        self._log_repeat_state = {True: (None, 0), False: (None, 0)}
        self._log_repeat_block = {True: None, False: None}
        worker = SessionWorker(SessionConfig(
            api_key=creds.api_key,
            source_lang=source_lang,
            target_lang=target_lang,
            mic_device=mic_device,
            mic_channel=self.mic_channel_combo.currentData() if self.mic_channel_combo.count() else None,
            output_device=output_device,
            file_path=file_path,
            mic_gain=0.0 if self._mic_muted else self.mic_gain_slider.value() / 100,
            mic_gate_threshold=self.mic_gate_slider.value() / 100,
            voice_id=voice_id,
            voice_cloning=voice_cloning,
            subtitles_only=self.subtitles_only_check.isChecked(),
            church_style=self.church_style_check.isChecked(),
        ))
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
        self.start_stop_btn.setText(tr("Stop"))
        self._set_config_enabled(False)
        self._session_billable_seconds = 0.0
        self._session_running_since = None
        self._session_started_at = datetime.now().isoformat(timespec="seconds")
        self._update_session_display()
        self._level_timer.start()
        if self._selected_file is not None:
            self._file_paused = True
            self.file_pause_btn.setText(tr("Wznów plik"))
            self.file_pause_btn.setEnabled(True)
            self._position_timer.start()
        else:
            self._file_paused = False
            self.file_pause_btn.setEnabled(False)

    def _on_subtitles_only_toggled(self, checked: bool) -> None:
        if self._worker is not None:
            self._worker.set_subtitles_only(checked)

    def _set_status_colors(self, dot_color: str, banner_bg: str) -> None:
        self.status_dot.setStyleSheet(f"background-color: {dot_color}; border-radius: 5px;")
        self.status_frame.setStyleSheet(
            f"#statusFrame {{ background-color: {banner_bg}; border: 1px solid {dot_color}; border-radius: 6px; }}"
        )

    def _on_state(self, state: SessionState) -> None:
        self._pause_request_pending = False
        self._current_session_state = state
        self.status_label.setText(tr(_STATE_LABELS.get(state, str(state))))
        self._set_status_colors(
            _STATUS_DOT_COLORS.get(state, _STATUS_DOT_IDLE_COLOR),
            _STATUS_BANNER_BG_COLORS.get(state, _STATUS_BANNER_IDLE_BG),
        )
        # No per-state text color/weight override here anymore -- an
        # earlier version made RECONNECTING's text red-on-red (this state's
        # own status_frame banner background is already a dark red tint),
        # a real WCAG contrast failure (2.46:1, well under the 4.5:1
        # minimum). The banner itself (background/border/dot, set above)
        # already carries the color signal; the plain default text color
        # reads clearly against every banner tint.
        if state == SessionState.RUNNING:
            if self._session_running_since is None:
                self._session_running_since = time.monotonic()
        elif self._session_running_since is not None:
            # Leaving RUNNING (Pauza, Stop, or an error) -- fold the elapsed
            # stretch into the running total and stop the clock, matching
            # Palabra actually stopping billing on Pauza (see README).
            self._session_billable_seconds += time.monotonic() - self._session_running_since
            self._session_running_since = None
        if state == SessionState.PAUSED:
            self._is_paused = True
            self.pause_btn.setText(tr("Wznów"))
            # Also re-enables it (not just RUNNING does): a reconnect landing
            # while the user had the session paused reports PAUSED directly
            # (see TranslationRunner.run()'s _paused_by_user branch) without
            # ever passing through RUNNING first -- RECONNECTING disables
            # this button, and without re-enabling it here too, it stayed
            # disabled forever with no way left to resume (F6 is guarded by
            # this same isEnabled() check, so it couldn't rescue this either).
            self.pause_btn.setEnabled(True)
        elif state == SessionState.RUNNING:
            self._is_paused = False
            self.pause_btn.setText(tr("Pauza"))
            self.pause_btn.setEnabled(True)
        elif state == SessionState.RECONNECTING:
            # Nothing to pause/resume mid-reconnect -- request_pause() would
            # already no-op safely (self._session is None during the gap),
            # but disabling the button avoids a click that visibly does
            # nothing.
            self.pause_btn.setEnabled(False)
        # Re-enabling controls happens in _on_worker_finished(), NOT here:
        # state_changed(STOPPED/ERROR) is emitted from inside TranslationRunner.run(),
        # while the mic/output device is still being released by the `with` block
        # in SessionWorker.start(). Re-enabling Start on this signal would let the
        # user launch a new session before the old device handle is actually freed.

    def _on_worker_finished(self) -> None:
        # _on_state(STOPPED/ERROR/...) already ran before this (see the
        # comment below) and folded any final RUNNING stretch into
        # _session_billable_seconds, so it's already the correct final
        # total here -- nothing still "in flight" left to add.
        final_seconds = self._session_billable_seconds
        if final_seconds > 0 and self._session_started_at is not None:
            final_cost = _estimated_cost(final_seconds)
            config.append_session_history(self._session_started_at, final_seconds, final_cost)
            if self._balance_usd is not None:
                self._balance_usd -= final_cost
                config.save_balance(self._balance_usd)
        self._update_session_display()  # freeze the main-window label at its final total, not mid-tick
        self._worker = None
        self._thread = None
        self.start_stop_btn.setText(tr("Start"))
        self.start_stop_btn.setEnabled(True)
        self._set_config_enabled(True)
        self._position_timer.stop()
        self._level_timer.stop()
        self.mic_level_bar.setValue(0)
        self.output_level_bar.setValue(0)
        self._is_paused = False
        self.pause_btn.setText(tr("Pauza"))
        self.pause_btn.setEnabled(False)
        self._file_paused = False
        self.file_pause_btn.setText(tr("Pauza pliku"))
        self.file_pause_btn.setEnabled(False)
        self.position_slider.setEnabled(False)
        self._set_skip_buttons_enabled(False)
        self.position_slider.setValue(0)
        self.position_label.setText("00:00 / 00:00")
        # A file swap requested just before the session ended (Stop/error/
        # natural end) could leave these set from _choose_file() with no
        # worker left to ever land it -- same reset _on_clear_file() already
        # does, needed here too so a NEW session started soon after doesn't
        # inherit a stale baseline/timestamp from the previous one (see
        # _update_position()'s use of them).
        self._file_swap_baseline_total_ms = None
        self._file_swap_pending_since = None

    def _on_pause_resume(self) -> None:
        # pause_btn.isEnabled() also covers SessionState.RECONNECTING (see
        # _on_state, which disables it there because request_pause() safely
        # no-ops mid-reconnect with no state_changed signal to ever come
        # back). A mouse click on the disabled button already can't reach
        # here, but the F6 QShortcut fires regardless of widget state --
        # without this check, F6 during a reconnect sets
        # _pause_request_pending without any signal that will ever clear
        # it, silently swallowing every later pause/resume attempt until
        # the next unrelated state change happens to reset it.
        if self._worker is None or self._pause_request_pending or not self.pause_btn.isEnabled():
            return
        self._pause_request_pending = True
        if self._is_paused:
            self._worker.resume()
        else:
            self._worker.pause()

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
            self.file_pause_btn.setText(tr("Wznów plik"))
        else:
            self._worker.resume_file()
            self.file_pause_btn.setText(tr("Pauza pliku"))

    def _on_seek(self) -> None:
        if self._worker is not None:
            self._worker.seek(float(self.position_slider.value()))

    def _on_skip_file(self, offset_seconds: int) -> None:
        if self._worker is None:
            return
        total = self._worker.total_ms
        if total <= 0:
            return  # file not decoded yet
        new_position = max(0.0, min(self._worker.position_ms + offset_seconds * 1000, total))
        self._worker.seek(new_position)
        self.position_slider.setValue(int(new_position))  # instant feedback, not waiting for _update_position's next tick

    def _set_skip_buttons_enabled(self, enabled: bool) -> None:
        for btn in self.skip_buttons:
            btn.setEnabled(enabled)

    def _set_skip_buttons_visible(self, visible: bool) -> None:
        for btn in self.skip_buttons:
            btn.setVisible(visible)

    def _update_position(self) -> None:
        if self._worker is None:
            self._position_timer.stop()
            return
        total = self._worker.total_ms
        if total <= 0:
            return  # file not decoded yet
        if not self.position_slider.isEnabled():
            if (
                self._file_swap_baseline_total_ms is not None
                and total == self._file_swap_baseline_total_ms
                and self._file_swap_pending_since is not None
                and time.monotonic() - self._file_swap_pending_since < FILE_SWAP_TIMEOUT_SECONDS
            ):
                # A file swap/clear was just requested (see _choose_file())
                # but SessionWorker.set_file() is fire-and-forget -- total_ms
                # hasn't changed from what it was right before the request,
                # so the swap hasn't actually landed in TranslationRunner yet
                # (still the OLD file's numbers). Wait for it instead of
                # re-enabling the controls against stale data; the timeout
                # above prevents this from waiting forever if the new file
                # genuinely happens to have the same duration as the old one.
                return
            self._file_swap_baseline_total_ms = None
            self._file_swap_pending_since = None
            self.position_slider.setEnabled(True)
            self._set_skip_buttons_enabled(True)
            self.position_slider.setRange(0, int(total))
        if not self.position_slider.isSliderDown():
            self.position_slider.setValue(int(self._worker.position_ms))
        self.position_label.setText(f"{_fmt_ms(self._worker.position_ms)} / {_fmt_ms(total)}")

    def _update_level_meter(self) -> None:
        if self._worker is None:
            self._level_timer.stop()
            self.mic_level_bar.setValue(0)
            self.output_level_bar.setValue(0)
            return
        self.mic_level_bar.setValue(int(self._worker.mic_level * 100))
        self.output_level_bar.setValue(int(self._worker.output_level * 100))

    def _update_session_display(self) -> None:
        if self._worker is None:
            return
        total_seconds = self._session_billable_seconds
        if self._session_running_since is not None:
            total_seconds += time.monotonic() - self._session_running_since
        minutes, seconds = divmod(int(total_seconds), 60)
        cost = _estimated_cost(total_seconds)
        text = f"{minutes:02d}:{seconds:02d} (~${cost:.2f})"
        if self._balance_usd is not None:
            text += f" | {tr('saldo')} ~${self._balance_usd - cost:.2f}"
        self.session_time_label.setText(text)

    def _on_transcript(self, event: TranscriptEvent) -> None:
        # Kept so a newly-opened overlay can be backfilled (see _open_overlay)
        # instead of starting empty if it's opened mid-session, and so the log
        # filter/tag toggles can retroactively re-render already-shown lines
        # (see _rebuild_log). Capped (see MAX_TRANSCRIPT_HISTORY_ENTRIES)
        # since a long session could otherwise accumulate an unbounded list.
        self._transcript_history.append(event)
        del self._transcript_history[:-MAX_TRANSCRIPT_HISTORY_ENTRIES]
        if self._overlay is not None:
            self._overlay.on_transcript(event)  # overlay applies its own, independent filter
        if not self._event_passes_log_filter(event):
            return  # filtered out entirely -- doesn't touch the log or the partial-line bookkeeping
        self._place_log_line(event)

    def _place_log_line(self, event: TranscriptEvent) -> None:
        """Appends/replaces the log's display for one already-filtered event,
        collapsing a finalized line that repeats the immediately preceding
        finalized line of the same kind (source vs. translation) into a
        single "text xN" line instead of one line per repeat. Shared by
        _on_transcript (live) and _rebuild_log (replay) so both stay in sync.
        """
        kind = event.is_translation
        if event.is_final:
            normalized = event.text.strip()
            prev_text, prev_count = self._log_repeat_state[kind]
            if normalized and normalized == prev_text and self._log_repeat_block[kind] is not None:
                count = prev_count + 1
                if self._partial_line_active:
                    # This segment's own partial growth already created/grew
                    # its own line as the widget's current last line -- it's
                    # redundant now, delete it instead of leaving two lines.
                    self._remove_last_log_block()
                # Select exactly this block's own text, NOT via
                # BlockUnderCursor -- on any block that isn't the document's
                # last one, that selection also eats the trailing paragraph
                # separator, so inserting plain text over it would silently
                # merge this line with whatever line currently follows it.
                # Explicit position math (block.length() includes that
                # separator, so -1 excludes it) selects only the text.
                block = self._log_repeat_block[kind]
                cursor = QTextCursor(block)
                cursor.setPosition(block.position())
                cursor.setPosition(block.position() + block.length() - 1, QTextCursor.MoveMode.KeepAnchor)
                cursor.insertText(self._format_log_line(event, suffix_count=count))
                self._log_repeat_state[kind] = (normalized, count)
                self._partial_line_active = False
                return
        text = self._format_log_line(event)
        if self._partial_line_active:
            self._replace_last_log_line(text)
        else:
            self._append_log_line(text)
        # A growing (non-final) line keeps getting replaced in place; once final,
        # the NEXT event (e.g. the translation) must start its own new line.
        self._partial_line_active = not event.is_final
        if event.is_final:
            normalized = event.text.strip()
            self._log_repeat_state[kind] = (normalized, 1)
            self._log_repeat_block[kind] = self.log.document().lastBlock()

    def _remove_last_log_block(self) -> None:
        # BlockUnderCursor on the document's LAST block (this is always
        # called right after moving to End, so it always is) already
        # includes the paragraph separator BEFORE it in the selection --
        # confirmed directly (selectedText() starts with U+2029). A
        # trailing cursor.deletePreviousChar() here used to assume that
        # separator still needed removing separately (true for a block
        # that ISN'T the last one, see _place_log_line's own comment on
        # BlockUnderCursor -- but that's the opposite end), so it deleted
        # one character too many: the last character of whatever
        # unrelated line preceded this one. Confirmed as a real,
        # reproducible corruption of both the live log and (since
        # _on_save_transcript() saves log.toPlainText() verbatim) any
        # saved transcript.
        cursor = self.log.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        cursor.select(QTextCursor.SelectionType.BlockUnderCursor)
        cursor.removeSelectedText()

    def _event_passes_log_filter(self, event: TranscriptEvent) -> bool:
        filter_mode = self.log_filter_combo.currentData()
        if filter_mode == "source" and event.is_translation:
            return False
        if filter_mode == "translation" and not event.is_translation:
            return False
        return True

    def _format_log_line(self, event: TranscriptEvent, suffix_count: int = 1) -> str:
        kind = "→" if event.is_translation else "•"
        suffix = "" if event.is_final else " …"
        if suffix_count >= 2:
            suffix = f" x{suffix_count}"
        return f"{kind} {event.text}{suffix}"

    def _rebuild_log(self) -> None:
        # Re-renders the whole log from history under the current filter, so
        # toggling it re-filters what's already on screen instead of only
        # affecting transcripts that arrive afterward.
        self.log.clear()
        self._partial_line_active = False
        self._log_repeat_state = {True: (None, 0), False: (None, 0)}
        self._log_repeat_block = {True: None, False: None}
        for event in self._transcript_history:
            if not self._event_passes_log_filter(event):
                continue
            self._place_log_line(event)

    def _on_save_transcript(self) -> None:
        content = self.log.toPlainText()
        if not content.strip():
            QMessageBox.information(self, tr("Brak transkrypcji"), tr("Nie ma jeszcze żadnej transkrypcji do zapisania."))
            return
        path, _ = QFileDialog.getSaveFileName(
            self,
            tr("Zapisz transkrypcję"),
            tr("transkrypcja.txt"),
            f"{tr('Pliki tekstowe')} (*.txt);;{tr('Wszystkie pliki')} (*)",
        )
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(content)
        except OSError as e:
            QMessageBox.critical(self, tr("Błąd zapisu"), f"{tr('Nie udało się zapisać pliku')}: {e}")

    def _on_clear_transcript(self) -> None:
        self.log.clear()
        self._transcript_history = []
        self._partial_line_active = False
        self._log_repeat_state = {True: (None, 0), False: (None, 0)}
        self._log_repeat_block = {True: None, False: None}
        if self._overlay is not None:
            self._overlay.clear()

    def _on_open_error_log(self) -> None:
        if not config.ERROR_LOG_PATH.exists():
            QMessageBox.information(
                self, tr("Brak błędów"), tr("Jeszcze żaden błąd nie został zapisany -- plik errors.log nie istnieje.")
            )
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(config.ERROR_LOG_PATH)))

    def _replace_last_log_line(self, text: str) -> None:
        cursor = self.log.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        cursor.select(QTextCursor.SelectionType.LineUnderCursor)
        cursor.insertText(text)

    def _on_error(self, message: str) -> None:
        self._pause_request_pending = False  # a failed pause/resume/seek won't produce a state change
        self._partial_line_active = False  # don't let a later partial transcript overwrite this line
        self._log_repeat_state = {True: (None, 0), False: (None, 0)}
        self._log_repeat_block = {True: None, False: None}
        # A reconnect-attempt announcement (TranslationRunner.run() always
        # fires on_state(RECONNECTING) immediately before this) gets a red
        # line in the main window's log, matching status_label's own color
        # while reconnecting -- never on the overlay, which never receives
        # error messages at all (only on_transcript). Anything else (a real
        # error, a warning, ...) stays the log's normal color.
        color = theme.DANGER if self._current_session_state == SessionState.RECONNECTING else None
        self._append_log_line(message, color)
        config.log_error(message)

    def _append_log_line(self, text: str, color: str | None = None) -> None:
        # The ONE place that appends a new line to self.log -- used by both
        # _place_log_line() (transcripts) and _on_error() (including the red
        # reconnect announcements), instead of QPlainTextEdit.appendPlainText()
        # directly: that method's format inheritance for whatever gets
        # appended NEXT turned out to be inconsistent in practice (confirmed
        # by testing) -- sometimes a previously-colored line bled its color
        # into the next one appended, sometimes not, seemingly depending on
        # what ran in between. Every insertion here states its own format
        # explicitly (None -> the widget's normal/default color) so a
        # colored line can never leak into whatever follows it.
        #
        # Uses its OWN QTextCursor, entirely separate from self.log's own
        # visible cursor/selection -- never calls setTextCursor() the way an
        # earlier version of this did. That earlier version broke scrolling
        # and text selection during a live session: every new line (arriving
        # every 1-2s) forced the view to jump to the bottom and wiped
        # whatever the user had selected, mid-copy. appendPlainText() itself
        # never did that; matching it here means only auto-scrolling when
        # the user was ALREADY at the bottom (so a live session still
        # follows along by default), and leaving their view/selection alone
        # otherwise.
        scrollbar = self.log.verticalScrollBar()
        was_at_bottom = scrollbar.value() >= scrollbar.maximum() - 2
        cursor = QTextCursor(self.log.document())
        cursor.movePosition(QTextCursor.MoveOperation.End)
        if not self.log.document().isEmpty():
            cursor.insertBlock()
        fmt = QTextCharFormat()
        if color is not None:
            fmt.setForeground(QColor(color))
        cursor.insertText(text, fmt)
        if was_at_bottom:
            scrollbar.setValue(scrollbar.maximum())

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
        self.overlay_btn.setText(tr("Zamknij okienko z tłumaczeniem"))

    def _on_overlay_closed(self) -> None:
        self._overlay = None
        self.overlay_btn.setText(tr("Odczep okienko z tłumaczeniem"))

    def _on_open_overlay_settings(self) -> None:
        # Opens the overlay on demand: settings need a live window for preview,
        # and this also means appearance can be adjusted (and is saved) even if
        # you haven't explicitly "detached" it yet.
        if self._overlay is None:
            self._open_overlay()
        self._overlay.open_settings_dialog(self)

    def keyPressEvent(self, event) -> None:
        # A plain letter, unlike F5/F6 (see their own QShortcut comment
        # above): a QShortcut for a bare letter fires regardless of which
        # child widget has focus, stealing keystrokes from text-entry
        # widgets like voice_custom_edit. Overriding keyPressEvent instead
        # relies on normal Qt event propagation -- a focused QLineEdit
        # consumes the key itself for typing, so this only ever sees "M"
        # when no text field is being typed into.
        if event.key() == Qt.Key.Key_M and not event.modifiers():
            self.mic_mute_check.toggle()
            return
        super().keyPressEvent(event)

    def closeEvent(self, event) -> None:
        if self._worker is not None and self._thread is not None:
            thread = self._thread
            worker = self._worker
            self.status_label.setText(tr("Zamykanie — kończę sesję..."))
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
                    tr("Zamykanie trwa dłużej niż zwykle"),
                    tr(
                        "Kończenie sesji (np. wolne połączenie) nie zdążyło się zakończyć.\n\n"
                        "Zamknięcie teraz może zostawić mikrofon/słuchawki zajęte, dopóki "
                        "aplikacja nie dokończy zwalniania urządzenia w tle — sprawdź Menedżera "
                        "zadań (python.exe), jeśli dźwięk przestanie działać.\n\n"
                        "Zamknąć mimo to?"
                    ),
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    QMessageBox.StandardButton.No,
                )
                if answer == QMessageBox.StandardButton.No:
                    self.status_label.setText(tr("Zatrzymywanie..."))
                    event.ignore()
                    return
        self._save_app_settings()
        if self._overlay is not None:
            self._overlay.close()
        super().closeEvent(event)
