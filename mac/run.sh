#!/bin/bash
cd "$(dirname "$0")/.."

if [ ! -f ".venv/bin/python" ]; then
    echo "Aplikacja nie jest jeszcze zainstalowana. Uruchom najpierw: bash install.sh"
    exit 1
fi

.venv/bin/python -m app.main
if [ $? -ne 0 ]; then
    echo
    echo "Aplikacja zakończyła się błędem - zobacz komunikat powyżej."
fi
