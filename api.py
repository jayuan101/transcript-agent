#!/usr/bin/env python3
"""
Transcript Agent — REST API (port 8000)
Your website calls these endpoints to drive the transcription engine in Docker.

Endpoints:
  POST /api/transcribe        Upload a file → get job_id back immediately
  GET  /api/jobs/{job_id}     Poll for status / results
  GET  /api/jobs/{job_id}/log Stream the live processing log
  POST /api/transcribe/sync   Upload + wait for result (small files only)
  GET  /api/providers         List all AI providers and their models
  GET  /api/stt-providers     List all STT (transcription) providers and models
  GET  /health                Health check
  GET  /docs                  Auto-generated interactive API docs (Swagger)
"""

import os
import uuid
import tempfile
import threading
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, UploadFile, File, Form, HTTPException, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse
import uvicorn
from dotenv import load_dotenv

from transcript_agent import run, ReportConfig, AUDIO_EXTS, VIDEO_EXTS

load_dotenv(Path(__file__).parent / ".env")

# ── app ───────────────────────────────────────────────────────────────────────

app = FastAPI(
    title="Transcript Agent API",
    description=(
        "Upload audio, video, or documents — get back transcripts, summaries, "
        "and speaker analysis.\n\n"
        "Supports **Whisper** (local, free) and **Deepgram** (cloud, fast) for "
        "transcription, and any AI provider for analysis (Claude, OpenAI, Gemini, Groq, …)."
    ),
    version="3.9.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

OUTPUTS_DIR = Path(__file__).parent / "outputs"
OUTPUTS_DIR.mkdir(exist_ok=True)

# ── provider catalogs (mirrors app.py _PROVIDERS / _STT_PROVIDERS) ────────────

AI_PROVIDERS = {
    "claude": {
        "display": "Claude (Anthropic)",
        "type": "anthropic",
        "key_env": "ANTHROPIC_API_KEY",
        "key_header": "sk-ant-api03-…",
        "docs": "console.anthropic.com → API keys",
        "models": [
            "claude-opus-4-7",
            "claude-sonnet-4-6",
            "claude-haiku-4-5-20251001",
            "claude-3-5-sonnet-20241022",
            "claude-3-5-haiku-20241022",
            "claude-3-opus-20240229",
        ],
        "base_url": None,
    },
    "openai": {
        "display": "OpenAI",
        "type": "openai",
        "key_env": "OPENAI_API_KEY",
        "key_header": "sk-…",
        "docs": "platform.openai.com → API keys",
        "models": [
            "gpt-4.1",
            "gpt-4.1-mini",
            "gpt-4o",
            "gpt-4o-mini",
            "o3",
            "o3-mini",
            "o1",
            "gpt-4-turbo",
            "gpt-3.5-turbo",
        ],
        "base_url": None,
    },
    "gemini": {
        "display": "Google Gemini",
        "type": "openai_compat",
        "key_env": "GEMINI_API_KEY",
        "key_header": "AIzaSy…",
        "docs": "aistudio.google.com → Get API key",
        "models": [
            "gemini-2.5-pro",
            "gemini-2.5-flash",
            "gemini-2.5-pro-preview-05-06",
            "gemini-2.0-flash-exp",
            "gemini-1.5-pro",
            "gemini-1.5-flash",
        ],
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai/",
    },
    "groq": {
        "display": "Groq",
        "type": "openai_compat",
        "key_env": "GROQ_API_KEY",
        "key_header": "gsk_…",
        "docs": "console.groq.com → API keys",
        "models": [
            "llama-3.3-70b-versatile",
            "llama-3.1-70b-versatile",
            "llama3-70b-8192",
            "deepseek-r1-distill-llama-70b",
            "mixtral-8x7b-32768",
            "gemma2-9b-it",
        ],
        "base_url": "https://api.groq.com/openai/v1",
    },
    "mistral": {
        "display": "Mistral",
        "type": "openai_compat",
        "key_env": "MISTRAL_API_KEY",
        "key_header": "…",
        "docs": "console.mistral.ai → API keys",
        "models": [
            "mistral-large-latest",
            "mistral-medium-latest",
            "mistral-small-latest",
            "open-mixtral-8x22b",
        ],
        "base_url": "https://api.mistral.ai/v1",
    },
    "together": {
        "display": "Together AI",
        "type": "openai_compat",
        "key_env": "TOGETHER_API_KEY",
        "key_header": "…",
        "docs": "api.together.ai → Settings → API keys",
        "models": [
            "meta-llama/Llama-3-70b-chat-hf",
            "meta-llama/Meta-Llama-3.1-70B-Instruct-Turbo",
            "Qwen/Qwen2-72B-Instruct",
            "mistralai/Mixtral-8x22B-Instruct-v0.1",
        ],
        "base_url": "https://api.together.xyz/v1",
    },
    "perplexity": {
        "display": "Perplexity",
        "type": "openai_compat",
        "key_env": "PERPLEXITY_API_KEY",
        "key_header": "pplx-…",
        "docs": "perplexity.ai → Settings → API",
        "models": [
            "llama-3.1-sonar-large-128k-online",
            "llama-3.1-sonar-huge-128k-online",
            "llama-3.1-sonar-small-128k-online",
        ],
        "base_url": "https://api.perplexity.ai",
    },
    "ollama": {
        "display": "Ollama (Local)",
        "type": "openai_compat",
        "key_env": None,
        "key_header": "none required",
        "docs": "ollama.ai — run models locally, no API key needed",
        "models": [
            "llama3.2",
            "llama3.1:70b",
            "mistral",
            "gemma2:27b",
            "qwen2.5:72b",
            "phi3.5",
        ],
        "base_url": "http://localhost:11434/v1",
    },
}

STT_PROVIDERS = {
    "whisper": {
        "display": "Whisper (Local)",
        "description": "OpenAI Whisper — runs on the server, free, private",
        "key_required": False,
        "models": ["tiny", "base", "small", "medium", "large"],
        "default_model": "base",
    },
    "deepgram": {
        "display": "Deepgram (Cloud)",
        "description": "Deepgram Nova — cloud API, fast and highly accurate",
        "key_required": True,
        "key_header": "dg-…",
        "docs": "console.deepgram.com → Create API Key",
        "models": [
            "nova-2",
            "nova-2-general",
            "nova-2-meeting",
            "nova-2-phonecall",
            "nova-2-voicemail",
            "nova",
            "enhanced",
            "base",
            "whisper-large",
            "whisper-medium",
            "whisper-small",
        ],
        "default_model": "nova-2",
    },
}

# ── in-memory job store ───────────────────────────────────────────────────────

_jobs: dict[str, dict] = {}
_jobs_lock = threading.Lock()


def _set_job(job_id: str, **kwargs):
    with _jobs_lock:
        _jobs.setdefault(job_id, {}).update(kwargs)


def _get_job(job_id: str) -> dict:
    with _jobs_lock:
        return dict(_jobs.get(job_id, {}))


# ── background worker ─────────────────────────────────────────────────────────

def _run_transcription(
    job_id: str,
    file_path: str,
    stem: str,
    # STT
    stt_provider: str,
    whisper_model: str,
    deepgram_api_key: Optional[str],
    deepgram_model: str,
    # language / speakers
    language: Optional[str],
    panel_mode: bool,
    num_speakers: Optional[int],
    # AI analysis
    ai_provider_key: str,
    ai_provider_type: str,
    ai_model: str,
    ai_base_url: Optional[str],
    report_style: str,
):
    log_lines: list[str] = []

    def on_log(msg: str):
        log_lines.append(msg)
        _set_job(job_id, progress=msg, log=list(log_lines))

    try:
        config  = ReportConfig(style=report_style)
        job_dir = str(OUTPUTS_DIR / job_id)

        _set_job(job_id, status="processing", log=log_lines)

        result = run(
            file_path=file_path,
            output_dir=job_dir,
            whisper_model=whisper_model,
            panel_mode=panel_mode,
            num_speakers=num_speakers,
            config=config,
            api_key=ai_provider_key,
            provider=ai_provider_type,
            model=ai_model or None,
            base_url=ai_base_url or None,
            language=language or None,
            on_log=on_log,
            stt_provider=stt_provider,
            deepgram_api_key=deepgram_api_key or None,
            deepgram_model=deepgram_model,
        )

        _set_job(job_id,
            status="done",
            transcript=result.clean_transcript,
            summary=result.summary,
            key_points=result.key_points,
            action_items=result.action_items,
            speaker_dialogue=result.speaker_dialogue,
            speaker_map=result.speaker_map,
            speaker_profiles=result.speaker_profiles,
            speaker_stats=[
                {
                    "name": s.name,
                    "words_per_minute": s.words_per_minute,
                    "pace_label": s.pace_label,
                    "speaking_percentage": s.speaking_percentage,
                    "accent_indicators": s.accent_indicators,
                    "accent_confidence": s.accent_confidence,
                }
                for s in result.speaker_stats
            ],
            log=list(log_lines),
        )

    except Exception as e:
        _set_job(job_id, status="error", error=str(e), log=list(log_lines))
    finally:
        try:
            os.unlink(file_path)
        except Exception:
            pass


def _resolve_ai(
    ai_provider: str,
    ai_model: Optional[str],
    ai_api_key: Optional[str],
) -> tuple[str, str, Optional[str], Optional[str]]:
    """Return (api_key, provider_type, resolved_model, base_url)."""
    p = AI_PROVIDERS.get(ai_provider.lower())
    if not p:
        # Fall back to Claude
        p = AI_PROVIDERS["claude"]
    key = (ai_api_key or "").strip() or (
        os.environ.get(p["key_env"], "") if p.get("key_env") else ""
    )
    ptype = p["type"]
    model = (ai_model or "").strip() or p["models"][0]
    base_url = p.get("base_url")
    return key, ptype, model, base_url


# ── endpoints ─────────────────────────────────────────────────────────────────

@app.get("/", include_in_schema=False)
def root():
    return RedirectResponse(url="/docs")


@app.get("/health")
def health():
    return {"status": "ok", "service": "transcript-agent", "version": "3.9.0"}


@app.get("/api/providers", summary="List available AI analysis providers and models")
def list_providers():
    """
    Returns all supported AI providers with their available models.
    Pass the provider `id` (key) as `ai_provider` and any model from its list as `ai_model`
    when calling `/api/transcribe`.
    """
    return {
        k: {
            "display": v["display"],
            "type": v["type"],
            "key_required": bool(v.get("key_env")),
            "key_format": v.get("key_header"),
            "docs": v.get("docs"),
            "models": v["models"],
        }
        for k, v in AI_PROVIDERS.items()
    }


@app.get("/api/stt-providers", summary="List available transcription (STT) providers and models")
def list_stt_providers():
    """
    Returns supported Speech-to-Text providers.
    Pass the provider `id` as `stt_provider` when calling `/api/transcribe`.
    - `whisper` — runs locally, no API key needed
    - `deepgram` — cloud API, fast; requires a `deepgram_api_key`
    """
    return STT_PROVIDERS


@app.post("/api/transcribe", summary="Start async transcription")
async def transcribe_async(
    background_tasks: BackgroundTasks,
    file:             UploadFile    = File(..., description="Audio, video, or document to transcribe"),
    # ── STT ────────────────────────────────────────────────────────────────────
    stt_provider:     str           = Form("whisper",  description="whisper | deepgram"),
    whisper_model:    str           = Form("base",     description="tiny | base | small | medium | large  (Whisper only)"),
    deepgram_api_key: Optional[str] = Form(None,       description="Deepgram API key — required when stt_provider=deepgram"),
    deepgram_model:   str           = Form("nova-2",   description="Deepgram model  (see GET /api/stt-providers)"),
    # ── language / speakers ────────────────────────────────────────────────────
    language:         Optional[str] = Form(None,       description="ISO code e.g. 'en', 'es'. None = auto-detect"),
    panel_mode:       bool          = Form(False,       description="Enable multi-speaker diarization"),
    num_speakers:     Optional[int] = Form(None,        description="Expected speaker count (2–20)"),
    # ── AI analysis ────────────────────────────────────────────────────────────
    ai_provider:      str           = Form("claude",   description="AI provider id — see GET /api/providers"),
    ai_model:         Optional[str] = Form(None,       description="Model name — see GET /api/providers. Defaults to provider's best model."),
    ai_api_key:       Optional[str] = Form(None,       description="API key for the chosen AI provider. Falls back to env var if omitted."),
    report_style:     str           = Form("formal",   description="formal | casual | executive | bullet"),
):
    """
    Upload a file and get a **job_id** back immediately.
    Poll `GET /api/jobs/{job_id}` for status and results.

    **STT providers:** `whisper` (local, free) · `deepgram` (cloud, fast)
    **AI providers:** `claude` · `openai` · `gemini` · `groq` · `mistral` · `together` · `perplexity` · `ollama`

    Supported files: `.mp3 .wav .m4a .mp4 .mov .mkv .webm .srt .vtt .pdf .docx .txt`
    """
    if stt_provider == "deepgram" and not (deepgram_api_key or "").strip():
        raise HTTPException(status_code=422, detail="deepgram_api_key is required when stt_provider=deepgram")

    ai_key, ai_type, ai_mdl, ai_base = _resolve_ai(ai_provider, ai_model, ai_api_key)

    job_id = uuid.uuid4().hex[:12]
    suffix = Path(file.filename or "upload").suffix or ".tmp"
    stem   = Path(file.filename or "upload").stem

    tmp = tempfile.NamedTemporaryFile(suffix=suffix, delete=False)
    tmp.write(await file.read())
    tmp.close()

    _set_job(job_id, status="queued", filename=file.filename, progress="Queued",
             stt_provider=stt_provider, ai_provider=ai_provider, ai_model=ai_mdl)

    background_tasks.add_task(
        _run_transcription,
        job_id, tmp.name, stem,
        stt_provider, whisper_model, deepgram_api_key, deepgram_model,
        language, panel_mode, num_speakers,
        ai_key, ai_type, ai_mdl, ai_base,
        report_style,
    )

    return {
        "job_id": job_id,
        "status": "queued",
        "poll_url": f"/api/jobs/{job_id}",
        "stt_provider": stt_provider,
        "ai_provider": ai_provider,
        "ai_model": ai_mdl,
    }


@app.get("/api/jobs/{job_id}", summary="Get job status and results")
def get_job(job_id: str):
    """
    Poll after calling `/api/transcribe`.

    **status** values: `queued` · `processing` · `done` · `error`
    """
    job = _get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found")
    return job


@app.get("/api/jobs/{job_id}/log", summary="Get live processing log")
def get_job_log(job_id: str):
    """Returns log lines captured so far — useful for a progress stream on your website."""
    job = _get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found")
    return {"job_id": job_id, "status": job.get("status"), "log": job.get("log", [])}


@app.post("/api/transcribe/sync", summary="Transcribe and wait for result")
async def transcribe_sync(
    file:             UploadFile    = File(...),
    stt_provider:     str           = Form("whisper"),
    whisper_model:    str           = Form("base"),
    deepgram_api_key: Optional[str] = Form(None),
    deepgram_model:   str           = Form("nova-2"),
    language:         Optional[str] = Form(None),
    panel_mode:       bool          = Form(False),
    num_speakers:     Optional[int] = Form(None),
    ai_provider:      str           = Form("claude"),
    ai_model:         Optional[str] = Form(None),
    ai_api_key:       Optional[str] = Form(None),
    report_style:     str           = Form("formal"),
):
    """
    Same as `/api/transcribe` but **waits** and returns the full result in one response.
    Best for short documents or text files. Use the async endpoint for audio/video.
    """
    if stt_provider == "deepgram" and not (deepgram_api_key or "").strip():
        raise HTTPException(status_code=422, detail="deepgram_api_key is required when stt_provider=deepgram")

    ai_key, ai_type, ai_mdl, ai_base = _resolve_ai(ai_provider, ai_model, ai_api_key)

    job_id = uuid.uuid4().hex[:12]
    suffix = Path(file.filename or "upload").suffix or ".tmp"
    stem   = Path(file.filename or "upload").stem

    tmp = tempfile.NamedTemporaryFile(suffix=suffix, delete=False)
    tmp.write(await file.read())
    tmp.close()

    _set_job(job_id, status="processing")

    _run_transcription(
        job_id, tmp.name, stem,
        stt_provider, whisper_model, deepgram_api_key, deepgram_model,
        language, panel_mode, num_speakers,
        ai_key, ai_type, ai_mdl, ai_base,
        report_style,
    )

    result = _get_job(job_id)
    if result.get("status") == "error":
        raise HTTPException(status_code=500, detail=result.get("error"))
    return result


# ── run ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    port = int(os.environ.get("API_PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")
