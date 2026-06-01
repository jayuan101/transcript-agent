---
title: Transcript Agent
emoji: 🎙️
colorFrom: blue
colorTo: indigo
sdk: docker
app_port: 7860
pinned: false
---

# Transcript Agent

AI-powered transcription, interview scoring, and report generation — 9 STT engines × 8 AI providers.

**Bring your own API key.** Billed to your account, nothing stored on the server.

---

## Features

| | |
|---|---|
| 🎤 **9 STT engines** | Whisper (local/offline), OpenAI, Groq, Deepgram, AssemblyAI, Google, Azure, ElevenLabs, Rev.ai |
| 🤖 **8 AI providers** | Claude (Anthropic), OpenAI, Gemini, Groq, Mistral, Together AI, Perplexity, Ollama |
| 🗣️ **37+ languages** | Auto-detect or select, with regional dialect variants and Indian language support |
| 🎯 **Interview Mode** | Always-on — scores every question Great / Good / Needs Improvement / Missed, 10-point overall score |
| 📊 **Deep Analysis** | Deflection rate, advancement likelihood %, coaching guide, prep tips |
| 📝 **Smart reports** | Summary, key points, action items, speaker profiles, speech analytics |
| 📁 **History tab** | Every session saved locally — tokens, cost, score, full Q&A replay |
| 📤 **Exports** | .txt, .docx, .pdf, .srt subtitles, .vtt subtitles, .json |
| 🌐 **Network monitor** | Always-live download/upload speed, animated bars, session totals |
| ⏱️ **ETA at every step** | Step tracker + time remaining for Loading, Extracting, Transcribing, and AI Analysis |
| ⏹️ **Stop & resume** | Cancel mid-job; re-submit the same file to resume from the saved transcript checkpoint |

---

## Supported formats

| Type | Formats |
|---|---|
| Audio | mp3, wav, m4a, flac, ogg, aac, wma |
| Video | mp4, mov, avi, mkv, webm |
| Docs | pdf, docx, txt, md, srt, vtt |

---

## Quick start

### Run locally (Python)

```bash
pip install gradio anthropic openai groq pdfplumber fpdf2 python-docx \
            deepgram-sdk assemblyai elevenlabs rev_ai \
            fastapi uvicorn python-multipart httpx requests
python app.py
# Opens http://localhost:7860
```

### Run with Docker

```bash
docker compose up
# or
docker run -p 7860:7860 ghcr.io/jayuan101/transcript-agent
```

### Windows desktop app

1. Download `TranscriptAgent-win64.zip` + `Install-TranscriptAgent.bat` from [Releases](https://github.com/jayuan101/transcript-agent/releases/latest)
2. Put both files in the same folder, double-click the `.bat`
3. It extracts, creates a Desktop shortcut, and launches automatically

### Mac desktop app

1. Download `TranscriptAgent.dmg` from [Releases](https://github.com/jayuan101/transcript-agent/releases/latest)
2. Open → drag to Applications → double-click to launch

---

## How to use

1. Enter your API key (Claude, OpenAI, Groq, etc.) in the sidebar
2. Choose your STT engine and AI provider
3. Upload a file or paste a URL / local path
4. Click **▶ Analyze**

Interview Mode is always active — every question in the audio is automatically scored and a coaching guide is generated.

---

## Architecture

```
app.py              — Gradio UI, processing loop, all frontend logic
transcript_agent.py — STT dispatch, LLM analysis, report generation
launcher.py         — PyInstaller entry point (opens browser on start)
```

---

## Releases

See [CHANGELOG.md](CHANGELOG.md) for full version history. Latest: **v1.1.10**
