@echo off
setlocal enabledelayedexpansion
title Transcript Agent

set "APPDIR=%~dp0"

:: Read version from app.py — single source of truth
set "APP_VER=2.2.3"
for /f "tokens=*" %%l in ('findstr "^APP_VERSION = " "%APPDIR%app.py" 2^>nul') do (
    for /f "tokens=3 delims= ^"" %%v in ("%%l") do set "APP_VER=%%v"
)

cls
echo.
echo  ============================================================
echo    Transcript Agent v!APP_VER!
echo  ============================================================
echo.

:: ── Locate Python ─────────────────────────────────────────────────────────────
set "PYTHON="
if exist "%APPDIR%venv\Scripts\python.exe" (
    set "PYTHON=%APPDIR%venv\Scripts\python.exe"
    goto :detect_gpu
)
where python >nul 2>&1
if %errorlevel%==0 ( set "PYTHON=python" & goto :detect_gpu )
where py >nul 2>&1
if %errorlevel%==0 ( set "PYTHON=py"     & goto :detect_gpu )

echo  ERROR: Python not found. Run setup_windows.bat first.
echo.
pause
exit /b 1

:: ── GPU Detection ─────────────────────────────────────────────────────────────
:detect_gpu
set "GPU_INFO=CPU (no GPU acceleration)"
set "GPU_ENV=cpu"

:: Check NVIDIA first
where nvidia-smi >nul 2>&1
if %errorlevel%==0 (
    for /f "tokens=1,2 delims=," %%a in ('nvidia-smi --query-gpu=name^,memory.total --format=csv^,noheader 2^>nul') do (
        set "GPU_INFO=NVIDIA %%a (%%b) — CUDA"
        set "GPU_ENV=cuda"
        goto :show_gpu
    )
)

:: Check if torch-directml installed (AMD / Intel DirectML)
"%PYTHON%" -c "import torch_directml; d=torch_directml.device(); print('dml')" >"%TEMP%\ta_gpu.tmp" 2>nul
set /p GPU_CHECK=<"%TEMP%\ta_gpu.tmp"
del "%TEMP%\ta_gpu.tmp" >nul 2>&1
if "!GPU_CHECK!"=="dml" (
    :: Get GPU name from wmic
    for /f "tokens=2 delims==" %%g in ('wmic path win32_VideoController get Name /value 2^>nul ^| findstr /i "AMD\|Intel\|Arc\|Radeon"') do (
        set "GPU_INFO=%%g (DirectML)"
        set "GPU_ENV=dml"
        goto :show_gpu
    )
    set "GPU_INFO=AMD/Intel GPU (DirectML)"
    set "GPU_ENV=dml"
    goto :show_gpu
)

:: Check Apple Silicon MPS (in case running on a Mac via this bat)
"%PYTHON%" -c "import torch; print('mps' if (hasattr(torch.backends,'mps') and torch.backends.mps.is_available()) else '')" >"%TEMP%\ta_gpu.tmp" 2>nul
set /p MPS_CHECK=<"%TEMP%\ta_gpu.tmp"
del "%TEMP%\ta_gpu.tmp" >nul 2>&1
if "!MPS_CHECK!"=="mps" (
    set "GPU_INFO=Apple Silicon (MPS)"
    set "GPU_ENV=mps"
)

:show_gpu
echo  GPU: !GPU_INFO!
if "!GPU_ENV!"=="cpu" (
    echo  Tip: Install CUDA ^(NVIDIA^) or DirectML ^(AMD/Intel^) for 5-10x faster transcription.
)
echo.
echo  Browser will open automatically when ready.
echo  Press Ctrl+C in this window to stop the app.
echo.

:: ── Set GPU env var so app.py reads it on startup ────────────────────────────
set "TA_GPU_DEVICE=!GPU_ENV!"

:: Background poller opens browser once server responds (up to 60 s)
start /b "" powershell -NoProfile -WindowStyle Hidden -Command ^
  "for($i=0;$i-lt60;$i++){Start-Sleep 1;try{$null=Invoke-WebRequest http://127.0.0.1:7860 -UseBasicParsing -TimeoutSec 1;Start-Process 'http://localhost:7860';break}catch{}}"

:: Run app
"%PYTHON%" "%APPDIR%app.py"

endlocal
