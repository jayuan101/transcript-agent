"""Build the Mac distribution zip with latest source files."""
import zipfile
from pathlib import Path

APP_VERSION = "2.0.3"

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
    f"Transcript Agent v{APP_VERSION} - Mac Package\n"
    "=============================================\n\n"
    "QUICK START\n"
    "-----------\n"
    "1. Install Python 3.10+ from https://www.python.org/downloads/\n"
    "   OR:  brew install python\n"
    "2. Open Terminal in this folder\n"
    "3. Run:  bash setup_mac.sh   (first time only - installs everything)\n"
    "4. Double-click 'Transcript Agent' on your Desktop to launch\n\n"
    "WHAT'S INCLUDED\n"
    "---------------\n"
    "* Transcription: 9 STT engines including Whisper (local/offline)\n"
    "* Interview Analysis: coaching, video delivery analysis, body language\n"
    "* Cultural scoring: American Standard + Indian to American adaptation\n"
    "* AI providers: Claude, OpenAI, Gemini, Groq, Mistral, Ollama (local)\n\n"
    "REQUIREMENTS\n"
    "------------\n"
    "* macOS 11 (Big Sur) or later\n"
    "* Python 3.10-3.13 from https://www.python.org/downloads/\n"
    "* Internet connection for first-time install (~2-3 GB)\n\n"
    "WHAT GETS INSTALLED\n"
    "-------------------\n"
    "* Python virtual environment in the venv/ folder\n"
    "* All dependencies (gradio, anthropic, openai, whisper, ffmpeg,\n"
    "  mediapipe, opencv-python-headless, etc.)\n"
    "* Desktop launcher 'Transcript Agent.command'\n\n"
    "NOTES\n"
    "-----\n"
    "* Your API keys are stored only in .env - never on any server.\n"
    "* To update: run setup_mac.sh and choose [2] Check for updates.\n"
    "* Video Analysis models (~9 MB) download on first use.\n"
    "* For Ollama: brew install ollama && ollama pull gemma3:27b\n"
)

with zipfile.ZipFile(out_zip, "w", compression=zipfile.ZIP_DEFLATED) as zf:
    for src_file, arc_name in files.items():
        zf.write(src_file, arc_name)
        print(f"  + {arc_name}")
    zf.writestr("TranscriptAgent/README.txt", readme)
    print("  + TranscriptAgent/README.txt")

print(f"\nBuilt: {out_zip}  ({out_zip.stat().st_size / 1024:.0f} KB)")
