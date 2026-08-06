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

APP_PATH="dist/CRC Translator.app"
PLIST="$APP_PATH/Contents/Info.plist"

# PyInstaller doesn't add a microphone-usage description to Info.plist on
# its own. Without one, macOS doesn't prompt for microphone permission at
# all -- it just silently denies it (no error, no dialog, the app simply
# never appears in System Settings -> Privacy & Security -> Microphone),
# which looked exactly like "translation connects fine but nothing happens
# when you speak" when this first shipped. Adding the key here makes macOS
# show the normal permission prompt on first use instead.
echo "Dodaję wymagane uprawnienie do mikrofonu w Info.plist..."
/usr/libexec/PlistBuddy -c "Add :NSMicrophoneUsageDescription string 'CRC Translator potrzebuje dostępu do mikrofonu, aby tłumaczyć mowę na żywo.'" "$PLIST"

# PyInstaller ad-hoc-signs the bundle as part of building it; editing
# Info.plist afterwards invalidates that signature (macOS would refuse to
# launch it as "damaged"), so it must be re-signed after this edit.
echo "Podpisuję ponownie po zmianie Info.plist..."
codesign --force --deep -s - "$APP_PATH"

echo
echo "Gotowe: dist/CRC Translator.app"
