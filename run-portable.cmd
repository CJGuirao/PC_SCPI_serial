@echo off
setlocal
if not exist "%~dp0.venv\Scripts\python.exe" (
    echo The project virtual environment is missing. See README.md for setup.
    pause
    exit /b 1
)
start "" "%~dp0.venv\Scripts\python.exe" -I -B "%~dp0portable_launcher.py"
endlocal


