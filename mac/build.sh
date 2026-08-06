#!/bin/bash
set -e
cd "$(dirname "$0")/.."

if [ ! -f ".venv/bin/python" ]; then
    echo "Brak .venv - uruchom najpierw: bash install.sh"
    exit 1
fi

echo "Instaluję PyInstaller (jeśli jeszcze nie ma)..."
.venv/bin/python -m pip install -r requirements-dev.txt

echo "Buduję CRC Translator.app (to zajmie chwilę)..."
# --onedir, not --onefile: PyInstaller itself deprecates (and will soon
# error on) --onefile combined with --windowed on macOS -- a proper .app
# bundle can't be a single file, and forcing it clashes with Gatekeeper.
# The user experience is identical either way (still one CRC Translator.app
# icon to double-click) -- only the internal layout differs.
.venv/bin/python -m PyInstaller --name "CRC Translator" --windowed --onedir --noconfirm launcher.py

echo
echo "Gotowe: dist/CRC Translator.app"
