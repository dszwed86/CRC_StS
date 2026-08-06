import os
import sys

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

from PySide6.QtWidgets import QApplication

from .gui import MainWindow


def main() -> None:
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
