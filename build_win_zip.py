"""Rebuild the Windows distribution zip with latest source files."""
import zipfile
from pathlib import Path

out_zip = Path("dist/TranscriptAgent-Windows.zip")
out_zip.parent.mkdir(exist_ok=True)

files = {
    "app.py":              "TranscriptAgent/app.py",
    "transcript_agent.py": "TranscriptAgent/transcript_agent.py",
    "requirements.txt":    "TranscriptAgent/requirements.txt",
    "setup_windows.bat":   "TranscriptAgent/setup_windows.bat",
    "run.bat":             "TranscriptAgent/run.bat",
    "launcher.py":         "TranscriptAgent/launcher.py",
    "CHANGELOG.md":        "TranscriptAgent/CHANGELOG.md",
}

readme = (
    "Transcript Agent v1.1.82 - Windows Package\n"
    "==========================================\n\n"
    "QUICK START\n"
    "-----------\n"
    "1. Double-click  setup_windows.bat   (first time only - installs everything)\n"
    "2. Double-click  run.bat             (every time after)\n\n"
    "REQUIREMENTS\n"
    "------------\n"
    "* Python 3.10-3.13 from https://www.python.org/downloads/\n"
    "  Tick 'Add Python to PATH' during install.\n"
    "* Internet connection for first-time pip install (~2 GB).\n\n"
    "WHAT GETS INSTALLED\n"
    "-------------------\n"
    "* Python virtual environment in the venv/ folder\n"
    "* All dependencies (gradio, anthropic, openai, whisper, ffmpeg, etc.)\n"
    "* Desktop shortcut 'Transcript Agent'\n\n"
    "NOTES\n"
    "-----\n"
    "* Your API keys are stored only in .env - never on any server.\n"
    "* To update: run setup_windows.bat and choose [2] Check for updates.\n"
)

with zipfile.ZipFile(out_zip, "w", compression=zipfile.ZIP_DEFLATED) as zf:
    for src_file, arc_name in files.items():
        zf.write(src_file, arc_name)
        print(f"  + {arc_name}")
    zf.writestr("TranscriptAgent/README.txt", readme)
    print("  + TranscriptAgent/README.txt")

print(f"\nBuilt: {out_zip}  ({out_zip.stat().st_size / 1024:.0f} KB)")
