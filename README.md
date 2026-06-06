---
title: Transcript Agent
emoji: 🎤
colorFrom: blue
colorTo: indigo
sdk: docker
app_port: 7860
pinned: false
---

<div align="center">

# 🎤 Transcript Agent

**AI-powered transcription, interview coaching, and video analysis — local-first.**

[![Version](https://img.shields.io/badge/version-v2.3.3-blue?style=flat-square)](https://github.com/jayuan101/transcript-agent/releases)
[![Platform](https://img.shields.io/badge/platform-Windows%20%7C%20Mac%20%7C%20Docker-lightgrey?style=flat-square)](#installation)
[![HuggingFace](https://img.shields.io/badge/🤗%20HuggingFace-Live%20Demo-orange?style=flat-square)](https://huggingface.co/spaces/Coastline6/transcript-agent-v2)
[![License](https://img.shields.io/badge/license-MIT-green?style=flat-square)](LICENSE)

[**Live Demo**](https://huggingface.co/spaces/Coastline6/transcript-agent-v2) · [**Releases**](https://github.com/jayuan101/transcript-agent/releases) · [**Changelog**](CHANGELOG.md)

</div>

---

## What it does

| | Feature | Details |
|---|---|---|
| 🎤 | **Transcription** | Whisper (local/offline), OpenAI, Groq, Deepgram, AssemblyAI, Google, Azure, ElevenLabs, Rev.ai |
| 🤖 | **AI Analysis** | Claude, OpenAI, Gemini, Groq, Mistral, Together AI, Perplexity, Ollama (local) |
| 🗣️ | **37+ Languages** | Auto-detect or choose, with regional dialect variants |
| 🎯 | **Interview Coaching** | Per-question scoring, 10-point score, coaching tips, advancement likelihood % |
| 🎥 | **Video Analysis** | Emotion, eye contact, posture, body language — score cards + annotated video download |
| 🔴 | **Live Interview** | Real-time webcam analysis — scores update every 5 seconds |
| 💪 | **Body Language** | Arm crossing, forward lean, shoulder tension, head nod — OPEN / ENGAGED / TENSE / CLOSED |
| 🌎 | **Cultural Analysis** | American Interview Standard score + Indian → American adaptation coaching |
| 📊 | **Reports** | Summary, key points, action items, speaker profiles, speech analytics |
| 📤 | **Exports** | `.txt` `.docx` `.pdf` `.srt` `.vtt` `.json` |
| 🔒 | **Local-first** | Files processed on your machine — nothing sent to the cloud without your API key |

---

## Supported formats

| Type | Formats |
|---|---|
| Audio | `mp3` `wav` `m4a` `flac` `ogg` `aac` `wma` |
| Video | `mp4` `mov` `avi` `mkv` `webm` `m4v` |
| Documents | `pdf` `docx` `txt` `md` `srt` `vtt` |

---

## Installation

### Windows

> **Requirement:** Python 3.10+ from [python.org](https://www.python.org/downloads/) — check **"Add Python to PATH"** during install.

1. [Download the latest release](https://github.com/jayuan101/transcript-agent/releases/latest) → `TranscriptAgent-Windows.zip`
2. Extract the zip
3. Double-click **`setup_windows.bat`** — installs everything and creates a Desktop shortcut
4. App opens automatically at `http://localhost:7860`

> **Tip:** If you see path-length errors, move the folder to `C:\TranscriptAgent\` first.

---

### Mac

> **Requirements:** Python 3.10+ and ffmpeg.

```bash
# Install Homebrew (if needed)
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"

# Install Python and ffmpeg
brew install python ffmpeg

# Clone and set up
git clone https://github.com/jayuan101/transcript-agent.git
cd transcript-agent
chmod +x setup_mac.sh
./setup_mac.sh
```

App opens at `http://localhost:7860`. To launch again: `./setup_mac.sh` → choose **[1] Launch app**.

---

### Docker (no Python required)

> **Requirement:** [Docker Desktop](https://www.docker.com/products/docker-desktop/)

```bash
git clone https://github.com/jayuan101/transcript-agent.git
cd transcript-agent

docker compose build
docker compose up -d
# Open http://localhost:7860

docker compose down   # to stop
```

> All data stays on your machine — nothing is pushed anywhere.

---

### Manual install (any platform)

```bash
git clone https://github.com/jayuan101/transcript-agent.git
cd transcript-agent

python -m venv venv
source venv/bin/activate        # Mac/Linux
# venv\Scripts\activate         # Windows

pip install -r requirements.txt
pip install torch --index-url https://download.pytorch.org/whl/cpu

python app.py   # http://localhost:7860
```

---

## Usage

### Basic transcription
1. Enter your API key in the sidebar (Claude, OpenAI, Groq, etc.)
2. Choose your STT engine and AI provider
3. Upload a file, paste a path, or paste a URL
4. Click **▶ Analyze**

### Interview coaching
Enable **Interview Mode** in the sidebar before analyzing. Every question is automatically scored with coaching tips, and an advancement likelihood % is generated.

### Video analysis (uploaded recording)
1. Go to the **🎥 Video Analysis** tab
2. Upload your interview video
3. Click **Scan Faces** → app detects everyone in the video
4. Assign roles (Candidate, Interviewer 1, etc.)
5. Click **Analyze Video**

Produces: per-person score cards, emotion timeline, body language report, cultural analysis, and annotated video download.

### Live interview (webcam)
1. Go to the **🔴 Live Interview** tab
2. Set Person 1 / Person 2 roles and cultural context
3. Click **▶ Start** and allow camera access
4. Scores update live every ~5 seconds
5. Click **⏹ Stop** when done

### Ollama — run AI locally (no API key)
1. Install Ollama from [ollama.ai](https://ollama.ai)
2. Pull a model:
   ```bash
   ollama pull gemma3:27b
   ```
3. In the app: select **Ollama (Local)** as provider

**Model guide by RAM:**
| RAM | Recommended model |
|---|---|
| 48 GB+ | `llama3.3` or `qwen2.5:72b` |
| 20–24 GB | `gemma3:27b` ★ default |
| 10–16 GB | `phi4` or `gemma3:12b` |
| 8 GB | `gemma3:12b` or `llama3.2` |

---

## REST API

A REST API runs on port 8000 alongside the UI.

| Method | Endpoint | Description |
|---|---|---|
| `POST` | `/api/transcribe` | Upload file, get `job_id` immediately |
| `GET` | `/api/jobs/{job_id}` | Poll for results |
| `GET` | `/api/jobs/{job_id}/log` | Stream live processing log |
| `GET` | `/health` | Health check |
| `GET` | `/docs` | Swagger UI |

---

## Project structure

```
app.py               — Gradio UI, all tabs, event wiring
transcript_agent.py  — STT engines, LLM analysis, report generation
video_analyzer.py    — Emotion, body language, cultural scoring
api.py               — FastAPI REST API
launcher.py          — Desktop app entry point
setup_windows.bat    — One-click Windows installer
setup_mac.sh         — One-click Mac installer
Dockerfile           — Docker image
requirements.txt     — Python dependencies
```

---

## Support the project

If this tool saves you time, consider buying me a coffee ☕

[![Donate via PayPal](https://img.shields.io/badge/Donate-PayPal-0070ba?style=for-the-badge&logo=paypal&logoColor=white)](https://paypal.me/jay247616)

---

<div align="center">
  <sub>Transcript Agent · Transcription by OpenAI Whisper · Analysis by Anthropic Claude · <a href="CHANGELOG.md">Changelog</a></sub>
</div>
