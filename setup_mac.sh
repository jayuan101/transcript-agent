#!/usr/bin/env bash
# ============================================================
#   Transcript Agent v1.1.87  |  macOS Installer
#   Run once to install, then double-click the Desktop launcher.
#   Run again at any time to update or repair.
# ============================================================

set -euo pipefail

APPDIR="$(cd "$(dirname "$0")" && pwd)"
VENV="$APPDIR/venv"
VPYTHON="$VENV/bin/python"
PIP="$VENV/bin/pip"
CURRENT_VERSION="2.0.0"
APP_URL="http://localhost:7860"

# ── Colour helpers ─────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
CYAN='\033[0;36m'; BOLD='\033[1m'; RESET='\033[0m'

info()    { echo -e "${CYAN}  $*${RESET}"; }
success() { echo -e "${GREEN}  ✓ $*${RESET}"; }
warn()    { echo -e "${YELLOW}  ⚠ $*${RESET}"; }
err()     { echo -e "${RED}  ✗ $*${RESET}" >&2; }
header()  { echo -e "\n${BOLD}  $*${RESET}\n"; }

clear
echo ""
echo -e "${BOLD}  ============================================================"
echo -e "    Transcript Agent v${CURRENT_VERSION}  |  macOS Installer"
echo -e "  ============================================================${RESET}"
echo ""

# ── Already installed? Show menu ──────────────────────────────────────────────
if [ -f "$VPYTHON" ]; then
    info "Existing installation found (v${CURRENT_VERSION})."
    echo ""
    echo "    [1]  Launch app"
    echo "    [2]  Check for updates"
    echo "    [3]  Reinstall from scratch"
    echo "    [4]  Exit"
    echo ""
    read -r -p "  Enter choice [1-4]: " CHOICE
    echo ""
    case "${CHOICE:-1}" in
        2) ACTION=update  ;;
        3) ACTION=install ;;
        4) exit 0         ;;
        *) ACTION=launch  ;;
    esac
else
    ACTION=install
fi

# ── Update ────────────────────────────────────────────────────────────────────
if [ "$ACTION" = update ]; then
    header "Checking for updates…"

    if command -v git &>/dev/null && [ -d "$APPDIR/.git" ]; then
        info "Pulling latest code via git…"
        GIT_OUT=$(git -C "$APPDIR" pull 2>&1)
        if echo "$GIT_OUT" | grep -q "Already up to date"; then
            success "Already up to date — v${CURRENT_VERSION}"
        else
            success "Updated!"; echo "  $GIT_OUT"
        fi
    else
        warn "git not found — updating packages only."
    fi

    info "Upgrading Python packages…"
    "$PIP" install --upgrade pip --quiet
    "$PIP" install -r "$APPDIR/requirements.txt" --upgrade --quiet
    "$PIP" install imageio-ffmpeg --upgrade --quiet
    success "All packages up to date."

    echo ""
    read -r -p "  Launch app now? [Y/n]: " L
    [[ "${L:-y}" =~ ^[Nn]$ ]] && exit 0
    ACTION=launch
fi

# ── Fresh install ─────────────────────────────────────────────────────────────
if [ "$ACTION" = install ]; then

    # 1 — Python ---------------------------------------------------------------
    header "[1/6] Checking for Python 3.10+…"
    PY=""
    for cmd in python3.13 python3.12 python3.11 python3.10 python3 python; do
        if command -v "$cmd" &>/dev/null; then
            if "$cmd" -c "import sys; sys.exit(0 if sys.version_info>=(3,10) else 1)" 2>/dev/null; then
                PY="$cmd"; break
            fi
        fi
    done

    if [ -z "$PY" ]; then
        err "Python 3.10+ not found."
        echo ""
        echo "  Install options:"
        echo "    • Official:  https://www.python.org/downloads/"
        echo "    • Homebrew:  brew install python"
        echo ""
        read -r -p "  Open python.org now? [Y/n]: " O
        [[ "${O:-y}" =~ ^[Nn]$ ]] || open "https://www.python.org/downloads/"
        exit 1
    fi
    success "Found: $("$PY" --version 2>&1)"

    # 2 — Venv -----------------------------------------------------------------
    header "[2/6] Setting up virtual environment…"
    if [ -f "$VENV/bin/activate" ]; then
        success "Already exists — skipping."
    else
        "$PY" -m venv "$VENV"
        success "Created."
    fi
    "$PIP" install --upgrade pip --quiet

    # 3 — PyTorch --------------------------------------------------------------
    header "[3/6] Installing PyTorch (CPU)…"
    info "This may take 5–10 minutes on first install (~700 MB)."
    if "$PIP" install torch --index-url https://download.pytorch.org/whl/cpu --quiet 2>/dev/null; then
        success "PyTorch installed (CPU build)."
    else
        warn "Index install failed — retrying with default index…"
        "$PIP" install torch --quiet
        success "PyTorch installed."
    fi

    # 4 — App requirements -----------------------------------------------------
    header "[4/6] Installing app requirements…"
    "$PIP" install -r "$APPDIR/requirements.txt" --quiet
    "$PIP" install imageio-ffmpeg --quiet
    success "All Python packages installed."

    if ! command -v ffmpeg &>/dev/null; then
        if command -v brew &>/dev/null; then
            echo ""
            read -r -p "  Install system ffmpeg via Homebrew (better video support)? [Y/n]: " BF
            if [[ ! "${BF:-y}" =~ ^[Nn]$ ]]; then
                brew install ffmpeg --quiet && success "ffmpeg installed."
            fi
        fi
    else
        success "ffmpeg already available."
    fi

    # 5 — API key --------------------------------------------------------------
    header "[5/6] API key setup…"
    if [ -f "$APPDIR/.env" ]; then
        success "Found existing .env — skipping."
    else
        echo "  You need an AI provider API key to use this app."
        echo "  Get a Claude key free at: https://console.anthropic.com"
        echo ""
        read -r -p "  Paste your Anthropic API key (or Enter to skip): " AKEY
        if [ -n "$AKEY" ]; then
            printf 'ANTHROPIC_API_KEY=%s\n' "$AKEY" > "$APPDIR/.env"
            success "Saved to .env"
        else
            info "Skipped — enter your key inside the app."
        fi
    fi

    # 6 — Desktop launcher -----------------------------------------------------
    header "[6/6] Creating desktop launcher…"
    LAUNCHER="$HOME/Desktop/Transcript Agent.command"
    # Use single-quoted heredoc so variables are literal in the output file,
    # except APPDIR and VPYTHON which we expand now to bake in absolute paths.
    BAKED_APPDIR="$APPDIR"
    BAKED_VPYTHON="$VPYTHON"
    cat > "$LAUNCHER" << CMDEOF
#!/usr/bin/env bash
# Transcript Agent v${CURRENT_VERSION} — desktop launcher
APPDIR="${BAKED_APPDIR}"
VPYTHON="${BAKED_VPYTHON}"

echo ""
echo "  Starting Transcript Agent v${CURRENT_VERSION}…"
echo "  Browser will open at http://localhost:7860"
echo "  Press Ctrl+C to stop."
echo ""

"\$VPYTHON" "\$APPDIR/app.py" &
APP_PID=\$!

for i in \$(seq 1 60); do
    sleep 1
    if curl -s --max-time 1 http://127.0.0.1:7860/ >/dev/null 2>&1; then
        open "http://localhost:7860" 2>/dev/null || true
        break
    fi
done

wait \$APP_PID
CMDEOF
    chmod +x "$LAUNCHER"
    success "Launcher created: ~/Desktop/Transcript Agent.command"
    info "Double-click it in Finder to start the app any time."

    echo ""
    echo -e "${GREEN}${BOLD}  ============================================================"
    echo -e "    Setup complete!  v${CURRENT_VERSION}"
    echo ""
    echo -e "    • Double-click 'Transcript Agent' on your Desktop to start"
    echo -e "    • Or run:  bash \"${APPDIR}/setup_mac.sh\"  to update"
    echo -e "  ============================================================${RESET}"
    echo ""
    read -r -p "  Launch Transcript Agent now? [Y/n]: " L
    [[ "${L:-y}" =~ ^[Nn]$ ]] && exit 0
    ACTION=launch
fi

# ── Launch ────────────────────────────────────────────────────────────────────
if [ "$ACTION" = launch ]; then
    echo ""
    info "Starting Transcript Agent v${CURRENT_VERSION}…"
    info "Browser will open at ${APP_URL}"
    echo "  Press Ctrl+C to stop."
    echo ""

    "$VPYTHON" "$APPDIR/app.py" &
    APP_PID=$!

    for i in $(seq 1 60); do
        sleep 1
        if curl -s --max-time 1 http://127.0.0.1:7860/ >/dev/null 2>&1; then
            open "$APP_URL" 2>/dev/null || true
            break
        fi
    done

    wait $APP_PID
fi
