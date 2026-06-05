"""Rebuild the Windows distribution zip with latest source files."""
import zipfile
from pathlib import Path

out_zip = Path("dist/TranscriptAgent-Windows.zip")
out_zip.parent.mkdir(exist_ok=True)

files = {
    "app.py":              "TranscriptAgent/app.py",
    "transcript_agent.py": "TranscriptAgent/transcript_agent.py",
    "video_analyzer.py":   "TranscriptAgent/video_analyzer.py",
    "api.py":              "TranscriptAgent/api.py",
    "requirements.txt":    "TranscriptAgent/requirements.txt",
    "setup_windows.bat":   "TranscriptAgent/setup_windows.bat",
    "run.bat":             "TranscriptAgent/run.bat",
    "launcher.py":         "TranscriptAgent/launcher.py",
    "CHANGELOG.md":        "TranscriptAgent/CHANGELOG.md",
    "README.md":           "TranscriptAgent/README.md",
}

readme = (
    "Transcript Agent v2.1.1 - Windows Package\n"
    "==========================================\n\n"
    "QUICK START\n"
    "-----------\n"
    "1. Install Python 3.10-3.13 from https://www.python.org/downloads/\n"
    "   IMPORTANT: Tick 'Add Python to PATH' during install.\n"
    "2. Double-click  setup_windows.bat   (first time only - installs everything)\n"
    "3. Double-click  run.bat             (every time after)\n\n"
    "WHAT'S INCLUDED\n"
    "---------------\n"
    "* Transcription: 9 STT engines including Whisper (local/offline)\n"
    "* Interview coaching: question scoring, coaching tips, advancement likelihood\n"
    "* Video Analysis: upload a recorded interview for per-person emotion,\n"
    "  eye contact, body language, and cultural style scoring\n"
    "* Live Interview: real-time webcam analysis with live score panel\n"
    "* AI providers: Claude, OpenAI, Gemini, Groq, Mistral, Ollama (local)\n\n"
    "REQUIREMENTS\n"
    "------------\n"
    "* Python 3.10-3.13 (64-bit) from https://www.python.org/downloads/\n"
    "* Internet connection for first-time install (~2-3 GB).\n"
    "* API key from any supported provider (Claude, OpenAI, Groq, etc.)\n"
    "  OR run Ollama locally - no API key needed.\n\n"
    "WHAT GETS INSTALLED\n"
    "-------------------\n"
    "* Python virtual environment in the venv/ folder\n"
    "* All dependencies (gradio, anthropic, openai, whisper, ffmpeg,\n"
    "  mediapipe, opencv-python, etc.)\n"
    "* Desktop shortcut 'Transcript Agent'\n\n"
    "NOTES\n"
    "-----\n"
    "* Your API keys are stored only in .env - never sent to any server.\n"
    "* To update: run setup_windows.bat and choose [2] Check for updates.\n"
    "* Video Analysis models (~9 MB) download automatically on first use.\n"
    "* For Ollama: install from https://ollama.ai then run: ollama pull gemma3:27b\n"
)

with zipfile.ZipFile(out_zip, "w", compression=zipfile.ZIP_DEFLATED) as zf:
    for src_file, arc_name in files.items():
        zf.write(src_file, arc_name)
        print(f"  + {arc_name}")
    zf.writestr("TranscriptAgent/README.txt", readme)
    print("  + TranscriptAgent/README.txt")

print(f"\nBuilt: {out_zip}  ({out_zip.stat().st_size / 1024:.0f} KB)")
