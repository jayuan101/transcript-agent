# Transcript Agent

AI-powered transcription app for interviews and meetings.  
Automatically identifies speakers, generates formatted reports, and exports to Word or PDF.  
Runs entirely on your machine — your files never leave your device.

---

## Quick Start — Docker (Recommended)

The fastest way to run Transcript Agent is with Docker. No Python, no setup.

### 1. Pull the image

```bash
docker pull sushi0934/transcript-agent:latest
```

### 2. Run it

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

### 3. Open the app

```
http://localhost:7860
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
| `GRADIO_SERVER_NAME` | `0.0.0.0` | Interface to bind to (`0.0.0.0` = all, `127.0.0.1` = localhost only) |
| `GRADIO_SERVER_PORT` | `7860` | Port for the web UI |
| `ANTHROPIC_API_KEY` | — | Pre-fill your Claude API key (optional) |
| `OPENAI_API_KEY` | — | Pre-fill your OpenAI API key (optional) |
| `HF_TOKEN` | — | HuggingFace token — required for speaker diarization (panel mode) |

---

## Available Tags

| Tag | Description |
|-----|-------------|
| `latest` | Always the most recent stable build |
| `1.8`, `1.7`, … | Pinned version tags |
| `1.8`, `1` | Major and minor floating tags |

```bash
# Pin to a specific version
docker pull sushi0934/transcript-agent:1.8

# Always latest
docker pull sushi0934/transcript-agent:latest
```

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

## Standalone Desktop App

Prefer a native desktop app? Download from the [Releases](https://github.com/jayuan101/transcript-agent-releases/releases) page:

| Platform | File |
|----------|------|
| Windows | `TranscriptAgent.exe` |
| macOS | `TranscriptAgent.dmg` |
| Linux | `TranscriptAgent-linux.AppImage` |

No Docker, no Python required.

---

## Features

- **Multi-speaker diarization** — auto-detects or use a fixed speaker count
- **9 AI providers** — Claude, GPT-4o, Gemini, Groq, Mistral, Ollama, and more
- **Custom AI endpoint** — any OpenAI-compatible API (LM Studio, vLLM, Azure, etc.)
- **Export** — Word (.docx) or PDF with formatted report
- **Auto-update** — desktop app notifies you and installs updates in one click
- **Sleep prevention** — keeps your machine awake during long transcriptions
- **Timezone-aware ETA** — shows finish time in your local timezone

---

## Supported Formats

**Audio:** `.mp3` `.wav` `.m4a` `.ogg` `.flac` `.aac`  
**Video:** `.mp4` `.mkv` `.webm` `.mov` `.avi`

---

## System Requirements (Docker)

- Docker Desktop or Docker Engine
- 8 GB RAM minimum (16 GB recommended)
- 5 GB free disk space
