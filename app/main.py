import os
import sys
from pathlib import Path

import certifi

# Must run before any network code (palabra_ai's websocket connections in
# particular) creates an SSL context. On macOS, Python installed from the
# official python.org installer ships its own OpenSSL that isn't wired up to
# the system's Keychain trust store, so the default SSL context can't verify
# any server certificate -- every wss:// connection to the Palabra API fails
# with "certificate verify failed: unable to get local issuer certificate".
# Pointing SSL_CERT_FILE at certifi's bundled CA list (a known-good, portable
# set of trusted roots) sidesteps that entirely, on every platform, without
# requiring the user to run python.org's separate "Install Certificates"
# step by hand.
os.environ.setdefault("SSL_CERT_FILE", certifi.where())

from PySide6.QtGui import QFont, QIcon
from PySide6.QtWidgets import QApplication

from .gui import MainWindow
from .theme import QSS as THEME_QSS


def _icon_path() -> str:
    # PyInstaller extracts bundled data files under sys._MEIPASS at runtime
    # (see the --add-data flags in windows/build.bat and mac/build.sh);
    # running from source, assets/ is just a normal sibling of app/.
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))
    return str(base / "assets" / "icon.png")


def main() -> None:
    app = QApplication(sys.argv)
    app.setWindowIcon(QIcon(_icon_path()))
    # A default font size for the app's own widgets, applied BEFORE the
    # stylesheet -- deliberately not "font-size: 10pt" in theme.py's QSS: a
    # QSS font-size on the universal "*" selector overrides every widget's
    # own setFont() call with no way for that widget to opt out (confirmed:
    # it silently forced OverlayWindow's caption_view -- meant to be large,
    # readable subtitles for OBS to capture -- down from its real ~28-60pt
    # size to 10pt). QApplication.setFont() only sets the inherited
    # default; any widget's own explicit setFont() still wins normally.
    app.setFont(QFont(app.font().family(), 10))
    app.setStyleSheet(THEME_QSS)
    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
