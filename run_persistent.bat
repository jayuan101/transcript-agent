@echo off
setlocal enabledelayedexpansion
title Transcript Agent

set "APPDIR=%~dp0"

:: ── Find Python (venv → system python → py launcher) ──────────────────────
set "PYTHON="
if exist "%APPDIR%venv\Scripts\python.exe" (
    set "PYTHON=%APPDIR%venv\Scripts\python.exe"
    goto :have_python
)
where python >nul 2>&1
if %errorlevel%==0 ( set "PYTHON=python" & goto :have_python )
where py >nul 2>&1
if %errorlevel%==0 ( set "PYTHON=py"     & goto :have_python )

echo.
echo  ERROR: Python not found. Run setup_windows.bat first.
echo.
pause & exit /b 1

:have_python
echo.
echo  Transcript Agent  ^|  auto-restart enabled
echo  Browser: http://localhost:7860
echo.

:: Open browser once on first launch
start "" "http://localhost:7860"
set "_first=1"

:restart_loop
if defined _first (
    set "_first="
) else (
    echo  [%time%] Restarting in 3 s...
    timeout /t 3 /nobreak >nul
    echo  [%time%] Restarting now.
)

"%PYTHON%" "%APPDIR%app.py"

goto :restart_loop
