"""Dev utility: render a window of this app offscreen and save it as a PNG,
so changes to the GUI can be checked visually instead of only programmatically.

Usage (from the project root, with the venv active):
    QT_QPA_PLATFORM=offscreen python scripts/screenshot_gui.py [output.png]

Optional env vars to drive the window into a specific state before the shot:
    SCREENSHOT_MODE=file       shows the window with a file selected (no Start)
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# The "offscreen" QPA platform doesn't discover system fonts on its own here,
# which renders every glyph as a blank box. Point it at the Windows fonts
# directory so screenshots actually show text.
if sys.platform == "win32" and "QT_QPA_FONTDIR" not in os.environ:
    os.environ["QT_QPA_FONTDIR"] = r"C:\Windows\Fonts"

from PySide6.QtWidgets import QApplication

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.gui import MainWindow


def main() -> None:
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("screenshot.png")

    app = QApplication(sys.argv[:1])
    window = MainWindow()
    window.show()

    if os.environ.get("SCREENSHOT_MODE") == "file":
        # Mirrors _choose_file()'s pre-Start branch (mode_combo was removed
        # by the same feature branch this script predates) -- no need for
        # probe_audio_file() here since the window is never started.
        window._selected_file = "example.wav"
        window.file_label.setText(window._selected_file)
        window.file_clear_btn.setEnabled(True)
        window.position_slider.setVisible(True)
        window.position_label.setVisible(True)
        window.file_pause_btn.setVisible(True)

    app.processEvents()
    window.grab().save(str(out))
    window.close()
    print(f"Saved screenshot to {out.resolve()}")


if __name__ == "__main__":
    main()
