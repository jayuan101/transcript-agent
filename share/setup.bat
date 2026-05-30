@echo off
setlocal enabledelayedexpansion
title Transcript Agent - First Time Setup
echo.
echo  ============================================
echo   Transcript Agent - Setup
echo  ============================================
echo.

REM Check Docker is installed
where docker >nul 2>&1
if %errorlevel% neq 0 (
    echo  ERROR: Docker Desktop is not installed.
    echo  Download it free from: https://www.docker.com/products/docker-desktop
    echo  Install it, restart your computer, then run this again.
    pause
    exit /b 1
)

echo  Docker found. Checking it's running...
docker info >nul 2>&1
if %errorlevel% neq 0 (
    echo  ERROR: Docker Desktop is not running.
    echo  Open Docker Desktop from your Start Menu, wait for it to fully start,
    echo  then run this script again.
    pause
    exit /b 1
)

echo  Docker is running.
echo.

REM Create .env if it doesn't exist
if not exist .env (
    echo  You need an Anthropic API key to use this app.
    echo  Get one free at: https://console.anthropic.com
    echo.
    set /p API_KEY="Paste your Anthropic API key here: "
    echo ANTHROPIC_API_KEY=!API_KEY!> .env
    echo  API key saved.
) else (
    echo  API key file found.
)

echo.
echo  [1/2] Pulling Transcript Agent image from Docker Hub...
echo  (First time only - downloads ~2 GB. Grab a coffee.)
echo.
docker compose pull
if %errorlevel% neq 0 (
    echo  ERROR: Could not pull image. Check your internet connection.
    pause
    exit /b 1
)

echo.
echo  [2/2] Starting Transcript Agent...
docker compose up -d

echo.
echo  Waiting for app to start...
timeout /t 20 /nobreak >nul

start "" "http://localhost:7860"

echo.
echo  ============================================
echo   App is running!
echo.
echo   UI:  http://localhost:7860
echo   API: http://localhost:8000/docs
echo.
echo   To stop:    docker compose down
echo   To restart: docker compose up -d
echo  ============================================
echo.
pause
