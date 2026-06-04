"""Build the Mac distribution zip with latest source files."""
import zipfile
from pathlib import Path

APP_VERSION = "1.1.87"

out_zip = Path("dist/TranscriptAgent-Mac.zip")
out_zip.parent.mkdir(exist_ok=True)

files = {
    "app.py":              "TranscriptAgent/app.py",
    "transcript_agent.py": "TranscriptAgent/transcript_agent.py",
    "requirements.txt":    "TranscriptAgent/requirements.txt",
    "setup_mac.sh":        "TranscriptAgent/setup_mac.sh",
    "launcher.py":         "TranscriptAgent/launcher.py",
    "CHANGELOG.md":        "TranscriptAgent/CHANGELOG.md",
}

readme = (
    f"Transcript Agent v{APP_VERSION} - Mac Package\n"
    "=============================================\n\n"
    "QUICK START\n"
    "-----------\n"
    "1. Open Terminal in this folder\n"
    "2. Run:  bash setup_mac.sh   (first time only - installs everything)\n"
    "3. Double-click 'Transcript Agent' on your Desktop to launch\n\n"
    "REQUIREMENTS\n"
    "------------\n"
    "* macOS 11 (Big Sur) or later\n"
    "* Python 3.10-3.13 from https://www.python.org/downloads/\n"
    "* Internet connection for first-time install (~2 GB)\n\n"
    "WHAT GETS INSTALLED\n"
    "-------------------\n"
    "* Python virtual environment in the venv/ folder\n"
    "* All dependencies (gradio, anthropic, openai, whisper, ffmpeg, etc.)\n"
    "* Desktop launcher 'Transcript Agent.command'\n\n"
    "NOTES\n"
    "-----\n"
    "* Your API keys are stored only in .env - never on any server.\n"
    "* To update: run setup_mac.sh and choose [2] Check for updates.\n"
)

with zipfile.ZipFile(out_zip, "w", compression=zipfile.ZIP_DEFLATED) as zf:
    for src_file, arc_name in files.items():
        zf.write(src_file, arc_name)
        print(f"  + {arc_name}")
    zf.writestr("TranscriptAgent/README.txt", readme)
    print("  + TranscriptAgent/README.txt")

print(f"\nBuilt: {out_zip}  ({out_zip.stat().st_size / 1024:.0f} KB)")
