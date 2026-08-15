"""Detachable, stylable overlay window showing live transcripts/translation —
meant to be captured by OBS as a Window Capture source, with a background that
can be made fully transparent so only the text shows over the video.
"""

from __future__ import annotations

from PySide6.QtCore import QPoint, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QGuiApplication
from PySide6.QtWidgets import (
    QCheckBox,
    QColorDialog,
    QComboBox,
    QDialog,
    QFontComboBox,
    QFormLayout,
    QGraphicsDropShadowEffect,
    QHBoxLayout,
    QLabel,
    QMenu,
    QPlainTextEdit,
    QPushButton,
    QSizeGrip,
    QSlider,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from . import config
from .translation_session import TranscriptEvent

MAX_LINES = 4  # keeps this a compact "recent captions" strip, not a full scrollback
PARAGRAPH_GAP_SECONDS = 2.5  # pause between sentences long enough to start a new line
MAX_PARAGRAPH_CHARS = 400  # merging still stops here even if pauses stay short --
# otherwise a speaker who never pauses long enough would grow one line without
# bound for as long as the session runs (found via a simulated 2h stress test)
SAMPLE_TEXT = "To jest przykładowy tekst — tak będzie wyglądać napis."

DEFAULT_SETTINGS = {
    "filter_mode": "translation",
    "font_family": "Arial",
    "font_size": 28,
    "font_color": "#FFFFFF",
    "bg_color": "#000000",
    "opacity_percent": 60,
    "shadow_enabled": False,
    "always_on_top": True,
}


def _swatch_style(color: QColor) -> str:
    return f"background-color: {color.name()}; border: 1px solid #888;"


class OverlayWindow(QWidget):
    """Frameless, click-through-transparent-capable window. Right-click for settings."""

    closed = Signal()

    def __init__(self):
        saved = {**DEFAULT_SETTINGS, **config.load_overlay_settings()}

        flags = Qt.WindowType.Window | Qt.WindowType.FramelessWindowHint
        if saved["always_on_top"]:
            flags |= Qt.WindowType.WindowStaysOnTopHint
        super().__init__(None, flags)

        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        self.setWindowTitle("Podgląd tłumaczenia")
        self.setMinimumSize(0, 0)  # let the resize grip shrink freely regardless of content

        self._filter_mode = saved["filter_mode"]
        self._font_color = QColor(saved["font_color"])
        self._bg_color = QColor(saved["bg_color"])
        self._opacity_percent = saved["opacity_percent"]
        self._font = QFont(saved["font_family"], saved["font_size"])
        self._shadow_enabled = False  # applied for real below, once text_edit exists
        self._drag_offset: QPoint | None = None
        self._showing_sample = False

        # Debounced so a drag/resize gesture (many move/resize events per
        # second) doesn't hit disk on every single one -- only once it settles.
        self._geometry_save_timer = QTimer(self)
        self._geometry_save_timer.setSingleShot(True)
        self._geometry_save_timer.setInterval(400)
        self._geometry_save_timer.timeout.connect(self._save_settings)

        self.resize(saved.get("width", 700), saved.get("height", 140))
        if "pos_x" in saved and "pos_y" in saved:
            self.move(saved["pos_x"], saved["pos_y"])
            self._clamp_to_screen()

        # No QLayout here, deliberately: a managed layout kept re-deriving the
        # window's size/minimum size from the content on every text change --
        # it could no longer be shrunk below whatever height the current text
        # needed, and even grew the window back open on every new line
        # (visible on Windows as a "QWindowsWindow::setGeometry: Unable to
        # set geometry..." warning). Instead, this is a plain child widget
        # manually kept the size of the window (see resizeEvent) -- exactly
        # like the resize grip below, which was never in a layout either.
        # This makes it structurally impossible for a text change to affect
        # the window's size: only resizeEvent (i.e. the grip, or a
        # saved-geometry restore) ever touches it.
        #
        # A scrollable QPlainTextEdit, not a QLabel: QLabel's AlignBottom only
        # actually bottom-anchors when the content is SMALLER than the widget.
        # Once wrapped content overflows the widget (a small window, or a lot
        # of text), Qt just clips from the bottom and pins the OLDEST text at
        # the top -- the newest, most relevant line disappears instead of
        # staying visible. Scrolling to the bottom explicitly (see _render()
        # and resizeEvent) is a mechanism we control directly, so the newest
        # line is always what's shown regardless of how much text there is or
        # how the window gets resized.
        self.caption_view = QPlainTextEdit(self)
        self.caption_view.setReadOnly(True)
        self.caption_view.setFrameStyle(0)
        self.caption_view.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.caption_view.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.caption_view.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
        # Display-only: mouse events pass through to the window for drag-to-move.
        self.caption_view.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.caption_view.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.caption_view.setGeometry(self.rect())

        self._grip = QSizeGrip(self)
        self._lines: list[str] = []
        # Parallel to _lines (kept in lockstep, including trimming) so a
        # repeated sentence can be collapsed into the RIGHT line even when
        # the two kinds (source/translation) interleave with the overlay
        # filter set to "both" -- the last-appended line isn't necessarily
        # this kind's line. None marks the placeholder sample line.
        self._line_kinds: list[bool | None] = []
        self._partial_line_active = False
        # Merges consecutive sentences into the same visual line when they
        # follow each other closely (continuing the same thought) instead of
        # always starting a new line per finalized segment -- a new line only
        # starts after a long-enough pause (a genuinely new thought). See
        # on_transcript() for how these are used together.
        self._paragraph_base = ""  # finalized text of the line currently being built
        self._last_final_time: float | None = None
        self._last_final_is_translation: bool | None = None
        self._merging = False  # locked in per-sentence so a slow-to-finalize
        # sentence's growing text doesn't visually "un-merge" mid-way through
        # Collapses a finalized sentence that repeats the immediately
        # preceding finalized sentence of the same kind into a trailing
        # "xN" counter instead of re-appending/re-merging the same text --
        # see on_transcript()'s repeat check, which runs before (and takes
        # priority over) the paragraph-merge logic above. _repeat_prefix
        # holds whatever paragraph text came before that repeating sentence
        # (e.g. "Witam wszystkich." if it was merged onto an earlier
        # sentence, "" if it started its own line) so a repeat can rebuild
        # the correct display text (prefix + sentence + counter) even after
        # the merge-preview logic above has already overwritten the line
        # with a live-growing (and, for a repeat, wrong) partial preview.
        self._repeat_state: dict[bool, tuple[str | None, int]] = {True: (None, 0), False: (None, 0)}
        self._repeat_prefix: dict[bool, str] = {True: "", False: ""}

        self._apply_style()
        if saved["shadow_enabled"]:
            self.set_shadow_enabled(True)

    # --- live style setters -------------------------------------------------
    # Each setter persists the full settings blob immediately (see
    # _save_settings) so the overlay looks the same next time it's opened,
    # including across app restarts -- no separate "Save" step.

    def set_font(self, family: str, size: int) -> None:
        self._font = QFont(family, size)
        self._apply_style()
        self._save_settings()

    def set_font_color(self, color: QColor) -> None:
        self._font_color = color
        self._apply_style()
        self._save_settings()

    def set_background_color(self, color: QColor) -> None:
        self._bg_color = color
        self._apply_style()
        self._save_settings()

    def set_opacity_percent(self, percent: int) -> None:
        self._opacity_percent = max(0, min(100, percent))
        self._apply_style()
        self._save_settings()

    def set_always_on_top(self, enabled: bool) -> None:
        flags = self.windowFlags()
        if enabled:
            flags |= Qt.WindowType.WindowStaysOnTopHint
        else:
            flags &= ~Qt.WindowType.WindowStaysOnTopHint
        was_visible = self.isVisible()
        self.setWindowFlags(flags)
        if was_visible:
            self.show()
        self._save_settings()

    def set_filter_mode(self, mode: str) -> None:
        self._filter_mode = mode
        self._save_settings()

    def _save_settings(self) -> None:
        config.save_overlay_settings(
            {
                "filter_mode": self._filter_mode,
                "font_family": self._font.family(),
                "font_size": self._font.pointSize(),
                "font_color": self._font_color.name(),
                "bg_color": self._bg_color.name(),
                "opacity_percent": self._opacity_percent,
                "shadow_enabled": self._shadow_enabled,
                "always_on_top": bool(self.windowFlags() & Qt.WindowType.WindowStaysOnTopHint),
                "pos_x": self.x(),
                "pos_y": self.y(),
                "width": self.width(),
                "height": self.height(),
            }
        )

    def _clamp_to_screen(self) -> None:
        """Falls back to a safe default position if the saved one is now
        off-screen (e.g. a second monitor was disconnected since it was
        saved) -- otherwise the overlay would reopen somewhere unreachable."""
        geo = self.geometry()
        if not any(screen.geometry().intersects(geo) for screen in QGuiApplication.screens()):
            self.move(100, 100)

    def set_shadow_enabled(self, enabled: bool) -> None:
        self._shadow_enabled = enabled
        if enabled:
            effect = QGraphicsDropShadowEffect(self.caption_view)
            effect.setBlurRadius(8)
            effect.setOffset(2, 2)
            effect.setColor(QColor(0, 0, 0, 220))
            self.caption_view.setGraphicsEffect(effect)
        else:
            self.caption_view.setGraphicsEffect(None)
        self._save_settings()

    def show_sample_text(self) -> None:
        """Fills the overlay with placeholder text so style changes are visible
        even with no live session running. A real transcript naturally replaces
        it once one arrives.

        No-ops if there's already real content on screen: opening the settings
        panel mid-session would otherwise overwrite the last real translation
        with the sample, and then wipe it to blank on close if no new
        transcript happened to arrive in the meantime -- losing content the
        user could actually see a moment ago. Real content already
        demonstrates the style live just as well as the sample would.
        """
        if self._lines:
            return
        self._lines = [SAMPLE_TEXT]
        self._line_kinds = [None]
        self._partial_line_active = False
        self._showing_sample = True
        self._render()

    def clear_sample(self) -> None:
        """Removes the placeholder text once appearance settings are closed,
        but only if no real transcript has arrived to replace it in the meantime."""
        if self._showing_sample:
            self._lines = []
            self._line_kinds = []
            self._partial_line_active = False
            self._showing_sample = False
            self._paragraph_base = ""
            self._last_final_time = None
            self._last_final_is_translation = None
            self._repeat_state = {True: (None, 0), False: (None, 0)}
            self._repeat_prefix = {True: "", False: ""}
            self._render()

    def _apply_style(self) -> None:
        alpha = round(self._opacity_percent / 100 * 255)
        bg = self._bg_color
        fg = self._font_color
        self.caption_view.setStyleSheet(
            f"""
            QPlainTextEdit {{
                background-color: rgba({bg.red()}, {bg.green()}, {bg.blue()}, {alpha});
                color: rgba({fg.red()}, {fg.green()}, {fg.blue()}, 255);
                border: none;
                padding: 6px;
            }}
            """
        )
        self.caption_view.setFont(self._font)

    # --- content --------------------------------------------------------

    def on_transcript(self, event: TranscriptEvent) -> None:
        if (self._filter_mode == "source" and event.is_translation) or (
            self._filter_mode == "translation" and not event.is_translation
        ):
            return
        if self._showing_sample:
            self._lines = []
            self._line_kinds = []
            self._showing_sample = False
            self._paragraph_base = ""
            self._last_final_time = None
            self._last_final_is_translation = None
            self._repeat_state = {True: (None, 0), False: (None, 0)}
            self._repeat_prefix = {True: "", False: ""}

        # Repeat detection takes priority over (runs before) paragraph
        # merging below: a repeated sentence must never get re-appended or
        # re-merged into the text, it only bumps a trailing "xN" counter on
        # whatever is currently shown. Only finalized sentences are compared
        # (mirrors the same "finals only" precedent used elsewhere). Checked
        # on EVERY final, even one sealing an already-growing partial: a
        # repeated sentence very often arrives as a growing partial (live
        # ASR builds it up word by word) that the merge-preview logic below
        # will have already merged into the display as a live guess -- by
        # the time it finalizes as a confirmed repeat, that preview must be
        # overwritten with the correct prefix+sentence+counter text, not
        # left as whatever the in-progress guess looked like.
        if event.is_final:
            normalized = event.text.strip()
            prev_text, prev_count = self._repeat_state[event.is_translation]
            if normalized and normalized == prev_text:
                # Find THIS kind's own line, not necessarily the last one --
                # with the overlay filter set to "both", source/translation
                # lines interleave, so the widget's last line can belong to
                # the other kind.
                idx = next(
                    (i for i in range(len(self._lines) - 1, -1, -1) if self._line_kinds[i] == event.is_translation),
                    None,
                )
                if idx is not None:
                    count = prev_count + 1
                    self._repeat_state[event.is_translation] = (normalized, count)
                    prefix = self._repeat_prefix[event.is_translation]
                    sep = " " if prefix else ""
                    self._lines[idx] = f"{prefix}{sep}{normalized} x{count}"
                    if idx == len(self._lines) - 1:
                        # Only keep the paragraph-merge bookkeeping in sync
                        # when the repeat landed on the actual last line --
                        # otherwise this repeat is on an earlier, different-
                        # kind line and must not affect what the *next*
                        # sentence merges onto.
                        self._paragraph_base = self._lines[idx]
                        self._last_final_time = event.timestamp
                        self._last_final_is_translation = event.is_translation
                    self._partial_line_active = False
                    self._render()
                    return

        # The event's own creation time, not time.monotonic() captured here:
        # replaying history (e.g. backfilling an overlay opened mid-session)
        # calls this in a tight loop with no real delay between calls, which
        # would otherwise make unrelated sentences spoken minutes apart look
        # like they happened back-to-back and get wrongly merged.
        now = event.timestamp
        # Whether this event is the first fragment of a new sentence (as
        # opposed to a further-along partial repeat of one already growing).
        # The merge/no-merge decision below is made only at that point and
        # then reused for every subsequent partial of the same sentence, so a
        # slow-to-finalize sentence doesn't visually "split off" mid-growth
        # once enough time has passed since the *previous* sentence ended.
        starting_new_sentence = not self._partial_line_active
        if starting_new_sentence:
            self._merging = (
                bool(self._lines)
                and self._last_final_time is not None
                # Never merge a source transcript onto the same line as a
                # translation (or vice versa): with the overlay filter set to
                # "both", the two normally arrive close together in time
                # (well under PARAGRAPH_GAP_SECONDS), which without this
                # check concatenated the two languages onto one caption line.
                and self._last_final_is_translation == event.is_translation
                and (now - self._last_final_time) < PARAGRAPH_GAP_SECONDS
                and len(self._paragraph_base) < MAX_PARAGRAPH_CHARS
            )

        # Captured before _paragraph_base is overwritten below: whatever
        # paragraph text this sentence is merging onto (or "" if it's
        # starting its own line) -- see _repeat_prefix's declaration.
        prefix_before = self._paragraph_base if self._merging else ""
        text = f"{self._paragraph_base} {event.text}" if self._merging else event.text
        if starting_new_sentence and not self._merging:
            self._lines.append(text)
            self._line_kinds.append(event.is_translation)
        else:
            self._lines[-1] = text
            # _line_kinds[-1] is already the right kind: merging only ever
            # happens onto a same-kind line (see the merge-eligibility check
            # above), and a growing partial never changes kind mid-segment.

        if event.is_final:
            self._paragraph_base = text
            self._last_final_time = now
            self._last_final_is_translation = event.is_translation
            self._repeat_state[event.is_translation] = (event.text.strip(), 1)
            self._repeat_prefix[event.is_translation] = prefix_before
        self._partial_line_active = not event.is_final
        del self._lines[:-MAX_LINES]
        del self._line_kinds[:-MAX_LINES]
        self._render()

    def _render(self) -> None:
        self.caption_view.setPlainText("\n".join(self._lines))
        self._scroll_to_bottom()

    def _scroll_to_bottom(self) -> None:
        bar = self.caption_view.verticalScrollBar()
        bar.setValue(bar.maximum())

    def clear(self) -> None:
        self._lines = []
        self._line_kinds = []
        self._partial_line_active = False
        self._showing_sample = False
        self._paragraph_base = ""
        self._last_final_time = None
        self._last_final_is_translation = None
        self._repeat_state = {True: (None, 0), False: (None, 0)}
        self._repeat_prefix = {True: "", False: ""}
        self._render()

    # --- window chrome: drag-to-move + right-click settings --------------

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_offset = event.globalPosition().toPoint() - self.pos()

    def mouseMoveEvent(self, event) -> None:
        if self._drag_offset is not None and event.buttons() & Qt.MouseButton.LeftButton:
            self.move(event.globalPosition().toPoint() - self._drag_offset)

    def mouseReleaseEvent(self, event) -> None:
        self._drag_offset = None

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self.caption_view.setGeometry(self.rect())
        # Resizing changes how much of the text fits, so the scroll position
        # needs re-pinning to the bottom on every resize step too -- otherwise
        # shrinking the window could leave an older line in view instead of
        # the newest one.
        self._scroll_to_bottom()
        grip_size = self._grip.sizeHint()
        self._grip.move(self.width() - grip_size.width(), self.height() - grip_size.height())
        self._geometry_save_timer.start()

    def moveEvent(self, event) -> None:
        super().moveEvent(event)
        self._geometry_save_timer.start()

    def contextMenuEvent(self, event) -> None:
        menu = QMenu(self)
        settings_action = menu.addAction("Ustawienia wyglądu...")
        close_action = menu.addAction("Zamknij okienko")
        chosen = menu.exec(event.globalPos())
        if chosen == settings_action:
            dialog = OverlaySettingsDialog(self, self)
            dialog.show()
        elif chosen == close_action:
            self.close()

    def closeEvent(self, event) -> None:
        self.closed.emit()
        super().closeEvent(event)


class OverlaySettingsDialog(QDialog):
    """Non-modal, live-preview styling panel for an OverlayWindow."""

    def __init__(self, overlay: OverlayWindow, parent=None):
        super().__init__(parent)
        self._overlay = overlay
        self.setWindowTitle("Ustawienia wyglądu")
        # This dialog isn't always parented to the overlay (MainWindow's
        # "Ustawienia wyglądu overlay..." button parents it to itself instead,
        # so the panel doesn't vanish behind the main window). Without this,
        # closing the overlay while the dialog stays open leaves it holding a
        # dead C++ object -- any further interaction (or even just closing the
        # dialog, which calls clear_sample()) then crashes with "Internal C++
        # object already deleted". Closing here happens synchronously inside
        # the overlay's own closeEvent, before the delayed WA_DeleteOnClose
        # deletion actually runs, so self._overlay is still valid at that point.
        overlay.closed.connect(self.close)
        overlay.show_sample_text()  # visible preview while customizing, even with no live session

        form = QFormLayout()

        self.filter_combo = QComboBox()
        self.filter_combo.addItem("Źródłowy i tłumaczenie", "both")
        self.filter_combo.addItem("Tylko źródłowy", "source")
        self.filter_combo.addItem("Tylko tłumaczenie", "translation")
        self.filter_combo.setCurrentIndex(self.filter_combo.findData(overlay._filter_mode))
        self.filter_combo.currentIndexChanged.connect(
            lambda: overlay.set_filter_mode(self.filter_combo.currentData())
        )
        form.addRow("Pokaż:", self.filter_combo)

        self.font_combo = QFontComboBox()
        self.font_combo.setCurrentFont(overlay._font)
        self.size_spin = QSpinBox()
        self.size_spin.setRange(8, 200)
        self.size_spin.setValue(overlay._font.pointSize())
        font_row = QHBoxLayout()
        font_row.addWidget(self.font_combo, stretch=1)
        font_row.addWidget(self.size_spin)
        self.font_combo.currentFontChanged.connect(self._on_font_changed)
        self.size_spin.valueChanged.connect(self._on_font_changed)
        form.addRow("Czcionka:", font_row)

        self.font_color_btn = QPushButton()
        self.font_color_btn.setFixedWidth(60)
        self.font_color_btn.setStyleSheet(_swatch_style(overlay._font_color))
        self.font_color_btn.clicked.connect(self._pick_font_color)
        form.addRow("Kolor tekstu:", self.font_color_btn)

        self.bg_color_btn = QPushButton()
        self.bg_color_btn.setFixedWidth(60)
        self.bg_color_btn.setStyleSheet(_swatch_style(overlay._bg_color))
        self.bg_color_btn.clicked.connect(self._pick_bg_color)
        form.addRow("Kolor tła:", self.bg_color_btn)

        self.opacity_slider = QSlider(Qt.Orientation.Horizontal)
        self.opacity_slider.setRange(0, 100)
        self.opacity_slider.setValue(overlay._opacity_percent)
        self.opacity_label = QLabel(f"{overlay._opacity_percent}%")
        self.opacity_slider.valueChanged.connect(self._on_opacity_changed)
        opacity_row = QHBoxLayout()
        opacity_row.addWidget(self.opacity_slider, stretch=1)
        opacity_row.addWidget(self.opacity_label)
        form.addRow("Nieprzezroczystość tła:", opacity_row)

        self.shadow_check = QCheckBox("Cień pod tekstem")
        self.shadow_check.setChecked(overlay._shadow_enabled)
        self.shadow_check.toggled.connect(overlay.set_shadow_enabled)
        form.addRow("", self.shadow_check)

        self.always_on_top_check = QCheckBox("Zawsze na wierzchu")
        self.always_on_top_check.setChecked(
            bool(overlay.windowFlags() & Qt.WindowType.WindowStaysOnTopHint)
        )
        self.always_on_top_check.toggled.connect(overlay.set_always_on_top)
        form.addRow("", self.always_on_top_check)

        close_btn = QPushButton("Zamknij")
        close_btn.clicked.connect(self.close)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(close_btn)

    def _on_font_changed(self, *_args) -> None:
        self._overlay.set_font(self.font_combo.currentFont().family(), self.size_spin.value())

    def _pick_font_color(self) -> None:
        color = QColorDialog.getColor(self._overlay._font_color, self, "Kolor tekstu")
        if color.isValid():
            self._overlay.set_font_color(color)
            self.font_color_btn.setStyleSheet(_swatch_style(color))

    def _pick_bg_color(self) -> None:
        color = QColorDialog.getColor(self._overlay._bg_color, self, "Kolor tła")
        if color.isValid():
            self._overlay.set_background_color(color)
            self.bg_color_btn.setStyleSheet(_swatch_style(color))

    def _on_opacity_changed(self, value: int) -> None:
        self._overlay.set_opacity_percent(value)
        self.opacity_label.setText(f"{value}%")

    def closeEvent(self, event) -> None:
        self._overlay.clear_sample()
        super().closeEvent(event)
