#!/bin/bash
set -e
cd "$(dirname "$0")"

if ! command -v python3 &>/dev/null; then
    echo "Nie znaleziono Pythona 3 na tym komputerze - próbuję zainstalować automatycznie..."
    echo
    if command -v brew &>/dev/null; then
        echo "Instaluję przez Homebrew..."
        brew install python || true  # failure is handled by the recheck below, not by set -e
    else
        # No package manager -- fetch the official installer and hand off to
        # macOS's normal graphical installer (needs the admin password, same
        # as installing any other Mac app from a downloaded .pkg).
        PY_VERSION="3.12.7"
        PKG_URL="https://www.python.org/ftp/python/${PY_VERSION}/python-${PY_VERSION}-macos11.pkg"
        PKG_PATH="/tmp/python-installer.pkg"
        echo "Pobieram oficjalny instalator Pythona ${PY_VERSION} z python.org..."
        if ! curl -fL -o "$PKG_PATH" "$PKG_URL"; then
            echo "Pobieranie nie powiodło się. Zainstaluj Pythona ręcznie z https://www.python.org/downloads/"
            exit 1
        fi
        echo "Otwieram instalator - postępuj zgodnie z instrukcjami na ekranie (poprosi o hasło administratora)."
        open "$PKG_PATH"
        echo
        echo "Po zakończeniu instalacji uruchom ponownie: bash install.sh"
        exit 0
    fi
    if ! command -v python3 &>/dev/null; then
        echo "Automatyczna instalacja Pythona nie powiodła się."
        echo "Zainstaluj Pythona ręcznie z https://www.python.org/downloads/"
        exit 1
    fi
fi

if [ ! -d ".venv" ]; then
    echo "Tworzę środowisko aplikacji, to zajmie chwilę..."
    if ! python3 -m venv .venv; then
        echo "Nie udało się utworzyć środowiska. Zobacz komunikat błędu powyżej."
        exit 1
    fi
fi

echo "Instaluję wymagane składniki..."
if ! .venv/bin/python -m pip install --upgrade pip || ! .venv/bin/python -m pip install -r requirements.txt; then
    echo "Instalacja nie powiodła się. Zobacz komunikat błędu powyżej."
    exit 1
fi

echo
echo "Gotowe! Uruchom aplikację poleceniem: bash run.sh"
