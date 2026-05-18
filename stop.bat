@echo off
echo  Stopping Transcript Agent...
docker stop transcript-agent >nul 2>&1
docker rm transcript-agent >nul 2>&1
echo  Stopped.
timeout /t 2 /nobreak >nul
