#!/bin/bash
# ============================================================
#   Transcript Agent — Mac Installer
#   No Docker required. Runs entirely on your Mac.
# ============================================================

set -e
APPDIR="$(cd "$(dirname "$0")" && pwd)"
VENV="$APPDIR/venv"
VPYTHON="$VENV/bin/python"
PIP="$VENV/bin/pip"
CURRENT_VERSION="2.3"

echo ""
echo "  ============================================================"
echo "    Transcript Agent  |  Mac Installer"
echo "  ============================================================"
echo ""

# ── Already installed? Show menu ─────────────────────────────────────────────
if [ -f "$VPYTHON" ]; then
    echo "  Existing installation found."
    echo ""
    echo "    [1]  Launch app"
    echo "    [2]  Check for updates"
    echo "    [3]  Reinstall from scratch"
    echo "    [4]  Exit"
    echo ""
    read -r -p "  Enter choice [1-4]: " CHOICE
    echo ""
    case "$CHOICE" in
        1) goto_launch=1 ;;
        2) goto_update=1 ;;
        3) goto_install=1 ;;
        4) exit 0 ;;
        *) goto_launch=1 ;;
    esac
else
    goto_install=1
fi

# ── Update flow ───────────────────────────────────────────────────────────────
if [ "${goto_update:-0}" = "1" ]; then
    echo "  Checking for updates..."
    echo ""

    UPDATED=0
    if command -v git &>/dev/null && [ -d "$APPDIR/.git" ]; then
        echo "  Pulling latest code via git..."
        GIT_OUT=$(git -C "$APPDIR" pull 2>&1)
        echo "  $GIT_OUT"
        if echo "$GIT_OUT" | grep -q "Already up to date"; then
            echo ""
            echo "  Already up to date. v$CURRENT_VERSION is the latest version."
        else
            echo ""
            echo "  Updated! Latest changes pulled."
            UPDATED=1
        fi
    else
        echo "  git not found or not a git repo. Updating pip packages only."
    fi

    echo ""
    echo "  Updating Python packages..."
    "$PIP" install -r "$APPDIR/requirements.txt" --upgrade --quiet
    "$PIP" install imageio-ffmpeg --upgrade --quiet
    echo "  Packages up to date."

    echo ""
    if [ "$UPDATED" = "1" ]; then
        echo "  Update applied successfully."
    else
        echo "  Everything is up to date. v$CURRENT_VERSION is the latest."
    fi
    echo ""
    read -r -p "  Launch app now? [Y/n]: " LAUNCH
    [[ "$LAUNCH" =~ ^[Nn]$ ]] && exit 0 || goto_launch=1
fi

# ── Fresh install ─────────────────────────────────────────────────────────────
if [ "${goto_install:-0}" = "1" ]; then

    # Step 1 — Python
    echo "  [1/5] Checking for Python 3.9+..."
    PY=""
    for cmd in python3 python3.13 python3.12 python3.11 python3.10 python3.9; do
        if command -v "$cmd" &>/dev/null; then PY="$cmd"; break; fi
    done

    if [ -z "$PY" ]; then
        echo ""
        echo "  ERROR: Python 3.9+ not found."
        echo "  Install from: https://www.python.org/downloads/"
        echo "  Or via Homebrew: brew install python"
        exit 1
    fi

    if ! "$PY" -c "import sys; sys.exit(0 if sys.version_info>=(3,9) else 1)" 2>/dev/null; then
        echo "  ERROR: Python 3.9+ required. Found: $($PY --version 2>&1)"
        exit 1
    fi
    echo "  Found: $($PY --version 2>&1)"

    # Step 2 — Venv
    echo ""
    echo "  [2/5] Setting up virtual environment..."
    if [ -f "$VENV/bin/activate" ]; then
        echo "  Already exists, skipping."
    else
        "$PY" -m venv "$VENV"
        echo "  Created: $VENV"
    fi

    # Step 3 — Dependencies
    echo ""
    echo "  [3/5] Installing dependencies..."
    echo "  First run downloads ~2 GB and takes 5-15 minutes."
    echo ""

    "$PIP" install --upgrade pip --quiet

    echo "  Installing PyTorch (CPU)..."
    "$PIP" install torch torchvision torchaudio \
        --index-url https://download.pytorch.org/whl/cpu --quiet || \
        "$PIP" install torch --quiet

    echo "  Installing app requirements..."
    "$PIP" install -r "$APPDIR/requirements.txt" --quiet

    echo "  Installing bundled ffmpeg..."
    "$PIP" install imageio-ffmpeg --quiet

    # Optional Homebrew ffmpeg
    if ! command -v ffmpeg &>/dev/null; then
        if command -v brew &>/dev/null; then
            read -r -p "  Install ffmpeg via Homebrew for best video support? [Y/n]: " BREWFF
            [[ "$BREWFF" =~ ^[Nn]$ ]] || brew install ffmpeg --quiet
        fi
    fi
    echo "  All dependencies installed."

    # Step 4 — API key
    echo ""
    echo "  [4/5] API key setup..."
    if [ -f "$APPDIR/.env" ]; then
        echo "  Found existing .env. Skipping."
    else
        echo "  Get a free key at: https://console.anthropic.com"
        echo ""
        read -r -p "  Paste your Anthropic API key (or Enter to skip): " AKEY
        if [ -n "$AKEY" ]; then
            echo "ANTHROPIC_API_KEY=$AKEY" > "$APPDIR/.env"
            echo "  Saved to .env"
        else
            echo "  Skipped. Enter your key inside the app."
        fi
    fi

    # Step 5 — Desktop launcher
    echo ""
    echo "  [5/5] Creating desktop launcher..."
    LAUNCHER="$HOME/Desktop/Transcript Agent.command"
    cat > "$LAUNCHER" << CMDEOF
#!/bin/bash
cd "$APPDIR"
"$VPYTHON" "$APPDIR/app.py" &
APP_PID=\$!
sleep 8 && open "http://localhost:7860"
wait \$APP_PID
CMDEOF
    chmod +x "$LAUNCHER"
    echo "  Launcher: ~/Desktop/Transcript Agent.command"
    echo "  Double-click it in Finder to start the app."

    echo ""
    echo "  ============================================================"
    echo "    Setup complete! v$CURRENT_VERSION"
    echo ""
    echo "    To start later: double-click 'Transcript Agent' on Desktop"
    echo "  ============================================================"
    echo ""
    read -r -p "  Launch Transcript Agent now? [Y/n]: " LAUNCH
    [[ "$LAUNCH" =~ ^[Nn]$ ]] && exit 0 || goto_launch=1
fi

# ── Launch ─────────────────────────────────────────────────────────────────────
if [ "${goto_launch:-0}" = "1" ]; then
    echo ""
    echo "  Starting Transcript Agent..."
    echo "  Browser will open at http://localhost:7860"
    echo "  Press Ctrl+C to stop."
    echo ""
    "$VPYTHON" "$APPDIR/app.py" &
    sleep 8
    open "http://localhost:7860" 2>/dev/null || true
    wait
fi
