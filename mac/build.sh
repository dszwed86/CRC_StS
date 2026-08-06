#!/bin/bash
set -e
cd "$(dirname "$0")/.."

if [ ! -f ".venv/bin/python" ]; then
    echo "Brak .venv - uruchom najpierw: bash install.sh"
    exit 1
fi

echo "Instaluję PyInstaller (jeśli jeszcze nie ma)..."
.venv/bin/python -m pip install -r requirements-dev.txt

echo "Buduję PalabraS2S.app (to zajmie chwilę)..."
.venv/bin/python -m PyInstaller --name PalabraS2S --windowed --onefile --noconfirm launcher.py

echo
echo "Gotowe: dist/PalabraS2S.app"
