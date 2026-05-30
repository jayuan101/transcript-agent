@echo off
setlocal enabledelayedexpansion
title Transcript Agent

set "APPDIR=%~dp0"

:: ── Locate Python: prefer venv, then system python, then py launcher ──────────
set "PYTHON="
if exist "%APPDIR%venv\Scripts\python.exe" (
    set "PYTHON=%APPDIR%venv\Scripts\python.exe"
    goto :have_python
)
where python >nul 2>&1
if %errorlevel%==0 (
    set "PYTHON=python"
    goto :have_python
)
where py >nul 2>&1
if %errorlevel%==0 (
    set "PYTHON=py"
    goto :have_python
)

echo.
echo  ERROR: Python not found.
echo  Run setup_windows.bat first to install everything.
echo.
pause
exit /b 1

:have_python
echo.
echo  Starting Transcript Agent...
echo  Browser will open at http://localhost:7860
echo.

:: Start the app (opens in the same window so Ctrl+C stops it)
start "" "http://localhost:7860"
"%PYTHON%" "%APPDIR%app.py"

endlocal
