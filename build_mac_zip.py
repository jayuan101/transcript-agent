"""Build the Mac distribution zip with latest source files."""
import re, zipfile
from pathlib import Path

# Single source of truth — read version from app.py
APP_VERSION = re.search(r'APP_VERSION\s*=\s*"([^"]+)"', Path("app.py").read_text(encoding="utf-8")).group(1)

out_zip = Path("dist/TranscriptAgent-Mac.zip")
out_zip.parent.mkdir(exist_ok=True)

files = {
    "app.py":              "TranscriptAgent/app.py",
    "transcript_agent.py": "TranscriptAgent/transcript_agent.py",
    "video_analyzer.py":   "TranscriptAgent/video_analyzer.py",
    "api.py":              "TranscriptAgent/api.py",
    "requirements.txt":    "TranscriptAgent/requirements.txt",
    "setup_mac.sh":        "TranscriptAgent/setup_mac.sh",
    "launcher.py":         "TranscriptAgent/launcher.py",
    "CHANGELOG.md":        "TranscriptAgent/CHANGELOG.md",
    "README.md":           "TranscriptAgent/README.md",
}

readme = (
    f"Transcript Agent v{APP_VERSION} - Mac\n"
    "=============================================\n\n"
    "FIRST TIME INSTALL\n"
    "------------------\n"
    "1. Install Python 3.10+ from https://www.python.org/downloads/\n"
    "   OR:  brew install python\n"
    "2. Open Terminal in this folder and run:\n"
    "      bash setup_mac.sh\n"
    "   - Installs all packages (~2-3 GB, one time only)\n"
    "   - Creates a 'Transcript Agent' shortcut on your Desktop\n"
    "   - Launches the app automatically\n\n"
    "HOW TO LAUNCH AFTER INSTALL\n"
    "---------------------------\n"
    "Option 1 (easiest):  Double-click 'Transcript Agent' on your Desktop\n"
    "Option 2:            Open Terminal in this folder -> bash setup_mac.sh -> [1] Launch app\n\n"
    "The app opens in your browser at  http://localhost:7860\n\n"
    "WHAT'S INCLUDED\n"
    "---------------\n"
    "* Transcription: 9 STT engines including Whisper (local/offline)\n"
    "* Interview coaching: per-question scoring, coaching tips, advancement %\n"
    "* Video Analysis: emotion, eye contact, body language per person\n"
    "* Live Interview: real-time webcam analysis, scores every 5 seconds\n"
    "* AI providers: Claude, OpenAI, Gemini, Groq, Mistral, Ollama (local)\n\n"
    "REQUIREMENTS\n"
    "------------\n"
    "* macOS 11 (Big Sur) or later\n"
    "* Python 3.10-3.13 - https://www.python.org/downloads/\n"
    "* Internet connection for first-time install\n"
    "* API key (Claude, OpenAI, Groq, etc.) OR Ollama for fully local AI\n\n"
    "NOTES\n"
    "-----\n"
    "* API keys stored only in .env on your machine - never sent anywhere.\n"
    "* To update: setup_mac.sh -> [2] Check for updates\n"
    "* Video Analysis models (~9 MB) download on first use\n"
    "* Ollama (local AI, no API key): brew install ollama && ollama pull gemma3:27b\n"
)

with zipfile.ZipFile(out_zip, "w", compression=zipfile.ZIP_DEFLATED) as zf:
    for src_file, arc_name in files.items():
        zf.write(src_file, arc_name)
        print(f"  + {arc_name}")
    zf.writestr("TranscriptAgent/README.txt", readme)
    print("  + TranscriptAgent/README.txt")

print(f"\nBuilt: {out_zip}  ({out_zip.stat().st_size / 1024:.0f} KB)  [v{APP_VERSION}]")
