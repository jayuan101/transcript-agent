@echo off
powercfg /change standby-timeout-ac 0
powercfg /change monitor-timeout-ac 0
echo.
echo  ☕  Sleep prevention ON
echo     Your computer will not sleep while plugged in.
echo     Run restore_sleep.bat when you are done transcribing.
echo.
pause
