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
echo  Browser will open automatically when ready.
echo.

:: Background poller opens browser as soon as the server responds (max 60 s)
start /b "" powershell -NoProfile -WindowStyle Hidden -Command ^
  "for($i=0;$i-lt60;$i++){Start-Sleep 1;try{$null=Invoke-WebRequest http://127.0.0.1:7860 -UseBasicParsing -TimeoutSec 1;Start-Process 'http://localhost:7860';break}catch{}}"

:: Run app in this window — Ctrl+C stops it cleanly
"%PYTHON%" "%APPDIR%app.py"

endlocal
