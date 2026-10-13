@echo off
rem ============================================================
rem  DY Live Data Analyzer (Python) - launcher
rem
rem  Keep this file PURE ASCII.
rem  Chinese text inside a .bat breaks cmd.exe parsing on
rem  non-UTF8 code pages, so all messages here are English.
rem
rem  This launcher does NOT blindly trust "python" on PATH:
rem  it probes every candidate and picks the first one that can
rem  actually "import PySide6", then prefers pythonw.exe so the
rem  GUI starts without a console window.
rem ============================================================
setlocal enabledelayedexpansion
cd /d "%~dp0"

if not exist "main.py" goto :nomain

rem ---- 1) find a Python that really has PySide6
set "PYEXE="
for /f "delims=" %%P in ('where python.exe 2^>nul') do (
    if not defined PYEXE (
        "%%P" -c "import PySide6" >nul 2>nul
        if !errorlevel!==0 set "PYEXE=%%P"
    )
)
if not defined PYEXE (
    for /f "delims=" %%P in ('where py.exe 2^>nul') do (
        if not defined PYEXE (
            "%%P" -c "import PySide6" >nul 2>nul
            if !errorlevel!==0 set "PYEXE=%%P"
        )
    )
)

if not defined PYEXE goto :nodep

rem ---- 2) derive pythonw.exe next to python.exe (no console window)
set "PYWEXE="
if /i "%PYEXE:~-10%"=="python.exe" set "PYWEXE=%PYEXE:~0,-10%pythonw.exe"
if defined PYWEXE if not exist "%PYWEXE%" set "PYWEXE="

if defined PYWEXE (
    start "" "%PYWEXE%" "main.py"
    exit /b 0
)

rem ---- fall back to python.exe (console kept so errors are visible)
"%PYEXE%" "main.py" %*
if errorlevel 1 pause
exit /b 0


:nomain
echo.
echo   [ERROR] main.py was not found in this folder.
echo   Please run this launcher from the program folder.
echo.
pause
exit /b 1


:nodep
echo.
echo   [ERROR] No usable Python + PySide6 was found.
echo.
echo   Step 1 - install Python 3.8 or newer:
echo       https://www.python.org/downloads/
echo     During setup, tick "Add python.exe to PATH".
echo.
echo   Step 2 - install the GUI dependency (in a command prompt):
echo       pip install PySide6-Essentials
echo.
echo   Step 3 - run this launcher again.
echo.
pause
exit /b 1
