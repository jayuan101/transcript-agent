@echo off
powercfg /change standby-timeout-ac 30
powercfg /change monitor-timeout-ac 15
echo.
echo  ✅  Sleep restored
echo     Computer will sleep after 30 min idle (plugged in).
echo.
pause
