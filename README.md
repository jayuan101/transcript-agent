# Transcript Agent

AI-powered transcription and interview analysis app for meetings, interviews, and recordings.  
Auto-identifies speakers, scores interview responses, generates formatted reports, and exports to PDF.  
Runs entirely on your machine — your files never leave your device.

**[Download latest release](https://github.com/jayuan101/transcript-agent-releases/releases)**

---

## Quick Start — Docker (Recommended)

The fastest way to run Transcript Agent is with Docker. No Python, no setup.

### 1. Pull the image

```bash
docker pull sushi0934/transcript-agent:latest
```

### 2. Run it

> **The `-p` flags are required.** Without them the app runs inside the container but is unreachable from your browser.

```bash
docker run -d \
  --name transcript-agent \
  -p 7860:7860 \
  -p 8000:8000 \
  -v transcript-agent-outputs:/app/outputs \
  -v transcript-agent-cache:/app/.cache \
  -e GRADIO_SERVER_NAME=0.0.0.0 \
  -e GRADIO_SERVER_PORT=7860 \
  --restart unless-stopped \
  sushi0934/transcript-agent:latest
```

| Flag | What it does |
|------|-------------|
| `-p 7860:7860` | Exposes the web UI on your machine at port 7860 |
| `-p 8000:8000` | Exposes the REST API on your machine at port 8000 |

### 3. Open the app

```
http://localhost:7860
```

Once the container starts you will see this in the logs (`docker logs transcript-agent`):

```
============================================
  Transcript Agent
============================================
  UI  ->  http://localhost:7860
  API ->  http://localhost:8000
============================================
```

### Stop it

```bash
docker stop transcript-agent && docker rm transcript-agent
```

---

## Docker Compose

Create a `docker-compose.yml`:

```yaml
services:
  transcript-agent:
    image: sushi0934/transcript-agent:latest
    container_name: transcript-agent
    ports:
      - "7860:7860"   # Gradio UI
      - "8000:8000"   # REST API
    volumes:
      - transcript-agent-outputs:/app/outputs
      - transcript-agent-cache:/app/.cache
    environment:
      - GRADIO_SERVER_NAME=0.0.0.0
      - GRADIO_SERVER_PORT=7860
    restart: unless-stopped

volumes:
  transcript-agent-outputs:
  transcript-agent-cache:
```

Then run:

```bash
docker compose up -d
```

Open `http://localhost:7860` in your browser.

---

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `GRADIO_SERVER_NAME` | `0.0.0.0` | Interface to bind (`0.0.0.0` = all, `127.0.0.1` = localhost only) |
| `GRADIO_SERVER_PORT` | `7860` | Port for the web UI |
| `ANTHROPIC_API_KEY` | — | Pre-fill your Claude API key (optional) |
| `OPENAI_API_KEY` | — | Pre-fill your OpenAI API key (optional) |
| `HF_TOKEN` | — | HuggingFace token — required for speaker diarization (panel mode) |

---

## Available Tags

| Tag | Description |
|-----|-------------|
| `latest` | Always the most recent stable build |
| `v3.16` | 8 STT engines (+ ElevenLabs, Rev.ai), engine dropdown, auto-fill key |
| `v3.15` | Interview Q&A richer detail, human-voice answers, PDF fix, version badge |
| `v3.14` | Timezone searchable dropdown, history Load buttons, color-coded downloads |
| `v3.13` | Timezone field as scrollable IANA dropdown |
| `v3.12` | Color-coded download buttons, ETA panel improvements |
| `v3.11` | STT timing display, faster startup, Interview Q&A improvements |

```bash
# Pin to a specific version
docker pull sushi0934/transcript-agent:v3.16

# Always latest
docker pull sushi0934/transcript-agent:latest
```

---

## Features

### Transcription
- **8 STT engines** — Whisper (local), Deepgram, AssemblyAI, Groq Whisper, OpenAI Whisper API, Google Cloud STT, ElevenLabs Scribe, Rev.ai
- **STT timing** — shows exactly how long the transcription step took per engine
- **Multi-speaker diarization** — auto-detects speakers or use a fixed count
- **51+ languages** — auto-detect or specify language and regional variant
- **Fast startup** — Whisper/PyTorch loads in the background, UI is instant

### Analysis
- **9 AI providers** — Claude, GPT-4o, Gemini, Groq, Mistral, Ollama, and more
- **Custom AI endpoint** — any OpenAI-compatible API (LM Studio, vLLM, Azure, etc.)
- **AI analysis depth** — Fast / Balanced / Deep (Deep enables extended thinking)

### Interview Mode
- **Question extraction** — identifies every question the interviewer asked
- **Answer scoring** — rates each response: Great / Good / Needs Improvement / Missed
- **Ideal answers** — shows how you could have answered each question
- **Coaching tips** — specific, actionable feedback per question
- **Deep mode** — deflection detection, % likelihood of advancing, prep guide for weak questions

### Output
- **Summary tab** — AI summary + full transcript + speaker dialogue in one view
- **Speaker profiles** — named speaker breakdown with role detection
- **Speech analytics** — WPM, pace, accent analysis per speaker
- **Export** — PDF report, Markdown, JSON, plain text, DOCX, SRT, VTT
- **Color-coded downloads** — each file type shown as a distinct chip for quick access
- **Auto-update** — desktop app notifies and installs updates in one click
- **Timezone-aware ETA** — searchable IANA timezone dropdown, auto-detected from browser

---

## Supported Formats

**Audio:** `.mp3` `.wav` `.m4a` `.ogg` `.flac` `.aac`  
**Video:** `.mp4` `.mkv` `.webm` `.mov` `.avi`  
**Documents:** `.pdf` `.docx` `.txt` `.md` `.srt` `.vtt`

---

## Standalone Desktop App

Prefer a native app? Download from the **[Releases page](https://github.com/jayuan101/transcript-agent-releases/releases)**:

| Platform | File |
|----------|------|
| Windows | `TranscriptAgent.exe` |
| macOS | `TranscriptAgent.dmg` |
| Linux | `TranscriptAgent-linux.AppImage` |

No Docker, no Python required.

---

## REST API

The container also exposes a REST API on port `8000`.

### Transcribe a file (async)

```bash
curl -X POST http://localhost:8000/api/transcribe \
  -F "file=@interview.mp3" \
  -F "whisper_model=base" \
  -F "panel_mode=false" \
  -F "report_style=formal"
```

Returns a `job_id`. Poll for results:

```bash
curl http://localhost:8000/api/jobs/<job_id>
```

### Transcribe synchronously

```bash
curl -X POST http://localhost:8000/api/transcribe/sync \
  -F "file=@interview.mp3" \
  -F "whisper_model=base"
```

---

## System Requirements (Docker)

- Docker Desktop or Docker Engine
- 8 GB RAM minimum (16 GB recommended for large models)
- 5 GB free disk space
