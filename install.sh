#!/bin/bash
set -e
cd "$(dirname "$0")"

# Building some Python packages from source (e.g. "av", when no prebuilt
# wheel matches this exact macOS/Python combination) needs a C compiler,
# which a fresh Mac doesn't have until Xcode Command Line Tools are
# installed. Checking this up front turns what would otherwise be a wall of
# cryptic compiler errors deep inside a pip failure into one clear step.
if ! xcode-select -p &>/dev/null; then
    echo "Brakuje Xcode Command Line Tools (wymagane do zainstalowania niektórych składników)."
    echo "Otwieram instalator systemowy - potwierdź w oknie, które się pojawi (może to potrwać kilka minut),"
    echo "a potem uruchom ponownie: bash install.sh"
    xcode-select --install 2>/dev/null || true
    exit 1
fi

# Can be forced manually, e.g. `PYTHON_BIN=python3.12 bash install.sh`, if
# the automatic detection below ever still picks a Python this app's
# dependencies don't support yet.
PYTHON_BIN="${PYTHON_BIN:-}"

# A python3 that exists but is too old doesn't count as "found" -- macOS
# ships its own /usr/bin/python3 placeholder stuck around 3.9 on every
# machine, present or not depending on whether Xcode Command Line Tools
# were ever installed, so "python3 exists" alone says nothing about whether
# it's usable. Auto-installing a good one below must still kick in for this
# case exactly like the "nothing found at all" case, not bail out to a
# manual-install message.
if [ -z "$PYTHON_BIN" ] && command -v python3 &>/dev/null; then
    if python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; then
        PYTHON_BIN="python3"
    fi
fi

if [ -z "$PYTHON_BIN" ]; then
    if command -v python3 &>/dev/null; then
        FOUND_OLD_VERSION=$(python3 -c 'import sys; print(".".join(map(str, sys.version_info[:3])))')
        echo "Znaleziony Python ($FOUND_OLD_VERSION) jest za stary (potrzeba co najmniej 3.11) - próbuję zainstalować nowszy automatycznie..."
    else
        echo "Nie znaleziono Pythona 3 na tym komputerze - próbuję zainstalować automatycznie..."
    fi
    echo
    if command -v brew &>/dev/null; then
        # A specific, long-supported version, not Homebrew's plain "python"
        # (which always tracks the newest CPython release): PySide6's
        # published wheels typically lag a new CPython release by a few
        # months, so "newest" can fail with a confusing "requires a
        # different Python" / "No matching distribution found for PySide6"
        # error even though that Python is new enough for this app's own
        # requirements.
        echo "Instaluję Pythona 3.12 przez Homebrew..."
        brew install python@3.12 || true  # failure is handled by the recheck below, not by set -e
        BREW_PY="$(brew --prefix python@3.12 2>/dev/null)/bin/python3.12"
        if [ -x "$BREW_PY" ]; then
            PYTHON_BIN="$BREW_PY"
        fi
    else
        # No package manager -- fetch the official installer and hand off to
        # macOS's normal graphical installer (needs the admin password, same
        # as installing any other Mac app from a downloaded .pkg). Pinned to
        # the same 3.12 version, for the same reason as the Homebrew branch
        # above.
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
    if [ -z "$PYTHON_BIN" ]; then
        echo "Automatyczna instalacja Pythona nie powiodła się."
        echo "Zainstaluj Pythona ręcznie z https://www.python.org/downloads/"
        exit 1
    fi
fi

if [ ! -d ".venv" ]; then
    echo "Tworzę środowisko aplikacji, to zajmie chwilę..."
    if ! "$PYTHON_BIN" -m venv .venv; then
        echo "Nie udało się utworzyć środowiska. Zobacz komunikat błędu powyżej."
        exit 1
    fi
fi

echo "Instaluję wymagane składniki..."
INSTALL_LOG="$(mktemp)"
set +e
.venv/bin/python -m pip install --upgrade pip 2>&1 | tee "$INSTALL_LOG"
PIP_UPGRADE_STATUS=${PIPESTATUS[0]}
PIP_INSTALL_STATUS=0
if [ "$PIP_UPGRADE_STATUS" -eq 0 ]; then
    .venv/bin/python -m pip install -r requirements.txt 2>&1 | tee -a "$INSTALL_LOG"
    PIP_INSTALL_STATUS=${PIPESTATUS[0]}
fi
set -e

if [ "$PIP_UPGRADE_STATUS" -ne 0 ] || [ "$PIP_INSTALL_STATUS" -ne 0 ]; then
    echo
    echo "Instalacja nie powiodła się. Pełny log zapisany w: $INSTALL_LOG"
    if grep -qiE "command .*(clang|gcc|cc1?) .*failed|error: command .* failed with exit" "$INSTALL_LOG"; then
        echo
        echo "Wygląda na brak narzędzi do kompilacji. Zainstaluj Xcode Command Line Tools:"
        echo "  xcode-select --install"
    elif grep -qiE "ffmpeg|libav|pkg-config.*not found" "$INSTALL_LOG"; then
        echo
        echo "Wygląda na brak biblioteki ffmpeg, potrzebnej do zbudowania jednego ze składników."
        echo "Zainstaluj ją przez Homebrew (https://brew.sh) poleceniem:"
        echo "  brew install ffmpeg"
    elif grep -qiE "requires a different Python|No matching distribution found for PySide6|Could not find a version that satisfies the requirement PySide6" "$INSTALL_LOG"; then
        PY_FOUND=$("$PYTHON_BIN" -c 'import sys; print(".".join(map(str, sys.version_info[:3])))')
        echo
        echo "Ten Python ($PY_FOUND) jest za nowy - PySide6 jeszcze go nie obsługuje."
        echo "Napraw to tak:"
        echo "  1. brew install python@3.12   (jeśli nie masz Homebrew, zainstaluj go z https://brew.sh)"
        echo "  2. rm -rf .venv"
        echo '  3. PYTHON_BIN="$(brew --prefix python@3.12)/bin/python3.12" bash install.sh'
    fi
    echo
    echo "Popraw powyższe i uruchom ponownie: bash install.sh"
    exit 1
fi
rm -f "$INSTALL_LOG"

echo
echo "Gotowe! Uruchom aplikację poleceniem: bash run.sh"
