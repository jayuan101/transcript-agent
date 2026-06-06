---
title: Transcript Agent
emoji: ðŸŽ™ï¸
colorFrom: blue
colorTo: indigo
sdk: docker
app_port: 7860
pinned: false
---

# Transcript Agent

AI-powered transcription, interview analysis, and report generation.  
9 STT engines Â· 8 AI providers Â· real-time video analysis Â· local-first.

---

## What it does

| Feature | Details |
|---|---|
| ðŸŽ¤ **Transcription** | Whisper (local/offline), OpenAI, Groq, Deepgram, AssemblyAI, Google, Azure, ElevenLabs, Rev.ai |
| ðŸ¤– **AI analysis** | Claude, OpenAI, Gemini, Groq, Mistral, Together AI, Perplexity, **Ollama (local â€” default: gemma3:27b)** |
| ðŸ—£ï¸ **37+ languages** | Auto-detect or choose, with regional dialect variants |
| ðŸŽ¯ **Interview coaching** | Per-question scoring (Great / Good / Needs Improvement / Missed), 10-point score, coaching tips, advancement likelihood |
| ðŸŽ¥ **Video Analysis** | Upload a recorded interview â€” detects emotion, eye contact, posture, body language per person. Score cards, emotion timeline, annotated video download |
| ðŸ”´ **Live Interview** | Real-time webcam analysis â€” live emotion, eye contact, body language badge, scores update every 5 seconds |
| ðŸ’ª **Body language** | Arm crossing, forward lean, shoulder tension, head nod â€” OPEN / ENGAGED / TENSE / CLOSED badge |
| ðŸŒŽ **Cultural analysis** | American Interview Standard score + Indian â†’ American adaptation score with coaching tips |
| ðŸ“Š **Reports** | Summary, key points, action items, speaker profiles, speech analytics |
| ðŸ“ **History** | Every session saved locally â€” tokens, cost, score, full Q&A replay |
| ðŸ“¤ **Exports** | .txt, .docx, .pdf, .srt, .vtt, .json |

---

## Supported file formats

| Type | Formats |
|---|---|
| Audio | mp3, wav, m4a, flac, ogg, aac, wma |
| Video | mp4, mov, avi, mkv, webm, m4v |
| Documents | pdf, docx, txt, md, srt, vtt |

---

## Installation

### Windows

**Requirements:** Python 3.10+ from [python.org](https://www.python.org/downloads/) â€” check **"Add Python to PATH"** during install.

```
1. Download or clone this repo
2. Double-click  setup_windows.bat
3. It creates a virtual environment, installs all packages, and launches the app
4. The app opens automatically at http://localhost:7860
```

To launch again later â€” just double-click `setup_windows.bat` and choose **[1] Launch app**.

> **Tip:** If you see errors about path length, move the folder to a short path like `C:\TranscriptAgent\` first.

---

### Mac

**Requirements:** Python 3.10+ and ffmpeg.

```bash
# Install Homebrew if you don't have it
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"

# Install Python and ffmpeg
brew install python ffmpeg

# Clone and set up
git clone https://github.com/jayuan101/transcript-agent.git
cd transcript-agent
chmod +x setup_mac.sh
./setup_mac.sh
```

The script creates a virtual environment, installs all dependencies, and opens the app at `http://localhost:7860`.

To launch again later:
```bash
./setup_mac.sh   # choose [1] Launch app
```

---

### Docker (local only)

No Python install needed. Requires [Docker Desktop](https://www.docker.com/products/docker-desktop/).

```bash
git clone https://github.com/jayuan101/transcript-agent.git
cd transcript-agent

# Build image locally (does NOT push anywhere)
docker compose build

# Start
docker compose up -d

# Open
http://localhost:7860

# Stop
docker compose down
```

> The image stays on your machine only. No data leaves your computer.

---

### Manual Python install (any platform)

```bash
git clone https://github.com/jayuan101/transcript-agent.git
cd transcript-agent

python -m venv venv

# Windows
venv\Scripts\activate
# Mac / Linux
source venv/bin/activate

pip install -r requirements.txt
pip install torch --index-url https://download.pytorch.org/whl/cpu

python app.py
# Opens at http://localhost:7860
```

---

## Using the app

### Basic transcription
1. Enter your API key in the sidebar (Claude, OpenAI, Groq, etc.)
2. Choose your STT engine and AI provider
3. Upload a file, paste a file path, or paste a URL
4. Click **â–¶ Analyze**

### Interview coaching
Enable **Interview Mode** in the sidebar before analyzing. Every question is automatically scored with coaching tips, and an advancement likelihood % is generated.

### Video Analysis (uploaded recording)
1. Click the **ðŸŽ¥ Video Analysis** tab
2. Upload your interview video
3. Click **Scan Faces** â€” the app detects how many people are in the video
4. Assign roles to each face (Candidate, Interviewer 1, etc.)
5. Click **Analyze Video**

Produces: per-person score cards, emotion timeline, body language report, cultural analysis, annotated video download.

### Live Interview (webcam)
1. Click the **ðŸ”´ Live Interview** tab
2. Set Person 1 / Person 2 roles
3. Choose Cultural Context (American / Indian â†’ American / Both)
4. Click **â–¶ Start** and allow camera access
5. Watch scores update live every ~5 seconds
6. Click **â¹ Stop** when done

### Ollama (run AI locally, no API key)
1. Install Ollama from [ollama.ai](https://ollama.ai)
2. Pull the recommended model:
   ```bash
   ollama pull gemma3:27b
   ```
3. In the app: select **Ollama (Local)** as provider â†’ model defaults to `gemma3:27b`
4. No API key needed

**Model guide by hardware:**
| RAM | Recommended model |
|-----|------------------|
| 48 GB+ | `llama3.3` or `qwen2.5:72b` |
| 20â€“24 GB | `gemma3:27b` â˜… default |
| 10â€“16 GB | `phi4` or `gemma3:12b` |
| 8 GB | `gemma3:12b` or `llama3.2` |

---

## Project structure

```
app.py               â€” Gradio UI, all tabs, event wiring
transcript_agent.py  â€” STT engines, LLM analysis, report generation
video_analyzer.py    â€” Video/webcam analysis: emotion, body language, cultural scoring
api.py               â€” FastAPI REST API (port 8000)
launcher.py          â€” Desktop app entry point (auto-opens browser)
setup_windows.bat    â€” One-click Windows installer
setup_mac.sh         â€” One-click Mac installer
Dockerfile           â€” Docker image definition
docker-compose.yml   â€” Local Docker run config
requirements.txt     â€” Python dependencies
```

---

## API

A REST API runs on port 8000 alongside the UI.

```
POST /api/transcribe        â€” upload file, get job_id immediately
GET  /api/jobs/{job_id}     â€” poll for results
GET  /api/jobs/{job_id}/log â€” stream live processing log
GET  /health                â€” health check
GET  /docs                  â€” Swagger UI
```

---

## Version history

See [CHANGELOG.md](CHANGELOG.md) for full history. Current: **v2.2.3**
