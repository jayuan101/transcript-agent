@echo off
title Transcript Agent

echo.
echo  ============================================
echo   Transcript Agent — Starting...
echo  ============================================
echo.

REM Check Docker is running
docker info >nul 2>&1
if errorlevel 1 (
    echo  ERROR: Docker is not running.
    echo  Please start Docker Desktop and try again.
    echo.
    pause
    exit /b 1
)

echo  Pulling latest image from Docker Hub...
docker pull sushi0934/transcript-agent:latest

echo.
echo  Stopping any previous instance...
docker rm -f transcript-agent >nul 2>&1

echo  Starting Transcript Agent...
docker run -d ^
  --name transcript-agent ^
  -p 7860:7860 ^
  -p 8000:8000 ^
  -e GRADIO_SERVER_NAME=0.0.0.0 ^
  -e GRADIO_SERVER_PORT=7860 ^
  -v transcript-agent-outputs:/app/outputs ^
  -v transcript-agent-cache:/app/.cache ^
  --restart unless-stopped ^
  sushi0934/transcript-agent:latest

if errorlevel 1 (
    echo.
    echo  ERROR: Failed to start. Check Docker Desktop is running.
    pause
    exit /b 1
)

echo.
echo  ============================================
echo   App is running at: http://localhost:7860
echo  ============================================
echo.
echo  Opening browser...
timeout /t 3 /nobreak >nul
start http://localhost:7860

echo  Press any key to stop the app, or close this window to keep it running.
pause >nul

echo.
echo  Stopping Transcript Agent...
docker stop transcript-agent
docker rm transcript-agent
echo  Done. Goodbye!
timeout /t 2 /nobreak >nul
