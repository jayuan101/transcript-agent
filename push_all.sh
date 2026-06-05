#!/usr/bin/env bash
# ============================================================
#   Transcript Agent — Push to ALL remotes (Mac/Linux)
#   Rebuilds Mac & Windows packages then pushes to GitHub + HF
# ============================================================
set -euo pipefail
APPDIR="$(cd "$(dirname "$0")" && pwd)"
cd "$APPDIR"

GREEN='\033[0;32m'; CYAN='\033[0;36m'; BOLD='\033[1m'; RESET='\033[0m'
info()    { echo -e "${CYAN}  $*${RESET}"; }
success() { echo -e "${GREEN}  ✓ $*${RESET}"; }

echo ""
echo -e "${BOLD}  ============================================================"
echo -e "    Transcript Agent — Push to ALL remotes"
echo -e "  ============================================================${RESET}"
echo ""

# ── 1. Rebuild packages ───────────────────────────────────────────────────────
info "[1/4] Rebuilding Windows package..."
venv/bin/python build_win_zip.py
success "Windows package built."

info "[2/4] Rebuilding Mac package..."
venv/bin/python build_mac_zip.py
success "Mac package built."

# ── 2. Stage + commit if needed ──────────────────────────────────────────────
info "[3/4] Staging changes..."
git add -f dist/TranscriptAgent-Windows.zip dist/TranscriptAgent-Mac.zip
git add app.py transcript_agent.py video_analyzer.py api.py requirements.txt \
        setup_windows.bat setup_mac.sh launcher.py CHANGELOG.md README.md \
        TranscriptAgent.spec build_win_zip.py build_mac_zip.py 2>/dev/null || true

if ! git diff --cached --quiet; then
    echo -n "  Enter commit message: "
    read -r MSG
    git commit -m "$MSG"
    success "Committed."
else
    info "Nothing to commit — pushing existing HEAD."
fi

# ── 3. Push to all remotes ────────────────────────────────────────────────────
info "[4/4] Pushing to GitHub + Hugging Face..."
git push all main 2>/dev/null || {
    info "'all' remote push failed — trying individually..."
    git push github main
    git push hf main
    git push origin main
}

echo ""
echo -e "${GREEN}${BOLD}  ============================================================"
echo -e "    Done! Pushed to all remotes."
echo -e "    GitHub Actions will auto-build Docker + sync to HF."
echo -e "  ============================================================${RESET}"
echo ""
