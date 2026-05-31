@echo off
set "SHORTCUT=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\Transcript Agent.lnk"
if exist "%SHORTCUT%" (
    del "%SHORTCUT%"
    echo  Auto-start removed.
) else (
    echo  Auto-start shortcut not found (already removed).
)
echo.
pause
