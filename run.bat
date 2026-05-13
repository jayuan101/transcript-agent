@echo off
title Transcript Agent

echo Stopping any old instance...
taskkill /F /IM python.exe >nul 2>&1
timeout /t 3 /nobreak >nul

echo Starting Transcript Agent...
start "" "C:\Users\Ja-Yuan Pendley\AppData\Local\Programs\Python\Python313\python.exe" "%~dp0app.py"

echo Waiting for app to start...
timeout /t 12 /nobreak >nul

echo Opening browser...
start "" "http://localhost:7860"

echo.
echo App is running at: http://localhost:7860
echo Close this window to stop the app.
echo.
pause
