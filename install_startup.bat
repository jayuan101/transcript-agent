@echo off
:: Adds Transcript Agent to Windows Startup so it auto-launches on every login.
:: Run this once.  To remove it, run remove_startup.bat or delete the shortcut.

set "STARTUP=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup"
set "TARGET=%~dp0run_persistent.bat"
set "SHORTCUT=%STARTUP%\Transcript Agent.lnk"

powershell -NoProfile -Command ^
  "$ws = New-Object -ComObject WScript.Shell; " ^
  "$s  = $ws.CreateShortcut('%SHORTCUT%'); " ^
  "$s.TargetPath      = '%TARGET%'; " ^
  "$s.WorkingDirectory= '%~dp0'; " ^
  "$s.WindowStyle     = 7; " ^
  "$s.Description     = 'Transcript Agent — auto-start'; " ^
  "$s.Save()"

if exist "%SHORTCUT%" (
    echo.
    echo  Transcript Agent will now start automatically when Windows starts.
    echo  Shortcut: %SHORTCUT%
    echo.
    echo  To disable auto-start, run remove_startup.bat
) else (
    echo.
    echo  ERROR: Could not create shortcut. Try running as Administrator.
)
echo.
pause
