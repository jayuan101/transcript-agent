@echo off
title Push Transcript Agent to Docker Hub
echo.
echo  ============================================
echo   Push Transcript Agent to Docker Hub
echo  ============================================
echo.

set /p DOCKER_USER="Enter your Docker Hub username: "

echo.
echo [1/3] Tagging image as %DOCKER_USER%/transcript-agent...
docker tag transcript-agent:latest %DOCKER_USER%/transcript-agent:latest
if %errorlevel% neq 0 ( echo ERROR: Tag failed. & pause & exit /b 1 )

echo.
echo [2/3] Pushing to Docker Hub (this may take a few minutes - image is ~4GB)...
docker push %DOCKER_USER%/transcript-agent:latest
if %errorlevel% neq 0 ( echo ERROR: Push failed. Are you logged in? Run: docker login & pause & exit /b 1 )

echo.
echo [3/3] Creating share package for your friend...

REM Write the friend's docker-compose file
(
echo services:
echo   transcript-agent:
echo     image: %DOCKER_USER%/transcript-agent:latest
echo     container_name: transcript-agent
echo     ports:
echo       - "7860:7860"
echo       - "8000:8000"
echo     volumes:
echo       - ./.env:/app/.env:ro
echo       - ./outputs:/app/outputs
echo       - whisper-cache:/app/.cache
echo     env_file:
echo       - .env
echo     environment:
echo       - GRADIO_SERVER_NAME=0.0.0.0
echo       - GRADIO_SERVER_PORT=7860
echo       - NO_PROXY=localhost,127.0.0.1,api.anthropic.com
echo     restart: unless-stopped
echo volumes:
echo   whisper-cache:
echo     name: transcript-agent-whisper-cache
) > share\docker-compose.yml

echo.
echo  ============================================
echo   Done! Your image is live at:
echo   https://hub.docker.com/r/%DOCKER_USER%/transcript-agent
echo.
echo   Share the 'share' folder with your friend.
echo   They just need Docker Desktop + their API key.
echo  ============================================
echo.
pause
