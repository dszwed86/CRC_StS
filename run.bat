@echo off
setlocal

cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo Aplikacja nie jest jeszcze zainstalowana.
    echo Kliknij dwa razy na install.bat, a potem sprobuj ponownie.
    echo.
    pause
    exit /b 1
)

".venv\Scripts\python.exe" -m app.main
if errorlevel 1 (
    echo.
    echo Aplikacja zakonczyla sie bledem - zobacz komunikat powyzej.
    pause
)
