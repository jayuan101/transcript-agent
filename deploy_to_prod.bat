@echo off
title Deploy to Production
cd /d "%~dp0"

echo ============================================
echo   Transcript Agent — Deploy DEV to PROD
echo ============================================
echo.
echo Source : %~dp0
echo Target : C:\Users\young\AppData\Local\TranscriptAgent\TranscriptAgent\_internal\
echo.

set PROD_DIR=C:\Users\young\AppData\Local\TranscriptAgent\TranscriptAgent\_internal
set PROD_ROOT=C:\Users\young\AppData\Local\TranscriptAgent

:: Confirm
set /p CONFIRM=Deploy source files to production? (y/N):
if /i not "%CONFIRM%"=="y" (
    echo Cancelled.
    pause
    exit /b 0
)

:: Stop production app
echo Stopping production app...
taskkill /IM TranscriptAgent.exe /F >nul 2>&1
for /f "tokens=5" %%a in ('netstat -ano 2^>nul ^| findstr ":7860.*LISTENING"') do (
    taskkill /PID %%a /F >nul 2>&1
)
timeout /t 2 /nobreak >nul

:: Copy files
echo Copying files...
copy /y app.py            "%PROD_DIR%\app.py"            && echo   [OK] app.py
copy /y transcript_agent.py "%PROD_DIR%\transcript_agent.py" && echo   [OK] transcript_agent.py
copy /y video_analyzer.py "%PROD_DIR%\video_analyzer.py"  && echo   [OK] video_analyzer.py
copy /y api.py            "%PROD_DIR%\api.py"            && echo   [OK] api.py

:: Also copy to root AppData for fallback
copy /y app.py            "%PROD_ROOT%\app.py"           >nul
copy /y transcript_agent.py "%PROD_ROOT%\transcript_agent.py" >nul
copy /y video_analyzer.py "%PROD_ROOT%\video_analyzer.py" >nul
copy /y api.py            "%PROD_ROOT%\api.py"           >nul

echo.
echo Deploy complete! Launching production...
start "" "C:\Users\young\AppData\Local\TranscriptAgent\TranscriptAgent\TranscriptAgent.exe"
timeout /t 18 /nobreak >nul
start http://localhost:7860

echo.
echo Production is live at http://localhost:7860
pause
