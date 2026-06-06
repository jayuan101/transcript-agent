#!/usr/bin/env python3
"""Transcript Agent — Gradio UI with drag-and-drop | v2.1"""

import os
import sys

# Allow TensorFlow (DeepFace) and PyTorch to use GPU when available.
# TF picks up GPU automatically; setting memory growth avoids OOM crashes.
os.environ.setdefault("TF_FORCE_GPU_ALLOW_GROWTH", "true")
# XLA JIT compilation — speeds up TF/DeepFace inference on GPU
os.environ.setdefault("TF_XLA_FLAGS", "--tf_xla_auto_jit=2")
# Disable TF info/warning spam
os.environ.setdefault("TF_CPP_MIN_LOG_LEVEL", "2")

# Fix for PyInstaller --noconsole: sys.stdout/stderr are None when there is no
# console window. uvicorn's DefaultFormatter calls stream.isatty() → crash.
if sys.stdout is None or sys.stderr is None:
    _log = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "app.log"),
                "w", encoding="utf-8", errors="replace", buffering=1)
    if sys.stdout is None:
        sys.stdout = _log
    if sys.stderr is None:
        sys.stderr = _log

# Windows: switch to SelectorEventLoop — ProactorEventLoop (default on Win32)
# has a hard 30-second socket-write timeout that kills long streaming sessions.
import asyncio
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

import gradio as gr
import uuid
import threading
import queue as Q
import time
import re
import urllib.parse
import html
import mimetypes
import tempfile
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent / ".env")
except ImportError:
    pass

try:
    from video_analyzer import VideoAnalyzer as _VideoAnalyzer
    _video_analyzer = _VideoAnalyzer()
    _HAS_VIDEO_ANALYZER = True
except Exception:
    _HAS_VIDEO_ANALYZER = False



def _resolve_nextcloud_token_url(url: str):
    """
    Decode a Nextcloud share token URL and return a direct S3 object URL.
    Supports nextim.itcapp.ai and path-style s3.amazonaws.com share URLs.
    """
    import base64, json
    try:
        parsed = urllib.parse.urlparse(url)
        if "/api/nextcloud/share" not in parsed.path:
            return None
        qs = urllib.parse.parse_qs(parsed.query)
        token_b64 = qs.get("token", [""])[0]
        if not token_b64:
            return None
        pad = (4 - len(token_b64) % 4) % 4
        token_data = json.loads(base64.b64decode(token_b64 + "=" * pad).decode())
        file_key = token_data.get("key", "")
        if not file_key:
            return None
        host = parsed.hostname or ""
        if host == "s3.amazonaws.com":
            # path-style: /s3.amazonaws.com/{bucket}/api/nextcloud/share
            bucket = parsed.path.lstrip("/").split("/")[0]
        elif "itcapp.ai" in host or "nextim" in host:
            # ITC App Nextcloud server — recordings live in this S3 bucket
            bucket = "nextcloud-talk-recordings-itc-eit"
        else:
            return None
        if not bucket:
            return None
        encoded_key = urllib.parse.quote(file_key, safe="")
        return f"https://{bucket}.s3.amazonaws.com/{encoded_key}"
    except Exception:
        return None


def _download_url(url: str, dest_dir: Path, on_progress=None) -> Path:
    """Download a URL to dest_dir; uses Content-Disposition to pick filename.

    Handles S3 application-level redirects (PermanentRedirect XML response with
    HTTP 200/301) by retrying against the correct endpoint.
    on_progress(bytes_received, total_bytes_or_0): called periodically during download.
    """
    import requests

    def _do_get(u: str, timeout=None):
        return requests.get(
            u,
            stream=True,
            timeout=timeout,
            headers={"User-Agent": "TranscriptAgent/1.0"},
            allow_redirects=True,
        )

    resp = _do_get(url)

    if resp.status_code == 401:
        raise ValueError(
            "The URL requires a login (401 Unauthorized). "
            "Download the file manually and paste its local path instead."
        )

    if resp.status_code == 403:
        resp.close()
        resp = None

        # Strategy 1: decode Nextcloud share token → direct S3 object URL
        direct_url = _resolve_nextcloud_token_url(url)
        if direct_url:
            try:
                _r = _do_get(direct_url)
                if _r.ok:
                    resp = _r
                else:
                    _r.close()
            except Exception:
                pass

        if resp is None:
            raise ValueError(
                "403 Forbidden — the server denied access to this URL.\n"
                "The share link may require a company VPN or internal network access.\n"
                "Download the file manually and paste its local path instead."
            )

    resp.raise_for_status()

    # Peek at the first chunk so we can detect S3 XML-style errors that arrive
    # as a normal 200 response.
    chunks = resp.iter_content(chunk_size=65536)
    try:
        first = next(chunks)
    except StopIteration:
        first = b""

    ct = (resp.headers.get("Content-Type", "") or "").split(";")[0].strip().lower()
    head = first[:512].lstrip()

    looks_xml = (
        ct in ("application/xml", "text/xml")
        or head.startswith(b"<?xml")
        or head.startswith(b"<Error")
    )

    if looks_xml:
        body = head.decode("utf-8", errors="replace")
        # Detect S3 PermanentRedirect and retry against the suggested endpoint.
        m_code   = re.search(r"<Code>([^<]+)</Code>", body)
        m_ep     = re.search(r"<Endpoint>([^<]+)</Endpoint>", body)
        m_bucket = re.search(r"<Bucket>([^<]+)</Bucket>", body)
        if m_code and m_code.group(1) == "PermanentRedirect" and m_ep:
            # Use resp.url (the actual S3 URL after following HTTP redirects) not
            # the original URL, so we get the correct object path.
            parsed = urllib.parse.urlparse(resp.url)
            new_host = m_ep.group(1).strip()
            bucket = m_bucket.group(1).strip() if m_bucket else ""
            _qs = parsed.query
            _is_presigned = any(k in _qs for k in ("X-Amz-Algorithm", "X-Amz-Credential", "AWSAccessKeyId"))
            if bucket and not new_host.startswith(bucket + "."):
                new_path = "/" + bucket.strip("/") + "/" + parsed.path.lstrip("/")
            else:
                new_path = parsed.path
            # Try with presigned params preserved first — the credential region
            # often already matches the redirect target (global → regional endpoint).
            new_url = urllib.parse.urlunparse(
                (parsed.scheme or "https", new_host, new_path,
                 parsed.params, _qs, parsed.fragment)
            )
            resp.close()
            resp = _do_get(new_url)
            if resp.status_code in (400, 403) and _is_presigned:
                # Params didn't work at the new endpoint — truly wrong region.
                # Try without presigned params as a last resort (public bucket).
                new_url_no_auth = urllib.parse.urlunparse(
                    (parsed.scheme or "https", new_host, new_path,
                     parsed.params, "", parsed.fragment)
                )
                resp.close()
                resp = _do_get(new_url_no_auth)
            if resp.status_code in (400, 403):
                _orig_host = urllib.parse.urlparse(url).hostname or ""
                _hint = (
                    f"Open {_orig_host} in your browser, download the recording, "
                    "then drag the file into the app or paste its local path."
                ) if _orig_host else "Download the file manually and paste its local path."
                raise ValueError(
                    f"The recording server ({_orig_host}) generated a presigned URL "
                    "for the wrong AWS region — this is a server configuration issue "
                    "that cannot be worked around automatically.\n\n" + _hint
                )
            resp.raise_for_status()
            chunks = resp.iter_content(chunk_size=65536)
            try:
                first = next(chunks)
            except StopIteration:
                first = b""
            ct = (resp.headers.get("Content-Type", "") or "").split(";")[0].strip().lower()
            head = first[:512].lstrip()
            if head.startswith(b"<?xml") or head.startswith(b"<Error"):
                snippet = head.decode("utf-8", errors="replace")[:400]
                raise ValueError(
                    "Download failed: server returned an XML error after "
                    "following S3 redirect.\n" + snippet
                )
        else:
            snippet = body[:400]
            msg = "Download failed: server returned an XML error instead of a media file."
            if m_code:
                msg += f"\nS3 error: {m_code.group(1)}"
            raise ValueError(msg + "\n" + snippet)

    # Prefer Content-Disposition filename; fall back to final (post-redirect) URL path
    cd = resp.headers.get("Content-Disposition", "")
    filename = None
    if cd:
        m = re.search(r"filename\*=(?:UTF-8'')?([^\s;]+)", cd, re.I)
        if m:
            filename = urllib.parse.unquote(m.group(1).strip('"'))
        else:
            m = re.search(r'filename=["\']?([^"\';]+)', cd, re.I)
            if m:
                filename = m.group(1).strip()
    if not filename:
        url_path = urllib.parse.urlparse(resp.url).path   # use final URL after redirects
        filename = Path(urllib.parse.unquote(url_path)).name or "download"
    if not Path(filename).suffix:
        filename += mimetypes.guess_extension(ct) or ""

    dest = dest_dir / filename
    total = 0
    last_progress = time.time()
    last_report   = time.time()
    total_size    = int(resp.headers.get("Content-Length", 0) or 0)
    with open(dest, "wb") as f:
        if first:
            f.write(first)
            total += len(first)
            last_progress = time.time()
        for chunk in chunks:
            if not chunk:
                if time.time() - last_progress > 60:
                    raise ValueError(
                        "Download stalled — no data received for 60 seconds. "
                        "The server dropped the connection. Download the file via "
                        "your browser and drag it into the app instead."
                    )
                continue
            f.write(chunk)
            total += len(chunk)
            last_progress = time.time()
            if on_progress and time.time() - last_report >= 2:
                on_progress(total, total_size)
                last_report = time.time()

    # Sanity check: a real audio/video file is never just a few hundred bytes.
    if total < 1024:
        try:
            preview = dest.read_bytes()[:400].decode("utf-8", errors="replace")
        except Exception:
            preview = ""
        try:
            dest.unlink()
        except Exception:
            pass
        raise ValueError(
            f"Download failed: only {total} bytes received — the URL did not "
            f"return a valid media file.\n{preview}"
        )

    return dest

# ── Sleep prevention — Windows + Mac ─────────────────────────────────────────
# Always active while the app is running; screen may still dim/turn off.
_ES_CONTINUOUS      = 0x80000000
_ES_SYSTEM_REQUIRED = 0x00000001
_sleep_active       = False
_sleep_thread       = None
_caffeinate_proc    = None   # Mac: caffeinate subprocess handle

def _set_lid_action(action: int):
    """0 = lid close keeps running, 1 = lid close sleeps (Windows only)."""
    if sys.platform != "win32":
        return
    import subprocess
    for idx in ("setacvalueindex", "setdcvalueindex"):
        subprocess.run(
            ["powercfg", f"/{idx}", "SCHEME_CURRENT", "SUB_BUTTONS", "LIDACTION", str(action)],
            capture_output=True, check=False,
        )
    subprocess.run(["powercfg", "/setactive", "SCHEME_CURRENT"], capture_output=True, check=False)

def _prevent_sleep():
    """Block idle/lid-close sleep on Windows and Mac. Safe to call multiple times."""
    global _sleep_active, _sleep_thread, _caffeinate_proc
    _sleep_active = True

    if sys.platform == "win32":
        import ctypes
        ctypes.windll.kernel32.SetThreadExecutionState(_ES_CONTINUOUS | _ES_SYSTEM_REQUIRED)
        _set_lid_action(0)
        if _sleep_thread is None or not _sleep_thread.is_alive():
            def _refresh():
                import ctypes as _ct
                while _sleep_active:
                    _ct.windll.kernel32.SetThreadExecutionState(_ES_CONTINUOUS | _ES_SYSTEM_REQUIRED)
                    time.sleep(60)
            _sleep_thread = threading.Thread(target=_refresh, daemon=True)
            _sleep_thread.start()

    elif sys.platform == "darwin":
        # caffeinate -i (idle sleep) -m (disk sleep) -s (system sleep on AC)
        if _caffeinate_proc is None or _caffeinate_proc.poll() is not None:
            try:
                import subprocess
                _caffeinate_proc = subprocess.Popen(
                    ["caffeinate", "-i", "-m", "-s"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
            except Exception:
                pass

def _allow_sleep():
    """Restore normal sleep behaviour. Called on clean app exit."""
    global _sleep_active, _caffeinate_proc
    _sleep_active = False
    if sys.platform == "win32":
        import ctypes
        ctypes.windll.kernel32.SetThreadExecutionState(_ES_CONTINUOUS)
        _set_lid_action(1)
    elif sys.platform == "darwin":
        if _caffeinate_proc and _caffeinate_proc.poll() is None:
            _caffeinate_proc.terminate()
            _caffeinate_proc = None

from transcript_agent import (
    run, ReportConfig, build_combined_report, LLMClient,
    AUDIO_EXTS, VIDEO_EXTS, IMAGE_EXTS, STT_ENGINES,
    load_history, save_history_entry,
    run_interview_analysis, extract_profile_text,
)

# ── AI provider configuration ─────────────────────────────────────────────────
_PROVIDERS = {
    "Claude (Anthropic)": {
        "type": "anthropic",
        "placeholder": "sk-ant-api03-…",
        "info": "console.anthropic.com → API keys · 🔒 Saved in your browser only — never on this server",
        "models": [
            "claude-opus-4-8",
            "claude-sonnet-4-6",
            "claude-haiku-4-5-20251001",
            "claude-3-5-sonnet-20241022",
            "claude-3-5-haiku-20241022",
        ],
        "base_url": None,
    },
    "OpenAI": {
        "type": "openai",
        "placeholder": "sk-…",
        "info": "platform.openai.com → API keys · 🔒 Saved in your browser only — never on this server",
        "models": [
            "gpt-4.1",
            "gpt-4.1-mini",
            "gpt-4.1-nano",
            "gpt-4o",
            "gpt-4o-mini",
            "o3",
            "o3-mini",
            "o4-mini",
        ],
        "base_url": None,
    },
    "Google Gemini": {
        "type": "openai_compat",
        "placeholder": "AIzaSy…",
        "info": "aistudio.google.com → Get API key · 🔒 Saved in your browser only — never on this server",
        "models": [
            "gemini-2.5-pro",
            "gemini-2.5-flash",
            "gemini-2.0-flash",
            "gemini-2.0-flash-lite",
        ],
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai/",
    },
    "Groq": {
        "type": "openai_compat",
        "placeholder": "gsk_…",
        "info": "console.groq.com → API keys · 🔒 Saved in your browser only — never on this server",
        "models": [
            "llama-3.3-70b-versatile",
            "llama-3.1-8b-instant",
            "deepseek-r1-distill-llama-70b",
            "qwen-qwq-32b",
            "gemma2-9b-it",
        ],
        "base_url": "https://api.groq.com/openai/v1",
    },
    "Mistral": {
        "type": "openai_compat",
        "placeholder": "…",
        "info": "console.mistral.ai → API keys · 🔒 Saved in your browser only — never on this server",
        "models": [
            "mistral-large-latest",
            "mistral-small-latest",
            "mistral-nemo-latest",
            "codestral-latest",
        ],
        "base_url": "https://api.mistral.ai/v1",
    },
    "Together AI": {
        "type": "openai_compat",
        "placeholder": "…",
        "info": "api.together.ai → Settings → API keys · 🔒 Saved in your browser only — never on this server",
        "models": [
            "meta-llama/Meta-Llama-3.3-70B-Instruct-Turbo",
            "meta-llama/Meta-Llama-3.1-70B-Instruct-Turbo",
            "Qwen/Qwen2.5-72B-Instruct-Turbo",
            "Qwen/QwQ-32B",
            "google/gemma-3-27b-it",
        ],
        "base_url": "https://api.together.xyz/v1",
    },
    "Perplexity": {
        "type": "openai_compat",
        "placeholder": "pplx-…",
        "info": "perplexity.ai → Settings → API · 🔒 Saved in your browser only — never on this server",
        "models": [
            "sonar-pro",
            "sonar",
            "sonar-reasoning-pro",
            "sonar-reasoning",
            "r1-1776",
        ],
        "base_url": "https://api.perplexity.ai",
    },
    "Ollama (Local)": {
        "type": "openai_compat",
        "placeholder": "none required",
        "info": "ollama.ai — run models locally, no API key needed",
        "models": [
            # ── DEFAULT (best balance) ────────────────────────────────
            "gemma3:27b",         # Google Gemma 3 27B — best quality/size ratio ★ default
            # ── Best quality (48 GB+ RAM) ─────────────────────────────
            "llama3.3",           # Meta Llama 3.3 70B — best instruction-following
            "qwen2.5:72b",        # Alibaba — exceptional analysis + long context
            "deepseek-r1:70b",    # Strong reasoning + analysis
            # ── Best balance (16–24 GB RAM) ───────────────────────────
            "qwen2.5:32b",        # Alibaba 32B — great for structured output
            "deepseek-r1:32b",    # Reasoning at 32B
            # ── Fast + capable (8–16 GB RAM) ─────────────────────────
            "phi4",               # Microsoft Phi-4 14B — punches above its weight
            "gemma3:12b",         # Google Gemma 3 12B — solid transcript work
            "qwen2.5:14b",        # Alibaba 14B — fast, good JSON output
            "llama3.2",           # Meta 3B/11B — fastest option
            # ── Alternatives ─────────────────────────────────────────
            "mistral-small3.1",   # Mistral 22B — good multilingual
            "mistral",            # Mistral 7B — lightweight baseline
        ],
        # In Docker (GRADIO_SERVER_NAME=0.0.0.0) localhost refers to the
        # container — use host.docker.internal to reach Ollama on the host machine.
        "base_url": (
            "http://host.docker.internal:11434/v1"
            if os.environ.get("GRADIO_SERVER_NAME") == "0.0.0.0"
            else "http://localhost:11434/v1"
        ),
    },
}

def _user_data_dir() -> Path:
    import os, sys
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA") or Path.home())
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    return base / "TranscriptAgent"

OUT_DIR      = _user_data_dir() / "outputs"
OUT_DIR.mkdir(parents=True, exist_ok=True)
HISTORY_PATH = OUT_DIR / "history.jsonl"

# Migrate history from old bundle-relative path if present
_old_history = Path(__file__).parent / "outputs" / "history.jsonl"
if _old_history.exists() and not HISTORY_PATH.exists():
    import shutil
    HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(_old_history, HISTORY_PATH)

_STT_CACHE_DIR = OUT_DIR / ".stt_cache"

def _stt_cache_key(file_path: str) -> str:
    import hashlib
    try:
        with open(file_path, "rb") as f:
            chunk = f.read(1_048_576)  # first 1 MB is enough to fingerprint the file
        return hashlib.sha256(chunk).hexdigest()[:20]
    except Exception:
        return ""

def _load_stt_cache(file_path: str):
    key = _stt_cache_key(file_path)
    if not key:
        return None
    cache_file = _STT_CACHE_DIR / f"{key}.json"
    if not cache_file.exists():
        return None
    try:
        import json as _j
        data = _j.loads(cache_file.read_text(encoding="utf-8"))
        return data["raw_text"], data["lang"], data["segments"]
    except Exception:
        return None

def _save_stt_cache(file_path: str, raw_text: str, lang: str, segments: list):
    key = _stt_cache_key(file_path)
    if not key:
        return
    _STT_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_file = _STT_CACHE_DIR / f"{key}.json"
    try:
        import json as _j
        cache_file.write_text(_j.dumps({"raw_text": raw_text, "lang": lang, "segments": segments}), encoding="utf-8")
    except Exception:
        pass



SUPPORTED = list(AUDIO_EXTS | VIDEO_EXTS | {".srt", ".vtt", ".txt", ".md", ".docx", ".pdf"})

FORMATS_MD = """
**Accepted formats**
🎵 `.mp3` `.wav` `.m4a` `.flac` `.ogg` `.aac` `.opus` `.wma` `.amr` `.aiff` `.mp2` `.3gp` `.caf` `.ac3` `.ape` + more
🎬 `.mp4` `.mov` `.avi` `.mkv` `.webm` `.flv` `.wmv` `.ts` `.mpg` `.vob` `.3gp` `.divx` + more
📝 `.srt` `.vtt`   📄 `.pdf` `.docx` `.txt` `.md`
"""

CSS = """
/* ── Hide Gradio chrome ── */
footer, .gradio-footer, .built-with, [data-testid="footer"],
a[href*="gradio.app"], a[href*="huggingface.co/spaces"]:not([id]) {
  display: none !important;
}

/* ── Design tokens ── */
:root {
  --ta-bg:            #f0f4fb;
  --ta-surface:       #ffffff;
  --ta-border:        #dde3ef;
  --ta-text:          #0d1b2e;
  --ta-sub:           #5a6a83;
  --ta-accent:        #2563eb;
  --ta-accent-h:      #1d4ed8;
  --ta-accent-lt:     #dbeafe;
  --ta-green:         #16a34a;
  --ta-green-lt:      #dcfce7;
  --ta-amber:         #d97706;
  --ta-amber-lt:      #fef3c7;
  --ta-radius:        12px;
  /* legacy aliases used throughout */
  --ta-card-bg:       #ffffff;
  --ta-card-border:   #dde3ef;
  --ta-card-text:     #0d1b2e;
  --ta-card-sub:      #5a6a83;
  --ta-card-val:      #0d1b2e;
  --ta-step-done-bg:  #dcfce7;
  --ta-step-done-bdr: #22c55e;
  --ta-step-done-clr: #166534;
  --ta-step-act-bg:   #dbeafe;
  --ta-step-act-bdr:  #2563eb;
  --ta-step-act-clr:  #1d4ed8;
  --ta-step-wait-bg:  #f1f5f9;
  --ta-step-wait-bdr: #dde3ef;
  --ta-step-wait-clr: #94a3b8;
  --ta-conn-line-done:#22c55e;
  --ta-conn-line-wait:#dde3ef;
  --ta-stat-bg:       rgba(255,255,255,0.8);
  --ta-stat-label:    #1e40af;
  --ta-stat-val:      #1d4ed8;
  --ta-log-bg:        #f8fafc;
  --ta-log-border:    #cbd5e1;
  --ta-log-text:      #475569;
  --ta-log-ts:        #94a3b8;
  --ta-err-bg:        linear-gradient(135deg,#fef2f2,#fee2e2);
  --ta-err-border:    #ef4444;
  --ta-err-title:     #991b1b;
  --ta-err-text:      #b91c1c;
}
html.dark {
  --ta-bg:            #080f1c;
  --ta-surface:       #111827;
  --ta-border:        #1e2d45;
  --ta-text:          #e2e8f0;
  --ta-sub:           #7a8fa6;
  --ta-accent:        #3b82f6;
  --ta-accent-h:      #60a5fa;
  --ta-accent-lt:     #1e3a5f;
  --ta-green:         #4ade80;
  --ta-green-lt:      #14532d;
  --ta-amber:         #fbbf24;
  --ta-amber-lt:      #78350f;
  --ta-card-bg:       #111827;
  --ta-card-border:   #1e2d45;
  --ta-card-text:     #e2e8f0;
  --ta-card-sub:      #7a8fa6;
  --ta-card-val:      #f1f5f9;
  --ta-step-done-bg:  #14532d;
  --ta-step-done-bdr: #4ade80;
  --ta-step-done-clr: #4ade80;
  --ta-step-act-bg:   #1e3a5f;
  --ta-step-act-bdr:  #60a5fa;
  --ta-step-act-clr:  #93c5fd;
  --ta-step-wait-bg:  #080f1c;
  --ta-step-wait-bdr: #1e2d45;
  --ta-step-wait-clr: #475569;
  --ta-conn-line-done:#4ade80;
  --ta-conn-line-wait:#1e2d45;
  --ta-stat-bg:       rgba(8,15,28,0.7);
  --ta-stat-label:    #93c5fd;
  --ta-stat-val:      #e2e8f0;
  --ta-log-bg:        #0a0f1e;
  --ta-log-border:    #1e3a5f;
  --ta-log-text:      #94a3b8;
  --ta-log-ts:        #64748b;
  --ta-err-bg:        #1a0505;
  --ta-err-border:    #7f1d1d;
  --ta-err-title:     #fca5a5;
  --ta-err-text:      #fecaca;
}

/* ── Error card ── */
.ta-err-card { background:linear-gradient(135deg,#fef2f2,#fee2e2);border:2px solid #ef4444;border-radius:12px;padding:18px 22px;display:flex;align-items:flex-start;gap:14px;font-family:sans-serif; }
html.dark .ta-err-card { background:#1a0505 !important;border-color:#7f1d1d !important; }
.ta-err-title { color:#991b1b;font-weight:700;font-size:1em; }
html.dark .ta-err-title { color:#fca5a5 !important; }
.ta-err-text { color:#b91c1c;font-size:0.88em;margin-top:5px; }
html.dark .ta-err-text { color:#fecaca !important; }

/* ── Deflection badge ── */
.ta-defl-partial { background:#fffbeb;border:1px solid #fde68a;border-radius:8px;padding:7px 12px;margin-bottom:10px;display:flex;align-items:flex-start;gap:8px; }
html.dark .ta-defl-partial { background:#451a03 !important;border-color:#92400e !important; }
.ta-defl-full { background:#fef2f2;border:1px solid #fecaca;border-radius:8px;padding:7px 12px;margin-bottom:10px;display:flex;align-items:flex-start;gap:8px; }
html.dark .ta-defl-full { background:#450a0a !important;border-color:#991b1b !important; }
.ta-defl-label-partial { font-size:0.78em;font-weight:700;color:#f59e0b;white-space:nowrap; }
html.dark .ta-defl-label-partial { color:#fbbf24 !important; }
.ta-defl-label-full { font-size:0.78em;font-weight:700;color:#ef4444;white-space:nowrap; }
html.dark .ta-defl-label-full { color:#f87171 !important; }
.ta-defl-note { font-size:0.78em;color:#374151; }
html.dark .ta-defl-note { color:#cbd5e1 !important; }

/* ── STT API key banner ── */
.ta-stt-banner { background:linear-gradient(135deg,#fffbeb,#fef3c7);border:1.5px solid #f59e0b;border-radius:8px;padding:8px 14px;display:flex;align-items:center;gap:8px;font-family:sans-serif; }
html.dark .ta-stt-banner { background:linear-gradient(135deg,#451a03,#78350f) !important;border-color:#d97706 !important; }
.ta-stt-banner-title { font-weight:700;color:#92400e;font-size:0.88em; }
html.dark .ta-stt-banner-title { color:#fde68a !important; }
.ta-stt-banner-body { color:#78350f;font-size:0.82em;margin-left:6px; }
html.dark .ta-stt-banner-body { color:#fcd34d !important; }

/* ── Done panel ── */
.ta-done-panel { background:linear-gradient(135deg,#d1fae5,#a7f3d0);border:2px solid #10b981;border-radius:16px;padding:28px 32px;text-align:center;font-family:sans-serif; }
html.dark .ta-done-panel { background:linear-gradient(135deg,#064e3b,#065f46) !important;border-color:#10b981 !important; }
.ta-done-title { color:#065f46;font-size:1.5em;font-weight:800;margin-top:8px;letter-spacing:-0.02em; }
html.dark .ta-done-title { color:#6ee7b7 !important; }
.ta-done-stat-label { font-size:0.72em;font-weight:700;text-transform:uppercase;letter-spacing:0.08em;color:#047857; }
html.dark .ta-done-stat-label { color:#34d399 !important; }
.ta-done-stat-val { font-size:1.6em;font-weight:800;color:#065f46; }
html.dark .ta-done-stat-val { color:#6ee7b7 !important; }
.ta-done-stat-box { background:rgba(255,255,255,0.6);border-radius:10px;padding:10px 20px; }
html.dark .ta-done-stat-box { background:rgba(0,0,0,0.3) !important; }

/* ── Interview question cards ── */
.ta-q-card { border-radius:14px;padding:16px 18px;margin-bottom:20px;background:#fff; }
html.dark .ta-q-card { background:#1e293b !important; }
.ta-q-title { font-weight:700;font-size:1em;color:#0f172a;line-height:1.5; }
html.dark .ta-q-title { color:#f1f5f9 !important; }
.ta-q-reason { font-size:0.85em;color:#334155;font-weight:500; }
html.dark .ta-q-reason { color:#94a3b8 !important; }
.ta-q-said { background:#f1f5f9;border-left:4px solid #94a3b8;border-radius:0 8px 8px 0;padding:12px 14px;margin-bottom:10px; }
html.dark .ta-q-said { background:#0f172a !important;border-left-color:#475569 !important; }
.ta-q-said-label { font-size:0.75em;font-weight:800;text-transform:uppercase;letter-spacing:0.08em;color:#475569;margin-bottom:6px; }
html.dark .ta-q-said-label { color:#94a3b8 !important; }
.ta-q-said-text { font-size:0.88em;line-height:1.7;margin:0;color:#1e293b; }
html.dark .ta-q-said-text { color:#cbd5e1 !important; }
.ta-q-ideal { background:#f0fdf4;border-left:4px solid #22c55e;border-radius:0 8px 8px 0;padding:12px 14px;margin-bottom:10px; }
html.dark .ta-q-ideal { background:#052e16 !important;border-left-color:#4ade80 !important; }
.ta-q-ideal-label { font-size:0.75em;font-weight:800;text-transform:uppercase;letter-spacing:0.08em;color:#15803d;margin-bottom:6px; }
html.dark .ta-q-ideal-label { color:#4ade80 !important; }
.ta-q-ideal-text { font-size:0.88em;line-height:1.7;margin:0;color:#14532d;font-style:italic; }
html.dark .ta-q-ideal-text { color:#86efac !important; }
.ta-q-tip { background:#faf5ff;border-left:4px solid #a855f7;border-radius:0 8px 8px 0;padding:12px 14px; }
html.dark .ta-q-tip { background:#1e0a2e !important;border-left-color:#c084fc !important; }
.ta-q-tip-label { font-size:0.75em;font-weight:800;text-transform:uppercase;letter-spacing:0.08em;color:#7c3aed;margin-bottom:6px; }
html.dark .ta-q-tip-label { color:#c084fc !important; }
.ta-q-tip-text { font-size:0.88em;margin:0;color:#3b0764; }
html.dark .ta-q-tip-text { color:#e9d5ff !important; }
.ta-q-deep { margin-top:12px;padding:12px 16px;background:#eff6ff;border:1px solid #bfdbfe;border-radius:12px; }
html.dark .ta-q-deep { background:#0c1a3a !important;border-color:#1e3a5f !important; }
html.dark .ta-q-deep div, html.dark .ta-q-deep b { color:#93c5fd !important; }

/* ── Base ── */
body { background: var(--ta-bg) !important; }
html.dark { color-scheme: dark; }
html.dark body, html.dark .gradio-container, html.dark .main, html.dark .contain {
  background: var(--ta-bg) !important; color: var(--ta-text) !important;
}
html.dark .block, html.dark .form, html.dark .panel-full-width,
html.dark .compact, html.dark .wrap, html.dark .upload-container {
  background: var(--ta-surface) !important; border-color: var(--ta-border) !important;
}
html.dark input, html.dark textarea, html.dark select {
  background: var(--ta-bg) !important; color: var(--ta-text) !important; border-color: var(--ta-border) !important;
}
html.dark span, html.dark p, html.dark div, html.dark h1, html.dark h2,
html.dark h3, html.dark h4, html.dark li, html.dark td { color: var(--ta-text) !important; }
html.dark .label-wrap span, html.dark .block-label, html.dark label span,
html.dark .info, html.dark .file-name { color: var(--ta-sub) !important; }
html.dark .tabs > .tab-nav button {
  color: var(--ta-sub) !important; background: var(--ta-surface) !important; border-color: var(--ta-border) !important;
}
html.dark .tabs > .tab-nav button.selected {
  color: var(--ta-text) !important; border-bottom-color: var(--ta-accent) !important; background: var(--ta-bg) !important;
}
html.dark .tabitem { background: var(--ta-bg) !important; }
html.dark .prose *, html.dark .markdown * { color: var(--ta-text) !important; }
html.dark [role="listbox"] { background: var(--ta-surface) !important; border-color: var(--ta-border) !important; }
html.dark [role="option"] { color: var(--ta-text) !important; background: var(--ta-surface) !important; }
html.dark [role="option"]:hover, html.dark [role="option"][aria-selected="true"] {
  background: var(--ta-border) !important; color: #fff !important;
}
html.dark .accordion, html.dark details { background: var(--ta-surface) !important; border-color: var(--ta-border) !important; }
html.dark .accordion .label-wrap, html.dark details summary { color: var(--ta-text) !important; }
html.dark .checkbox-group label span, html.dark .radio-group label span { color: #cbd5e1 !important; }
html.dark .file-preview { background: var(--ta-surface) !important; color: var(--ta-text) !important; }
html.dark .dropdown-arrow svg { fill: var(--ta-sub) !important; }
html.dark button { background: var(--ta-surface) !important; border-color: var(--ta-border) !important; color: var(--ta-text) !important; }
html.dark ::-webkit-scrollbar-track { background: var(--ta-bg) !important; }
html.dark ::-webkit-scrollbar-thumb { background: var(--ta-border) !important; }
html.dark ::-webkit-scrollbar-thumb:hover { background: #334155 !important; }
html.dark #ta-btn-light { background: transparent !important; color: var(--ta-sub) !important; }
html.dark #ta-btn-dark  { background: var(--ta-accent) !important; color: #fff !important; }

/* ── Top bar ── */
.ta-topbar {
  /* dark mode default — deep navy */
  background: linear-gradient(135deg,#050e20 0%,#0c1f42 45%,#142e6e 100%);
  border-radius: 16px;
  padding: 18px 26px;
  display: flex;
  align-items: center;
  gap: 16px;
  margin-bottom: 10px;
  position: relative;
  overflow: hidden;
  box-shadow: 0 4px 24px rgba(0,0,0,0.2);
  transition: background 0.3s ease, box-shadow 0.3s ease;
}
/* ── Light mode topbar — vibrant royal blue ── */
html:not(.dark) .ta-topbar {
  background: linear-gradient(135deg, #1e40af 0%, #2563eb 45%, #4f46e5 100%);
  box-shadow: 0 4px 24px rgba(37,99,235,0.3), 0 1px 6px rgba(37,99,235,0.15);
}
html:not(.dark) .ta-topbar::after {
  background: radial-gradient(circle, rgba(165,180,252,0.3) 0%, transparent 65%);
}
html:not(.dark) .ta-topbar-tag {
  color: rgba(219,234,254,0.92) !important;
}
html:not(.dark) .ta-pill {
  background: rgba(255,255,255,0.18);
  border-color: rgba(255,255,255,0.28);
  color: #fff;
}
html:not(.dark) .ta-topbar-icon {
  background: rgba(255,255,255,0.28);
  border-color: rgba(255,255,255,0.5);
  box-shadow: 0 2px 10px rgba(0,0,0,0.18), inset 0 1px 0 rgba(255,255,255,0.4);
  font-size: 1.5em;
}
.ta-topbar::after {
  content: '';
  position: absolute;
  top: -60px; right: -40px;
  width: 280px; height: 280px;
  background: radial-gradient(circle, rgba(59,130,246,0.2) 0%, transparent 65%);
  pointer-events: none;
}
.ta-topbar-icon {
  width: 42px; height: 42px;
  background: rgba(255,255,255,0.1);
  border: 1px solid rgba(255,255,255,0.18);
  border-radius: 12px;
  display: flex; align-items: center; justify-content: center;
  font-size: 1.35em; flex-shrink: 0; position: relative; z-index: 1;
}
.ta-topbar-body { flex: 1; min-width: 0; position: relative; z-index: 1; }
.ta-topbar-name {
  font-size: 1.2em; font-weight: 800; letter-spacing: -0.02em;
  color: #fff; display: block; line-height: 1.15;
}
.ta-topbar-tag {
  font-size: 0.76em; color: rgba(148,163,184,0.9);
  display: block; margin-top: 2px;
}
.ta-topbar-pills { display: flex; gap: 6px; flex-wrap: wrap; align-items: center; position: relative; z-index: 1; }
.ta-pill {
  font-size: 0.69em; font-weight: 600; padding: 3px 10px;
  border-radius: 20px; background: rgba(255,255,255,0.1);
  border: 1px solid rgba(255,255,255,0.14); color: #cbd5e1; white-space: nowrap;
}

/* ── Section label ── */
.ta-section-label {
  font-size: 0.65em; font-weight: 700; text-transform: uppercase;
  letter-spacing: 0.13em; color: var(--ta-sub);
  padding: 14px 0 6px; display: block;
  border-bottom: 1px solid var(--ta-border); margin-bottom: 8px;
}

/* ── Stat cells ── */
.ta-stat-cell { display:flex;flex-direction:column;align-items:center;padding:0 16px;text-align:center;flex:1;min-width:90px; }
.ta-stat-val  { font-size:0.87em;font-weight:700;color:var(--ta-card-text);white-space:nowrap; }
.ta-stat-key  { font-size:0.67em;font-weight:600;text-transform:uppercase;letter-spacing:0.08em;color:var(--ta-card-sub);margin-top:2px; }

/* ── Checkboxes ── */
input[type="checkbox"] {
  -webkit-appearance: none !important; appearance: none !important;
  width: 17px !important; height: 17px !important; min-width: 17px !important;
  border: 2px solid var(--ta-accent) !important;
  border-radius: 4px !important; background: var(--ta-surface) !important;
  cursor: pointer !important; position: relative !important;
  vertical-align: middle !important; flex-shrink: 0 !important;
  transition: background 0.14s, border-color 0.14s !important;
}
input[type="checkbox"]:checked {
  background: var(--ta-accent) !important; border-color: var(--ta-accent) !important;
}
input[type="checkbox"]:checked::after {
  content: '' !important; position: absolute !important;
  left: 4px !important; top: 1px !important;
  width: 5px !important; height: 9px !important;
  border: 2px solid #fff !important; border-top: none !important; border-left: none !important;
  transform: rotate(45deg) !important; display: block !important;
}
html.dark input[type="checkbox"] { background: var(--ta-bg) !important; }

/* ── Analyze button pulse animation ── */
@keyframes ta-pulse-ring {
  0%   { box-shadow: 0 0 0 0 rgba(220,38,38,0.55), 0 3px 12px rgba(220,38,38,0.35); }
  60%  { box-shadow: 0 0 0 10px rgba(220,38,38,0), 0 3px 12px rgba(220,38,38,0.35); }
  100% { box-shadow: 0 0 0 0 rgba(220,38,38,0), 0 3px 12px rgba(220,38,38,0.35); }
}
@keyframes ta-spin { to { transform: rotate(360deg); } }

/* ── Analyze button ── */
button.ta-analyze-btn, #ta-analyze-btn {
  background: linear-gradient(135deg,#b91c1c,#ef4444) !important;
  color: #fff !important; font-size: 0.92em !important; font-weight: 700 !important;
  border: none !important; border-radius: 9px !important;
  padding: 10px 20px !important; width: 100% !important;
  letter-spacing: 0.02em !important; cursor: pointer !important;
  animation: ta-pulse-ring 1.8s ease-out infinite !important;
  transition: background 0.18s, transform 0.18s !important;
}
button.ta-analyze-btn:hover, #ta-analyze-btn:hover {
  background: linear-gradient(135deg,#991b1b,#dc2626) !important;
  transform: translateY(-1px) !important;
}
button.ta-analyze-btn:active, #ta-analyze-btn:active {
  transform: translateY(1px) !important;
  animation: none !important;
}
button.ta-analyze-btn.ta-running, #ta-analyze-btn.ta-running {
  background: linear-gradient(135deg,#7f1d1d,#b91c1c) !important;
  animation: none !important;
  cursor: default !important;
  opacity: 0.85 !important;
}
html.dark button.ta-analyze-btn, html.dark #ta-analyze-btn {
  background: linear-gradient(135deg,#991b1b,#ef4444) !important;
  color: #fff !important; border: none !important;
}

/* ── Stop / Cancel button ── */
.ta-cancel-btn { flex: 0 0 auto !important; min-width: 80px !important; }
.ta-cancel-btn button {
  background: #dc2626 !important;
  color: #fff !important;
  border: 2px solid #fca5a5 !important;
  border-radius: 8px !important;
  font-size: 0.85em !important;
  font-weight: 800 !important;
  letter-spacing: 0.03em !important;
  padding: 7px 14px !important;
  box-shadow: 0 3px 12px rgba(220,38,38,0.5), inset 0 1px 0 rgba(255,255,255,0.15) !important;
  transition: all 0.12s !important;
  width: 100% !important;
  cursor: pointer !important;
}
.ta-cancel-btn button:hover {
  background: #b91c1c !important;
  border-color: #f87171 !important;
  transform: translateY(-1px) !important;
  box-shadow: 0 5px 18px rgba(220,38,38,0.65) !important;
}
.ta-cancel-btn button:active {
  transform: translateY(2px) !important;
  box-shadow: 0 1px 4px rgba(220,38,38,0.4) !important;
  background: #991b1b !important;
}
.ta-cancel-btn button:active { transform: translateY(0) !important; }
html.dark .ta-cancel-btn button { box-shadow: 0 2px 10px rgba(239,68,68,0.4) !important; }
.ta-status-bar { flex: 1 1 auto !important; min-width: 0 !important; }

/* ── Dropdowns ── */
[role="listbox"] { max-height: 220px !important; overflow-y: auto !important; }
#provider-sel [role="listbox"], #model-sel [role="listbox"] { max-height: 280px !important; scrollbar-width: thin !important; }

/* ── Update banner ── */
.ta-update-banner {
  background: linear-gradient(135deg,#eff6ff,#dbeafe);
  border: 2px solid #3b82f6; border-radius: 12px;
  padding: 14px 18px; margin: 8px 0; font-family: sans-serif;
}
html.dark .ta-update-banner {
  background: linear-gradient(135deg,#1e3a5f,rgba(30,64,175,0.12)) !important;
  border-color: #60a5fa !important;
}
.ta-upd-btn {
  padding: 7px 14px; border-radius: 8px; border: none;
  cursor: pointer; font-weight: 700; font-size: 0.84em;
  transition: all 0.18s; white-space: nowrap;
}
.ta-upd-win { background: #1d4ed8; color: #fff; }
.ta-upd-win:hover { background: #1e40af; transform: translateY(-1px); }
.ta-upd-mac { background: #1f2937; color: #fff; }
.ta-upd-mac:hover { background: #111827; transform: translateY(-1px); }
.ta-upd-btn:disabled { opacity: 0.6; cursor: not-allowed; transform: none !important; }
#ta-hidden-update-btn,#ta-hidden-update-btn button { display:none !important; visibility:hidden !important; opacity:0 !important; pointer-events:none !important; width:0 !important; height:0 !important; overflow:hidden !important; position:fixed !important; left:-9999px !important; }

/* ── Banner text fix ── */
#api-banner strong, #api-banner b { color: inherit !important; font-weight: 700; }

/* ── Responsive / Mobile ── */
@media (max-width: 768px) {
  /* Stack the two main columns vertically */
  .gradio-container .gr-row { flex-direction: column !important; }
  .gradio-container .gr-column { min-width: 0 !important; width: 100% !important; flex: 1 1 100% !important; }

  /* Top bar: stack provider + model dropdowns */
  #provider-sel, #model-sel { min-width: 0 !important; width: 100% !important; }

  /* Header topbar: wrap pills and shrink font */
  .ta-topbar { padding: 12px 14px !important; }
  .ta-topbar-pills { flex-wrap: wrap !important; gap: 4px !important; }
  .ta-topbar-pills .ta-pill { font-size: 0.62em !important; padding: 2px 7px !important; }

  /* Stats bar: wrap cells */
  .ta-stat-row { flex-wrap: wrap !important; gap: 8px !important; }
  .ta-stat-cell { min-width: 70px !important; padding: 0 8px !important; }

  /* Log box: reduce height on mobile */
  #ta-log-wrap { max-height: 200px !important; }

  /* Download buttons: stack */
  .ta-dl-wrap .ta-dl-btn { width: 100% !important; box-sizing: border-box !important; }

  /* Prevent images/panels overflowing */
  img, .ta-done-panel, .ta-q-card { max-width: 100% !important; box-sizing: border-box !important; }

  /* Tabs: allow scrolling */
  .tab-nav { overflow-x: auto !important; white-space: nowrap !important; }

  /* Buttons: full width on mobile */
  #ta-analyze-btn button, #ta-cancel-btn button {
    font-size: 0.88em !important;
  }

  /* Section headers */
  .ta-section-head { font-size: 0.72em !important; }
}

@media (max-width: 480px) {
  /* Extra small phones */
  .ta-topbar { padding: 10px 10px !important; }
  .ta-topbar-icon { font-size: 1.1em !important; }
  .ta-topbar-title { font-size: 0.92em !important; }
  .ta-stat-cell { min-width: 60px !important; font-size: 0.78em !important; }
  #ta-log-wrap { font-size: 0.72em !important; max-height: 160px !important; }
  .ta-q-card { padding: 10px 12px !important; }
}

"""

_SB = (
    "background:#ffffff;border:3px solid #2563eb;border-radius:10px;"
    "padding:16px 20px;font-size:1.05em;font-family:sans-serif;"
    "min-height:60px;box-shadow:0 2px 10px rgba(37,99,235,0.15);"
)

_ANIM = (
    "<style>"
    "@keyframes pgslide{0%{left:-45%}100%{left:110%}}"
    "</style>"
)

def _fmt_eta(eta_secs: int) -> str:
    m, s = divmod(max(0, eta_secs), 60)
    return f"{m}m {s:02d}s" if m else f"{s}s"


def _status_compact(icon: str, title: str, elapsed: str = "") -> str:
    """Minimal one-line status — used when eta_panel carries the detail."""
    elap = (f'<span style="color:var(--ta-card-sub);font-size:.85em;margin-left:10px;">'
            f'elapsed: {elapsed}</span>') if elapsed else ""
    return (f'<div style="background:var(--ta-card-bg);border:3px solid #2563eb;border-radius:10px;'
            f'padding:16px 20px;font-size:1.05em;font-family:sans-serif;min-height:60px;'
            f'box-shadow:0 2px 10px rgba(37,99,235,0.15);">'
            f'<div style="color:var(--ta-card-text);font-weight:700;font-size:1em;">'
            f'{icon} {title}{elap}</div></div>')


def _status_html(icon: str, title: str, subtitle: str = "", elapsed: str = "",
                 pct: float = None, eta_secs: int = None) -> str:
    import datetime as _dt

    elap = (
        f'<span style="color:#6b7280;font-size:.88em;margin-left:12px;font-weight:400;">'
        f'elapsed: {elapsed}</span>'
    ) if elapsed else ""

    sub = (
        f'<div style="color:#374151;font-size:.93em;margin-top:5px;">{subtitle}</div>'
    ) if subtitle else ""

    eta_html = ""
    if eta_secs is not None and eta_secs > 0:
        finish_str = (_dt.datetime.now() + _dt.timedelta(seconds=eta_secs)).strftime("%I:%M %p").lstrip("0")
        eta_html = (
            f'<div style="margin-top:8px;display:flex;gap:10px;flex-wrap:wrap;">'
            f'<span style="background:#dbeafe;border-radius:6px;padding:4px 12px;'
            f'color:#1d4ed8;font-weight:700;">⏱ ETA {_fmt_eta(eta_secs)}</span>'
            f'<span style="background:#f0fdf4;border-radius:6px;padding:4px 12px;'
            f'color:#15803d;font-weight:700;">🕐 Done by {finish_str}</span>'
            f'</div>'
        )

    if pct is not None:
        fill = f"{pct*100:.0f}%"
        bar_html = (
            f'<div style="margin-top:10px;background:#dbeafe;border-radius:8px;height:18px;overflow:hidden;">'
            f'<div style="width:{fill};height:100%;background:#2563eb;border-radius:8px;'
            f'transition:width 0.6s ease;"></div></div>'
            f'<div style="color:#1d4ed8;font-weight:700;font-size:.95em;margin-top:4px;">{fill} complete</div>'
        )
    else:
        bar_html = (
            f'{_ANIM}'
            f'<div style="margin-top:10px;background:#dbeafe;border-radius:8px;height:18px;'
            f'overflow:hidden;position:relative;">'
            f'<div style="position:absolute;width:45%;height:100%;background:#2563eb;'
            f'border-radius:8px;animation:pgslide 1.4s ease-in-out infinite;"></div></div>'
        )

    return (
        f'<div style="{_SB}">'
        f'<div style="color:#111827;font-weight:700;font-size:1.1em;">{icon} {title}{elap}</div>'
        f'{sub}{eta_html}{bar_html}'
        f'</div>'
    )

PACE_EMOJI = {"Slow": "🐢", "Normal": "🚶", "Fast": "🏃", "Very Fast": "⚡"}

# ── language options ──────────────────────────────────────────────────────────

LANGUAGES = [
    ("🔍 Auto-detect",                "auto"),
    ("🇺🇸 English",                   "en"),
    ("🇪🇸 Spanish",                   "es"),
    ("🇫🇷 French",                    "fr"),
    ("🇧🇷 Portuguese",                "pt"),
    ("🇩🇪 German",                    "de"),
    ("🇮🇹 Italian",                   "it"),
    ("🇯🇵 Japanese",                  "ja"),
    ("🇨🇳 Chinese (Mandarin)",         "zh"),
    ("🇰🇷 Korean",                    "ko"),
    ("🇸🇦 Arabic",                    "ar"),
    ("🇷🇺 Russian",                   "ru"),
    # ── Indian languages ─────────────────────────────────────────────────────
    ("🇮🇳 Hindi",                     "hi"),
    ("🇮🇳 Bengali",                   "bn"),
    ("🇮🇳 Tamil",                     "ta"),
    ("🇮🇳 Telugu",                    "te"),
    ("🇮🇳 Gujarati",                  "gu"),
    ("🇮🇳 Kannada",                   "kn"),
    ("🇮🇳 Malayalam",                 "ml"),
    ("🇮🇳 Marathi",                   "mr"),
    ("🇮🇳 Punjabi",                   "pa"),
    ("🇮🇳 Urdu",                      "ur"),
    # ── Other languages ───────────────────────────────────────────────────────
    ("🇳🇱 Dutch",                     "nl"),
    ("🇵🇱 Polish",                    "pl"),
    ("🇹🇷 Turkish",                   "tr"),
    ("🇻🇳 Vietnamese",                "vi"),
    ("🇹🇭 Thai",                      "th"),
    ("🇸🇪 Swedish",                   "sv"),
    ("🇳🇴 Norwegian",                 "no"),
    ("🇩🇰 Danish",                    "da"),
    ("🇬🇷 Greek",                     "el"),
    ("🇮🇱 Hebrew",                    "he"),
    ("🇷🇴 Romanian",                  "ro"),
    ("🇭🇺 Hungarian",                 "hu"),
    ("🇨🇿 Czech",                     "cs"),
    ("🇫🇮 Finnish",                   "fi"),
    ("🇺🇦 Ukrainian",                 "uk"),
    ("🇮🇩 Indonesian",                "id"),
]

# ── per-language regional variants ────────────────────────────────────────────
# First entry in each list is the "auto / no hint" value; these are filtered out
# before being passed to the backend so only meaningful hints are forwarded.

LANGUAGE_VARIANTS = {
    "en": [
        ("🌍 Auto (General English)",          "General English"),
        ("🇺🇸 American English",               "American English (en-US)"),
        ("🇬🇧 British English",                "British English (en-GB)"),
        ("🇦🇺 Australian English",             "Australian English (en-AU)"),
        ("🇨🇦 Canadian English",               "Canadian English (en-CA)"),
        ("🇮🇪 Irish English",                  "Irish English (en-IE)"),
        ("🏴 Scottish English",                "Scottish English"),
        ("🇿🇦 South African English",          "South African English (en-ZA)"),
        ("🇮🇳 Indian English",                 "Indian English (en-IN)"),
        ("🇸🇬 Singaporean English",            "Singaporean English (en-SG)"),
        ("🇵🇭 Filipino English",               "Filipino English (en-PH)"),
        ("🌍 Nigerian English",                "Nigerian English"),
        ("🇯🇲 Caribbean English",              "Caribbean English"),
    ],
    "es": [
        ("🌎 Auto (General Spanish)",          "General Spanish"),
        ("🇨🇴 Colombian Spanish",              "Colombian Spanish (es-CO)"),
        ("🇲🇽 Mexican Spanish",                "Mexican Spanish (es-MX)"),
        ("🇪🇸 Castilian Spanish (Spain)",      "Castilian Spanish (es-ES)"),
        ("🇦🇷 Argentinian Spanish",            "Argentinian Spanish (es-AR)"),
        ("🇨🇱 Chilean Spanish",                "Chilean Spanish (es-CL)"),
        ("🇻🇪 Venezuelan Spanish",             "Venezuelan Spanish (es-VE)"),
        ("🇵🇪 Peruvian Spanish",               "Peruvian Spanish (es-PE)"),
        ("🇨🇺 Cuban Spanish",                  "Cuban Spanish (es-CU)"),
        ("🇵🇷 Puerto Rican Spanish",           "Puerto Rican Spanish (es-PR)"),
        ("🇩🇴 Dominican Spanish",              "Dominican Spanish (es-DO)"),
        ("🇬🇹 Guatemalan Spanish",             "Guatemalan Spanish (es-GT)"),
        ("🇪🇨 Ecuadorian Spanish",             "Ecuadorian Spanish (es-EC)"),
        ("🇧🇴 Bolivian Spanish",               "Bolivian Spanish (es-BO)"),
        ("🇺🇾 Uruguayan Spanish",              "Uruguayan Spanish (es-UY)"),
        ("🇵🇾 Paraguayan Spanish",             "Paraguayan Spanish (es-PY)"),
        ("🇭🇳 Honduran Spanish",               "Honduran Spanish (es-HN)"),
        ("🇸🇻 Salvadoran Spanish",             "Salvadoran Spanish (es-SV)"),
        ("🇳🇮 Nicaraguan Spanish",             "Nicaraguan Spanish (es-NI)"),
        ("🇨🇷 Costa Rican Spanish",            "Costa Rican Spanish (es-CR)"),
        ("🇵🇦 Panamanian Spanish",             "Panamanian Spanish (es-PA)"),
        ("🇺🇸 US Latino Spanish",              "US Latino Spanish (es-US)"),
    ],
    "fr": [
        ("🌍 Auto (General French)",           "General French"),
        ("🇫🇷 France French",                  "France French (fr-FR)"),
        ("🇨🇦 Canadian French (Québécois)",    "Canadian French (fr-CA)"),
        ("🇧🇪 Belgian French",                 "Belgian French (fr-BE)"),
        ("🇨🇭 Swiss French",                   "Swiss French (fr-CH)"),
        ("🌍 West African French",             "West African French"),
        ("🌍 North African French",            "North African French"),
        ("🇲🇬 Malagasy French",               "Malagasy French"),
    ],
    "pt": [
        ("🌍 Auto (General Portuguese)",       "General Portuguese"),
        ("🇧🇷 Brazilian Portuguese",           "Brazilian Portuguese (pt-BR)"),
        ("🇵🇹 European Portuguese",            "European Portuguese (pt-PT)"),
        ("🇦🇴 Angolan Portuguese",             "Angolan Portuguese (pt-AO)"),
        ("🇲🇿 Mozambican Portuguese",          "Mozambican Portuguese (pt-MZ)"),
        ("🇨🇻 Cape Verdean Portuguese",        "Cape Verdean Portuguese"),
        ("🇸🇹 São Tomé Portuguese",            "São Tomé Portuguese"),
    ],
    "de": [
        ("🌍 Auto (General German)",           "General German"),
        ("🇩🇪 Standard German (Germany)",      "Standard German (de-DE)"),
        ("🇦🇹 Austrian German",                "Austrian German (de-AT)"),
        ("🇨🇭 Swiss German",                   "Swiss German (de-CH)"),
        ("🇱🇺 Luxembourg German",              "Luxembourg German"),
        ("🌍 Bavarian dialect",                "Bavarian German"),
        ("🌍 Low German (Plattdeutsch)",        "Low German"),
    ],
    "it": [
        ("🌍 Auto (General Italian)",          "General Italian"),
        ("🇮🇹 Standard Italian",               "Standard Italian (it-IT)"),
        ("🇨🇭 Swiss Italian",                  "Swiss Italian (it-CH)"),
        ("🌍 Sicilian",                        "Sicilian Italian"),
        ("🌍 Neapolitan",                      "Neapolitan Italian"),
        ("🌍 Venetian dialect",                "Venetian Italian"),
        ("🌍 Roman dialect",                   "Roman Italian"),
    ],
    "zh": [
        ("🌍 Auto (General Chinese)",          "General Chinese"),
        ("🇨🇳 Mandarin Simplified (Mainland)", "Mainland Mandarin (zh-CN)"),
        ("🇹🇼 Mandarin Traditional (Taiwan)",  "Taiwan Mandarin (zh-TW)"),
        ("🇭🇰 Cantonese (Hong Kong)",          "Cantonese (zh-HK)"),
        ("🇸🇬 Singaporean Mandarin",           "Singaporean Mandarin (zh-SG)"),
        ("🌍 Shanghainese / Wu",               "Shanghainese (Wu dialect)"),
    ],
    "ja": [
        ("🌍 Auto (General Japanese)",         "General Japanese"),
        ("🇯🇵 Standard Japanese (Tokyo)",      "Standard Japanese (Tokyo)"),
        ("🌍 Kansai / Osaka dialect",          "Kansai dialect"),
        ("🌍 Kyushu dialect",                  "Kyushu dialect"),
        ("🌍 Tohoku dialect",                  "Tohoku dialect"),
    ],
    "ko": [
        ("🌍 Auto (General Korean)",           "General Korean"),
        ("🇰🇷 Standard Korean (Seoul)",        "Standard Korean (Seoul)"),
        ("🌍 Gyeongsang dialect",              "Gyeongsang dialect"),
        ("🌍 Jeolla dialect",                  "Jeolla dialect"),
        ("🌍 Jeju dialect",                    "Jeju dialect"),
    ],
    "ar": [
        ("🌍 Auto / Modern Standard Arabic",   "Modern Standard Arabic"),
        ("🇪🇬 Egyptian Arabic",                "Egyptian Arabic"),
        ("🇸🇦 Saudi Arabic",                   "Saudi Arabic"),
        ("🇦🇪 Gulf Arabic",                    "Gulf Arabic"),
        ("🇱🇧 Levantine Arabic",               "Levantine Arabic"),
        ("🇲🇦 Moroccan Arabic (Darija)",       "Moroccan Arabic"),
        ("🇮🇶 Iraqi Arabic",                   "Iraqi Arabic"),
        ("🇹🇳 Tunisian Arabic",                "Tunisian Arabic"),
        ("🇩🇿 Algerian Arabic",                "Algerian Arabic"),
        ("🇾🇪 Yemeni Arabic",                  "Yemeni Arabic"),
        ("🇸🇩 Sudanese Arabic",                "Sudanese Arabic"),
    ],
    "ru": [
        ("🌍 Auto (General Russian)",          "General Russian"),
        ("🇷🇺 Standard Russian (Moscow)",      "Standard Russian"),
        ("🌍 St. Petersburg Russian",          "St. Petersburg Russian"),
        ("🌍 Siberian Russian",                "Siberian Russian"),
        ("🌍 Ural Russian",                    "Ural Russian"),
    ],
    # ── Indian languages ────────────────────────────────────────────────────
    "hi": [
        ("🔍 Auto (General Hindi)",            "General Hindi"),
        ("🇮🇳 Standard Hindi (Delhi)",         "Standard Hindi (Delhi)"),
        ("🇮🇳 Mumbai Hindi",                   "Mumbai Hindi"),
        ("🇮🇳 Bihari Hindi",                   "Bihari Hindi"),
        ("🇮🇳 Rajasthani Hindi",               "Rajasthani Hindi"),
        ("🇮🇳 Bhojpuri-accented Hindi",        "Bhojpuri-accented Hindi"),
    ],
    "bn": [
        ("🔍 Auto (General Bengali)",          "General Bengali"),
        ("🇮🇳 Indian Bengali (Kolkata)",       "Indian Bengali (Kolkata)"),
        ("🇧🇩 Bangladeshi Bengali (Dhaka)",    "Bangladeshi Bengali (Dhaka)"),
        ("🌍 Sylheti dialect",                 "Sylheti dialect"),
        ("🌍 Chittagonian dialect",            "Chittagonian dialect"),
    ],
    "ta": [
        ("🔍 Auto (General Tamil)",            "General Tamil"),
        ("🇮🇳 Indian Tamil (Chennai)",         "Indian Tamil (Chennai)"),
        ("🇱🇰 Sri Lankan Tamil",               "Sri Lankan Tamil"),
        ("🇸🇬 Singaporean Tamil",              "Singaporean Tamil"),
        ("🇲🇾 Malaysian Tamil",                "Malaysian Tamil"),
    ],
    "te": [
        ("🔍 Auto (General Telugu)",           "General Telugu"),
        ("🇮🇳 Standard Telugu (Hyderabad)",    "Standard Telugu (Hyderabad)"),
        ("🇮🇳 Coastal Andhra Telugu",          "Coastal Andhra Telugu"),
        ("🇮🇳 Rayalaseema Telugu",             "Rayalaseema Telugu"),
    ],
    "gu": [
        ("🔍 Auto (General Gujarati)",         "General Gujarati"),
        ("🇮🇳 Standard Gujarati (Ahmedabad)",  "Standard Gujarati"),
        ("🇮🇳 Saurashtra Gujarati",            "Saurashtra Gujarati"),
        ("🇬🇧 British Gujarati",               "British Gujarati"),
        ("🇺🇸 American Gujarati",              "American Gujarati"),
    ],
    "kn": [
        ("🔍 Auto (General Kannada)",          "General Kannada"),
        ("🇮🇳 Standard Kannada (Bangalore)",   "Standard Kannada (Bangalore)"),
        ("🇮🇳 Dharwad Kannada",                "Dharwad Kannada"),
        ("🇮🇳 Old Mysore Kannada",             "Old Mysore Kannada"),
    ],
    "ml": [
        ("🔍 Auto (General Malayalam)",        "General Malayalam"),
        ("🇮🇳 Standard Malayalam (Kerala)",    "Standard Malayalam"),
        ("🇮🇳 Central Kerala dialect",         "Central Kerala dialect"),
        ("🇮🇳 North Kerala (Malabar) dialect", "North Kerala dialect"),
        ("🌍 Gulf Malayalam",                  "Gulf Malayalam"),
    ],
    "mr": [
        ("🔍 Auto (General Marathi)",          "General Marathi"),
        ("🇮🇳 Standard Marathi (Pune/Mumbai)", "Standard Marathi"),
        ("🇮🇳 Nagpuri Marathi",               "Nagpuri Marathi"),
        ("🇮🇳 Konkani-accented Marathi",       "Konkani-accented Marathi"),
    ],
    "pa": [
        ("🔍 Auto (General Punjabi)",          "General Punjabi"),
        ("🇮🇳 Indian Punjabi (Amritsar)",      "Indian Punjabi"),
        ("🇵🇰 Pakistani Punjabi (Lahore)",     "Pakistani Punjabi"),
        ("🇬🇧 British Punjabi",               "British Punjabi"),
        ("🇨🇦 Canadian Punjabi",              "Canadian Punjabi"),
    ],
    "ur": [
        ("🌍 Auto (General Urdu)",             "General Urdu"),
        ("🇵🇰 Pakistani Urdu",                 "Pakistani Urdu"),
        ("🇮🇳 Indian Urdu",                    "Indian Urdu"),
        ("🌍 Deccani Urdu",                    "Deccani Urdu"),
    ],
    # ── Other languages ─────────────────────────────────────────────────────
    "nl": [
        ("🌍 Auto (General Dutch)",            "General Dutch"),
        ("🇳🇱 Netherlands Dutch",              "Netherlands Dutch (nl-NL)"),
        ("🇧🇪 Belgian Dutch / Flemish",        "Belgian Dutch / Flemish (nl-BE)"),
        ("🇸🇷 Surinamese Dutch",               "Surinamese Dutch"),
    ],
    "tr": [
        ("🌍 Auto (General Turkish)",          "General Turkish"),
        ("🇹🇷 Istanbul Turkish",               "Istanbul Turkish"),
        ("🌍 Anatolian Turkish",               "Anatolian Turkish"),
        ("🇨🇾 Cypriot Turkish",                "Cypriot Turkish"),
    ],
    "vi": [
        ("🌍 Auto (General Vietnamese)",       "General Vietnamese"),
        ("🇻🇳 Northern Vietnamese (Hanoi)",    "Northern Vietnamese"),
        ("🇻🇳 Southern Vietnamese (Ho Chi Minh City)", "Southern Vietnamese"),
        ("🇻🇳 Central Vietnamese",             "Central Vietnamese"),
    ],
    "sv": [
        ("🌍 Auto (General Swedish)",          "General Swedish"),
        ("🇸🇪 Sweden Swedish",                 "Sweden Swedish"),
        ("🇫🇮 Finland Swedish",                "Finland Swedish"),
    ],
    "no": [
        ("🌍 Auto (General Norwegian)",        "General Norwegian"),
        ("🌍 Bokmål",                          "Norwegian Bokmål"),
        ("🌍 Nynorsk",                         "Norwegian Nynorsk"),
    ],
    "pl": [
        ("🌍 Auto (General Polish)",           "General Polish"),
        ("🌍 Warsaw Polish",                   "Warsaw Polish"),
        ("🌍 Silesian-accented Polish",        "Silesian-accented Polish"),
        ("🌍 Kashubian-accented Polish",       "Kashubian-accented Polish"),
    ],
    "th": [
        ("🌍 Auto (General Thai)",             "General Thai"),
        ("🇹🇭 Central Thai (Bangkok)",         "Central Thai"),
        ("🌍 Northern Thai (Kham Mueang)",     "Northern Thai"),
        ("🌍 Northeastern Thai (Isan)",        "Northeastern Thai"),
        ("🌍 Southern Thai",                   "Southern Thai"),
    ],
    "el": [
        ("🌍 Auto (General Greek)",            "General Greek"),
        ("🇬🇷 Standard Modern Greek",          "Standard Modern Greek"),
        ("🇨🇾 Cypriot Greek",                  "Cypriot Greek"),
    ],
    "he": [
        ("🌍 Auto (General Hebrew)",           "General Hebrew"),
        ("🇮🇱 Modern Israeli Hebrew",          "Modern Israeli Hebrew"),
        ("🌍 Mizrahi-accented Hebrew",         "Mizrahi-accented Hebrew"),
        ("🌍 Ashkenazi-accented Hebrew",       "Ashkenazi-accented Hebrew"),
    ],
    "ro": [
        ("🌍 Auto (General Romanian)",         "General Romanian"),
        ("🇷🇴 Romanian (Romania)",             "Romanian (ro-RO)"),
        ("🇲🇩 Moldovan Romanian",              "Moldovan Romanian"),
    ],
    "hu": [
        ("🌍 Auto (General Hungarian)",        "General Hungarian"),
        ("🇭🇺 Standard Hungarian",             "Standard Hungarian"),
        ("🇷🇴 Transylvanian Hungarian",        "Transylvanian Hungarian"),
        ("🇸🇰 Slovak Hungarian",               "Slovak Hungarian"),
    ],
    "cs": [
        ("🌍 Auto (General Czech)",            "General Czech"),
        ("🇨🇿 Bohemian Czech",                 "Bohemian Czech"),
        ("🌍 Moravian Czech",                  "Moravian Czech"),
        ("🌍 Silesian Czech",                  "Silesian Czech"),
    ],
    "fi": [
        ("🌍 Auto (General Finnish)",          "General Finnish"),
        ("🇫🇮 Standard Finnish",               "Standard Finnish"),
        ("🇫🇮 Helsinki Finnish",               "Helsinki Finnish"),
        ("🌍 Finland Swedish-accented Finnish","Finland Swedish-accented Finnish"),
    ],
    "da": [
        ("🌍 Auto (General Danish)",           "General Danish"),
        ("🇩🇰 Standard Danish (Copenhagen)",   "Standard Danish"),
        ("🌍 Jutlandic Danish",                "Jutlandic Danish"),
    ],
    "uk": [
        ("🌍 Auto (General Ukrainian)",        "General Ukrainian"),
        ("🇺🇦 Standard Ukrainian (Kyiv)",      "Standard Ukrainian"),
        ("🌍 Western Ukrainian",               "Western Ukrainian"),
        ("🌍 Eastern Ukrainian",               "Eastern Ukrainian"),
    ],
    "id": [
        ("🌍 Auto (General Indonesian)",       "General Indonesian"),
        ("🇮🇩 Standard Bahasa Indonesia",      "Standard Bahasa Indonesia"),
        ("🌍 Javanese-accented",               "Javanese-accented Indonesian"),
        ("🌍 Sundanese-accented",              "Sundanese-accented Indonesian"),
        ("🌍 Balinese-accented",               "Balinese-accented Indonesian"),
        ("🌍 Batak-accented",                  "Batak-accented Indonesian"),
    ],
}

# Values that represent "no specific variant" — filtered out before passing to backend
_VARIANT_AUTO_VALUES = {v[0][1] for v in LANGUAGE_VARIANTS.values()}


# ── helpers ───────────────────────────────────────────────────────────────────

def stats_to_markdown(speaker_stats) -> str:
    if not speaker_stats:
        return "_No speech analytics available. Upload an audio or video file to see speaker stats._"
    lines = [
        '<div class="ta-pace-ref" style="display:flex;flex-wrap:wrap;gap:8px;align-items:center;'
        'margin-bottom:14px;padding:10px 14px;background:#f8fafc;'
        'border:1px solid #e2e8f0;border-radius:10px;">'
        '<span class="ta-pace-label" style="font-size:0.75em;font-weight:700;text-transform:uppercase;'
        'letter-spacing:0.08em;color:#64748b;margin-right:4px;">Pace&nbsp;reference</span>'
        '<span class="ta-chip ta-chip-slow" style="background:#f1f5f9;border:1px solid #cbd5e1;border-radius:6px;'
        'padding:3px 10px;font-size:0.8em;font-weight:600;color:#475569;">🐢 Slow &lt;120 wpm</span>'
        '<span class="ta-chip ta-chip-normal" style="background:#dbeafe;border:1px solid #93c5fd;border-radius:6px;'
        'padding:3px 10px;font-size:0.8em;font-weight:600;color:#1d4ed8;">🚶 Normal 120–150</span>'
        '<span class="ta-chip ta-chip-fast" style="background:#fef9c3;border:1px solid #fde047;border-radius:6px;'
        'padding:3px 10px;font-size:0.8em;font-weight:600;color:#a16207;">🏃 Fast 150–180</span>'
        '<span class="ta-chip ta-chip-vfast" style="background:#fee2e2;border:1px solid #fca5a5;border-radius:6px;'
        'padding:3px 10px;font-size:0.8em;font-weight:600;color:#dc2626;">⚡ Very Fast &gt;180</span>'
        '</div>',
        "",
    ]
    for s in speaker_stats:
        emoji = PACE_EMOJI.get(s.pace_label, "")
        wpm   = f"**{s.words_per_minute} WPM**" if s.words_per_minute else "N/A"
        pct   = f"{s.speaking_percentage}% of conversation" if s.speaking_percentage else ""

        lines += [
            f"### {emoji} {s.name}",
            "",
            "| Metric | Value |",
            "|--------|-------|",
            f"| 🎙️ Speech rate | {wpm} — {s.pace_label} |",
        ]
        if pct:
            lines.append(f"| 🕐 Speaking time | {pct} |")
        if s.accent_indicators:
            lines.append(f"| 🌍 Accent analysis | {s.accent_indicators} |")
        if s.accent_confidence:
            lines.append(f"| 🎯 Confidence | {s.accent_confidence.capitalize()} |")
        lines.append("")

    return "\n".join(lines)


# ── processing — generator streams every update live to the UI ────────────────
#
# Output order (15 items, must match outputs= list in .click()):
def _generate_pdf(stem: str, combined_text: str, path: Path,
                  result=None, va_result=None) -> str:
    # Generate a structured colour-coded PDF report.
    # When result (TranscriptResult) is provided the report is built from
    # structured data for best quality. Falls back to plain-text parsing
    # when only combined_text is available (e.g. translated reports).
    from fpdf import FPDF

    # ── Colour palette ────────────────────────────────────────────────────────
    _C = {
        "great":   (22, 163, 74),    # green
        "good":    (37, 99, 235),    # blue
        "ni":      (217, 119,  6),   # amber
        "missed":  (220,  38, 38),   # red
        "header":  (30,  41, 59),    # slate-800
        "sub":     (71,  85, 105),   # slate-600
        "muted":   (148,163,184),    # slate-400
        "bg_grey": (241,245,249),    # slate-100
        "bg_blue": (239,246,255),    # blue-50
        "bg_amber":(255,251,235),    # amber-50
        "accent":  (59, 130,246),    # blue-500
        "white":   (255,255,255),
    }
    _SCORE_COL = {
        "Great": _C["great"], "Good": _C["good"],
        "Needs Improvement": _C["ni"], "Missed": _C["missed"],
    }
    _SCORE_ICON = {"Great": "GREAT", "Good": "GOOD",
                   "Needs Improvement": "NEEDS IMPROVEMENT", "Missed": "MISSED"}

    def _s(text: str) -> str:
        """Sanitise to latin-1, strip problematic unicode."""
        return (str(text or "")
                .replace("★","*").replace("◑","~")
                .replace("△","^").replace("✗","x")
                .replace("⚠","!").replace("✅","[ok]")
                .replace("❌","[x]").replace("✓","[ok]")
                .replace("✔","[ok]").replace("☑","[x]")
                .replace("☐","[ ]").replace("•","-")
                .replace("'","'").replace("'","'")
                .replace(""",'"').replace(""",'"')
                .replace("–","-").replace("—","--")
                .replace("\U0001f4dd","").replace("\U0001f4ac","")
                .replace("\U0001f3cb","").replace("\U0001f9ea","")
                .replace("\U0001f3a4","").replace("\U0001f4c2","")
                .replace("\U0001f50d","").replace("\U0001f916","")
                .encode("latin-1", errors="replace").decode("latin-1"))

    class _PDF(FPDF):
        def header(self):
            if self.page_no() == 1:
                return
            self.set_font("Helvetica", "I", 7)
            self.set_text_color(*_C["muted"])
            self.cell(0, 8, _s(stem[:80]), align="L", new_x="LMARGIN", new_y="NEXT")
            self.set_draw_color(*_C["muted"])
            self.set_line_width(0.2)
            self.line(self.l_margin, self.get_y(), self.w - self.r_margin, self.get_y())
            self.ln(2)

        def footer(self):
            self.set_y(-13)
            self.set_font("Helvetica", "I", 7)
            self.set_text_color(*_C["muted"])
            self.cell(0, 8, f"Transcript Agent  |  Page {self.page_no()}", align="C")

    pdf = _PDF()
    pdf.set_auto_page_break(auto=True, margin=20)
    pdf.set_margins(20, 22, 20)
    pdf.add_page()
    W = pdf.w - pdf.l_margin - pdf.r_margin

    def _section_header(title: str, r, g, b):
        pdf.ln(4)
        pdf.set_fill_color(r, g, b)
        pdf.set_text_color(*_C["white"])
        pdf.set_font("Helvetica", "B", 10)
        pdf.cell(W, 8, f"  {_s(title)}", new_x="LMARGIN", new_y="NEXT", fill=True)
        pdf.set_text_color(0, 0, 0)
        pdf.ln(3)

    def _body(text: str, size=10, indent=0):
        pdf.set_font("Helvetica", "", size)
        pdf.set_text_color(*_C["sub"])
        if indent:
            pdf.set_x(pdf.l_margin + indent)
            pdf.multi_cell(W - indent, 5, _s(text))
        else:
            pdf.multi_cell(W, 5, _s(text))
        pdf.set_text_color(0, 0, 0)

    def _label_block(label: str, text: str, bg: tuple, lc: tuple, indent=6):
        """Shaded block: coloured left rule + label + body text."""
        pdf.ln(2)
        pdf.set_fill_color(*bg)
        pdf.set_draw_color(*lc)
        pdf.set_line_width(0.8)
        # measure height by simulating multi_cell
        pdf.set_font("Helvetica", "B", 8)
        label_h = 5
        pdf.set_font("Helvetica", "", 9)
        # draw left rule + shaded rect
        x0, y0 = pdf.get_x() + indent, pdf.get_y()
        block_w = W - indent
        # label
        pdf.set_xy(x0, y0)
        pdf.set_font("Helvetica", "B", 8)
        pdf.set_text_color(*lc)
        pdf.cell(block_w, label_h, _s(label), new_x="LMARGIN", new_y="NEXT")
        # body
        pdf.set_x(x0)
        pdf.set_font("Helvetica", "", 9)
        pdf.set_text_color(*_C["sub"])
        pdf.multi_cell(block_w, 5, _s(text))
        # draw the left rule line
        y1 = pdf.get_y()
        pdf.set_draw_color(*lc)
        pdf.line(pdf.l_margin + indent - 2, y0, pdf.l_margin + indent - 2, y1)
        pdf.set_text_color(0, 0, 0)
        pdf.set_draw_color(0, 0, 0)
        pdf.ln(2)

    # ── Title ─────────────────────────────────────────────────────────────────
    pdf.set_font("Helvetica", "B", 18)
    pdf.set_text_color(*_C["header"])
    pdf.multi_cell(W, 10, _s(stem), align="C")
    pdf.set_draw_color(*_C["accent"])
    pdf.set_line_width(1.0)
    pdf.line(pdf.l_margin, pdf.get_y(), pdf.w - pdf.r_margin, pdf.get_y())
    pdf.ln(6)

    # ── Build from structured result if available ─────────────────────────────
    if result is not None:
        # Meta row
        pdf.set_font("Helvetica", "", 9)
        pdf.set_text_color(*_C["muted"])
        meta = []
        if getattr(result, "detected_language", ""):
            meta.append(f"Language: {result.detected_language}")
        if getattr(result, "stt_engine", ""):
            meta.append(f"STT: {result.stt_engine}")
        if meta:
            pdf.cell(W, 5, "  ".join(meta), new_x="LMARGIN", new_y="NEXT")
            pdf.ln(3)
        pdf.set_text_color(0, 0, 0)

        # Summary
        if getattr(result, "summary", ""):
            _section_header("SUMMARY", *_C["header"])
            _body(result.summary)
            pdf.ln(2)

        # Key Points
        if getattr(result, "key_points", []):
            _section_header("KEY POINTS", *_C["sub"])
            for kp in result.key_points:
                pdf.set_font("Helvetica", "", 10)
                pdf.set_x(pdf.l_margin + 4)
                pdf.multi_cell(W - 4, 5, f"- {_s(kp)}")
            pdf.ln(2)

        # Action Items
        if getattr(result, "action_items", []):
            _section_header("ACTION ITEMS", *_C["sub"])
            for ai in result.action_items:
                if isinstance(ai, dict):
                    txt = ai.get("action", ai.get("item", str(ai)))
                    owner = ai.get("owner", "")
                    tl    = ai.get("timeline", "")
                    line  = f"[ ] {txt}"
                    if owner: line += f"  (Owner: {owner})"
                    if tl:    line += f"  [{tl}]"
                else:
                    line = f"[ ] {str(ai)}"
                pdf.set_font("Helvetica", "", 10)
                pdf.set_x(pdf.l_margin + 4)
                pdf.multi_cell(W - 4, 5, _s(line))
            pdf.ln(2)

        # Speaker profiles
        if getattr(result, "speaker_profiles", {}):
            _section_header("SPEAKER PROFILES", *_C["sub"])
            for name, profile in result.speaker_profiles.items():
                pdf.set_font("Helvetica", "B", 10)
                pdf.cell(W, 5, _s(name), new_x="LMARGIN", new_y="NEXT")
                pdf.set_font("Helvetica", "", 9)
                pdf.set_text_color(*_C["sub"])
                pdf.multi_cell(W, 5, _s(profile))
                pdf.set_text_color(0, 0, 0)
                pdf.ln(2)

        # Interview Coaching
        ia = getattr(result, "interview_analysis", {}) or {}
        if ia and not ia.get("parse_error"):
            _section_header("INTERVIEW COACHING ANALYSIS", *_C["accent"])

            # Score banner
            score   = ia.get("overall_score", "—")
            verdict = ia.get("overall_verdict", "")
            adv     = ia.get("advance_likelihood", "")
            defl    = ia.get("deflection_rate", "")
            pdf.set_fill_color(*_C["bg_grey"])
            pdf.set_font("Helvetica", "B", 14)
            pdf.set_text_color(*_C["header"])
            pdf.cell(W, 10, f"Overall Score: {_s(str(score))}/10", new_x="LMARGIN", new_y="NEXT", fill=True)
            if verdict:
                pdf.set_font("Helvetica", "I", 10)
                pdf.set_text_color(*_C["sub"])
                pdf.multi_cell(W, 5, _s(verdict))
            pdf.set_text_color(0, 0, 0)
            pdf.ln(2)

            if adv or defl:
                pdf.set_font("Helvetica", "", 9)
                stats = []
                if adv:  stats.append(f"Advance Likelihood: {adv}%")
                if defl: stats.append(f"Deflection Rate: {defl}%")
                pdf.cell(W, 5, "  |  ".join(stats), new_x="LMARGIN", new_y="NEXT")
                pdf.ln(3)

            # Per-question
            qs = ia.get("questions", [])
            _DEFL_LABEL = {"partial": "! PARTIALLY DEFLECTED", "full": "X DID NOT ANSWER"}
            for q in qs:
                qid       = q.get("id", "")
                question  = q.get("question", "")
                sc        = q.get("score", "")
                reason    = q.get("score_reason", "")
                dfl       = (q.get("deflection") or "none").lower().strip()
                dfl_note  = q.get("deflection_note", "")
                said      = q.get("answer_said") or q.get("answer_summary", "")
                ideal     = q.get("model_answer") or q.get("ideal_answer", "")
                tip       = q.get("coaching_tip", "")
                sc_col    = _SCORE_COL.get(sc, _C["sub"])
                sc_lbl    = _SCORE_ICON.get(sc, sc.upper())

                # Question header row
                pdf.ln(3)
                pdf.set_fill_color(*_C["bg_grey"])
                pdf.set_font("Helvetica", "B", 9)
                pdf.set_text_color(*_C["header"])
                pdf.multi_cell(W, 6, f"Q{_s(str(qid))}: {_s(question)}", fill=True)

                # Score badge
                pdf.set_fill_color(*sc_col)
                pdf.set_text_color(*_C["white"])
                pdf.set_font("Helvetica", "B", 8)
                pdf.cell(50, 5, f" {sc_lbl} ", fill=True)
                if reason:
                    pdf.set_text_color(*_C["sub"])
                    pdf.set_font("Helvetica", "I", 8)
                    pdf.multi_cell(W - 50, 5, f"  {_s(reason)}")
                else:
                    pdf.ln(5)
                pdf.set_text_color(0, 0, 0)

                # Deflection warning
                if dfl in _DEFL_LABEL:
                    pdf.set_font("Helvetica", "B", 8)
                    pdf.set_text_color(*_C["ni"])
                    pdf.cell(W, 4, _DEFL_LABEL[dfl], new_x="LMARGIN", new_y="NEXT")
                    if dfl_note:
                        pdf.set_font("Helvetica", "I", 8)
                        pdf.multi_cell(W, 4, _s(dfl_note))
                    pdf.set_text_color(0, 0, 0)

                if said:
                    _label_block("WHAT WAS SAID", said, _C["bg_grey"], _C["sub"])
                if ideal:
                    _label_block("WHAT YOU COULD HAVE SAID", ideal, _C["bg_blue"], _C["accent"])
                if tip:
                    _label_block("COACHING TIP", tip, _C["bg_amber"], _C["ni"])

            # Deep analysis
            adv_reason = ia.get("advance_reasoning", "")
            if adv_reason:
                pdf.ln(3)
                _section_header("DEEP ANALYSIS", *_C["sub"])
                if adv: pdf.set_font("Helvetica", "B", 10); pdf.cell(W, 5, f"Advance Likelihood: {adv}%", new_x="LMARGIN", new_y="NEXT")
                if defl: pdf.set_font("Helvetica", "B", 10); pdf.cell(W, 5, f"Deflection Rate: {defl}%", new_x="LMARGIN", new_y="NEXT")
                _body(adv_reason)

        # ── Coding Challenges section ─────────────────────────────────────────
        _coding = ia.get("coding_challenges", []) if ia and not ia.get("parse_error") else []
        if _coding:
            _section_header("CODING CHALLENGE ANALYSIS", *_C["accent"])
            _det_role = (ia or {}).get("detected_role", "")
            if _det_role and _det_role.lower() not in ("", "unknown"):
                pdf.set_font("Helvetica", "I", 9)
                pdf.set_text_color(*_C["sub"])
                pdf.cell(W, 5, f"  Detected Role: {_s(_det_role)}", new_x="LMARGIN", new_y="NEXT")
                pdf.set_text_color(0, 0, 0)
                pdf.ln(2)

            _cs = (ia or {}).get("coding_score")
            if _cs is not None and str(_cs) not in ("", "null"):
                try:
                    _cs_num = int(str(_cs))
                    _cs_col = (_C["great"] if _cs_num >= 8 else _C["good"]
                               if _cs_num >= 6 else _C["ni"] if _cs_num >= 4 else _C["missed"])
                except Exception:
                    _cs_col = _C["sub"]
                pdf.set_fill_color(*_cs_col)
                pdf.set_text_color(*_C["white"])
                pdf.set_font("Helvetica", "B", 13)
                pdf.cell(W, 9, f"  Coding Score: {_s(str(_cs))} / 10", new_x="LMARGIN", new_y="NEXT", fill=True)
                pdf.set_text_color(0, 0, 0)
                pdf.ln(4)

            _CS_COL = {"Great": _C["great"], "Good": _C["good"],
                       "Needs Improvement": _C["ni"], "Missed": _C["missed"]}
            for _ch in _coding:
                _cid      = _ch.get("id", "")
                _prob     = _ch.get("problem", "")
                _ans      = _ch.get("candidate_answer", "")
                _sc       = _ch.get("score", "")
                _reason   = _ch.get("score_reason", "")
                _cappr    = _ch.get("candidate_approach", "")
                _lang_req = _ch.get("language_requested", "")
                _lang_used= _ch.get("language_used", "")
                _libs     = _ch.get("libraries_used", "")
                _opt      = _ch.get("optimal_solution", "")
                _oappr    = _ch.get("optimal_approach", "")
                _tc       = _ch.get("time_complexity", "")
                _spc      = _ch.get("space_complexity", "")
                _role_ctx = _ch.get("role_context", "")
                _tip      = _ch.get("coaching_tip", "")
                _sc_col   = _CS_COL.get(_sc, _C["sub"])

                # Challenge header
                pdf.ln(3)
                pdf.set_fill_color(*_C["bg_grey"])
                pdf.set_font("Helvetica", "B", 9)
                pdf.set_text_color(*_C["header"])
                pdf.multi_cell(W, 6, f"Challenge {_s(str(_cid))}: {_s(_prob)}", fill=True)

                # Language / library line
                _lang_line_parts = []
                if _lang_req and _lang_req.lower() not in ("", "not specified"):
                    _lang_line_parts.append(f"Asked in: {_lang_req}")
                if _lang_used and _lang_used.lower() not in ("", "not specified"):
                    _lang_line_parts.append(f"Solution: {_lang_used}")
                if _libs and _libs.lower() not in ("", "none", "not specified"):
                    _lang_line_parts.append(f"Libraries: {_libs}")
                if _lang_line_parts:
                    pdf.set_font("Helvetica", "I", 8)
                    pdf.set_text_color(*_C["sub"])
                    pdf.cell(W, 4, "  " + "   |   ".join(_lang_line_parts), new_x="LMARGIN", new_y="NEXT")
                    pdf.set_text_color(0, 0, 0)
                    pdf.ln(1)

                # Score badge
                pdf.set_fill_color(*_sc_col)
                pdf.set_text_color(*_C["white"])
                pdf.set_font("Helvetica", "B", 8)
                pdf.cell(50, 5, f" {_s(_sc).upper()} ", fill=True)
                if _reason:
                    pdf.set_text_color(*_C["sub"])
                    pdf.set_font("Helvetica", "I", 8)
                    pdf.multi_cell(W - 50, 5, f"  {_s(_reason)}")
                else:
                    pdf.ln(5)
                pdf.set_text_color(0, 0, 0)

                if _cappr:
                    _label_block("CANDIDATE'S APPROACH", _cappr, _C["bg_grey"], _C["sub"])
                if _ans:
                    _label_block("WHAT THEY SAID / DID", _ans, _C["bg_grey"], _C["sub"])

                # Optimal solution — dark code block
                if _opt:
                    pdf.ln(3)
                    pdf.set_fill_color(15, 23, 42)
                    pdf.set_text_color(148, 163, 184)
                    pdf.set_font("Helvetica", "B", 7)
                    _sol_hdr = "  OPTIMAL SOLUTION"
                    if _lang_used: _sol_hdr += f" — {_lang_used}"
                    if _libs and _libs.lower() not in ("", "none"): _sol_hdr += f" ({_libs})"
                    pdf.cell(W, 5, _s(_sol_hdr), new_x="LMARGIN", new_y="NEXT", fill=True)
                    pdf.set_font("Courier", "", 7.5)
                    pdf.set_text_color(226, 232, 240)
                    for _line in _opt.splitlines():
                        pdf.set_fill_color(15, 23, 42)
                        pdf.set_x(pdf.l_margin)
                        pdf.multi_cell(W, 4.5, _s(_line) if _line.strip() else " ", fill=True)
                    pdf.ln(2)
                    pdf.set_text_color(0, 0, 0)
                    pdf.set_font("Helvetica", "", 9)

                # Complexity + approach + role context
                _pdf_parts = []
                if _oappr:    _pdf_parts.append(f"Approach: {_s(_oappr)}")
                if _tc:       _pdf_parts.append(f"Time: {_s(_tc)}")
                if _spc:      _pdf_parts.append(f"Space: {_s(_spc)}")
                if _role_ctx: _pdf_parts.append(f"Role context: {_s(_role_ctx)}")
                if _pdf_parts:
                    pdf.ln(2)
                    pdf.set_font("Helvetica", "", 8)
                    pdf.set_text_color(*_C["sub"])
                    pdf.multi_cell(W, 4.5, "  " + "   |   ".join(_pdf_parts))
                    pdf.set_text_color(0, 0, 0)

                if _tip:
                    _label_block("COACHING TIP (informational)", _tip, _C["bg_amber"], _C["ni"])

        # Video delivery section
        if va_result and not getattr(va_result, "error", None) and va_result.persons:
            _section_header("VIDEO DELIVERY ANALYSIS", 59, 130, 246)
            pdf.set_font("Helvetica", "", 9)
            pdf.set_text_color(*_C["sub"])
            pdf.cell(W, 5, f"Overall: {va_result.overall_score:.0f}/100  |  "
                           f"Duration: {int(va_result.duration_seconds//60)}m {int(va_result.duration_seconds%60)}s  |  "
                           f"Participants: {va_result.person_count}", new_x="LMARGIN", new_y="NEXT")
            pdf.set_text_color(0,0,0)
            pdf.ln(3)
            for pid, p in va_result.persons.items():
                sc_col = _SCORE_COL.get("Great" if p.overall >= 80 else "Good" if p.overall >= 65 else "Needs Improvement" if p.overall >= 50 else "Missed", _C["sub"])
                pdf.set_fill_color(*sc_col)
                pdf.set_text_color(*_C["white"])
                pdf.set_font("Helvetica", "B", 9)
                pdf.cell(W, 6, f"  {_s(p.role)}  —  {p.overall:.0f}/100", fill=True, new_x="LMARGIN", new_y="NEXT")
                pdf.set_text_color(0,0,0)
                pdf.set_font("Helvetica", "", 9)
                pdf.set_text_color(*_C["sub"])
                metrics = (f"Confidence: {p.confidence:.0f}   Composure: {p.composure:.0f}   "
                           f"Eye Contact: {p.eye_contact:.0f}   Engagement: {p.engagement:.0f}   Energy: {p.energy:.0f}")
                pdf.cell(W, 5, metrics, new_x="LMARGIN", new_y="NEXT")
                body_lang = (f"Open Posture: {p.open_body_pct:.0f}%   "
                             f"Arms Crossed: {p.arm_crossed_pct:.0f}%   "
                             f"Forward Lean: {p.forward_lean_pct:.0f}%   "
                             f"Mood: {_s(p.dominant_emotion)}")
                pdf.cell(W, 5, body_lang, new_x="LMARGIN", new_y="NEXT")
                if p.cultural:
                    pdf.cell(W, 5,
                             f"American Standard: {p.cultural.american_score:.0f}/100   "
                             f"Adaptation Score: {p.cultural.adaptation_score:.0f}/100",
                             new_x="LMARGIN", new_y="NEXT")
                    for t in p.cultural.american_tips[:2]:
                        pdf.set_font("Helvetica", "", 8)
                        pdf.set_x(pdf.l_margin + 4)
                        pdf.multi_cell(W - 4, 4, f"- {_s(t)}")
                pdf.set_text_color(0,0,0)
                pdf.ln(3)

        # Transcript
        if getattr(result, "speaker_dialogue", ""):
            _section_header("SPEAKER DIALOGUE", *_C["sub"])
            _body(result.speaker_dialogue, size=9)
        elif getattr(result, "clean_transcript", ""):
            _section_header("TRANSCRIPT", *_C["sub"])
            _body(result.clean_transcript, size=9)

    else:
        # ── Fallback: plain-text parsing (used for translated reports) ────────
        pdf.set_font("Helvetica", "", 10)
        for line in combined_text.splitlines():
            stripped = line.rstrip()
            if not stripped:
                pdf.ln(2); continue
            if set(stripped.strip()) <= {"=", "-", " "} and len(stripped.strip()) > 4:
                continue
            inner = stripped.strip()
            if (inner.isupper() and 3 < len(inner) < 70
                    and not set(inner) <= {"=", "-", " "}):
                _section_header(inner, *_C["header"])
            elif inner.startswith("Q") and ":" in inner[:8]:
                pdf.set_font("Helvetica", "B", 10)
                pdf.set_text_color(*_C["header"])
                pdf.multi_cell(W, 5, _s(inner))
                pdf.set_text_color(0,0,0)
                pdf.set_font("Helvetica", "", 10)
            elif any(inner.startswith(k) for k in ("WHAT WAS SAID","WHAT YOU COULD","COACHING TIP","Said    :","Ideal   :","Tip     :")):
                _body(inner, size=9, indent=4)
            else:
                pdf.set_font("Helvetica", "", 10)
                pdf.set_text_color(*_C["sub"])
                pdf.multi_cell(W, 5, _s(stripped))
                pdf.set_text_color(0,0,0)

    pdf.output(str(path))
    return str(path)


#   0  status_bar       1  summary_out      2  transcript_out   3  dialogue_out
#   4  profiles_out     5  analytics_out    6  combined_out
#   7  dl_transcript    8  dl_speakers      9  dl_report
#   10 dl_combined      11 dl_json          12 dl_pdf
#   13 download_accordion  14 log_out       15 eta_panel  16 result_state  17 dl_active
# ---------------------------------------------------------------------------

_NOCHANGE = (gr.update(),) * 29   # yield this to keep connection alive without changes

def _out(status=gr.update(), summary=gr.update(), transcript=gr.update(),
         dialogue=gr.update(), profiles=gr.update(), analytics=gr.update(),
         combined=gr.update(), interview=gr.update(),
         dl_t=gr.update(), dl_s=gr.update(), dl_r=gr.update(),
         dl_c=gr.update(), dl_j=gr.update(), dl_p=gr.update(),
         dl_srt=gr.update(), dl_vtt=gr.update(), dl_docx=gr.update(),
         dl_acc=gr.update(), log=gr.update(), eta=gr.update(),
         net=gr.update(), stats=gr.update(), rs=None,
         iv_scores=gr.update(), iv_tl=gr.update(), iv_sum=gr.update(),
         iv_vid=gr.update(), iv_prog=gr.update(),
         dl_wait=gr.update()):
    def _dl(v):
        if isinstance(v, gr.update().__class__): return v
        return gr.update(value=v, visible=bool(v)) if v else gr.update(visible=False)
    return (status, summary, transcript, dialogue, profiles, analytics,
            combined, interview,
            _dl(dl_t), _dl(dl_s), _dl(dl_r), _dl(dl_c), _dl(dl_j), _dl(dl_p),
            _dl(dl_srt), _dl(dl_vtt), _dl(dl_docx),
            dl_acc, log, eta, net, stats, rs,
            iv_scores, iv_tl, iv_sum, iv_vid, iv_prog,
            dl_wait)


# Pricing: (input $/MTok, output $/MTok)
_MODEL_PRICING: dict[str, tuple[float, float]] = {
    # Claude
    "claude-opus-4-8":              (15.00, 75.00),
    "claude-sonnet-4-6":            ( 3.00, 15.00),
    "claude-haiku-4-5-20251001":    ( 0.80,  4.00),
    "claude-3-5-sonnet-20241022":   ( 3.00, 15.00),
    "claude-3-5-haiku-20241022":    ( 0.80,  4.00),
    # OpenAI
    "gpt-4.1":                      ( 2.00,  8.00),
    "gpt-4.1-mini":                 ( 0.40,  1.60),
    "gpt-4.1-nano":                 ( 0.10,  0.40),
    "gpt-4o":                       ( 2.50, 10.00),
    "gpt-4o-mini":                  ( 0.15,  0.60),
    "o3":                           (10.00, 40.00),
    "o3-mini":                      ( 1.10,  4.40),
    "o4-mini":                      ( 1.10,  4.40),
    # Gemini
    "gemini-2.5-pro":               ( 1.25, 10.00),
    "gemini-2.5-flash":             ( 0.15,  0.60),
    "gemini-2.0-flash":             ( 0.075, 0.30),
    "gemini-2.0-flash-lite":        ( 0.019, 0.075),
}


def _stats_panel_html(elapsed: str = "", tok_in: int = 0, tok_out: int = 0,
                       dl_mb: float = 0, dl_speed: float = 0,
                       done: bool = False,
                       model_name: str = "", provider_type: str = "") -> str:
    color = "#22c55e" if done else "#3b82f6"
    icon  = "✅" if done else "⏳"

    cells = []
    if elapsed:
        cells.append(f'<div class="ta-stat-cell"><div class="ta-stat-val">{icon} {elapsed}</div>'
                     f'<div class="ta-stat-key">Duration</div></div>')

    if tok_in or tok_out:
        # Token counts
        cells.append(f'<div class="ta-stat-cell">'
                     f'<div class="ta-stat-val" style="color:{color};">'
                     f'{tok_in:,}<span style="opacity:0.6;font-size:0.78em;margin-left:2px;">in</span>'
                     f'&nbsp;/&nbsp;'
                     f'{tok_out:,}<span style="opacity:0.6;font-size:0.78em;margin-left:2px;">out</span>'
                     f'</div>'
                     f'<div class="ta-stat-key">🤖 Tokens (session)</div></div>')
        # Cost estimate
        pricing = _MODEL_PRICING.get(model_name)
        if pricing and (tok_in or tok_out):
            cost = tok_in / 1_000_000 * pricing[0] + tok_out / 1_000_000 * pricing[1]
            cost_str = f"${cost:.4f}" if cost < 0.01 else f"${cost:.3f}"
            mdl_short = (model_name.replace("claude-","").replace("-20241022","")
                         .replace("-20240229","").replace("-preview-05-06",""))
            cells.append(f'<div class="ta-stat-cell">'
                         f'<div class="ta-stat-val" style="color:#f59e0b;">{cost_str}</div>'
                         f'<div class="ta-stat-key">💰 Est. cost ({mdl_short})</div></div>')

    if dl_mb > 0:
        speed_str = f"{dl_speed:.1f} MB/s" if dl_speed > 0 else ""
        cells.append(f'<div class="ta-stat-cell">'
                     f'<div class="ta-stat-val" style="color:#7c3aed;">{dl_mb:.1f} MB'
                     f'{(" · " + speed_str) if speed_str else ""}</div>'
                     f'<div class="ta-stat-key">📡 Downloaded</div></div>')

    if not cells:
        return ""

    sep = '<div style="width:1px;background:var(--ta-card-border,#e2e8f0);align-self:stretch;margin:0 4px;"></div>'
    return (
        f'<div style="display:flex;align-items:center;gap:0;'
        f'background:var(--ta-card-bg,#f8fafc);border:1px solid var(--ta-card-border,#e2e8f0);'
        f'border-radius:10px;padding:10px 16px;margin-top:8px;flex-wrap:wrap;row-gap:8px;">'
        + sep.join(cells)
        + '</div>'
    )


_PDF_LANGUAGES = [
    "Same as source",
    "English", "Spanish", "French", "German", "Portuguese",
    "Italian", "Dutch", "Russian", "Chinese (Simplified)",
    "Japanese", "Korean", "Arabic", "Hindi", "Turkish",
]


def _translate_transcript(
    text: str, target_language: str, api_key: str,
    provider: str = "anthropic", model: str = None, base_url: str = None,
    use_gpu: bool = True,
) -> str:
    """Translate raw transcript text to target_language."""
    _model = model or ("claude-sonnet-4-6" if provider == "anthropic" else "gpt-4o")
    client = LLMClient(provider=provider, api_key=api_key, model=_model, base_url=base_url,
                       use_gpu=use_gpu)
    return client.chat(
        system="You are a professional translator. Translate text accurately and naturally.",
        user=(
            f"Translate the following transcript to {target_language}. "
            "Keep speaker labels (e.g. 'Speaker A:') unchanged. "
            "Preserve timestamps in [HH:MM:SS] format unchanged. "
            "Only return the translated text, nothing else.\n\n"
            + text
        ),
        max_tokens=8192,
    )


def _translate_combined_text(
    combined_text: str, target_language: str, api_key: str,
    provider: str = "anthropic", model: str = None, base_url: str = None,
    use_gpu: bool = True,
) -> str:
    """Translate the combined report text to target_language using the selected provider."""
    _model = model or ("claude-sonnet-4-6" if provider == "anthropic" else "gpt-4o")
    client = LLMClient(provider=provider, api_key=api_key, model=_model, base_url=base_url,
                       use_gpu=use_gpu)
    return client.chat(
        system="You are a professional translator.",
        user=(
            f"Translate the following transcript report to {target_language}. "
            "Preserve ALL formatting exactly — keep the section headers in ALL CAPS, "
            "keep divider lines (=== and ---), bullet characters (•, ☐), "
            "and timestamps in [HH:MM:SS] format unchanged. "
            "Only return the translated text, nothing else.\n\n"
            + combined_text
        ),
        max_tokens=8192,
    )


def generate_pdf_in_language(result_state, target_lang: str, api_key: str,
                              provider_name: str = "Claude (Anthropic)", model_name: str = None):
    """Generate (or re-generate) the PDF and DOCX in the chosen language."""
    if not result_state:
        return gr.update(), gr.update()
    stem     = result_state["stem"]
    combined = result_state["combined_text"]
    detected = result_state.get("detected_language", "")
    out_dir  = Path(result_state["out_dir"])

    if target_lang and target_lang != "Same as source":
        cfg = _PROVIDERS.get(provider_name, _PROVIDERS["Claude (Anthropic)"])
        combined = _translate_combined_text(
            combined, target_lang, api_key,
            provider=cfg["type"], model=model_name, base_url=cfg["base_url"],
        )
        suffix   = f"_{target_lang.replace(' ', '_')}"
    else:
        suffix = ""

    pdf_path  = out_dir / f"{stem}_report{suffix}.pdf"
    docx_path = out_dir / f"{stem}_report{suffix}.docx"

    _generate_pdf(f"{stem}  [{detected or target_lang}]", combined, pdf_path)

    # DOCX — reconstruct minimal TranscriptResult from stored state
    try:
        from transcript_agent import generate_docx, TranscriptResult
        _r = TranscriptResult(
            summary          = result_state.get("summary", ""),
            key_points       = result_state.get("key_points", []),
            action_items     = result_state.get("action_items", []),
            speaker_dialogue = result_state.get("speaker_dialogue", ""),
            clean_transcript = result_state.get("clean_transcript", ""),
            detected_language= detected,
        )
        if target_lang and target_lang != "Same as source":
            # For translated DOCX write the combined translated text as transcript
            _r.clean_transcript = combined
            _r.summary = ""
            _r.key_points = []
            _r.action_items = []
        generate_docx(_r, f"{stem}  [{detected or target_lang}]", str(docx_path))
        docx_out = str(docx_path) if docx_path.exists() else None
    except Exception:
        docx_out = None

    return str(pdf_path), docx_out


_PULSE_CSS = (
    '<style>'
    '@keyframes ta-pulse-ring{'
    '0%{box-shadow:0 0 0 0 rgba(37,99,235,0.45)}'
    '70%{box-shadow:0 0 0 8px rgba(37,99,235,0)}'
    '100%{box-shadow:0 0 0 0 rgba(37,99,235,0)}'
    '}'
    '</style>'
)


def _step_vars(state: str):
    """Return (bg, bdr, clr) CSS-variable strings for done/active/waiting."""
    if state == "done":   return "var(--ta-step-done-bg)", "var(--ta-step-done-bdr)", "var(--ta-step-done-clr)"
    if state == "active": return "var(--ta-step-act-bg)",  "var(--ta-step-act-bdr)",  "var(--ta-step-act-clr)"
    return                       "var(--ta-step-wait-bg)", "var(--ta-step-wait-bdr)", "var(--ta-step-wait-clr)"


def _stat_card(label: str, val: str,
               label_var: str = "--ta-stat-label",
               val_var:   str = "--ta-stat-val") -> str:
    """Stat tile used inside progress panels (elapsed, ETA, done-by…)."""
    _id = ' id="ta-live-elapsed"' if label == "Elapsed" else ''
    return (
        f'<div style="background:var(--ta-stat-bg);border-radius:8px;'
        f'padding:8px 14px;flex:1;min-width:90px;text-align:center;">'
        f'<div style="font-size:0.68em;font-weight:700;text-transform:uppercase;'
        f'letter-spacing:0.08em;color:var({label_var});">{label}</div>'
        f'<div style="font-size:1.3em;font-weight:800;color:var({val_var});'
        f'font-family:monospace;"{_id}>{val}</div></div>'
    )


def _step_tracker_html(stage: str, done: bool = False) -> str:
    # ── Map stage → which sub-steps are done/active/waiting ──────────────────
    # Phases: p1 = Transcription, p2 = AI Analysis, p3 = Complete
    # Sub-steps per phase:
    #   p1: Load (📁), Extract (🔊), Transcribe (🎤)
    #   p2: Analyze (🤖)
    #   p3: Done (✅)

    def _node(icon, state):
        bg, bdr, _ = _step_vars(state)
        anim  = "animation:ta-pulse-ring 1.6s ease-out infinite;" if state == "active" else ""
        inner = (f'<span style="font-size:0.85em;line-height:1;opacity:0.5;">{icon}</span>'
                 if state == "waiting"
                 else f'<span style="font-size:1.05em;line-height:1;">{icon}</span>')

        return (
            f'<div style="display:flex;flex-direction:column;align-items:center;gap:5px;">'
            f'<div style="width:42px;height:42px;border-radius:50%;background:{bg};'
            f'border:2px solid {bdr};display:flex;align-items:center;justify-content:center;'
            f'transition:all 0.35s;{anim}">'
            f'{inner}'
            f'</div>'
            f'</div>'
        )

    def _hline(color, width="28px"):
        return (
            f'<div style="width:{width};height:2px;background:{color};border-radius:2px;'
            f'flex-shrink:0;transition:background 0.4s;align-self:center;"></div>'
        )

    if done:
        p1_steps = ["done","done","done"]; p1_state = "done"; p1_hint = "Transcription complete"
        p2_state = "done"; p2_hint = "AI analysis complete"
        p3_state = "done"; p3_hint = "All done!"
        conn1 = conn2 = "var(--ta-conn-line-done)"
    elif stage in ("loading",):
        p1_steps = ["active","waiting","waiting"]; p1_state = "active"; p1_hint = "Loading file…"
        p2_state = "waiting"; p2_hint = "Waiting"; p3_state = "waiting"; p3_hint = ""
        conn1 = conn2 = "var(--ta-conn-line-wait)"
    elif stage == "extracting":
        p1_steps = ["done","active","waiting"]; p1_state = "active"; p1_hint = "Extracting audio…"
        p2_state = "waiting"; p2_hint = "Waiting"; p3_state = "waiting"; p3_hint = ""
        conn1 = conn2 = "var(--ta-conn-line-wait)"
    elif stage == "whisper":
        p1_steps = ["done","done","active"]; p1_state = "active"; p1_hint = "Converting speech…"
        p2_state = "waiting"; p2_hint = "Waiting"; p3_state = "waiting"; p3_hint = ""
        conn1 = conn2 = "var(--ta-conn-line-wait)"
    elif stage in ("claude","interview"):
        p1_steps = ["done","done","done"]; p1_state = "done"; p1_hint = "Transcription complete"
        hint = "Reading & analyzing…" if stage == "claude" else "Scoring interview responses…"
        p2_state = "active"; p2_hint = hint; p3_state = "waiting"; p3_hint = ""
        conn1 = "var(--ta-conn-line-done)"; conn2 = "var(--ta-conn-line-wait)"
    elif stage == "idle":
        p1_steps = ["waiting","waiting","waiting"]; p1_state = "waiting"; p1_hint = ""
        p2_state = "waiting"; p2_hint = ""; p3_state = "waiting"; p3_hint = ""
        conn1 = conn2 = "var(--ta-conn-line-wait)"
    else:
        p1_steps = ["active","waiting","waiting"]; p1_state = "active"; p1_hint = "Starting…"
        p2_state = "waiting"; p2_hint = ""; p3_state = "waiting"; p3_hint = ""
        conn1 = conn2 = "var(--ta-conn-line-wait)"

    def _phase_col(label, nodes_html, hint, state):
        _, _, clr = _step_vars(state)
        hint_html = (
            f'<div style="font-size:0.64em;font-weight:600;color:{clr};'
            f'text-align:center;margin-top:4px;min-height:14px;letter-spacing:0.01em;">{html.escape(hint)}</div>'
        ) if hint else '<div style="min-height:14px;"></div>'
        return (
            f'<div style="display:flex;flex-direction:column;align-items:center;'
            f'justify-content:center;padding:10px 8px 8px;">'
            f'<div style="font-size:0.6em;font-weight:800;text-transform:uppercase;'
            f'letter-spacing:0.1em;color:{clr};margin-bottom:8px;white-space:nowrap;">{label}</div>'
            f'<div style="display:flex;align-items:center;gap:0;">{nodes_html}</div>'
            f'{hint_html}'
            f'</div>'
        )

    # build phase 1 node row
    p1_icons = [("📁", p1_steps[0]), ("🔊", p1_steps[1]), ("🎤", p1_steps[2])]
    p1_nodes = ""
    for i, (ic, st) in enumerate(p1_icons):
        p1_nodes += _node(ic, st)
        if i < len(p1_icons) - 1:
            lc = "var(--ta-conn-line-done)" if p1_steps[i] == "done" and p1_steps[i + 1] == "done" else "var(--ta-conn-line-wait)"
            p1_nodes += _hline(lc, "20px")

    p1_col = _phase_col("Step 1 · Transcription", p1_nodes, p1_hint, p1_state)
    p2_col = _phase_col("Step 2 · AI Analysis",   _node("🤖", p2_state), p2_hint, p2_state)
    p3_col = _phase_col("Step 3 · Complete",       _node("✅", p3_state), p3_hint, p3_state)

    def _phase_box_wrap(col_html, state):
        bb, bd, _ = _step_vars(state)
        return (
            f'<div style="flex:1;background:{bb};border:1.5px solid {bd};border-radius:12px;'
            f'min-width:0;transition:all 0.35s;">{col_html}</div>'
        )

    def _big_connector(color):
        return (
            f'<div style="width:18px;height:2px;background:{color};border-radius:2px;'
            f'flex-shrink:0;align-self:center;transition:background 0.4s;"></div>'
        )

    return (
        _PULSE_CSS +
        f'<div style="display:flex;align-items:stretch;gap:0;margin-bottom:10px;">'
        f'{_phase_box_wrap(p1_col, p1_state)}'
        f'{_big_connector(conn1)}'
        f'{_phase_box_wrap(p2_col, p2_state)}'
        f'{_big_connector(conn2)}'
        f'{_phase_box_wrap(p3_col, p3_state)}'
        f'</div>'
    )


def _net_panel_html(direction: str, received: int, total: int,
                    speed_bps: float = 0, done: bool = False) -> str:
    if done:
        return gr.update()  # let JS keep rendering; never blank it out
    recv_mb  = received / 1_048_576
    speed_mb = speed_bps / 1_048_576
    pct      = min(100.0, received / total * 100) if total > 0 else 0
    eta_str  = ""
    if speed_bps > 0 and total > 0 and received < total:
        secs = max(0, (total - received) / speed_bps)
        eta_str = f"{int(secs//60)}m {int(secs%60):02d}s" if secs >= 60 else f"{int(secs)}s"

    icon  = "⬆️" if direction == "upload" else "⬇️"
    color = "#7c3aed" if direction == "upload" else "#2563eb"
    size_str = f"{recv_mb:.1f} MB"
    if total > 0:
        size_str += f" / {total/1_048_576:.1f} MB"

    bar = (
        f'<div style="height:6px;background:#e2e8f0;border-radius:4px;overflow:hidden;margin:6px 0;">'
        f'<div style="width:{pct:.0f}%;height:100%;background:{color};'
        f'border-radius:4px;transition:width 0.4s ease;"></div></div>'
    ) if total > 0 else (
        f'<style>@keyframes netslide{{0%{{left:-40%}}100%{{left:110%}}}}</style>'
        f'<div style="height:6px;background:#e2e8f0;border-radius:4px;overflow:hidden;'
        f'position:relative;margin:6px 0;">'
        f'<div style="position:absolute;width:40%;height:100%;background:{color};'
        f'border-radius:4px;animation:netslide 1.2s ease-in-out infinite;"></div></div>'
    )

    stats = f'<span style="color:{color};font-weight:700;">{speed_mb:.1f} MB/s</span>'
    if eta_str:
        stats += f' &nbsp;·&nbsp; <span style="color:#64748b;">ETA {eta_str}</span>'
    if total > 0:
        stats += f' &nbsp;·&nbsp; <span style="color:#64748b;">{pct:.0f}%</span>'

    return (
        f'<div style="background:var(--ta-card-bg);border:1px solid {color}33;'
        f'border-radius:10px;padding:10px 14px;margin-bottom:8px;">'
        f'<div style="display:flex;align-items:center;gap:8px;font-size:0.82em;">'
        f'<span>{icon}</span>'
        f'<span style="font-weight:700;color:var(--ta-card-text);">{direction.title()}</span>'
        f'<span style="color:#64748b;">{size_str}</span>'
        f'<span style="margin-left:auto;font-size:0.78em;">{stats}</span>'
        f'</div>'
        f'{bar}'
        f'</div>'
    )


def _eta_panel_html(stage: str, pct: float = None, eta_secs: int = None,
                    elapsed: str = "", done: bool = False, word_count: int = 0) -> str:
    import datetime as _dt

    _slide_css = (
        "<style>@keyframes pgslide{0%{left:-45%}100%{left:110%}}</style>"
    )

    # ── Done ──────────────────────────────────────────────────────────────────
    tracker = _step_tracker_html(stage, done)

    # ── Idle (before any job starts) ─────────────────────────────────────────
    if stage == "idle":
        return tracker + (
            '<div style="background:var(--ta-card-bg);border:2px solid var(--ta-card-border);'
            'border-radius:16px;padding:20px 24px;font-family:sans-serif;text-align:center;">'
            '<div style="color:var(--ta-card-sub);font-size:0.9em;">'
            'Upload a file or paste a URL to start</div>'
            '</div>'
        )

    if done:
        return tracker + (
            '<div class="ta-done-panel">'
            '<div style="font-size:3em;line-height:1;">&#10003;</div>'
            '<div class="ta-done-title">All Done!</div>'
            '<div style="display:flex;justify-content:center;gap:20px;margin-top:14px;flex-wrap:wrap;">'
            '<div class="ta-done-stat-box">'
            '<div class="ta-done-stat-label">Total Time</div>'
            f'<div class="ta-done-stat-val" style="font-family:monospace;">{elapsed}</div>'
            '</div>'
            '<div class="ta-done-stat-box">'
            '<div class="ta-done-stat-label">Progress</div>'
            '<div class="ta-done-stat-val">100%</div>'
            '</div>'
            '</div>'
            '</div>'
        )

    # ── Whisper with real % ───────────────────────────────────────────────────
    if stage == "whisper" and pct is not None and pct > 0:
        pct_int    = int(pct * 100)
        bar_fill   = f"{pct_int}%"
        eta_str    = _fmt_eta(eta_secs) if (eta_secs and eta_secs > 0) else "—"
        finish_str = ""
        if eta_secs and eta_secs > 0:
            finish_str = (_dt.datetime.now() + _dt.timedelta(seconds=eta_secs)).strftime("%I:%M %p").lstrip("0")

        return tracker + (
            '<div style="background:var(--ta-card-bg);border:2px solid var(--ta-step-act-bdr);'
            'border-radius:16px;padding:24px 28px;font-family:sans-serif;">'
            '<div style="font-size:0.72em;font-weight:700;text-transform:uppercase;'
            'letter-spacing:0.1em;color:var(--ta-step-act-clr);margin-bottom:12px;">'
            'Step 1 of 2 &nbsp;&mdash;&nbsp; Transcribing Audio</div>'
            '<div style="display:flex;align-items:flex-end;gap:20px;margin-bottom:14px;flex-wrap:wrap;">'
            '<div style="display:flex;align-items:flex-end;gap:4px;">'
            f'<div style="font-size:4.5em;font-weight:900;color:var(--ta-step-act-clr);'
            f'font-family:monospace;line-height:1;letter-spacing:-0.04em;">{pct_int}</div>'
            '<div style="font-size:2em;font-weight:700;color:var(--ta-step-act-bdr);'
            'margin-bottom:6px;">%</div></div>'
            '<div style="display:flex;flex-direction:column;justify-content:flex-end;padding-bottom:4px;">'
            '<div style="font-size:0.65em;font-weight:700;text-transform:uppercase;'
            'letter-spacing:0.08em;color:var(--ta-card-sub);">⏱ Time Left</div>'
            f'<div style="font-size:2.8em;font-weight:900;color:var(--ta-step-act-clr);'
            f'font-family:monospace;line-height:1;letter-spacing:-0.03em;">{eta_str}</div>'
            '</div></div>'
            '<div style="background:var(--ta-step-wait-bg);border-radius:8px;height:14px;'
            'overflow:hidden;margin-bottom:12px;">'
            f'<div style="width:{bar_fill};height:100%;'
            'background:linear-gradient(90deg,var(--ta-step-act-bdr),var(--ta-step-act-clr));'
            'border-radius:8px;transition:width 0.5s ease;"></div></div>'
            '<div style="display:flex;gap:12px;flex-wrap:wrap;">'
            + (_stat_card("Done By", finish_str, "--ta-step-done-clr", "--ta-step-done-clr") if finish_str else "")
            + _stat_card("Elapsed", elapsed, "--ta-card-sub", "--ta-card-val") +
            '</div></div>'
        )

    # ── Other stages (loading / extracting / claude / whisper indeterminate) ──
    stage_cfg = {
        "loading":    ("var(--ta-step-act-bdr)",  "Starting up…",              "Step 0 of 3", "var(--ta-step-act-clr)"),
        "extracting": ("var(--ta-step-done-bdr)", "Extracting audio…",         "Step 1 of 3", "var(--ta-step-done-clr)"),
        "whisper":    ("var(--ta-step-act-bdr)",  "Transcribing audio…",       "Step 1 of 3", "var(--ta-step-act-clr)"),
        "stt_cloud":  ("var(--ta-step-act-bdr)",  "Uploading & transcribing…", "Step 1 of 3", "var(--ta-step-act-clr)"),
        "claude":     ("#a855f7",                 "Analyzing with AI…",        "Step 2 of 3", "#c4b5fd"),
    }
    color, label, step, text_clr = stage_cfg.get(
        stage, ("var(--ta-card-border)", "Processing…", "", "var(--ta-card-sub)")
    )

    # ── Claude stage: elapsed-based simulated percentage ──────────────────────
    if stage == "claude":
        import math as _math, re as _re
        _m = _re.match(r'(?:(\d+)m\s*)?(\d+)s', elapsed or "")
        _elapsed_s = (int(_m.group(1) or 0) * 60 + int(_m.group(2) or 0)) if _m else 0
        # Total estimated AI time: scale by word count if known, else default 90s
        _total_ai = max(90, int(word_count * 0.14)) if word_count > 0 else 90
        # Asymptotic curve: grows fast → slows → caps at 92% until done
        ai_pct   = min(92, int(100 * (1 - _math.exp(-_elapsed_s / (_total_ai * 0.4))))) if _elapsed_s > 0 else 5
        # Estimate remaining from asymptotic curve
        _est_rem = max(0, int(_total_ai - _elapsed_s))
        eta_str  = _fmt_eta(_est_rem) if _est_rem > 3 else "Almost done…"
        # Sub-label
        if word_count > 0:
            _sub_label = f"🤖 Analyzing {word_count:,}-word transcript — writing full report…"
        else:
            _sub_label = "🤖 Reading transcript and writing your report…"

        return tracker + (
            '<div style="background:var(--ta-card-bg);border:2px solid #a855f7;'
            'border-radius:16px;padding:24px 28px;font-family:sans-serif;">'
            '<div style="font-size:0.72em;font-weight:700;text-transform:uppercase;'
            'letter-spacing:0.1em;color:#c4b5fd;margin-bottom:12px;">'
            f'Step 2 of 3 &nbsp;&mdash;&nbsp; Analyzing with AI</div>'
            '<div style="display:flex;align-items:flex-end;gap:20px;margin-bottom:14px;flex-wrap:wrap;">'
            '<div style="display:flex;align-items:flex-end;gap:4px;">'
            f'<div style="font-size:4.5em;font-weight:900;color:#c4b5fd;'
            f'font-family:monospace;line-height:1;letter-spacing:-0.04em;">{ai_pct}</div>'
            '<div style="font-size:2em;font-weight:700;color:#a855f7;margin-bottom:6px;">%</div>'
            '</div>'
            '<div style="display:flex;flex-direction:column;justify-content:flex-end;padding-bottom:4px;">'
            '<div style="font-size:0.65em;font-weight:700;text-transform:uppercase;'
            'letter-spacing:0.08em;color:#c4b5fd;">⏱ Time Left</div>'
            f'<div style="font-size:2.8em;font-weight:900;color:#c4b5fd;'
            f'font-family:monospace;line-height:1;letter-spacing:-0.03em;">{eta_str}</div>'
            '</div></div>'
            '<div style="background:var(--ta-step-wait-bg);border-radius:8px;height:14px;'
            'overflow:hidden;margin-bottom:12px;">'
            f'<div style="width:{ai_pct}%;height:100%;'
            'background:linear-gradient(90deg,#a855f7,#c4b5fd);'
            'border-radius:8px;transition:width 0.6s ease;"></div></div>'
            '<div style="display:flex;gap:10px;flex-wrap:wrap;">'
            + _stat_card("Elapsed", elapsed) +
            '</div>'
            f'<div style="font-size:0.82em;color:#c4b5fd;margin-top:10px;">'
            f'{_sub_label}</div>'
            '</div>'
        )

    overlay_pct = ""
    est_time_stat = ""
    if stage in ("loading", "extracting"):
        overlay_pct = (
            '<div style="display:flex;align-items:flex-end;gap:4px;margin-bottom:14px;">'
            f'<div style="font-size:4.5em;font-weight:900;color:{text_clr};'
            f'font-family:monospace;line-height:1;">—</div></div>'
        )
        est_label = "< 5s" if stage == "loading" else "10–60s"
        est_time_stat = _stat_card("Est. Time", est_label, "--ta-card-sub", "--ta-card-val")

    return tracker + (
        f'<div style="background:var(--ta-card-bg);border:2px solid {color};'
        f'border-radius:16px;padding:24px 28px;font-family:sans-serif;">'
        f'<div style="font-size:0.72em;font-weight:700;text-transform:uppercase;'
        f'letter-spacing:0.1em;color:{text_clr};margin-bottom:12px;">'
        f'{step} &nbsp;&mdash;&nbsp; {label}</div>'
        f'{overlay_pct}'
        f'{_slide_css}'
        f'<div style="background:var(--ta-step-wait-bg);border-radius:8px;height:14px;'
        f'overflow:hidden;position:relative;margin-bottom:10px;">'
        f'<div style="position:absolute;width:40%;height:100%;background:{color};'
        f'border-radius:8px;opacity:0.85;animation:pgslide 1.6s ease-in-out infinite;"></div>'
        f'</div>'
        f'<div style="display:flex;gap:8px;">'
        + _stat_card("Elapsed", elapsed, "--ta-card-sub", "--ta-card-val")
        + est_time_stat
        + '</div></div>'
    )


def _friendly_api_error(err: str, provider_name: str = "", model_name: str = "") -> str:
    """Convert a raw API error string into a short, human-readable message."""
    import re as _re
    e = err.lower()
    # connection refused / unreachable
    if ("connection error" in e or "connection refused" in e or "connect call failed" in e
            or "econnrefused" in e or "name or service not known" in e
            or ("connection" in e and "error" in e)):
        if provider_name == "Ollama (Local)":
            return (
                "Cannot reach Ollama. Fix: open a terminal and run  ollama serve  "
                "— keep that window open while using the app."
            )
        return (
            f"Cannot connect to {provider_name or 'the API'}. "
            "Check your internet connection, firewall, or VPN, then try again."
        )
    # 529 / overloaded
    if "overloaded" in e or "529" in err or "at capacity" in e:
        return (
            "Claude is temporarily at capacity (overloaded). "
            "The app will retry automatically — or wait a moment and try again."
        )
    # 429 / quota
    if "429" in err or "resource_exhausted" in e or "quota" in e:
        delay_m = _re.search(r"retry in (\d+(?:\.\d+)?)s", err, _re.IGNORECASE)
        retry = f" Retry in {float(delay_m.group(1)):.0f}s." if delay_m else ""
        if "perday" in err or ("limit: 0" in err):
            alt = " Switch to a different model (e.g. gemini-2.5-flash)." if "gemini" in model_name else " Try a different model or provider."
            return f"Daily quota exhausted for {model_name or 'this model'}.{alt}"
        return f"Rate limit hit for {model_name or 'this model'}.{retry} Wait a moment and try again."
    # 401 / auth
    if "401" in err or "authentication" in e or "api_key" in e or "credentials" in e:
        return f"API key rejected by {provider_name or 'the provider'}. Check that you pasted the correct key."
    # 404 / model not found
    if "404" in err or "not found" in e:
        if "ollama" in provider_name.lower() or "local" in provider_name.lower():
            return (f"Model '{model_name}' is not downloaded yet. "
                    f"Run this in your terminal first:  ollama pull {model_name}")
        return f"Model '{model_name}' not found or no longer available. Try a different model."
    # context length
    if "context" in e and ("length" in e or "window" in e or "limit" in e):
        return f"Input too long for {model_name or 'this model'}. Try a shorter recording or a model with a larger context window."
    # raw error is short enough — show as-is; otherwise truncate
    if len(err) <= 200:
        return err
    # For long raw errors (like full JSON blobs), show just the first sentence
    first = _re.split(r"[.\n]", err)[0].strip()
    return first[:200] if first else err[:200]


def _fmt_action_item(a) -> str:
    """Format an action item — handles both plain strings and structured dicts."""
    if isinstance(a, dict):
        action   = a.get("action", a.get("item", str(a)))
        owner    = a.get("owner", a.get("assigned_to", ""))
        timeline = a.get("timeline", a.get("due", ""))
        parts = [action]
        if owner:    parts.append(f"**Owner:** {owner}")
        if timeline: parts.append(f"**Timeline:** {timeline}")
        return " · ".join(parts)
    return str(a)


def _fmt_action_item_md(a) -> str:
    """Format an action item as a Markdown checkbox line."""
    if isinstance(a, dict):
        action   = a.get("action", a.get("item", str(a)))
        owner    = a.get("owner", a.get("assigned_to", ""))
        timeline = a.get("timeline", a.get("due", ""))
        line = f"- [ ] {action}"
        meta = []
        if owner:    meta.append(f"*{owner}*")
        if timeline: meta.append(f"*{timeline}*")
        if meta:     line += "  \n  " + " · ".join(meta)
        return line
    return f"- [ ] {a}"


def _err(msg: str) -> tuple:
    """Yield this tuple to display an inline error card instead of a popup."""
    html = (
        '<div class="ta-err-card">'
        '<div style="font-size:1.8em;line-height:1;flex-shrink:0;">❌</div>'
        '<div>'
        '<div class="ta-err-title">Something went wrong</div>'
        f'<div class="ta-err-text">{msg}</div>'
        '</div>'
        '</div>'
    )
    return _out(status=html, eta="")


def _build_interview_html(ia: dict) -> str:
    """Render interview analysis dict → HTML string for the coaching tab."""
    if not ia:
        return '<p style="color:#94a3b8;">No interview analysis available for this file type.</p>'
    if ia.get("parse_error"):
        return f'<pre style="font-size:0.8em;overflow:auto;">{ia.get("raw","")}</pre>'

    qs = ia.get("questions", [])
    _SCORE_COLOR = {
        "Great": "#22c55e", "Good": "#3b82f6",
        "Needs Improvement": "#f59e0b", "Missed": "#ef4444",
    }
    _score_val = ia.get("overall_score", "—")
    _verdict   = ia.get("overall_verdict", "")
    try:
        _score_num = int(_score_val)
        _score_bg  = ("#166534" if _score_num >= 8 else
                      "#1d4ed8" if _score_num >= 6 else
                      "#92400e" if _score_num >= 4 else "#991b1b")
    except (ValueError, TypeError):
        _score_bg = "#1e293b"

    _adv_pct    = ia.get("advance_likelihood", "")
    _adv_reason = ia.get("advance_reasoning", "")
    try:
        _adv_num = int(str(_adv_pct).strip().rstrip("%"))
    except (ValueError, TypeError):
        _adv_num = None
    if _adv_num is not None:
        _adv_color = ("#166534" if _adv_num >= 70 else "#1d4ed8" if _adv_num >= 45 else "#991b1b")
        _adv_label = ("Likely to advance" if _adv_num >= 70 else
                      "Borderline"        if _adv_num >= 45 else "Unlikely to advance")
        _adv_banner = (
            f'<div style="background:{_adv_color};border-radius:14px;'
            f'padding:16px 22px;margin-bottom:14px;display:flex;align-items:center;gap:16px;">'
            f'<div style="text-align:center;background:rgba(255,255,255,0.18);'
            f'border-radius:10px;padding:8px 16px;min-width:80px;">'
            f'<div style="font-size:2.2em;font-weight:900;color:#fff;line-height:1;">{_adv_num}%</div>'
            f'<div style="font-size:0.7em;font-weight:700;color:rgba(255,255,255,0.75);'
            f'text-transform:uppercase;letter-spacing:0.08em;">likelihood</div></div>'
            f'<div><div style="font-size:0.72em;font-weight:700;text-transform:uppercase;'
            f'letter-spacing:0.1em;color:rgba(255,255,255,0.7);margin-bottom:4px;">🚀 Advancing to Next Round</div>'
            f'<div style="font-size:1.15em;font-weight:800;color:#fff;">{_adv_label}</div>'
            + (f'<div style="font-size:0.82em;color:rgba(255,255,255,0.8);margin-top:4px;">{_adv_reason}</div>'
               if _adv_reason else '')
            + f'</div></div>'
        )
    else:
        _adv_banner = ""

    html = (
        f'<div style="padding:4px 0;">'
        + _adv_banner
        + f'<div style="background:{_score_bg};border-radius:16px;padding:20px 24px;margin-bottom:20px;'
        f'display:flex;align-items:center;gap:20px;">'
        f'<div style="background:rgba(255,255,255,0.15);border-radius:12px;padding:10px 18px;'
        f'text-align:center;min-width:80px;">'
        f'<div style="font-size:2.6em;font-weight:900;color:#fff;line-height:1;">{_score_val}</div>'
        f'<div style="font-size:0.75em;font-weight:700;color:rgba(255,255,255,0.75);'
        f'letter-spacing:0.08em;text-transform:uppercase;">out of 10</div></div>'
        f'<div><div style="font-size:0.72em;font-weight:700;text-transform:uppercase;'
        f'letter-spacing:0.1em;color:rgba(255,255,255,0.7);margin-bottom:4px;">🎯 Overall Score</div>'
        f'<div style="font-size:1.3em;font-weight:800;color:#fff;">{_verdict}</div></div></div>'
    )
    _DEFLECT_STYLE = {
        "partial": ("⚠️ Deflected",      "#f59e0b", "#fffbeb", "#fde68a"),
        "full":    ("🚫 Did Not Answer", "#ef4444", "#fef2f2", "#fecaca"),
    }
    for q in qs:
        sc           = q.get("score", "")
        col          = _SCORE_COLOR.get(sc, "#6b7280")
        answer_said  = q.get("answer_said") or q.get("answer_summary", "")
        model_answer = q.get("model_answer") or q.get("ideal_answer", "")
        coaching_tip = q.get("coaching_tip", "")
        deflection   = (q.get("deflection") or "none").lower().strip()
        defl_note    = q.get("deflection_note", "")

        defl_html = ""
        if deflection in _DEFLECT_STYLE:
            dlbl, _dcol, _dbg, _dbdr = _DEFLECT_STYLE[deflection]
            cls = "partial" if deflection == "partial" else "full"
            defl_html = (
                f'<div class="ta-defl-{cls}">'
                f'<span class="ta-defl-label-{cls}">{dlbl}</span>'
                + (f'<span class="ta-defl-note">{defl_note}</span>' if defl_note else '')
                + '</div>'
            )

        html += (
            f'<div class="ta-q-card" style="border:2px solid {col};">'
            f'<div style="display:flex;gap:10px;align-items:flex-start;margin-bottom:10px;">'
            f'<span style="background:{col};color:#fff;font-size:0.78em;font-weight:800;'
            f'padding:3px 10px;border-radius:20px;white-space:nowrap;flex-shrink:0;margin-top:2px;">'
            f'Q{q.get("id","")}</span>'
            f'<div class="ta-q-title">{q.get("question","")}</div></div>'
            f'<div style="display:flex;gap:8px;margin-bottom:12px;flex-wrap:wrap;align-items:center;">'
            f'<span style="background:{col};color:#fff;font-size:0.8em;font-weight:800;'
            f'padding:4px 14px;border-radius:20px;">{sc}</span>'
            f'<span class="ta-q-reason">{q.get("score_reason","")}</span></div>'
            + defl_html
            + f'<div class="ta-q-said">'
            f'<div class="ta-q-said-label">📝 What they said</div>'
            f'<p class="ta-q-said-text">{answer_said}</p></div>'
            f'<div class="ta-q-ideal">'
            f'<div class="ta-q-ideal-label">💬 What you could have said</div>'
            f'<p class="ta-q-ideal-text">{model_answer}</p></div>'
            + (f'<div class="ta-q-tip">'
               f'<div class="ta-q-tip-label">🏋️ Coaching Tip</div>'
               f'<p class="ta-q-tip-text">{coaching_tip}</p></div>'
               if coaching_tip else '')
            + '</div>'
        )
    if ia.get("advance_likelihood"):
        html += (
            f'<div class="ta-q-deep">'
            f'<div style="font-weight:700;margin-bottom:4px;">🔬 Deep Analysis</div>'
            f'<div>Deflection rate: <b>{ia.get("deflection_rate","—")}%</b> · '
            f'Advance likelihood: <b>{ia.get("advance_likelihood","—")}%</b></div>'
            f'<div style="font-size:0.82em;margin-top:4px;color:#475569;">'
            f'{ia.get("advance_reasoning","")}</div></div>'
        )

    # ── Coding Challenges ────────────────────────────────────────────────────
    _coding = ia.get("coding_challenges", [])
    if _coding:
        _cs = ia.get("coding_score")
        _cs_disp = f"{_cs} / 10" if _cs is not None and str(_cs) not in ("", "null") else None
        try:
            _cs_num = int(str(_cs)) if _cs_disp else None
            _cs_bg  = ("#166534" if _cs_num >= 8 else "#1d4ed8" if _cs_num >= 6
                       else "#92400e" if _cs_num >= 4 else "#991b1b")
        except Exception:
            _cs_num, _cs_bg = None, "#1e293b"

        html += '<div style="margin-top:24px;">'
        if _cs_disp:
            html += (
                f'<div style="background:{_cs_bg};border-radius:14px;padding:16px 22px;'
                f'margin-bottom:16px;display:flex;align-items:center;gap:16px;">'
                f'<div style="background:rgba(255,255,255,0.18);border-radius:10px;'
                f'padding:8px 16px;text-align:center;min-width:80px;">'
                f'<div style="font-size:2.2em;font-weight:900;color:#fff;line-height:1;">{_cs}</div>'
                f'<div style="font-size:0.7em;font-weight:700;color:rgba(255,255,255,0.75);'
                f'text-transform:uppercase;letter-spacing:.08em;">out of 10</div></div>'
                f'<div style="font-size:0.72em;font-weight:700;text-transform:uppercase;'
                f'letter-spacing:.1em;color:rgba(255,255,255,0.7);">💻 Coding Score</div>'
                f'</div>'
            )

        html += (
            '<div style="font-size:0.72em;font-weight:700;text-transform:uppercase;'
            'letter-spacing:.1em;color:#64748b;margin-bottom:12px;">💻 Coding Challenges</div>'
        )

        # Show detected role badge
        _role = ia.get("detected_role", "")
        if _role and _role.lower() not in ("", "unknown"):
            html += (
                f'<div style="display:inline-flex;align-items:center;gap:8px;'
                f'background:#0f172a;border-radius:20px;padding:5px 14px;margin-bottom:12px;">'
                f'<span style="font-size:0.75em;color:#94a3b8;">🎯 Role detected:</span>'
                f'<span style="font-size:0.82em;font-weight:700;color:#38bdf8;">{_role}</span>'
                f'</div>'
            )

        _CC_COL = {"Great":"#22c55e","Good":"#3b82f6","Needs Improvement":"#f59e0b","Missed":"#ef4444"}
        for _ch in _coding:
            _sc      = _ch.get("score","")
            _col     = _CC_COL.get(_sc,"#6b7280")
            _prob    = _ch.get("problem","")
            _ans     = _ch.get("candidate_answer","")
            _cappr   = _ch.get("candidate_approach","")
            _lang_req= _ch.get("language_requested","")
            _lang_used=_ch.get("language_used","")
            _libs    = _ch.get("libraries_used","")
            _role_ctx= _ch.get("role_context","")
            _opt     = _ch.get("optimal_solution","").replace("<","&lt;").replace(">","&gt;")
            _oappr   = _ch.get("optimal_approach","")
            _tc      = _ch.get("time_complexity","")
            _spc     = _ch.get("space_complexity","")
            _tip     = _ch.get("coaching_tip","")
            _reason  = _ch.get("score_reason","")

            # Build language/library badges
            _lang_badges = ""
            if _lang_req and _lang_req.lower() not in ("", "not specified"):
                _lang_badges += (
                    f'<span style="background:#1e3a5f;color:#60a5fa;font-size:0.75em;font-weight:700;'
                    f'padding:3px 10px;border-radius:20px;border:1px solid #3b82f6;">Asked in: {_lang_req}</span> '
                )
            if _lang_used and _lang_used.lower() not in ("", "not specified"):
                _lang_badges += (
                    f'<span style="background:#052e16;color:#86efac;font-size:0.75em;font-weight:700;'
                    f'padding:3px 10px;border-radius:20px;border:1px solid #22c55e;">{_lang_used}</span> '
                )
            if _libs and _libs.lower() not in ("", "none", "not specified"):
                for _lib in _libs.split(","):
                    _lib = _lib.strip()
                    if _lib:
                        _lang_badges += (
                            f'<span style="background:#1c1917;color:#fdba74;font-size:0.75em;font-weight:700;'
                            f'padding:3px 10px;border-radius:20px;border:1px solid #f97316;">{_lib}</span> '
                        )

            # Optimal solution header — shows language
            _sol_header = "✅ Optimal Solution"
            if _lang_used:
                _sol_header += f" — {_lang_used}"
            if _libs and _libs.lower() not in ("", "none", "not specified"):
                _sol_header += f" ({_libs})"

            html += (
                f'<div class="ta-q-card" style="border:2px solid {_col};margin-bottom:14px;">'
                f'<div style="display:flex;gap:10px;align-items:flex-start;margin-bottom:8px;">'
                f'<span style="background:{_col};color:#fff;font-size:0.78em;font-weight:800;'
                f'padding:3px 10px;border-radius:20px;white-space:nowrap;flex-shrink:0;margin-top:2px;">💻</span>'
                f'<div class="ta-q-title">{_prob}</div></div>'
                + (f'<div style="display:flex;gap:6px;flex-wrap:wrap;margin-bottom:10px;">{_lang_badges}</div>'
                   if _lang_badges else '')
                + f'<div style="display:flex;gap:8px;margin-bottom:12px;flex-wrap:wrap;align-items:center;">'
                f'<span style="background:{_col};color:#fff;font-size:0.8em;font-weight:800;'
                f'padding:4px 14px;border-radius:20px;">{_sc}</span>'
                f'<span class="ta-q-reason">{_reason}</span></div>'
            )
            if _cappr:
                html += (
                    f'<div class="ta-q-said">'
                    f'<div class="ta-q-said-label">🧠 Candidate\'s Approach</div>'
                    f'<p class="ta-q-said-text">{_cappr}</p></div>'
                )
            if _ans:
                html += (
                    f'<div class="ta-q-said">'
                    f'<div class="ta-q-said-label">📝 What they said / did</div>'
                    f'<p class="ta-q-said-text">{_ans}</p></div>'
                )
            if _opt:
                html += (
                    f'<div style="margin:10px 0;">'
                    f'<div style="font-size:0.74em;font-weight:700;text-transform:uppercase;'
                    f'letter-spacing:.08em;color:#22d3ee;background:#0f172a;'
                    f'padding:6px 14px;border-radius:8px 8px 0 0;">{_sol_header}</div>'
                    f'<pre style="background:#0f172a;color:#e2e8f0;font-size:0.8em;'
                    f'padding:14px 16px;margin:0;border-radius:0 0 8px 8px;'
                    f'overflow-x:auto;white-space:pre-wrap;word-break:break-word;">{_opt}</pre>'
                    f'</div>'
                )
            _meta_parts = []
            if _oappr:    _meta_parts.append(f"<b>Approach:</b> {_oappr}")
            if _tc:       _meta_parts.append(f"<b>Time:</b> {_tc}")
            if _spc:      _meta_parts.append(f"<b>Space:</b> {_spc}")
            if _role_ctx: _meta_parts.append(f"<b>Role context:</b> {_role_ctx}")
            if _meta_parts:
                html += (
                    f'<div style="font-size:0.82em;color:#475569;margin:6px 0;'
                    f'padding:8px 12px;background:#f1f5f9;border-radius:6px;">'
                    + "<br>".join(_meta_parts) + "</div>"
                )
            if _tip:
                html += (
                    f'<div class="ta-q-tip">'
                    f'<div class="ta-q-tip-label">🏋️ Coaching Tip '
                    f'<span style="font-weight:400;font-size:0.85em;opacity:0.7;">(informational — does not affect score)</span></div>'
                    f'<p class="ta-q-tip-text">{_tip}</p></div>'
                )
            html += '</div>'

        html += '</div>'

    html += '</div>'
    return html


def _build_unified_interview_html(ia: dict, va_result) -> str:
    """Merge interview coaching HTML with video delivery analysis HTML."""
    coaching = _build_interview_html(ia)

    if not _HAS_VIDEO_ANALYZER or not va_result or va_result.error or not va_result.persons:
        return coaching

    # ── Delivery section header ───────────────────────────────────────────────
    delivery = (
        '<div style="margin:32px 0 20px;border-top:3px solid #e2e8f0;padding-top:24px;">'
        '<div style="display:flex;align-items:center;gap:10px;margin-bottom:18px;">'
        '<span style="font-size:1.2em;">🎥</span>'
        '<span style="font-size:1.1em;font-weight:800;color:#1e293b;">Delivery Analysis</span>'
        '<span style="font-size:0.75em;color:#94a3b8;margin-left:4px;">'
        f'· {int(va_result.duration_seconds//60)}m {int(va_result.duration_seconds%60)}s · '
        f'{va_result.person_count} participant{"s" if va_result.person_count!=1 else ""}'
        '</span></div>'
    )

    # ── Score cards ───────────────────────────────────────────────────────────
    delivery += _video_analyzer.render_score_cards_html(va_result, ia=ia)

    # ── Emotion timeline (inline Plotly) ──────────────────────────────────────
    try:
        fig = _video_analyzer.render_timeline_figure(va_result)
        if fig:
            tl_html = fig.to_html(
                full_html=False,
                include_plotlyjs="cdn",
                config={"displayModeBar": False},
            )
            delivery += (
                '<div style="margin-top:16px;border:1px solid #e2e8f0;border-radius:14px;'
                'padding:16px;background:var(--ta-card-bg,#f8fafc);">'
                '<div style="font-weight:800;color:#475569;margin-bottom:8px;font-size:0.9em;">'
                'Emotion Timeline</div>'
                + tl_html
                + '</div>'
            )
    except Exception:
        pass

    delivery += '</div>'  # close delivery section
    return coaching + delivery


def process_file(
    uploaded_file,
    path_input,
    path_input_2,
    panel_mode,
    num_speakers,
    stt_engine,
    stt_api_key,
    stt_model,
    interview_mode,
    interview_deep,
    candidate_profile,
    language_input,
    language_variant,
    transcript_output_lang,
    report_style,
    inc_summary,
    inc_key_points,
    inc_action_items,
    inc_transcript,
    inc_profiles,
    inc_analytics,
    user_api_key,
    provider_name,
    model_name,
    transcription_only=False,
    image_files=None,
    iv_person_count=2,
    iv_role_0="Candidate",
    iv_role_1="Interviewer 1",
    iv_role_2="Interviewer 2",
    iv_role_3="Interviewer 3",
    use_gpu=True,
):
    yield _NOCHANGE   # immediate tick — clears the loading indicator right away
    _va_res = None  # video analysis result — populated later if video + Interview Mode
    # Route unified model dropdown to the correct STT parameter
    if stt_engine == "whisper_local":
        _whisper_model = stt_model or "base"
        stt_model = None
    else:
        _whisper_model = "base"

    # ── validation (all errors shown inline, no popup) ────────────────────────
    api_key = (user_api_key or "").strip()
    provider_cfg = _PROVIDERS.get(provider_name, _PROVIDERS["Claude (Anthropic)"])
    # API key not needed when transcription only — no AI call is made
    if not api_key and provider_name != "Ollama (Local)" and not transcription_only:
        yield _err(f"Add your {provider_name} API key at the top to get started.")
        return
    provider_type = provider_cfg["type"]
    base_url      = provider_cfg["base_url"]

    # ── Auto-pull Ollama model if not already downloaded ─────────────────────
    if provider_name == "Ollama (Local)" and model_name:
        import subprocess as _sp, urllib.request as _ur, json as _json
        def _ollama_has_model(m):
            try:
                r = _ur.urlopen("http://localhost:11434/api/tags", timeout=3)
                tags = _json.loads(r.read())
                return any(t.get("name","").split(":")[0] == m.split(":")[0]
                           for t in tags.get("models", []))
            except Exception:
                return False
        if not _ollama_has_model(model_name):
            yield _out(log=_add_log(f"⬇️ Downloading {model_name} via Ollama — this may take a few minutes…", "info"),
                       status=_status_compact("⬇️", f"Pulling {model_name}…"))
            try:
                _sp.run(["ollama", "pull", model_name], check=True, timeout=1800)
                yield _out(log=_add_log(f"✅ {model_name} downloaded successfully", "done"))
            except Exception as _pe:
                yield _err(f"Failed to download {model_name}: {_pe}\nMake sure Ollama is running: ollama serve")
                return

    # prefer pasted path/URL (no upload wait) over drag-and-drop
    pasted = (path_input or "").strip().strip('"').strip("'")
    if pasted:
        uploaded_file = pasted
    _file2 = (path_input_2 or "").strip().strip('"').strip("'")

    if not uploaded_file:
        yield _err("Drop a file, paste a file path, or paste a URL above to get started.")
        return

    # ── Log helpers (must be defined before download section uses them) ────────
    start_time    = time.time()
    log_entries   = []
    _total_dl_mb  = 0.0   # must be initialised before the URL-download section
    _peak_dl_speed = 0.0

    def _ts():
        import datetime as _dt
        return _dt.datetime.now().strftime("%I:%M %p").lstrip("0")

    def _elapsed():
        secs = int(time.time() - start_time)
        m, s = divmod(secs, 60)
        return f"{m}m {s:02d}s" if m else f"{s}s"

    _KIND_COLORS = {
        'header':   ('#f8fafc', True),
        'info':     ('#94a3b8', False),
        'progress': ('#86efac', False),
        'warn':     ('#fbbf24', False),
        'error':    ('#f87171', False),
        'done':     ('#4ade80', True),
        'download': ('#22d3ee', False),
        'ai':       ('#c4b5fd', False),
    }

    def _render_log():
        parts = []
        for kind, ts, text in log_entries:
            color, bold = _KIND_COLORS.get(kind, ('#94a3b8', False))
            weight = 'font-weight:700;' if bold else ''
            if kind == 'header':
                parts.append(
                    f'<div style="background:var(--ta-accent-lt,#dbeafe);color:var(--ta-text,#0d1b2e);font-weight:700;'
                    f'margin:10px -16px 6px;padding:5px 16px;letter-spacing:0.07em;'
                    f'font-size:0.85em;border-left:3px solid var(--ta-accent,#2563eb);">{text}</div>'
                )
            elif kind == 'progress':
                # text-only line in log — the ETA panel owns the visual bar
                parts.append(
                    f'<div><span style="color:var(--ta-log-ts,#94a3b8);">[{ts}]</span> '
                    f'<span style="color:{color};{weight}">{text}</span></div>'
                )
            else:
                parts.append(
                    f'<div><span style="color:var(--ta-log-ts,#94a3b8);">[{ts}]</span> '
                    f'<span style="color:{color};{weight}">{text}</span></div>'
                )
        scroll = '<div id="ta-log-end"></div><script>document.getElementById("ta-log-end")?.scrollIntoView();</script>'
        inner = "".join(parts) + scroll if parts else '<span style="color:var(--ta-log-ts,#94a3b8);">Starting…</span>'
        return (
            '<div id="ta-log-wrap" style="background:var(--ta-log-bg,#f8fafc);border:1px solid var(--ta-log-border,#cbd5e1);'
            'border-radius:10px;padding:12px 16px;min-height:120px;max-height:260px;'
            'overflow-y:auto;font-family:\'Courier New\',monospace;font-size:0.80em;line-height:1.7;">'
            + inner + '</div>'
        )

    def _add_log(text, kind='info'):
        log_entries.append((kind, _ts(), text))
        return _render_log()

    def _add_header(text):
        log_entries.append(('header', _ts(), text))
        return _render_log()

    # ── Download remote URL (threaded so we can stream progress to the log) ───
    if isinstance(uploaded_file, str) and (
        uploaded_file.startswith("http://") or uploaded_file.startswith("https://")
    ):
        _dl_log = _add_header("📥  DOWNLOADING FILE")
        yield _out(
            status=_status_compact("⬇️", "Downloading file from URL…", "0s"),
            eta=_eta_panel_html("loading", elapsed="0s"),
            log=_dl_log,
        )
        _dl_dir  = Path(tempfile.mkdtemp(prefix="ta_dl_"))
        _dl_q    = Q.Queue()
        _dl_done = False

        def _dl_worker():
            def _on_progress(received, total):
                _dl_q.put(("dl_progress", received, total))
            try:
                path = _download_url(uploaded_file, _dl_dir, on_progress=_on_progress)
                _dl_q.put(("dl_done", str(path)))
            except Exception as exc:
                _dl_q.put(("dl_error", str(exc)))

        threading.Thread(target=_dl_worker, daemon=True).start()

        _dl_stall   = 0
        _speed_hist = []   # [(timestamp, bytes_received)] for rolling speed calc

        def _dl_speed(recv):
            now = time.time()
            _speed_hist.append((now, recv))
            _speed_hist[:] = [(t, b) for t, b in _speed_hist if now - t < 4]
            if len(_speed_hist) >= 2:
                dt = _speed_hist[-1][0] - _speed_hist[0][0]
                db = _speed_hist[-1][1] - _speed_hist[0][1]
                return db / dt if dt > 0 else 0
            return 0

        while True:
            try:
                dmsg = _dl_q.get(timeout=1.0)
                _dl_stall = 0
                last_activity = time.time()
                if dmsg[0] == "dl_done":
                    uploaded_file = dmsg[1]
                    log = _add_log(f"✅ Download complete — {Path(uploaded_file).name}", "done")
                    yield _out(log=log)
                    break
                elif dmsg[0] == "dl_error":
                    yield _err(f"Download failed: {dmsg[1]}")
                    return
                elif dmsg[0] == "dl_progress":
                    recv, total = dmsg[1], dmsg[2]
                    speed = _dl_speed(recv)
                    recv_mb = max(0, recv) / 1_048_576
                    _total_dl_mb = recv_mb            # track for stats panel
                    _peak_dl_speed = max(_peak_dl_speed, speed / 1_048_576)
                    net_html = _net_panel_html("download", recv, total, speed)
                    _last_net_dir = "down"
                    if total and total > 0:
                        pct = min(100.0, recv / total * 100)
                        log = _add_log(
                            f"⬇️  {recv_mb:.1f} MB / {total/1_048_576:.1f} MB  "
                            f"({pct:.0f}%)  {speed/1_048_576:.1f} MB/s", "download")
                    else:
                        log = _add_log(f"⬇️  {recv_mb:.1f} MB  {speed/1_048_576:.1f} MB/s", "download")
                    yield _out(
                        status=_status_compact("⬇️", "Downloading…", _elapsed()),
                        log=log,
                    )
            except Q.Empty:
                _dl_stall += 1
                elapsed = _elapsed()
                if _dl_stall == 30:
                    log = _add_log("⚠️  Still downloading… (30s with no data). Large file or slow connection.", "warn")
                    yield _out(log=log)
                elif _dl_stall == 55:
                    log = _add_log("🚨  About to timeout (5s left). If this fails, download manually via browser.", "error")
                    yield _out(log=log)
                else:
                    yield _out(status=_status_compact("⬇️", "Downloading…", elapsed))

    from pathlib import Path as _P
    if not _P(uploaded_file).exists():
        yield _err(f"File not found: {uploaded_file}")
        return

    _fname = Path(uploaded_file).name
    log = _add_log(f"📂  File ready: {_fname}")
    # Tell the browser a job is starting — survives page refresh
    _job_js = f"<script>window.taJobStart && window.taJobStart({repr(_fname)})</script>"
    yield _out(
        status=_status_compact("⏳", "Starting…", _elapsed()) + _job_js,
        eta=_eta_panel_html("loading", elapsed=_elapsed()),
        log=log,
    )

    config = ReportConfig(
        style=report_style,
        include_summary=inc_summary,
        include_key_points=inc_key_points,
        include_action_items=inc_action_items,
        include_transcript=inc_transcript,
        include_speaker_profiles=inc_profiles,
        include_speech_analytics=inc_analytics,
    )
    # num_speakers is a numeric count of speakers; convert to a context string for Claude
    try:
        _n = int(num_speakers) if num_speakers not in (None, "") else None
    except (ValueError, TypeError):
        _n = None
    speaker_names = f"{_n} speakers" if _n and _n > 0 else None
    speakers = None  # WhisperX diarization disabled (requires HF_TOKEN)
    lang_code = language_input if language_input and language_input != "auto" else None
    lang_variant = (
        language_variant
        if language_variant and language_variant not in _VARIANT_AUTO_VALUES
        else None
    )
    stem     = Path(uploaded_file).stem
    job_id   = uuid.uuid4().hex[:8]
    job_dir  = OUT_DIR / f"{stem}_{job_id}"
    job_dir.mkdir(parents=True, exist_ok=True)
    ext      = Path(uploaded_file).suffix.lower()
    is_av    = ext in (AUDIO_EXTS | VIDEO_EXTS)

    # ── cancel event (set by GeneratorExit when user clicks Stop) ────────────
    _cancel_ev = threading.Event()

    # ── check for a cached STT result so we can skip re-transcribing ─────────
    _pre_transcribed = _load_stt_cache(uploaded_file) if is_av else None
    if _pre_transcribed:
        log = _add_log("⚡ Resuming — using saved transcript, skipping re-transcription", "done")
        yield _out(log=log)

    # ── thread communication ──────────────────────────────────────────────────
    q = Q.Queue()

    def on_whisper_progress(pct):
        q.put(("pct", pct))

    def on_raw_transcript(text):
        q.put(("transcript", text))

    def on_stage_change(stage):
        q.put(("stage", stage))

    def on_log(msg, kind=None):
        q.put(("log", msg))

    def background():
        try:
            result = run(
                file_path=uploaded_file,
                file_path_2=_file2 or None,
                output_dir=str(job_dir),
                whisper_model=_whisper_model,
                stt_engine=stt_engine,
                stt_api_key=(stt_api_key or "").strip() or None,
                stt_model=(stt_model or "").strip() or None,
                panel_mode=False,
                num_speakers=None,
                config=config,
                api_key=api_key,
                provider=provider_type,
                model=model_name,
                base_url=base_url,
                language=lang_code,
                language_variant=lang_variant,
                speaker_names=speaker_names or None,
                interview_mode=interview_mode,
                interview_deep=interview_deep,
                candidate_profile=candidate_profile or "",
                history_path=HISTORY_PATH,
                on_whisper_progress=on_whisper_progress if is_av else None,
                on_raw_transcript=on_raw_transcript if is_av else None,
                on_stage_change=on_stage_change if is_av else None,
                on_stt_done=lambda s: q.put(("stt_done", s)),
                on_token_usage=lambda i, o: q.put(("tokens", i, o)),
                on_log=on_log,
                cancel_event=_cancel_ev,
                pre_transcribed=_pre_transcribed,
                transcription_only=bool(transcription_only),
                image_paths=image_files if isinstance(image_files, list) else ([image_files] if image_files else []),
                use_gpu=bool(use_gpu),
            )
            q.put(("done", result))
        except ImportError as e:
            pkg = str(e)
            install_cmd = pkg.split('pip install ')[-1].strip()
            q.put(("error", (
                f"A required package is not installed for the selected STT engine.\n\n"
                f"Quick fix — change the STT Engine to Whisper (Local / Offline) in the sidebar, "
                f"or install the missing package:\n\n"
                f"  pip install {install_cmd}\n\n"
                f"Then restart the app. ({pkg})"
            )))
        except Exception as e:
            msg = str(e)
            _ml = msg.lower()
            if "write operation timed out" in _ml:
                msg = ("Connection timed out while sending data. "
                       "If you are on a slow network, try a smaller Whisper model or a shorter file.")
            elif ("connection error" in _ml or "connection refused" in _ml
                  or "econnrefused" in _ml or not msg.strip()
                  or type(e).__name__ in ("ConnectError", "APIConnectionError",
                                         "ServiceUnavailableError")):
                # Raw connection errors often stringify as just "Connection error."
                # Re-raise as a friendlier string so _friendly_api_error can match it.
                msg = f"Connection error. Could not reach the API. ({type(e).__name__}: {msg})"
            q.put(("error", msg))

    t = threading.Thread(target=background, daemon=True)
    t.start()

    # ── live update loop ──────────────────────────────────────────────────────
    whisper_pct     = 0.0
    raw_shown       = False
    claude_started  = False
    stage           = "loading"
    last_activity   = time.time()
    stall_warned    = set()
    _tok_in         = 0       # accumulate token counts
    _tok_out        = 0
    _raw_stt_text   = ""      # raw STT text; stored here so "done" handler can update cache
    _word_count     = 0       # word count of transcript, for Claude ETA estimation
    # _total_dl_mb and _peak_dl_speed are NOT reset here — they were already
    # initialised at the top of process_file and may hold URL-download values.

    def _eta_secs(pct):
        if pct <= 0.01:
            return None
        return max(0, int((time.time() - start_time) * (1.0 - pct) / pct))

    def _eta_str(pct):
        s = _eta_secs(pct)
        if s is None:
            return ""
        em, es = divmod(s, 60)
        return f"~{em}m {es:02d}s" if em else f"~{es}s"

    try:
     while True:
        try:
            msg = q.get(timeout=1.0)
            last_activity = time.time()
        except GeneratorExit:
            _cancel_ev.set()
            raise
        except Q.Empty:
            elapsed  = _elapsed()
            quiet    = int(time.time() - last_activity)
            eta_upd  = gr.update()

            # ── stall detection ──────────────────────────────────────────────
            stall_key = f"{stage}_{quiet}"
            if stage == "loading" and quiet == 60 and stall_key not in stall_warned:
                stall_warned.add(stall_key)
                log = _add_log("⚠️  Still preparing… (60s). Large file or slow start.", "warn")
                yield _out(log=log)
            elif stage == "extracting" and quiet == 120 and stall_key not in stall_warned:
                stall_warned.add(stall_key)
                log = _add_log("⚠️  Audio extraction taking 2+ min. Large video file — still working.", "warn")
                yield _out(log=log)
            elif stage == "whisper" and quiet == 300 and stall_key not in stall_warned:
                stall_warned.add(stall_key)
                log = _add_log("⚠️  Whisper running 5 min without a progress update. CPU transcription is slow for long recordings — still active.", "warn")
                yield _out(log=log)
            elif stage == "whisper" and quiet == 900 and stall_key not in stall_warned:
                stall_warned.add(stall_key)
                log = _add_log("🚨  15 min with no Whisper update. If progress bar is frozen, consider restarting and using a smaller Whisper model (tiny/base).", "error")
                yield _out(log=log)
            elif stage == "claude" and quiet == 120 and stall_key not in stall_warned:
                stall_warned.add(stall_key)
                log = _add_log(f"⚠️  {model_name} taking 2+ min. Long transcript or slow API — still waiting.", "warn")
                yield _out(log=log)
            elif stage == "claude" and quiet == 600 and stall_key not in stall_warned:
                stall_warned.add(stall_key)
                log = _add_log(f"🚨  10 min waiting for {provider_name} ({model_name}). Check your API key quota or try a faster model.", "error")
                yield _out(log=log)
            elif stage == "claude" and quiet > 0 and quiet % 30 == 0:
                yield _out(eta=_eta_panel_html("claude", elapsed=elapsed, word_count=_word_count))

            # ── eta panel update ─────────────────────────────────────────────
            if stage in ("whisper",):
                eta_s   = _eta_secs(whisper_pct) if whisper_pct > 0 else None
                eta_upd = _eta_panel_html("whisper", pct=whisper_pct or None,
                                          eta_secs=eta_s, elapsed=elapsed)
                yield _out(status=_status_compact("🎤", "Transcribing audio…", elapsed), eta=eta_upd)
            elif stage == "stt_cloud":
                yield _out(status=_status_compact("☁️", "Uploading & transcribing…", elapsed),
                           eta=_eta_panel_html("stt_cloud", elapsed=elapsed))
            elif stage == "extracting":
                yield _out(status=_status_compact("🎬", "Extracting audio…", elapsed),
                           eta=_eta_panel_html("extracting", elapsed=elapsed))
            elif stage in ("claude",) or claude_started:
                yield _out(status=_status_compact("🤖", "Analyzing with AI…", elapsed),
                           eta=_eta_panel_html("claude", elapsed=elapsed, word_count=_word_count))
            else:
                yield _out(status=_status_compact("⏳", "Loading…", elapsed),
                           eta=_eta_panel_html("loading", elapsed=elapsed))
            continue

        kind = msg[0]

        if kind == "tokens":
            _tok_in, _tok_out = msg[1], msg[2]
            log_text = _add_log(f"🤖 Tokens: {_tok_in:,} in / {_tok_out:,} out", "ai")
            yield _out(log=log_text,
                       stats=_stats_panel_html(_elapsed(), _tok_in, _tok_out, _total_dl_mb,
                                               _peak_dl_speed, model_name=model_name,
                                               provider_type=provider_type))

        elif kind == "log":
            log_text = _add_log(msg[1], "info")
            yield _out(log=log_text)

        elif kind == "stage":
            stage   = msg[1]
            elapsed = _elapsed()
            if stage == "extracting":
                log = _add_header("🎬  EXTRACTING AUDIO")
                yield _out(status=_status_compact("🎬", "Extracting audio from video…", elapsed),
                           eta=_eta_panel_html("extracting", elapsed=elapsed), log=log)
            elif stage == "whisper":
                log = _add_header("🎤  TRANSCRIBING AUDIO  (Step 1 of 2)")
                log = _add_log(f"Whisper {_whisper_model} loaded — transcription in progress…", "info")
                yield _out(status=_status_compact("🎤", f"Transcribing audio…  [{_whisper_model}]", elapsed),
                           eta=_eta_panel_html("whisper", elapsed=elapsed), log=log)
            elif stage == "claude" and not claude_started:
                log = _add_header("🤖  AI ANALYSIS  (Step 2 of 2)")
                log = _add_log(f"Sending transcript to {provider_name} — {model_name}…", "ai")
                yield _out(status=_status_compact("🤖", f"Analyzing with {model_name}…", elapsed),
                           eta=_eta_panel_html("claude", elapsed=elapsed, word_count=_word_count), log=log)

        elif kind == "pct":
            whisper_pct = msg[1]
            elapsed     = _elapsed()
            eta_s       = _eta_secs(whisper_pct)
            eta_txt     = _eta_str(whisper_pct)
            pct_int     = int(whisper_pct * 100)
            # Log: clean text only — ETA panel owns the visual bar
            log_text = _add_log(
                f"🎤 {pct_int}%{('  —  ETA ' + eta_txt) if eta_txt else ''}",
                "progress"
            )
            yield _out(
                status=_status_compact("🎤", f"Transcribing…  {pct_int}%", elapsed),
                eta=_eta_panel_html("whisper", pct=whisper_pct, eta_secs=eta_s, elapsed=elapsed),
                log=log_text,
            )

        elif kind == "transcript":
            raw_shown     = True
            elapsed       = _elapsed()
            _raw_stt_text = msg[1]
            _word_count   = len(_raw_stt_text.split())
            # Interim cache save (no lang/segments yet — updated fully on "done")
            _save_stt_cache(uploaded_file, _raw_stt_text, "", [])
            log_text  = _add_log("✅ Transcription complete!", "done")
            log_text  = _add_header("🤖  AI ANALYSIS  (Step 2 of 2)")
            log_text  = _add_log(f"Sending transcript to {provider_name} — {model_name}…", "ai")
            yield _out(
                status=_status_compact("🤖", f"Analyzing with {model_name}…", elapsed),
                eta=_eta_panel_html("claude", elapsed=elapsed, word_count=_word_count),
                transcript=msg[1],
                log=log_text,
            )
            claude_started = True
            stage = "claude"

        elif kind == "done":
            result = msg[1]

            if transcription_only:
                summary_md = "_Transcription only — AI analysis was skipped. See the Transcript tab for the full text._"
            else:
            # Strip any transcript/full-text section Claude may embed inside result.summary
                _summary_text = re.sub(
                    r'\n*#{1,3}\s*(Full\s+)?Transcript[\s\S]*',
                    '', result.summary, flags=re.IGNORECASE
                ).strip()
                summary_md = f"## Summary\n\n{_summary_text}"
            if not transcription_only:
                if inc_key_points and result.key_points:
                    kp_lines = "\n".join(f"- {p}" for p in result.key_points)
                    summary_md += f"\n\n---\n\n## Key Points\n\n{kp_lines}"
                if inc_action_items and result.action_items:
                    ai_lines = "\n".join(_fmt_action_item_md(a) for a in result.action_items)
                    summary_md += f"\n\n---\n\n## Action Items\n\n{ai_lines}"

            if result.speaker_profiles:
                profiles_md = "\n\n---\n\n".join(
                    f"### {name}\n\n{profile}" for name, profile in result.speaker_profiles.items()
                )
                if result.speaker_map:
                    mapping = "\n".join(f"- `{k}` → **{v}**" for k, v in result.speaker_map.items())
                    profiles_md = f"## Speaker Map\n\n{mapping}\n\n---\n\n{profiles_md}"
            else:
                profiles_md = "_Enable **Panel Mode** for speaker profiles._"

            analytics_md  = stats_to_markdown(result.speaker_stats)
            combined_text = build_combined_report(result, config)

            # ── Build Interview Analysis tab (coaching + optional video delivery) ──
            # iv_html is NOT written to the UI until the very final yield — no mid-run updates
            iv_html = _build_interview_html(result.interview_analysis)
            _va_annotated_path = None
            _va_res = None
            _va_timeline_html = ""

            _is_video_file = Path(uploaded_file).suffix.lower() in {
                ".mp4", ".mov", ".avi", ".mkv", ".webm", ".m4v",
                ".flv", ".wmv", ".ts", ".mts", ".vob", ".ogv",
            }
            if (interview_mode and _is_video_file and _HAS_VIDEO_ANALYZER
                    and not transcription_only and not _cancel_ev.is_set()):
                log_text = _add_log("━━━ Step 3 of 3 — Video Delivery Analysis ━━━", "info")
                log_text = _add_log("🎥 Scanning faces and analysing delivery — this may take a minute…", "ai")
                yield _out(
                    status=_status_compact("🎥", "Step 3 of 3 — Video delivery…", _elapsed()),
                    log=log_text,
                    iv_prog=gr.update(
                        value='<p style="color:#3b82f6;font-size:0.84em;padding:4px 0;">🎥 Scanning video frames…</p>',
                        visible=True,
                    ),
                )
                _va_q: Q.Queue = Q.Queue()

                def _va_worker():
                    try:
                        thumbs, _ = _video_analyzer.scan_faces(uploaded_file, use_gpu=bool(use_gpu))
                        pids = list(thumbs.keys())
                        _role_labels = [iv_role_0, iv_role_1, iv_role_2, iv_role_3]
                        _rm = {pid: (_role_labels[i] if i < len(_role_labels) else f"Person {i+1}")
                               for i, pid in enumerate(pids[:int(iv_person_count or 2)])}
                        def _pcb(v): _va_q.put(("pct", v))
                        res = _video_analyzer.analyze_video(
                            uploaded_file, _rm, sample_fps=1.0, progress_cb=_pcb,
                            use_gpu=bool(use_gpu),
                        )
                        _va_q.put(("done", res))
                    except Exception as _e:
                        _va_q.put(("err", str(_e)))

                _va_t = threading.Thread(target=_va_worker, daemon=True)
                _va_t.start()

                while _va_t.is_alive() or not _va_q.empty():
                    if _cancel_ev.is_set():
                        break
                    try: _va_msg = _va_q.get(timeout=1.0)
                    except Q.Empty:
                        yield _NOCHANGE  # keepalive so Gradio stream doesn't freeze
                        continue
                    if _va_msg[0] == "pct":
                        _pct = int(_va_msg[1] * 100)
                        log_text = _add_log(f"🎥 Video analysis {_pct}%…", "progress")
                        yield _out(
                            status=_status_compact("🎥", f"Step 3 of 3 — Video {_pct}%…", _elapsed()),
                            log=log_text,
                            iv_prog=gr.update(
                                value=f'<p style="color:#3b82f6;font-size:0.84em;padding:4px 0;">🎥 Analysing frames… {_pct}%</p>',
                                visible=True,
                            ),
                        )
                    elif _va_msg[0] == "done":
                        _va_res = _va_msg[1]
                        if _va_res and not getattr(_va_res, "error", None):
                            log_text = _add_log("✅ Video delivery analysis complete.", "done")
                            yield _out(log=log_text)
                        else:
                            log_text = _add_log(f"⚠️ Video analysis: {getattr(_va_res,'error','failed')}", "warn")
                            yield _out(log=log_text)
                        break
                    elif _va_msg[0] == "err":
                        log_text = _add_log(f"⚠️ Video delivery analysis failed: {_va_msg[1]}", "warn")
                        yield _out(log=log_text)
                        break

                # ── Claude interprets the video analysis results ──────────────
                if _va_res and not getattr(_va_res, "error", None):
                    try:
                        log_text = _add_log("🤖 Claude writing interview assessment…", "ai")
                        yield _out(
                            status=_status_compact("🤖", "Claude reviewing video results…", _elapsed()),
                            log=log_text,
                        )
                        _persons = getattr(_va_res, "persons", {})
                        # _persons is Dict[int, PersonScore] — access dataclass attrs directly
                        _candidate_scores = []
                        for _pid, _p in _persons.items():
                            if "candidate" in _p.role.lower():
                                _avg = (_p.confidence + _p.composure + _p.eye_contact
                                        + _p.engagement + _p.energy) / 5
                                _candidate_scores.append(_avg)
                        _overall_pct = ((_candidate_scores[0] / 100) if _candidate_scores
                                        else sum(
                                            (_p.overall / 100) for _p in _persons.values()
                                        ) / max(len(_persons), 1))
                        _grade = ("A" if _overall_pct >= 0.85 else
                                  "B" if _overall_pct >= 0.70 else
                                  "C" if _overall_pct >= 0.55 else
                                  "D" if _overall_pct >= 0.40 else "F")
                        _scores_summary = "\n".join(
                            f"- {_p.role}: confidence={_p.confidence:.0f}%, "
                            f"composure={_p.composure:.0f}%, "
                            f"eye_contact={_p.eye_contact:.0f}%, "
                            f"engagement={_p.engagement:.0f}%, "
                            f"dominant_emotion={_p.dominant_emotion}, "
                            f"talk_pct={_p.talk_time_pct:.0f}%"
                            for _p in _persons.values()
                        )
                        _transcript_snippet = (result.clean_transcript or "")[:2000]
                        _va_claude_prompt = (
                            "You are an expert interview coach. Based on the following video delivery "
                            "analysis data and transcript excerpt, write a concise, actionable assessment "
                            "(3-5 short paragraphs) covering: overall impression, key strengths, areas to "
                            "improve, and one specific tip for each participant. "
                            f"Overall grade: {_grade} ({_overall_pct:.0%}).\n\n"
                            f"PARTICIPANTS:\n{_scores_summary}\n\n"
                            f"TRANSCRIPT EXCERPT:\n{_transcript_snippet}\n\n"
                            "Write in a supportive, professional tone. Be specific and actionable."
                        )
                        _llm = LLMClient(
                            provider=provider_name,
                            model=model_name,
                            api_key=user_api_key or "",
                            use_gpu=bool(use_gpu),
                        )
                        _va_claude_text = _llm.chat(
                            system="You are an expert interview coach providing concise, actionable feedback.",
                            user=_va_claude_prompt,
                            max_tokens=800,
                        )
                        # grade badge colours
                        _grade_color = {"A":"#16a34a","B":"#2563eb","C":"#d97706","D":"#dc2626","F":"#7f1d1d"}.get(_grade,"#6b7280")
                        _va_claude_html = (
                            f'<div style="margin-top:16px;padding:16px 18px;'
                            f'background:linear-gradient(135deg,rgba(29,78,216,0.08),rgba(59,130,246,0.05));'
                            f'border:1.5px solid rgba(59,130,246,0.25);border-radius:14px;">'
                            f'<div style="display:flex;align-items:center;gap:12px;margin-bottom:10px;">'
                            f'<span style="font-size:0.82em;font-weight:700;color:#3b82f6;">🤖 Claude\'s Interview Assessment</span>'
                            f'<span style="font-size:1.1em;font-weight:800;color:{_grade_color};'
                            f'background:rgba(0,0,0,0.06);border-radius:6px;padding:2px 10px;">'
                            f'Grade: {_grade}</span></div>'
                            f'<div style="font-size:0.84em;line-height:1.7;color:var(--ta-text);">'
                            + _va_claude_text.replace("\n\n", "</p><p style='margin:0 0 10px'>")
                                             .replace("\n", "<br>")
                            + '</div></div>'
                        )
                        iv_html = _build_unified_interview_html(result.interview_analysis, _va_res)
                        iv_html = iv_html + _va_claude_html
                        log_text = _add_log(f"🤖 Assessment complete — Final Grade: {_grade}", "done")
                        yield _out(log=log_text)
                        # pre-render timeline once to avoid double call in final yield
                        try:
                            _tl_fig = _video_analyzer.render_timeline_figure(_va_res)
                            _va_timeline_html = (_tl_fig.to_html(
                                full_html=False, include_plotlyjs="cdn",
                                config={"displayModeBar": False}
                            ) if _tl_fig else "")
                        except Exception:
                            _va_timeline_html = ""
                    except Exception as _ce:
                        log_text = _add_log(f"⚠️ Claude video assessment skipped: {_ce}", "warn")
                        yield _out(log=log_text)

            # ── Update cache with lang + segments now that we have them ────────
            if _raw_stt_text:
                _save_stt_cache(uploaded_file,
                                _raw_stt_text,
                                result.detected_language or "",
                                result.segments or [])

            # ── STT timing into the log ───────────────────────────────────────
            if result.stt_seconds > 0:
                eng_label = STT_ENGINES.get(result.stt_engine, result.stt_engine)
                log_text = _add_log(f"🎤 {eng_label} — {result.stt_seconds:.1f}s", "done")

            _f_t_path = job_dir / f"{stem}_transcript.txt"
            _f_t_path.write_text(result.clean_transcript, encoding="utf-8")
            if transcription_only:
                f_t = str(_f_t_path)
                f_s = f_r = f_c = f_j = f_p = None
                f_srt = f_vtt = f_docx = None
            else:
                f_t    = str(_f_t_path)
                f_s    = str(job_dir / f"{stem}_speakers.txt")
                f_r    = str(job_dir / f"{stem}_report.md")
                f_c    = str(job_dir / f"{stem}_combined.txt")
                f_j    = str(job_dir / f"{stem}_full.json")
                f_srt  = str(job_dir / f"{stem}.srt")   if (job_dir / f"{stem}.srt").exists()  else None
                f_vtt  = str(job_dir / f"{stem}.vtt")   if (job_dir / f"{stem}.vtt").exists()  else None
                f_docx = str(job_dir / f"{stem}_report.docx") if (job_dir / f"{stem}_report.docx").exists() else None
                # ── Append video delivery section to PDF/DOCX if available ──
                _va_combined_section = ""
                if _va_res and not getattr(_va_res, "error", None) and _va_res.persons:
                    _divider = "=" * 60
                    _thin    = "-" * 40
                    _lines   = ["", _divider, "VIDEO DELIVERY ANALYSIS", _divider,
                                f"  Duration : {int(_va_res.duration_seconds//60)}m {int(_va_res.duration_seconds%60)}s  |  "
                                f"Participants : {_va_res.person_count}  |  "
                                f"Overall : {_va_res.overall_score:.0f}/100", ""]
                    for _pid, _p in _va_res.persons.items():
                        _lines.append(f"  {_p.role.upper()}")
                        _lines.append(f"  {'_'*30}")
                        _lines.append(f"    Confidence   : {_p.confidence:.0f}/100")
                        _lines.append(f"    Composure    : {_p.composure:.0f}/100")
                        _lines.append(f"    Eye Contact  : {_p.eye_contact:.0f}/100")
                        _lines.append(f"    Engagement   : {_p.engagement:.0f}/100")
                        _lines.append(f"    Energy       : {_p.energy:.0f}/100")
                        _lines.append(f"    Dominant Mood: {_p.dominant_emotion}")
                        _lines.append(f"    Open Posture : {_p.open_body_pct:.0f}%  |  Arms Crossed: {_p.arm_crossed_pct:.0f}%  |  Forward Lean: {_p.forward_lean_pct:.0f}%")
                        if _p.cultural:
                            _lines.append(f"    Cultural Scores:")
                            _lines.append(f"      American Interview Standard : {_p.cultural.american_score:.0f}/100")
                            _lines.append(f"      Indian → American Adaptation: {_p.cultural.adaptation_score:.0f}/100")
                            for _t in _p.cultural.american_tips[:3]:
                                _lines.append(f"      • {_t[:120]}")
                            for _t in _p.cultural.adaptation_tips[:3]:
                                _lines.append(f"      → {_t[:120]}")
                        _lines.append("")
                    if _va_res.observations:
                        _lines += ["  KEY OBSERVATIONS", "  " + _thin]
                        for _o in _va_res.observations:
                            _lines.append(f"    • {_o}")
                    _va_combined_section = "\n".join(_lines)
                    combined_text = combined_text + _va_combined_section

                # ── Generate DOCX (always — includes all sections + video) ──────
                f_docx_path = job_dir / f"{stem}_report.docx"
                try:
                    from transcript_agent import generate_docx as _gen_docx
                    _gen_docx(result, stem, str(f_docx_path), va_result=_va_res)
                    f_docx = str(f_docx_path) if f_docx_path.exists() else None
                except Exception:
                    f_docx = None

                # ── Generate PDF (always — includes all sections + video) ────────
                f_p_path = job_dir / f"{stem}_report.pdf"
                try:
                    _generate_pdf(stem, combined_text, f_p_path,
                                 result=result, va_result=_va_res)
                    f_p = str(f_p_path)
                except Exception as _pdf_err:
                    import traceback as _tb
                    print(f"[PDF ERROR] {_pdf_err}\n{_tb.format_exc()}", flush=True)
                    f_p = None

            # ── Translate transcript output if a different language was chosen ──
            _out_lang = (transcript_output_lang or "Same as source").strip()
            _display_transcript = result.clean_transcript
            _display_dialogue   = result.speaker_dialogue
            if _out_lang and _out_lang != "Same as source":
                try:
                    log_text = _add_log(f"🌐 Translating transcript to {_out_lang}…", "ai")
                    yield _out(
                        status=_status_compact("🌐", f"Translating to {_out_lang}…", _elapsed()),
                        log=log_text,
                    )
                    _display_transcript = _translate_transcript(
                        result.clean_transcript, _out_lang,
                        api_key, provider_type, model_name, base_url,
                        use_gpu=bool(use_gpu),
                    )
                    if result.speaker_dialogue:
                        _display_dialogue = _translate_transcript(
                            result.speaker_dialogue, _out_lang,
                            api_key, provider_type, model_name, base_url,
                            use_gpu=bool(use_gpu),
                        )
                except Exception as _te:
                    log_text = _add_log(f"⚠️ Translation failed: {_te} — showing original", "warn")
                    yield _out(log=log_text)

            total_elapsed = _elapsed()
            log_text = _add_header("✅  COMPLETE")
            log_text = _add_log(f"All done in {total_elapsed}. Results ready in all tabs.", "done")
            yield _out(
                status=_status_compact("✅", "Done! All tabs are ready.", total_elapsed)
                      + "<script>window.taJobEnd && window.taJobEnd()</script>",
                eta=_eta_panel_html("done", elapsed=total_elapsed, done=True),
                summary=summary_md,
                transcript=_display_transcript,
                dialogue=_display_dialogue,
                profiles=profiles_md,
                analytics=analytics_md,
                combined=combined_text,
                interview=iv_html,
                dl_t=f_t, dl_s=f_s, dl_r=f_r, dl_c=f_c, dl_j=f_j, dl_p=f_p,
                dl_srt=f_srt, dl_vtt=f_vtt, dl_docx=f_docx,
                dl_acc=gr.update(open=True),
                dl_wait=gr.update(visible=False),

                stats=_stats_panel_html(total_elapsed, _tok_in, _tok_out,
                                        _total_dl_mb, _peak_dl_speed, done=True,
                                        model_name=model_name, provider_type=provider_type),
                rs={"stem": stem, "combined_text": combined_text,
                    "detected_language": result.detected_language,
                    "out_dir": str(job_dir),
                    "summary": result.summary or "",
                    "key_points": result.key_points or [],
                    "action_items": result.action_items or [],
                    "speaker_dialogue": result.speaker_dialogue or "",
                    "clean_transcript": result.clean_transcript or ""},
                log=log_text,
                iv_scores=gr.update(
                    value=_video_analyzer.render_score_cards_html(_va_res, ia=result.interview_analysis) if _va_res and not getattr(_va_res,'error',None) else "",
                    visible=bool(_va_res and not getattr(_va_res,'error',None))),
                iv_tl=gr.update(
                    value=_va_timeline_html,
                    visible=bool(_va_timeline_html)),
                iv_sum=gr.update(
                    value=("<ul style='margin:0;padding-left:18px;'>" + "".join(f"<li style='font-size:0.88em;color:#374151;margin-bottom:6px;'>{o}</li>" for o in getattr(_va_res,'observations',[])) + "</ul>") if _va_res and getattr(_va_res,'observations',None) else "",
                    visible=bool(_va_res and getattr(_va_res,'observations',None))),
                iv_vid=gr.update(value=None, visible=False),
                iv_prog=gr.update(
                    value="<p style='color:#22c55e;font-size:0.84em;padding:4px 0;'>✅ Step 3 complete — Delivery analysis ready.</p>" if _va_res and not getattr(_va_res,'error',None) else "",
                    visible=bool(_va_res and not getattr(_va_res,'error',None))),
            )
            break

        elif kind == "error":
            err_msg = str(msg[1])
            if "cancelled" in err_msg.lower():
                yield _out(
                    status=_IDLE_STATUS,
                    eta=_eta_panel_html("idle"),
                    log=_IDLE_LOG,
                    summary=gr.update(value=""), transcript=gr.update(value=""),
                    dialogue=gr.update(value=""), profiles=gr.update(value=""),
                    analytics=gr.update(value=""), combined=gr.update(value=""),
                    interview=gr.update(value=""),
                    dl_t=None, dl_s=None, dl_r=None, dl_c=None,
                    dl_j=None, dl_p=None, dl_srt=None, dl_vtt=None, dl_docx=None,
                    dl_acc=gr.update(open=False),
                )
            else:
                display_msg = _friendly_api_error(err_msg, provider_name, model_name)
                log_text = _add_log(f"🚨 {display_msg}", "error")
                yield _out(log=log_text)
                yield _err(f"Processing failed: {display_msg}")
            break
    finally:
        _cancel_ev.set()   # always signal the background thread to stop


def toggle_speakers(is_panel):
    return gr.update(visible=is_panel)


_STT_MODELS = {
    "whisper_local":  [],   # handled via _WHISPER_SIZES in toggle_stt_engine
    "openai_whisper": ["whisper-1", "gpt-4o-transcribe", "gpt-4o-mini-transcribe"],
    "groq_whisper":   ["whisper-large-v3-turbo", "whisper-large-v3", "distil-whisper-large-v3-en"],
    "deepgram":       ["nova-3", "nova-2", "nova", "enhanced", "base"],
    "assemblyai":     ["best", "nano", "slam-1"],
    "google_stt":     ["latest_long", "latest_short", "command_and_search", "phone_call"],
    "azure_speech":   ["conversation", "dictation", "command_and_search"],
    "elevenlabs":     ["scribe_v1"],
    "revai":          ["machine", "fusion"],
}

# Engines whose key can be auto-filled from the main AI provider key
_STT_AUTOFILL_PREFIX = {
    "openai_whisper": ("sk-", "OpenAI"),
    "groq_whisper":   ("gsk_", "Groq"),
}


_WHISPER_SIZES = ["tiny", "base", "small", "medium", "large-v2", "large-v3", "turbo"]


_STT_KEY_INFO = {
    "deepgram": (
        "console.deepgram.com",
        "https://console.deepgram.com",
        "200 hours free — no credit card required to start",
    ),
    "assemblyai": (
        "assemblyai.com",
        "https://www.assemblyai.com/dashboard/signup",
        "Free tier available — sign up and copy your API key",
    ),
    "openai_whisper": (
        "platform.openai.com → API keys",
        "https://platform.openai.com/api-keys",
        "Billed per minute of audio — ~$0.006/min",
    ),
    "groq_whisper": (
        "console.groq.com",
        "https://console.groq.com/keys",
        "Free tier with generous limits",
    ),
    "elevenlabs": (
        "elevenlabs.io",
        "https://elevenlabs.io/app/settings/api-keys",
        "Free tier available — best for speaker-aware transcription",
    ),
    "revai": (
        "rev.ai",
        "https://www.rev.ai/access_token",
        "300 minutes free on signup",
    ),
    "google_stt": (
        "console.cloud.google.com",
        "https://console.cloud.google.com/apis/credentials",
        "60 mins free/month — requires Google Cloud account",
    ),
    "azure_speech": (
        "portal.azure.com",
        "https://portal.azure.com/#create/Microsoft.CognitiveServicesSpeechServices",
        "5 hours free/month — requires Azure account",
    ),
}


def _stt_key_banner(engine: str) -> str:
    info = _STT_KEY_INFO.get(engine)
    if not info:
        return (
            '<div class="ta-stt-banner">'
            '<span style="font-size:1.1em;flex-shrink:0;">🔑</span>'
            '<div style="flex:1;min-width:0;">'
            '<span class="ta-stt-banner-title">API Key Required</span>'
            '<span class="ta-stt-banner-body">Enter your API key below — never stored on this server.</span>'
            '</div></div>'
        )
    label, url, note = info
    return (
        f'<div class="ta-stt-banner">'
        f'<span style="font-size:1.1em;flex-shrink:0;">🔑</span>'
        f'<div style="flex:1;min-width:0;">'
        f'<span class="ta-stt-banner-title">API Key Required</span>'
        f'<span class="ta-stt-banner-body">'
        f'Get your key at <a href="{url}" target="_blank" '
        f'style="color:#3b82f6;font-weight:600;">{label}</a> — {note}.'
        f'</span></div></div>'
    )


def toggle_stt_engine(engine, main_api_key="", stored_model=None):
    is_local = engine == "whisper_local"
    models   = _WHISPER_SIZES if is_local else _STT_MODELS.get(engine, [])

    key_kw = {"visible": not is_local}
    if engine in _STT_AUTOFILL_PREFIX:
        prefix, _ = _STT_AUTOFILL_PREFIX[engine]
        if (main_api_key or "").startswith(prefix):
            key_kw["value"] = main_api_key

    label   = "Whisper model size" if is_local else "STT Model"
    info    = "tiny = fastest · turbo ≈ large speed  |  large-v3 = most accurate" if is_local else ""
    if stored_model and stored_model in models:
        default = stored_model
    elif is_local:
        default = "base"
    else:
        default = models[0] if models else None

    banner = gr.update(
        visible=not is_local,
        value=_stt_key_banner(engine) if not is_local else "",
    )

    return (
        gr.update(**key_kw),                                                # stt_key_input
        gr.update(choices=models, value=default, label=label, info=info,   # stt_model_input
                  visible=bool(models)),
        banner,                                                             # stt_key_banner
    )


_VARIANT_LABELS = {
    "en": "English regional variant",
    "es": "Spanish regional variant",
    "fr": "French regional variant",
    "pt": "Portuguese regional variant",
    "de": "German regional variant",
    "it": "Italian regional variant",
    "zh": "Chinese regional variant",
    "ja": "Japanese dialect",
    "ko": "Korean dialect",
    "ar": "Arabic regional variant",
    "ru": "Russian regional variant",
    "hi": "Hindi dialect",
    "bn": "Bengali regional variant",
    "ta": "Tamil regional variant",
    "te": "Telugu dialect",
    "gu": "Gujarati regional variant",
    "kn": "Kannada dialect",
    "ml": "Malayalam dialect",
    "mr": "Marathi dialect",
    "pa": "Punjabi regional variant",
    "ur": "Urdu regional variant",
    "nl": "Dutch regional variant",
    "tr": "Turkish regional variant",
    "vi": "Vietnamese regional variant",
    "sv": "Swedish regional variant",
    "no": "Norwegian variant",
    "pl": "Polish regional variant",
    "th": "Thai regional variant",
    "el": "Greek regional variant",
    "he": "Hebrew regional variant",
    "ro": "Romanian regional variant",
    "hu": "Hungarian regional variant",
    "cs": "Czech regional variant",
    "fi": "Finnish regional variant",
    "da": "Danish regional variant",
    "uk": "Ukrainian regional variant",
    "id": "Indonesian regional variant",
}

def toggle_language_variant(lang):
    variants = LANGUAGE_VARIANTS.get(lang, [])
    if variants:
        # Use the preloaded full choices list so Gradio never rejects a value
        # that isn't in the currently rendered choices (fixes Indian languages).
        all_choices = [
            (lbl, val)
            for vs in LANGUAGE_VARIANTS.values()
            for lbl, val in vs
        ]
        first_val = variants[0][1]
        return gr.update(choices=all_choices, value=first_val,
                         label="Regional variant / dialect",
                         visible=True, interactive=True)
    return gr.update(choices=[], value=None,
                     label="Regional variant / dialect",
                     visible=True, interactive=False,
                     placeholder="No variants for this language")


# ── build theme ────────────────────────────────────────────────────────────────
_THEME = gr.themes.Soft(
    primary_hue=gr.themes.colors.blue,
    secondary_hue=gr.themes.colors.slate,
    neutral_hue=gr.themes.colors.slate,
    font=[gr.themes.GoogleFont("Inter"), "ui-sans-serif", "system-ui", "sans-serif"],
    font_mono=[gr.themes.GoogleFont("JetBrains Mono"), "ui-monospace", "Courier New", "monospace"],
).set(
    body_background_fill="#f1f5f9",
    body_text_color="#1e293b",
    button_primary_background_fill="*primary_500",
    button_primary_background_fill_hover="*primary_600",
    button_primary_text_color="white",
    button_primary_border_color="transparent",
    block_background_fill="white",
    block_border_color="#e2e8f0",
    block_border_width="1px",
    block_shadow="0 1px 3px 0 rgba(0,0,0,0.07)",
    block_radius="12px",
    block_label_text_weight="600",
    block_label_text_color="#475569",
    block_label_text_size="*text_sm",
    input_background_fill="white",
    input_border_color="#e2e8f0",
    panel_background_fill="white",
    panel_border_color="#e2e8f0",
)

# ── HTML snippets ───────────────────────────────────────────────────────────────
_HERO = """
<div class="ta-topbar">
  <div class="ta-topbar-icon">🎤</div>
  <div class="ta-topbar-body">
    <span class="ta-topbar-name">Transcript Agent</span>
    <span class="ta-topbar-tag">Whisper transcription &nbsp;·&nbsp; Multi-provider AI &nbsp;·&nbsp; Speaker diarization</span>
  </div>
  <div class="ta-topbar-pills">
    <span class="ta-pill">🤖 8 AI Providers</span>
    <span class="ta-pill">🌐 37+ Languages</span>
    <span class="ta-pill">🔒 100% Private</span>
    <span class="ta-pill">🎵 Audio · Video · Docs</span>
  </div>
</div>
"""

_API_BANNER = """
<div id="api-banner" style="background:linear-gradient(135deg,#fffbeb,#fef3c7);border:1.5px solid #f59e0b;border-radius:10px;
     padding:10px 16px;display:flex;align-items:center;gap:10px;margin-top:6px;font-family:sans-serif;
     transition:all 0.25s;">
  <span id="api-banner-icon" style="font-size:1.2em;flex-shrink:0;transition:all 0.25s;">🔑</span>
  <div style="flex:1;min-width:0;">
    <span id="api-banner-title" style="font-weight:700;color:#92400e;font-size:0.88em;">API Key Required</span>
    <span id="api-banner-sub" style="color:#78350f;font-size:0.82em;margin-left:6px;">
      Enter your provider key — billed to your account, never stored here.
    </span>
  </div>
  <div id="api-banner-badge" style="display:none;background:#16a34a;color:#fff;
       font-size:0.68em;font-weight:700;padding:3px 10px;border-radius:20px;
       letter-spacing:0.06em;flex-shrink:0;">KEY SET ✓</div>
</div>
"""

_THEME_TOGGLE = ""  # buttons injected via _THEME_JS into <body> — not gr.HTML

# ── Theme JS — injected via gr.Blocks(js=...) which is the guaranteed execution
# path. gr.HTML uses Svelte {#html} which deliberately does NOT run <script> tags.
_THEME_JS = """
/* ── Viewport meta tag for mobile responsiveness ── */
(function(){
  if (!document.querySelector('meta[name="viewport"]')) {
    var m = document.createElement('meta');
    m.name = 'viewport';
    m.content = 'width=device-width, initial-scale=1.0';
    document.head.appendChild(m);
  }
})();

/* ── OTA update button handler ── */
window.taDoUpdate = function(url, btn, platform) {
  if (!url) return;
  btn.disabled = true;
  btn.textContent = '⏳ Opening download…';
  window.open(url, '_blank');
  setTimeout(function() {
    btn.textContent = platform === 'win'
      ? '✅ Run the installer to update'
      : '✅ Open .dmg to update';
    btn.style.background = '#22c55e';
    btn.style.color = '#fff';
  }, 1800);
};

window.taClickUpdateBtn = function(btn) {
  btn.disabled = true;
  btn.textContent = '⏳ Updating…';
  var prog = document.getElementById('ta-update-progress');
  if (prog) prog.style.display = 'block';
  /* Trigger the hidden Gradio update button */
  var hidden = document.getElementById('ta-hidden-update-btn');
  if (hidden) hidden.click();
};

(function(){
  window.__taThemeRan = true;
  var _dark = false;

  /* ── Inject toggle widget directly into <body> so Gradio can never remove it ─
     We do NOT use gr.HTML() for the buttons — Gradio 6 re-renders those
     components and strips IDs/styles. Injecting via JS is permanent.           */
  /* Sync button visuals to current _dark state — called after every inject */
  function _syncToggleUI() {
    var bl = document.getElementById('ta-btn-light');
    var bd = document.getElementById('ta-btn-dark');
    var wg = document.getElementById('ta-widget');
    if (!bl || !bd) return;
    var _base = 'display:flex;align-items:center;gap:5px;padding:6px 14px;border-radius:24px;border:none;cursor:pointer;font-size:0.82em;font-weight:700;transition:all 0.22s;';
    bl.style.cssText = _base + (_dark
      ? 'background:transparent;color:#64748b;box-shadow:none;'
      : 'background:#3b82f6;color:#fff;box-shadow:0 2px 6px rgba(59,130,246,0.4);');
    bd.style.cssText = _base + (_dark
      ? 'background:#3b82f6;color:#fff;box-shadow:0 2px 6px rgba(59,130,246,0.4);'
      : 'background:transparent;color:#64748b;box-shadow:none;');
    if (wg) {
      wg.style.background  = _dark ? 'rgba(15,23,42,0.96)' : 'rgba(255,255,255,0.96)';
      wg.style.borderColor = _dark ? '#334155' : '#e2e8f0';
    }
  }

  function _injectToggle() {
    if (!document.getElementById('ta-widget')) {
      var w = document.createElement('div');
      w.id = 'ta-widget';
      w.style.cssText = (
        'position:fixed;top:14px;right:18px;z-index:9999;display:flex;align-items:center;'
        + 'background:rgba(255,255,255,0.96);backdrop-filter:blur(12px);'
        + 'border:1px solid #e2e8f0;border-radius:30px;padding:4px;'
        + 'box-shadow:0 2px 14px rgba(0,0,0,0.13);gap:2px;'
      );
      /* Both buttons start neutral — _syncToggleUI() will set correct active state */
      w.innerHTML = (
        '<button id="ta-btn-light" style="display:flex;align-items:center;gap:5px;padding:6px 14px;'
        + 'border-radius:24px;border:none;cursor:pointer;font-size:0.82em;font-weight:700;">☀️ Light</button>'
        + '<button id="ta-btn-dark" style="display:flex;align-items:center;gap:5px;padding:6px 14px;'
        + 'border-radius:24px;border:none;cursor:pointer;font-size:0.82em;font-weight:700;">🌙 Dark</button>'
      );
      document.documentElement.appendChild(w);
    }
    /* Always sync after inject — fixes re-injection resetting wrong button active */
    _syncToggleUI();
    /* Inject pulse-ring keyframe once */
    if (!document.getElementById('ta-float-css')) {
      var _fcs = document.createElement('style');
      _fcs.id = 'ta-float-css';
      _fcs.textContent = (
        '@keyframes ta-spin{to{transform:rotate(360deg)}}'
        + '#ta-float-analyze{transition:background 0.3s,box-shadow 0.3s,transform 0.15s!important}'
        + '#ta-float-ring{position:absolute;width:62px;height:62px;border-radius:50%;'
        + 'border:3px solid transparent;border-top-color:#ef4444;border-right-color:#ef4444;'
        + 'pointer-events:none;animation:ta-spin 0.9s linear infinite;display:none;'
        + 'top:-3px;left:-3px;}'
      );
      document.head.appendChild(_fcs);
    }

    function _buildFloat() {
      var fw = document.createElement('div');
      fw.id = 'ta-float-wrap';
      fw.style.cssText = (
        'position:fixed;top:50%;right:18px;z-index:2147483647;'
        + 'transform:translateY(-50%);'
        + 'display:flex;flex-direction:column;align-items:center;gap:8px;pointer-events:none;'
      );
      /* pulse ring (shown only in stop mode) */
      var fring = document.createElement('div');
      fring.id = 'ta-float-ring';

      var flabel = document.createElement('div');
      flabel.id = 'ta-float-label';
      flabel.style.cssText = (
        'font-size:0.7em;font-weight:700;letter-spacing:0.06em;text-transform:uppercase;'
        + 'color:#fff;backdrop-filter:blur(6px);'
        + 'padding:3px 10px;border-radius:12px;opacity:0;transition:opacity 0.2s,background 0.3s;'
        + 'pointer-events:none;white-space:nowrap;background:rgba(185,28,28,0.85);'
      );
      flabel.textContent = 'Analyze';

      var fwrap = document.createElement('div');
      fwrap.style.cssText = 'position:relative;width:56px;height:56px;pointer-events:all;';

      var fbtn = document.createElement('button');
      fbtn.id = 'ta-float-analyze';
      fbtn.style.cssText = (
        'width:56px;height:56px;border-radius:50%;border:none;cursor:pointer;'
        + 'background:linear-gradient(135deg,#b91c1c,#ef4444);color:#fff;'
        + 'font-size:1.5em;display:flex;align-items:center;justify-content:center;'
        + 'box-shadow:0 4px 24px rgba(220,38,38,0.55);outline:none;pointer-events:all;'
        + 'position:relative;z-index:1;animation:ta-pulse-ring 1.8s ease-out infinite;'
      );
      fbtn.textContent = '⏺';

      fbtn.addEventListener('mouseenter', function() {
        if (fbtn.dataset.mode !== 'stop') this.style.transform = 'scale(1.1)';
        flabel.style.opacity = '1';
      });
      fbtn.addEventListener('mouseleave', function() {
        this.style.transform = 'scale(1)';
        flabel.style.opacity = '0';
      });

      fring.style.position = 'absolute';
      fring.style.top = '-3px';
      fring.style.left = '-3px';
      fring.style.zIndex = '0';

      fwrap.appendChild(fring);
      fwrap.appendChild(fbtn);
      fw.appendChild(flabel);
      fw.appendChild(fwrap);
      return fw;
    }

    function _setFloatMode(mode) {
      var fbtn   = document.getElementById('ta-float-analyze');
      var flabel = document.getElementById('ta-float-label');
      var fring  = document.getElementById('ta-float-ring');
      if (!fbtn) return;
      /* also update the main sidebar analyze button */
      var mainBtn = document.querySelector('#ta-analyze-btn button, button.ta-analyze-btn');
      if (mode === 'stop') {
        fbtn.textContent = '⏹';
        fbtn.style.background = 'linear-gradient(135deg,#7f1d1d,#b91c1c)';
        fbtn.style.boxShadow = '0 4px 20px rgba(127,29,29,0.7)';
        fbtn.style.animation = 'none';
        fbtn.dataset.mode = 'stop';
        flabel.textContent = 'Stop';
        flabel.style.background = 'rgba(127,29,29,0.9)';
        if (fring) fring.style.display = 'block';
        if (mainBtn) { mainBtn.classList.add('ta-running'); mainBtn.textContent = '⏸  Running…'; }
      } else {
        fbtn.textContent = '⏺';
        fbtn.style.background = 'linear-gradient(135deg,#b91c1c,#ef4444)';
        fbtn.style.boxShadow = '0 4px 24px rgba(220,38,38,0.55)';
        fbtn.style.animation = 'ta-pulse-ring 1.8s ease-out infinite';
        fbtn.dataset.mode = 'analyze';
        flabel.textContent = 'Analyze';
        flabel.style.background = 'rgba(185,28,28,0.85)';
        if (fring) fring.style.display = 'none';
        if (mainBtn) { mainBtn.classList.remove('ta-running'); mainBtn.textContent = '⏺  Analyze'; }
      }
    }

    function _watchEta() {
      if (window.__taEtaObs) return;
      var panel = document.getElementById('ta-eta-panel');
      if (!panel) return;
      window.__taEtaObs = true;
      new MutationObserver(function() {
        var isIdle = panel.innerHTML.indexOf('Upload a file or paste a URL') !== -1
                  || panel.innerHTML.indexOf('ta-done-panel') !== -1;
        _setFloatMode(isIdle ? 'analyze' : 'stop');
      }).observe(panel, { childList: true, subtree: true });
    }

    function _ensureFloat() {
      if (!document.body) return;
      if (!document.getElementById('ta-float-wrap')) {
        document.body.appendChild(_buildFloat());
      }
      _watchEta();
    }

    _ensureFloat();
    _setFloatMode('analyze');  // always start in analyze mode
    if (!window.__taFloatObs) {
      window.__taFloatObs = true;
      new MutationObserver(_ensureFloat).observe(document.body, { childList: true });
    }
  }
  /* Run immediately and re-check every 2 s so Gradio re-renders never lose the buttons */
  document.body ? _injectToggle() : document.addEventListener('DOMContentLoaded', _injectToggle);
  setInterval(_injectToggle, 2000);

  /* ── PERMANENT static CSS injected directly into <head> ─────────────────────
     Gradio 6 embeds css=CSS as JSON data in <script> tags and injects it later
     via its own pipeline — we can't rely on it being in the DOM at toggle time.
     We inject all our static CSS here so it's guaranteed to be real CSS. */
  if (!document.getElementById('ta-static')) {
    var ps = document.createElement('style');
    ps.id = 'ta-static';
    ps.textContent = [
      /* Custom checkboxes — visible in both modes */
      'input[type=checkbox]{-webkit-appearance:none!important;appearance:none!important;width:18px!important;height:18px!important;min-width:18px!important;border:2px solid #2563eb!important;border-radius:4px!important;background:#fff!important;cursor:pointer!important;position:relative!important;vertical-align:middle!important;flex-shrink:0!important}',
      'input[type=checkbox]:checked{background:#2563eb!important;border-color:#2563eb!important}',
      'input[type=checkbox]:checked::after{content:""!important;position:absolute!important;left:4px!important;top:1px!important;width:6px!important;height:10px!important;border:2px solid #fff!important;border-top:none!important;border-left:none!important;transform:rotate(45deg)!important;display:block!important}',
      'html.dark input[type=checkbox]{background:#1e293b!important;border-color:#60a5fa!important}',
      'html.dark input[type=checkbox]:checked{background:#3b82f6!important;border-color:#3b82f6!important}',
      '.checkbox-wrap{align-items:center!important;gap:8px!important}',
      /* CSS vars — light defaults for step tracker + ETA panel */
      ':root{--ta-card-bg:#f8fafc;--ta-card-border:#e2e8f0;--ta-card-text:#1e293b;--ta-card-sub:#64748b;--ta-card-val:#111827;',
      '--ta-step-done-bg:#dcfce7;--ta-step-done-bdr:#22c55e;--ta-step-done-clr:#166534;',
      '--ta-step-act-bg:#dbeafe;--ta-step-act-bdr:#2563eb;--ta-step-act-clr:#1d4ed8;',
      '--ta-step-wait-bg:#f1f5f9;--ta-step-wait-bdr:#e2e8f0;--ta-step-wait-clr:#94a3b8;',
      '--ta-conn-line-done:#22c55e;--ta-conn-line-wait:#e2e8f0;--ta-stat-bg:rgba(255,255,255,0.7);',
      '--ta-stat-label:#1e40af;--ta-stat-val:#1d4ed8}',
      /* CSS vars — dark overrides */
      'html.dark{--ta-card-bg:#1e293b;--ta-card-border:#334155;--ta-card-text:#e2e8f0;--ta-card-sub:#94a3b8;--ta-card-val:#f1f5f9;',
      '--ta-step-done-bg:#14532d;--ta-step-done-bdr:#4ade80;--ta-step-done-clr:#4ade80;',
      '--ta-step-act-bg:#1e3a5f;--ta-step-act-bdr:#60a5fa;--ta-step-act-clr:#93c5fd;',
      '--ta-step-wait-bg:#0f172a;--ta-step-wait-bdr:#334155;--ta-step-wait-clr:#475569;',
      '--ta-conn-line-done:#4ade80;--ta-conn-line-wait:#334155;--ta-stat-bg:rgba(15,23,42,0.6);',
      '--ta-stat-label:#93c5fd;--ta-stat-val:#e2e8f0}',
      /* ── Floating analyze button — always on top, always viewport-pinned ── */
      '#ta-float-wrap{z-index:2147483647!important;pointer-events:all!important}',
      /* ── Global typography ── */
      '.gradio-container,.contain,.main{font-family:"Inter",system-ui,-apple-system,sans-serif!important}',
      'body{background:#f4f6fb!important}',
      /* ── Cards / blocks — elevated, consistent radius ── */
      '.block,.form,.padded{border-radius:16px!important;box-shadow:0 1px 3px rgba(0,0,0,0.05),0 4px 16px rgba(0,0,0,0.04)!important;border:1px solid #e8edf4!important;transition:box-shadow 0.2s!important}',
      /* ── Inputs — cleaner focus ring ── */
      'input[type=text],input[type=password],input:not([type]),textarea,select{border-radius:10px!important;border:1.5px solid #e2e8f0!important;transition:border-color 0.18s,box-shadow 0.18s!important;font-family:inherit!important}',
      'input[type=text]:focus,input[type=password]:focus,input:not([type]):focus,textarea:focus{border-color:#3b82f6!important;box-shadow:0 0 0 3px rgba(59,130,246,0.12)!important;outline:none!important}',
      /* ── Labels ── */
      'label>span:first-child,.block-label{font-size:0.82em!important;font-weight:600!important;color:#475569!important;letter-spacing:0.01em!important}',
      '.info{font-size:0.74em!important;color:#6b7280!important}',
      /* ── Tabs — refined underline style ── */
      '.tabs>.tab-nav{border-bottom:2px solid #e8edf4!important;gap:2px!important}',
      '.tabs>.tab-nav button{font-weight:600!important;font-size:0.84em!important;padding:10px 16px!important;border-radius:8px 8px 0 0!important;letter-spacing:0.01em!important;transition:all 0.15s!important;color:#475569!important;background:transparent!important}',
      '.tabs>.tab-nav button.selected{color:#2563eb!important;border-bottom:2px solid #2563eb!important;margin-bottom:-2px!important;background:transparent!important}',
      /* ── Accordions ── */
      '.accordion,.details{border-radius:12px!important;border:1px solid #e8edf4!important}',
      /* ── Analyze button — pulsing red ── */
      '@keyframes ta-pulse-ring{0%{box-shadow:0 0 0 0 rgba(220,38,38,0.55),0 3px 12px rgba(220,38,38,0.35)}60%{box-shadow:0 0 0 10px rgba(220,38,38,0),0 3px 12px rgba(220,38,38,0.35)}100%{box-shadow:0 0 0 0 rgba(220,38,38,0),0 3px 12px rgba(220,38,38,0.35)}}',
      'button.ta-analyze-btn,#ta-analyze-btn{background:linear-gradient(135deg,#b91c1c,#ef4444)!important;color:#fff!important;font-size:0.9em!important;font-weight:700!important;border:none!important;border-radius:8px!important;padding:8px 18px!important;letter-spacing:0.02em!important;cursor:pointer!important;width:100%!important;animation:ta-pulse-ring 1.8s ease-out infinite!important}',
      'button.ta-analyze-btn:hover,#ta-analyze-btn:hover{background:linear-gradient(135deg,#991b1b,#dc2626)!important;transform:translateY(-1px)!important}',
      'button.ta-analyze-btn.ta-running,#ta-analyze-btn.ta-running{background:linear-gradient(135deg,#7f1d1d,#b91c1c)!important;animation:none!important;opacity:0.85!important;cursor:default!important}',
      /* ── Stop / Cancel button ── */
      '#ta-cancel-btn button,.ta-cancel-btn button{background:#dc2626!important;color:#fff!important;border:2px solid #fca5a5!important;border-radius:8px!important;font-size:0.85em!important;font-weight:800!important;letter-spacing:0.03em!important;padding:7px 14px!important;box-shadow:0 3px 12px rgba(220,38,38,0.5),inset 0 1px 0 rgba(255,255,255,0.15)!important;transition:all 0.12s!important;width:100%!important;cursor:pointer!important}',
      '#ta-cancel-btn button:hover,.ta-cancel-btn button:hover{background:#b91c1c!important;border-color:#f87171!important;transform:translateY(-1px)!important;box-shadow:0 5px 18px rgba(220,38,38,0.65)!important}',
      '#ta-cancel-btn button:active,.ta-cancel-btn button:active{transform:translateY(2px)!important;box-shadow:0 1px 4px rgba(220,38,38,0.4)!important;background:#991b1b!important}',
      /* ── Scrollable dropdowns ── */
      '[role=listbox]{max-height:220px!important;overflow-y:auto!important;border-radius:12px!important;box-shadow:0 8px 24px rgba(0,0,0,0.12)!important}',
      '#provider-sel [role=listbox],#model-sel [role=listbox]{max-height:280px!important;overflow-y:auto!important}',
      /* ── File upload area ── */
      '.upload-container{border-radius:14px!important;border:2px dashed #cbd5e1!important;transition:border-color 0.2s!important}',
      '.upload-container:hover{border-color:#3b82f6!important}',
      /* ── Hero — all text uses !important so Gradio light-theme can't override ── */
      '.ta-hero{background:linear-gradient(145deg,#040c1e 0%,#0a1628 30%,#0f2044 60%,#162d6b 100%);border-radius:22px;padding:36px 44px 30px;color:#fff!important;margin-bottom:6px;position:relative;overflow:hidden;box-shadow:0 8px 40px rgba(0,0,0,0.32),0 2px 8px rgba(0,0,0,0.2)}',
      '.ta-hero-blob-tr{position:absolute;top:-70px;right:-50px;width:320px;height:320px;background:radial-gradient(circle,rgba(59,130,246,0.22) 0%,transparent 68%);pointer-events:none}',
      '.ta-hero-blob-bl{position:absolute;bottom:-50px;left:40px;width:240px;height:240px;background:radial-gradient(circle,rgba(99,102,241,0.14) 0%,transparent 65%);pointer-events:none}',
      '.ta-hero-grid{position:absolute;inset:0;background-image:radial-gradient(rgba(255,255,255,0.055) 1px,transparent 1px);background-size:28px 28px;pointer-events:none}',
      '.ta-hero-inner{position:relative}',
      /* header row */
      '.ta-hero-header{display:flex;align-items:center;gap:20px;margin-bottom:22px}',
      '.ta-hero-icon-box{background:linear-gradient(135deg,rgba(255,255,255,0.13),rgba(255,255,255,0.04));border:1px solid rgba(255,255,255,0.2);border-radius:18px;padding:14px 16px;backdrop-filter:blur(12px);flex-shrink:0;box-shadow:inset 0 1px 0 rgba(255,255,255,0.14),0 4px 16px rgba(0,0,0,0.2)}',
      '.ta-hero-eyebrow{font-size:0.67em!important;font-weight:700!important;letter-spacing:0.15em!important;text-transform:uppercase!important;color:rgba(147,197,253,0.95)!important;margin-bottom:5px!important}',
      '.ta-hero-title{font-size:2.05em!important;font-weight:800!important;letter-spacing:-0.04em!important;line-height:1.05!important;background:linear-gradient(110deg,#fff 25%,#bfdbfe 70%,#93c5fd 100%)!important;-webkit-background-clip:text!important;-webkit-text-fill-color:transparent!important;background-clip:text!important}',
      '.ta-hero-sub{color:#cbd5e1!important;font-size:0.83em!important;font-weight:400!important;margin-top:6px!important;-webkit-text-fill-color:#cbd5e1!important}',
      /* stats strip */
      '.ta-hero-stats{display:flex;align-items:center;gap:0;background:rgba(255,255,255,0.10);border:1px solid rgba(255,255,255,0.18);border-radius:12px;padding:12px 20px;margin-bottom:18px;flex-wrap:wrap;row-gap:8px}',
      '.ta-hero-stat{display:flex;flex-direction:column;align-items:center;flex:1;min-width:60px}',
      '.ta-hero-stat-n{font-size:1.3em!important;font-weight:800!important;color:#fff!important;line-height:1!important;letter-spacing:-0.02em!important}',
      '.ta-hero-stat-l{font-size:0.67em!important;font-weight:600!important;color:#e2e8f0!important;text-transform:uppercase!important;letter-spacing:0.06em!important;margin-top:2px!important}',
      '.ta-hero-stat-sep{width:1px;height:32px;background:rgba(255,255,255,0.2);flex-shrink:0;margin:0 4px}',
      /* feature chips */
      '.ta-hero-chips{display:flex;gap:7px;flex-wrap:wrap}',
      '.ta-hero-chip{border-radius:8px;padding:4px 12px;font-size:0.72em!important;font-weight:600!important;letter-spacing:0.02em}',
      '.ta-hc-blue{background:rgba(59,130,246,0.28);border:1px solid rgba(96,165,250,0.4);color:#bfdbfe!important}',
      '.ta-hc-purple{background:rgba(139,92,246,0.25);border:1px solid rgba(167,139,250,0.4);color:#ddd6fe!important}',
      '.ta-hc-indigo{background:rgba(99,102,241,0.28);border:1px solid rgba(129,140,248,0.4);color:#c7d2fe!important}',
      /* ── Stop button wrapper ── */
      '.ta-cancel-btn{flex:0 0 auto!important;min-width:80px!important}',
      /* Status bar fills remaining width */
      '.ta-status-bar{flex:1 1 auto!important;min-width:0!important}',
      /* ── Network monitor panel ── */
      '#ta-net-monitor{transition:all 0.3s}',
      'html.dark #ta-net-monitor .ta-net-card{background:#1e293b!important;border-color:rgba(59,130,246,0.25)!important}',
      '#live-log,#live-log>*{background:var(--ta-log-bg)!important;border-color:var(--ta-log-border)!important}',
      /* ── Download section ── */
      '.ta-dl-wrap{padding:4px 2px}',
      '.ta-dl-desc{font-size:0.82em;color:var(--ta-card-sub);margin:0 0 12px}',
      '.ta-dl-btn{display:flex;align-items:center;gap:12px;border-radius:12px;padding:13px 18px;text-decoration:none;font-weight:600;font-size:0.88em;transition:opacity 0.15s,box-shadow 0.15s}',
      '.ta-dl-btn:hover{opacity:0.88}',
      '.ta-dl-win{background:linear-gradient(135deg,#1d4ed8,#3b82f6);box-shadow:0 4px 14px rgba(29,78,216,0.35)}',
      '.ta-dl-mac{background:linear-gradient(135deg,#15803d,#22c55e);box-shadow:0 4px 14px rgba(22,163,74,0.35)}',
      '.ta-dl-btn-title{font-size:0.95em;font-weight:700;color:#fff}',
      '.ta-dl-btn-sub{font-size:0.75em;font-weight:400;color:rgba(255,255,255,0.88);margin-top:1px}',
      '.ta-dl-update-row{margin-top:14px;padding-top:12px;border-top:1px solid var(--ta-card-border)}',
      '.ta-dl-update-label{font-size:0.78em;font-weight:600;color:var(--ta-card-text);margin:0 0 6px}',
      '.ta-dl-code{font-size:0.74em;background:var(--ta-step-wait-bg);color:var(--ta-card-text);padding:5px 10px;border-radius:7px;border:1px solid var(--ta-card-border)}',
      '.ta-dl-footer{font-size:0.74em;color:var(--ta-card-sub);margin:12px 0 0}',
      /* ── Download panel ── */
      '#ta-dl-accordion{border-radius:14px!important;border:1.5px solid var(--ta-border)!important;margin-top:12px!important}',
      '.ta-dl-panel-header{display:flex;flex-direction:column;gap:3px;padding:4px 0 10px}',
      '.ta-dl-panel-title{font-size:0.92em;font-weight:700;color:var(--ta-text)}',
      '.ta-dl-panel-sub{font-size:0.76em;color:var(--ta-sub)}',
      '#ta-dl-format-sel{border-radius:10px!important}',
      '#ta-dl-active{border-radius:12px!important;border:2px solid var(--ta-accent)!important;background:var(--ta-accent-lt)!important;margin-top:10px!important}',
      '#ta-dl-active .download-link{background:var(--ta-accent)!important;color:#fff!important;border-radius:8px!important;font-weight:600!important}',
      '.ta-dl-divider{height:1px;background:var(--ta-border);margin:14px 0 10px}',
      '#ta-dl-regen-row{align-items:flex-end!important;gap:8px!important}',
      '#ta-pdf-regen-btn{border-radius:8px!important;font-size:0.82em!important}',
      /* ── Changelog ── */
      '.ta-cl-wrap{max-height:360px;overflow-y:auto;padding:2px 4px 4px}',
      '.ta-cl-status{display:flex;align-items:center;gap:10px;padding:10px 14px;background:#f0fdf4;border:1px solid #bbf7d0;border-radius:10px;margin-bottom:14px}',
      '.ta-cl-status-txt{font-size:0.78em;font-weight:600;color:#166534}',
      '.ta-cl-status-badge{font-size:0.7em;font-weight:700;padding:2px 9px;border-radius:20px;background:#22c55e;color:#fff;letter-spacing:0.05em}',
      '.ta-cl-status-badge.outdated{background:#f59e0b}',
      '.ta-cl-entry{position:relative;padding:12px 14px 12px 18px;border-radius:12px;border:1px solid #e8edf4;background:#fff;margin-bottom:10px;border-left:3px solid #e2e8f0}',
      '.ta-cl-entry.ta-cl-latest{background:#eff6ff;border-color:#bfdbfe;border-left-color:#3b82f6}',
      '.ta-cl-meta{display:flex;align-items:center;gap:8px;margin-bottom:8px;flex-wrap:wrap}',
      '.ta-cl-ver{font-size:0.72em;font-weight:800;padding:2px 10px;border-radius:20px;background:#dbeafe;color:#1d4ed8;letter-spacing:0.04em}',
      '.ta-cl-latest .ta-cl-ver{background:#2563eb;color:#fff}',
      '.ta-cl-new-badge{font-size:0.66em;font-weight:700;padding:2px 7px;border-radius:20px;background:#22c55e;color:#fff;letter-spacing:0.06em}',
      '.ta-cl-date{font-size:0.73em;color:#94a3b8;font-weight:500}',
      '.ta-cl-list{margin:0;padding:0 0 0 4px;list-style:none}',
      '.ta-cl-list li{font-size:0.8em;line-height:1.65;color:#374151;padding:1px 0;display:flex;align-items:flex-start;gap:7px}',
      '.ta-cl-list li::before{content:"→";color:#3b82f6;font-weight:700;flex-shrink:0;margin-top:1px;font-size:0.85em}',
      '.ta-cl-latest .ta-cl-list li::before{color:#2563eb}',
      /* ── Scrollbar ── */
      '::-webkit-scrollbar{width:6px;height:6px}',
      '::-webkit-scrollbar-track{background:transparent}',
      '::-webkit-scrollbar-thumb{background:#d1d5db;border-radius:6px}',
      '::-webkit-scrollbar-thumb:hover{background:#9ca3af}',
      /* ── Live log terminal ── */
      '#live-log textarea{background:#0a0f1e!important;color:#86efac!important;font-family:"JetBrains Mono","Courier New",monospace!important;font-size:0.79em!important;border-color:#1e3a5f!important;border-radius:10px!important;line-height:1.7!important}',
      /* ── Interview Vision tab ── */
      '.iv-tab-header{padding:8px 0 14px}',
      '.iv-tab-title{font-size:1.05em;font-weight:700;color:var(--ta-text);margin-bottom:4px}',
      '.iv-tab-sub{font-size:0.82em;color:var(--ta-sub);line-height:1.5}',
      '.iv-form-card{background:var(--ta-surface);border:1.5px solid var(--ta-border);border-radius:16px;padding:18px;display:flex;flex-direction:column;gap:12px;margin-bottom:14px}',
      '.iv-form-card #iv-video-input{border-radius:10px!important;margin:0!important}',
      '#iv-video-input{border-radius:12px!important;margin-bottom:0!important}',
      '#iv-controls-row{gap:8px!important;flex-wrap:wrap!important;margin:0!important}',
      '#iv-analyze-btn{width:100%!important;margin:0!important}',
      '#iv-progress{min-height:0!important}',
      '#iv-scores-panel,#iv-timeline,#iv-summary{margin-bottom:10px!important}',
      '#iv-output-video{border-radius:12px!important;margin-top:4px!important}',
      '.iv-score-card{background:var(--ta-surface);border:1.5px solid var(--ta-border);border-radius:14px;padding:16px 18px;margin-bottom:14px}',
      '.iv-score-card-title{font-size:0.78em;font-weight:800;text-transform:uppercase;letter-spacing:0.08em;color:var(--ta-sub);margin-bottom:12px}',
      '.iv-score-row{display:flex;align-items:center;gap:10px;margin-bottom:8px}',
      '.iv-score-label{font-size:0.82em;color:var(--ta-text);flex:1}',
      '.iv-score-bar-wrap{flex:2;background:var(--ta-step-wait-bg);border-radius:99px;height:8px;overflow:hidden}',
      '.iv-score-bar{height:8px;border-radius:99px;transition:width 0.6s ease}',
      '.iv-score-val{font-size:0.82em;font-weight:700;color:var(--ta-text);min-width:36px;text-align:right}',
      '.iv-score-green{background:#22c55e}',
      '.iv-score-amber{background:#f59e0b}',
      '.iv-score-red{background:#ef4444}',
      '.iv-overall-badge{display:inline-flex;align-items:center;gap:8px;padding:10px 18px;border-radius:99px;font-weight:700;font-size:1.05em;margin-top:4px}',
      '.iv-timeline-wrap{background:var(--ta-surface);border:1.5px solid var(--ta-border);border-radius:14px;padding:16px 18px;margin-bottom:14px}',
      '.iv-timeline-title{font-size:0.78em;font-weight:800;text-transform:uppercase;letter-spacing:0.08em;color:var(--ta-sub);margin-bottom:12px}',
      '.iv-timeline-row{display:flex;align-items:center;gap:10px;margin-bottom:8px}',
      '.iv-timeline-label{font-size:0.78em;color:var(--ta-text);min-width:90px}',
      '.iv-timeline-bar{flex:1;display:flex;height:20px;border-radius:6px;overflow:hidden}',
      '.iv-obs-card{background:var(--ta-surface);border:1.5px solid var(--ta-border);border-radius:14px;padding:16px 18px}',
      '.iv-obs-title{font-size:0.78em;font-weight:800;text-transform:uppercase;letter-spacing:0.08em;color:var(--ta-sub);margin-bottom:10px}',
      '.iv-obs-item{display:flex;gap:8px;align-items:flex-start;margin-bottom:8px;font-size:0.86em;color:var(--ta-text)}',
    ].join('');
    document.head.appendChild(ps);
  }

  /* ── Dynamic dark/light override tag ─────────────────────────────────────────
     Re-appended to END of <head> on every toggle so it always wins cascade ties
     against Gradio's own stylesheets (last stylesheet wins when both !important). */
  var st = document.getElementById('ta-override') || document.createElement('style');
  st.id = 'ta-override';

  /* Gradio 6 CSS variable overrides + element rules. Every color/bg has !important
     so Gradio inline styles can't win. */
  var DARK_RULES = [
    /* CSS variables — set on html.dark so they cascade into all Gradio components */
    'html.dark{color-scheme:dark;color:#e2e8f0!important;background:#0f172a!important;'
      +'--body-background-fill:#0f172a;--background-fill-primary:#0f172a;'
      +'--background-fill-secondary:#1e293b;--block-background-fill:#1e293b;'
      +'--block-border-color:#334155;--block-label-text-color:#94a3b8;'
      +'--input-background-fill:#0f172a;--input-border-color:#475569;'
      +'--panel-background-fill:#1e293b;--panel-border-color:#334155;'
      +'--border-color-primary:#334155;--body-text-color:#e2e8f0;'
      +'--body-text-color-subdued:#94a3b8;--neutral-100:#1e293b;--neutral-200:#334155;'
      +'--neutral-700:#94a3b8;--neutral-800:#cbd5e1;--neutral-900:#e2e8f0;}',
    /* page & containers */
    'html.dark body,html.dark .gradio-container,html.dark .main,html.dark .contain{background:#0f172a!important;color:#e2e8f0!important}',
    /* blocks */
    'html.dark .block,html.dark .form,html.dark .wrap,html.dark .panel-full-width,html.dark .compact,html.dark .upload-container,html.dark .padded{background:#1e293b!important;border-color:#334155!important}',
    /* text — !important so Gradio's inline color= can't override */
    'html.dark span,html.dark p,html.dark div,html.dark h1,html.dark h2,html.dark h3,html.dark h4,html.dark li,html.dark td,html.dark th,html.dark strong,html.dark em{color:#e2e8f0!important}',
    /* labels */
    'html.dark .label-wrap span,html.dark .block-label,html.dark label>span,html.dark .info,html.dark .file-name{color:#94a3b8!important}',
    /* inputs */
    'html.dark input,html.dark textarea,html.dark select,[role=combobox]{background:#0f172a!important;color:#e2e8f0!important;border-color:#475569!important}',
    'html.dark input::placeholder,html.dark textarea::placeholder{color:#64748b!important;opacity:1!important}',
    /* tabs */
    'html.dark .tabs>.tab-nav button{color:#94a3b8!important;background:#1e293b!important;border-color:#334155!important}',
    'html.dark .tabs>.tab-nav button.selected{color:#e2e8f0!important;border-bottom-color:#3b82f6!important;background:#0f172a!important}',
    'html.dark .tabitem{background:#0f172a!important}',
    /* markdown */
    'html.dark .prose,html.dark .markdown{color:#e2e8f0!important;background:transparent!important}',
    'html.dark .prose *,html.dark .markdown *{color:#e2e8f0!important}',
    'html.dark .prose a,html.dark .markdown a{color:#60a5fa!important}',
    'html.dark .prose code,html.dark .markdown code{background:#0f172a!important;color:#86efac!important}',
    /* dropdowns */
    'html.dark [role=listbox]{background:#1e293b!important;border-color:#334155!important}',
    'html.dark [role=option]{color:#e2e8f0!important;background:#1e293b!important}',
    'html.dark [role=option]:hover,html.dark [role=option][aria-selected=true]{background:#334155!important;color:#fff!important}',
    /* accordion */
    'html.dark .accordion,html.dark details{background:#1e293b!important;border-color:#334155!important}',
    'html.dark .accordion .label-wrap,html.dark details summary{color:#e2e8f0!important}',
    'html.dark .checkbox-group label span,html.dark .radio-group label span{color:#cbd5e1!important}',
    'html.dark .file-preview{background:#1e293b!important;color:#e2e8f0!important}',
    'html.dark .dropdown-arrow svg{fill:#94a3b8!important}',
    /* buttons */
    'html.dark button{background:#1e293b!important;border-color:#334155!important;color:#e2e8f0!important}',
    'html.dark button.selected{background:#334155!important}',
    'html.dark button.ta-analyze-btn,html.dark #ta-analyze-btn{background:linear-gradient(135deg,#991b1b,#ef4444)!important;color:#fff!important;border:none!important}',
    /* theme toggle — restore correct colors */
    'html.dark #ta-btn-light{background:transparent!important;color:#94a3b8!important}',
    'html.dark #ta-btn-dark{background:#3b82f6!important;color:#fff!important}',
    /* ── New redesign: block/card shadow in dark ── */
    'html.dark .block,html.dark .form,html.dark .padded{box-shadow:0 1px 3px rgba(0,0,0,0.3),0 4px 16px rgba(0,0,0,0.2)!important;border-color:#2d3a4e!important}',
    /* ── Inputs — dark border + focus ring ── */
    'html.dark input[type=text],html.dark input[type=password],html.dark input:not([type]),html.dark textarea,html.dark select{border:1.5px solid #334155!important;background:#0f172a!important;color:#e2e8f0!important}',
    'html.dark input[type=text]:focus,html.dark input[type=password]:focus,html.dark input:not([type]):focus,html.dark textarea:focus{border-color:#3b82f6!important;box-shadow:0 0 0 3px rgba(59,130,246,0.18)!important}',
    /* ── Tab nav divider ── */
    'html.dark .tabs>.tab-nav{border-bottom-color:#2d3a4e!important}',
    'html.dark .tabs>.tab-nav button.selected{color:#60a5fa!important;border-bottom-color:#3b82f6!important}',
    /* ── Accordion border ── */
    'html.dark .accordion,html.dark .details{border-color:#2d3a4e!important}',
    /* ── Cancel button — dark ── */
    'html.dark button[aria-label="Stop / Cancel"],html.dark button.stop-btn{background:#1e1215!important;color:#f87171!important;border-color:#7f1d1d!important}',
    'html.dark button[aria-label="Stop / Cancel"]:hover,html.dark button.stop-btn:hover{background:#2d1515!important;border-color:#ef4444!important}',
    /* ── Upload drop zone — dark dashed border ── */
    'html.dark .upload-container{border-color:#334155!important;border-style:dashed!important;background:#0f172a!important}',
    'html.dark .upload-container:hover{border-color:#3b82f6!important}',
    /* ── Dropdown listbox shadow — dark ── */
    'html.dark [role=listbox]{box-shadow:0 8px 32px rgba(0,0,0,0.55)!important;border-color:#334155!important}',
    /* ── Labels ── */
    'html.dark label>span:first-child,html.dark .block-label{color:#94a3b8!important}',
    'html.dark .info{color:#64748b!important}',
    /* ── Hero dark mode — add border + stronger glow to separate from dark page ── */
    'html.dark .ta-hero{border:1px solid rgba(59,130,246,0.22)!important;box-shadow:0 8px 48px rgba(0,0,0,0.6),0 0 0 1px rgba(59,130,246,0.08),0 2px 8px rgba(0,0,0,0.4)!important}',
    'html.dark .ta-hero-blob-tr{background:radial-gradient(circle,rgba(59,130,246,0.28) 0%,transparent 68%)!important}',
    'html.dark .ta-hero-blob-bl{background:radial-gradient(circle,rgba(99,102,241,0.2) 0%,transparent 65%)!important}',
    'html.dark .ta-hero-stats{background:rgba(255,255,255,0.04)!important;border-color:rgba(255,255,255,0.08)!important}',
    /* ── Changelog dark mode ── */
    'html.dark .ta-cl-status{background:#14532d!important;border-color:#166534!important}',
    'html.dark .ta-cl-status-txt{color:#4ade80!important}',
    'html.dark .ta-cl-entry{background:#1e293b!important;border-color:#2d3a4e!important;border-left-color:#334155!important}',
    'html.dark .ta-cl-entry.ta-cl-latest{background:#1e3a5f!important;border-color:#1e40af!important;border-left-color:#3b82f6!important}',
    'html.dark .ta-cl-ver{background:#1e3a5f!important;color:#93c5fd!important}',
    'html.dark .ta-cl-latest .ta-cl-ver{background:#2563eb!important;color:#fff!important}',
    'html.dark .ta-cl-date{color:#64748b!important}',
    'html.dark .ta-cl-list li{color:#cbd5e1!important}',
    'html.dark .ta-cl-list li::before{color:#60a5fa!important}',
    'html.dark .ta-cl-latest .ta-cl-list li::before{color:#93c5fd!important}',
    /* ── Download section dark mode ── */
    'html.dark .ta-dl-win{box-shadow:0 4px 20px rgba(29,78,216,0.55)!important}',
    'html.dark .ta-dl-mac{box-shadow:0 4px 20px rgba(22,163,74,0.50)!important}',
    'html.dark .ta-dl-code{background:#0f172a!important;color:#cbd5e1!important;border-color:#334155!important}',
    'html.dark #ta-dl-accordion{border-color:var(--ta-border)!important}',
    'html.dark .ta-dl-panel-title{color:var(--ta-text)!important}',
    'html.dark .ta-dl-panel-sub{color:var(--ta-sub)!important}',
    'html.dark #ta-dl-active{border-color:var(--ta-accent)!important;background:var(--ta-accent-lt)!important}',
    /* ── Pace reference chips ── */
    'html.dark .ta-pace-ref{background:#1e293b!important;border-color:#334155!important}',
    'html.dark .ta-pace-label{color:#94a3b8!important}',
    'html.dark .ta-chip-slow{background:#0f172a!important;border-color:#475569!important;color:#94a3b8!important}',
    'html.dark .ta-chip-normal{background:#1e3a5f!important;border-color:#3b82f6!important;color:#93c5fd!important}',
    'html.dark .ta-chip-fast{background:#3b2d00!important;border-color:#ca8a04!important;color:#fbbf24!important}',
    'html.dark .ta-chip-vfast{background:#450a0a!important;border-color:#991b1b!important;color:#f87171!important}',
    /* scrollbars */
    'html.dark ::-webkit-scrollbar-track{background:#0f172a!important}',
    'html.dark ::-webkit-scrollbar-thumb{background:#334155!important}',
    'html.dark ::-webkit-scrollbar-thumb:hover{background:#475569!important}',
  ].join('');

  /* ── DOM patcher ─────────────────────────────────────────────────────────────
     Sets/removes inline styles directly on elements Gradio styles inline.
     Uses removeProperty() in light mode — setProperty('x','') is a no-op. */
  function _sp(el, prop, val) {
    if (val) el.style.setProperty(prop, val, 'important');
    else     el.style.removeProperty(prop);
  }

  /* _SKIP: elements whose colors we manage separately */
  function _skip(el) {
    return el.closest('#ta-widget') || el.closest('#api-banner') || el.closest('#ta-wake-notice');
  }

  function patchDOM(dark) {
    var bg0 = dark ? '#0f172a' : null;
    var bg1 = dark ? '#1e293b' : null;
    var fg  = dark ? '#e2e8f0' : null;
    var fg2 = dark ? '#94a3b8' : null;
    var bd  = dark ? '#334155' : null;

    /* Page containers */
    document.querySelectorAll('.gradio-container,.main,.contain,body').forEach(function(el){
      _sp(el,'background',bg0); _sp(el,'color',fg);
    });

    /* Blocks, forms, accordions — backgrounds */
    document.querySelectorAll('.block,.form,.wrap,.panel-full-width,.compact,details,summary').forEach(function(el){
      _sp(el,'background',bg1); _sp(el,'border-color',bd);
      if(fg) _sp(el,'color',fg);
    });

    /* Inputs */
    document.querySelectorAll('input,textarea,select').forEach(function(el){
      _sp(el,'background',bg0); _sp(el,'color',fg); _sp(el,'border-color',dark?'#475569':null);
    });

    /* Nuclear text patch — every text node inside .gradio-container gets the
       correct color. We skip: images, buttons, our own widgets, and the banner.
       This ensures accordion labels, info text, step numbers, etc. are visible. */
    var SKIP_TAGS = {IMG:1,VIDEO:1,CANVAS:1,SVG:1,PATH:1,INPUT:1,TEXTAREA:1,SELECT:1};
    document.querySelectorAll('.gradio-container *').forEach(function(el){
      if (SKIP_TAGS[el.tagName]) return;
      if (_skip(el)) return;
      /* Labels/info get subdued color; everything else gets full white */
      var isLabel = el.classList.contains('block-label') ||
                    (el.parentElement && (el.parentElement.classList.contains('label-wrap') ||
                                          el.parentElement.classList.contains('info')));
      _sp(el, 'color', dark ? (isLabel ? '#94a3b8' : '#e2e8f0') : null);
    });

    /* Dropdowns specifically */
    document.querySelectorAll('[role=listbox],[role=option],[role=combobox]').forEach(function(el){
      _sp(el,'background',bg1); _sp(el,'color',fg); _sp(el,'border-color',bd);
    });

    /* Big button — keep it blue */
    document.querySelectorAll('.big-btn button').forEach(function(el){
      _sp(el,'background',dark?'linear-gradient(135deg,#1e40af,#3b82f6)':null);
      _sp(el,'color',dark?'#fff':null);
    });
  }

  /* ── Gradio CSS variable names — set as inline props on <html> ──────────────
     Inline custom properties on documentElement beat ALL :root CSS rules,
     which is how Gradio reads them. This is the only approach that reliably
     overrides Gradio's Soft theme variables regardless of specificity. */
  var DARK_VARS = {
    '--body-background-fill':      '#0f172a',
    '--background-fill-primary':   '#0f172a',
    '--background-fill-secondary': '#1e293b',
    '--block-background-fill':     '#1e293b',
    '--input-background-fill':     '#0f172a',
    '--panel-background-fill':     '#1e293b',
    '--chatbot-background-fill':   '#1e293b',
    '--body-text-color':           '#e2e8f0',
    '--block-label-text-color':    '#94a3b8',
    '--block-title-text-color':    '#e2e8f0',
    '--block-info-text-color':     '#94a3b8',
    '--block-border-color':        '#334155',
    '--block-border-width':        '1px',
    '--input-border-color':        '#475569',
    '--border-color-primary':      '#334155',
    '--border-color-accent':       '#3b82f6',
    '--neutral-100':               '#1e293b',
    '--neutral-200':               '#334155',
    '--neutral-300':               '#475569',
    '--neutral-400':               '#64748b',
    '--neutral-500':               '#94a3b8',
    '--neutral-600':               '#cbd5e1',
    '--neutral-700':               '#e2e8f0',
    '--neutral-800':               '#f1f5f9',
    '--neutral-900':               '#f8fafc',
    '--color-accent':              '#3b82f6',
    '--link-text-color':           '#60a5fa',
    '--shadow-drop':               '0 1px 3px rgba(0,0,0,0.5)',
  };

  function setGradioVars(dark) {
    var root = document.documentElement;
    Object.keys(DARK_VARS).forEach(function(k) {
      if (dark) root.style.setProperty(k, DARK_VARS[k]);
      else      root.style.removeProperty(k);
    });
  }

  /* ── Core apply function ─────────────────────────────────────────────────── */
  function applyTheme(dark) {
    _dark = dark;

    /* 1. Set Gradio CSS vars inline on <html> — beats all :root theme rules */
    setGradioVars(dark);

    /* 2. Toggle .dark class for our html.dark CSS rules */
    document.documentElement.classList.toggle('dark', dark);
    document.body.classList.toggle('dark', dark);

    /* 3. Re-append override sheet last in <head> */
    if (st.parentNode) st.parentNode.removeChild(st);
    document.head.appendChild(st);
    st.textContent = dark ? DARK_RULES : '';

    /* 4. Patch inline styles (handles elements Gradio styles inline) */
    patchDOM(dark);

    /* Direct body/html inline styles — these beat everything */
    _sp(document.body, 'background', dark ? '#0f172a' : null);
    _sp(document.body, 'color',      dark ? '#e2e8f0' : null);
    _sp(document.documentElement, 'background', dark ? '#0f172a' : null);

    localStorage.setItem('ta-dark',      dark ? 'true'  : 'false');
    localStorage.setItem('theme',        dark ? 'dark'  : 'light');
    localStorage.setItem('gradio-theme', dark ? 'dark'  : 'light');

    /* ── Toggle pill visuals — synced via shared helper ── */
    _syncToggleUI();

    /* ── Floating ▶ button — adapts to dark/light ── */
    var fb = document.getElementById('ta-float-analyze');
    if (fb) {
      fb.style.background  = dark
        ? 'linear-gradient(135deg,#1e3a8a,#2563eb)'
        : 'linear-gradient(135deg,#1d4ed8,#3b82f6)';
      fb.style.boxShadow   = dark
        ? '0 4px 20px rgba(37,99,235,0.7)'
        : '0 4px 20px rgba(29,78,216,0.5)';
    }

    /* API banner */
    var banner = document.getElementById('api-banner');
    var title  = document.getElementById('api-banner-title');
    var sub    = document.getElementById('api-banner-sub');
    if (banner && !banner.dataset.state) {
      banner.style.background  = dark ? '#1c1608'        : '#fffcf0';
      banner.style.borderColor = dark ? '#78350f'        : '#fde68a';
      banner.style.boxShadow   = dark ? '0 1px 4px rgba(120,53,15,0.18)' : '0 1px 4px rgba(245,158,11,0.08)';
      if (title) title.style.color = dark ? '#fbbf24' : '#92400e';
      if (sub)   sub.style.color   = dark ? '#d97706' : '#a16207';
    }
  }

  /* ── MutationObservers ───────────────────────────────────────────────────────
     1. Prevent Gradio stripping .dark off <html>
     2. Watch <head> — when Gradio injects a new <style> after ta-override,
        immediately move ta-override back to the END so our rules win cascade
     3. Re-patch DOM when Gradio adds new body nodes */
  new MutationObserver(function(muts) {
    if (!_dark) return;
    muts.forEach(function(m) {
      if (m.attributeName === 'class' && !m.target.classList.contains('dark'))
        m.target.classList.add('dark');
    });
  }).observe(document.documentElement, { attributes: true, attributeFilter: ['class'] });

  /* Watch <head> for new <style> injections — move ta-override to end immediately */
  new MutationObserver(function(muts) {
    if (!_dark) return;
    var newStyle = muts.some(function(m){
      return Array.prototype.some.call(m.addedNodes, function(n){
        return n.nodeType === 1 && n.tagName === 'STYLE' && n.id !== 'ta-override' && n.id !== 'ta-static';
      });
    });
    if (newStyle && st.parentNode) {
      st.parentNode.removeChild(st);
      document.head.appendChild(st);
    }
  }).observe(document.head, { childList: true });

  var _patchTimer = null;
  new MutationObserver(function(muts) {
    if (!_dark) return;
    var hasNodes = muts.some(function(m){ return m.addedNodes.length > 0; });
    if (!hasNodes) return;
    if (_patchTimer) return;
    _patchTimer = setTimeout(function(){ _patchTimer = null; setGradioVars(true); patchDOM(true); }, 50);
  }).observe(document.body || document.documentElement, { childList: true, subtree: true });

  /* Periodic re-apply in dark mode — catches any Gradio re-renders we miss */
  setInterval(function() {
    if (!_dark) return;
    /* Re-append ta-override to ensure it stays last */
    if (st.parentNode && st.parentNode.lastChild !== st) {
      st.parentNode.removeChild(st);
      document.head.appendChild(st);
    }
    setGradioVars(true);
  }, 1000);

  /* ── Init — single init guard prevents double event-listener bug ─────────── */
  var _inited = false;
  function init() {
    if (_inited) return;
    var bl = document.getElementById('ta-btn-light');
    var bd = document.getElementById('ta-btn-dark');
    if (!bl || !bd) { setTimeout(init, 250); return; }
    _inited = true;
    applyTheme(localStorage.getItem('ta-dark') === 'true');
    bl.addEventListener('click', function() { applyTheme(false); });
    bd.addEventListener('click', function() { applyTheme(true);  });
  }
  /* Use event delegation as belt-and-suspenders so re-mounts can't break it */
  document.addEventListener('click', function(e) {
    var t = e.target.closest('#ta-btn-light,#ta-btn-dark');
    if (!t) return;
    applyTheme(t.id === 'ta-btn-dark');
  });
  document.readyState === 'loading'
    ? document.addEventListener('DOMContentLoaded', function() { setTimeout(init, 150); })
    : setTimeout(init, 150);

  /* ── 👁 Password show/hide eye ─────────────────────────────────────────── */
  function addEyes() {
    document.querySelectorAll('input[type="password"]').forEach(function(inp) {
      if (inp.dataset.eye) return;
      inp.dataset.eye = '1';
      var eye = document.createElement('button');
      eye.type = 'button';
      eye.textContent = '👁';
      eye.style.cssText = 'position:absolute;right:10px;top:50%;transform:translateY(-50%);'
        + 'background:none;border:none;cursor:pointer;font-size:1em;opacity:0.5;z-index:20;padding:2px;';
      eye.addEventListener('click', function() {
        var show = inp.type === 'password';
        inp.type = show ? 'text' : 'password';
        eye.textContent  = show ? '🙈' : '👁';
        eye.style.opacity = show ? '0.9' : '0.5';
      });
      inp.parentElement.style.position = 'relative';
      inp.parentElement.appendChild(eye);
    });
  }
  setTimeout(addEyes, 600);
  setTimeout(addEyes, 1800);
  setTimeout(addEyes, 3500);

  /* ── 🔑 API key banner — multi-provider aware ───────────────────────────── */
  var KEY_PROVIDERS = [
    { prefix: 'sk-ant',  name: 'Anthropic'   },
    { prefix: 'sk-',     name: 'OpenAI'      },
    { prefix: 'AIzaSy',  name: 'Google Gemini' },
    { prefix: 'gsk_',    name: 'Groq'        },
    { prefix: 'pplx-',   name: 'Perplexity'  },
    { prefix: 'ollama',  name: 'Ollama'      },
  ];

  function detectProvider(v) {
    for (var i = 0; i < KEY_PROVIDERS.length; i++) {
      if (v.startsWith(KEY_PROVIDERS[i].prefix)) return KEY_PROVIDERS[i].name;
    }
    return null;
  }

  function watchApiKey() {
    var inp = document.querySelector('input[type="password"]');
    if (!inp) { setTimeout(watchApiKey, 600); return; }

    /* Use setProperty(...,'important') so banner colors win over our broad
       html.dark div{color:...!important} rule that would otherwise make
       light text invisible on light-green backgrounds. */
    function _bs(el, prop, val) {
      if (el) el.style.setProperty(prop, val, 'important');
    }

    function refreshBanner() {
      var v        = inp.value.trim();
      var banner   = document.getElementById('api-banner');
      var icon     = document.getElementById('api-banner-icon');
      var title    = document.getElementById('api-banner-title');
      var sub      = document.getElementById('api-banner-sub');
      var badge    = document.getElementById('api-banner-badge');
      var provider = detectProvider(v);

      if (provider || v.length >= 20) {
        /* ── Approved ── */
        banner.dataset.state = 'approved';
        var label = provider ? (provider + ' Key Approved') : 'API Key Set';
        var detail = provider
          ? ('Your <strong>' + provider + '</strong> key is set. Ready to <strong>Analyze File</strong>.')
          : 'Key entered. Ready to <strong>Analyze File</strong>.';
        if (_dark) {
          _bs(banner, 'background', 'linear-gradient(135deg,#052e16,#14532d)');
          _bs(banner, 'border-color', '#22c55e');
          if (title) { title.textContent = label; _bs(title, 'color', '#4ade80'); }
          if (sub)   { sub.innerHTML = detail;    _bs(sub,   'color', '#86efac'); }
        } else {
          _bs(banner, 'background', 'linear-gradient(135deg,#f0fdf4,#dcfce7)');
          _bs(banner, 'border-color', '#22c55e');
          if (title) { title.textContent = label; _bs(title, 'color', '#166534'); }
          if (sub)   { sub.innerHTML = detail;    _bs(sub,   'color', '#15803d'); }
        }
        if (icon)  icon.textContent = '✅';
        if (badge) badge.style.display = 'block';

      } else if (v.length > 0) {
        /* ── Too short ── */
        banner.dataset.state = 'error';
        _bs(banner, 'background',   'linear-gradient(135deg,#fef2f2,#fee2e2)');
        _bs(banner, 'border-color', '#ef4444');
        if (icon)  icon.textContent = '⚠️';
        if (title) { title.textContent = 'Key Too Short'; _bs(title, 'color', '#991b1b'); }
        if (sub)   { sub.innerHTML = 'Paste your full API key (Anthropic, OpenAI, Gemini, Groq, etc.)'; _bs(sub, 'color', '#b91c1c'); }
        if (badge) badge.style.display = 'none';

      } else {
        /* ── Empty ── */
        delete banner.dataset.state;
        if (_dark) {
          _bs(banner, 'background',   'linear-gradient(135deg,#292107,#3b2d00)');
          _bs(banner, 'border-color', '#d97706');
          if (title) { title.textContent = 'API Key Required'; _bs(title, 'color', '#fbbf24'); }
          if (sub)   { sub.innerHTML = 'Enter your API key below. Billed to your account — nothing stored here.'; _bs(sub, 'color', '#fcd34d'); }
        } else {
          _bs(banner, 'background',   'linear-gradient(135deg,#fffbeb,#fef3c7)');
          _bs(banner, 'border-color', '#f59e0b');
          if (title) { title.textContent = 'API Key Required'; _bs(title, 'color', '#92400e'); }
          if (sub)   { sub.innerHTML = 'Enter your API key below (Anthropic, OpenAI, Gemini, Groq, etc.). Billed to your account — nothing stored here.'; _bs(sub, 'color', '#78350f'); }
        }
        if (icon)  icon.textContent = '🔑';
        if (badge) badge.style.display = 'none';
      }
    }

    /* Re-run banner colors whenever dark mode changes */
    var _origApply = applyTheme;
    applyTheme = function(dark) { _origApply(dark); setTimeout(refreshBanner, 50); };

    inp.addEventListener('input',  refreshBanner);
    inp.addEventListener('change', refreshBanner);
    refreshBanner();
  }
  setTimeout(watchApiKey, 800);
  setTimeout(watchApiKey, 2200);

  /* ── 💤 Wake detection — passive indicator, NO auto-reload ─────────────────
     Server-side sleep prevention keeps the CPU running and the connection alive.
     If the system DID suspend (overriding our prevention), we just show a
     "Processing continued in background" notice — no forced reload. */
  (function(){
    var _last = Date.now();
    setInterval(function(){
      var now = Date.now();
      var gap = now - _last;
      _last = now;
      if (gap > 10000) {
        /* Computer was suspended/resumed — show a non-intrusive notice */
        var existing = document.getElementById('ta-wake-notice');
        if (existing) return;
        var n = document.createElement('div');
        n.id = 'ta-wake-notice';
        n.style.cssText = 'position:fixed;bottom:20px;right:20px;z-index:99999;'
          + 'background:#0f172a;color:#e2e8f0;padding:12px 18px;border-radius:10px;'
          + 'font-family:sans-serif;font-size:0.85em;border:1px solid #334155;'
          + 'box-shadow:0 4px 20px rgba(0,0,0,0.5);max-width:280px;';
        n.innerHTML = '💤 Woke from sleep — processing continued in background.'
          + '<br><span style="color:#64748b;font-size:0.85em;">Results will appear when complete.</span>'
          + '<button onclick="this.parentElement.remove()" style="float:right;margin-left:8px;'
          + 'background:none;border:none;color:#94a3b8;cursor:pointer;font-size:1.1em;">✕</button>';
        document.body.appendChild(n);
        /* auto-hide after 8s */
        setTimeout(function(){ if(n.parentElement) n.remove(); }, 8000);
      }
    }, 3000);
  })();

  /* ── 🔄 Background job tracker ──────────────────────────────────────────────
     Saves the current job state to localStorage so if the page crashes or
     the user navigates away, a banner shows on return: "Job still running".
     app.py Python side sets 'ta-job-active' when processing starts/ends. */
  (function(){
    function showJobBanner(msg, sub) {
      if (document.getElementById('ta-job-banner')) return;
      var b = document.createElement('div');
      b.id = 'ta-job-banner';
      b.style.cssText = 'position:fixed;bottom:20px;left:50%;transform:translateX(-50%);'
        + 'z-index:99998;background:#1e3a5f;color:#e2e8f0;padding:14px 22px;border-radius:12px;'
        + 'font-family:sans-serif;font-size:0.88em;font-weight:600;border:1px solid #2563eb;'
        + 'box-shadow:0 4px 20px rgba(0,0,0,0.5);display:flex;align-items:center;gap:12px;max-width:380px;';
      b.innerHTML = '<span style="font-size:1.3em;">⚙️</span>'
        + '<div><div>' + msg + '</div>'
        + '<div style="color:#93c5fd;font-size:0.82em;margin-top:2px;">' + sub + '</div></div>'
        + '<button onclick="this.parentElement.remove()" style="margin-left:auto;background:none;'
        + 'border:none;color:#94a3b8;cursor:pointer;font-size:1.1em;flex-shrink:0;">✕</button>';
      document.body.appendChild(b);
      setTimeout(function(){ if(b.parentElement) b.remove(); }, 12000);
    }

    /* On page load: check if a job was running when the page was last closed */
    var jobInfo = localStorage.getItem('ta-job-running');
    if (jobInfo) {
      try {
        var j = JSON.parse(jobInfo);
        var elapsed = Math.round((Date.now() - j.started) / 1000);
        var mins = Math.floor(elapsed / 60), secs = elapsed % 60;
        showJobBanner(
          'Processing continued in background',
          (j.file || 'File') + ' — ' + mins + 'm ' + secs + 's elapsed. Check outputs or wait for results.'
        );
      } catch(e) {}
    }

    /* ── Stop button tooltip ── */
    (function addStopTooltip() {
      var btn = document.getElementById('ta-cancel-btn');
      if (!btn) { setTimeout(addStopTooltip, 300); return; }
      var b = btn.querySelector('button') || btn;
      b.title = 'Stop transcription';
    })();

    /* Expose helpers for the Python→JS bridge (set via gr.HTML status updates) */
    window.taJobStart = function(filename) {
      localStorage.setItem('ta-job-running', JSON.stringify({ file: filename, started: Date.now() }));
    };
    window.taJobEnd = function() {
      localStorage.removeItem('ta-job-running');
      var b = document.getElementById('ta-job-banner');
      if (b) b.remove();
    };
  })();

  /* ── Live elapsed counter ──────────────────────────────────────────────────
     The server yields every ~1 s so the Elapsed stat looks frozen between
     updates. This JS timer updates #ta-live-elapsed every 200 ms so it
     counts smoothly at all stages.                                         */
  (function(){
    var _t0 = 0, _iv = null;
    function _fmt(s) {
      var m = Math.floor(s/60), sec = Math.floor(s%60);
      return m ? m + 'm ' + (sec < 10 ? '0' : '') + sec + 's' : sec + 's';
    }
    function _tick() {
      var el = document.getElementById('ta-live-elapsed');
      if (el && _t0) el.textContent = _fmt((Date.now() - _t0) / 1000);
    }
    /* Patch taJobStart/taJobEnd — defined just above */
    var _oS = window.taJobStart, _oE = window.taJobEnd;
    window.taJobStart = function(f) {
      _t0 = Date.now();
      if (_iv) clearInterval(_iv);
      _iv = setInterval(_tick, 200);
      if (_oS) _oS(f);
    };
    window.taJobEnd = function() {
      if (_iv) { clearInterval(_iv); _iv = null; }
      _t0 = 0;
      if (_oE) _oE();
    };
  })();

  /* ── 🧠 Remember provider + model choice across sessions ──────────────────
     Saves the user's AI provider and model to localStorage so they never
     have to re-pick them on every visit.                                    */
  (function(){
    var PKEY = 'ta-provider';
    var MKEY = 'ta-model';

    /* Read the currently displayed value from a Gradio dropdown container */
    function dropVal(id) {
      var el = document.getElementById(id);
      if (!el) return '';
      var inp = el.querySelector('input[type="text"],input:not([type])');
      if (inp && inp.value && inp.value.trim()) return inp.value.trim();
      var span = el.querySelector('.selected_wrap span, button span');
      if (span && span.textContent.trim()) return span.textContent.trim();
      var btn = el.querySelector('button');
      if (btn && btn.textContent.trim()) return btn.textContent.trim();
      return '';
    }

    /* Click a named option inside a Gradio dropdown, then call cb when done */
    function pickOption(id, text, cb) {
      var el = document.getElementById(id);
      if (!el || !text) return;
      var trigger = el.querySelector('button');
      if (!trigger) return;
      trigger.click();                       /* open the listbox */
      setTimeout(function() {
        var opts = el.querySelectorAll('[role="option"],li');
        for (var i = 0; i < opts.length; i++) {
          if (opts[i].textContent.trim() === text) {
            opts[i].click();
            if (cb) setTimeout(cb, 800);   /* wait for Gradio server round-trip */
            return;
          }
        }
        trigger.click();                   /* option not found — close */
      }, 200);
    }

    /* Watch a dropdown and persist its value whenever it changes */
    function watchDropdown(id, key) {
      function attach() {
        var el = document.getElementById(id);
        if (!el) { setTimeout(attach, 800); return; }
        var last = '';
        new MutationObserver(function() {
          var v = dropVal(id);
          if (v && v !== last) { last = v; localStorage.setItem(key, v); }
        }).observe(el, { childList: true, subtree: true, characterData: true, attributes: true });
      }
      attach();
    }

    /* On page load: restore saved provider then model */
    function restore() {
      var provider = localStorage.getItem(PKEY);
      var model    = localStorage.getItem(MKEY);
      if (!provider && !model) return;

      function tryRestore(attempt) {
        if (attempt > 20) return;
        var el = document.getElementById('provider-sel');
        if (!el || !el.querySelector('button')) {
          setTimeout(function(){ tryRestore(attempt + 1); }, 600);
          return;
        }
        if (provider && dropVal('provider-sel') !== provider) {
          /* Set provider first; model list updates via server round-trip */
          pickOption('provider-sel', provider, function() {
            if (model) pickOption('model-sel', model);
          });
        } else if (model) {
          pickOption('model-sel', model);
        }
      }
      tryRestore(0);
    }

    watchDropdown('provider-sel', PKEY);
    watchDropdown('model-sel',    MKEY);
    setTimeout(restore, 2000);   /* wait for Gradio to finish rendering */
  })();

  /* ── 💾 Remember all settings (STT engine, checkboxes, language, style…) ───
     Watches every UI control and saves its value to localStorage on change.
     On page load, restores each saved value so the user's last session state
     is exactly preserved.                                                    */
  (function(){
    /* id → { type: 'dropdown'|'checkbox'|'number', key: localStorage key } */
    var FIELDS = {
      'ta-stt-engine':       { type:'dropdown', key:'ta-stt-engine' },
      'ta-stt-model':        { type:'dropdown', key:'ta-stt-model' },
      'ta-language':         { type:'dropdown', key:'ta-language' },
      'ta-report-style':     { type:'dropdown', key:'ta-report-style' },
      'ta-interview-toggle': { type:'checkbox', key:'ta-interview' },
      'ta-interview-deep':   { type:'checkbox', key:'ta-interview-deep' },
      'ta-inc-summary':      { type:'checkbox', key:'ta-inc-summary' },
      'ta-inc-keypoints':    { type:'checkbox', key:'ta-inc-keypoints' },
      'ta-inc-action':       { type:'checkbox', key:'ta-inc-action' },
      'ta-inc-transcript':   { type:'checkbox', key:'ta-inc-transcript' },
      'ta-inc-profiles':     { type:'checkbox', key:'ta-inc-profiles' },
      'ta-inc-analytics':    { type:'checkbox', key:'ta-inc-analytics' },
      'ta-speakers':         { type:'number',   key:'ta-speakers' },
    };

    /* ── Watchers ── */
    var _watching = {};   /* track which IDs already have listeners */

    function attachWatcher(id, cfg) {
      if (_watching[id]) return;
      var el = document.getElementById(id);
      if (!el) return;
      _watching[id] = true;

      if (cfg.type === 'dropdown') {
        var last = (el.querySelector('input.border-none') || {}).value || '';
        new MutationObserver(function() {
          var inp = el.querySelector('input.border-none');
          var v = inp ? inp.value : '';
          if (v && v !== last) { last = v; localStorage.setItem(cfg.key, v); }
        }).observe(el, {childList:true, subtree:true, characterData:true, attributes:true});
      } else if (cfg.type === 'checkbox') {
        var cb = el.querySelector('input[type=checkbox]');
        if (!cb) { _watching[id] = false; return; }
        cb.addEventListener('change', function() {
          localStorage.setItem(cfg.key, cb.checked ? 'true' : 'false');
        });
      } else if (cfg.type === 'number') {
        var ni = el.querySelector('input[type=number]');
        if (!ni) { _watching[id] = false; return; }
        ni.addEventListener('change', function() {
          localStorage.setItem(cfg.key, ni.value);
        });
      }
    }

    function watchField(id, cfg) {
      /* Try immediately, then retry for elements that start hidden */
      (function try_(n) {
        if (_watching[id]) return;
        if (document.getElementById(id)) { attachWatcher(id, cfg); return; }
        if (n < 30) setTimeout(function(){try_(n+1);}, 700);
      })(0);
    }

    /* Document-level observer: attach watchers as soon as elements appear */
    new MutationObserver(function() {
      Object.keys(FIELDS).forEach(function(id) {
        if (!_watching[id] && document.getElementById(id)) {
          attachWatcher(id, FIELDS[id]);
        }
      });
    }).observe(document.body, {childList:true, subtree:true});

    /* ── Restore helpers ── */
    function restoreDropdown(id, savedVal) {
      if (!savedVal) return;
      (function try_(n) {
        var el = document.getElementById(id);
        if (!el) { if (n<25) setTimeout(function(){try_(n+1);},600); return; }
        var inp = el.querySelector('input.border-none');
        if (inp && inp.value === savedVal) return;   /* already correct */
        /* Trigger = input itself (Gradio Svelte dropdowns have no separate button).
           Must dispatch full mouse sequence — Svelte listens on mousedown, not click. */
        var trigger = inp || el.querySelector('button');
        if (!trigger) { if (n<25) setTimeout(function(){try_(n+1);},400); return; }
        ['mousedown','mouseup','click'].forEach(function(ev) {
          trigger.dispatchEvent(new MouseEvent(ev, {bubbles:true, cancelable:true, view:window}));
        });
        setTimeout(function() {
          /* Options render as a Svelte portal outside the component — search body.
             Option text may include a leading checkmark (✓ ) — strip it when matching. */
          var opts = document.body.querySelectorAll('[role=option], .options li, ul[id*=dropdown] li');
          /* Narrow to visible ones */
          var visible = Array.from(opts).filter(function(o) {
            var r = o.getBoundingClientRect();
            return r.width > 0 && r.height > 0;
          });
          var found = false;
          for (var i = 0; i < visible.length; i++) {
            var raw = visible[i].textContent.trim();
            var txt = raw.replace(/^[✓✔✅][ ]*/, '');  /* strip checkmark */
            var dv  = visible[i].getAttribute('data-value') || '';
            if (txt === savedVal || dv === savedVal || raw === savedVal) {
              visible[i].click(); found = true; break;
            }
          }
          if (!found) { trigger.click(); }   /* close without change */
        }, 400);
      })(0);
    }

    function restoreCheckbox(id, savedVal) {
      if (savedVal === null) return;
      var want = savedVal === 'true';
      (function try_(n) {
        var el = document.getElementById(id);
        var cb = el && el.querySelector('input[type=checkbox]');
        if (!cb) { if (n<20) setTimeout(function(){try_(n+1);},500); return; }
        if (cb.checked !== want) {
          /* Click the label/span — Gradio listens on the label, not the input */
          var lbl = el.querySelector('label') || cb.parentElement;
          if (lbl) lbl.click(); else cb.click();
        }
      })(0);
    }

    function restoreNumber(id, savedVal) {
      if (!savedVal) return;
      (function try_(n) {
        var el = document.getElementById(id);
        var ni = el && el.querySelector('input[type=number]');
        if (!ni) { if (n<20) setTimeout(function(){try_(n+1);},500); return; }
        if (ni.value === savedVal) return;
        ni.value = savedVal;
        ['input','change'].forEach(function(ev){
          ni.dispatchEvent(new Event(ev, {bubbles:true}));
        });
      })(0);
    }

    /* Open an accordion by its header text, run cb(), then close it */
    function withAccordion(headerText, cb) {
      var btns = Array.from(document.querySelectorAll('button'));
      var btn  = btns.find(function(b) {
        return b.textContent.trim().toLowerCase().indexOf(headerText.toLowerCase()) >= 0 &&
               b.closest('.accordion, [data-accordion]') !== null ||
               b.parentElement && b.parentElement.classList.contains('label-wrap');
      });
      /* Fallback: any button whose text starts with the header */
      if (!btn) {
        btn = btns.find(function(b){
          return b.textContent.trim().toLowerCase().startsWith(headerText.toLowerCase());
        });
      }
      if (!btn) { cb(); return; }
      var isOpen = btn.getAttribute('aria-expanded') === 'true';
      if (!isOpen) btn.click();
      setTimeout(function() {
        cb();
        if (!isOpen) setTimeout(function(){ btn.click(); }, 500);
      }, 600);
    }

    function restoreAll() {
      /* ── Things always in the DOM ── */
      restoreCheckbox('ta-interview-toggle', localStorage.getItem('ta-interview'));
      restoreCheckbox('ta-interview-deep',   localStorage.getItem('ta-interview-deep'));
      restoreNumber('ta-speakers',           localStorage.getItem('ta-speakers'));
      restoreDropdown('ta-stt-model',        localStorage.getItem('ta-stt-model'));

      /* ── Language accordion ── */
      var langSaved  = localStorage.getItem('ta-language');
      if (langSaved && langSaved !== 'auto') {
        withAccordion('Language', function() {
          restoreDropdown('ta-language', langSaved);
        });
      }

      /* ── Report Format accordion — checkboxes + style ── */
      var anyReport = ['ta-report-style','ta-inc-summary','ta-inc-keypoints',
                       'ta-inc-action','ta-inc-transcript','ta-inc-profiles',
                       'ta-inc-analytics']
                      .some(function(k){ return localStorage.getItem(FIELDS[k] ? FIELDS[k].key : k) !== null; });
      if (anyReport) {
        withAccordion('Report Format', function() {
          restoreDropdown('ta-report-style', localStorage.getItem('ta-report-style'));
          ['ta-inc-summary','ta-inc-keypoints','ta-inc-action',
           'ta-inc-transcript','ta-inc-profiles','ta-inc-analytics'].forEach(function(id) {
            restoreCheckbox(id, localStorage.getItem(FIELDS[id].key));
          });
        });
      }

      /* ── STT engine last — triggers server call to show/hide related fields ── */
      var sttSaved = localStorage.getItem('ta-stt-engine');
      if (sttSaved && sttSaved !== 'Whisper (Local / Offline)') {
        setTimeout(function(){ restoreDropdown('ta-stt-engine', sttSaved); }, 800);
      }
    }

    /* Start watching all fields */
    Object.keys(FIELDS).forEach(function(id) { watchField(id, FIELDS[id]); });
    /* Restore after Gradio has finished rendering */
    setTimeout(restoreAll, 3200);
  })();

  /* ── 🔑 API key — saved per-provider in browser localStorage only ──────────
     The key is NEVER sent to the server for storage. It lives exclusively in
     the user's own browser on their device. Each AI provider gets its own
     slot so switching providers swaps in the right key automatically.       */
  (function(){
    var AKEY = 'ta-apikey-';   /* prefix + provider label, e.g. ta-apikey-Claude (Anthropic) */

    function apiInput() { return document.querySelector('input[type="password"]'); }
    function curProvider() { return localStorage.getItem('ta-provider') || 'Claude (Anthropic)'; }

    /* Save current key under the current provider slot */
    function saveKey() {
      var inp = apiInput(); if (!inp) return;
      var slot = AKEY + curProvider();
      if (inp.value) localStorage.setItem(slot, inp.value);
      else           localStorage.removeItem(slot);
    }

    /* Fill the password field from localStorage (skip if user already typed) */
    function restoreKey(provider) {
      var slot  = AKEY + (provider || curProvider());
      var saved = localStorage.getItem(slot);
      if (!saved) return;
      function trySet(n) {
        if (n > 20) return;
        var inp = apiInput();
        if (!inp) { setTimeout(function(){ trySet(n + 1); }, 400); return; }
        if (inp.value) return;   /* never overwrite what the user manually typed */
        inp.value = saved;
        /* dispatch events so Gradio and the banner JS both react */
        ['input', 'change'].forEach(function(ev) {
          inp.dispatchEvent(new Event(ev, { bubbles: true }));
        });
      }
      trySet(0);
    }

    /* Attach save listeners to the password field */
    function watchApiInput() {
      function attach() {
        var inp = apiInput();
        if (!inp) { setTimeout(attach, 800); return; }
        if (inp.dataset.taSaving) return;   /* avoid duplicate listeners */
        inp.dataset.taSaving = '1';
        inp.addEventListener('input',  saveKey);
        inp.addEventListener('change', saveKey);
      }
      attach();
    }

    /* When the provider changes, clear the field and load the new provider's key */
    function watchProviderForKey() {
      function attach() {
        var el = document.getElementById('provider-sel');
        if (!el) { setTimeout(attach, 800); return; }
        var last = curProvider();
        new MutationObserver(function() {
          var now = localStorage.getItem('ta-provider') || '';
          if (now && now !== last) {
            last = now;
            var inp = apiInput();
            if (inp) inp.value = '';           /* clear the old provider's key */
            setTimeout(function(){ restoreKey(now); }, 1200); /* wait for Gradio to update the field label */
          }
        }).observe(el, { childList: true, subtree: true, characterData: true, attributes: true });
      }
      attach();
    }

    watchApiInput();
    watchProviderForKey();
    setTimeout(function(){ restoreKey(); }, 2800);   /* restore after everything else settles */
  })();

  /* ── ⚡ Instant provider→model swap (no server round-trip) ─────────────────
     Gradio sends the provider change to Python, but we also update the model
     dropdown client-side immediately so there's zero visible lag.            */
  (function(){
    var PROVIDERS = """ + repr({k: v["models"] for k, v in _PROVIDERS.items()}).replace("'", '"') + """;

    function swapModels(providerName) {
      var models = PROVIDERS[providerName];
      if (!models || !models.length) return;

      /* Find the model dropdown by its elem_id wrapper */
      var sel = document.querySelector('#model-sel input, #model-sel [data-testid="dropdown"]');
      var wrap = document.getElementById('model-sel');
      if (!wrap) return;

      /* Update the visible input field immediately (visual only — Python handles state) */
      var input = wrap.querySelector('input');
      if (input) { input.value = models[0]; }
    }

    function wireProviderDrop() {
      var wrap = document.getElementById('provider-sel');
      if (!wrap) { setTimeout(wireProviderDrop, 500); return; }
      wrap.addEventListener('change', function(e){
        swapModels(e.target.value || (wrap.querySelector('input') || {}).value || '');
      });
      /* Also intercept click on listbox options */
      document.addEventListener('click', function(e) {
        var opt = e.target.closest('#provider-sel [role=option]');
        if (opt) swapModels((opt.textContent || '').trim());
      });
    }
    setTimeout(wireProviderDrop, 1000);
  })();

  /* ── ▶ Floating play button — wire click + hover directly on every instance ──
     Svelte blocks event bubbling from inside gr.HTML components, so document
     delegation is unreliable. Wire each button directly instead.              */
  (function(){
    function scrollToResults() {
      var t = document.getElementById('ta-results-tabs') || document.getElementById('ta-eta-panel');
      if (t) t.scrollIntoView({ behavior: 'smooth', block: 'start' });
    }
    function doAnalyze() {
      var btn = document.querySelector('#ta-analyze-btn, button.ta-analyze-btn');
      if (!btn) return;
      btn.dispatchEvent(new MouseEvent('click', {bubbles: true, cancelable: true, view: window}));
      setTimeout(scrollToResults, 300);
    }

    function wireFloatBtn(fb) {
      if (fb.dataset.taWired) return;
      fb.dataset.taWired = '1';
      var label = document.getElementById('ta-float-label');
      fb.addEventListener('click', function() {
        if (fb.dataset.mode === 'stop') {
          var cancelBtn = document.querySelector('#ta-cancel-btn button');
          if (cancelBtn) cancelBtn.click();
        } else {
          doAnalyze();
        }
      });
      fb.addEventListener('mouseenter', function(){
        if (fb.dataset.mode !== 'stop') this.style.transform = 'scale(1.1)';
        if (label) label.style.opacity = '1';
      });
      fb.addEventListener('mouseleave', function(){
        this.style.transform = 'scale(1)';
        if (label) label.style.opacity = '0';
      });
    }

    /* Wire all present instances, then re-check every 800 ms for new ones */
    function wireAll() {
      document.querySelectorAll('#ta-float-analyze').forEach(wireFloatBtn);
      /* Also wire the main Analyze button so clicking it scrolls to results */
      document.querySelectorAll('#ta-analyze-btn, button.ta-analyze-btn').forEach(function(mb) {
        if (mb.dataset.taScrollWired) return;
        mb.dataset.taScrollWired = '1';
        mb.addEventListener('click', function() {
          setTimeout(scrollToResults, 300);
        });
      });
    }
    wireAll();
    setInterval(wireAll, 800);
  })();



  /* ── 🌐 Live Network Speed Monitor ──────────────────────────────────────────
     Tracks upload AND download bytes separately.
     Shows per-direction live speed + session totals, both light & dark mode. */
  (function(){
    var WIN_MS   = 3000;   /* 3-second rolling window for speed calc */
    var _pingMs  = 0;

    /* separate rolling logs for RX (download) and TX (upload) */
    var _rxLog = [], _txLog = [];
    /* session totals (bytes) */
    var _rxTotal = 0, _txTotal = 0;

    /* active upload state */
    var _upLoaded = 0, _upTotal = 0, _upStart = 0, _upActive = false;
    var _upLastPushed = 0;

    function _pushRx(b) { if (b > 0) { _rxLog.push({t:Date.now(),b:b}); _rxTotal += b; } }
    function _pushTx(b) { if (b > 0) { _txLog.push({t:Date.now(),b:b}); _txTotal += b; } }

    function _speed(log) {
      var now = Date.now();
      /* prune old entries in-place */
      var i = 0;
      while (i < log.length && now - log[i].t >= WIN_MS) i++;
      if (i > 0) log.splice(0, i);
      var tot = 0;
      for (var j = 0; j < log.length; j++) tot += log[j].b;
      return tot / (WIN_MS / 1000);
    }

    function fmtSpeed(bps) {
      if (bps >= 1048576) return (bps/1048576).toFixed(1) + ' MB/s';
      if (bps >= 1024)    return (bps/1024).toFixed(0)    + ' KB/s';
      if (bps > 0)        return Math.round(bps)          + ' B/s';
      return '0 B/s';
    }
    function fmtSize(bytes) {
      if (bytes >= 1073741824) return (bytes/1073741824).toFixed(2) + ' GB';
      if (bytes >= 1048576)    return (bytes/1048576).toFixed(1)    + ' MB';
      if (bytes >= 1024)       return (bytes/1024).toFixed(0)       + ' KB';
      return bytes + ' B';
    }

    /* ── Periodic background ping — measures latency + baseline download speed ── */
    var _pingEndpoints = ['/queue/status', '/info', window.location.pathname];
    var _pingIdx = 0;
    (function pingLoop() {
      var url = _pingEndpoints[_pingIdx % _pingEndpoints.length] + '?_t=' + Date.now();
      _pingIdx++;
      var t0 = performance.now();
      fetch(url, { cache: 'no-store' })
        .then(function(r) {
          _pingMs = Math.round(performance.now() - t0);
          var cl = r.headers && r.headers.get('content-length');
          if (cl) _pushRx(parseInt(cl, 10));
          return r.text();
        })
        .then(function(txt) {
          if (txt && txt.length > 0) _pushRx(txt.length);
        })
        .catch(function() { _pingMs = 0; })
        .finally(function() { setTimeout(pingLoop, 3000); });
    })();

    /* bar sparkline — 12 segments */
    function _bars(bps, color) {
      var SEGS = 12, MAX = 5*1048576;
      var fill = Math.min(SEGS, Math.round(bps / MAX * SEGS));
      var out = '<div style="display:flex;align-items:flex-end;gap:2px;height:20px;">';
      for (var i = 0; i < SEGS; i++) {
        var h = i < fill ? Math.max(4, Math.round((i+1)/SEGS*20)) : 3;
        out += '<div style="width:4px;height:' + h + 'px;background:'
             + (i < fill ? color : 'var(--ta-card-border,#e2e8f0)')
             + ';border-radius:2px 2px 0 0;transition:height 0.25s,background 0.25s;"></div>';
      }
      return out + '</div>';
    }

    function _dot(color, active) {
      return '<span style="width:8px;height:8px;background:' + color + ';border-radius:50%;'
           + 'display:inline-block;flex-shrink:0;box-shadow:0 0 6px ' + color + ';'
           + (active ? 'animation:tapulse 1.4s ease-in-out infinite;' : 'opacity:0.3;')
           + '"></span>';
    }

    /* card: icon | direction | big speed | bars | session total */
    function _card(icon, label, bps, total, color, upDetail) {
      var active = bps > 200;
      var parts = fmtSpeed(bps).split(' ');
      var num = parts[0], unit = parts[1] || '';
      return '<div style="flex:1;min-width:0;background:var(--ta-card-bg,#f8fafc);'
           + 'border:1px solid ' + (active ? color + '55' : 'var(--ta-card-border,#e2e8f0)') + ';'
           + 'border-radius:12px;padding:10px 12px;transition:border-color 0.3s;">'
           /* header row */
           + '<div style="display:flex;align-items:center;gap:6px;margin-bottom:6px;">'
           +   _dot(color, active)
           +   '<span style="font-size:0.72em;font-weight:700;text-transform:uppercase;'
           +     'letter-spacing:0.07em;color:var(--ta-card-sub,#64748b);">' + icon + ' ' + label + '</span>'
           + '</div>'
           /* big speed number */
           + '<div style="display:flex;align-items:baseline;gap:3px;margin-bottom:8px;">'
           +   '<span style="font-size:1.6em;font-weight:800;color:' + (active ? color : 'var(--ta-card-sub,#94a3b8)') + ';'
           +     'line-height:1;transition:color 0.3s;">' + num + '</span>'
           +   '<span style="font-size:0.75em;font-weight:600;color:var(--ta-card-sub,#94a3b8);">' + unit + '</span>'
           + '</div>'
           /* sparkline bars */
           + _bars(bps, color)
           /* session total */
           + '<div style="margin-top:6px;font-size:0.7em;color:var(--ta-card-sub,#64748b);">'
           +   'Session&nbsp;<span style="font-weight:700;color:var(--ta-card-text,#475569);">' + fmtSize(total) + '</span>'
           + '</div>'
           + (upDetail || '')
           + '</div>';
    }

    function render() {
      var p = document.getElementById('ta-net-monitor');
      if (!p) return;

      var rxBps = _speed(_rxLog);
      var txBps = _speed(_txLog);

      var rxColor = rxBps > 1048576 ? '#22c55e' : rxBps > 102400 ? '#3b82f6' : '#64748b';
      var txColor = txBps > 1048576 ? '#22c55e' : txBps > 102400 ? '#a855f7' : '#64748b';

      /* upload progress bar when active */
      var upDetail = '';
      if (_upActive && _upTotal > 0) {
        var pct = Math.min(100, _upLoaded / _upTotal * 100);
        var eta = txBps > 0 && _upTotal > _upLoaded ? Math.round((_upTotal - _upLoaded) / txBps) : 0;
        upDetail = '<div style="margin-top:6px;">'
          + '<div style="height:3px;background:var(--ta-card-border,#e2e8f0);border-radius:2px;overflow:hidden;margin-bottom:3px;">'
          + '<div style="width:' + pct.toFixed(0) + '%;height:100%;background:#a855f7;border-radius:2px;transition:width 0.3s;"></div>'
          + '</div>'
          + '<span style="font-size:0.68em;color:var(--ta-card-sub,#64748b);">'
          + pct.toFixed(0) + '%' + (eta > 0 ? ' · ETA ' + eta + 's' : '') + '</span></div>';
      }

      /* ping + connection badge */
      var footer = '';
      var connInfo = '';
      try {
        var nc = navigator.connection || navigator.mozConnection || navigator.webkitConnection;
        if (nc) {
          var et = nc.effectiveType || '';
          var dl = nc.downlink;
          if (et) connInfo += et.toUpperCase();
          if (dl)  connInfo += (connInfo ? ' · ' : '') + dl + ' Mbps';
        }
      } catch(ex) {}
      if (_pingMs > 0 || connInfo) {
        var pingColor = _pingMs < 80 ? '#22c55e' : _pingMs < 200 ? '#f59e0b' : '#ef4444';
        footer = '<div style="display:flex;align-items:center;gap:10px;margin-top:8px;'
               + 'padding-top:8px;border-top:1px solid var(--ta-card-border,#e2e8f0);'
               + 'font-size:0.72em;color:var(--ta-card-sub,#64748b);">'
               + (_pingMs > 0 ?
                   '<span>🏓 Ping&nbsp;<strong style="color:' + pingColor + ';">' + _pingMs + ' ms</strong></span>' : '')
               + (connInfo ? '<span>📶 ' + connInfo + '</span>' : '')
               + '</div>';
      }

      p.innerHTML = (
        '<style>@keyframes tapulse{0%,100%{opacity:1}50%{opacity:0.25}}</style>'
        + '<div style="margin-top:8px;">'
        + '<div style="font-size:0.7em;font-weight:700;text-transform:uppercase;letter-spacing:0.08em;'
        + 'color:var(--ta-card-sub,#94a3b8);margin-bottom:6px;">🌐 Live Network</div>'
        + '<div style="display:flex;gap:8px;">'
        + _card('⬇', 'Download', rxBps, _rxTotal, rxColor)
        + _card('⬆', 'Upload',   txBps, _txTotal, txColor, upDetail)
        + '</div>'
        + footer
        + '</div>'
      );
    }

    /* ── EventSource intercept — measures Gradio SSE stream (main processing traffic) ── */
    (function() {
      var OrigES = window.EventSource;
      if (!OrigES) return;
      function PatchedES(url, init) {
        var es = new OrigES(url, init);
        var origAdd = es.addEventListener.bind(es);
        es.addEventListener = function(evt, fn, opts) {
          origAdd(evt, function(e) {
            try {
              if (e.data) {
                var b = (typeof TextEncoder !== 'undefined')
                  ? new TextEncoder().encode(e.data).length
                  : e.data.length;
                _pushRx(b);
              }
            } catch(ex) {}
            fn.call(this, e);
          }, opts);
        };
        return es;
      }
      PatchedES.prototype = OrigES.prototype;
      PatchedES.CONNECTING = OrigES.CONNECTING;
      PatchedES.OPEN = OrigES.OPEN;
      PatchedES.CLOSED = OrigES.CLOSED;
      window.EventSource = PatchedES;
    })();

    /* ── PerformanceObserver: resource downloads ── */
    if (window.PerformanceObserver) {
      try {
        var _obs = new PerformanceObserver(function(list) {
          list.getEntries().forEach(function(e) {
            var b = e.transferSize > 0 ? e.transferSize : e.encodedBodySize;
            if (b > 0) _pushRx(b);
          });
        });
        _obs.observe({entryTypes: ['resource']});
      } catch(ex) {}
    }

    /* ── Fetch intercept: track upload body + response size ── */
    var _origFetch = window.fetch;
    window.fetch = function(url, opts) {
      /* track request body size as upload */
      if (opts && opts.body) {
        var bSz = 0;
        if (typeof opts.body === 'string')         bSz = opts.body.length;
        else if (opts.body && opts.body.byteLength) bSz = opts.body.byteLength;
        if (bSz > 0) _pushTx(bSz);
      }
      /* Measure response Content-Length when available (safe — no stream clone) */
      return _origFetch.apply(this, arguments).then(function(resp) {
        try {
          var cl = resp.headers && resp.headers.get('content-length');
          if (cl) _pushRx(parseInt(cl, 10));
        } catch(ex) {}
        return resp;
      });
    };

    /* ── XHR intercept: upload progress + download bytes ── */
    var _origOpen = XMLHttpRequest.prototype.open;
    var _origSend = XMLHttpRequest.prototype.send;

    XMLHttpRequest.prototype.open = function(method, url) {
      this._taUrl    = url || '';
      this._taMethod = (method || '').toUpperCase();
      return _origOpen.apply(this, arguments);
    };

    XMLHttpRequest.prototype.send = function(data) {
      var self = this;
      var isUpload = self._taUrl && self._taUrl.indexOf('upload') >= 0
                  && data && (data instanceof FormData || data instanceof Blob
                              || data instanceof ArrayBuffer
                              || (typeof data === 'object' && data.size));
      if (isUpload) {
        _upStart = Date.now(); _upActive = true;
        _upLoaded = 0; _upTotal = 0; _upLastPushed = 0;
        self.upload.addEventListener('progress', function(e) {
          var delta = e.loaded - _upLastPushed;
          if (delta > 0) { _pushTx(delta); _upLastPushed = e.loaded; }
          _upLoaded = e.loaded;
          _upTotal  = e.lengthComputable ? e.total : 0;
        });
        self.upload.addEventListener('load',  function() { _upActive = false; });
        self.upload.addEventListener('error', function() { _upActive = false; });
        self.upload.addEventListener('abort', function() { _upActive = false; });
      }
      /* XHR download bytes */
      self.addEventListener('progress', function(e) {
        var delta = e.loaded - (self._taLastRx || 0);
        if (delta > 0) _pushRx(delta);
        self._taLastRx = e.loaded;
      });
      return _origSend.apply(this, arguments);
    };

    /* ── Start render loop — retry until element exists ── */
    (function startRender() {
      if (!document.getElementById('ta-net-monitor')) {
        setTimeout(startRender, 250); return;
      }
      render();
      setInterval(render, 500);
    })();
  })();

  /* ── 🔌 Server reconnect heartbeat ────────────────────────────────────────
     Pings the server every 4 s.  If 2 consecutive pings fail, shows a
     "Reconnecting…" banner.  When the server comes back, reloads the page
     so Gradio re-initialises cleanly (avoids stale SSE connections).       */
  (function(){
    var _fails   = 0;
    var _banner  = null;

    function _showBanner() {
      if (_banner) return;
      _banner = document.createElement('div');
      _banner.id = 'ta-reconnect-banner';
      _banner.style.cssText = (
        'position:fixed;top:0;left:0;right:0;z-index:99999;'
        + 'background:#1e40af;color:#fff;text-align:center;'
        + 'padding:10px 16px;font-size:0.88em;font-weight:600;'
        + 'display:flex;align-items:center;justify-content:center;gap:10px;'
      );
      _banner.innerHTML = (
        '<span style="display:inline-block;width:10px;height:10px;border:2px solid #fff;'
        + 'border-top-color:transparent;border-radius:50%;animation:ta-spin 0.8s linear infinite;"></span>'
        + '&nbsp;Server restarting — reconnecting automatically…'
      );
      var style = document.createElement('style');
      style.textContent = '@keyframes ta-spin{to{transform:rotate(360deg)}}';
      document.head.appendChild(style);
      document.body.prepend(_banner);
    }

    function _hideBanner() {
      if (_banner) { _banner.remove(); _banner = null; }
    }

    function _ping() {
      fetch(window.location.origin + '/?_hb=' + Date.now(), {
        cache: 'no-store', method: 'HEAD'
      }).then(function(r) {
        if (r.ok || r.status < 500) {
          if (_fails >= 2) {
            /* Server came back — reload so Gradio re-initialises */
            window.location.reload();
          }
          _fails = 0;
          _hideBanner();
        } else {
          _fails++;
        }
      }).catch(function() {
        _fails++;
        if (_fails >= 2) _showBanner();
      });
    }

    setInterval(_ping, 4000);
  })();

})();
"""

_IDLE_STATUS = """
<div style="background:var(--ta-card-bg);border:1.5px dashed var(--ta-card-border);
     border-radius:12px;padding:28px 20px;text-align:center;">
  <div style="font-size:2em;margin-bottom:8px;opacity:0.35;">🎙</div>
  <div style="color:var(--ta-card-text);font-size:0.95em;font-weight:700;margin-bottom:4px;">Ready to process</div>
  <div style="color:var(--ta-card-sub);font-size:0.8em;line-height:1.5;">
    Upload a file, then click <strong style="color:var(--ta-step-act-clr);">Analyze</strong>
  </div>
</div>
"""

_IDLE_LOG = (
    '<div id="ta-log-wrap" style="background:var(--ta-log-bg,#f8fafc);border:1px solid var(--ta-log-border,#cbd5e1);'
    'border-radius:10px;padding:14px 18px;min-height:160px;max-height:320px;'
    'overflow-y:auto;font-family:\'JetBrains Mono\',\'Courier New\',monospace;font-size:0.81em;line-height:1.75;">'
    '<span style="color:var(--ta-log-text,#475569);">Progress and logs appear here…</span>'
    '</div>'
)

_FORMATS = """
<div style="background:var(--ta-card-bg);border:1px solid var(--ta-card-border);
     border-radius:10px;padding:14px 16px;font-family:sans-serif;">
  <div style="display:flex;flex-direction:column;gap:8px;">
    <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap;">
      <span style="font-size:0.78em;font-weight:700;text-transform:uppercase;
            letter-spacing:0.06em;color:var(--ta-card-sub);min-width:38px;">🎵 Audio</span>
      <span style="background:var(--ta-step-act-bg);color:var(--ta-step-act-clr);
            font-size:0.75em;font-weight:600;padding:2px 8px;border-radius:5px;">mp3</span>
      <span style="background:var(--ta-step-act-bg);color:var(--ta-step-act-clr);
            font-size:0.75em;font-weight:600;padding:2px 8px;border-radius:5px;">wav</span>
      <span style="background:var(--ta-step-act-bg);color:var(--ta-step-act-clr);
            font-size:0.75em;font-weight:600;padding:2px 8px;border-radius:5px;">m4a</span>
      <span style="background:var(--ta-step-act-bg);color:var(--ta-step-act-clr);
            font-size:0.75em;font-weight:600;padding:2px 8px;border-radius:5px;">flac</span>
      <span style="background:var(--ta-step-act-bg);color:var(--ta-step-act-clr);
            font-size:0.75em;font-weight:600;padding:2px 8px;border-radius:5px;">ogg</span>
      <span style="background:var(--ta-step-act-bg);color:var(--ta-step-act-clr);
            font-size:0.75em;font-weight:600;padding:2px 8px;border-radius:5px;">aac</span>
    </div>
    <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap;">
      <span style="font-size:0.78em;font-weight:700;text-transform:uppercase;
            letter-spacing:0.06em;color:var(--ta-card-sub);min-width:38px;">🎬 Video</span>
      <span style="background:var(--ta-step-done-bg);color:var(--ta-step-done-clr);
            font-size:0.75em;font-weight:600;padding:2px 8px;border-radius:5px;">mp4</span>
      <span style="background:var(--ta-step-done-bg);color:var(--ta-step-done-clr);
            font-size:0.75em;font-weight:600;padding:2px 8px;border-radius:5px;">mov</span>
      <span style="background:var(--ta-step-done-bg);color:var(--ta-step-done-clr);
            font-size:0.75em;font-weight:600;padding:2px 8px;border-radius:5px;">avi</span>
      <span style="background:var(--ta-step-done-bg);color:var(--ta-step-done-clr);
            font-size:0.75em;font-weight:600;padding:2px 8px;border-radius:5px;">mkv</span>
      <span style="background:var(--ta-step-done-bg);color:var(--ta-step-done-clr);
            font-size:0.75em;font-weight:600;padding:2px 8px;border-radius:5px;">webm</span>
    </div>
    <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap;">
      <span style="font-size:0.78em;font-weight:700;text-transform:uppercase;
            letter-spacing:0.06em;color:var(--ta-card-sub);min-width:38px;">📄 Docs</span>
      <span style="background:var(--ta-step-wait-bg);color:var(--ta-card-text);
            border:1px solid var(--ta-card-border);
            font-size:0.75em;font-weight:600;padding:2px 8px;border-radius:5px;">pdf</span>
      <span style="background:var(--ta-step-wait-bg);color:var(--ta-card-text);
            border:1px solid var(--ta-card-border);
            font-size:0.75em;font-weight:600;padding:2px 8px;border-radius:5px;">docx</span>
      <span style="background:var(--ta-step-wait-bg);color:var(--ta-card-text);
            border:1px solid var(--ta-card-border);
            font-size:0.75em;font-weight:600;padding:2px 8px;border-radius:5px;">txt</span>
      <span style="background:var(--ta-step-wait-bg);color:var(--ta-card-text);
            border:1px solid var(--ta-card-border);
            font-size:0.75em;font-weight:600;padding:2px 8px;border-radius:5px;">md</span>
      <span style="background:var(--ta-step-wait-bg);color:var(--ta-card-text);
            border:1px solid var(--ta-card-border);
            font-size:0.75em;font-weight:600;padding:2px 8px;border-radius:5px;">srt</span>
      <span style="background:var(--ta-step-wait-bg);color:var(--ta-card-text);
            border:1px solid var(--ta-card-border);
            font-size:0.75em;font-weight:600;padding:2px 8px;border-radius:5px;">vtt</span>
    </div>
  </div>
</div>
"""

def _badge(text, style="act"):
    _state = "active" if style == "act" else "done" if style == "done" else "waiting"
    bg, _, clr = _step_vars(_state)
    if style == "wait":
        clr = "var(--ta-card-sub)"
    bdr = "border:1px solid var(--ta-card-border);" if style == "wait" else ""
    return (f'<span style="background:{bg};color:{clr};{bdr}'
            f'font-size:0.72em;font-weight:600;padding:2px 8px;border-radius:5px;white-space:nowrap;">{text}</span>')

def _cap_row(icon, label, items, style="act"):
    return (
        f'<div style="display:flex;align-items:flex-start;gap:10px;padding:7px 0;'
        f'border-bottom:1px solid var(--ta-card-border);">'
        f'<span style="font-size:1.05em;min-width:22px;">{icon}</span>'
        f'<div><div style="font-size:0.68em;font-weight:700;text-transform:uppercase;'
        f'letter-spacing:0.07em;color:var(--ta-card-sub);margin-bottom:4px;">{label}</div>'
        f'<div style="display:flex;gap:4px;flex-wrap:wrap;">'
        + "".join(_badge(i, style) for i in items)
        + '</div></div></div>'
    )

_CAPABILITIES = (
    '<div style="font-family:sans-serif;">'
    + _cap_row("🎵", "Audio", ["mp3","wav","m4a","flac","ogg","aac","opus","wma","amr","aiff","+ more"])
    + _cap_row("🎬", "Video", ["mp4","mov","avi","mkv","webm","flv","wmv","ts","mpg","vob","+ more"], "done")
    + _cap_row("📄", "Documents", ["pdf","docx","txt","md","srt","vtt"], "wait")
    + _cap_row("🎤", "STT engines", ["Whisper","Deepgram","AssemblyAI","OpenAI","Groq","ElevenLabs","+ more"])
    + _cap_row("🤖", "AI providers", ["Claude","OpenAI","Gemini","Groq","Mistral","Together","Perplexity","Ollama"], "done")
    + _cap_row("📤", "Outputs", ["Summary","Transcript","Speaker dialogue","PDF","DOCX","SRT","VTT","JSON"], "wait")
    + '<div style="display:flex;gap:4px;flex-wrap:wrap;padding-top:8px;border-top:1px solid var(--ta-card-border);margin-top:4px;">'
    + "".join(_badge(f, "wait") for f in [
        "Speaker detection","Multi-language","Interview coaching",
        "Live ETA","Transcription-only","URL import","Dark mode"
    ])
    + '</div></div>'
)

_SECTION = lambda label: f'<div class="ta-section-label">{label}</div>'

# ── Changelog ────────────────────────────────────────────────────────────────
_RELEASES = [
    {
        "version": "2.0.3",
        "date": "2026-06-05",
        "notes": [
            "Full Q&A details in UI + DOCX + PDF — what was said, ideal answer, coaching tip, deflection notes, deep analysis",
            "History: Export / Import / Delete entry / Clear all history",
            "GPU acceleration toggle — auto-detects CUDA, used by Whisper + DeepFace + WhisperX",
            "Download section redesigned as clean button grid (PDF, DOCX, Transcript, SRT, JSON...)",
            "STT engine signup links — Deepgram (200h free), Groq, AssemblyAI, ElevenLabs, Rev.ai",
            "Overloaded error retry with exponential backoff (5→15→30→60s), friendly error messages",
            "Video analysis: Haar cascade fallback for Zoom screen recordings, body language, cultural scoring",
            "Combined Final Score: 60% answer quality + 40% delivery with advance likelihood badge",
            "Mac DMG fix: video_analyzer.py + api.py bundled, mediapipe/cv2 in hidden imports",
            "Docker: GPU support (TF_FORCE_GPU_ALLOW_GROWTH), NVIDIA deploy block in compose files",
            "OTA update checker now points to correct GitHub repo",
        ],
    },
    {
        "version": "1.1.73",
        "date": "2026-06-03",
        "notes": [
            "Windows/Mac installers: Microsoft Store stub detection, 32-bit warning, proxy/path guidance",
            "All version numbers synced across app, installers, and both remotes",
            "requirements.txt: added python-dotenv, tqdm, tiktoken, regex, requests; gradio>=6.0.0",
        ],
    },
    {
        "version": "1.1.72",
        "date": "2026-06-03",
        "notes": [
            "Complete Windows/Mac installer overhaul with correct dependencies",
        ],
    },
    {
        "version": "1.1.71",
        "date": "2026-06-03",
        "notes": [
            "Exclude post-interview debrief from scoring — only live interview questions counted",
            "Hide server file path from output log",
        ],
    },
    {
        "version": "1.1.70",
        "date": "2026-06-03",
        "notes": [
            "Dark mode: fix Deflected/Did-Not-Answer badge readability",
            "Dark mode: fix STT API key banner readability",
        ],
    },
    {
        "version": "1.1.69",
        "date": "2026-06-03",
        "notes": [
            "Timestamps now show local computer time (not UTC)",
            "Clicking Analyze or float Play button scrolls to Summary section",
        ],
    },
    {
        "version": "1.1.68",
        "date": "2026-06-03",
        "notes": [
            "Dark mode: fix STT API key required banner colors",
        ],
    },
    {
        "version": "1.1.67",
        "date": "2026-06-03",
        "notes": [
            "Disable extended thinking for transcript analysis — was consuming full token budget leaving no room for JSON output",
        ],
    },
    {
        "version": "1.1.65",
        "date": "2026-06-03",
        "notes": [
            "Speaker profiles now work with Deepgram — standard prompt asks Claude for speaker_map and speaker_profiles",
            "Deepgram/AssemblyAI transcripts include Speaker N: labels so Claude can identify and profile each person",
        ],
    },
    {
        "version": "1.1.64",
        "date": "2026-06-03",
        "notes": [
            "PDF + DOCX language button: now generates both formats, renamed dropdown to 'Output language (PDF & DOCX)'",
            "result_state stores summary/key_points/action_items so DOCX can be rebuilt on re-generation",
        ],
    },
    {
        "version": "1.1.63",
        "date": "2026-06-03",
        "notes": [
            "Dark mode: All Done panel, question cards, Total Time, Coaching Tip, Deep Analysis sections",
        ],
    },
    {
        "version": "1.1.62",
        "date": "2026-06-02",
        "notes": [
            "UTC timestamps in log, Deepgram ETA stage indicators, better AI analysis progress transparency",
            "LAN IP shown on startup for network access",
        ],
    },
    {
        "version": "1.1.61",
        "date": "2026-06-02",
        "notes": [
            "Yield immediately on Analyze click — clears loading indicator instantly",
        ],
    },
    {
        "version": "1.1.60",
        "date": "2026-06-02",
        "notes": [
            "Eliminate queued delay on Analyze click — dedicated concurrency slot",
        ],
    },
    {
        "version": "1.1.59",
        "date": "2026-06-02",
        "notes": [
            "Fix cloud STT model bleeding from Whisper stored value — separate BrowserState per engine",
        ],
    },
    {
        "version": "1.1.58",
        "date": "2026-06-02",
        "notes": [
            "Deepgram: extract audio with ffmpeg before upload — fixes timeout on 3-hour videos",
            "No read timeout — Deepgram processes at ~5x real-time so allow however long it needs",
        ],
    },
    {
        "version": "1.1.55",
        "date": "2026-06-02",
        "notes": [
            "Deepgram: use detect_language=True when no language selected (was hardcoding 'en')",
            "Return actual detected language from Deepgram response",
        ],
    },
    {
        "version": "1.1.54",
        "date": "2026-06-02",
        "notes": [
            "Fix STT API key field not appearing for cloud engines — Gradio DOM visibility fix",
        ],
    },
    {
        "version": "1.1.39",
        "date": "2026-06-02",
        "notes": [
            "Verified: v1.1.38 space confirmed live — all features healthy",
        ],
    },
    {
        "version": "1.1.38",
        "date": "2026-06-01",
        "notes": [
            "Verified: v1.1.37 space confirmed live — Interview Mode, profile upload, float button all healthy",
        ],
    },
    {
        "version": "1.1.37",
        "date": "2026-06-01",
        "notes": [
            "Verified: Interview Mode profile upload, Re-analyze with Profile button, and float ▶ button confirmed live on space",
        ],
    },
    {
        "version": "1.1.36",
        "date": "2026-06-01",
        "notes": [
            "Feat: Interview coaching profile upload — resume/bio personalises 'what you could have said' answers",
            "Feat: Re-analyze with Profile button skips re-transcription, re-runs coaching only",
            "Feat: Float ▶ button tracks viewport center-right as user scrolls",
        ],
    },
    {
        "version": "1.1.35",
        "date": "2026-06-01",
        "notes": [
            "Fix: floating ▶ button moved to top-right; play button now correctly triggers analysis; step tracker made compact",
        ],
    },
    {
        "version": "1.1.34",
        "date": "2026-06-01",
        "notes": [
            "Verified: floating ▶ button follows user at all scroll depths (scrollTop 0–1200+)",
        ],
    },
    {
        "version": "1.1.33",
        "date": "2026-06-01",
        "notes": [
            "Fix: floating ▶ button verified to follow user while scrolling — confirmed via Playwright scroll test",
        ],
    },
    {
        "version": "1.1.32",
        "date": "2026-06-01",
        "notes": [
            "Fix: floating ▶ button now truly follows scroll — position:absolute + scroll-event tracking replaces broken position:fixed (Gradio uses html as scroll container)",
        ],
    },
    {
        "version": "1.1.31",
        "date": "2026-06-01",
        "notes": [
            "Fix: floating ▶ button now truly stays pinned while scrolling — rAF loop replaces scroll-event guard",
            "Fix: Transcription Only mode no longer requires an API key",
        ],
    },
    {
        "version": "1.1.30",
        "date": "2026-06-01",
        "notes": [
            "Interview Mode now optional — visible checkbox, off by default (was always-on)",
            "Fix: transcription-only mode no longer errors on missing output files",
            "Fix: floating ▶ button stays viewport-pinned during scroll — z-index maxed, JS scroll fallback added",
        ],
    },
    {
        "version": "1.1.13",
        "date": "2026-06-01",
        "notes": [
            "Fix: NameError crash — _pre_transcribed now assigned before it is checked",
            "Fix: on_raw_transcript no longer fires twice on cache-hit path (duplicate queue message)",
        ],
    },
    {
        "version": "1.1.12",
        "date": "2026-06-01",
        "notes": [
            "Fix: Elapsed counter now updates live every 200 ms (JS timer) — no longer frozen between server yields",
            "Fix: floating ▶ button dispatches full mousedown→mouseup→click so Gradio/Svelte always triggers analysis",
        ],
    },
    {
        "version": "1.1.11",
        "date": "2026-06-01",
        "notes": [
            "Fix: floating ▶ button now correctly triggers analysis — dispatches full mousedown→mouseup→click sequence that Gradio/Svelte requires",
        ],
    },
    {
        "version": "1.1.10",
        "date": "2026-06-01",
        "notes": [
            "Interview Mode always on — checkboxes removed, mode and deep analysis always active",
            "Windows exe rebuilt and verified: launches cleanly on Windows",
        ],
    },
    {
        "version": "1.1.9",
        "date": "2026-06-01",
        "notes": [
            "Stop button tooltip ('Stop transcription') on hover",
            "Stop button now cancels AI analysis immediately, not just the UI stream",
            "Transcript checkpoint cache — resubmitting the same file skips re-transcription",
            "ETA panel visible from page load (idle step tracker shown before job starts)",
            "Est. Time shown for Loading and Extracting stages",
            "Network monitor ping reduced to 2 s — always shows live data",
        ],
    },
    {
        "version": "1.1.8",
        "date": "2026-06-01",
        "notes": [
            "Fix: pandas and gradio_client now bundled — prevents startup crash on some Windows configurations",
        ],
    },
    {
        "version": "1.1.7",
        "date": "2026-06-01",
        "notes": [
            "Fix: auto-collect version.txt from all Gradio micro-deps (groovy, safehttpx, etc.) — Windows app now launches",
        ],
    },
    {
        "version": "1.1.6",
        "date": "2026-06-01",
        "notes": [
            "Fix: collect_all for gradio + safehttpx data files in bundle",
        ],
    },
    {
        "version": "1.1.5",
        "date": "2026-06-01",
        "notes": [
            "Fix: numpy now bundled — was excluded, caused crash on startup",
        ],
    },
    {
        "version": "1.1.4",
        "date": "2026-06-01",
        "notes": [
            "Fix: missing STT package error now tells user to switch to Whisper (Local) as the quick fix",
        ],
    },
    {
        "version": "1.1.3",
        "date": "2026-06-01",
        "notes": [
            "Fix: Windows python311.dll error — Install-TranscriptAgent.bat extracts to %LOCALAPPDATA% and creates a Desktop shortcut",
            "Fix: running TranscriptAgent.exe from inside the zip no longer fails",
        ],
    },
    {
        "version": "1.1.2",
        "date": "2026-05-31",
        "notes": [
            "Advancement likelihood % — shown at top of Interview Coaching tab (green ≥70%, blue ≥45%, red <45%)",
            "Translate output to — language dropdown above Analyze button to translate transcript & report",
            "Fix: Gradio 6.15.2 rendering bug that silently dropped the translate dropdown from the UI",
        ],
    },
    {
        "version": "1.1.1",
        "date": "2026-05-31",
        "notes": [
            "Fix: Summary, Transcript, and Speaker Dialogue tabs now always populate",
            "Fix: JSON schema reordered — summary/key-points written first so they survive token-limit cuts",
            "Fix: empty transcript fields fall back to raw STT text instead of blank output",
        ],
    },
    {
        "version": "1.1",
        "date": "2026-05-31",
        "notes": [
            "GitHub OTA update checker — auto-detects new releases, Windows + Mac one-click download buttons",
            "Floating ▶ Analyze button — fixed click handler (CSS selector), works on all page states",
            "AI analysis stage — live % progress bar with ETA estimate (elapsed-based curve)",
            "Network monitor — always-on rendering from page load, never shows stale 'Connecting…'",
            "Interview Q&A in History — shows candidate's exact words, score, and deflection flag per question",
            "Transcript Output Language — translate transcript to any language after STT completes",
            "ETA shown at every step — Transcription, AI Analysis, and Complete each display elapsed + remaining",
        ],
    },
    {
        "version": "1.0",
        "date": "2026-05-31",
        "notes": [
            "9 STT engines — Whisper (local/offline), OpenAI, Groq, Deepgram, AssemblyAI, Google, Azure, ElevenLabs, Rev.ai",
            "Interview Mode — per-question scoring (Great/Good/Needs Improvement/Missed), 10-point overall score",
            "Deep Analysis — deflection rate, advancement likelihood, interview coaching guide",
            "Session History — every job saved with tokens, cost, score, and full Q&A replay",
            "Live Network Monitor — real-time upload/download speed, animated bars, always-on ping display",
            "Session Stats — token counts (in/out), estimated cost per model, download MB",
            "New exports — .srt subtitles, .vtt subtitles, .docx Word document",
            "Floating ▶ Analyze button — always visible bottom-right, never lost in scroll",
        ],
    },
    {
        "version": "2.3",
        "date": "2025-05-30",
        "notes": [
            "Windows & Mac native installers (no Docker required)",
            "Download buttons for installers added to the UI",
            "Cancel button to stop transcription mid-run",
            "API key remembered per-provider in browser (local only)",
            "AI provider & model remembered across sessions",
        ],
    },
    {
        "version": "2.2",
        "date": "2025-05-29",
        "notes": [
            "Professional UI redesign — refined hero, cards, tabs, buttons",
            "Full dark mode support for all redesigned elements",
            "Gradio footer, version badge & API tab hidden",
            "Pace reference legend moved to top of Speech Analytics",
        ],
    },
    {
        "version": "2.1",
        "date": "2025-05-15",
        "notes": [
            "Multi-provider LLM support: OpenAI, Gemini, Groq, Mistral, Together AI, Perplexity, Ollama",
            "Claude model selector (Haiku / Sonnet / Opus) with latest public IDs",
            "PDF report export with per-language translation",
            "3-step processing tracker and live ETA panel",
            "37+ languages with regional dialect variants",
            "Indian language support (Hindi, Bengali, Tamil, Telugu, and more)",
        ],
    },
    {
        "version": "2.0",
        "date": "2025-05-01",
        "notes": [
            "Speaker diarization (Panel Mode) via WhisperX",
            "Speech analytics: WPM, pace label, accent detection",
            "API key approval banner with live validation",
            "Docker-based share package (setup.bat / setup.sh)",
        ],
    },
]

APP_VERSION = "2.3.6"

def _build_changelog():
    latest      = _RELEASES[0]["version"]
    is_latest   = APP_VERSION == latest
    badge_cls   = "ta-cl-status-badge" if is_latest else "ta-cl-status-badge outdated"
    badge_text  = f"v{APP_VERSION} — Up to date ✓" if is_latest else f"v{APP_VERSION} — Update available"

    status = (
        f'<div class="ta-cl-status">'
        f'<span style="font-size:1.1em;">{"✅" if is_latest else "🔔"}</span>'
        f'<span class="ta-cl-status-txt">Current version</span>'
        f'<span class="{badge_cls}">{badge_text}</span>'
        f'</div>'
    )

    entries = ""
    for i, r in enumerate(_RELEASES):
        is_first = i == 0
        entry_cls = "ta-cl-entry ta-cl-latest" if is_first else "ta-cl-entry"
        new_badge = '<span class="ta-cl-new-badge">NEW</span>' if is_first else ""
        items = "".join(f"<li><span>{n}</span></li>" for n in r["notes"])
        entries += (
            f'<div class="{entry_cls}">'
            f'  <div class="ta-cl-meta">'
            f'    <span class="ta-cl-ver">v{r["version"]}</span>'
            f'    {new_badge}'
            f'    <span class="ta-cl-date">🗓 {r["date"]}</span>'
            f'  </div>'
            f'  <ul class="ta-cl-list">{items}</ul>'
            f'</div>'
        )

    return f'<div class="ta-cl-wrap">{status}{entries}</div>'

_CHANGELOG_HTML = _build_changelog()

# ── GitHub OTA update checker ─────────────────────────────────────────────────
_GH_RELEASES_REPO = "jayuan101/transcript-agent"
_LATEST_WIN_URL   = ""   # set by _check_github_update, read by _do_in_app_update
_LATEST_VERSION   = ""

def _check_github_update() -> str:
    """Poll GitHub releases API; return update banner HTML or empty string."""
    global _LATEST_WIN_URL, _LATEST_VERSION
    import urllib.request as _ur, json as _json
    try:
        req = _ur.Request(
            f"https://api.github.com/repos/{_GH_RELEASES_REPO}/releases/latest",
            headers={"User-Agent": f"TranscriptAgent/{APP_VERSION}",
                     "Accept": "application/vnd.github.v3+json"},
        )
        with _ur.urlopen(req) as r:
            data = _json.loads(r.read())
        latest_tag = data.get("tag_name", "").lstrip("v")
        if not latest_tag:
            return ""
        try:
            from packaging.version import Version
            newer = Version(latest_tag) > Version(APP_VERSION)
        except Exception:
            newer = latest_tag > APP_VERSION
        if not newer:
            return ""
        assets   = {a["name"]: a["browser_download_url"] for a in data.get("assets", [])}
        win_url  = (assets.get("TranscriptAgent-win64.zip")
                    or assets.get("TranscriptAgent.exe")
                    or data.get("html_url", ""))
        mac_url  = assets.get("TranscriptAgent.dmg") or data.get("html_url", "")
        html_url = data.get("html_url",
                            f"https://github.com/{_GH_RELEASES_REPO}/releases/latest")
        body     = (data.get("body") or "")[:180]
        notes    = body + ("…" if len(data.get("body") or "") > 180 else "")
        _LATEST_WIN_URL = win_url
        _LATEST_VERSION = latest_tag
        return _build_update_banner(latest_tag, win_url, mac_url, html_url, notes)
    except Exception:
        return ""


def _build_update_banner(latest_tag, win_url, mac_url, html_url, notes=""):
    notes_html = (f'<div style="font-size:0.78em;color:var(--ta-card-sub);margin-top:3px;">'
                  f'{notes}</div>') if notes else ""
    return f"""
<div class="ta-update-banner">
  <div style="display:flex;align-items:center;gap:12px;flex-wrap:wrap;">
    <span style="font-size:1.5em;flex-shrink:0;">🔔</span>
    <div style="flex:1;min-width:160px;">
      <div style="font-weight:800;font-size:0.95em;color:var(--ta-card-text);">
        Update available — v{latest_tag}
      </div>
      {notes_html}
    </div>
    <div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center;">
      <button onclick="taClickUpdateBtn(this)" class="ta-upd-btn ta-upd-win" id="ta-upd-now-btn">
        ⬆ Update Now
      </button>
      <a href="{html_url}" target="_blank"
         style="font-size:0.78em;color:#3b82f6;white-space:nowrap;font-weight:600;">
        Release notes →
      </a>
    </div>
  </div>
  <div id="ta-update-progress" style="display:none;margin-top:10px;font-size:0.84em;
       background:rgba(255,255,255,0.6);border-radius:8px;padding:10px 14px;">
    <span id="ta-update-progress-text">⏳ Updating…</span>
  </div>
</div>
"""


def _do_in_app_update():
    """
    Windows exe  → download new zip, write a PowerShell updater, launch it detached, exit.
    Source install → git pull + pip upgrade (existing behaviour).
    """
    import sys as _sys, os as _os, tempfile as _tmp
    import urllib.request as _ur, subprocess as _sp
    from pathlib import Path as _P

    is_bundle = getattr(_sys, "frozen", False)
    is_win    = _sys.platform == "win32"

    def _bann(body, color="#1e40af", bg="#eff6ff", border="#93c5fd"):
        return (f'<div class="ta-update-banner" style="background:{bg};border-color:{border};">'
                f'<div style="font-size:0.88em;color:{color};">{body}</div></div>')

    # ── Windows exe: silent auto-update ──────────────────────────────────────
    if is_bundle and is_win and _LATEST_WIN_URL:
        try:
            tmp_dir  = _P(_tmp.gettempdir()) / "TranscriptAgent-update"
            tmp_dir.mkdir(exist_ok=True)
            zip_path = tmp_dir / "TranscriptAgent-win64.zip"

            yield _bann("⬇️ Downloading update… 0%")

            with _ur.urlopen(_LATEST_WIN_URL, timeout=300) as resp:
                total      = int(resp.headers.get("Content-Length") or 0)
                downloaded = 0
                with open(zip_path, "wb") as fout:
                    while True:
                        chunk = resp.read(131_072)
                        if not chunk:
                            break
                        fout.write(chunk)
                        downloaded += len(chunk)
                        if total:
                            pct = int(downloaded * 100 / total)
                            mb  = downloaded / 1_048_576
                            tmb = total / 1_048_576
                            yield _bann(
                                f'⬇️ Downloading… {pct}%'
                                f'<span style="opacity:0.65;margin-left:8px;">({mb:.1f} / {tmb:.1f} MB)</span>'
                                f'<div style="margin-top:8px;height:6px;background:#dbeafe;border-radius:3px;">'
                                f'<div style="width:{pct}%;height:100%;background:#3b82f6;'
                                f'border-radius:3px;transition:width 0.4s;"></div></div>'
                            )

            yield _bann("✅ Download complete — preparing update…")

            exe_path    = _sys.executable
            install_dir = str(_P(exe_path).parent.parent)
            pid         = _os.getpid()
            ps_path     = tmp_dir / "ta_updater.ps1"

            ps_path.write_text(
                f"$zip = '{zip_path}'\n"
                f"$dir = '{install_dir}'\n"
                f"$exe = '{exe_path}'\n"
                f"$pid = {pid}\n"
                "while (Get-Process -Id $pid -ErrorAction SilentlyContinue)"
                " { Start-Sleep -Milliseconds 500 }\n"
                "Start-Sleep -Seconds 1\n"
                "Expand-Archive -Path $zip -DestinationPath $dir -Force\n"
                "Start-Process -FilePath $exe\n"
                "Remove-Item -Path $zip -Force -ErrorAction SilentlyContinue\n"
                "Remove-Item -LiteralPath $MyInvocation.MyCommand.Path"
                " -Force -ErrorAction SilentlyContinue\n",
                encoding="utf-8",
            )

            _sp.Popen(
                ["powershell", "-WindowStyle", "Hidden",
                 "-ExecutionPolicy", "Bypass", "-File", str(ps_path)],
                creationflags=_sp.DETACHED_PROCESS | _sp.CREATE_NEW_PROCESS_GROUP,
                close_fds=True,
            )

            yield _bann(
                "🚀 Done! The app will close and reopen automatically on the new version…",
                color="#166534", bg="#f0fdf4", border="#86efac",
            )

            import time as _time
            _time.sleep(2)
            _os._exit(0)

        except Exception as _e:
            yield _bann(
                f"⚠️ Auto-update failed: {_e}<br>"
                "Download the latest installer manually from the release page.",
                color="#991b1b", bg="#fef2f2", border="#fca5a5",
            )
            return

    # ── Bundled but not Windows (Mac / Linux) ────────────────────────────────
    if is_bundle:
        yield _bann(
            "ℹ️ Grab the latest version from the link above and reinstall — takes 2 minutes!",
            color="#92400e", bg="#fffbeb", border="#fcd34d",
        )
        return

    # ── Source install: git pull + pip upgrade ────────────────────────────────
    BASE  = _os.path.dirname(_os.path.abspath(__file__))
    lines = []
    ok    = True

    if _os.path.isdir(_os.path.join(BASE, ".git")):
        try:
            r = _sp.run(["git", "-C", BASE, "pull"],
                        capture_output=True, text=True, timeout=60)
            if r.returncode == 0:
                msg = r.stdout.strip().splitlines()[0] if r.stdout.strip() else "OK"
                lines.append(f"✓ Code: {msg}")
            else:
                lines.append(f"⚠ git pull failed — {r.stderr.strip()[:120]}")
                ok = False
        except Exception as e:
            lines.append(f"⚠ git not available — {e}")
    else:
        lines.append("ℹ No .git directory — skipping code pull.")

    req = _os.path.join(BASE, "requirements.txt")
    if _os.path.exists(req):
        try:
            _sp.run([_sys.executable, "-m", "pip", "install",
                     "setuptools", "wheel", "--quiet"],
                    capture_output=True, text=True, timeout=60)
            r2 = _sp.run([_sys.executable, "-m", "pip", "install",
                          "-r", req, "--upgrade", "--quiet"],
                         capture_output=True, text=True, timeout=300)
            if r2.returncode == 0:
                lines.append("✓ Python packages upgraded.")
            else:
                lines.append(f"⚠ pip upgrade had warnings — {r2.stderr.strip()[:120]}")
        except Exception as e:
            lines.append(f"⚠ pip failed — {e}")
            ok = False

    color  = "#166534" if ok else "#991b1b"
    bg     = "#f0fdf4" if ok else "#fef2f2"
    border = "#86efac" if ok else "#fca5a5"
    msg    = "✅ Update applied! Close this window and reopen the app." if ok else "⚠ Update incomplete."
    items  = "".join(f'<li style="margin:3px 0;">{l}</li>' for l in lines)
    yield (f'<div class="ta-update-banner" style="background:{bg};border-color:{border};">'
           f'<div style="font-weight:800;font-size:0.95em;color:{color};margin-bottom:6px;">{msg}</div>'
           f'<ul style="margin:0;padding-left:18px;font-size:0.82em;color:{color};">{items}</ul>'
           f'</div>')

# ── Desktop download section ──────────────────────────────────────────────────
_HF_RAW = "https://huggingface.co/spaces/Coastline6/transcript-agent-v2/resolve/main"

_DOWNLOAD_SECTION = f"""
<div class="ta-dl-wrap">
  <p class="ta-dl-desc">
    Install Transcript Agent on your own computer — no Docker, no cloud.
    Your files stay private on your machine.
  </p>

  <div style="display:flex;flex-direction:column;gap:10px;">

    <a class="ta-dl-btn ta-dl-win" href="{_HF_RAW}/setup_windows.bat" download="setup_windows.bat">
      <span style="font-size:1.5em;flex-shrink:0;">🪟</span>
      <div>
        <div class="ta-dl-btn-title">Download for Windows</div>
        <div class="ta-dl-btn-sub">setup_windows.bat &nbsp;·&nbsp; Double-click to install</div>
      </div>
    </a>

    <a class="ta-dl-btn ta-dl-mac" href="{_HF_RAW}/setup_mac.sh" download="setup_mac.sh">
      <span style="font-size:1.5em;flex-shrink:0;">🍎</span>
      <div>
        <div class="ta-dl-btn-title">Download for Mac</div>
        <div class="ta-dl-btn-sub">setup_mac.sh &nbsp;·&nbsp; Run in Terminal to install</div>
      </div>
    </a>

  </div>

  <div class="ta-dl-update-row">
    <p class="ta-dl-update-label">Already installed? Run the installer again to update:</p>
    <div style="display:flex;gap:8px;flex-wrap:wrap;">
      <code class="ta-dl-code">Windows: setup_windows.bat → choose [2] Update</code>
      <code class="ta-dl-code">Mac: ./setup_mac.sh → choose [2] Update</code>
    </div>
  </div>

  <p class="ta-dl-footer">
    🔒 Runs entirely on your machine &nbsp;·&nbsp;
    No account needed &nbsp;·&nbsp;
    Bring your own API key
  </p>
</div>
"""

# ── UI ──────────────────────────────────────────────────────────────────────────

with gr.Blocks(title=f"Transcript Agent v{APP_VERSION}") as demo:

    gr.HTML(_HERO)
    gr.HTML(_API_BANNER)
    update_banner = gr.HTML(value="", elem_id="ta-update-banner-wrap")
    _hidden_update_btn = gr.Button("_upd", elem_id="ta-hidden-update-btn")
    # Theme toggle pill — rendered as static HTML, styled to fixed top-right.
    # Click handlers wired below via .click(fn=None, js=...) which IS executed by Gradio 6.x.
    gr.HTML(_THEME_TOGGLE)

    # ── Browser-persisted settings (single BrowserState per setting) ───────────
    # stt_engine is intentionally excluded — restoring it via demo.load() triggers
    # stt_engine.change() → _toggle_and_save_stt → causes STT Model to disappear.
    bsr_whisper  = bsw_whisper  = gr.BrowserState("base",   storage_key="ta-bs-whisper")
    bsr_stt_model= bsw_stt_model= gr.BrowserState(None,    storage_key="ta-bs-stt-model")
    bsr_language = bsw_language = gr.BrowserState("auto",   storage_key="ta-bs-language")
    bsr_style    = bsw_style    = gr.BrowserState("formal", storage_key="ta-bs-style")
    bsr_interview= bsw_interview= gr.BrowserState(True,     storage_key="ta-bs-interview")
    bsr_deep     = bsw_deep     = gr.BrowserState(True,     storage_key="ta-bs-deep")
    bsw_stt      = gr.BrowserState("whisper_local",         storage_key="ta-bs-stt")
    bsr_inc_sum  = bsw_inc_sum  = gr.BrowserState(True,     storage_key="ta-bs-inc-sum")
    bsr_inc_kp   = bsw_inc_kp   = gr.BrowserState(True,     storage_key="ta-bs-inc-kp")
    bsr_inc_ac   = bsw_inc_ac   = gr.BrowserState(True,     storage_key="ta-bs-inc-ac")
    bsr_inc_tr   = bsw_inc_tr   = gr.BrowserState(True,     storage_key="ta-bs-inc-tr")
    bsr_inc_pr   = bsw_inc_pr   = gr.BrowserState(True,     storage_key="ta-bs-inc-pr")
    bsr_inc_an   = bsw_inc_an   = gr.BrowserState(True,     storage_key="ta-bs-inc-an")
    bsr_speakers = bsw_speakers = gr.BrowserState(None,     storage_key="ta-bs-speakers")
    bsr_gpu      = bsw_gpu      = gr.BrowserState(None,     storage_key="ta-bs-gpu")

    with gr.Row():
        provider_dropdown = gr.Dropdown(
            label="AI Provider",
            choices=list(_PROVIDERS.keys()),
            value="Claude (Anthropic)",
            scale=1,
            elem_id="provider-sel",
        )
        model_dropdown = gr.Dropdown(
            label="Model",
            choices=_PROVIDERS["Claude (Anthropic)"]["models"],
            value=_PROVIDERS["Claude (Anthropic)"]["models"][0],
            scale=2,
            allow_custom_value=True,
            elem_id="model-sel",
        )

    user_api_key = gr.Textbox(
        label="Claude (Anthropic) API Key",
        placeholder="sk-ant-api03-…",
        type="password",
        info="console.anthropic.com → API keys → Create key",
    )

    with gr.Row(equal_height=False):

        # ── left sidebar ──────────────────────────────────────────────────────
        with gr.Column(scale=1, min_width=0):

            gr.HTML(_SECTION("Step 1 — Upload"))
            file_input = gr.File(
                label="Drag & drop a file or click to browse",
                file_types=SUPPORTED,
                type="filepath",
            )
            gr.HTML("""
<style>
.ta-large-file-warn{margin:6px 0 2px;padding:8px 10px;border-radius:8px;font-size:0.78em;line-height:1.5;
  background:#fefce8;border:1px solid #fbbf24;color:#78350f;}
@media(prefers-color-scheme:dark){.ta-large-file-warn{background:#422006;border-color:#d97706;color:#fde68a;}}
</style>
<div class="ta-large-file-warn">
  ⚠️ <strong>Large file? Paste the path below instead of uploading.</strong> Files &gt;500 MB will time out on upload.
</div>""")
            path_input = gr.Textbox(
                label="Paste file path or URL (large files — no upload, no timeout)",
                placeholder='e.g.  C:\\Videos\\interview.mp4  or  https://example.com/recording.webm',
            )
            path_input_2 = gr.Textbox(
                label="Part 2 — paste second file path to merge into one transcript (optional)",
                placeholder='e.g.  C:\\Videos\\interview_part2.mp4',
            )
            image_input = gr.File(visible=False, type="filepath", elem_id="ta-image-input")
            with gr.Accordion("⚡ What we support", open=False):
                gr.HTML(_CAPABILITIES)

            gr.HTML(_SECTION("Step 2 — Configure"))
            with gr.Accordion("Processing Options", open=True):
                speakers_input = gr.Number(
                    label="Number of speakers (optional)",
                    value=None, minimum=0, maximum=20, step=1,
                    info="Leave blank or 0 to auto-detect. AI will label each speaker.",
                    elem_id="ta-speakers",
                )
                stt_engine_input = gr.Dropdown(
                    label="STT Engine",
                    choices=[(v, k) for k, v in STT_ENGINES.items()],
                    value="whisper_local",
                    elem_id="ta-stt-engine",
                )
                stt_key_banner = gr.HTML(value="", visible=True, elem_id="ta-stt-key-banner")
                stt_model_input = gr.Dropdown(
                    label="Whisper model size",
                    choices=_WHISPER_SIZES,
                    value="base",
                    info="tiny = fastest · turbo ≈ large speed  |  large-v3 = most accurate",
                    visible=True,
                    allow_custom_value=True,
                    elem_id="ta-stt-model",
                )
                stt_key_input = gr.Textbox(
                    label="STT API Key",
                    placeholder="API key for the selected cloud STT engine",
                    type="password",
                    info="🔒 Saved in your browser only — never stored on this server",
                    visible=True,
                    elem_id="ta-stt-key-input",
                )
                # ── GPU toggle — detect NVIDIA / Apple Silicon / AMD / Intel ──
                # run.bat sets TA_GPU_DEVICE so we can use its detection result
                _ta_gpu_env    = os.environ.get("TA_GPU_DEVICE", "")
                _gpu_available = False
                _gpu_label     = "no GPU detected"
                _gpu_mismatch  = False  # True = GPU present but wrong torch build
                try:
                    import torch as _torch_chk
                    if _torch_chk.cuda.is_available():
                        _gpu_available = True
                        _n = _torch_chk.cuda.get_device_name(0)
                        _gpu_label = f"NVIDIA CUDA — {_n}"
                    elif (hasattr(_torch_chk.backends, "mps")
                          and _torch_chk.backends.mps.is_available()):
                        _gpu_available = True
                        _gpu_label = "Apple Silicon MPS"
                    else:
                        try:
                            import torch_directml as _dml_chk
                            _gpu_available = True
                            _gpu_label = "AMD/Intel DirectML"
                        except ImportError:
                            pass
                        # Check if NVIDIA GPU exists but torch is CPU build
                        if not _gpu_available and "+cpu" in getattr(_torch_chk, "__version__", ""):
                            try:
                                import subprocess as _sp
                                _smi = _sp.run(
                                    ["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"],
                                    capture_output=True, text=True, timeout=3)
                                if _smi.returncode == 0 and _smi.stdout.strip():
                                    _gpu_mismatch = True
                                    _gpu_label = f"NVIDIA GPU found ({_smi.stdout.strip().splitlines()[0]}) — wrong PyTorch build"
                            except Exception:
                                pass
                except Exception:
                    pass
                # Override with launcher/run.bat detection if available
                if not _gpu_available and _ta_gpu_env in ("cuda","mps","dml"):
                    _gpu_available = True
                    _gpu_label = os.environ.get("TA_GPU_NAME") or {
                        "cuda":"NVIDIA CUDA","mps":"Apple Silicon MPS","dml":"AMD/Intel DirectML"
                    }.get(_ta_gpu_env, _ta_gpu_env)
                # ── GPU badge ─────────────────────────────────────────────────
                if _gpu_available:
                    _badge_bg, _badge_bdr, _badge_clr = "#dcfce7", "#22c55e", "#15803d"
                    _badge_icon, _badge_title         = "⚡", "GPU Active"
                    _dark_bg, _dark_bdr, _dark_clr    = "#052e16", "#16a34a", "#4ade80"
                elif _gpu_mismatch:
                    _badge_bg, _badge_bdr, _badge_clr = "#fff7ed", "#f97316", "#9a3412"
                    _badge_icon, _badge_title         = "⚠️", "GPU Mismatch"
                    _dark_bg, _dark_bdr, _dark_clr    = "#431407", "#ea580c", "#fdba74"
                else:
                    _badge_bg, _badge_bdr, _badge_clr = "#f1f5f9", "#cbd5e1", "#475569"
                    _badge_icon, _badge_title         = "🖥", "CPU Mode"
                    _dark_bg, _dark_bdr, _dark_clr    = "#1e293b", "#334155", "#94a3b8"
                gr.HTML(f"""
<style>
.ta-gpu-badge{{display:flex;align-items:center;gap:10px;padding:10px 14px;
  border-radius:10px;margin-bottom:8px;
  background:{_badge_bg};border:1px solid {_badge_bdr};}}
html.dark .ta-gpu-badge{{background:{_dark_bg}!important;border-color:{_dark_bdr}!important;}}
.ta-gpu-badge-icon{{font-size:1.4em;line-height:1;}}
.ta-gpu-badge-title{{font-size:0.72em;font-weight:700;text-transform:uppercase;
  letter-spacing:0.07em;color:{_badge_clr};}}
html.dark .ta-gpu-badge-title{{color:{_dark_clr}!important;}}
.ta-gpu-badge-name{{font-size:0.85em;font-weight:700;color:#111827;margin-top:1px;}}
html.dark .ta-gpu-badge-name{{color:#f1f5f9!important;}}
</style>
<div class="ta-gpu-badge">
  <span class="ta-gpu-badge-icon">{_badge_icon}</span>
  <div>
    <div class="ta-gpu-badge-title">{_badge_title}</div>
    <div class="ta-gpu-badge-name">{_gpu_label}</div>
    {'<div style="font-size:0.75em;margin-top:4px;color:#9a3412;">PyTorch CPU build installed — run setup_windows.bat → Fix GPU (option 5) to enable CUDA.</div>' if _gpu_mismatch else ''}
  </div>
</div>""")
                gpu_toggle = gr.Checkbox(
                    label="Enable GPU acceleration",
                    value=_gpu_available,
                    info=("Whisper 5-10x faster, DeepFace & Ollama on GPU. Uncheck to force CPU."
                          if _gpu_available else
                          "No GPU found. Supports NVIDIA (CUDA), AMD/Intel (DirectML), Apple Silicon (MPS)."),
                    elem_id="ta-gpu-toggle",
                )
                panel_toggle = gr.Checkbox(value=False, visible=False)

            with gr.Accordion("🎤 Interview Mode", open=False):
                interview_toggle = gr.Checkbox(label="Enable Interview Mode — score every question and generate a coaching guide", value=False, elem_id="ta-interview-toggle")
                interview_deep   = gr.Checkbox(label="Deep Analysis (deflection rate, advancement likelihood)", value=False, elem_id="ta-interview-deep")
                gr.HTML("""
<div style="margin-top:10px;padding:10px 12px;background:var(--ta-card-bg,#f8fafc);
     border:1px solid var(--ta-card-border,#e2e8f0);border-radius:10px;">
  <div style="font-size:0.78em;font-weight:700;color:var(--ta-stat-label,#1e40af);margin-bottom:4px;">
    💼 Candidate Profile <span style="font-weight:400;color:var(--ta-card-sub,#64748b);">(optional)</span>
  </div>
  <div style="font-size:0.72em;color:var(--ta-card-sub,#64748b);line-height:1.5;">
    Upload a resume or bio for personalised "what you could have said" answers that reference
    your actual experience, projects, and background.
  </div>
</div>""")
                profile_upload = gr.File(
                    label="Resume / Profile (PDF, DOCX, or TXT)",
                    file_types=[".pdf", ".docx", ".doc", ".txt", ".md"],
                    file_count="single",
                    type="filepath",
                )
                profile_text_state = gr.State(value="")

            with gr.Accordion("Language", open=False):
                language_input = gr.Dropdown(
                    label="Audio language (input)",
                    choices=LANGUAGES,
                    value="auto",
                    elem_id="ta-language",
                )
                # Pre-load every possible variant value so Gradio never rejects
                # a value update because the new value isn't in the current choices.
                _all_variant_choices = [
                    (label, val)
                    for variants in LANGUAGE_VARIANTS.values()
                    for label, val in variants
                ]
                language_variant = gr.Dropdown(
                    label="Regional variant / dialect",
                    choices=_all_variant_choices,
                    value=None,
                    visible=True,
                    interactive=False,
                    info="Select a language above to unlock regional variants",
                )

            with gr.Accordion("Report Format", open=False):
                report_style = gr.Dropdown(
                    label="Writing style",
                    choices=[
                        ("Formal — professional, structured",  "formal"),
                        ("Casual — conversational, friendly",  "casual"),
                        ("Executive brief — ultra concise",    "executive"),
                        ("Bullet-heavy — minimal prose",       "bullet"),
                    ],
                    value="formal",
                    elem_id="ta-report-style",
                )
                gr.HTML("""
<div style="font-size:0.7em;font-weight:700;text-transform:uppercase;
     letter-spacing:0.1em;color:var(--ta-card-sub);margin:8px 0 4px;">
  Include in report
</div>
<div style="background:var(--ta-card-bg);border:1px solid var(--ta-card-border);
     border-radius:8px;padding:10px 14px;font-size:0.88em;">
  <div style="color:var(--ta-card-sub);font-size:0.78em;margin-bottom:6px;">
    Tick what to include — untick to skip
  </div>
</div>""")
                with gr.Row():
                    with gr.Column(min_width=130):
                        inc_summary    = gr.Checkbox(label="Summary",          value=True, elem_id="ta-inc-summary")
                        inc_key_points = gr.Checkbox(label="Key points",       value=True, elem_id="ta-inc-keypoints")
                        inc_action     = gr.Checkbox(label="Action items",     value=True, elem_id="ta-inc-action")
                    with gr.Column(min_width=130):
                        inc_transcript = gr.Checkbox(label="Full transcript",  value=True, elem_id="ta-inc-transcript")
                        inc_profiles   = gr.Checkbox(label="Speaker profiles", value=True, elem_id="ta-inc-profiles")
                        inc_analytics  = gr.Checkbox(label="Speech analytics", value=True, elem_id="ta-inc-analytics")

            transcript_output_lang = gr.Dropdown(
                label="Translate output to",
                choices=_PDF_LANGUAGES,
                value="Same as source",
                info="Translate transcript & report to a different language",
                elem_id="ta-output-lang",
            )

            gr.HTML("""
<div style="font-size:0.7em;font-weight:700;text-transform:uppercase;
     letter-spacing:0.1em;color:var(--ta-card-sub);margin:8px 0 4px;">
  Step 3 — Run
</div>""")
            transcription_only_toggle = gr.Checkbox(
                label="Transcription only (skip AI analysis)",
                value=False,
                elem_id="ta-transcription-only",
            )
            process_btn = gr.Button(
                "⏺  Analyze",
                variant="primary", size="sm",
                elem_classes=["ta-analyze-btn"],
                elem_id="ta-analyze-btn",
            )

            result_state = gr.State(value=None)

            download_accordion = gr.Accordion("⬇  Download Results", open=True, elem_id="ta-dl-accordion")
            with download_accordion:
                dl_waiting = gr.HTML(
                    '<div style="padding:10px 4px;text-align:center;">'
                    '<div style="font-size:1.5em;margin-bottom:6px;">📂</div>'
                    '<div style="font-size:0.85em;font-weight:600;color:#475569;">Run an analysis to generate your reports</div>'
                    '<div style="font-size:0.78em;color:#94a3b8;margin-top:4px;">'
                    'PDF · DOCX · Transcript · SRT · JSON — all appear here when done</div>'
                    '</div>',
                    elem_id="ta-dl-waiting"
                )
                # ── Reports row (PDF + DOCX — colored/formatted) ─────────────
                gr.HTML('<div style="font-size:0.72em;font-weight:700;text-transform:uppercase;'
                        'letter-spacing:.08em;color:#94a3b8;margin-bottom:6px;" id="ta-dl-hdr-reports">'
                        '📄 Reports — with color &amp; formatting</div>')
                with gr.Row():
                    dl_pdf   = gr.DownloadButton(label="📑 Download PDF",        value=None, visible=False,
                                                 variant="primary", size="sm", elem_id="ta-dl-pdf")
                    dl_docx  = gr.DownloadButton(label="📝 Download DOCX (Word)", value=None, visible=False,
                                                 variant="primary", size="sm", elem_id="ta-dl-docx")
                    dl_report= gr.DownloadButton(label="📋 Markdown (plain)",    value=None, visible=False,
                                                 variant="secondary", size="sm", elem_id="ta-dl-report")
                # ── Transcripts row ───────────────────────────────────────────
                gr.HTML('<div style="font-size:0.72em;font-weight:700;text-transform:uppercase;'
                        'letter-spacing:.08em;color:#94a3b8;margin:10px 0 6px;">'
                        '🎙 Transcripts — plain text</div>')
                with gr.Row():
                    dl_transcript = gr.DownloadButton(label="📄 Transcript .txt",      value=None, visible=False,
                                                      variant="secondary", size="sm", elem_id="ta-dl-transcript")
                    dl_speakers   = gr.DownloadButton(label="🎙 Speaker Dialogue .txt", value=None, visible=False,
                                                      variant="secondary", size="sm", elem_id="ta-dl-speakers")
                    dl_combined   = gr.DownloadButton(label="📦 Combined .txt",         value=None, visible=False,
                                                      variant="secondary", size="sm", elem_id="ta-dl-combined")
                # ── Subtitles & Data row ──────────────────────────────────────
                gr.HTML('<div style="font-size:0.72em;font-weight:700;text-transform:uppercase;'
                        'letter-spacing:.08em;color:#94a3b8;margin:10px 0 6px;">'
                        '🎬 Subtitles &amp; Data — plain text</div>')
                with gr.Row():
                    dl_srt  = gr.DownloadButton(label="🎬 SRT Subtitles", value=None, visible=False,
                                                variant="secondary", size="sm", elem_id="ta-dl-srt")
                    dl_vtt  = gr.DownloadButton(label="🎬 VTT Subtitles", value=None, visible=False,
                                                variant="secondary", size="sm", elem_id="ta-dl-vtt")
                    dl_json = gr.DownloadButton(label="🗂 Raw JSON",      value=None, visible=False,
                                                variant="secondary", size="sm", elem_id="ta-dl-json")
                # ── Regen controls (hidden until ready) ───────────────────────
                pdf_lang_input     = gr.Dropdown(label="Output language (PDF & DOCX)", choices=_PDF_LANGUAGES,
                                                 value="Same as source", visible=False, elem_id="ta-dl-lang-sel")
                pdf_regen_btn      = gr.Button("↺  Regenerate PDF & DOCX", visible=False, elem_id="ta-pdf-regen-btn")
                report_format_radio= gr.Radio(choices=["PDF","DOCX"], value="PDF", label="Report format",
                                              interactive=True, visible=False)
                dl_active          = gr.State(value=None)
                dl_format_dropdown = gr.State(value=None)

        # ── results panel ─────────────────────────────────────────────────────
        with gr.Column(scale=2):

            # ETA panel first — most important live feedback, scroll target on Analyze
            eta_panel   = gr.HTML(value=_eta_panel_html("idle"), elem_id="ta-eta-panel")

            with gr.Row(equal_height=True):
                status_bar = gr.HTML(
                    value=_IDLE_STATUS,
                    elem_classes=["ta-status-bar"],
                    elem_id="ta-status-bar",
                )
                cancel_btn = gr.Button(
                    "⏹  Stop",
                    variant="stop",
                    size="sm",
                    min_width=80,
                    elem_classes=["ta-cancel-btn"],
                    elem_id="ta-cancel-btn",
                )
            log_out     = gr.HTML(
                value='<div id="ta-log-wrap" style="'
                      'background:var(--ta-log-bg,#f8fafc);border:1px solid var(--ta-log-border,#cbd5e1);border-radius:10px;'
                      'padding:14px 18px;min-height:160px;max-height:320px;'
                      'overflow-y:auto;font-family:\'JetBrains Mono\',\'Courier New\',monospace;'
                      'font-size:0.81em;line-height:1.75;">'
                      '<span style="color:var(--ta-log-text,#475569);">Progress and logs appear here…</span>'
                      '</div>',
                elem_id="live-log",
                label="Live Processing Log",
            )
            stats_panel = gr.HTML(value="", elem_id="ta-stats-panel")
            net_monitor = gr.HTML(
                value=(
                    '<style>@keyframes tapulse{0%,100%{opacity:1}50%{opacity:0.25}}</style>'
                    '<div style="margin-top:8px;">'
                    '<div style="font-size:0.7em;font-weight:700;text-transform:uppercase;letter-spacing:0.08em;'
                    'color:var(--ta-card-sub,#94a3b8);margin-bottom:6px;">🌐 Live Network</div>'
                    '<div style="display:flex;gap:8px;">'
                    # Download card
                    '<div style="flex:1;background:var(--ta-card-bg,#f8fafc);border:1px solid var(--ta-card-border,#e2e8f0);border-radius:12px;padding:10px 12px;">'
                    '<div style="display:flex;align-items:center;gap:6px;margin-bottom:6px;">'
                    '<span style="width:8px;height:8px;background:#64748b;border-radius:50%;display:inline-block;opacity:0.3;"></span>'
                    '<span style="font-size:0.72em;font-weight:700;text-transform:uppercase;letter-spacing:0.07em;color:var(--ta-card-sub,#64748b);">⬇ Download</span>'
                    '</div>'
                    '<div style="display:flex;align-items:baseline;gap:3px;margin-bottom:8px;">'
                    '<span style="font-size:1.6em;font-weight:800;color:var(--ta-card-sub,#94a3b8);line-height:1;">0</span>'
                    '<span style="font-size:0.75em;font-weight:600;color:var(--ta-card-sub,#94a3b8);">B/s</span>'
                    '</div>'
                    '<div style="font-size:0.7em;color:var(--ta-card-sub,#64748b);">Session <strong>0 B</strong></div>'
                    '</div>'
                    # Upload card
                    '<div style="flex:1;background:var(--ta-card-bg,#f8fafc);border:1px solid var(--ta-card-border,#e2e8f0);border-radius:12px;padding:10px 12px;">'
                    '<div style="display:flex;align-items:center;gap:6px;margin-bottom:6px;">'
                    '<span style="width:8px;height:8px;background:#64748b;border-radius:50%;display:inline-block;opacity:0.3;"></span>'
                    '<span style="font-size:0.72em;font-weight:700;text-transform:uppercase;letter-spacing:0.07em;color:var(--ta-card-sub,#64748b);">⬆ Upload</span>'
                    '</div>'
                    '<div style="display:flex;align-items:baseline;gap:3px;margin-bottom:8px;">'
                    '<span style="font-size:1.6em;font-weight:800;color:var(--ta-card-sub,#94a3b8);line-height:1;">0</span>'
                    '<span style="font-size:0.75em;font-weight:600;color:var(--ta-card-sub,#94a3b8);">B/s</span>'
                    '</div>'
                    '<div style="font-size:0.7em;color:var(--ta-card-sub,#64748b);">Session <strong>0 B</strong></div>'
                    '</div>'
                    '</div></div>'
                ),
                elem_id="ta-net-monitor",
            )

            with gr.Tabs(elem_id="ta-results-tabs"):
                with gr.TabItem("Summary"):
                    summary_out = gr.Markdown(value="")

                with gr.TabItem("Transcript"):
                    transcript_out = gr.Textbox(
                        lines=28, buttons=["copy"], label="",
                        placeholder="Clean transcript will appear here…",
                    )

                with gr.TabItem("Speaker Dialogue"):
                    dialogue_out = gr.Textbox(
                        lines=28, buttons=["copy"], label="",
                        placeholder="Speaker-labelled dialogue will appear here…",
                    )

                with gr.TabItem("Speaker Profiles"):
                    profiles_out = gr.Markdown(
                        value="_Enable Panel Mode to generate speaker profiles._"
                    )

                with gr.TabItem("Speech Analytics"):
                    analytics_out = gr.Markdown(
                        value="_Speech rate and accent analysis will appear here after processing._"
                    )

                with gr.TabItem("🎥 Interview Analysis"):

                    # ── Coaching results (auto-populated on ▶ Analyze) ────────────
                    interview_out = gr.HTML(
                        value='<p style="color:#94a3b8;padding:12px;">Enable <b>Interview Mode</b> in the sidebar and click <b>▶ Analyze</b> to see coaching results here.</p>'
                    )

                    # Roles configured in Interview Mode sidebar; analysis runs via ▶ Analyze
                    _IV_ROLES = ["Candidate","Interviewer 1","Interviewer 2","Interviewer 3","Late Joiner"]
                    iv_role_0 = gr.Dropdown(choices=_IV_ROLES, value="Candidate",     label="Person 1 role", visible=False)
                    iv_role_1 = gr.Dropdown(choices=_IV_ROLES, value="Interviewer 1", label="Person 2 role", visible=False)
                    iv_role_2 = gr.Dropdown(choices=_IV_ROLES, value="Interviewer 2", label="Person 3 role", visible=False)
                    iv_role_3 = gr.Dropdown(choices=_IV_ROLES, value="Interviewer 3", label="Person 4 role", visible=False)
                    iv_person_count = gr.State(value=2)
                    iv_scan_status  = gr.State(value=None)
                    iv_analyze_btn  = gr.State(value=None)
                    iv_thumb_0 = iv_thumb_1 = iv_thumb_2 = iv_thumb_3 = gr.State(value=None)

                    # ── Results ───────────────────────────────────────────────────
                    iv_progress     = gr.HTML(value="", elem_id="iv-progress")
                    iv_scores_panel = gr.HTML(value="", elem_id="iv-scores-panel")
                    iv_timeline     = gr.HTML(value="", elem_id="iv-timeline")
                    iv_summary      = gr.HTML(value="", elem_id="iv-summary")
                    iv_output_video = gr.Video(
                        label="Annotated video", elem_id="iv-output-video",
                        interactive=False, visible=False,
                    )

                    # Compatibility stubs
                    va_inline_video = gr.State(value=None)
                    va_video_in     = gr.State(value=None)
                    va_analyze_btn  = gr.State(value=None)
                    va_status_html  = gr.State(value=None)
                    va_score_html   = gr.State(value=None)
                    va_timeline_plt = gr.State(value=None)
                    va_video_out    = gr.State(value=None)

                with gr.TabItem("📂 History"):
                    with gr.Row():
                        history_refresh_btn = gr.Button("🔄 Refresh", size="sm", scale=1)
                        history_export_btn  = gr.DownloadButton(
                            "⬇ Export", value=None, visible=True,
                            variant="secondary", size="sm", scale=1,
                            elem_id="ta-history-export",
                        )
                        history_import_file = gr.File(
                            label="⬆ Import (.jsonl)", file_types=[".jsonl",".json"],
                            visible=True, scale=2, height=50,
                        )
                        history_delete_btn  = gr.Button(
                            "🗑 Delete Selected", variant="stop", size="sm", scale=1,
                            interactive=False, elem_id="ta-history-delete",
                        )
                        history_clear_btn   = gr.Button(
                            "🗑 Clear All", variant="stop", size="sm", scale=1,
                            elem_id="ta-history-clear",
                        )
                    history_action_status = gr.HTML(value="", visible=False)
                    history_selected_id   = gr.State(value=None)   # id of selected row
                    history_table = gr.Dataframe(
                        headers=["Date", "File", "STT Engine", "STT (s)", "Provider", "Tokens", "Cost", "Score", "Verdict"],
                        datatype=["str","str","str","number","str","str","str","str","str"],
                        interactive=False,
                        wrap=True,
                    )
                    history_selected_summary = gr.Markdown(value="", label="Session Summary")

                with gr.TabItem("Copy All"):
                    combined_out = gr.Textbox(
                        lines=30, buttons=["copy"], label="",
                        placeholder="All sections combined will appear here…",
                    )

    # ── Footer — changelog link on HF website, update banner on desktop ─────────
    _ON_HF = bool(os.environ.get("SPACE_ID"))

    _changelog_link = (
        'View changelog on '
        '<a href="https://huggingface.co/spaces/Coastline6/transcript-agent/blob/main/CHANGELOG.md"'
        '   target="_blank" style="color:#3b82f6;font-weight:600;">GitHub →</a>'
        if _ON_HF else ""
    )

    gr.HTML(f"""
    <div style="text-align:center;padding:20px 0 4px;font-size:0.76em;color:#94a3b8;">
      Transcript Agent &nbsp;&bull;&nbsp; Transcription by OpenAI Whisper
      &nbsp;&bull;&nbsp; Analysis by Anthropic Claude
      &nbsp;&bull;&nbsp; Files processed privately on your machine
      {"&nbsp;&bull;&nbsp;" + _changelog_link if _changelog_link else ""}
    </div>
    <div style="text-align:center;padding:8px 0 20px;">
      <a href="https://paypal.me/jay247616"
         target="_blank"
         style="display:inline-flex;align-items:center;gap:8px;background:#0070ba;color:#fff;font-size:0.85em;font-weight:600;padding:9px 20px;border-radius:20px;text-decoration:none;">
        <svg xmlns="http://www.w3.org/2000/svg" width="18" height="18" viewBox="0 0 24 24" fill="white">
          <path d="M7.076 21.337H2.47a.641.641 0 0 1-.633-.74L4.944.901C5.026.382 5.474 0 5.998 0h7.46c2.57 0 4.578.543 5.69 1.81 1.01 1.15 1.304 2.42 1.012 4.287-.023.143-.047.288-.077.437-.983 5.05-4.349 6.797-8.647 6.797h-2.19c-.524 0-.968.382-1.05.9l-1.12 7.106zm14.146-14.42a3.35 3.35 0 0 0-.607-.541c-.013.076-.026.175-.041.254-.93 4.778-4.005 7.201-9.138 7.201h-2.19a.563.563 0 0 0-.556.479l-1.187 7.527h-.506l-.24 1.516a.56.56 0 0 0 .554.647h3.882c.46 0 .85-.334.922-.788.06-.26.76-4.852.816-5.09a.932.932 0 0 1 .923-.788h.58c3.76 0 6.705-1.528 7.565-5.946.36-1.847.174-3.388-.777-4.471z"/>
        </svg>
        Donation
      </a>
    </div>
    """)

    # ── Interview Vision helpers ──────────────────────────────────────────────
    def _build_iv_scores_html(result: dict) -> str:
        persons     = result.get("persons", {})
        interaction = result.get("interaction", {})

        def _bar_class(v):
            if v >= 70:
                return "iv-score-green"
            if v >= 50:
                return "iv-score-amber"
            return "iv-score-red"

        def _score_row(label, value):
            cls  = _bar_class(value)
            return (
                f'<div class="iv-score-row">'
                f'<span class="iv-score-label">{label}</span>'
                f'<div class="iv-score-bar-wrap">'
                f'<div class="iv-score-bar {cls}" style="width:{value}%"></div>'
                f'</div>'
                f'<span class="iv-score-val">{value}</span>'
                f'</div>'
            )

        html_parts = []
        for pid in sorted(persons.keys()):
            p    = persons[pid]
            role = p.get("role", f"Person {pid}")
            scores = p.get("scores", {})
            talk   = p.get("talk_pct", 0)
            dom    = p.get("dominant_emotion", "neutral")
            html_parts.append(
                f'<div class="iv-score-card">'
                f'<div class="iv-score-card-title">{role}</div>'
            )
            for k, v in scores.items():
                html_parts.append(_score_row(k.replace("_", " ").title(), int(v)))
            html_parts.append(_score_row("Talk time %", int(talk)))
            html_parts.append(
                f'<div style="margin-top:8px;font-size:0.78em;color:var(--ta-sub);">'
                f'Dominant emotion: <b>{dom}</b></div>'
                f'</div>'
            )

        # Interaction card
        rapport = interaction.get("rapport", 0)
        tb      = interaction.get("talk_balance", 0)
        overall = interaction.get("overall", 0)
        cls_o   = _bar_class(overall)
        html_parts.append(
            f'<div class="iv-score-card">'
            f'<div class="iv-score-card-title">Interaction</div>'
        )
        html_parts.append(_score_row("Rapport", rapport))
        html_parts.append(_score_row("Talk balance", tb))
        html_parts.append(
            f'<div style="margin-top:10px;">'
            f'<span class="iv-overall-badge" style="background:{"#22c55e" if overall>=70 else "#f59e0b" if overall>=50 else "#ef4444"};color:#fff;">'
            f'Overall: {overall}</span></div></div>'
        )
        return "".join(html_parts)

    def _build_iv_timeline_html(result: dict) -> str:
        persons      = result.get("persons", {})
        duration     = result.get("duration_secs", 60)
        emo_colors   = {
            "happy":     "#22c55e",
            "neutral":   "#94a3b8",
            "surprised": "#f59e0b",
            "fear":      "#ef4444",
            "angry":     "#ef4444",
            "sad":       "#3b82f6",
            "disgusted": "#a855f7",
        }
        total_mins  = max(1, int(duration / 60))

        parts = [
            '<div class="iv-timeline-wrap">',
            '<div class="iv-timeline-title">Emotion Timeline</div>',
        ]

        for pid in sorted(persons.keys()):
            p    = persons[pid]
            role = p.get("role", f"Person {pid}")
            tl   = p.get("emotions_timeline", [])

            # Build minute-buckets
            buckets: dict[int, list] = {}
            for entry in tl:
                m = int(entry["t"] / 60)
                buckets.setdefault(m, []).append(entry["emotion"])

            from collections import Counter
            parts.append(
                f'<div class="iv-timeline-row">'
                f'<span class="iv-timeline-label">{role}</span>'
                f'<div class="iv-timeline-bar">'
            )
            for m in range(total_mins):
                emos   = buckets.get(m, ["neutral"])
                dom_e  = Counter(emos).most_common(1)[0][0]
                color  = emo_colors.get(dom_e, "#94a3b8")
                pct    = 100.0 / total_mins
                parts.append(
                    f'<div title="{m}m: {dom_e}" '
                    f'style="width:{pct:.2f}%;background:{color};"></div>'
                )
            parts.append('</div></div>')

        # Legend
        parts.append('<div style="display:flex;flex-wrap:wrap;gap:8px;margin-top:10px;">')
        for emo, col in emo_colors.items():
            parts.append(
                f'<span style="display:flex;align-items:center;gap:4px;font-size:0.75em;color:var(--ta-text);">'
                f'<span style="width:12px;height:12px;border-radius:3px;background:{col};display:inline-block;"></span>'
                f'{emo}</span>'
            )
        parts.append('</div>')
        # X-axis labels
        parts.append('<div style="display:flex;justify-content:space-between;margin-top:4px;font-size:0.72em;color:var(--ta-sub);">')
        for m in range(0, total_mins + 1, max(1, total_mins // 6)):
            parts.append(f'<span>{m}m</span>')
        parts.append('</div>')
        parts.append('</div>')
        return "".join(parts)

    def _build_iv_summary_html(result: dict) -> str:
        obs = result.get("observations", [])
        parts = [
            '<div class="iv-obs-card">',
            '<div class="iv-obs-title">Observations</div>',
        ]
        for o in obs:
            parts.append(
                f'<div class="iv-obs-item">'
                f'<span style="color:var(--ta-accent);flex-shrink:0;">→</span>'
                f'<span>{o}</span>'
                f'</div>'
            )
        parts.append('</div>')
        return "".join(parts)

    # ── Face scan on file upload → populate thumbnails + roles ──────────────────
    def _iv_scan_on_upload(file_path):
        """Auto-scan faces when a video is uploaded — show thumbnails + set defaults."""
        _no_vid = (
            gr.update(visible=False), gr.update(visible=False),
            gr.update(visible=False), gr.update(visible=False),
            gr.update(visible=True),  gr.update(visible=True),   # always show 2 role dropdowns
            gr.update(visible=False), gr.update(visible=False),
            2, gr.update(value=""),
        )
        if not file_path or not _HAS_VIDEO_ANALYZER:
            return _no_vid
        _ext = Path(file_path).suffix.lower()
        if _ext not in {".mp4",".mov",".avi",".mkv",".webm",".m4v",".flv",".wmv"}:
            return _no_vid
        try:
            thumbs, dur = _video_analyzer.scan_faces(file_path)
            pids  = list(thumbs.keys())
            n     = min(len(pids), 4)
            dur_s = f"{int(dur//60)}m {int(dur%60)}s"

            if n:
                status = (
                    f'<div style="color:#22c55e;font-size:0.82em;padding:4px 0;">'
                    f'Detected <b>{n} face{"s" if n!=1 else ""}</b> · {dur_s} '
                    f'— confirm roles below then click Analyze.</div>'
                )
            else:
                status = (
                    f'<div style="color:#3b82f6;font-size:0.82em;padding:6px 8px;'
                    f'background:#eff6ff;border-radius:8px;border:1px solid #bfdbfe;">'
                    f'<b>Zoom / Teams recording detected.</b> Face thumbnails could not be '
                    f'auto-generated because faces are small inside the screen recording window. '
                    f'<b>This is normal</b> — just set the roles below '
                    f'(Candidate / Interviewer) and click <b>Analyze Video</b>. '
                    f'The analysis will still run fully. &nbsp;·&nbsp; {dur_s}</div>'
                )

            # Thumbnails — show only detected faces
            thumb_ups = [
                gr.update(value=thumbs.get(pids[i]) if i < n else None, visible=(i < n))
                for i in range(4)
            ]
            # Role dropdowns — always show at least 2 so user can assign manually
            show_count = max(n, 2)
            role_ups = [gr.update(visible=(i < show_count)) for i in range(4)]
            return (*thumb_ups, *role_ups, show_count, gr.update(value=status))
        except Exception as e:
            return (*[gr.update(visible=False)]*4,
                    gr.update(visible=True), gr.update(visible=True),   # keep 2 role dropdowns
                    gr.update(visible=False), gr.update(visible=False),
                    2,
                    gr.update(value=f'<div style="color:#f59e0b;font-size:0.82em;padding:4px 0;">'
                                   f'Could not auto-detect faces ({e}) — '
                                   f'set roles manually and click Analyze.</div>'))

    file_input.change(
        fn=_iv_scan_on_upload,
        inputs=[file_input],
        outputs=[
            iv_thumb_0, iv_thumb_1, iv_thumb_2, iv_thumb_3,
            iv_role_0,  iv_role_1,  iv_role_2,  iv_role_3,
            iv_person_count, iv_scan_status,
        ],
        queue=True,
    )

    # ── Analyze Video button → run delivery analysis ──────────────────────────
    def _iv_analyze_video(file_path, person_count, role_0, role_1, role_2, role_3):
        """Run video delivery analysis using video_analyzer.py."""
        if not file_path:
            err = '<p style="color:#ef4444;padding:12px;">Please upload a video first.</p>'
            return err, "", "", None, ""
        if not _HAS_VIDEO_ANALYZER:
            err = '<p style="color:#ef4444;padding:12px;">Video analysis packages not installed. Run: pip install mediapipe opencv-python</p>'
            return err, "", "", None, ""

        count = int(person_count or 2)
        role_list = [role_0, role_1, role_2, role_3]
        role_map  = {i: role_list[i] for i in range(min(count, 4)) if role_list[i]}

        try:
            result = _video_analyzer.analyze_video(file_path, role_map, sample_fps=1.0)
        except Exception as exc:
            err_html = f'<p style="color:#ef4444;padding:12px;">Analysis failed: {exc}</p>'
            return (gr.update(visible=False), gr.update(visible=False),
                    gr.update(visible=False), gr.update(visible=False),
                    gr.update(value=err_html, visible=True))

        if result.error:
            err_html = f'<p style="color:#ef4444;padding:12px;">{result.error[:300]}</p>'
            return (gr.update(visible=False), gr.update(visible=False),
                    gr.update(visible=False), gr.update(visible=False),
                    gr.update(value=err_html, visible=True))

        scores_html = _video_analyzer.render_score_cards_html(result)
        try:
            fig = _video_analyzer.render_timeline_figure(result)
            tl  = fig.to_html(full_html=False, include_plotlyjs="cdn",
                               config={"displayModeBar": False}) if fig else ""
        except Exception:
            tl = ""

        obs_html = ""
        if result.observations:
            obs_html = ('<div style="border:1px solid #e2e8f0;border-radius:14px;padding:16px 18px;">'
                        '<div style="font-weight:700;color:#475569;margin-bottom:8px;">Observations</div>'
                        '<ul style="margin:0;padding-left:18px;">'
                        + "".join(f'<li style="font-size:0.88em;color:#374151;margin-bottom:6px;">{o}</li>'
                                  for o in result.observations)
                        + '</ul></div>')

        ann  = result.annotated_video_path
        status_html = '<p style="color:#22c55e;font-size:0.84em;padding:4px 0;">✅ Delivery analysis complete.</p>'
        return (
            gr.update(value=scores_html, visible=bool(scores_html)),
            gr.update(value=tl,          visible=bool(tl)),
            gr.update(value=obs_html,    visible=bool(obs_html)),
            gr.update(value=ann,         visible=bool(ann)),
            gr.update(value=status_html, visible=True),
        )

    # ── event wiring ──────────────────────────────────────────────────────────
    def on_provider_change(provider):
        cfg = _PROVIDERS.get(provider, _PROVIDERS["Claude (Anthropic)"])
        return (
            gr.update(choices=cfg["models"], value=cfg["models"][0]),
            gr.update(label=f"{provider} API Key", placeholder=cfg["placeholder"], info=cfg["info"]),
        )

    provider_dropdown.change(
        fn=on_provider_change,
        inputs=[provider_dropdown],
        outputs=[model_dropdown, user_api_key],
        queue=False,
    )

    # STT engine → show/hide model/key + save to BrowserState (single handler, no race)
    # STT engine toggle — split into two separate handlers so the UI update
    # (toggle_stt_engine) is instant with no processing indicator, while the
    # BrowserState save fires independently in the background.
    stt_engine_input.change(
        fn=toggle_stt_engine,
        inputs=[stt_engine_input, user_api_key],
        outputs=[stt_key_input, stt_model_input, stt_key_banner],
        queue=False,
    )
    stt_engine_input.change(
        fn=lambda v: v,
        inputs=[stt_engine_input],
        outputs=[bsw_stt],
        queue=False,
    )

    interview_toggle.change(
        fn=lambda v: gr.update(visible=v),
        inputs=interview_toggle,
        outputs=interview_deep,
    )

    language_input.change(
        fn=toggle_language_variant,
        inputs=[language_input],
        outputs=[language_variant],
    )

    def _history_cost(e):
        """Compute cost string from a history entry's tok_in/tok_out + model."""
        tok_in  = e.get("tok_in",  0) or 0
        tok_out = e.get("tok_out", 0) or 0
        if not (tok_in or tok_out):
            return "—"
        pricing = _MODEL_PRICING.get(e.get("ai_model", ""))
        if not pricing:
            return "—"
        cost = tok_in / 1_000_000 * pricing[0] + tok_out / 1_000_000 * pricing[1]
        return f"${cost:.4f}" if cost < 0.01 else f"${cost:.3f}"

    # History helpers
    def refresh_history():
        rows = []
        for e in load_history(HISTORY_PATH):
            tok_in  = e.get("tok_in",  0) or 0
            tok_out = e.get("tok_out", 0) or 0
            tok_str = f"{tok_in:,}↑ {tok_out:,}↓" if (tok_in or tok_out) else "—"
            rows.append([
                e.get("timestamp",""), e.get("filename",""),
                e.get("stt_engine",""), e.get("stt_secs",0),
                e.get("ai_provider",""),
                tok_str,
                _history_cost(e),
                e.get("overall_score","—"),
                e.get("overall_verdict","—"),
            ])
        return rows or [["No history yet","","","","","","","",""]]

    _SCORE_EMOJI = {"Great": "🟢", "Good": "🔵", "Needs Improvement": "🟡", "Missed": "🔴"}

    def load_history_row(evt: gr.SelectData):
        """Called immediately when a row is clicked — no setTimeout race."""
        entries = load_history(HISTORY_PATH)
        idx = evt.index[0] if hasattr(evt, "index") else 0
        if not entries or idx >= len(entries):
            return "_No session data found._", None, gr.update(interactive=False)
        e = entries[idx]
        tok_in  = e.get("tok_in",  0) or 0
        tok_out = e.get("tok_out", 0) or 0
        tok_line = ""
        if tok_in or tok_out:
            cost_str = _history_cost(e)
            tok_line = (
                f"**Tokens:** {tok_in:,} in / {tok_out:,} out"
                + (f"  —  **Est. cost:** {cost_str}" if cost_str != "—" else "")
                + "  \n"
            )
        md = (
            f"### 📂 {e.get('filename','')}\n\n"
            f"**Date:** {e.get('timestamp','')}  \n"
            f"**STT Engine:** {e.get('stt_engine','')} ({e.get('stt_secs',0)}s)  \n"
            f"**AI Provider:** {e.get('ai_provider','')} / {e.get('ai_model','')}  \n"
            f"**Language:** {e.get('language','')}  \n"
            f"**Words:** {e.get('word_count','')}  \n"
            + tok_line
        )
        if e.get("overall_score"):
            md += f"**Score:** {e.get('overall_score')}/10 — {e.get('overall_verdict','')}\n\n"
        if e.get("summary"):
            md += f"---\n**Summary preview:**\n\n{e['summary']}\n\n"

        # ── Interview Q&A replay (candidate's own words only, no AI answers) ──
        qs = e.get("interview_questions", [])
        if qs:
            md += "---\n## 🎤 Interview Q&A\n\n"
            for i, q in enumerate(qs, 1):
                question    = q.get("question", "").strip()
                answer_said = q.get("answer_said", "").strip()
                score       = q.get("score", "")
                score_reason = q.get("score_reason", "").strip()
                deflection  = (q.get("deflection") or "none").lower()
                if not question:
                    continue
                emoji = _SCORE_EMOJI.get(score, "⚪")
                defl_note = ""
                if deflection == "partial":
                    defl_note = "  ⚠️ *Partially deflected*"
                elif deflection == "full":
                    defl_note = "  🚫 *Did not answer*"
                md += (
                    f"**Q{i}: {question}**{defl_note}  \n"
                    f"*Candidate said:* {answer_said}  \n"
                    f"{emoji} **{score}**"
                    + (f" — {score_reason}" if score_reason else "")
                    + "  \n\n"
                )
        return md, e.get("id"), gr.update(interactive=True)

    history_refresh_btn.click(fn=refresh_history, outputs=history_table)
    history_table.select(
        fn=load_history_row,
        outputs=[history_selected_summary, history_selected_id, history_delete_btn],
    )

    # ── History export ────────────────────────────────────────────────────────
    def _export_history():
        """Return the history JSONL file path for download; create if empty."""
        HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
        if not HISTORY_PATH.exists():
            HISTORY_PATH.write_text("", encoding="utf-8")
        return gr.update(value=str(HISTORY_PATH), visible=True)

    history_export_btn.click(fn=_export_history, outputs=history_export_btn)

    # ── History import ────────────────────────────────────────────────────────
    def _import_history(upload):
        """Merge uploaded history JSONL into current history, deduplicating by id."""
        if not upload:
            return gr.update(value="<p style='color:#f59e0b;font-size:0.82em;'>No file uploaded.</p>", visible=True), refresh_history()
        try:
            uploaded = Path(upload.name) if hasattr(upload, "name") else Path(str(upload))
            existing = load_history(HISTORY_PATH)
            existing_ids = {e.get("id") for e in existing if e.get("id")}
            added = 0
            with open(uploaded, encoding="utf-8", errors="replace") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                        if entry.get("id") not in existing_ids:
                            save_history_entry(entry, HISTORY_PATH)
                            existing_ids.add(entry.get("id"))
                            added += 1
                    except Exception:
                        pass
            msg = f"<p style='color:#22c55e;font-size:0.82em;'>✅ Imported {added} new session(s) into history.</p>"
            return gr.update(value=msg, visible=True), refresh_history()
        except Exception as ex:
            return gr.update(value=f"<p style='color:#ef4444;font-size:0.82em;'>Import failed: {ex}</p>", visible=True), refresh_history()

    history_import_file.change(
        fn=_import_history,
        inputs=[history_import_file],
        outputs=[history_action_status, history_table],
        queue=True,
    )

    # ── Delete selected entry ─────────────────────────────────────────────────
    def _delete_history_entry(entry_id):
        if not entry_id:
            return gr.update(value="<p style='color:#f59e0b;font-size:0.82em;'>No entry selected.</p>", visible=True), refresh_history(), None, gr.update(interactive=False)
        try:
            entries = load_history(HISTORY_PATH)
            kept = [e for e in entries if e.get("id") != entry_id]
            # Rewrite the file with the remaining entries (oldest first)
            HISTORY_PATH.write_text(
                "\n".join(json.dumps(e, ensure_ascii=False, default=str) for e in reversed(kept)) + ("\n" if kept else ""),
                encoding="utf-8",
            )
            msg = f"<p style='color:#22c55e;font-size:0.82em;'>✅ Entry deleted.</p>"
            return gr.update(value=msg, visible=True), refresh_history(), None, gr.update(interactive=False)
        except Exception as ex:
            return gr.update(value=f"<p style='color:#ef4444;font-size:0.82em;'>Delete failed: {ex}</p>", visible=True), refresh_history(), None, gr.update(interactive=False)

    history_delete_btn.click(
        fn=_delete_history_entry,
        inputs=[history_selected_id],
        outputs=[history_action_status, history_table, history_selected_id, history_delete_btn],
        queue=True,
    )

    # ── Clear all history ─────────────────────────────────────────────────────
    def _clear_all_history():
        try:
            HISTORY_PATH.write_text("", encoding="utf-8")
            msg = "<p style='color:#22c55e;font-size:0.82em;'>✅ All history cleared.</p>"
            return gr.update(value=msg, visible=True), refresh_history(), None, gr.update(interactive=False)
        except Exception as ex:
            return gr.update(value=f"<p style='color:#ef4444;font-size:0.82em;'>Clear failed: {ex}</p>", visible=True), refresh_history(), None, gr.update(interactive=False)

    history_clear_btn.click(
        fn=_clear_all_history,
        outputs=[history_action_status, history_table, history_selected_id, history_delete_btn],
        queue=True,
    )

    process_event = process_btn.click(
        fn=process_file,
        inputs=[
            file_input, path_input, path_input_2,
            panel_toggle, speakers_input,
            stt_engine_input, stt_key_input, stt_model_input,
            interview_toggle, interview_deep, profile_text_state,
            language_input, language_variant,
            transcript_output_lang,
            report_style,
            inc_summary, inc_key_points, inc_action,
            inc_transcript, inc_profiles, inc_analytics,
            user_api_key,
            provider_dropdown, model_dropdown,
            transcription_only_toggle,
            image_input,
            iv_person_count, iv_role_0, iv_role_1, iv_role_2, iv_role_3,
            gpu_toggle,
        ],
        concurrency_id="analyze",
        concurrency_limit=1,
        outputs=[
            status_bar,
            summary_out, transcript_out, dialogue_out,
            profiles_out, analytics_out, combined_out,
            interview_out,
            dl_transcript, dl_speakers, dl_report, dl_combined, dl_json, dl_pdf,
            dl_srt, dl_vtt, dl_docx,
            download_accordion,
            log_out,
            eta_panel,
            net_monitor,
            stats_panel,
            result_state,
            iv_scores_panel, iv_timeline, iv_summary, iv_output_video, iv_progress,
            dl_waiting,
        ],
    )
    cancel_btn.click(fn=None, cancels=[process_event], queue=False)

    # ── Profile upload — parse file → store text ─────────────────────────────
    def _parse_profile(file_path):
        if not file_path:
            return ""
        try:
            text = extract_profile_text(file_path)
            if text and text.strip():
                return text
        except Exception:
            pass
        return ""

    profile_upload.change(
        fn=_parse_profile,
        inputs=[profile_upload],
        outputs=[profile_text_state],
        queue=False,
    )

    def _regen_and_show(rs, target_lang, api_key, provider_name, model_name):
        """Regenerate PDF+DOCX and immediately show the result in dl_active."""
        if not rs:
            return gr.update(), gr.update(), gr.update(
                value=None, visible=True,
                label="Run Analyze first, then click Regenerate.")
        pdf_path, docx_path = generate_pdf_in_language(
            rs, target_lang, api_key, provider_name, model_name)
        if pdf_path:
            active = gr.update(value=pdf_path, visible=True, label="Report (.pdf)")
        elif docx_path:
            active = gr.update(value=docx_path, visible=True, label="Report (.docx)")
        else:
            active = gr.update(visible=False)
        return pdf_path or gr.update(), docx_path or gr.update(), active

    pdf_regen_btn.click(
        fn=_regen_and_show,
        inputs=[result_state, pdf_lang_input, user_api_key, provider_dropdown, model_dropdown],
        outputs=[dl_pdf, dl_docx, dl_active],
    )

    def _toggle_report_format(choice):
        return gr.update(visible=(choice == "PDF")), gr.update(visible=(choice == "DOCX"))

    report_format_radio.change(
        fn=_toggle_report_format,
        inputs=[report_format_radio],
        outputs=[dl_pdf, dl_docx],
        queue=False,
    )



    # ── Save settings → bsw_* (WRITE instances, never inputs to demo.load) ──────
    _id = lambda v: v
    # Route model saves to the correct state: Whisper models → bsw_whisper,
    # cloud STT models → bsw_stt_model.  Prevents "base" (a valid Whisper AND
    # legacy Deepgram name) from bleeding across engines on page reload.
    def _save_stt_model(engine, model):
        if engine == "whisper_local":
            return model, gr.update()
        return gr.update(), model
    stt_model_input.change(
        fn=_save_stt_model,
        inputs=[stt_engine_input, stt_model_input],
        outputs=[bsw_whisper, bsw_stt_model],
        queue=False,
    )
    language_input.change(  fn=_id, inputs=language_input,   outputs=bsw_language, queue=False)
    report_style.change(    fn=_id, inputs=report_style,     outputs=bsw_style,    queue=False)
    interview_toggle.change(fn=_id, inputs=interview_toggle, outputs=bsw_interview,queue=False)
    interview_deep.change(  fn=_id, inputs=interview_deep,   outputs=bsw_deep,     queue=False)
    # stt_engine: two handlers above — one for instant UI, one for BrowserState save
    inc_summary.change(     fn=_id, inputs=inc_summary,      outputs=bsw_inc_sum,  queue=False)
    inc_key_points.change(  fn=_id, inputs=inc_key_points,   outputs=bsw_inc_kp,   queue=False)
    inc_action.change(      fn=_id, inputs=inc_action,       outputs=bsw_inc_ac,   queue=False)
    inc_transcript.change(  fn=_id, inputs=inc_transcript,   outputs=bsw_inc_tr,   queue=False)
    inc_profiles.change(    fn=_id, inputs=inc_profiles,     outputs=bsw_inc_pr,   queue=False)
    inc_analytics.change(   fn=_id, inputs=inc_analytics,    outputs=bsw_inc_an,   queue=False)
    speakers_input.change(  fn=_id, inputs=speakers_input,   outputs=bsw_speakers, queue=False)
    gpu_toggle.change(      fn=_id, inputs=gpu_toggle,        outputs=bsw_gpu,      queue=False)

    # ── Restore on page load → reads bsr_* (READ instances, never written by .change) ──
    def _restore_settings(whisper, lang, style, interview, deep,
                          inc_s, inc_k, inc_a, inc_t, inc_p, inc_an, speakers):
        def _b(v, default): return v if isinstance(v, bool) else default
        return (
            gr.update(value=whisper or "base"),
            gr.update(value=lang    or "auto"),
            gr.update(value=style   or "formal"),
            gr.update(value=_b(interview,  True)),
            gr.update(value=_b(deep,       True)),
            gr.update(value=_b(inc_s,  True)),
            gr.update(value=_b(inc_k,  True)),
            gr.update(value=_b(inc_a,  True)),
            gr.update(value=_b(inc_t,  True)),
            gr.update(value=_b(inc_p,  True)),
            gr.update(value=_b(inc_an, True)),
            gr.update(value=speakers),
        )

    # Single combined restore — eliminates the race condition where two separate
    # demo.load calls both wrote to stt_model_input and the last one to arrive
    # (often _restore_settings with "base") would overwrite toggle_stt_engine's
    # correct Deepgram choice.  The stored model (bsr_whisper) is passed through
    # toggle_stt_engine so Deepgram restores nova-3 (or whatever was last used)
    # instead of always defaulting to the first item in the list.
    def _restore_all(whisper_model, stt_model, lang, style, interview, deep,
                     inc_s, inc_k, inc_a, inc_t, inc_p, inc_an, speakers, stt_engine, gpu):
        def _b(v, default): return v if isinstance(v, bool) else default
        engine = stt_engine or "whisper_local"
        stored = whisper_model if engine == "whisper_local" else stt_model
        key_upd, model_upd, banner_upd = toggle_stt_engine(
            engine, "", stored_model=stored,
        )
        return (
            key_upd,
            model_upd,
            banner_upd,
            gr.update(value=lang    or "auto"),
            gr.update(value=style   or "formal"),
            gr.update(value=_b(interview,  True)),
            gr.update(value=_b(deep,       True)),
            gr.update(value=_b(inc_s,  True)),
            gr.update(value=_b(inc_k,  True)),
            gr.update(value=_b(inc_a,  True)),
            gr.update(value=_b(inc_t,  True)),
            gr.update(value=_b(inc_p,  True)),
            gr.update(value=_b(inc_an, True)),
            gr.update(value=speakers),
            gr.update() if gpu is None else gr.update(value=_b(gpu, _gpu_available)),
        )

    demo.load(
        fn=_restore_all,
        inputs=[bsr_whisper, bsr_stt_model, bsr_language, bsr_style, bsr_interview, bsr_deep,
                bsr_inc_sum, bsr_inc_kp, bsr_inc_ac, bsr_inc_tr, bsr_inc_pr, bsr_inc_an, bsr_speakers,
                bsw_stt, bsr_gpu],
        outputs=[stt_key_input, stt_model_input, stt_key_banner,
                 language_input, report_style, interview_toggle, interview_deep,
                 inc_summary, inc_key_points, inc_action, inc_transcript, inc_profiles, inc_analytics,
                 speakers_input, gpu_toggle],
        queue=False,
    )

    # ── Video Analysis events ─────────────────────────────────────────────────
    if _HAS_VIDEO_ANALYZER:

        def _va_analyze(video_path):
            """One click: auto-detect faces, assign roles, run full analysis."""
            if not video_path:
                yield (
                    gr.update(value="<p style='color:#f59e0b;'>Upload a video first.</p>"),
                    gr.update(), gr.update(visible=False), gr.update(visible=False),
                )
                return

            progress_val  = [0.0]
            result_holder = [None]
            exc_holder    = [None]

            def _worker():
                try:
                    # Auto-detect faces → auto-assign roles
                    thumbs, _ = _video_analyzer.scan_faces(video_path)
                    pids = list(thumbs.keys())
                    role_map = {}
                    if pids:
                        role_map[pids[0]] = "Candidate"
                        for _i, _p in enumerate(pids[1:], 1):
                            role_map[_p] = f"Interviewer {_i}"

                    def _pcb(v): progress_val[0] = v

                    result_holder[0] = _video_analyzer.analyze_video(
                        video_path, role_map, sample_fps=1.0, progress_cb=_pcb
                    )
                except Exception as e:
                    exc_holder[0] = e

            yield (
                gr.update(value='<div style="color:#3b82f6;padding:8px 0;font-size:0.84em;">'
                                'Detecting faces and analysing… this may take a minute.</div>'),
                gr.update(), gr.update(visible=False), gr.update(visible=False),
            )

            t = threading.Thread(target=_worker, daemon=True)
            t.start()

            while t.is_alive():
                pct = int(progress_val[0] * 100)
                yield (
                    gr.update(value=(
                        f'<div style="color:#3b82f6;padding:8px 0;font-size:0.84em;">'
                        f'Analysing… {pct}%'
                        f'<div style="background:#e2e8f0;border-radius:4px;height:6px;margin-top:6px;">'
                        f'<div style="background:#3b82f6;height:6px;border-radius:4px;width:{pct}%;"></div>'
                        f'</div></div>'
                    )),
                    gr.update(), gr.update(visible=False), gr.update(visible=False),
                )
                time.sleep(1.5)

            t.join()

            if exc_holder[0]:
                yield (
                    gr.update(value=f'<p style="color:#ef4444;">Analysis failed: {exc_holder[0]}</p>'),
                    gr.update(), gr.update(visible=False), gr.update(visible=False),
                )
                return

            result = result_holder[0]
            if result is None or result.error:
                yield (
                    gr.update(value=f'<p style="color:#ef4444;">{result.error if result else "Unknown error"}</p>'),
                    gr.update(), gr.update(visible=False), gr.update(visible=False),
                )
                return

            score_html   = _video_analyzer.render_score_cards_html(result)
            timeline_fig = _video_analyzer.render_timeline_figure(result)
            ann_path     = result.annotated_video_path
            yield (
                gr.update(value='<div style="color:#22c55e;padding:8px 0;font-size:0.84em;">Done!</div>'),
                gr.update(value=score_html),
                gr.update(value=timeline_fig, visible=timeline_fig is not None),
                gr.update(value=ann_path, visible=ann_path is not None),
            )


    # Check for updates on page load (non-blocking, skipped on HF Spaces)
    if not bool(os.environ.get("SPACE_ID")):
        demo.load(fn=_check_github_update, outputs=[update_banner], queue=False)
        _hidden_update_btn.click(fn=_do_in_app_update, outputs=[update_banner], show_progress=False)

    # _THEME_JS is injected via demo.launch(js=_THEME_JS) below — no second injection needed



if __name__ == "__main__":
    # Keep the machine awake the entire time the app is running
    import atexit as _atexit
    _prevent_sleep()
    _atexit.register(_allow_sleep)

    _host   = os.environ.get("GRADIO_SERVER_NAME", "0.0.0.0")
    _port   = int(os.environ.get("GRADIO_SERVER_PORT", 7860))
    _docker = _host == "0.0.0.0"
    demo.queue(max_size=5, default_concurrency_limit=4)
    import inspect as _inspect
    _launch_sig = _inspect.signature(demo.launch).parameters
    _launch_kw = dict(
        server_name=_host,
        server_port=_port,
        js=_THEME_JS,
        theme=_THEME,
        css=CSS,
        allowed_paths=[str(OUT_DIR), tempfile.gettempdir()],
        inbrowser=not _docker,
        show_error=True,
        share=False,
    )
    if "max_file_size" in _launch_sig:
        _launch_kw["max_file_size"] = "10gb"
    if "strict_cors" in _launch_sig:
        _launch_kw["strict_cors"] = not _docker
    if "show_api" in _launch_sig:
        _launch_kw["show_api"] = False

    import socket as _socket
    try:
        _lan_ip = _socket.gethostbyname(_socket.gethostname())
    except Exception:
        _lan_ip = "unknown"
    print(f"\n  Local:   http://127.0.0.1:{_port}")
    print(f"  Network: http://{_lan_ip}:{_port}\n")

    if _docker:
        # HF Spaces / Docker: launch Gradio on port 7860, then graft REST API
        # routes onto the same app. HF only exposes 7860; port 8000 is blocked.
        _launch_kw["prevent_thread_lock"] = True
        demo.launch(**_launch_kw)
        try:
            from api import app as _api_app
            _gradio_app = demo.server.app
            for _route in _api_app.routes:
                _path = getattr(_route, "path", "")
                if _path.startswith("/api/") or _path == "/health":
                    _gradio_app.routes.append(_route)
        except Exception as _e:
            print(f"[warn] REST API graft skipped: {_e}")
        demo.block_thread()
    else:
        demo.launch(**_launch_kw)
