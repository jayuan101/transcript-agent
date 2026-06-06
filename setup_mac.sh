#!/usr/bin/env bash
# ============================================================
#   Transcript Agent v2.2.2  |  macOS Installer
#   Run once to install, then double-click the Desktop launcher.
#   Run again at any time to update, repair, or fix GPU.
# ============================================================

set -euo pipefail

APPDIR="$(cd "$(dirname "$0")" && pwd)"
VENV="$APPDIR/venv"
VPYTHON="$VENV/bin/python"
PIP="$VENV/bin/pip"
CURRENT_VERSION="2.2.2"
APP_URL="http://localhost:7860"
GITHUB_REPO="jayuan101/transcript-agent"

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

    # Check GitHub for latest version
    LATEST_VER=""
    LATEST_VER=$(curl -sf "https://api.github.com/repos/${GITHUB_REPO}/releases/latest" 2>/dev/null \
        | grep '"tag_name"' | sed 's/.*"v\([^"]*\)".*/\1/' || true)

    # Detect GPU mismatch: Apple Silicon has MPS but torch is CPU-only
    GPU_MISMATCH=0
    _ARCH=$(uname -m)
    if [ "$_ARCH" = "arm64" ]; then
        if ! "$VPYTHON" -c "import torch; exit(0 if (hasattr(torch.backends,'mps') and torch.backends.mps.is_available()) else 1)" 2>/dev/null; then
            GPU_MISMATCH=1
        fi
    fi

    if [ "$GPU_MISMATCH" = "1" ]; then
        echo -e "${YELLOW}  *** GPU MISMATCH: Apple Silicon detected but PyTorch MPS is not active ***"
        echo -e "  *** Choose [5] to reinstall PyTorch with MPS support               ***${RESET}"
        echo ""
    fi

    if [ -n "$LATEST_VER" ] && [ "$LATEST_VER" != "$CURRENT_VERSION" ]; then
        echo -e "${YELLOW}  ★ UPDATE AVAILABLE: v${CURRENT_VERSION} → v${LATEST_VER}${RESET}"
        echo ""
        echo "    [1]  Launch app"
        echo -e "    [2]  ${BOLD}Update to v${LATEST_VER}${RESET}  ← new version available"
        echo "    [3]  Reinstall from scratch"
        echo "    [4]  Exit"
        echo "    [5]  Fix GPU (reinstall PyTorch with MPS/correct build)"
    else
        echo "    [1]  Launch app"
        echo "    [2]  Check for updates"
        echo "    [3]  Reinstall from scratch"
        echo "    [4]  Exit"
        echo "    [5]  Fix GPU (reinstall PyTorch with MPS/correct build)"
    fi
    echo ""
    read -r -p "  Enter choice [1-5]: " CHOICE
    echo ""
    case "${CHOICE:-1}" in
        2) ACTION=update   ;;
        3) ACTION=install  ;;
        4) exit 0          ;;
        5) ACTION=fix_gpu  ;;
        *) ACTION=launch   ;;
    esac
else
    ACTION=install
fi

# ── Fix GPU (reinstall PyTorch with correct build) ────────────────────────────
if [ "$ACTION" = fix_gpu ]; then
    header "Fix GPU — Reinstall PyTorch"
    _ARCH=$(uname -m)

    if [ "$_ARCH" = "arm64" ]; then
        info "Apple Silicon (arm64) detected."
        info "Uninstalling current PyTorch…"
        "$PIP" uninstall torch torchvision torchaudio -y 2>/dev/null || true
        info "Installing PyTorch with MPS GPU support…"
        "$PIP" install torch torchvision torchaudio
        echo ""
        info "Verifying MPS is available…"
        "$VPYTHON" -c "
import torch
mps = hasattr(torch.backends,'mps') and torch.backends.mps.is_available()
print('  MPS available:', mps)
print('  PyTorch:', torch.__version__)
if not mps:
    print('  Note: MPS not available — ensure macOS 12.3+ and run on Apple Silicon.')
"
    else
        info "Intel Mac detected — CPU build is correct, no GPU acceleration available."
        info "Current PyTorch version:"
        "$VPYTHON" -c "import torch; print(' ', torch.__version__)"
    fi

    echo ""
    success "Done! Restart the app to apply."
    echo ""
    read -r -p "  Launch app now? [Y/n]: " L
    [[ "${L:-y}" =~ ^[Nn]$ ]] && exit 0
    ACTION=launch
fi

# ── Update ────────────────────────────────────────────────────────────────────
if [ "$ACTION" = update ]; then
    header "Updating Transcript Agent…"

    if command -v git &>/dev/null && [ -d "$APPDIR/.git" ]; then
        info "Pulling latest code via git…"
        GIT_OUT=$(git -C "$APPDIR" pull 2>&1)
        if echo "$GIT_OUT" | grep -q "Already up to date"; then
            success "Code already up to date."
        else
            success "Code updated!"
            echo "  $GIT_OUT"
        fi
    else
        warn "git not found — updating packages only."
    fi

    info "Upgrading Python packages…"
    "$PIP" install --upgrade pip setuptools wheel --quiet
    "$PIP" install setuptools wheel --quiet
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
    "$PIP" install --upgrade pip setuptools wheel
    if [ $? -ne 0 ]; then
        warn "Could not upgrade pip/setuptools — retrying…"
        "$PIP" install setuptools wheel
    fi

    # 3 — PyTorch (auto-detect Apple Silicon MPS vs Intel CPU) ─────────────────
    header "[3/6] Installing PyTorch…"
    _ARCH=$(uname -m)
    if [ "$_ARCH" = "arm64" ]; then
        info "Apple Silicon (M1/M2/M3/M4) detected — installing PyTorch with MPS GPU support."
        info "MPS acceleration makes Whisper 3-5x faster than CPU."
        "$PIP" install torch torchvision torchaudio --quiet
        success "PyTorch installed with Apple MPS GPU support."
    else
        info "Intel Mac detected — installing CPU build (~700 MB)."
        if "$PIP" install torch --index-url https://download.pytorch.org/whl/cpu --quiet 2>/dev/null; then
            success "PyTorch installed (CPU build)."
        else
            warn "Index install failed — retrying with default index…"
            "$PIP" install torch --quiet
            success "PyTorch installed."
        fi
    fi

    # 4 — App requirements -----------------------------------------------------
    header "[4/6] Installing app requirements…"
    "$PIP" install setuptools wheel --quiet
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

    # 6 — Desktop launcher (.app bundle with icon) --------------------------------
    header "[6/6] Creating desktop launcher…"
    BAKED_APPDIR="$APPDIR"
    BAKED_VPYTHON="$VPYTHON"

    APP_BUNDLE="$HOME/Desktop/Transcript Agent.app"
    rm -rf "$APP_BUNDLE"
    mkdir -p "$APP_BUNDLE/Contents/MacOS"
    mkdir -p "$APP_BUNDLE/Contents/Resources"

    # Inner launch script
    cat > "$APP_BUNDLE/Contents/MacOS/TranscriptAgent" << CMDEOF
#!/usr/bin/env bash
APPDIR="${BAKED_APPDIR}"
VPYTHON="${BAKED_VPYTHON}"

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
    chmod +x "$APP_BUNDLE/Contents/MacOS/TranscriptAgent"

    # Info.plist
    cat > "$APP_BUNDLE/Contents/Info.plist" << PLISTEOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleName</key><string>Transcript Agent</string>
    <key>CFBundleDisplayName</key><string>Transcript Agent</string>
    <key>CFBundleExecutable</key><string>TranscriptAgent</string>
    <key>CFBundleIdentifier</key><string>com.transcriptagent.app</string>
    <key>CFBundleVersion</key><string>${CURRENT_VERSION}</string>
    <key>CFBundleIconFile</key><string>icon</string>
    <key>NSHighResolutionCapable</key><true/>
    <key>LSUIElement</key><false/>
</dict>
</plist>
PLISTEOF

    # Generate icon.icns and copy into the bundle
    "$PIP" install pillow --quiet 2>/dev/null || true
    if "$VPYTHON" "$APPDIR/create_icon.py" --mac 2>/dev/null && [ -f "$APPDIR/icon.icns" ]; then
        cp "$APPDIR/icon.icns" "$APP_BUNDLE/Contents/Resources/icon.icns"
    fi

    # Tell Finder to refresh the icon cache for this bundle
    touch "$APP_BUNDLE"

    success "Launcher created: ~/Desktop/Transcript Agent.app"
    info "Double-click it in Finder to start the app any time."

    echo ""
    echo -e "${GREEN}${BOLD}  ============================================================"
    echo -e "    Setup complete!  v${CURRENT_VERSION}"
    echo ""
    echo -e "    • Double-click 'Transcript Agent' app on your Desktop to start"
    echo -e "    • Or run:  bash \"${APPDIR}/setup_mac.sh\"  to update or fix GPU"
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
