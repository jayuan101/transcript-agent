@echo off
setlocal enabledelayedexpansion
title Transcript Agent [DEV]
cd /d "%~dp0"

:: Kill any old dev instance on port 7861
for /f "tokens=5" %%a in ('netstat -ano 2^>nul ^| findstr ":7861.*LISTENING"') do (
    taskkill /PID %%a /F >nul 2>&1
)

echo Starting Transcript Agent DEV on port 7861...
echo Source: %~dp0app.py
echo.

:: ── GPU Detection (same as run.bat) ───────────────────────────────────────────
set "GPU_INFO=CPU (no GPU acceleration)"
set "GPU_ENV=cpu"

where nvidia-smi >nul 2>&1
if %errorlevel%==0 (
    for /f "tokens=1,2 delims=," %%a in ('nvidia-smi --query-gpu=name^,memory.total --format=csv^,noheader 2^>nul') do (
        set "GPU_INFO=NVIDIA %%a (%%b) — CUDA"
        set "GPU_ENV=cuda"
        goto :show_gpu
    )
)

"C:\Users\young\AppData\Local\TranscriptAgent\venv\Scripts\python.exe" -c "import torch_directml; d=torch_directml.device(); print('dml')" >"%TEMP%\ta_gpu_dev.tmp" 2>nul
set /p GPU_CHECK=<"%TEMP%\ta_gpu_dev.tmp"
del "%TEMP%\ta_gpu_dev.tmp" >nul 2>&1
if "!GPU_CHECK!"=="dml" (
    for /f "tokens=2 delims==" %%g in ('wmic path win32_VideoController get Name /value 2^>nul ^| findstr /i "AMD\|Intel\|Arc\|Radeon"') do (
        set "GPU_INFO=%%g (DirectML)"
        set "GPU_ENV=dml"
        goto :show_gpu
    )
    set "GPU_INFO=AMD/Intel GPU (DirectML)"
    set "GPU_ENV=dml"
    goto :show_gpu
)

"C:\Users\young\AppData\Local\TranscriptAgent\venv\Scripts\python.exe" -c "import torch; print('mps' if (hasattr(torch.backends,'mps') and torch.backends.mps.is_available()) else '')" >"%TEMP%\ta_gpu_dev.tmp" 2>nul
set /p MPS_CHECK=<"%TEMP%\ta_gpu_dev.tmp"
del "%TEMP%\ta_gpu_dev.tmp" >nul 2>&1
if "!MPS_CHECK!"=="mps" (
    set "GPU_INFO=Apple Silicon (MPS)"
    set "GPU_ENV=mps"
)

:show_gpu
echo  GPU: !GPU_INFO!
echo.

:: ── Dev environment vars ──────────────────────────────────────────────────────
set TA_DEV_MODE=1
set GRADIO_SERVER_PORT=7861
set GRADIO_SERVER_NAME=127.0.0.1
set GRADIO_ANALYTICS_ENABLED=False
set TA_GPU_DEVICE=!GPU_ENV!

:: Use the installed app venv (has all deps including Whisper, DeepFace, etc.)
"C:\Users\young\AppData\Local\TranscriptAgent\venv\Scripts\python.exe" app.py

pause
endlocal
