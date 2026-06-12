# Transcript Agent — React Bootstrap UI

A React + [react-bootstrap](https://react-bootstrap.netlify.app/) frontend for the
Transcript Agent. It talks to the FastAPI backend in [`../api.py`](../api.py).

## Run in dev

Two processes:

```bash
# 1. Backend (from the repo root) — serves on :8000
python api.py

# 2. Frontend (from this folder) — serves on :5173, proxies /api + /health to :8000
npm install
npm run dev
```

Open http://localhost:5173.

To point at a different backend: `API_TARGET=http://host:port npm run dev`.

## Build

```bash
npm run build      # outputs to dist/
npm run preview    # preview the production build
```

## What it does

Three top-level tabs, all driven by the FastAPI backend:

1. **📝 Transcribe & Analyze** — upload audio / video / document; configure
   Whisper model, language, report style, panel (multi-speaker) mode, and
   **Interview Mode** (per-question coaching, scores, candidate profile).
   Results show in sub-tabs: Summary (key points / action items), Transcript,
   Speaker Dialogue, Speaker Profiles, Speech Analytics, and Interview Coaching,
   plus a Download Results section (`.txt .srt .vtt .docx .md .json`).
   Uses `POST /api/transcribe` → poll `GET /api/jobs/{id}` → download via
   `GET /api/jobs/{id}/download/{name}`.

2. **🎥 Video Analysis** — upload an interview video to get delivery score cards
   (confidence, composure, eye contact, engagement, body language, emotion,
   cultural fit) plus an annotated video to play and download.
   Uses `POST /api/analyze-video`.

3. **🔴 Live Interview** — records your webcam in 5-second clips, analyzes each
   on the server, and refreshes live delivery scores. Reuses
   `POST /api/analyze-video` per clip.

Large files (e.g. a 3-hour video) are streamed to the backend in chunks and
processed in the background, so they don't block or exhaust server memory.
