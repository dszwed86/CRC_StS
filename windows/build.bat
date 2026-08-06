@echo off
setlocal

cd /d "%~dp0.."

if not exist ".venv\Scripts\python.exe" (
    echo Brak .venv - uruchom najpierw install.bat.
    pause
    exit /b 1
)

echo Instaluje PyInstaller (jesli jeszcze nie ma)...
".venv\Scripts\python.exe" -m pip install -r requirements-dev.txt
if errorlevel 1 (
    echo Instalacja PyInstaller nie powiodla sie.
    pause
    exit /b 1
)

echo Buduje "CRC Translator.exe" (to zajmie chwile)...
".venv\Scripts\python.exe" -m PyInstaller --name "CRC Translator" --windowed --onefile --icon "assets\icon.ico" --noconfirm launcher.py
if errorlevel 1 (
    echo Budowanie nie powiodlo sie. Zobacz komunikat bledu powyzej.
    pause
    exit /b 1
)

echo.
echo Gotowe: dist\CRC Translator.exe
pause
