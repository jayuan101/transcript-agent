#!/usr/bin/env python3
"""Transcript Agent — Gradio UI with drag-and-drop | v2.1"""

import gradio as gr
import os
import sys
import uuid
import threading
import queue as Q
import time
import re
import urllib.parse
import mimetypes
import tempfile
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent / ".env")
except ImportError:
    pass


def _get_proxyscrape_proxies() -> list:
    """Return proxy URLs to try when a direct download is blocked (403)."""
    import requests as _r
    api_key = os.environ.get("PROXYSCRAPE_API_KEY", "").strip()
    params = {
        "request": "displayproxies",
        "proxy_format": "protocolipport",
        "format": "text",
        "anonymity": "elite,anonymous",
        "timeout": "8000",
        "country": "US,GB,DE,CA,AU,NL",
    }
    if api_key:
        params["apiKey"] = api_key
    try:
        r = _r.get(
            "https://api.proxyscrape.com/v3/free-proxy-list/get",
            params=params,
            timeout=10,
        )
        if r.ok:
            lines = [l.strip() for l in r.text.splitlines() if l.strip()]
            return ["http://" + l if "://" not in l else l for l in lines[:20]]
    except Exception:
        pass
    return []


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
    On 403 Forbidden, retries via ProxyScrape proxy (uses PROXYSCRAPE_API_KEY env var).
    on_progress(bytes_received, total_bytes_or_0): called periodically during download.
    """
    import requests

    def _do_get(u: str, proxies=None, timeout=300):
        return requests.get(
            u,
            stream=True,
            timeout=timeout,
            headers={"User-Agent": "TranscriptAgent/1.0"},
            allow_redirects=True,
            proxies=proxies,
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

        # Strategy 2: retry via ProxyScrape proxy
        if resp is None:
            for proxy_url in _get_proxyscrape_proxies():
                try:
                    pr = {"http": proxy_url, "https": proxy_url}
                    _r = _do_get(url, proxies=pr, timeout=90)
                    if _r.ok:
                        resp = _r
                        break
                    _r.close()
                except Exception:
                    continue

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

# ── Windows sleep prevention ──────────────────────────────────────────────────
_ES_CONTINUOUS      = 0x80000000
_ES_SYSTEM_REQUIRED = 0x00000001   # blocks idle auto-sleep; screen can still turn off
_sleep_active       = False
_sleep_thread       = None

def _set_lid_action(action: int):
    """Set lid-close power action: 0=do nothing, 1=sleep. Requires no elevation on most systems."""
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
    """Block idle sleep (screen can turn off). Prevent lid-close sleep.
    Refresh thread re-asserts every 60 s — Windows can silently drop the flag."""
    global _sleep_active, _sleep_thread
    if sys.platform != "win32":
        return
    import ctypes
    _sleep_active = True
    ctypes.windll.kernel32.SetThreadExecutionState(_ES_CONTINUOUS | _ES_SYSTEM_REQUIRED)
    _set_lid_action(0)   # lid close → do nothing (keep running)
    if _sleep_thread is None or not _sleep_thread.is_alive():
        def _refresh():
            import ctypes as _ct
            while _sleep_active:
                _ct.windll.kernel32.SetThreadExecutionState(_ES_CONTINUOUS | _ES_SYSTEM_REQUIRED)
                time.sleep(60)
        _sleep_thread = threading.Thread(target=_refresh, daemon=True)
        _sleep_thread.start()

def _allow_sleep():
    global _sleep_active
    _sleep_active = False
    if sys.platform != "win32":
        return
    import ctypes
    ctypes.windll.kernel32.SetThreadExecutionState(_ES_CONTINUOUS)
    _set_lid_action(1)   # restore: lid close → sleep

from transcript_agent import (
    run, ReportConfig, build_combined_report, LLMClient,
    AUDIO_EXTS, VIDEO_EXTS,
)

# ── AI provider configuration ─────────────────────────────────────────────────
_PROVIDERS = {
    "Claude (Anthropic)": {
        "type": "anthropic",
        "placeholder": "sk-ant-api03-…",
        "info": "console.anthropic.com → API keys → Create key",
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
    "OpenAI": {
        "type": "openai",
        "placeholder": "sk-…",
        "info": "platform.openai.com → API keys",
        "models": [
            "gpt-4o",
            "gpt-4o-mini",
            "gpt-4-turbo",
            "o1",
            "o1-mini",
            "o3-mini",
            "gpt-3.5-turbo",
        ],
        "base_url": None,
    },
    "Google Gemini": {
        "type": "openai_compat",
        "placeholder": "AIzaSy…",
        "info": "aistudio.google.com → Get API key",
        "models": [
            "gemini-2.5-pro-preview-05-06",
            "gemini-2.0-flash-exp",
            "gemini-2.0-flash-thinking-exp",
            "gemini-1.5-pro",
            "gemini-1.5-flash",
            "gemini-1.5-flash-8b",
        ],
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai/",
    },
    "Groq": {
        "type": "openai_compat",
        "placeholder": "gsk_…",
        "info": "console.groq.com → API keys",
        "models": [
            "llama-3.3-70b-versatile",
            "llama-3.1-70b-versatile",
            "llama3-70b-8192",
            "llama3-8b-8192",
            "mixtral-8x7b-32768",
            "gemma2-9b-it",
        ],
        "base_url": "https://api.groq.com/openai/v1",
    },
    "Mistral": {
        "type": "openai_compat",
        "placeholder": "…",
        "info": "console.mistral.ai → API keys",
        "models": [
            "mistral-large-latest",
            "mistral-medium-latest",
            "mistral-small-latest",
            "open-mixtral-8x22b",
            "open-mistral-nemo",
        ],
        "base_url": "https://api.mistral.ai/v1",
    },
    "Together AI": {
        "type": "openai_compat",
        "placeholder": "…",
        "info": "api.together.ai → Settings → API keys",
        "models": [
            "meta-llama/Llama-3-70b-chat-hf",
            "meta-llama/Meta-Llama-3.1-70B-Instruct-Turbo",
            "mistralai/Mixtral-8x22B-Instruct-v0.1",
            "Qwen/Qwen2-72B-Instruct",
            "google/gemma-2-27b-it",
        ],
        "base_url": "https://api.together.xyz/v1",
    },
    "Perplexity": {
        "type": "openai_compat",
        "placeholder": "pplx-…",
        "info": "perplexity.ai → Settings → API",
        "models": [
            "llama-3.1-sonar-large-128k-online",
            "llama-3.1-sonar-huge-128k-online",
            "llama-3.1-sonar-small-128k-online",
            "llama-3.1-70b-instruct",
        ],
        "base_url": "https://api.perplexity.ai",
    },
    "Ollama (Local)": {
        "type": "openai_compat",
        "placeholder": "none required",
        "info": "ollama.ai — run models locally, no API key needed",
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

OUT_DIR = Path(__file__).parent / "outputs"
OUT_DIR.mkdir(exist_ok=True)

JOB_STATUS_FILE = OUT_DIR / ".job_status.json"

def _write_job_status(status: str, **kwargs):
    import json, datetime
    data = {"status": status, "updated": datetime.datetime.now().isoformat(), **kwargs}
    try:
        JOB_STATUS_FILE.write_text(json.dumps(data, indent=2))
    except Exception:
        pass

def _read_job_status():
    import json
    try:
        return json.loads(JOB_STATUS_FILE.read_text())
    except Exception:
        return None

def load_last_result():
    """Load the most recently completed job and return its results for all output tabs."""
    import json
    status = _read_job_status()
    if not status or status.get("status") != "done":
        return [gr.update()] * 13 + [gr.update(value="No completed job found. Run a transcription first.", visible=True)]
    try:
        job_dir = Path(status["job_dir"])
        stem    = status["stem"]
        data    = status.get("result", {})   # formatted strings saved at completion
        f_t = str(job_dir / f"{stem}_transcript.txt")
        f_s = str(job_dir / f"{stem}_speakers.txt")
        f_r = str(job_dir / f"{stem}_report.md")
        f_c = str(job_dir / f"{stem}_combined.txt")
        f_j = str(job_dir / f"{stem}_full.json")
        f_p = str(job_dir / f"{stem}_report.pdf") if (job_dir / f"{stem}_report.pdf").exists() else None
        completed = status.get("completed", "")[:16].replace("T", " ")
        msg = f"✅ Loaded: **{stem}** (completed {completed})"
        return [
            data.get("summary", ""),
            data.get("transcript", ""),
            data.get("dialogue", ""),
            data.get("profiles", ""),
            data.get("analytics", ""),
            data.get("combined", ""),
            f_t if Path(f_t).exists() else None,
            f_s if Path(f_s).exists() else None,
            f_r if Path(f_r).exists() else None,
            f_c if Path(f_c).exists() else None,
            f_j if Path(f_j).exists() else None,
            f_p,
            gr.update(open=True),
            gr.update(value=msg, visible=True),
        ]
    except Exception as e:
        return [gr.update()] * 13 + [gr.update(value=f"Error loading results: {e}", visible=True)]

def get_job_banner():
    """Return HTML banner showing current job status — called on page load."""
    status = _read_job_status()
    if not status:
        return ""
    s = status.get("status")
    name = status.get("stem", "Unknown file")
    updated = status.get("updated", "")[:16].replace("T", " ")
    if s == "running":
        import datetime as _dtt
        try:
            started = _dtt.datetime.fromisoformat(status.get("updated", ""))
            age_hours = ((_dtt.datetime.now() - started).total_seconds()) / 3600
        except Exception:
            age_hours = 0
        if age_hours > 4:
            return (
                '<div id="job-banner" style="background:#422006;border:1px solid #f97316;border-radius:8px;'
                'padding:12px 16px;margin-bottom:12px;display:flex;align-items:center;gap:10px;">'
                '<span style="font-size:1.3em">⚠️</span>'
                '<div><strong style="color:#fdba74">Previous job did not complete</strong>'
                f'<div style="color:#fed7aa;font-size:0.85em">{name} — started {updated} but never finished '
                '(browser disconnected or computer slept). Click <strong>Load Last Result</strong> to check '
                'if any results were saved, or run a new job.</div>'
                '</div></div>'
            )
        return (
            '<div id="job-banner" style="background:#1e3a5f;border:1px solid #3b82f6;border-radius:8px;'
            'padding:12px 16px;margin-bottom:12px;display:flex;align-items:center;gap:10px;">'
            '<span style="font-size:1.3em">⏳</span>'
            '<div><strong style="color:#93c5fd">Transcription in progress</strong>'
            f'<div style="color:#cbd5e1;font-size:0.85em">{name} — started {updated}. '
            'Keep this tab open to see results, or come back later and click <strong>Load Last Result</strong>.</div>'
            '</div></div>'
        )
    elif s == "done":
        completed = status.get("completed", "")[:16].replace("T", " ")
        return (
            '<div id="job-banner" style="background:#14532d;border:1px solid #4ade80;border-radius:8px;'
            'padding:12px 16px;margin-bottom:12px;display:flex;align-items:center;gap:10px;">'
            '<span style="font-size:1.3em">✅</span>'
            '<div><strong style="color:#4ade80">Last transcription completed</strong>'
            f'<div style="color:#bbf7d0;font-size:0.85em">{name} — finished {completed}. '
            'Click <strong>Load Last Result</strong> to view it.</div>'
            '</div></div>'
        )
    elif s == "error":
        return (
            '<div id="job-banner" style="background:#450a0a;border:1px solid #f87171;border-radius:8px;'
            'padding:12px 16px;margin-bottom:12px;display:flex;align-items:center;gap:10px;">'
            '<span style="font-size:1.3em">🚨</span>'
            '<div><strong style="color:#f87171">Last transcription failed</strong>'
            f'<div style="color:#fca5a5;font-size:0.85em">{name} — {updated}. '
            f'Error: {status.get("error","unknown")}</div>'
            '</div></div>'
        )
    return ""



SUPPORTED = list(AUDIO_EXTS | VIDEO_EXTS | {".srt", ".vtt", ".txt", ".md", ".docx", ".pdf"})

FORMATS_MD = """
**Accepted formats**
🎵 `.mp3` `.wav` `.m4a` `.flac` `.ogg` `.aac`
🎬 `.mp4` `.mov` `.avi` `.mkv` `.webm`
📝 `.srt` `.vtt`   📄 `.pdf` `.docx` `.txt` `.md`
"""

CSS = """
footer { display: none !important; }

/* page */
body { background: #f1f5f9 !important; }

/* ── Checkbox — fully custom so both checked and unchecked are visible ── */
input[type="checkbox"] {
    -webkit-appearance: none !important;
    appearance: none !important;
    width: 18px !important;
    height: 18px !important;
    min-width: 18px !important;
    border: 2px solid #2563eb !important;
    border-radius: 4px !important;
    background: #ffffff !important;
    cursor: pointer !important;
    position: relative !important;
    vertical-align: middle !important;
    transition: background 0.15s, border-color 0.15s !important;
    flex-shrink: 0 !important;
}
input[type="checkbox"]:checked {
    background: #2563eb !important;
    border-color: #2563eb !important;
}
input[type="checkbox"]:checked::after {
    content: '' !important;
    position: absolute !important;
    left: 4px !important;
    top: 1px !important;
    width: 6px !important;
    height: 10px !important;
    border: 2px solid #fff !important;
    border-top: none !important;
    border-left: none !important;
    transform: rotate(45deg) !important;
    display: block !important;
}
input[type="checkbox"]:hover { border-color: #1d4ed8 !important; }
input[type="checkbox"]:focus { outline: 2px solid #93c5fd !important; outline-offset: 2px !important; }

/* Dark mode checkboxes */
html.dark input[type="checkbox"] {
    background: #1e293b !important;
    border-color: #60a5fa !important;
}
html.dark input[type="checkbox"]:checked {
    background: #3b82f6 !important;
    border-color: #3b82f6 !important;
}
html.dark input[type="checkbox"]:hover { border-color: #93c5fd !important; }

.checkbox-wrap { align-items: center !important; gap: 8px !important; }
.checkbox-group label, .checkbox-wrap label {
    font-size: 0.9em !important;
    font-weight: 500 !important;
    cursor: pointer !important;
    display: flex !important;
    align-items: center !important;
    gap: 8px !important;
}
html.dark .checkbox-group label span,
html.dark .checkbox-wrap label span { color: #e2e8f0 !important; }

/* process button */
.big-btn button {
    background: linear-gradient(135deg,#1e40af,#3b82f6) !important;
    color: #fff !important;
    font-size: 1.08em !important;
    font-weight: 700 !important;
    letter-spacing: 0.04em !important;
    border: none !important;
    border-radius: 10px !important;
    padding: 15px !important;
    min-height: 54px !important;
    width: 100% !important;
    box-shadow: 0 4px 14px rgba(29,78,216,0.40) !important;
    transition: all 0.15s ease !important;
}
.big-btn button:hover {
    background: linear-gradient(135deg,#1e3a8a,#1d4ed8) !important;
    box-shadow: 0 6px 20px rgba(29,78,216,0.55) !important;
    transform: translateY(-1px) !important;
}

/* scrollable dropdowns — generic */
[role="listbox"] {
    max-height: 220px !important;
    overflow-y: auto !important;
}

/* provider + model dropdowns — taller scroll area, thin scrollbar */
#provider-sel [role="listbox"],
#model-sel [role="listbox"] {
    max-height: 280px !important;
    overflow-y: auto !important;
    scrollbar-width: thin !important;
}
#provider-sel [role="listbox"]::-webkit-scrollbar,
#model-sel [role="listbox"]::-webkit-scrollbar { width: 6px !important; }
#provider-sel [role="listbox"]::-webkit-scrollbar-thumb,
#model-sel [role="listbox"]::-webkit-scrollbar-thumb {
    background: #94a3b8 !important; border-radius: 4px !important;
}
html.dark #provider-sel [role="listbox"]::-webkit-scrollbar-thumb,
html.dark #model-sel [role="listbox"]::-webkit-scrollbar-thumb {
    background: #475569 !important;
}

/* live log dark terminal */
#live-log textarea {
    background: #0f172a !important;
    color: #86efac !important;
    font-family: 'Courier New', monospace !important;
    font-size: 0.80em !important;
    line-height: 1.65 !important;
    border-color: #1e3a5f !important;
    border-radius: 8px !important;
}

/* Fix banner text — Gradio overrides <strong> color to white */
#api-banner strong, #api-banner b { color: inherit !important; font-weight: 700; }
#api-banner-sub { color: #92400e !important; }

/* ── Dark mode static rules (JS-injected sheet wins by cascade order) ── */
html.dark { color-scheme: dark; color: #e2e8f0 !important; background: #0f172a !important; }
html.dark body, html.dark .gradio-container, html.dark .main, html.dark .contain {
    background: #0f172a !important; color: #e2e8f0 !important;
}
html.dark .block, html.dark .form, html.dark .panel-full-width, html.dark .compact,
html.dark .wrap, html.dark .upload-container {
    background: #1e293b !important; border-color: #334155 !important;
}
html.dark input, html.dark textarea, html.dark select {
    background: #0f172a !important; color: #e2e8f0 !important; border-color: #475569 !important;
}
html.dark span, html.dark p, html.dark div, html.dark h1, html.dark h2,
html.dark h3, html.dark h4, html.dark li, html.dark td { color: #e2e8f0 !important; }
html.dark .label-wrap span, html.dark .block-label, html.dark label span,
html.dark .info, html.dark .file-name { color: #94a3b8 !important; }
html.dark .tabs > .tab-nav button {
    color: #94a3b8 !important; background: #1e293b !important; border-color: #334155 !important;
}
html.dark .tabs > .tab-nav button.selected {
    color: #e2e8f0 !important; border-bottom-color: #3b82f6 !important; background: #0f172a !important;
}
html.dark .tabitem { background: #0f172a !important; }
html.dark .prose, html.dark .markdown { color: #e2e8f0 !important; }
html.dark .prose *, html.dark .markdown * { color: #e2e8f0 !important; }
html.dark [role="listbox"] { background: #1e293b !important; border-color: #334155 !important; }
html.dark [role="option"] { color: #e2e8f0 !important; background: #1e293b !important; }
html.dark [role="option"]:hover, html.dark [role="option"][aria-selected="true"] {
    background: #334155 !important; color: #fff !important;
}
html.dark .accordion, html.dark details { background: #1e293b !important; border-color: #334155 !important; }
html.dark .accordion .label-wrap, html.dark details summary { color: #e2e8f0 !important; }
html.dark .checkbox-group label span, html.dark .radio-group label span { color: #cbd5e1 !important; }
html.dark .file-preview { background: #1e293b !important; color: #e2e8f0 !important; }
html.dark .dropdown-arrow svg { fill: #94a3b8 !important; }
html.dark button { background: #1e293b !important; border-color: #334155 !important; color: #e2e8f0 !important; }
html.dark .big-btn button { background: linear-gradient(135deg,#1e40af,#3b82f6) !important; color: #fff !important; }
html.dark #ta-btn-light { background: transparent !important; color: #94a3b8 !important; }
html.dark #ta-btn-dark  { background: #3b82f6 !important; color: #fff !important; }
html.dark ::-webkit-scrollbar-track { background: #0f172a !important; }
html.dark ::-webkit-scrollbar-thumb { background: #334155 !important; }
html.dark ::-webkit-scrollbar-thumb:hover { background: #475569 !important; }

/* ── Adaptive CSS variables used by step-tracker and ETA panel ── */
:root {
    --ta-card-bg:          #f8fafc;
    --ta-card-border:      #e2e8f0;
    --ta-card-text:        #1e293b;
    --ta-card-sub:         #64748b;
    --ta-card-val:         #111827;
    --ta-step-done-bg:     #dcfce7;
    --ta-step-done-bdr:    #22c55e;
    --ta-step-done-clr:    #166534;
    --ta-step-act-bg:      #dbeafe;
    --ta-step-act-bdr:     #2563eb;
    --ta-step-act-clr:     #1d4ed8;
    --ta-step-wait-bg:     #f1f5f9;
    --ta-step-wait-bdr:    #e2e8f0;
    --ta-step-wait-clr:    #94a3b8;
    --ta-conn-line-done:   #22c55e;
    --ta-conn-line-wait:   #e2e8f0;
    --ta-stat-bg:          rgba(255,255,255,0.7);
    --ta-stat-label:       #1e40af;
    --ta-stat-val:         #1d4ed8;
}
html.dark {
    --ta-card-bg:          #1e293b;
    --ta-card-border:      #334155;
    --ta-card-text:        #e2e8f0;
    --ta-card-sub:         #94a3b8;
    --ta-card-val:         #f1f5f9;
    --ta-step-done-bg:     #14532d;
    --ta-step-done-bdr:    #4ade80;
    --ta-step-done-clr:    #4ade80;
    --ta-step-act-bg:      #1e3a5f;
    --ta-step-act-bdr:     #60a5fa;
    --ta-step-act-clr:     #93c5fd;
    --ta-step-wait-bg:     #0f172a;
    --ta-step-wait-bdr:    #334155;
    --ta-step-wait-clr:    #475569;
    --ta-conn-line-done:   #4ade80;
    --ta-conn-line-wait:   #334155;
    --ta-stat-bg:          rgba(15,23,42,0.6);
    --ta-stat-label:       #93c5fd;
    --ta-stat-val:         #e2e8f0;
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
    lines = []
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

    lines += [
        "---",
        "",
        "**Pace reference:** 🐢 Slow < 120 wpm · 🚶 Normal 120–150 · 🏃 Fast 150–180 · ⚡ Very Fast > 180",
    ]
    return "\n".join(lines)


# ── processing — generator streams every update live to the UI ────────────────
#
# Output order (15 items, must match outputs= list in .click()):
def _generate_pdf(stem: str, combined_text: str, path: Path) -> str:
    """Render the combined report as a formatted PDF using fpdf2."""
    from fpdf import FPDF

    class _PDF(FPDF):
        def footer(self):
            self.set_y(-12)
            self.set_font("Helvetica", "I", 8)
            self.set_text_color(150, 150, 150)
            self.cell(0, 10, f"Page {self.page_no()}", align="C")

    def _safe(text: str) -> str:
        return (text
                .replace("☐", "[ ]").replace("☑", "[x]")
                .replace("•", "-").replace("’", "'")
                .replace("“", '"').replace("”", '"')
                .replace("–", "-").replace("—", "--")
                .encode("latin-1", errors="replace").decode("latin-1"))

    pdf = _PDF()
    pdf.set_auto_page_break(auto=True, margin=18)
    pdf.add_page()
    pdf.set_margins(20, 20, 20)

    # Title
    pdf.set_font("Helvetica", "B", 20)
    pdf.cell(0, 12, _safe(stem), new_x="LMARGIN", new_y="NEXT", align="C")
    pdf.set_draw_color(80, 80, 80)
    pdf.set_line_width(0.5)
    pdf.line(20, pdf.get_y(), 190, pdf.get_y())
    pdf.ln(5)
    pdf.set_font("Helvetica", "", 10)

    for line in combined_text.splitlines():
        stripped = line.rstrip()
        # Skip pure divider lines (===... or ---...)
        if stripped and set(stripped) <= {"=", "-", " "} and len(stripped) > 4:
            continue
        if not stripped:
            pdf.ln(2)
            continue
        inner = stripped.strip()
        # ALL-CAPS section headers
        if (inner.isupper() and 2 < len(inner) < 60
                and not set(inner) <= {"=", "-", " "}):
            pdf.ln(5)
            pdf.set_fill_color(235, 235, 235)
            pdf.set_font("Helvetica", "B", 11)
            pdf.cell(0, 7, _safe(inner), new_x="LMARGIN", new_y="NEXT", fill=True)
            pdf.ln(2)
            pdf.set_font("Helvetica", "", 10)
        else:
            pdf.multi_cell(0, 5, _safe(stripped))

    pdf.output(str(path))
    return str(path)


#   0  status_bar       1  summary_out      2  transcript_out   3  dialogue_out
#   4  profiles_out     5  analytics_out    6  combined_out
#   7  dl_transcript    8  dl_speakers      9  dl_report
#   10 dl_combined      11 dl_json          12 dl_pdf
#   13 download_accordion  14 log_out       15 eta_panel  16 result_state
# ---------------------------------------------------------------------------

_NOCHANGE = (gr.update(),) * 17   # yield this to keep connection alive without changes

def _out(status=gr.update(), summary=gr.update(), transcript=gr.update(),
         dialogue=gr.update(), profiles=gr.update(), analytics=gr.update(),
         combined=gr.update(), dl_t=gr.update(), dl_s=gr.update(),
         dl_r=gr.update(), dl_c=gr.update(), dl_j=gr.update(), dl_p=gr.update(),
         dl_acc=gr.update(), log=gr.update(), eta=gr.update(), rs=None):
    return (status, summary, transcript, dialogue, profiles, analytics,
            combined, dl_t, dl_s, dl_r, dl_c, dl_j, dl_p, dl_acc, log, eta, rs)


_PDF_LANGUAGES = [
    "Same as source",
    "English", "Spanish", "French", "German", "Portuguese",
    "Italian", "Dutch", "Russian", "Chinese (Simplified)",
    "Japanese", "Korean", "Arabic", "Hindi", "Turkish",
]


def _translate_combined_text(
    combined_text: str, target_language: str, api_key: str,
    provider: str = "anthropic", model: str = None, base_url: str = None,
) -> str:
    """Translate the combined report text to target_language using the selected provider."""
    _model = model or ("claude-sonnet-4-6" if provider == "anthropic" else "gpt-4o")
    client = LLMClient(provider=provider, api_key=api_key, model=_model, base_url=base_url)
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
    """Generate (or re-generate) the PDF in the chosen language."""
    if not result_state:
        return gr.update()
    stem         = result_state["stem"]
    combined     = result_state["combined_text"]
    detected     = result_state.get("detected_language", "")
    out_dir      = Path(result_state["out_dir"])

    if target_lang and target_lang != "Same as source":
        cfg = _PROVIDERS.get(provider_name, _PROVIDERS["Claude (Anthropic)"])
        combined = _translate_combined_text(
            combined, target_lang, api_key,
            provider=cfg["type"], model=model_name, base_url=cfg["base_url"],
        )
        pdf_name = f"{stem}_report_{target_lang.replace(' ', '_')}.pdf"
    else:
        pdf_name = f"{stem}_report.pdf"

    pdf_path = out_dir / pdf_name
    _generate_pdf(f"{stem}  [{detected or target_lang}]", combined, pdf_path)
    return str(pdf_path)


def _step_tracker_html(stage: str, done: bool = False) -> str:
    if done:
        states = ["done", "done", "done"]
    elif stage in ("loading",):
        states = ["active", "waiting", "waiting"]
    elif stage in ("extracting", "whisper"):
        states = ["done", "active", "waiting"]
    elif stage == "claude":
        states = ["done", "done", "active"]
    else:
        states = ["active", "waiting", "waiting"]

    labels = [("📁", "Upload"), ("🎤", "Transcribe"), ("🤖", "Analyze")]
    parts  = []
    for i, ((icon, label), state) in enumerate(zip(labels, states)):
        if state == "done":
            bg = "var(--ta-step-done-bg)"; bdr = "var(--ta-step-done-bdr)"
            clr = "var(--ta-step-done-clr)"; dot = "✓"
        elif state == "active":
            bg = "var(--ta-step-act-bg)"; bdr = "var(--ta-step-act-bdr)"
            clr = "var(--ta-step-act-clr)"; dot = "●"
        else:
            bg = "var(--ta-step-wait-bg)"; bdr = "var(--ta-step-wait-bdr)"
            clr = "var(--ta-step-wait-clr)"; dot = "○"
        parts.append(
            f'<div style="display:flex;flex-direction:column;align-items:center;gap:5px;flex:1;">'
            f'<div style="background:{bg};border:2px solid {bdr};border-radius:50%;'
            f'width:38px;height:38px;display:flex;align-items:center;justify-content:center;'
            f'font-size:1.05em;font-weight:800;color:{clr};transition:all 0.4s;">{dot}</div>'
            f'<div style="font-size:0.68em;font-weight:700;color:{clr};text-align:center;'
            f'text-transform:uppercase;letter-spacing:0.06em;">{icon} {label}</div>'
            f'</div>'
        )
        if i < 2:
            lc = "var(--ta-conn-line-done)" if states[i] == "done" else "var(--ta-conn-line-wait)"
            parts.append(
                f'<div style="flex:0.6;height:2px;background:{lc};margin-top:19px;'
                f'border-radius:2px;transition:background 0.4s;"></div>'
            )

    return (
        '<div style="display:flex;align-items:flex-start;padding:10px 16px 8px;'
        'background:var(--ta-card-bg);border:1px solid var(--ta-card-border);'
        'border-radius:12px;margin-bottom:10px;">'
        + "".join(parts) + "</div>"
    )


def _eta_panel_html(stage: str, pct: float = None, eta_secs: int = None,
                    elapsed: str = "", done: bool = False) -> str:
    import datetime as _dt

    _slide_css = (
        "<style>@keyframes pgslide{0%{left:-45%}100%{left:110%}}</style>"
    )

    # ── Done ──────────────────────────────────────────────────────────────────
    tracker = _step_tracker_html(stage, done)

    if done:
        return tracker + (
            '<div style="background:linear-gradient(135deg,#d1fae5,#a7f3d0);'
            'border:2px solid #10b981;border-radius:16px;padding:28px 32px;'
            'text-align:center;font-family:sans-serif;">'
            '<div style="font-size:3em;line-height:1;">&#10003;</div>'
            '<div style="color:#065f46;font-size:1.5em;font-weight:800;margin-top:8px;'
            'letter-spacing:-0.02em;">All Done!</div>'
            '<div style="display:flex;justify-content:center;gap:20px;margin-top:14px;flex-wrap:wrap;">'
            '<div style="background:rgba(255,255,255,0.6);border-radius:10px;padding:10px 20px;">'
            '<div style="font-size:0.72em;font-weight:700;text-transform:uppercase;'
            'letter-spacing:0.08em;color:#047857;">Total Time</div>'
            f'<div style="font-size:1.6em;font-weight:800;color:#065f46;'
            f'font-family:monospace;">{elapsed}</div>'
            '</div>'
            '<div style="background:rgba(255,255,255,0.6);border-radius:10px;padding:10px 20px;">'
            '<div style="font-size:0.72em;font-weight:700;text-transform:uppercase;'
            'letter-spacing:0.08em;color:#047857;">Progress</div>'
            '<div style="font-size:1.6em;font-weight:800;color:#065f46;">100%</div>'
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

        def _stat(label_txt, val_txt, label_var="--ta-stat-label", val_var="--ta-stat-val"):
            return (
                f'<div style="background:var(--ta-stat-bg);border-radius:8px;'
                f'padding:8px 14px;flex:1;min-width:90px;text-align:center;">'
                f'<div style="font-size:0.68em;font-weight:700;text-transform:uppercase;'
                f'letter-spacing:0.08em;color:var({label_var});">{label_txt}</div>'
                f'<div style="font-size:1.3em;font-weight:800;color:var({val_var});'
                f'font-family:monospace;">{val_txt}</div></div>'
            )

        return tracker + (
            '<div style="background:var(--ta-card-bg);border:2px solid var(--ta-step-act-bdr);'
            'border-radius:16px;padding:24px 28px;font-family:sans-serif;">'
            '<div style="font-size:0.72em;font-weight:700;text-transform:uppercase;'
            'letter-spacing:0.1em;color:var(--ta-step-act-clr);margin-bottom:12px;">'
            'Step 1 of 2 &nbsp;&mdash;&nbsp; Transcribing Audio</div>'
            '<div style="display:flex;align-items:flex-end;gap:4px;margin-bottom:14px;">'
            f'<div style="font-size:4.5em;font-weight:900;color:var(--ta-step-act-clr);'
            f'font-family:monospace;line-height:1;letter-spacing:-0.04em;">{pct_int}</div>'
            '<div style="font-size:2em;font-weight:700;color:var(--ta-step-act-bdr);'
            'margin-bottom:6px;">%</div></div>'
            '<div style="background:var(--ta-step-wait-bg);border-radius:8px;height:14px;'
            'overflow:hidden;margin-bottom:10px;">'
            f'<div style="width:{bar_fill};height:100%;'
            'background:linear-gradient(90deg,var(--ta-step-act-bdr),var(--ta-step-act-clr));'
            'border-radius:8px;transition:width 0.5s ease;"></div></div>'
            '<div style="display:flex;gap:16px;flex-wrap:wrap;">'
            + _stat("Time Left", eta_str)
            + (_stat("Done By", finish_str, "--ta-step-done-clr", "--ta-step-done-clr") if finish_str else "")
            + _stat("Elapsed", elapsed, "--ta-card-sub", "--ta-card-val") +
            '</div></div>'
        )

    # ── Other stages (loading / extracting / claude / whisper indeterminate) ──
    stage_cfg = {
        "loading":    ("var(--ta-step-act-bdr)",  "Starting up…",        "Step 0 of 2", "var(--ta-step-act-clr)"),
        "extracting": ("var(--ta-step-done-bdr)", "Extracting audio…",   "Step 1 of 2", "var(--ta-step-done-clr)"),
        "whisper":    ("var(--ta-step-act-bdr)",  "Transcribing audio…", "Step 1 of 2", "var(--ta-step-act-clr)"),
        "claude":     ("#a855f7",                 "Analyzing with AI…",  "Step 2 of 2", "#c4b5fd"),
    }
    color, label, step, text_clr = stage_cfg.get(
        stage, ("var(--ta-card-border)", "Processing…", "", "var(--ta-card-sub)")
    )

    overlay_pct = ""
    if stage == "claude":
        overlay_pct = (
            '<div style="display:flex;align-items:flex-end;gap:4px;margin-bottom:14px;">'
            f'<div style="font-size:4.5em;font-weight:900;color:{text_clr};'
            f'font-family:monospace;line-height:1;letter-spacing:-0.04em;">50</div>'
            f'<div style="font-size:2em;font-weight:700;color:{color};margin-bottom:6px;">%+</div>'
            '</div>'
            f'<div style="font-size:0.82em;color:var(--ta-card-sub);margin-bottom:12px;">'
            'AI is reading the transcript and writing your report…</div>'
        )
    elif stage in ("loading", "extracting"):
        overlay_pct = (
            '<div style="display:flex;align-items:flex-end;gap:4px;margin-bottom:14px;">'
            f'<div style="font-size:4.5em;font-weight:900;color:{text_clr};'
            f'font-family:monospace;line-height:1;">—</div></div>'
        )

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
        f'<div style="background:var(--ta-stat-bg);border-radius:8px;padding:8px 14px;">'
        f'<div style="font-size:0.68em;font-weight:700;text-transform:uppercase;'
        f'letter-spacing:0.08em;color:var(--ta-card-sub);">Elapsed</div>'
        f'<div style="font-size:1.3em;font-weight:800;color:var(--ta-card-val);'
        f'font-family:monospace;">{elapsed}</div>'
        f'</div></div></div>'
    )


def _err(msg: str) -> tuple:
    """Yield this tuple to display an inline error card instead of a popup."""
    html = (
        '<div style="background:linear-gradient(135deg,#fef2f2,#fee2e2);'
        'border:2px solid #ef4444;border-radius:12px;padding:18px 22px;'
        'display:flex;align-items:flex-start;gap:14px;font-family:sans-serif;">'
        '<div style="font-size:1.8em;line-height:1;flex-shrink:0;">❌</div>'
        '<div>'
        '<div style="color:#991b1b;font-weight:700;font-size:1em;">Something went wrong</div>'
        f'<div style="color:#b91c1c;font-size:0.88em;margin-top:5px;">{msg}</div>'
        '</div>'
        '</div>'
    )
    return _out(status=html, eta="")


def process_file(
    uploaded_file,
    path_input,
    panel_mode,
    speaker_names_raw,
    num_speakers,
    whisper_model,
    language_input,
    language_variant,
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
):
    # ── validation (all errors shown inline, no popup) ────────────────────────
    api_key = (user_api_key or "").strip()
    provider_cfg = _PROVIDERS.get(provider_name, _PROVIDERS["Claude (Anthropic)"])
    if not api_key and provider_name != "Ollama (Local)":
        yield _err(f"Please enter your {provider_name} API key at the top of the page.")
        return
    provider_type = provider_cfg["type"]
    base_url      = provider_cfg["base_url"]
    _prevent_sleep()

    # prefer pasted path/URL (no upload wait) over drag-and-drop
    pasted = (path_input or "").strip().strip('"').strip("'")
    if pasted:
        uploaded_file = pasted

    if not uploaded_file:
        yield _err("Please drag a file, paste a file path, or paste a URL above.")
        return

    # ── Log helpers (must be defined before download section uses them) ────────
    start_time    = time.time()
    log_entries   = []

    def _ts():
        secs = int(time.time() - start_time)
        m, s = divmod(secs, 60)
        return f"{m:02d}:{s:02d}"

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
                    f'<div style="color:{color};{weight}margin-top:8px;border-top:1px solid #1e3a5f;'
                    f'padding-top:6px;letter-spacing:0.05em;">{text}</div>'
                )
            elif kind == 'progress':
                # text-only line in log — the ETA panel owns the visual bar
                parts.append(
                    f'<div><span style="color:#334155;">[{ts}]</span> '
                    f'<span style="color:{color};{weight}">{text}</span></div>'
                )
            else:
                parts.append(
                    f'<div><span style="color:#334155;">[{ts}]</span> '
                    f'<span style="color:{color};{weight}">{text}</span></div>'
                )
        scroll = '<div id="ta-log-end"></div><script>document.getElementById("ta-log-end")?.scrollIntoView();</script>'
        inner = "".join(parts) + scroll if parts else '<span style="color:#334155;">Starting…</span>'
        return (
            '<div id="ta-log-wrap" style="background:#0f172a;border:1px solid #1e3a5f;'
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

        _dl_stall = 0
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
                    if total:
                        pct = recv / total * 100
                        log = _add_log(f"⬇️  {recv//1024//1024} MB / {total//1024//1024} MB  ({pct:.0f}%)", "download")
                    else:
                        log = _add_log(f"⬇️  {recv//1024//1024} MB received…", "download")
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
    # Use names if provided, otherwise fall back to numeric count
    _names = (speaker_names_raw or "").strip()
    if _names:
        speaker_names = _names
    else:
        try:
            _n = int(num_speakers) if num_speakers not in (None, "", 0) else None
        except (ValueError, TypeError):
            _n = None
        speaker_names = f"{_n} speakers" if _n and _n >= 1 else None
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

    _write_job_status("running", stem=stem, job_id=job_id, job_dir=str(job_dir))

    # ── thread communication ──────────────────────────────────────────────────
    q = Q.Queue()

    def on_whisper_progress(pct):
        q.put(("pct", pct))

    def on_raw_transcript(text):
        q.put(("transcript", text))

    def on_stage_change(stage):
        q.put(("stage", stage))

    def on_log(msg):
        q.put(("log", msg))

    def background():
        try:
            result = run(
                file_path=uploaded_file,
                output_dir=str(job_dir),
                whisper_model=whisper_model,
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
                on_whisper_progress=on_whisper_progress if is_av else None,
                on_raw_transcript=on_raw_transcript if is_av else None,
                on_stage_change=on_stage_change if is_av else None,
                on_log=on_log,
            )
            # Write status directly so it persists even if the browser disconnected
            import datetime as _dtt
            _write_job_status("done", stem=stem, job_id=job_id, job_dir=str(job_dir),
                              completed=_dtt.datetime.now().isoformat())
            q.put(("done", result))
        except Exception as e:
            # Write error status directly — generator may already be dead
            _write_job_status("error", stem=stem, job_id=job_id, job_dir=str(job_dir),
                              error=str(e)[:300])
            q.put(("error", str(e)))

    t = threading.Thread(target=background, daemon=True)
    t.start()

    # ── live update loop ──────────────────────────────────────────────────────
    whisper_pct     = 0.0
    raw_shown       = False
    claude_started  = False
    stage           = "loading"
    last_activity   = time.time()
    stall_warned    = set()

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
                log = _add_log("⚠️  AI analysis taking 2+ min. Long transcript or slow API — still waiting.", "warn")
                yield _out(log=log)
            elif stage == "claude" and quiet == 600 and stall_key not in stall_warned:
                stall_warned.add(stall_key)
                log = _add_log("🚨  10 min waiting for AI response. Check your API key quota or try a faster model.", "error")
                yield _out(log=log)

            # ── eta panel update ─────────────────────────────────────────────
            if stage in ("whisper",):
                eta_s   = _eta_secs(whisper_pct) if whisper_pct > 0 else None
                eta_upd = _eta_panel_html("whisper", pct=whisper_pct or None,
                                          eta_secs=eta_s, elapsed=elapsed)
                yield _out(status=_status_compact("🎤", "Transcribing audio…", elapsed), eta=eta_upd)
            elif stage == "extracting":
                yield _out(status=_status_compact("🎬", "Extracting audio…", elapsed),
                           eta=_eta_panel_html("extracting", elapsed=elapsed))
            elif stage in ("claude",) or claude_started:
                yield _out(status=_status_compact("🤖", "Analyzing with AI…", elapsed),
                           eta=_eta_panel_html("claude", elapsed=elapsed))
            else:
                yield _out(status=_status_compact("⏳", "Loading…", elapsed),
                           eta=_eta_panel_html("loading", elapsed=elapsed))
            continue

        kind = msg[0]

        if kind == "log":
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
                log = _add_log("Whisper model loaded — transcription in progress…", "info")
                yield _out(status=_status_compact("🎤", "Transcribing audio…", elapsed),
                           eta=_eta_panel_html("whisper", elapsed=elapsed), log=log)
            elif stage == "claude":
                log = _add_header("🤖  AI ANALYSIS  (Step 2 of 2)")
                log = _add_log("Sending transcript to AI for analysis…", "ai")
                yield _out(status=_status_compact("🤖", "Analyzing with AI…", elapsed),
                           eta=_eta_panel_html("claude", elapsed=elapsed), log=log)

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
            raw_shown = True
            elapsed   = _elapsed()
            log_text  = _add_log("✅ Transcription complete!", "done")
            log_text  = _add_header("🤖  AI ANALYSIS  (Step 2 of 2)")
            log_text  = _add_log("Sending transcript to AI for analysis…", "ai")
            yield _out(
                status=_status_compact("🤖", "Analyzing with AI…", elapsed),
                eta=_eta_panel_html("claude", elapsed=elapsed),
                transcript=msg[1],
                log=log_text,
            )
            claude_started = True
            stage = "claude"

        elif kind == "done":
            result = msg[1]

            summary_md = f"## Summary\n\n{result.summary}"
            if inc_key_points and result.key_points:
                summary_md += "\n\n## Key Points\n" + "\n".join(f"- {p}" for p in result.key_points)
            if inc_action_items and result.action_items:
                summary_md += "\n\n## Action Items\n" + "\n".join(f"- [ ] {a}" for a in result.action_items)

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

            f_t = str(job_dir / f"{stem}_transcript.txt")
            f_s = str(job_dir / f"{stem}_speakers.txt")
            f_r = str(job_dir / f"{stem}_report.md")
            f_c = str(job_dir / f"{stem}_combined.txt")
            f_j = str(job_dir / f"{stem}_full.json")
            f_p_path = job_dir / f"{stem}_report.pdf"
            try:
                _generate_pdf(stem, combined_text, f_p_path)
                f_p = str(f_p_path)
            except Exception:
                f_p = None

            total_elapsed = _elapsed()
            import datetime as _dtt
            _write_job_status("done", stem=stem, job_id=job_id, job_dir=str(job_dir),
                              completed=_dtt.datetime.now().isoformat(),
                              result={"summary": summary_md, "transcript": result.clean_transcript,
                                      "dialogue": result.speaker_dialogue, "profiles": profiles_md,
                                      "analytics": analytics_md, "combined": combined_text})
            log_text = _add_header("✅  COMPLETE")
            log_text = _add_log(f"All done in {total_elapsed}. Results ready in all tabs.", "done")
            yield _out(
                status=_status_compact("✅", "Done! All tabs are ready.", total_elapsed)
                      + "<script>window.taJobEnd && window.taJobEnd()</script>",
                eta=_eta_panel_html("done", elapsed=total_elapsed, done=True),
                summary=summary_md,
                transcript=result.clean_transcript,
                dialogue=result.speaker_dialogue,
                profiles=profiles_md,
                analytics=analytics_md,
                combined=combined_text,
                dl_t=f_t, dl_s=f_s, dl_r=f_r, dl_c=f_c, dl_j=f_j, dl_p=f_p,
                dl_acc=gr.update(open=True),
                rs={"stem": stem, "combined_text": combined_text,
                    "detected_language": result.detected_language,
                    "out_dir": str(job_dir)},
                log=log_text,
            )
            break

        elif kind == "error":
            _write_job_status("error", stem=stem, job_id=job_id, job_dir=str(job_dir),
                              error=str(msg[1])[:200])
            log_text = _add_log(f"🚨 {msg[1]}", "error")
            yield _out(log=log_text)
            yield _err(f"Processing failed: {msg[1]}")
            break
    finally:
        _allow_sleep()


def toggle_speakers(is_panel):
    return gr.update(visible=is_panel)


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
        return gr.update(choices=variants, value=variants[0][1], visible=True)
    return gr.update(choices=[], value=None, visible=False)


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
<div style="background:linear-gradient(135deg,#0f172a 0%,#1e3a5f 55%,#2563eb 100%);
     border-radius:16px;padding:36px 44px 32px;color:#fff;margin-bottom:8px;">
  <div style="display:flex;align-items:center;gap:16px;margin-bottom:16px;">
    <div style="font-size:3em;line-height:1;filter:drop-shadow(0 2px 8px rgba(0,0,0,0.3));">🎙️</div>
    <div>
      <div style="font-size:1.9em;font-weight:800;letter-spacing:-0.03em;line-height:1.1;">Transcript Agent</div>
      <div style="color:#93c5fd;font-size:0.95em;font-weight:500;margin-top:5px;">
        AI-powered transcription &amp; analysis &mdash; Whisper + Claude
      </div>
    </div>
  </div>
  <div style="display:flex;gap:8px;flex-wrap:wrap;">
    <span style="background:rgba(255,255,255,0.15);border:1px solid rgba(255,255,255,0.3);border-radius:20px;padding:4px 13px;font-size:0.76em;font-weight:600;letter-spacing:0.02em;">🎵 Audio &amp; Video</span>
    <span style="background:rgba(255,255,255,0.15);border:1px solid rgba(255,255,255,0.3);border-radius:20px;padding:4px 13px;font-size:0.76em;font-weight:600;letter-spacing:0.02em;">📄 Documents</span>
    <span style="background:rgba(255,255,255,0.15);border:1px solid rgba(255,255,255,0.3);border-radius:20px;padding:4px 13px;font-size:0.76em;font-weight:600;letter-spacing:0.02em;">🗣️ Speaker Diarization</span>
    <span style="background:rgba(255,255,255,0.15);border:1px solid rgba(255,255,255,0.3);border-radius:20px;padding:4px 13px;font-size:0.76em;font-weight:600;letter-spacing:0.02em;">📊 Speech Analytics</span>
    <span style="background:rgba(255,255,255,0.15);border:1px solid rgba(255,255,255,0.3);border-radius:20px;padding:4px 13px;font-size:0.76em;font-weight:600;letter-spacing:0.02em;">🌐 37+ Languages</span>
  </div>
</div>
"""

_API_BANNER = """
<div id="api-banner" style="background:linear-gradient(135deg,#fffbeb,#fef3c7);border:1.5px solid #f59e0b;
     border-radius:12px;padding:14px 20px;display:flex;align-items:center;gap:14px;
     transition:background 0.35s,border-color 0.35s;">
  <div id="api-banner-icon" style="font-size:1.6em;transition:all 0.3s;">🔑</div>
  <div style="flex:1;">
    <div id="api-banner-title" style="font-weight:700;color:#92400e;font-size:0.9em;transition:color 0.3s;">API Key Required</div>
    <div id="api-banner-sub" style="color:#92400e;font-size:0.8em;margin-top:2px;transition:color 0.3s;">
      Enter your <span style="font-weight:700;color:#78350f;">Anthropic API key</span> below. Usage is billed directly to your account — nothing is stored here.
    </div>
  </div>
  <div id="api-banner-badge" style="display:none;background:#22c55e;color:#fff;font-size:0.72em;
       font-weight:700;padding:4px 12px;border-radius:20px;letter-spacing:0.04em;">APPROVED ✓</div>
</div>
"""

_THEME_TOGGLE = """
<!-- segmented light / dark control — inline styles so Gradio can't strip them -->
<div id="ta-widget"
  style="position:fixed;top:14px;right:18px;z-index:9999;display:flex;align-items:center;
         background:rgba(255,255,255,0.95);backdrop-filter:blur(10px);
         border:1px solid #e2e8f0;border-radius:28px;padding:4px;
         box-shadow:0 2px 12px rgba(0,0,0,0.12);gap:2px;">
  <button id="ta-btn-light" title="Light mode"
    style="display:flex;align-items:center;gap:5px;padding:5px 12px;border-radius:22px;
           border:none;cursor:pointer;font-size:0.82em;font-weight:600;
           background:#3b82f6;color:#fff;transition:all 0.2s;">
    ☀️ Light
  </button>
  <button id="ta-btn-dark" title="Dark mode"
    style="display:flex;align-items:center;gap:5px;padding:5px 12px;border-radius:22px;
           border:none;cursor:pointer;font-size:0.82em;font-weight:600;
           background:transparent;color:#64748b;transition:all 0.2s;">
    🌙 Dark
  </button>
</div>
"""

# ── Theme JS — injected via gr.Blocks(js=...) which is the guaranteed execution
# path. gr.HTML uses Svelte {#html} which deliberately does NOT run <script> tags.
_THEME_JS = """
(function(){
  var _dark = false;

  /* ── PERMANENT static CSS injected directly into <head> ─────────────────────
     Gradio 6 embeds css=CSS as JSON data in <script> tags and injects it later
     via its own pipeline — we can't rely on it being in the DOM at toggle time.
     We inject all our static CSS here so it's guaranteed to be real CSS. */
  if (!document.getElementById('ta-static')) {
    var ps = document.createElement('style');
    ps.id = 'ta-static';
    ps.textContent = [
      /* Light mode page base */
      'body{background:#f1f5f9!important}',
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
      /* Process button */
      '.big-btn button{background:linear-gradient(135deg,#1e40af,#3b82f6)!important;color:#fff!important;font-size:1.08em!important;font-weight:700!important;border:none!important;border-radius:10px!important;padding:15px!important;width:100%!important;box-shadow:0 4px 14px rgba(29,78,216,0.40)!important}',
      /* Scrollable dropdowns */
      '[role=listbox]{max-height:220px!important;overflow-y:auto!important}',
      '#provider-sel [role=listbox],#model-sel [role=listbox]{max-height:280px!important;overflow-y:auto!important}',
      /* Live log terminal */
      '#live-log textarea{background:#0f172a!important;color:#86efac!important;font-family:"Courier New",monospace!important;font-size:0.80em!important;border-color:#1e3a5f!important}',
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
    'html.dark .big-btn button{background:linear-gradient(135deg,#1e40af,#3b82f6)!important;color:#fff!important;border:none!important}',
    /* theme toggle — restore correct colors */
    'html.dark #ta-btn-light{background:transparent!important;color:#94a3b8!important}',
    'html.dark #ta-btn-dark{background:#3b82f6!important;color:#fff!important}',
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

    /* Update button visuals */
    var bl = document.getElementById('ta-btn-light');
    var bd = document.getElementById('ta-btn-dark');
    var wg = document.getElementById('ta-widget');
    if (bl && bd) {
      bl.style.background = dark ? 'transparent' : '#3b82f6';
      bl.style.color      = dark ? '#94a3b8'     : '#fff';
      bd.style.background = dark ? '#3b82f6'     : 'transparent';
      bd.style.color      = dark ? '#fff'         : '#64748b';
    }
    if (wg) {
      wg.style.background  = dark ? 'rgba(15,23,42,0.95)' : 'rgba(255,255,255,0.95)';
      wg.style.borderColor = dark ? '#334155' : '#e2e8f0';
    }

    /* API banner */
    var banner = document.getElementById('api-banner');
    var title  = document.getElementById('api-banner-title');
    var sub    = document.getElementById('api-banner-sub');
    if (banner && !banner.dataset.state) {
      banner.style.background  = dark ? 'linear-gradient(135deg,#292107,#3b2d00)' : 'linear-gradient(135deg,#fffbeb,#fef3c7)';
      banner.style.borderColor = dark ? '#d97706' : '#f59e0b';
      if (title) title.style.color = dark ? '#fbbf24' : '#92400e';
      if (sub)   sub.style.color   = dark ? '#fcd34d' : '#a16207';
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
          if (sub)   { sub.innerHTML = 'Enter your API key below (Anthropic, OpenAI, Gemini, Groq, etc.). Billed to your account — nothing stored here.'; _bs(sub, 'color', '#a16207'); }
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

    /* ── Screen Wake Lock ─────────────────────────────────────────────────────
       Prevents the display from sleeping during transcription.
       While the screen stays on, Windows will not enter deep sleep.
       Browser auto-releases the lock when the tab is hidden — we re-acquire
       when the tab becomes visible again if a job is still running. */
    var _taWakeLock = null;

    window._taAcquireWakeLock = async function() {
      if (!('wakeLock' in navigator)) return;
      try {
        _taWakeLock = await navigator.wakeLock.request('screen');
        var n = document.getElementById('ta-wake-notice');
        if (n) n.style.display = 'flex';
      } catch(e) {}
    };

    window._taReleaseWakeLock = async function() {
      if (_taWakeLock) { try { await _taWakeLock.release(); } catch(e) {} _taWakeLock = null; }
      var n = document.getElementById('ta-wake-notice');
      if (n) n.style.display = 'none';
    };

    /* Watch status bar — release wake lock when job finishes or errors */
    var _taStatusObs = new MutationObserver(function() {
      var bar = document.querySelector('.status-bar, [data-testid="status-bar"], #status-bar');
      if (!bar) bar = document.querySelector('.eta-panel, .gradio-html');
      var txt = document.body.innerText || '';
      if ((txt.includes('All tabs are ready') || txt.includes('Processing failed')) && _taWakeLock) {
        window._taReleaseWakeLock();
        localStorage.removeItem('ta-job-running');
      }
    });
    setTimeout(function() {
      _taStatusObs.observe(document.body, { childList: true, subtree: true, characterData: true });
    }, 2000);

    document.addEventListener('visibilitychange', function() {
      if (document.visibilityState === 'visible' && !_taWakeLock) {
        if (localStorage.getItem('ta-job-running')) window._taAcquireWakeLock();
      }
    });

    window.taJobStart = function(filename) {
      localStorage.setItem('ta-job-running', JSON.stringify({ file: filename, started: Date.now() }));
      window._taAcquireWakeLock();
    };
    window.taJobEnd = function() {
      localStorage.removeItem('ta-job-running');
      window._taReleaseWakeLock();
      var b = document.getElementById('ta-job-banner');
      if (b) b.remove();
    };
  })();
})();
"""

_IDLE_STATUS = """
<div style="background:linear-gradient(135deg,#1e3a5f,#1e40af);border-radius:12px;
     padding:18px 22px;display:flex;align-items:center;gap:16px;">
  <div style="font-size:2em;">📂</div>
  <div>
    <div style="color:#fff;font-size:1em;font-weight:700;">Ready to process</div>
    <div style="color:#93c5fd;font-size:0.85em;margin-top:3px;">
      Upload a file on the left, then click <strong style="color:#fff;background:rgba(255,255,255,0.2);
      padding:1px 8px;border-radius:4px;">Analyze File</strong>
    </div>
  </div>
</div>
"""

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

_SECTION = lambda label: f"""
<div style="font-size:0.7em;font-weight:700;text-transform:uppercase;letter-spacing:0.1em;
     color:var(--ta-card-sub);margin:4px 0 2px;">{label}</div>
"""

# ── UI ──────────────────────────────────────────────────────────────────────────

with gr.Blocks(title="Transcript Agent") as demo:

    gr.HTML(_THEME_TOGGLE)
    gr.HTML(_HERO)
    gr.HTML(_API_BANNER)

    # ── Wake lock notice (shown while transcription runs) ────────────────────
    gr.HTML(
        '<div id="ta-wake-notice" style="display:none;background:#1e3a5f;border:1px solid #3b82f6;'
        'border-radius:8px;padding:8px 14px;margin-bottom:8px;align-items:center;gap:8px;font-size:0.85em;">'
        '<span>☕</span>'
        '<span style="color:#93c5fd"><strong>Screen kept awake</strong> — your computer will not sleep during transcription. '
        'Safe to step away.</span></div>'
    )

    # ── Job status banner (updates on page load) ──────────────────────────────
    job_banner = gr.HTML(value=get_job_banner())
    with gr.Row():
        load_last_btn = gr.Button("📂 Load Last Result", size="sm", variant="secondary")
        load_last_msg = gr.Markdown(visible=False)

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
        with gr.Column(scale=1, min_width=320):

            gr.HTML(_SECTION("Step 1 — Upload"))
            file_input = gr.File(
                label="Drag & drop a file or click to browse",
                file_types=SUPPORTED,
                type="filepath",
            )
            path_input = gr.Textbox(
                label="Or paste a file path or URL (large files / no upload wait)",
                placeholder='e.g.  C:\\Videos\\interview.mp4  or  https://example.com/recording.webm',
            )
            gr.HTML(_FORMATS)

            gr.HTML(_SECTION("Step 2 — Configure"))
            with gr.Accordion("Processing Options", open=True):
                speakers_name_input = gr.Textbox(visible=False, value="")
                speakers_count_input = gr.Number(
                    label="Number of speakers (optional)",
                    value=None,
                    step=1,
                    info="How many people are speaking? AI will label them Speaker 1, Speaker 2, etc.",
                )
                whisper_input = gr.Dropdown(
                    label="Whisper model",
                    choices=["tiny", "base", "small", "medium", "large"],
                    value="base",
                    info="tiny = fastest   |   large = most accurate",
                )
                # panel_toggle kept as hidden dummy so existing wiring doesn't break
                panel_toggle = gr.Checkbox(value=False, visible=False)

            with gr.Accordion("Language", open=False):
                language_input = gr.Dropdown(
                    label="Transcript language",
                    choices=LANGUAGES,
                    value="auto",
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
                    visible=False,
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
                        inc_summary    = gr.Checkbox(label="Summary",          value=True)
                        inc_key_points = gr.Checkbox(label="Key points",       value=True)
                        inc_action     = gr.Checkbox(label="Action items",     value=True)
                    with gr.Column(min_width=130):
                        inc_transcript = gr.Checkbox(label="Full transcript",  value=True)
                        inc_profiles   = gr.Checkbox(label="Speaker profiles", value=True)
                        inc_analytics  = gr.Checkbox(label="Speech analytics", value=True)

            gr.HTML("""
<div style="font-size:0.7em;font-weight:700;text-transform:uppercase;
     letter-spacing:0.1em;color:var(--ta-card-sub);margin:8px 0 4px;">
  Step 3 — Run
</div>""")
            process_btn = gr.Button(
                "Analyze File",
                variant="primary", size="lg",
                elem_classes=["big-btn"],
            )

            result_state = gr.State(value=None)

            download_accordion = gr.Accordion("Download Outputs", open=False)
            with download_accordion:
                dl_transcript = gr.File(label="Transcript (.txt)")
                dl_speakers   = gr.File(label="Speaker Dialogue (.txt)")
                dl_report     = gr.File(label="Report (.md)")
                dl_pdf        = gr.File(label="Report (.pdf)")
                dl_combined   = gr.File(label="Combined Report (.txt)")
                dl_json       = gr.File(label="Raw Data (.json)")
                gr.HTML("<hr style='margin:8px 0;opacity:0.3'>")
                with gr.Row():
                    pdf_lang_input = gr.Dropdown(
                        label="PDF transcript language",
                        choices=_PDF_LANGUAGES,
                        value="Same as source",
                        scale=3,
                    )
                    pdf_regen_btn = gr.Button("Generate PDF", scale=1, size="sm")

        # ── results panel ─────────────────────────────────────────────────────
        with gr.Column(scale=2):

            status_bar = gr.HTML(value=_IDLE_STATUS)
            eta_panel  = gr.HTML(value="")
            log_out    = gr.HTML(
                value='<div id="ta-log-wrap" style="background:#0f172a;border:1px solid #1e3a5f;'
                      'border-radius:10px;padding:12px 16px;min-height:120px;max-height:260px;'
                      'overflow-y:auto;font-family:\'Courier New\',monospace;font-size:0.80em;'
                      'line-height:1.7;color:#475569;">'
                      '<span style="color:#334155;">Progress appears here once you click Analyze File…</span>'
                      '</div>',
                elem_id="live-log",
                label="Live Processing Log",
            )

            with gr.Tabs():
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

                with gr.TabItem("Copy All"):
                    combined_out = gr.Textbox(
                        lines=30, buttons=["copy"], label="",
                        placeholder="All sections combined will appear here…",
                    )

    gr.HTML("""
    <div style="text-align:center;padding:20px 0 4px;font-size:0.76em;color:#94a3b8;">
      Transcript Agent &nbsp;&bull;&nbsp; Transcription by OpenAI Whisper
      &nbsp;&bull;&nbsp; Analysis by Anthropic Claude
      &nbsp;&bull;&nbsp; Files processed privately on your machine
    </div>
    """)

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
    )

    # panel_toggle is hidden dummy — no change handler needed
    language_input.change(
        fn=toggle_language_variant,
        inputs=language_input,
        outputs=language_variant,
    )

    process_btn.click(
        fn=process_file,
        js="(...args) => { if(window._taAcquireWakeLock) window._taAcquireWakeLock(); return args; }",
        inputs=[
            file_input, path_input,
            panel_toggle, speakers_name_input, speakers_count_input, whisper_input,
            language_input, language_variant,
            report_style,
            inc_summary, inc_key_points, inc_action,
            inc_transcript, inc_profiles, inc_analytics,
            user_api_key,
            provider_dropdown, model_dropdown,
        ],
        outputs=[
            status_bar,
            summary_out, transcript_out, dialogue_out,
            profiles_out, analytics_out, combined_out,
            dl_transcript, dl_speakers, dl_report, dl_combined, dl_json, dl_pdf,
            download_accordion,
            log_out,
            eta_panel,
            result_state,
        ],
    )

    pdf_regen_btn.click(
        fn=generate_pdf_in_language,
        inputs=[result_state, pdf_lang_input, user_api_key, provider_dropdown, model_dropdown],
        outputs=[dl_pdf],
    )

    # ── Load Last Result ──────────────────────────────────────────────────────
    load_last_btn.click(
        fn=load_last_result,
        inputs=[],
        outputs=[
            summary_out, transcript_out, dialogue_out, profiles_out, analytics_out,
            combined_out, dl_transcript, dl_speakers, dl_report, dl_combined,
            dl_json, dl_pdf, download_accordion, load_last_msg,
        ],
    )



    # ── Refresh banner on page load ───────────────────────────────────────────
    demo.load(fn=get_job_banner, inputs=[], outputs=[job_banner])


if __name__ == "__main__":
    _host   = os.environ.get("GRADIO_SERVER_NAME", "0.0.0.0")
    _port   = int(os.environ.get("GRADIO_SERVER_PORT", 7860))
    _docker = _host == "0.0.0.0"
    demo.queue(max_size=5, default_concurrency_limit=1)
    demo.launch(
        server_name=_host,
        server_port=_port,
        js=_THEME_JS,
        theme=_THEME,
        css=CSS,
        allowed_paths=[str(OUT_DIR), tempfile.gettempdir()],
        max_file_size="4gb",
        inbrowser=not _docker,
        show_error=True,
        share=not _docker,
        strict_cors=not _docker,
    )
