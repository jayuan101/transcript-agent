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
import sys
import json
import time
import uuid
import tempfile
import threading
from pathlib import Path
from dataclasses import asdict
from typing import Optional

from fastapi import FastAPI, UploadFile, File, Form, HTTPException, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse, HTMLResponse, FileResponse
from fastapi.staticfiles import StaticFiles
import uvicorn
from dotenv import load_dotenv

from transcript_agent import (
    run, ReportConfig, AUDIO_EXTS, VIDEO_EXTS,
    load_history, save_history_entry, STT_ENGINES,
    TranscriptResult, generate_docx, extract_profile_text, LLMClient,
    stt_transcribe,
)

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


# ── history + cost (shared with the Gradio app so it's saved no matter the UI) ──
def _user_data_dir() -> Path:
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA") or Path.home())
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    return base / "TranscriptAgent"

_DATA_OUT    = _user_data_dir() / "outputs"
_DATA_OUT.mkdir(parents=True, exist_ok=True)
HISTORY_PATH = _DATA_OUT / "history.jsonl"   # same file the desktop UI writes
TRASH_PATH   = _DATA_OUT / "trash.jsonl"

# App version + release repo — mirrored from app.py so the "Check for Updates"
# banner works in the React UI too. Read APP_VERSION straight from app.py so the
# two never drift.
_GH_RELEASES_REPO = "jayuan101/transcript-agent"
def _read_app_version() -> str:
    try:
        import re as _re
        txt = (Path(__file__).parent / "app.py").read_text(encoding="utf-8")
        m = _re.search(r'APP_VERSION\s*=\s*"([^"]+)"', txt)
        if m:
            return m.group(1)
    except Exception:
        pass
    return "0.0.0"
APP_VERSION = _read_app_version()

# Model → ($/1M input, $/1M output). Mirrors app.py _MODEL_PRICING.
_MODEL_PRICING: dict[str, tuple] = {
    "claude-opus-4-8":            (15.00, 75.00),
    "claude-sonnet-4-6":          ( 3.00, 15.00),
    "claude-haiku-4-5-20251001":  ( 0.80,  4.00),
    "claude-3-5-sonnet-20241022": ( 3.00, 15.00),
    "claude-3-5-haiku-20241022":  ( 0.80,  4.00),
    "gpt-4.1":      (2.00, 8.00), "gpt-4.1-mini": (0.40, 1.60), "gpt-4.1-nano": (0.10, 0.40),
    "gpt-4o":       (2.50, 10.00), "gpt-4o-mini": (0.15, 0.60),
    "o3": (10.00, 40.00), "o3-mini": (1.10, 4.40), "o4-mini": (1.10, 4.40),
    "gemini-2.5-pro": (1.25, 10.00), "gemini-2.5-flash": (0.15, 0.60),
    "gemini-2.0-flash": (0.075, 0.30), "gemini-2.0-flash-lite": (0.019, 0.075),
}

def _cost_usd(model: str, tok_in: int, tok_out: int) -> float:
    p = _MODEL_PRICING.get(model or "")
    if not p or not (tok_in or tok_out):
        return 0.0
    return round((tok_in or 0) / 1_000_000 * p[0] + (tok_out or 0) / 1_000_000 * p[1], 6)

def _entry_public(e: dict) -> dict:
    """A history entry plus a computed cost, for the API/UI."""
    tok_in, tok_out = e.get("tok_in", 0) or 0, e.get("tok_out", 0) or 0
    return {
        "id": e.get("id"), "timestamp": e.get("timestamp"), "filename": e.get("filename"),
        "ai_model": e.get("ai_model"), "ai_provider": e.get("ai_provider"),
        "stt_engine": e.get("stt_engine"), "language": e.get("language"),
        "word_count": e.get("word_count"), "overall_score": e.get("overall_score"),
        "overall_verdict": e.get("overall_verdict"), "summary": e.get("summary"),
        "tok_in": tok_in, "tok_out": tok_out,
        "cost_usd": _cost_usd(e.get("ai_model"), tok_in, tok_out),
    }

# ── in-memory job store ───────────────────────────────────────────────────────
# Keys: job_id → dict with status, log, result fields
_jobs: dict[str, dict] = {}
_jobs_lock = threading.Lock()

# Per-job cancel events — set by POST /api/jobs/{id}/cancel and passed to run()
# (which checks it before the LLM analysis stage and aborts cleanly).
_cancel_events: dict[str, "threading.Event"] = {}


def _set_job(job_id: str, **kwargs):
    with _jobs_lock:
        _jobs.setdefault(job_id, {}).update(kwargs)


def _get_job(job_id: str) -> dict:
    with _jobs_lock:
        return dict(_jobs.get(job_id, {}))


# ── LLM pricing (input $/MTok, output $/MTok) — for the live cost estimate ─────
_MODEL_PRICING: dict[str, tuple[float, float]] = {
    "claude-opus-4-8": (15.00, 75.00),
    "claude-sonnet-4-6": (3.00, 15.00),
    "claude-haiku-4-5-20251001": (0.80, 4.00),
    "claude-3-5-sonnet-20241022": (3.00, 15.00),
    "claude-3-5-haiku-20241022": (0.80, 4.00),
    "gpt-4.1": (2.00, 8.00), "gpt-4.1-mini": (0.40, 1.60), "gpt-4.1-nano": (0.10, 0.40),
    "gpt-4o": (2.50, 10.00), "gpt-4o-mini": (0.15, 0.60),
    "o3": (10.00, 40.00), "o3-mini": (1.10, 4.40), "o4-mini": (1.10, 4.40),
    "gemini-2.5-pro": (1.25, 10.00), "gemini-2.5-flash": (0.15, 0.60),
    "gemini-2.0-flash": (0.075, 0.30), "gemini-2.0-flash-lite": (0.019, 0.075),
}


def _estimate_cost(model: str, tok_in: int, tok_out: int):
    p = _MODEL_PRICING.get(model or "")
    if not p:
        return None
    return round(tok_in / 1_000_000 * p[0] + tok_out / 1_000_000 * p[1], 4)


def _write_pdf(result, stem: str, out_dir: str, suffix: str = "") -> Optional[str]:
    """Render a simple PDF report from a TranscriptResult. Returns the path or None.

    Uses fpdf2 (core latin-1 fonts), so non-latin glyphs are replaced rather than
    crashing — the DOCX export keeps full-fidelity Unicode.
    """
    try:
        from fpdf import FPDF
    except Exception:
        return None

    def _s(text):
        s = str(text or "").encode("latin-1", "replace").decode("latin-1")
        # Pre-break any unbroken token longer than 40 chars (URLs, long ids) so
        # multi_cell's word-wrap never hits "Not enough horizontal space" — and
        # we avoid fpdf2's buggy wrapmode="CHAR" (which can infinite-loop).
        out = []
        for tok in s.split(" "):
            while len(tok) > 40:
                out.append(tok[:40]); tok = tok[40:]
            out.append(tok)
        return " ".join(out)

    try:
        pdf = FPDF()
        pdf.set_auto_page_break(True, margin=15)
        pdf.add_page()

        # _s() pre-breaks long tokens, so default word-wrap is safe here.
        # new_x=LMARGIN/new_y=NEXT returns the cursor to the left margin on a new
        # line after each cell — otherwise a short heading leaves x at the right
        # edge and the next cell has ~0 width ("not enough horizontal space").
        def _cell(h, text):
            pdf.multi_cell(0, h, _s(text), new_x="LMARGIN", new_y="NEXT")

        pdf.set_font("Helvetica", "B", 16)
        _cell(9, stem)
        pdf.ln(2)

        meta = []
        if getattr(result, "detected_language", ""):
            meta.append(f"Language: {result.detected_language}")
        if getattr(result, "stt_engine", ""):
            meta.append(f"STT: {result.stt_engine}")
        if meta:
            pdf.set_font("Helvetica", "", 10)
            pdf.set_text_color(110, 110, 110)
            _cell(6, "   ".join(meta))
            pdf.set_text_color(0, 0, 0)
            pdf.ln(2)

        def _section(title, body_lines):
            if not body_lines:
                return
            pdf.set_font("Helvetica", "B", 13)
            _cell(8, title)
            pdf.set_font("Helvetica", "", 11)
            for line in body_lines:
                _cell(6, line)
            pdf.ln(2)

        if getattr(result, "summary", ""):
            _section("Summary", [result.summary])
        if getattr(result, "key_points", []):
            _section("Key Points", [f"- {p}" for p in result.key_points])
        if getattr(result, "action_items", []):
            def _ai(a):
                if isinstance(a, dict):
                    return f"- {a.get('action', a.get('item', a))}"
                return f"- {a}"
            _section("Action Items", [_ai(a) for a in result.action_items])
        # Keep the PDF a concise *report* — the full transcript lives in the DOCX
        # / .txt. Cap it so a huge transcript can't make the PDF render crawl.
        if getattr(result, "clean_transcript", ""):
            _txt = result.clean_transcript or ""
            _capped = _txt[:6000]
            lines = _capped.splitlines() or [_capped]
            if len(_txt) > len(_capped):
                lines.append("… (truncated — see the DOCX or .txt for the full transcript)")
            _section("Transcript (excerpt)", lines)

        path = str(Path(out_dir) / f"{stem}_report{suffix}.pdf")
        pdf.output(path)
        return path
    except Exception as _e:
        import traceback; print(f"[PDF] write failed: {_e}"); traceback.print_exc()
        return None


async def _save_upload(file: UploadFile, suffix: str) -> str:
    """Stream an upload to a temp file in 1 MB chunks.

    Avoids buffering the whole file in RAM — critical for multi-GB inputs such
    as a 3-hour video, where ``await file.read()`` would otherwise OOM the
    server. Returns the temp file path.
    """
    tmp = tempfile.NamedTemporaryFile(suffix=suffix, delete=False)
    try:
        while True:
            chunk = await file.read(1024 * 1024)  # 1 MB
            if not chunk:
                break
            tmp.write(chunk)
    finally:
        tmp.close()
        await file.close()
    return tmp.name


# ── background worker ─────────────────────────────────────────────────────────

def _download_to_temp(url: str, on_log=None, cancel_event=None) -> tuple[str, str]:
    """Stream a direct file URL to a temp file. Returns (temp_path, filename_stem).

    Handles only direct download links (S3, Dropbox direct, Nextcloud direct,
    plain http(s) files) — not YouTube/streaming sites. No read timeout, so
    multi-GB files download fine.
    """
    import re
    import requests
    from urllib.parse import urlparse, unquote

    if on_log:
        on_log(f"⬇ Downloading {url} …")
    with requests.get(
        url, stream=True, timeout=(15, None),
        headers={"User-Agent": "TranscriptAgent/1.0"}, allow_redirects=True,
    ) as resp:
        if resp.status_code in (401, 403):
            raise ValueError(
                f"URL returned {resp.status_code} — it needs a login or VPN. "
                "Download the file manually and upload it instead."
            )
        resp.raise_for_status()

        # Prefer the server-suggested filename, else fall back to the URL path.
        fname = None
        cd = resp.headers.get("Content-Disposition", "") or ""
        m = re.search(r"filename\*?=(?:UTF-8'')?\"?([^\";]+)\"?", cd)
        if m:
            fname = unquote(m.group(1).strip())
        if not fname:
            fname = os.path.basename(urlparse(url).path) or "download"
        suffix = Path(fname).suffix or ".tmp"
        stem = Path(fname).stem or "download"

        tmp = tempfile.NamedTemporaryFile(suffix=suffix, delete=False)
        total = 0
        cancelled = False
        try:
            for chunk in resp.iter_content(chunk_size=1024 * 1024):  # 1 MB
                if cancel_event and cancel_event.is_set():
                    cancelled = True
                    break
                if chunk:
                    tmp.write(chunk)
                    total += len(chunk)
        finally:
            tmp.close()
        if cancelled:
            try:
                os.unlink(tmp.name)
            except Exception:
                pass
            raise RuntimeError("Cancelled by user.")
        if total == 0:
            try:
                os.unlink(tmp.name)
            except Exception:
                pass
            raise ValueError("Downloaded 0 bytes — is the link a direct file URL?")
        if on_log:
            on_log(f"⬇ Downloaded {total // (1024 * 1024)} MB → {fname}")
        return tmp.name, stem


def _scan_downloads(job_dir: str) -> list[dict]:
    """List downloadable result files produced in a job's output dir."""
    d = Path(job_dir)
    if not d.exists():
        return []
    out = []
    for p in sorted(d.glob("*")):
        if p.is_file():
            out.append({"name": p.name, "size": p.stat().st_size})
    return out


def _net_monitor_loop(job_id: str, stop_event: "threading.Event"):
    """Sample system network counters every second and publish per-job totals so
    the UI can show, in real time, how much data the run is pulling/pushing
    (uploading audio to a cloud STT, downloading models, LLM traffic, …).
    Counters are system-wide (psutil), so they include other machine traffic —
    treated as a live throughput gauge, not a per-process meter."""
    try:
        import psutil
    except Exception:
        return
    base = psutil.net_io_counters()
    t0 = last_t = time.time()
    last = base
    while not stop_event.wait(1.0):
        cur = psutil.net_io_counters()
        now = time.time()
        dt = max(1e-3, now - last_t)
        _set_job(job_id, network={
            "recv_mb":    round(max(0, cur.bytes_recv - base.bytes_recv) / 1_048_576, 2),
            "sent_mb":    round(max(0, cur.bytes_sent - base.bytes_sent) / 1_048_576, 2),
            "dn_rate_mbs": round(max(0, cur.bytes_recv - last.bytes_recv) / dt / 1_048_576, 2),
            "up_rate_mbs": round(max(0, cur.bytes_sent - last.bytes_sent) / dt / 1_048_576, 2),
            "elapsed":    round(now - t0, 1),
        })
        last, last_t = cur, now


def _run_transcription(
    job_id: str,
    file_path: str,
    stem: str,
    whisper_model: str,
    language: Optional[str],
    panel_mode: bool,
    num_speakers: Optional[int],
    report_style: str,
    interview_mode: bool = False,
    interview_deep: bool = False,
    candidate_profile: str = "",
    provider: str = "anthropic",
    model: Optional[str] = None,
    base_url: Optional[str] = None,
    llm_api_key: str = "",
    stt_engine: str = "whisper_local",
    stt_api_key: str = "",
    stt_model: Optional[str] = None,
    language_variant: Optional[str] = None,
    transcription_only: bool = False,
    use_gpu: bool = True,
    include_summary: bool = True,
    include_key_points: bool = True,
    include_action_items: bool = True,
    include_transcript: bool = True,
    include_speaker_profiles: bool = True,
    include_speech_analytics: bool = True,
    source_url: str = "",
):
    log_lines: list[str] = []

    # Cancellation — the /cancel endpoint sets this; run() checks it before the
    # LLM stage and aborts. Registered here so it exists for the whole job.
    cancel_event = threading.Event()
    _cancel_events[job_id] = cancel_event

    # Live network monitor — runs for the whole job, stopped in `finally`.
    _net_stop = threading.Event()
    threading.Thread(target=_net_monitor_loop, args=(job_id, _net_stop), daemon=True).start()

    def on_log(msg: str, kind: str = None):
        # Cloud STT engines (Deepgram, AssemblyAI…) call on_log(msg, kind) with a
        # severity tag; local Whisper calls on_log(msg). Accept both so a cloud
        # engine doesn't crash the job with a "takes 1 positional argument" error.
        log_lines.append(msg)
        _set_job(job_id, progress=msg, log=list(log_lines))

    # Live token usage → cost, so the UI can show spend for this run.
    _tok = {"in": 0, "out": 0}
    def on_token_usage(total_in, total_out):
        _tok["in"], _tok["out"] = total_in or 0, total_out or 0
        _set_job(job_id, tok_in=_tok["in"], tok_out=_tok["out"],
                 cost_usd=_cost_usd(model or "", _tok["in"], _tok["out"]))

    try:
        # When no file was uploaded, download the source URL server-side first
        # (large files never pass through the browser).
        if not file_path and source_url:
            file_path, dl_stem = _download_to_temp(source_url, on_log=on_log, cancel_event=cancel_event)
            stem = dl_stem or stem

        # User-supplied LLM key wins; fall back to the server's env key only
        # when the selected provider is Anthropic (the env default).
        api_key = (llm_api_key or "").strip() or (
            os.environ.get("ANTHROPIC_API_KEY", "") if provider == "anthropic" else ""
        )
        config  = ReportConfig(
            style=report_style,
            include_summary=include_summary,
            include_key_points=include_key_points,
            include_action_items=include_action_items,
            include_transcript=include_transcript,
            include_speaker_profiles=include_speaker_profiles,
            include_speech_analytics=include_speech_analytics,
        )
        job_dir = str(OUTPUTS_DIR / job_id)

        _set_job(job_id, status="processing", log=log_lines, stem=stem, job_dir=job_dir)

        result = run(
            file_path=file_path,
            output_dir=job_dir,
            whisper_model=whisper_model,
            stt_engine=stt_engine or "whisper_local",
            stt_api_key=(stt_api_key or "").strip() or None,
            stt_model=stt_model or None,
            panel_mode=panel_mode,
            num_speakers=num_speakers,
            config=config,
            api_key=api_key,
            provider=provider or "anthropic",
            model=model or None,
            base_url=base_url or None,
            language=language or None,
            language_variant=language_variant or None,
            interview_mode=interview_mode,
            interview_deep=interview_deep,
            candidate_profile=candidate_profile or "",
            transcription_only=transcription_only,
            use_gpu=use_gpu,
            on_log=on_log,
            on_token_usage=on_token_usage,
            cancel_event=cancel_event,
            history_path=HISTORY_PATH,   # persisted to the shared history.jsonl
        )

        if cancel_event.is_set():
            _set_job(job_id, status="error", error="Cancelled by user.",
                     log=list(log_lines))
            return

        # ── Combined delivery analysis ─────────────────────────────────────────
        # Mirror the original UI: for an interview *video*, the same run also
        # scores on-screen delivery (body language, emotion, eye contact) and
        # produces an annotated video. Audio/documents skip this automatically.
        delivery = None
        _is_video = Path(file_path).suffix.lower() in {".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v"}
        if interview_mode and _is_video:
            try:
                on_log("🎥 Analyzing on-screen delivery (body language, emotion, eye contact)…")
                from video_analyzer import VideoAnalyzer
                analyzer = VideoAnalyzer()
                default_roles = ["Candidate", "Interviewer", "Panelist", "Panelist"]
                role_map = {i: default_roles[i] for i in range(4)}

                def _vpcb(v, info=None):
                    on_log(f"Delivery analysis… {int((v or 0) * 100)}%" + (f"  {info}" if info else ""))

                va = analyzer.analyze_video(
                    file_path, role_map, sample_fps=0.5,
                    progress_cb=_vpcb, annotate=True, use_gpu=use_gpu,
                )
                if getattr(va, "error", None):
                    on_log(f"⚠ Delivery analysis skipped: {va.error}")
                else:
                    annotated_name = None
                    if va.annotated_video_path and Path(va.annotated_video_path).is_file():
                        dest = Path(job_dir) / "annotated_video.mp4"
                        try:
                            Path(va.annotated_video_path).replace(dest)
                            annotated_name = dest.name
                        except Exception:
                            annotated_name = None
                    delivery = {
                        "overall_score": round(va.overall_score, 1),
                        "rapport_score": round(va.rapport_score, 1),
                        "talk_balance_score": round(va.talk_balance_score, 1),
                        "candidate_talk_pct": round(va.candidate_talk_pct, 1),
                        "duration_seconds": round(va.duration_seconds, 1),
                        "person_count": va.person_count,
                        "observations": va.observations,
                        "persons": [_person_to_dict(p) for p in va.persons.values()],
                        "annotated_video": annotated_name,
                    }
            except Exception as _ve:
                on_log(f"⚠ Delivery analysis failed: {_ve}")
                delivery = None

        # Also render a PDF report (DOCX is produced by run()), so the UI can
        # offer both DOCX and PDF downloads.
        try:
            if not transcription_only:
                on_log("📄 Generating PDF report…")
                _write_pdf(result, stem, job_dir)
        except Exception as _pe:
            on_log(f"⚠ PDF export skipped: {_pe}")

        _set_job(job_id,
            status="done",
            provider=provider or "anthropic",
            model=model or None,
            delivery=delivery,
            transcript=result.clean_transcript,
            summary=result.summary,
            key_points=result.key_points,
            action_items=result.action_items,
            speaker_dialogue=result.speaker_dialogue,
            speaker_map=result.speaker_map,
            speaker_profiles=result.speaker_profiles,
            interview_analysis=result.interview_analysis or {},
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
            downloads=_scan_downloads(job_dir),
            log=list(log_lines),
        )

    except Exception as e:
        _set_job(job_id, status="error", error=str(e), log=list(log_lines))
    finally:
        _net_stop.set()
        _cancel_events.pop(job_id, None)
        try:
            os.unlink(file_path)
        except Exception:
            pass


# ── endpoints ─────────────────────────────────────────────────────────────────

def _detect_device() -> dict:
    """Report the compute device local Whisper would actually use, so the UI can
    show it honestly and only offer GPU when one is really usable."""
    try:
        import torch
        if torch.cuda.is_available():
            return {"gpu_available": True, "device": "cuda",
                    "name": torch.cuda.get_device_name(0), "kind": "NVIDIA CUDA"}
        if hasattr(torch.backends, "mps") and torch.backends.mps.is_available():
            return {"gpu_available": True, "device": "mps", "name": "Apple Silicon", "kind": "Apple MPS"}
        try:
            import torch_directml  # AMD / Intel on Windows
            return {"gpu_available": True, "device": "dml",
                    "name": "DirectML GPU", "kind": "DirectML"}
        except Exception:
            pass
        return {"gpu_available": False, "device": "cpu", "name": "CPU",
                "kind": "CPU only", "reason": "No CUDA / MPS / DirectML available in this environment."}
    except Exception as e:
        return {"gpu_available": False, "device": "cpu", "name": "CPU", "kind": "CPU only", "reason": str(e)}


@app.get("/api/devices", summary="Available compute device (GPU/CPU) for local Whisper")
def get_devices():
    """Tells the UI whether a GPU is usable. Note: this only affects local
    Whisper — cloud STT engines (Deepgram, AssemblyAI…) run on their servers and
    don't use your machine's GPU/CPU at all."""
    return _detect_device()


@app.get("/health")
def health():
    """Quick health check — returns 200 if the API is running."""
    return {"status": "ok", "service": "transcript-agent"}


@app.post("/api/transcribe", summary="Start async transcription")
async def transcribe_async(
    background_tasks: BackgroundTasks,
    file:          Optional[UploadFile] = File(None, description="Audio, video, or document to transcribe (or use source_url)"),
    source_url:    str           = Form("",       description="Direct file URL to download server-side instead of uploading"),
    whisper_model: str           = Form("base",   description="tiny | base | small | medium | large"),
    language:      Optional[str] = Form(None,     description="ISO code e.g. 'en', 'es'. None = auto-detect"),
    panel_mode:    bool          = Form(False,     description="Enable multi-speaker diarization"),
    num_speakers:  Optional[int] = Form(None,      description="Expected speaker count (2-20)"),
    report_style:  str           = Form("formal",  description="formal | casual | executive | bullet"),
    interview_mode:    bool      = Form(False,     description="Enable interview coaching analysis"),
    interview_deep:    bool      = Form(False,     description="Deep per-question interview analysis"),
    candidate_profile: str       = Form("",        description="Optional candidate resume/profile text"),
    profile_file:  Optional[UploadFile] = File(None, description="Optional résumé/profile file (.pdf .docx .txt) — parsed to text"),
    provider:      str           = Form("anthropic", description="LLM provider type: anthropic | openai | openai_compat"),
    model:         Optional[str] = Form(None,      description="LLM model id (provider default if omitted)"),
    base_url:      Optional[str] = Form(None,      description="Custom base URL for openai_compat providers"),
    llm_api_key:   str           = Form("",        description="User LLM API key (falls back to server env)"),
    stt_engine:    str           = Form("whisper_local", description="STT engine key, e.g. whisper_local | deepgram | assemblyai"),
    stt_api_key:   str           = Form("",        description="API key for cloud STT engines"),
    stt_model:     Optional[str] = Form(None,      description="Model id for cloud STT engines"),
    language_variant: Optional[str] = Form(None,   description="Regional variant / dialect"),
    transcription_only: bool     = Form(False,     description="Skip AI analysis — return raw transcript only"),
    use_gpu:       bool          = Form(True,      description="Use GPU acceleration for Whisper if available"),
    include_summary:          bool = Form(True,    description="Include the summary section"),
    include_key_points:       bool = Form(True,    description="Include key points"),
    include_action_items:     bool = Form(True,    description="Include action items"),
    include_transcript:       bool = Form(True,    description="Include the full transcript"),
    include_speaker_profiles: bool = Form(True,    description="Include speaker profiles"),
    include_speech_analytics: bool = Form(True,    description="Include speech analytics"),
):
    """
    Upload a file and get a **job_id** back immediately.
    Poll `GET /api/jobs/{job_id}` for status and results.

    Works with: `.mp3 .wav .m4a .mp4 .mov .mkv .webm .srt .vtt .pdf .docx .txt`
    """
    job_id = uuid.uuid4().hex[:12]
    url = (source_url or "").strip()

    has_file = file is not None and bool(file.filename)
    if not has_file and not url:
        raise HTTPException(status_code=400, detail="Provide either a file upload or a source_url.")

    if has_file:
        # stream uploaded file to a temp path (chunked — safe for huge files)
        stem   = Path(file.filename or "upload").stem
        suffix = Path(file.filename or "upload").suffix or ".tmp"
        tmp_name = await _save_upload(file, suffix)
        filename = file.filename
        url = ""  # an uploaded file takes precedence over a URL
    else:
        # the worker downloads the URL (keeps the request fast for huge files)
        from urllib.parse import urlparse
        stem = Path(urlparse(url).path).stem or "download"
        tmp_name = None
        filename = url

    # Optional résumé/profile file → text (overrides the pasted profile text).
    if profile_file is not None and profile_file.filename:
        try:
            p_suffix = Path(profile_file.filename).suffix or ".txt"
            p_tmp = await _save_upload(profile_file, p_suffix)
            extracted = extract_profile_text(p_tmp)
            if extracted and extracted.strip():
                candidate_profile = extracted
            try:
                os.unlink(p_tmp)
            except Exception:
                pass
        except Exception:
            pass

    _set_job(job_id, status="queued", filename=filename, progress="Queued")

    background_tasks.add_task(
        _run_transcription,
        job_id, tmp_name, stem,
        whisper_model, language, panel_mode, num_speakers, report_style,
        interview_mode, interview_deep, candidate_profile,
        provider, model, base_url, llm_api_key,
        stt_engine, stt_api_key, stt_model,
        language_variant, transcription_only, use_gpu,
        include_summary, include_key_points, include_action_items,
        include_transcript, include_speaker_profiles, include_speech_analytics,
        url,
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


@app.get("/api/jobs/{job_id}/download/{name}", summary="Download a generated result file")
def download_file(job_id: str, name: str):
    """Download a result file (transcript, report, .srt, .vtt, .docx, annotated video…)."""
    job_dir = OUTPUTS_DIR / job_id
    target = (job_dir / name).resolve()
    # prevent path traversal — must stay inside the job dir
    if job_dir.resolve() not in target.parents or not target.is_file():
        raise HTTPException(status_code=404, detail=f"File '{name}' not found for job '{job_id}'")
    return FileResponse(str(target), filename=name)


# ── history (file-based, no DB — shared with the desktop UI) ───────────────────
_history_lock = threading.Lock()

@app.get("/api/history", summary="Past runs with token spend + cost")
def get_history():
    """Every past run (newest first) with computed cost, plus a session/all-time
    total. Reads the same history.jsonl the desktop UI writes, so it persists
    across restarts and is shared no matter which UI created the run."""
    # load_history already returns newest-first.
    entries = [_entry_public(e) for e in load_history(HISTORY_PATH)]
    total_cost = round(sum(e["cost_usd"] for e in entries), 6)
    total_in  = sum(e["tok_in"]  for e in entries)
    total_out = sum(e["tok_out"] for e in entries)
    return {"entries": entries, "count": len(entries),
            "total_cost_usd": total_cost, "total_tok_in": total_in, "total_tok_out": total_out}

@app.delete("/api/history/{entry_id}", summary="Delete a run (moves it to trash)")
def delete_history(entry_id: str):
    """Move a history entry to trash.jsonl (recoverable), mirroring the desktop UI."""
    with _history_lock:
        entries = load_history(HISTORY_PATH)
        keep, removed = [], None
        for e in entries:
            if e.get("id") == entry_id and removed is None:
                removed = e
            else:
                keep.append(e)
        if removed is None:
            raise HTTPException(status_code=404, detail=f"History entry '{entry_id}' not found")
        # append removed to trash, rewrite history without it
        save_history_entry(removed, TRASH_PATH)
        HISTORY_PATH.write_text(
            "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in keep),
            encoding="utf-8")
    return {"deleted": entry_id, "remaining": len(keep)}


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

    tmp_name = await _save_upload(file, suffix)

    _set_job(job_id, status="processing")

    # run synchronously (blocks until done)
    _run_transcription(
        job_id, tmp_name, stem,
        whisper_model, language, panel_mode, num_speakers, report_style,
    )

    result = _get_job(job_id)
    if result.get("status") == "error":
        raise HTTPException(status_code=500, detail=result.get("error"))
    return result


# ── video analysis ────────────────────────────────────────────────────────────

def _person_to_dict(p) -> dict:
    d = {
        "person_id": p.person_id,
        "role": p.role,
        "confidence": round(p.confidence, 1),
        "composure": round(p.composure, 1),
        "eye_contact": round(p.eye_contact, 1),
        "engagement": round(p.engagement, 1),
        "energy": round(p.energy, 1),
        "receptiveness": round(p.receptiveness, 1),
        "overall": round(p.overall, 1),
        "talk_time_pct": round(p.talk_time_pct, 1),
        "open_body_pct": round(p.open_body_pct, 1),
        "arm_crossed_pct": round(p.arm_crossed_pct, 1),
        "forward_lean_pct": round(p.forward_lean_pct, 1),
        "dominant_emotion": p.dominant_emotion,
        "emotion_distribution": p.emotion_distribution,
    }
    if p.cultural:
        d["cultural"] = {
            "american_score": round(p.cultural.american_score, 1),
            "adaptation_score": round(p.cultural.adaptation_score, 1),
            "american_tips": p.cultural.american_tips,
            "adaptation_tips": p.cultural.adaptation_tips,
        }
    return d


def _run_video_analysis(job_id: str, file_path: str, role_map: dict, sample_fps: float):
    log_lines: list[str] = []

    def on_log(msg):
        log_lines.append(str(msg))
        _set_job(job_id, progress=str(msg), log=list(log_lines))

    try:
        from video_analyzer import VideoAnalyzer
        analyzer = VideoAnalyzer()
        job_dir = OUTPUTS_DIR / job_id
        job_dir.mkdir(parents=True, exist_ok=True)
        _set_job(job_id, status="processing", job_dir=str(job_dir))

        def _pcb(v, info=None):
            on_log(f"Analyzing… {int((v or 0) * 100)}%" + (f"  {info}" if info else ""))

        result = analyzer.analyze_video(
            file_path, role_map, sample_fps=sample_fps,
            progress_cb=_pcb, annotate=True,
        )
        if result.error:
            _set_job(job_id, status="error", error=result.error, log=list(log_lines))
            return

        annotated_name = None
        if result.annotated_video_path and Path(result.annotated_video_path).is_file():
            dest = job_dir / "annotated_video.mp4"
            try:
                Path(result.annotated_video_path).replace(dest)
                annotated_name = dest.name
            except Exception:
                annotated_name = None

        _set_job(job_id,
            status="done",
            overall_score=round(result.overall_score, 1),
            rapport_score=round(result.rapport_score, 1),
            talk_balance_score=round(result.talk_balance_score, 1),
            candidate_talk_pct=round(result.candidate_talk_pct, 1),
            duration_seconds=round(result.duration_seconds, 1),
            person_count=result.person_count,
            observations=result.observations,
            persons=[_person_to_dict(p) for p in result.persons.values()],
            annotated_video=annotated_name,
            log=list(log_lines),
        )
    except Exception as e:
        import traceback
        _set_job(job_id, status="error", error=f"{e}\n{traceback.format_exc()}", log=list(log_lines))
    finally:
        try:
            os.unlink(file_path)
        except Exception:
            pass


@app.post("/api/transcribe-clip", summary="Quickly transcribe a short audio/video clip (live transcription)")
async def transcribe_clip_endpoint(
    file: UploadFile = File(..., description="Short audio/video clip (a few seconds)"),
    whisper_model: str = Form("tiny", description="Local Whisper model size for fast clip transcription"),
    language: str = Form("", description="Language code, or empty for auto-detect"),
):
    """
    Transcribe a short clip synchronously and return the text immediately —
    used by the Live Interview tab to show a rolling live transcript.
    Always uses local Whisper (fast `tiny`/`base` models) so it works offline
    with no API key and returns within a couple of seconds.
    """
    suffix = Path(file.filename or "clip").suffix or ".webm"
    tmp_name = await _save_upload(file, suffix)
    try:
        text, lang, _segs, secs = stt_transcribe(
            tmp_name, engine="whisper_local",
            whisper_model=whisper_model or "tiny",
            language=language or None,
        )
        return {"text": (text or "").strip(), "language": lang, "seconds": round(secs, 2)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        try:
            os.unlink(tmp_name)
        except Exception:
            pass


@app.post("/api/analyze-video", summary="Analyze interview video delivery (body language, emotion, eye contact)")
async def analyze_video_endpoint(
    background_tasks: BackgroundTasks,
    file:         UploadFile = File(..., description="Interview video (.mp4 .mov .webm .mkv)"),
    person_count: int        = Form(2,   description="Number of people on screen (1-4)"),
    roles:        str        = Form("",  description="Comma-separated roles, e.g. 'Candidate,Interviewer'"),
    sample_fps:   float      = Form(0.5, description="Frames per second to sample (lower = faster)"),
):
    """
    Upload an interview video to get delivery score cards (confidence, composure,
    eye contact, engagement, body language, emotion) plus an annotated video.
    Returns a **job_id**; poll `GET /api/jobs/{job_id}` for results.
    """
    job_id = uuid.uuid4().hex[:12]
    suffix = Path(file.filename or "upload").suffix or ".mp4"
    tmp_name = await _save_upload(file, suffix)

    role_list = [r.strip() for r in roles.split(",") if r.strip()]
    n = max(1, min(int(person_count or 1), 4))
    default_roles = ["Candidate", "Interviewer", "Panelist", "Panelist"]
    role_map = {i: (role_list[i] if i < len(role_list) else default_roles[i]) for i in range(n)}

    _set_job(job_id, status="queued", filename=file.filename, progress="Queued")
    background_tasks.add_task(_run_video_analysis, job_id, tmp_name, role_map, float(sample_fps))
    return {"job_id": job_id, "status": "queued", "poll_url": f"/api/jobs/{job_id}"}


# ── cancel a running job ───────────────────────────────────────────────────────
@app.post("/api/jobs/{job_id}/cancel", summary="Cancel a running job")
def cancel_job(job_id: str):
    """Signal a running transcription to stop. It aborts before the (expensive)
    LLM analysis stage; the job then ends with status `error` (Cancelled)."""
    job = _get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found")
    ev = _cancel_events.get(job_id)
    if ev is None:
        raise HTTPException(status_code=409, detail="Job is not running — nothing to cancel.")
    ev.set()
    _set_job(job_id, progress="Cancelling…")
    return {"job_id": job_id, "cancelling": True}


# ── trash (restore / empty) — history delete already moves entries here ─────────
@app.get("/api/trash", summary="List trashed runs")
def get_trash():
    """Runs that were deleted from History (recoverable). Newest first."""
    entries = [_entry_public(e) for e in load_history(TRASH_PATH)]
    return {"entries": entries, "count": len(entries)}


@app.post("/api/trash/{entry_id}/restore", summary="Restore a trashed run to History")
def restore_trash(entry_id: str):
    with _history_lock:
        trashed = load_history(TRASH_PATH)
        restore, keep = None, []
        for e in trashed:
            if e.get("id") == entry_id and restore is None:
                restore = e
            else:
                keep.append(e)
        if restore is None:
            raise HTTPException(status_code=404, detail=f"Trash entry '{entry_id}' not found")
        save_history_entry(restore, HISTORY_PATH)
        TRASH_PATH.write_text(
            "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in keep),
            encoding="utf-8")
    return {"restored": entry_id, "remaining": len(keep)}


@app.post("/api/trash/empty", summary="Permanently empty the trash")
def empty_trash():
    with _history_lock:
        count = len(load_history(TRASH_PATH))
        TRASH_PATH.write_text("", encoding="utf-8")
    return {"emptied": count}


# ── regenerate PDF & DOCX (optionally translated) ──────────────────────────────
def _combined_report_text(r) -> str:
    parts = []
    if getattr(r, "summary", ""):
        parts.append("# Summary\n" + r.summary)
    if getattr(r, "key_points", None):
        parts.append("# Key Points\n" + "\n".join(f"- {k}" for k in r.key_points))
    if getattr(r, "action_items", None):
        parts.append("# Action Items\n" + "\n".join(f"- {a}" for a in r.action_items))
    if getattr(r, "speaker_dialogue", ""):
        parts.append("# Dialogue\n" + r.speaker_dialogue)
    elif getattr(r, "clean_transcript", ""):
        parts.append("# Transcript\n" + r.clean_transcript)
    return "\n\n".join(parts)


_DEFAULT_MODEL = {
    "anthropic": "claude-haiku-4-5-20251001",
    "openai": "gpt-4.1-mini",
}

@app.post("/api/jobs/{job_id}/regenerate", summary="Regenerate PDF & DOCX (optionally in another language)")
def regenerate_reports(
    job_id: str,
    target_lang: str       = Form("",          description="Output language e.g. 'Spanish'. Empty / 'Same as source' = no translation"),
    provider:    str       = Form("anthropic", description="LLM provider for translation"),
    model:       Optional[str] = Form(None,    description="LLM model for translation"),
    base_url:    Optional[str] = Form(None),
    llm_api_key: str       = Form(""),
):
    job = _get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found")
    if job.get("status") != "done":
        raise HTTPException(status_code=400, detail="Run must finish before regenerating reports.")

    stem    = job.get("stem") or "report"
    job_dir = job.get("job_dir") or str(OUTPUTS_DIR / job_id)

    result = TranscriptResult(
        summary          = job.get("summary", "") or "",
        key_points       = job.get("key_points", []) or [],
        action_items     = job.get("action_items", []) or [],
        speaker_dialogue = job.get("speaker_dialogue", "") or "",
        clean_transcript = job.get("transcript", "") or "",
        detected_language= job.get("detected_language", "") or "",
    )

    suffix = ""
    tgt = (target_lang or "").strip()
    if tgt and tgt.lower() not in ("same as source", "source"):
        api_key = (llm_api_key or "").strip() or (
            os.environ.get("ANTHROPIC_API_KEY", "") if provider == "anthropic" else "")
        use_model = model or _DEFAULT_MODEL.get(provider, _DEFAULT_MODEL["anthropic"])
        combined = _combined_report_text(result)
        try:
            client = LLMClient(provider=provider or "anthropic", api_key=api_key,
                               model=use_model, base_url=base_url or None)
            translated = client.chat(
                system=(f"You are a professional translator. Translate the user's report into {tgt}. "
                        "Preserve the markdown headings and overall structure. Output only the translation."),
                user=combined, max_tokens=8000)
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"Translation failed: {e}")
        result = TranscriptResult(
            summary="", key_points=[], action_items=[], speaker_dialogue="",
            clean_transcript=translated, detected_language=tgt,
        )
        suffix = "_" + tgt.replace(" ", "_")

    title    = f"{stem}  [{result.detected_language or tgt}]" if (result.detected_language or tgt) else stem
    pdf_path = _write_pdf(result, stem, job_dir, suffix=suffix)
    docx_path = None
    try:
        dp = str(Path(job_dir) / f"{stem}_report{suffix}.docx")
        if generate_docx(result, title, dp):
            docx_path = dp
    except Exception:
        docx_path = None

    downloads = _scan_downloads(job_dir)
    _set_job(job_id, downloads=downloads)
    return {
        "job_id": job_id, "target_lang": tgt or "source",
        "pdf":  Path(pdf_path).name if pdf_path else None,
        "docx": Path(docx_path).name if docx_path else None,
        "downloads": downloads,
    }


# ── check for app updates (GitHub releases) ────────────────────────────────────
@app.get("/api/update-check", summary="Check GitHub for a newer release")
def update_check():
    """Compares the running APP_VERSION against the latest GitHub release."""
    import urllib.request as _ur
    info = {"current": APP_VERSION, "latest": None, "update_available": False,
            "url": f"https://github.com/{_GH_RELEASES_REPO}/releases/latest", "notes": ""}
    try:
        req = _ur.Request(
            f"https://api.github.com/repos/{_GH_RELEASES_REPO}/releases/latest",
            headers={"User-Agent": f"TranscriptAgent/{APP_VERSION}",
                     "Accept": "application/vnd.github.v3+json"},
        )
        with _ur.urlopen(req, timeout=8) as r:
            data = json.loads(r.read())
        latest = (data.get("tag_name", "") or "").lstrip("v")
        if latest:
            info["latest"] = latest
            try:
                from packaging.version import Version
                info["update_available"] = Version(latest) > Version(APP_VERSION)
            except Exception:
                info["update_available"] = latest > APP_VERSION
            info["url"]   = data.get("html_url", info["url"])
            info["notes"] = (data.get("body") or "")[:280]
    except Exception as e:
        info["error"] = str(e)
    return info


# ── static React UI (frontend/dist) ───────────────────────────────────────────
# Serve the built React + Bootstrap UI at "/" when available. Mounted LAST so
# the API routes (/api/*, /health, /docs) always take precedence.
_FRONTEND_DIST = Path(__file__).parent / "frontend" / "dist"
if _FRONTEND_DIST.is_dir():
    app.mount("/", StaticFiles(directory=str(_FRONTEND_DIST), html=True), name="ui")
else:
    @app.get("/", include_in_schema=False)
    def _root():
        return HTMLResponse(
            "<h2>Transcript Agent API</h2>"
            "<p>The React UI is not built. Run <code>npm run build</code> in "
            "<code>frontend/</code>, or open <a href='/docs'>/docs</a> for the API.</p>"
        )


# ── run ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    port = int(os.environ.get("API_PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")
