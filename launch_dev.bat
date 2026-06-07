@echo off
title Transcript Agent [DEV]
cd /d "%~dp0"

:: Kill any old dev instance on port 7861
for /f "tokens=5" %%a in ('netstat -ano 2^>nul ^| findstr ":7861.*LISTENING"') do (
    taskkill /PID %%a /F >nul 2>&1
)

echo Starting Transcript Agent DEV on port 7861...
echo Source: %~dp0app.py
echo.

set TA_DEV_MODE=1
set GRADIO_SERVER_PORT=7861
set GRADIO_SERVER_NAME=127.0.0.1
set GRADIO_ANALYTICS_ENABLED=False

:: Use the installed venv Python (has all deps)
"C:\Users\young\AppData\Local\TranscriptAgent\venv\Scripts\python.exe" app.py

pause
