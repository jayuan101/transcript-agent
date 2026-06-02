@echo off
setlocal enabledelayedexpansion
title Transcript Agent - Setup
chcp 65001 >nul 2>&1

set "APPDIR=%~dp0"
set "VENV=%APPDIR%venv"
set "VPYTHON=%VENV%\Scripts\python.exe"
set "PIP=%VENV%\Scripts\pip.exe"
set "CURRENT_VERSION=1.1.39"

cls
echo.
echo  ============================================================
echo    Transcript Agent  ^|  Windows Installer
echo  ============================================================
echo.

:: -- Already installed? Show menu ---------------------------------------------
if exist "!VPYTHON!" (
    echo  Existing installation found.
    echo.
    echo    [1]  Launch app
    echo    [2]  Check for updates
    echo    [3]  Reinstall from scratch
    echo    [4]  Exit
    echo.
    set /p "CHOICE= Enter choice [1-4]: "
    echo.
    if "!CHOICE!"=="1" goto :launch
    if "!CHOICE!"=="2" goto :update
    if "!CHOICE!"=="3" goto :fresh_install
    if "!CHOICE!"=="4" goto :end
    goto :launch
)

:: -- Fresh install -------------------------------------------------------------
:fresh_install

:: Step 1 - Detect Python
echo  [1/5] Checking for Python 3.9+...

set "PY="
where python >nul 2>&1 && set "PY=python"
if "!PY!"=="" where py >nul 2>&1 && set "PY=py"
if "!PY!"=="" (
    echo.
    echo  ERROR: Python is not installed or not in PATH.
    echo.
    echo  Install Python 3.10+ from:
    echo    https://www.python.org/downloads/
    echo.
    echo  IMPORTANT: Tick "Add Python to PATH" during installation,
    echo             then run this setup again.
    echo.
    set /p "OPN= Open the download page now? [Y/n]: "
    if /i "!OPN!" neq "n" start "" "https://www.python.org/downloads/"
    goto :fail
)
"!PY!" -c "import sys; sys.exit(0 if sys.version_info>=(3,9) else 1)" >nul 2>&1
if %errorlevel% neq 0 (
    echo  ERROR: Python 3.9+ required. Update from https://www.python.org/downloads/
    goto :fail
)
for /f "tokens=*" %%v in ('"!PY!" --version 2^>^&1') do echo  Found: %%v

:: Step 2 - Virtual environment
echo.
echo  [2/5] Setting up virtual environment...
if exist "!VPYTHON!" (
    echo  Already exists, skipping.
) else (
    "!PY!" -m venv "!VENV!"
    if %errorlevel% neq 0 ( echo  ERROR: Could not create venv. & goto :fail )
    echo  Created: !VENV!
)

:: Step 3 - Install dependencies
echo.
echo  [3/5] Installing dependencies...
echo  First run downloads about 2 GB and takes 5-15 minutes.
echo.

"!PIP!" install --upgrade pip --quiet

echo   Installing PyTorch (CPU)...
"!PIP!" install torch torchvision torchaudio ^
    --index-url https://download.pytorch.org/whl/cpu --quiet
if %errorlevel% neq 0 (
    echo   Retrying with default index...
    "!PIP!" install torch --quiet
)

echo   Installing app requirements...
"!PIP!" install -r "!APPDIR!requirements.txt" --quiet
if %errorlevel% neq 0 ( echo  ERROR: pip install failed. & goto :fail )

echo   Installing bundled ffmpeg...
"!PIP!" install imageio-ffmpeg --quiet

echo  All dependencies installed.

:: Step 4 - API key
echo.
echo  [4/5] API key setup...
if exist "!APPDIR!.env" (
    echo  Found existing .env file. Skipping.
) else (
    echo  You need an API key to use this app.
    echo  Get one free at: https://console.anthropic.com
    echo.
    set /p "AKEY= Paste your Anthropic API key (or Enter to skip): "
    if "!AKEY!" neq "" (
        echo ANTHROPIC_API_KEY=!AKEY!>"!APPDIR!.env"
        echo  Saved to .env
    ) else (
        echo  Skipped. You can enter your key inside the app.
    )
)

:: Step 5 - Desktop shortcut
echo.
echo  [5/5] Creating desktop shortcut...
call :make_shortcut
if %errorlevel%==0 (
    echo  Shortcut created: "Transcript Agent" on Desktop
) else (
    echo  Note: Could not create shortcut. Use run.bat to launch instead.
)

echo.
echo  ============================================================
echo    Setup complete! v!CURRENT_VERSION!
echo.
echo    Launch anytime: double-click "Transcript Agent" on Desktop
echo                    or run run.bat in this folder.
echo  ============================================================
echo.
set /p "LAUNCH= Launch Transcript Agent now? [Y/n]: "
if /i "!LAUNCH!" neq "n" goto :launch
goto :end

:: -- Update flow ---------------------------------------------------------------
:update
echo  Checking for updates...
echo.

:: Try git pull first
set "UPDATED=0"
where git >nul 2>&1
if %errorlevel%==0 (
    echo  Pulling latest code via git...
    git -C "!APPDIR!" pull 2>&1
    if !errorlevel!==0 (
        for /f "tokens=*" %%l in ('git -C "!APPDIR!" log -1 --format^="%%s"') do (
            if "%%l"=="Already up to date." (
                echo.
                echo  Already up to date. v!CURRENT_VERSION! is the latest version.
            ) else (
                echo.
                echo  Updated! Latest changes pulled.
                set "UPDATED=1"
            )
        )
    ) else (
        echo  git pull failed. Updating pip packages instead.
    )
) else (
    echo  git not found. Updating pip packages only.
)

echo.
echo  Updating Python packages...
"!PIP!" install -r "!APPDIR!requirements.txt" --upgrade --quiet
"!PIP!" install imageio-ffmpeg --upgrade --quiet
echo  Packages up to date.

echo.
if "!UPDATED!"=="1" (
    echo  Update applied successfully. Ready to launch.
) else (
    echo  Everything is up to date. v!CURRENT_VERSION! is the latest.
)
echo.
set /p "LAUNCH= Launch app now? [Y/n]: "
if /i "!LAUNCH!" neq "n" goto :launch
goto :end

:: -- Launch --------------------------------------------------------------------
:launch
echo.
echo  Starting Transcript Agent v!CURRENT_VERSION!...
echo  Browser will open automatically when ready.
echo  Press Ctrl+C to stop.
echo.

:: Background PowerShell poller - opens browser once server responds (max 60 s)
start /b "" powershell -NoProfile -WindowStyle Hidden -Command ^
  "for($i=0;$i-lt60;$i++){Start-Sleep 1;try{$null=Invoke-WebRequest http://127.0.0.1:7860 -UseBasicParsing -TimeoutSec 1;Start-Process 'http://localhost:7860';break}catch{}}"

:: Run app in this window so Ctrl+C stops it cleanly
"!VPYTHON!" "!APPDIR!app.py"
goto :end

:: -- Shortcut helper -----------------------------------------------------------
:make_shortcut
set "PSSCRIPT=%TEMP%\ta_shortcut_%RANDOM%.ps1"
(
    echo $sh = New-Object -ComObject WScript.Shell
    echo $desk = [Environment]::GetFolderPath^('Desktop'^)
    echo $lnk = $sh.CreateShortcut^("$desk\Transcript Agent.lnk"^)
    echo $lnk.TargetPath = '!APPDIR!run.bat'
    echo $lnk.WorkingDirectory = '!APPDIR!'
    echo $lnk.WindowStyle = 1
    echo $lnk.Save^(^)
) > "!PSSCRIPT!"
powershell -NoProfile -ExecutionPolicy Bypass -File "!PSSCRIPT!" >nul 2>&1
set "SC_ERR=%errorlevel%"
del "!PSSCRIPT!" >nul 2>&1
exit /b %SC_ERR%

:fail
echo.
echo  Setup failed. Fix the issue above and run this script again.
pause
exit /b 1

:end
endlocal
