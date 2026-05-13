#!/usr/bin/env python3
"""
Transcript Agent — REST API (port 8000)
Your website calls these endpoints to drive the transcription engine in Docker.

Endpoints:
  POST /api/transcribe        Upload a file → get job_id back immediately
  GET  /api/jobs/{job_id}     Poll for status / results
  GET  /api/jobs/{job_id}/log Stream the live processing log
  POST /api/transcribe/sync   Upload + wait for result (small files only)
  GET  /health                Health check
  GET  /docs                  Auto-generated interactive API docs (Swagger)
"""

import os
import uuid
import tempfile
import threading
from pathlib import Path
from dataclasses import asdict
from typing import Optional

from fastapi import FastAPI, UploadFile, File, Form, HTTPException, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse, RedirectResponse, HTMLResponse
import uvicorn
from dotenv import load_dotenv

from transcript_agent import run, ReportConfig, AUDIO_EXTS, VIDEO_EXTS

load_dotenv(Path(__file__).parent / ".env")

# ── app ───────────────────────────────────────────────────────────────────────

app = FastAPI(
    title="Transcript Agent API",
    description="Upload audio, video, or documents — get back transcripts, summaries, and speaker analysis.",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],       # tighten to your website domain in production
    allow_methods=["*"],
    allow_headers=["*"],
)

OUTPUTS_DIR = Path(__file__).parent / "outputs"
OUTPUTS_DIR.mkdir(exist_ok=True)

# ── in-memory job store ───────────────────────────────────────────────────────
# Keys: job_id → dict with status, log, result fields
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
    whisper_model: str,
    language: Optional[str],
    panel_mode: bool,
    num_speakers: Optional[int],
    report_style: str,
):
    log_lines: list[str] = []

    def on_log(msg: str):
        log_lines.append(msg)
        _set_job(job_id, progress=msg, log=list(log_lines))

    try:
        api_key = os.environ.get("ANTHROPIC_API_KEY", "")
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
            api_key=api_key,
            language=language or None,
            on_log=on_log,
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


# ── endpoints ─────────────────────────────────────────────────────────────────

@app.get("/", include_in_schema=False)
def root():
    """Root — redirect to the interactive API docs."""
    return RedirectResponse(url="/docs")


@app.get("/health")
def health():
    """Quick health check — returns 200 if the API is running."""
    return {"status": "ok", "service": "transcript-agent"}


@app.post("/api/transcribe", summary="Start async transcription")
async def transcribe_async(
    background_tasks: BackgroundTasks,
    file:          UploadFile    = File(..., description="Audio, video, or document to transcribe"),
    whisper_model: str           = Form("base",   description="tiny | base | small | medium | large"),
    language:      Optional[str] = Form(None,     description="ISO code e.g. 'en', 'es'. None = auto-detect"),
    panel_mode:    bool          = Form(False,     description="Enable multi-speaker diarization"),
    num_speakers:  Optional[int] = Form(None,      description="Expected speaker count (2-20)"),
    report_style:  str           = Form("formal",  description="formal | casual | executive | bullet"),
):
    """
    Upload a file and get a **job_id** back immediately.
    Poll `GET /api/jobs/{job_id}` for status and results.

    Works with: `.mp3 .wav .m4a .mp4 .mov .mkv .webm .srt .vtt .pdf .docx .txt`
    """
    job_id = uuid.uuid4().hex[:12]
    stem   = Path(file.filename or "upload").stem
    suffix = Path(file.filename or "upload").suffix or ".tmp"

    # save uploaded file to a temp path
    tmp = tempfile.NamedTemporaryFile(suffix=suffix, delete=False)
    tmp.write(await file.read())
    tmp.close()

    _set_job(job_id, status="queued", filename=file.filename, progress="Queued")

    background_tasks.add_task(
        _run_transcription,
        job_id, tmp.name, stem,
        whisper_model, language, panel_mode, num_speakers, report_style,
    )

    return {"job_id": job_id, "status": "queued", "poll_url": f"/api/jobs/{job_id}"}


@app.get("/api/jobs/{job_id}", summary="Get job status and results")
def get_job(job_id: str):
    """
    Poll this after calling `/api/transcribe`.

    **status** values:
    - `queued`     — waiting to start
    - `processing` — actively running
    - `done`       — results are ready
    - `error`      — something failed (`error` field has details)
    """
    job = _get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found")
    return job


@app.get("/api/jobs/{job_id}/log", summary="Get live processing log")
def get_job_log(job_id: str):
    """Returns the log lines captured so far — useful for showing progress on your website."""
    job = _get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found")
    return {"job_id": job_id, "status": job.get("status"), "log": job.get("log", [])}


@app.post("/api/transcribe/sync", summary="Transcribe and wait for result")
async def transcribe_sync(
    file:          UploadFile    = File(...),
    whisper_model: str           = Form("base"),
    language:      Optional[str] = Form(None),
    panel_mode:    bool          = Form(False),
    num_speakers:  Optional[int] = Form(None),
    report_style:  str           = Form("formal"),
):
    """
    Same as `/api/transcribe` but **waits** and returns the full result in one response.
    Best for short documents or text files.
    For audio/video files use the async endpoint to avoid HTTP timeouts.
    """
    job_id = uuid.uuid4().hex[:12]
    stem   = Path(file.filename or "upload").stem
    suffix = Path(file.filename or "upload").suffix or ".tmp"

    tmp = tempfile.NamedTemporaryFile(suffix=suffix, delete=False)
    tmp.write(await file.read())
    tmp.close()

    _set_job(job_id, status="processing")

    # run synchronously (blocks until done)
    _run_transcription(
        job_id, tmp.name, stem,
        whisper_model, language, panel_mode, num_speakers, report_style,
    )

    result = _get_job(job_id)
    if result.get("status") == "error":
        raise HTTPException(status_code=500, detail=result.get("error"))
    return result


# ── run ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    port = int(os.environ.get("API_PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")
