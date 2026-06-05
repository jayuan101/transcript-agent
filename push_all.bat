@echo off
setlocal
title Transcript Agent - Push to All

echo.
echo  ============================================================
echo    Transcript Agent - Push to ALL remotes
echo    GitHub + Hugging Face + Rebuild Mac^&Windows packages
echo  ============================================================
echo.

:: ── 1. Rebuild Mac and Windows packages ─────────────────────────────────────
echo [1/4] Rebuilding Windows package...
venv\Scripts\python.exe build_win_zip.py
if errorlevel 1 ( echo ERROR: Windows build failed & pause & exit /b 1 )

echo.
echo [2/4] Rebuilding Mac package...
venv\Scripts\python.exe build_mac_zip.py
if errorlevel 1 ( echo ERROR: Mac build failed & pause & exit /b 1 )

:: ── 2. Stage and commit if there are changes ─────────────────────────────────
echo.
echo [3/4] Staging changes...
git add -f dist\TranscriptAgent-Windows.zip dist\TranscriptAgent-Mac.zip
git add app.py transcript_agent.py video_analyzer.py api.py requirements.txt
git add setup_windows.bat setup_mac.sh launcher.py CHANGELOG.md README.md
git add TranscriptAgent.spec build_win_zip.py build_mac_zip.py

git diff --cached --quiet
if errorlevel 1 (
    set /p MSG="  Enter commit message: "
    git commit -m "%MSG%"
    if errorlevel 1 ( echo ERROR: Commit failed & pause & exit /b 1 )
) else (
    echo   Nothing to commit - pushing existing HEAD.
)

:: ── 3. Push to all remotes ────────────────────────────────────────────────────
echo.
echo [4/4] Pushing to GitHub + Hugging Face...
git push all main
if errorlevel 1 (
    echo   'all' remote push failed - trying individually...
    git push github main
    git push hf main
    git push origin main
)

echo.
echo  ============================================================
echo    Done! Pushed to all remotes.
echo    GitHub Actions will auto-build Docker + sync to HF.
echo  ============================================================
echo.
pause
