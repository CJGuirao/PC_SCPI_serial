@echo off
setlocal
if not exist "%~dp0.venv\Scripts\python.exe" (
    echo The project virtual environment is missing. See README.md for setup.
    pause
    exit /b 1
)
rem A CSR BlueSuite install exports TCL_LIBRARY machine-wide, and Tk then fails to
rem start with "This probably means that Tcl wasn't installed properly". `set VAR=`
rem REMOVES the variable, which is what it needs - setting it empty does not work.
set TCL_LIBRARY=
set TK_LIBRARY=
start "" "%~dp0.venv\Scripts\python.exe" -I -B "%~dp0portable_launcher.py"
endlocal


