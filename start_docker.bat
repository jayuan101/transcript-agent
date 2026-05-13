@echo off
title Transcript Agent - Docker
echo.
echo  ============================================
echo   Transcript Agent — Docker Launcher
echo  ============================================
echo.

where docker >nul 2>&1
if %errorlevel% neq 0 (
    echo  ERROR: Docker is not installed or not running.
    echo  Download from: https://www.docker.com/products/docker-desktop
    pause
    exit /b 1
)

echo  [1/3] Building image (first run takes ~5-10 minutes)...
docker compose build
if %errorlevel% neq 0 (
    echo  ERROR: Build failed. Check the output above.
    pause
    exit /b 1
)

echo.
echo  [2/3] Starting container...
docker compose up -d
if %errorlevel% neq 0 (
    echo  ERROR: Could not start container.
    pause
    exit /b 1
)

echo.
echo  [3/3] Waiting for app to be ready (30 seconds)...
timeout /t 30 /nobreak >nul

echo.
echo  Opening browser...
start "" "http://localhost:7860"

echo.
echo  ============================================
echo   App is running at: http://localhost:7860
echo.
echo   To share with another computer on the
echo   same network, give them your IP address:
echo   http://YOUR_IP:7860
echo.
echo   To stop:  docker compose down
echo   To view logs:  docker compose logs -f
echo  ============================================
echo.
pause
