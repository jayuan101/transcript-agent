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

[![Version](https://img.shields.io/badge/version-v2.5.15-blue?style=flat-square)](https://github.com/jayuan101/transcript-agent/releases)
[![Docker Hub](https://img.shields.io/badge/Docker%20Hub-sushi0934%2Ftranscript--agent-2496ed?style=flat-square&logo=docker&logoColor=white)](https://hub.docker.com/r/sushi0934/transcript-agent)
[![HuggingFace](https://img.shields.io/badge/🤗%20HuggingFace-Live%20Demo-orange?style=flat-square)](https://huggingface.co/spaces/Coastline6/transcript-agent-v2)
[![License](https://img.shields.io/badge/license-MIT-green?style=flat-square)](LICENSE)

[**Live Demo**](https://huggingface.co/spaces/Coastline6/transcript-agent-v2) · [**Docker Hub**](https://hub.docker.com/r/sushi0934/transcript-agent) · [**Releases**](https://github.com/jayuan101/transcript-agent/releases) · [**Changelog**](CHANGELOG.md)

</div>

---

## Two editions: Production vs Development

This repo ships **two editions** on two branches. They share the same Python engine (`transcript_agent.py`, `video_analyzer.py`, `interview_vision.py`) but use a **different UI**.

| | 🟢 Production (`main`) | 🧪 Development (`dev`) |
|---|---|---|
| **UI** | Gradio (`app.py`) | React + PrimeReact (`frontend/`), served by `api.py` |
| **Default Docker entrypoint** | `python app.py` | `python api.py` (`UI_MODE=react`) |
| **🔴 Live Interview (real-time webcam)** | ❌ Not available | ✅ Yes |
| **Video delivery analysis (uploaded file)** | ✅ Yes | ✅ Yes |
| **Transcription, coaching, exports, history** | ✅ Yes | ✅ Yes |
| **Legacy Gradio UI** | (it *is* the UI) | available via `UI_MODE=gradio` |
| **Build-from-source compose** | `docker-compose.prod.yml` (pull image) | `docker-compose.yml` (local build) |

> **TL;DR:** If you want the stable Docker Hub release, use **Production**. If you need the new React UI and the **Live Interview** webcam feature, use **Development** (`dev` branch).

---

## What it does

| | Feature | Edition | Details |
|---|---|---|---|
| 🎤 | **Transcription** | both | 9 STT engines — Whisper (local/offline), OpenAI, Groq, Deepgram, AssemblyAI, Google Cloud, Azure, ElevenLabs Scribe, Rev.ai |
| 🤖 | **AI Analysis** | both | 13 providers — Claude, OpenAI, Gemini, Groq, Mistral, Together AI, Perplexity, xAI Grok, DeepSeek, OpenRouter, Cerebras, Cohere, Ollama (local) |
| 🗣️ | **37+ Languages** | both | Auto-detect or choose, with regional dialect variants |
| 🎯 | **Interview Coaching** | both | Per-question scoring, 10-point score, coaching tips, question-type breakdown, advancement likelihood % |
| 🎥 | **Video Analysis** (uploaded) | both | Emotion, eye contact, head pose, posture, body language — per-person score cards + annotated video |
| 💬 | **Live Transcription** | both | Real-time clip transcription (Deepgram by default, local Whisper fallback) |
| 💪 | **Body Language** | both | Arm crossing, forward/back lean, shoulder tension, head nod — OPEN / ENGAGED / TENSE / CLOSED |
| 🌎 | **Cultural Analysis** | both | American Interview Standard score + Indian → American adaptation coaching |
| 👤 | **Speaker Names** | both | On-screen participant-name OCR (Teams / Meet / Zoom / Nextcloud) maps speakers to real names |
| 📊 | **Reports & History** | both | Summary, key points, action items, speaker profiles, token spend + cost history |
| 📤 | **Exports** | both | `.txt` `.docx` `.pdf` `.srt` `.vtt` `.json` |
| 🔴 | **Live Interview (webcam)** | **dev only** | Real-time webcam analysis — records 5s clips, scores update live |

---

## Supported formats

| Type | Formats |
|---|---|
| Audio | `mp3` `wav` `m4a` `flac` `ogg` `aac` `wma` |
| Video | `mp4` `mov` `avi` `mkv` `webm` `m4v` |
| Documents | `pdf` `docx` `txt` `md` `srt` `vtt` |

---

# 🟢 Production (`main`)

Stable Gradio UI, distributed as a pre-built Docker Hub image — **no git clone, no build**.

> **Requirement:** [Docker](https://www.docker.com/products/docker-desktop/) (Desktop or Engine).

```bash
mkdir transcript-agent && cd transcript-agent

# Add your API keys (at least one LLM key) — see "Configuration" below
cp .env.example .env

# Pull and start the production image
docker compose -f docker-compose.prod.yml pull
docker compose -f docker-compose.prod.yml up -d
```

Open **http://localhost:7860** (Gradio UI) and **http://localhost:8000/docs** (REST API).

| Command | Action |
|---|---|
| `docker compose -f docker-compose.prod.yml up -d` | Start in the background |
| `docker compose -f docker-compose.prod.yml pull` | Update to the latest image |
| `docker compose -f docker-compose.prod.yml logs -f` | Follow logs |
| `docker compose -f docker-compose.prod.yml down` | Stop and remove the container |

**Image tags:** `sushi0934/transcript-agent:latest` (auto-updates) · `sushi0934/transcript-agent:2.5.13` (pinned).
The prod compose file sets `pull_policy: always`, `restart: unless-stopped`, and a healthcheck. The container runs `python app.py` (Gradio), which grafts the REST API onto its own server.

### Run production without Docker

```bash
git clone -b main https://github.com/jayuan101/transcript-agent.git
cd transcript-agent

python -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt
pip install torch --index-url https://download.pytorch.org/whl/cpu

python app.py                   # Gradio UI + REST API on port 7860
```

On Windows you can instead double-click [`run.bat`](run.bat), which detects your GPU and opens the browser automatically.

---

# 🧪 Development (`dev`)

Adds the **React + PrimeReact UI** (served by `api.py`) and the **🔴 Live Interview** webcam feature. The legacy Gradio UI is still available via `UI_MODE=gradio`.

```bash
git clone -b dev https://github.com/jayuan101/transcript-agent.git
cd transcript-agent
cp .env.example .env            # add your keys

# Build & run the React UI + REST API (UI_MODE=react is the default)
docker compose build
docker compose up -d            # http://localhost:7860
```

### Run the React UI from source

```bash
python -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt
pip install torch --index-url https://download.pytorch.org/whl/cpu

# Build the React UI once (output frontend/dist is served by api.py at "/")
cd frontend && npm install && npm run build && cd ..

python api.py                   # React UI + REST API (port 8000, override with API_PORT)
```

For live front-end development with hot reload:

```bash
cd frontend
npm run dev                     # Vite dev server, proxies to a running `python api.py`
```

### Dev launcher (Windows, Gradio on 7861)

[`launch_dev.bat`](launch_dev.bat) runs the **Gradio** app in dev mode on **port 7861** (isolated from a production instance on 7860), with `TA_DEV_MODE=1` showing a **[DEV]** banner:

```bash
launch_dev.bat
```

> ⚠️ Note: `launch_dev.bat` runs the **Gradio** UI, which does **not** include the 🔴 Live Interview tab. To develop Live Interview, use `python api.py` + `npm run dev` above.

### Promote dev → production

- **Windows desktop:** [`deploy_to_prod.bat`](deploy_to_prod.bat) stops the running prod app, copies the updated `.py` source into the installed app's `_internal` folder, and relaunches it.
- **Docker Hub:** [`push_to_dockerhub.bat`](push_to_dockerhub.bat) builds and pushes a new image; production then pulls it via `docker-compose.prod.yml`.

---

## Configuration (`.env`)

Used by both editions. Copy [`.env.example`](.env.example) → `.env` and fill in **at least one** LLM key. Keys are mounted read-only into the container and never leave your machine.

```dotenv
# AI / LLM providers (pick one or more)
ANTHROPIC_API_KEY=          # Claude  — https://console.anthropic.com/keys
OPENAI_API_KEY=             # GPT     — https://platform.openai.com/api-keys
GEMINI_API_KEY=             # Gemini  — https://aistudio.google.com/app/apikey
GROQ_API_KEY=               # Groq    — https://console.groq.com/keys

# Speech-to-Text engines (optional — Whisper runs locally for free)
DEEPGRAM_API_KEY=
ASSEMBLYAI_API_KEY=
ELEVENLABS_API_KEY=
REV_AI_ACCESS_TOKEN=

# App settings
TZ=America/New_York         # your timezone
```

> You can also enter keys directly in the UI sidebar — they are saved in your browser only.

---

## GPU acceleration (NVIDIA)

Local Whisper and emotion detection run much faster on a GPU. Uncomment the `deploy.resources` block in the compose file:

```yaml
    deploy:
      resources:
        reservations:
          devices:
            - driver: nvidia
              count: all
              capabilities: [gpu]
```

- **Linux:** `sudo apt install nvidia-container-toolkit && sudo systemctl restart docker`
- **Windows:** WSL2 + NVIDIA driver ≥ 510 + Docker Desktop ≥ 4.13

---

## Auto-update in production

The `latest` tag plus `pull_policy: always` means `docker compose -f docker-compose.prod.yml up -d` always re-pulls the newest image. For hands-off updates, uncomment the **Watchtower** service in the prod compose file to re-pull hourly:

```yaml
  watchtower:
    image: containrrr/watchtower
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock
    command: --interval 3600 --cleanup transcript-agent
    restart: unless-stopped
```

---

## REST API

Available in **both** editions. Runs on **port 8000** by default (and on 7860 inside the container). Interactive Swagger docs: `/docs`.

| Method | Endpoint | Description |
|---|---|---|
| `POST` | `/api/transcribe` | Start async transcription — returns a `job_id` immediately |
| `POST` | `/api/transcribe/sync` | Transcribe and wait for the result |
| `GET` | `/api/jobs/{job_id}` | Get job status and results |
| `GET` | `/api/jobs/{job_id}/log` | Stream live processing log |
| `GET` | `/api/jobs/{job_id}/download/{name}` | Download a generated result file |
| `POST` | `/api/jobs/{job_id}/cancel` | Cancel a running job |
| `POST` | `/api/jobs/{job_id}/regenerate` | Regenerate PDF & DOCX (optionally in another language) |
| `POST` | `/api/transcribe-clip` | Quick live transcription of a short clip (used by Live Interview) |
| `POST` | `/api/analyze-video` | Analyze interview video delivery (body language, emotion, eye contact) |
| `GET` | `/api/history` | Past runs with token spend + cost |
| `DELETE` | `/api/history/{entry_id}` | Delete a run (moves it to trash) |
| `GET` | `/api/trash` · `POST` `/api/trash/{id}/restore` · `POST` `/api/trash/empty` | Trash management |
| `GET` | `/api/devices` | Available compute device (GPU/CPU) for local Whisper |
| `GET` | `/api/update-check` | Check GitHub for a newer release |
| `GET` | `/health` | Health check (used by the container healthcheck) |
| `GET` | `/docs` | Swagger UI |

---

## Usage

1. Open **http://localhost:7860**
2. Enter an API key in the sidebar (or set it in `.env`) and pick your STT engine + AI provider
3. Upload a file, paste a path, or paste a URL → click **▶ Analyze**
4. Enable **Interview Mode** for per-question scoring, or use the **🎥 Video Analysis** tab
5. *(dev only)* Open the **🔴 Live Interview** tab for real-time webcam coaching

### Ollama — run AI locally (no API key)
1. Install Ollama from [ollama.ai](https://ollama.ai), then `ollama pull gemma3:27b`
2. In the app, select **Ollama (Local)** as the provider

| RAM | Recommended model |
|---|---|
| 48 GB+ | `llama3.3`, `qwen2.5:72b`, or `deepseek-r1:70b` |
| 16–24 GB | `gemma3:27b` ★ default, `qwen3:32b` |
| 10–16 GB | `phi4`, `qwen3:14b`, or `gemma3:12b` |
| 8 GB | `gemma3:12b` or `llama3.2` |

> In Docker, the app reaches Ollama on the host via `host.docker.internal:11434` automatically.

---

## Project structure

```
transcript_agent.py  — STT engines, LLM analysis, report & export generation   (both)
video_analyzer.py    — Emotion, eye contact, posture, body language, cultural   (both)
interview_vision.py  — On-screen participant-name OCR + speaker mapping         (both)
api.py               — FastAPI REST API (+ serves the React UI on dev)          (both)
app.py               — Gradio UI                                                (production UI)
frontend/            — React + PrimeReact web UI, incl. Live Interview          (dev only)
entrypoint.sh        — Docker entrypoint (app.py on main, api.py on dev)
docker-compose.prod.yml  — Pull pre-built image from Docker Hub                 (production)
docker-compose.yml       — Build from source                                    (dev)
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
