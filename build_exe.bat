@echo off
title Building Transcript Agent .exe
echo.
echo ============================================
echo  Transcript Agent — Building .exe
echo ============================================
echo.

set PYTHON="C:\Users\Ja-Yuan Pendley\AppData\Local\Programs\Python\Python313\python.exe"

echo Cleaning previous build...
rmdir /s /q build 2>nul
rmdir /s /q dist 2>nul
del /f TranscriptAgent.spec 2>nul

echo.
echo Running PyInstaller...
%PYTHON% -m PyInstaller launcher.py ^
  --name TranscriptAgent ^
  --onefile ^
  --noconsole ^
  --clean ^
  --collect-all gradio ^
  --collect-all gradio_client ^
  --collect-all whisper ^
  --hidden-import app ^
  --hidden-import transcript_agent ^
  --hidden-import api ^
  --hidden-import pystray._win32 ^
  --hidden-import webview.platforms.edgechromium ^
  --hidden-import webview.platforms.winforms ^
  --hidden-import webview.platforms.mshtml ^
  --hidden-import anthropic ^
  --hidden-import openai ^
  --hidden-import fpdf ^
  --hidden-import pdfplumber ^
  --hidden-import docx ^
  --hidden-import psutil ^
  --hidden-import dotenv ^
  --hidden-import tiktoken ^
  --hidden-import bottle ^
  --hidden-import pythonnet ^
  --add-data "app.py;." ^
  --add-data "transcript_agent.py;." ^
  --add-data "api.py;." ^
  --add-data "icon.py;."

if errorlevel 1 (
    echo.
    echo ERROR: Build failed. See output above.
    pause
    exit /b 1
)

echo.
echo ============================================
echo  Build complete: dist\TranscriptAgent.exe
echo ============================================
echo.
pause
