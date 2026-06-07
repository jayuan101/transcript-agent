"""Build the Mac OTA launcher files for inclusion in the .dmg."""
import os, re, stat
from pathlib import Path

APP_VERSION = re.search(r'APP_VERSION\s*=\s*"([^"]+)"', Path("app.py").read_text(encoding="utf-8")).group(1)

OUT = Path("dist/mac_launcher")
OUT.mkdir(parents=True, exist_ok=True)

LAUNCHER = r"""#!/bin/bash
INSTALL_DIR="$HOME/Applications/TranscriptAgent"
APP_PATH="$INSTALL_DIR/TranscriptAgent.app"
VER_FILE="$INSTALL_DIR/version.txt"
CURRENT_VER=$(cat "$VER_FILE" 2>/dev/null || echo "0.0.0")
API="https://api.github.com/repos/jayuan101/transcript-agent/releases/latest"

LATEST=$(curl -sf --max-time 8 "$API" | grep '"tag_name"' | sed 's/.*"v\([^"]*\)".*/\1/')
ASSET_URL=$(curl -sf --max-time 8 "$API" | python3 -c \
  "import sys,json; d=json.load(sys.stdin); \
   print(next((a['browser_download_url'] for a in d.get('assets',[]) \
   if a['name']=='TranscriptAgent-Mac.zip'),''))" 2>/dev/null)

if [ -n "$LATEST" ] && [ "$LATEST" != "$CURRENT_VER" ] && [ -n "$ASSET_URL" ]; then
  osascript -e "display notification \"Updating to v$LATEST...\" with title \"Transcript Agent\""
  ZIP="/tmp/ta_mac_update.zip"
  curl -L "$ASSET_URL" -o "$ZIP" --silent
  mkdir -p "$INSTALL_DIR"
  unzip -o "$ZIP" -d "$INSTALL_DIR" -x "__MACOSX/*" > /dev/null 2>&1
  echo "$LATEST" > "$VER_FILE"
  rm -f "$ZIP"
  osascript -e "display notification \"Updated to v$LATEST - launching!\" with title \"Transcript Agent\""
fi

if [ -d "$APP_PATH" ]; then
  open "$APP_PATH"
else
  osascript -e "display dialog \"App not found at: $APP_PATH\n\nPlease reinstall from GitHub.\" with title \"Launch Error\" buttons {\"OK\"} default button 1"
fi
"""

COMMAND = r"""#!/bin/bash
bash "$HOME/Applications/TranscriptAgent/ta_launcher.sh"
"""

launcher_sh = OUT / "ta_launcher.sh"
launcher_sh.write_text(LAUNCHER, encoding="utf-8")
launcher_sh.chmod(launcher_sh.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

command_file = OUT / "Launch Transcript Agent.command"
command_file.write_text(COMMAND, encoding="utf-8")
command_file.chmod(command_file.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

version_file = OUT / "version.txt"
version_file.write_text(APP_VERSION, encoding="utf-8")

print(f"Mac OTA launcher built for v{APP_VERSION}")
print(f"  {launcher_sh}")
print(f"  {command_file}")
print(f"  {version_file}")
