@echo off
setlocal enabledelayedexpansion
title Transcript Agent - Setup
chcp 65001 >nul 2>&1

set "APPDIR=%~dp0"
set "VENV=%APPDIR%venv"
set "VPYTHON=%VENV%\Scripts\python.exe"
set "PIP=%VENV%\Scripts\pip.exe"
set "CURRENT_VERSION=2.0.0"

cls
echo.
echo  ============================================================
echo    Transcript Agent v%CURRENT_VERSION%  ^|  Windows Installer
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

:: Path length check ? warn if install path is too deep (Windows 260-char limit)
set "PATHLEN=0"
for /f %%i in ('echo !APPDIR!^| find /v /c ""') do set "PATHLEN=%%i"
if "!APPDIR:~80,1!" neq "" (
    echo.
    echo  WARNING: Install path is long: !APPDIR!
    echo  Deep paths cause failures on Windows ^(260-char limit^).
    echo  Recommended: move the folder to a short path such as C:\TranscriptAgent\
    echo.
    set /p "CONT= Continue anyway? [Y/n]: "
    if /i "!CONT!"=="n" goto :end
)

:: Step 1 - Detect Python (skip Windows Store stubs in WindowsApps)
echo  [1/5] Checking for Python 3.10+...

set "PY="

:: Check 'py' launcher first (most reliable on Windows)
where py >nul 2>&1
if %errorlevel%==0 (
    py -c "import sys; sys.exit(0 if sys.version_info>=(3,10) else 1)" >nul 2>&1
    if !errorlevel!==0 set "PY=py"
)

:: Check 'python' but skip Microsoft Store stubs (they live in WindowsApps)
if "!PY!"=="" (
    for /f "tokens=*" %%p in ('where python 2^>nul') do (
        if "!PY!"=="" (
            echo %%p | findstr /i "WindowsApps" >nul 2>&1
            if !errorlevel! neq 0 (
                "%%p" -c "import sys; sys.exit(0 if sys.version_info>=(3,10) else 1)" >nul 2>&1
                if !errorlevel!==0 set "PY=%%p"
            )
        )
    )
)

if "!PY!"=="" (
    echo.
    echo  ERROR: Python 3.10+ not found.
    echo.
    echo  If you have Python from the Microsoft Store, it may be a stub.
    echo  Install Python 3.10+ from the official site:
    echo    https://www.python.org/downloads/
    echo.
    echo  IMPORTANT: Tick "Add Python to PATH" during installation,
    echo             then run this setup again.
    echo.
    set /p "OPN= Open the download page now? [Y/n]: "
    if /i "!OPN!" neq "n" start "" "https://www.python.org/downloads/"
    goto :fail
)
for /f "tokens=*" %%v in ('"!PY!" --version 2^>^&1') do echo  Found: %%v

:: Warn if 32-bit Python on 64-bit OS (PyTorch may fail)
"!PY!" -c "import sys,struct; sys.exit(0 if struct.calcsize('P')==8 else 1)" >nul 2>&1
if %errorlevel% neq 0 (
    echo.
    echo  WARNING: 32-bit Python detected. PyTorch requires 64-bit Python.
    echo  Install 64-bit Python 3.10+ from https://www.python.org/downloads/
    goto :fail
)

:: Step 2 - Virtual environment
echo.
echo  [2/5] Setting up virtual environment...
if exist "!VPYTHON!" (
    echo  Already exists, skipping.
) else (
    "!PY!" -m venv "!VENV!"
    if %errorlevel% neq 0 ( echo  ERROR: Could not create venv. & goto :fail )
    echo  Created.
)

:: Step 3 - Install dependencies
echo.
echo  [3/5] Installing dependencies (first run: ~15 min, ~2 GB)...
echo.

"!VPYTHON!" -m pip install --upgrade pip --quiet 2>nul

echo   Installing PyTorch (CPU only - smaller download)...
"!PIP!" install torch --index-url https://download.pytorch.org/whl/cpu --quiet
if %errorlevel% neq 0 (
    echo   Retrying with default index...
    "!PIP!" install torch --quiet
)

echo   Installing app requirements...
"!PIP!" install -r "!APPDIR!requirements.txt" --quiet
if %errorlevel% neq 0 (
    echo.
    echo  ERROR: pip install failed.
    echo.
    echo  Common fixes:
    echo    - Corporate network / proxy: set HTTPS_PROXY=http://proxy:port
    echo      then run this setup again.
    echo    - Antivirus blocking: temporarily disable, then re-run.
    echo    - Long path limit: enable via Group Policy ^(gpedit.msc^) or:
    echo      reg add HKLM\SYSTEM\CurrentControlSet\Control\FileSystem /v LongPathsEnabled /t REG_DWORD /d 1 /f
    echo.
    goto :fail
)

echo   Installing bundled ffmpeg (no system install needed)...
"!PIP!" install imageio-ffmpeg --quiet

echo   Installing video analysis packages (mediapipe, opencv)...
"!PIP!" install mediapipe opencv-python plotly --quiet
if %errorlevel% neq 0 (
    echo   Note: Video analysis packages failed to install.
    echo   The app will still work for transcription and interview coaching.
    echo   To retry later: venv\Scripts\pip install mediapipe opencv-python plotly
)

echo   All dependencies installed.

:: Step 4 - API key
echo.
echo  [4/5] API key setup...
if exist "!APPDIR!.env" (
    echo  Found existing .env file. Skipping.
) else (
    echo  You need an AI provider API key to use this app.
    echo  Get a Claude key free at: https://console.anthropic.com
    echo.
    set /p "AKEY= Paste your Anthropic API key (or Enter to skip): "
    if "!AKEY!" neq "" (
        echo ANTHROPIC_API_KEY=!AKEY!>"!APPDIR!.env"
        echo  Saved to .env
    ) else (
        echo  Skipped. Enter your key inside the app.
    )
)

:: Step 5 - Desktop shortcut
echo.
echo  [5/5] Creating desktop shortcut...
call :make_shortcut
if %errorlevel%==0 (
    echo  Shortcut created on Desktop.
) else (
    echo  Note: Could not create shortcut. Use run.bat to launch.
)

echo.
echo  ============================================================
echo    Setup complete!  v%CURRENT_VERSION%
echo.
echo    Start anytime:  double-click "Transcript Agent" on Desktop
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

set "UPDATED=0"
where git >nul 2>&1
if %errorlevel%==0 (
    echo  Pulling latest code via git...
    git -C "!APPDIR!" pull 2>&1
    if !errorlevel!==0 set "UPDATED=1"
) else (
    echo  git not found - updating packages only.
)

echo.
echo  Updating Python packages...
"!VPYTHON!" -m pip install --upgrade pip --quiet 2>nul
"!PIP!" install -r "!APPDIR!requirements.txt" --upgrade --quiet
"!PIP!" install imageio-ffmpeg --upgrade --quiet
"!PIP!" install mediapipe opencv-python plotly --upgrade --quiet
echo  All packages up to date.

echo.
set /p "LAUNCH= Launch app now? [Y/n]: "
if /i "!LAUNCH!" neq "n" goto :launch
goto :end

:: -- Launch --------------------------------------------------------------------
:launch
echo.
echo  Starting Transcript Agent v%CURRENT_VERSION%...
echo  Browser will open automatically when ready.
echo  Press Ctrl+C to stop.
echo.

start /b "" powershell -NoProfile -WindowStyle Hidden -Command ^
  "for($i=0;$i-lt60;$i++){Start-Sleep 1;try{$null=Invoke-WebRequest http://127.0.0.1:7860 -UseBasicParsing -TimeoutSec 1;Start-Process 'http://localhost:7860';break}catch{}}"

"%VPYTHON%" "%APPDIR%app.py"
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
