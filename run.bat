@echo off
rem Launch the oscilloscope app from this directory.
rem
rem Two things this has to get right, both measured on this machine:
rem   1. The venv interpreter, because the system Python has no pyusb or numpy.
rem   2. TCL_LIBRARY and TK_LIBRARY must be REMOVED, not just emptied. A CSR
rem      BlueSuite install exports them machine-wide, and Tk refuses to start while
rem      they point at it ("This probably means that Tcl wasn't installed properly").
rem      In cmd, `set VAR=` removes a variable - but only on its own line: the space
rem      before && in `set VAR= && ...` becomes the value and it fails anyway.
rem
rem Any arguments are passed through to main.py.
setlocal
set TCL_LIBRARY=
set TK_LIBRARY=
if not exist "%~dp0.venv\Scripts\python.exe" (
    echo.
    echo The virtual environment is missing: %~dp0.venv\Scripts\python.exe
    echo Create it first - see README.md:
    echo   python -m venv .venv
    echo   .venv\Scripts\python.exe -m pip install -r requirements.txt
    echo.
    pause
    exit /b 1
)
"%~dp0.venv\Scripts\python.exe" "%~dp0main.py" %*
set EXITCODE=%ERRORLEVEL%
if not "%EXITCODE%"=="0" (
    echo.
    echo The app exited with code %EXITCODE%.
    pause
)
endlocal & exit /b %EXITCODE%
