@echo off
setlocal

cd /d "%~dp0.."

where python >nul 2>nul
if errorlevel 1 (
    echo Nie znaleziono Pythona na tym komputerze - probuje zainstalowac automatycznie...
    echo.
    where winget >nul 2>nul
    if errorlevel 1 (
        echo Nie znaleziono winget ^(menadzera pakietow Windows^) na tym komputerze.
        echo Zainstaluj Python 3.11 lub nowszy recznie z https://www.python.org/downloads/
        echo WAZNE: podczas instalacji zaznacz opcje "Add python.exe to PATH".
        echo.
        pause
        exit /b 1
    )
    winget install --id Python.Python.3.12 -e --silent --accept-package-agreements --accept-source-agreements
    if errorlevel 1 (
        echo.
        echo Automatyczna instalacja Pythona nie powiodla sie.
        echo Zainstaluj Python 3.11 lub nowszy recznie z https://www.python.org/downloads/
        pause
        exit /b 1
    )
    echo.
    echo Python zostal zainstalowany. Zamknij to okno i uruchom install.bat jeszcze raz
    echo ^(system musi odswiezyc informacje o zainstalowanych programach^).
    pause
    exit /b 0
)

if not exist ".venv" (
    echo Tworze srodowisko aplikacji, to zajmie chwile...
    python -m venv .venv
    if errorlevel 1 (
        echo.
        echo Nie udalo sie utworzyc srodowiska. Zobacz komunikat bledu powyzej.
        pause
        exit /b 1
    )
)

echo Instaluje wymagane skladniki...
".venv\Scripts\python.exe" -m pip install --upgrade pip
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 (
    echo.
    echo Instalacja nie powiodla sie. Zobacz komunikat bledu powyzej.
    pause
    exit /b 1
)

echo.
echo Gotowe! Uruchom aplikacje klikajac dwa razy na run.bat
pause
