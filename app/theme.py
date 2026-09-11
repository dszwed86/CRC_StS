"""The app's visual identity: a dark, disciplined palette in the spirit of
the CRC Translator badge (assets/icon.png -- a plain black-and-white
monogram) and the broadcast tooling this app sits alongside on screen
during a live service (OBS, mixers, tally-light panels). One accent color
carries the whole identity: amber, the "standby/attention" color on a
studio tally light -- distinct from the red this app's own code already
uses for connection errors (see gui.py's SessionState.RECONNECTING
handling) and from any "on-air green", so it never collides with an
existing status color.

Applied once, app-wide, via QApplication.setStyleSheet() in app/main.py.
Per-widget setStyleSheet() calls elsewhere (status colors, level-meter
colors) still take precedence for that one widget -- Qt's stylesheet
cascade applies a widget's own sheet on top of the app-wide one -- so nothing
here needs to account for those; this file only sets the shared chrome.
"""

# Named like a small design-token table -- change a look system-wide by
# editing one value here, not by hunting through QSS selectors below.
BG_BASE = "#15171B"  # window background
BG_PANEL = "#1C1F24"  # group boxes, the log, the overlay preview
BG_RAISED = "#242830"  # buttons/inputs at rest
BG_RAISED_HOVER = "#2C313A"
BG_RAISED_PRESSED = "#1A1D22"
BORDER = "#343A44"
BORDER_SUBTLE = "#282C33"
TEXT_PRIMARY = "#EDEBE4"  # warm off-white, echoes the logo's white without reading clinical
TEXT_SECONDARY = "#9AA1AC"
TEXT_DISABLED = "#5B6068"
ACCENT = "#D9A544"
ACCENT_HOVER = "#E6B769"
ACCENT_PRESSED = "#C0902F"
ACCENT_TEXT = "#1A1300"  # dark text on top of a filled-accent control
# Status colors for text/messages set via per-widget setStyleSheet() calls
# scattered across gui.py (error/success text in dialogs, the update-check
# link, ...) -- brightened versions of colors this app already used before
# the dark theme existed. The originals (e.g. "#b02a2a" red) were tuned for
# a light background and measured well under WCAG's 4.5:1 minimum once this
# theme's near-black backgrounds shipped (as low as 2.46:1 for red-on-red
# where the status banner below is also red-tinted) -- a real accessibility
# regression, not a deliberate look.
DANGER = "#FF6B6B"
SUCCESS = "#5EC269"
WARNING = "#E0A33A"
LINK = "#6FA8F5"

QSS = f"""
* {{
    color: {TEXT_PRIMARY};
}}

QWidget {{
    selection-background-color: {ACCENT};
    selection-color: {ACCENT_TEXT};
}}

/* Deliberately NOT a blanket "QWidget {{ background-color: ... }}" rule --
   this app nests plain QWidgets purely for layout inside QGroupBox panels
   (mic_gain_row, file_row, the "Zaawansowane" panel, ...), and a blanket
   background painted every one of those its own flat BG_BASE instead of
   showing the group box's own BG_PANEL through, which read as a visible
   mismatched-color seam cutting across otherwise-continuous panels (a real
   user report). Only paint a background where one is actually meant to be
   visible: the top-level window/dialog canvas and QGroupBox's own panel
   below -- everything else stays transparent and shows its ancestor's
   already-painted background through, same as plain (non-stylesheet) Qt. */
QMainWindow, QDialog {{
    background-color: {BG_BASE};
}}

QLabel {{
    background-color: transparent;
}}

QToolTip {{
    background-color: {BG_RAISED};
    color: {TEXT_PRIMARY};
    border: 1px solid {BORDER};
    border-radius: 4px;
    padding: 4px 6px;
}}

/* -- Group boxes: the app's "card" -- one step lighter than the window,
   a hairline border, and a title that reads as a section label rather
   than a plain bold word. */
QGroupBox {{
    background-color: {BG_PANEL};
    border: 1px solid {BORDER};
    border-radius: 8px;
    margin-top: 16px;
    padding: 14px 10px 10px 10px;
    font-weight: 600;
}}

QGroupBox::title {{
    subcontrol-origin: margin;
    subcontrol-position: top left;
    left: 10px;
    top: -2px;
    padding: 0 6px;
    color: {TEXT_SECONDARY};
    background-color: {BG_BASE};
}}

/* The "Zaawansowane" disclosure panel's own card -- same panel background/
   border/radius as a QGroupBox above it, but as a plain QFrame (see
   advanced_panel's construction) since its "title" is the toggle button
   above it, not a QGroupBox's own title notch. */
QFrame#advancedPanel {{
    background-color: {BG_PANEL};
    border: 1px solid {BORDER};
    border-radius: 8px;
    padding: 6px;
}}

/* -- Buttons: quiet/outlined by default; the one true "go" action
   (Start/Stop) opts into the filled accent look via objectName. -- */
QPushButton {{
    background-color: {BG_RAISED};
    color: {TEXT_PRIMARY};
    border: 1px solid {BORDER};
    border-radius: 6px;
    padding: 6px 14px;
}}

QPushButton:hover {{
    background-color: {BG_RAISED_HOVER};
    border-color: {ACCENT};
}}

QPushButton:pressed {{
    background-color: {BG_RAISED_PRESSED};
}}

QPushButton:disabled {{
    color: {TEXT_DISABLED};
    border-color: {BORDER_SUBTLE};
}}

/* Once a widget's border/background are styled via QSS at all, Qt stops
   drawing its native keyboard-focus rectangle there and nothing replaces
   it -- confirmed directly (tabbing through the window, every button and
   checkbox showed hasFocus() == True with zero visible difference), which
   left keyboard navigation effectively silent for every button and
   checkbox in the app. */
QPushButton:focus {{
    border-color: {ACCENT};
}}

QPushButton#primaryButton {{
    background-color: {ACCENT};
    color: {ACCENT_TEXT};
    border: 1px solid {ACCENT};
    font-weight: 600;
    padding: 6px 22px;
}}

QPushButton#primaryButton:hover {{
    background-color: {ACCENT_HOVER};
    border-color: {ACCENT_HOVER};
}}

QPushButton#primaryButton:pressed {{
    background-color: {ACCENT_PRESSED};
    border-color: {ACCENT_PRESSED};
}}

QPushButton#primaryButton:disabled {{
    background-color: {BG_RAISED};
    color: {TEXT_DISABLED};
    border-color: {BORDER_SUBTLE};
}}

/* -- The "Zaawansowane" disclosure toggle: quiet, not a call to action --
   flat until hovered, muted label color, no border of its own. -- */
QPushButton#advancedToggle {{
    background-color: transparent;
    color: {TEXT_SECONDARY};
    border: none;
    border-radius: 6px;
    padding: 4px 8px;
    text-align: left;
}}

QPushButton#advancedToggle:hover {{
    background-color: {BG_RAISED};
    color: {TEXT_PRIMARY};
}}

QPushButton#advancedToggle:checked {{
    color: {TEXT_PRIMARY};
}}

/* -- Inputs -- */
QLineEdit, QPlainTextEdit, QListWidget, QComboBox, QSpinBox, QDoubleSpinBox {{
    background-color: {BG_RAISED};
    color: {TEXT_PRIMARY};
    border: 1px solid {BORDER};
    border-radius: 6px;
    padding: 4px 8px;
}}

QPlainTextEdit, QListWidget {{
    background-color: {BG_PANEL};
    padding: 8px;
}}

QLineEdit:focus, QComboBox:focus, QPlainTextEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus {{
    border-color: {ACCENT};
}}

QLineEdit:disabled, QComboBox:disabled, QSpinBox:disabled, QDoubleSpinBox:disabled {{
    color: {TEXT_DISABLED};
    border-color: {BORDER_SUBTLE};
}}

QComboBox::drop-down {{
    border: none;
    width: 22px;
}}

/* Styling ::drop-down at all (even just border/width above) makes Qt stop
   drawing its native arrow glyph there -- with no ::down-arrow rule to
   replace it, every combo box in the app rendered as a bare rectangle
   indistinguishable from a QLineEdit, with no visual hint it opens a list
   (a real user-visible defect). A small triangle built from transparent
   side borders around a zero-size box -- the standard way to draw one in
   Qt's CSS-subset style sheets without needing an image asset. */
QComboBox::down-arrow {{
    width: 0;
    height: 0;
    border-left: 4px solid transparent;
    border-right: 4px solid transparent;
    border-top: 5px solid {TEXT_SECONDARY};
    margin-right: 8px;
}}

QComboBox QAbstractItemView {{
    background-color: {BG_RAISED};
    color: {TEXT_PRIMARY};
    border: 1px solid {BORDER};
    selection-background-color: {ACCENT};
    selection-color: {ACCENT_TEXT};
    outline: none;
}}

/* -- Checkboxes: a small filled square when checked, accent-colored. -- */
QCheckBox {{
    spacing: 8px;
}}

QCheckBox::indicator {{
    width: 16px;
    height: 16px;
    border: 1px solid {BORDER};
    border-radius: 4px;
    background-color: {BG_RAISED};
}}

QCheckBox::indicator:hover {{
    border-color: {ACCENT};
}}

QCheckBox::indicator:checked {{
    background-color: {ACCENT};
    border-color: {ACCENT};
}}

QCheckBox::indicator:disabled {{
    border-color: {BORDER_SUBTLE};
}}

QCheckBox::indicator:focus {{
    border-color: {ACCENT};
}}

/* -- Sliders: thin groove, round accent handle -- the one place the
   accent color appears as a literal "tally light" dot the user drags. -- */
QSlider::groove:horizontal {{
    height: 4px;
    background-color: {BG_RAISED};
    border-radius: 2px;
}}

QSlider::sub-page:horizontal {{
    background-color: {ACCENT};
    border-radius: 2px;
}}

QSlider::handle:horizontal {{
    width: 14px;
    height: 14px;
    margin: -6px 0;
    border-radius: 7px;
    background-color: {ACCENT};
    border: 2px solid {BG_BASE};
}}

QSlider::handle:horizontal:hover {{
    background-color: {ACCENT_HOVER};
}}

/* -- Progress bars (mic/output level meters): the widget's own inline
   ::chunk color (a VU-meter convention -- green for mic, blue for output)
   wins over this; this only sets the resting track look. -- */
QProgressBar {{
    background-color: {BG_RAISED_HOVER};
    border: 1px solid {BORDER};
    border-radius: 5px;
    text-align: center;
    color: transparent;
}}

QProgressBar::chunk {{
    border-radius: 3px;
}}

QScrollBar:vertical {{
    background-color: {BG_PANEL};
    width: 12px;
    margin: 0;
}}

QScrollBar::handle:vertical {{
    background-color: {BG_RAISED_HOVER};
    border-radius: 5px;
    min-height: 24px;
}}

QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
    height: 0;
}}

QScrollBar:horizontal {{
    background-color: {BG_PANEL};
    height: 12px;
    margin: 0;
}}

QScrollBar::handle:horizontal {{
    background-color: {BG_RAISED_HOVER};
    border-radius: 5px;
    min-width: 24px;
}}

QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{
    width: 0;
}}

/* Qt only paints sub-controls a stylesheet actually names once it takes
   over a scrollbar's rendering at all -- leaving add-page/sub-page (the
   track on either side of the handle) and the little square where a
   vertical and horizontal scrollbar meet unstyled rendered as a light
   checkerboard placeholder pattern on this dark background, on every
   scrollable area in the app (a real user-visible defect, not a deliberate
   look). Painting them the same as the scrollbar's own background makes
   the track read as one continuous strip behind the handle. */
QScrollBar::add-page, QScrollBar::sub-page {{
    background-color: {BG_PANEL};
}}

QAbstractScrollArea::corner {{
    background-color: {BG_PANEL};
}}

QMenu {{
    background-color: {BG_RAISED};
    color: {TEXT_PRIMARY};
    border: 1px solid {BORDER};
}}

QMenu::item:selected {{
    background-color: {ACCENT};
    color: {ACCENT_TEXT};
}}
"""
