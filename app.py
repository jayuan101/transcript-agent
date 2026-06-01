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
    AUDIO_EXTS, VIDEO_EXTS, STT_ENGINES,
    load_history, save_history_entry,
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
            "claude-3-opus-20240229",
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
            "o1",
            "o1-mini",
            "gpt-4-turbo",
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
        "info": "console.groq.com → API keys · 🔒 Saved in your browser only — never on this server",
        "models": [
            "llama-3.3-70b-versatile",
            "llama-3.1-70b-versatile",
            "llama-3.1-8b-instant",
            "deepseek-r1-distill-llama-70b",
            "gemma2-9b-it",
            "mixtral-8x7b-32768",
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
            "codestral-latest",
            "open-mixtral-8x22b",
            "open-mistral-nemo",
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
            "meta-llama/Meta-Llama-3.1-8B-Instruct-Turbo",
            "Qwen/Qwen2.5-72B-Instruct-Turbo",
            "mistralai/Mixtral-8x22B-Instruct-v0.1",
            "google/gemma-2-27b-it",
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
            "llama3.3",
            "llama3.2",
            "llama3.1:70b",
            "gemma3:27b",
            "qwen2.5:72b",
            "phi4",
            "deepseek-r1:70b",
            "mistral",
        ],
        "base_url": "http://localhost:11434/v1",
    },
}

OUT_DIR      = Path(__file__).parent / "outputs"
OUT_DIR.mkdir(exist_ok=True)
HISTORY_PATH = OUT_DIR / "history.jsonl"



SUPPORTED = list(AUDIO_EXTS | VIDEO_EXTS | {".srt", ".vtt", ".txt", ".md", ".docx", ".pdf"})

FORMATS_MD = """
**Accepted formats**
🎵 `.mp3` `.wav` `.m4a` `.flac` `.ogg` `.aac`
🎬 `.mp4` `.mov` `.avi` `.mkv` `.webm`
📝 `.srt` `.vtt`   📄 `.pdf` `.docx` `.txt` `.md`
"""

CSS = """
/* Hide Gradio footer, version badge, and "Built with Gradio" link */
footer,
.gradio-footer,
.built-with,
[data-testid="footer"],
.svelte-footer,
a[href*="gradio.app"],
a[href*="huggingface.co/spaces"]:not([id]) { display: none !important; }

/* page */
body { background: #f4f6fb !important; }

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

/* ── Stats panel ── */
.ta-stat-cell { display:flex;flex-direction:column;align-items:center;padding:0 16px;text-align:center;flex:1;min-width:100px; }
.ta-stat-val  { font-size:0.88em;font-weight:700;color:var(--ta-card-text);white-space:nowrap; }
.ta-stat-key  { font-size:0.68em;font-weight:600;text-transform:uppercase;letter-spacing:0.08em;color:var(--ta-card-sub);margin-top:2px; }

/* ── Hero — guaranteed injection via Gradio CSS pipeline ── */
.ta-hero {
    background: linear-gradient(145deg,#040c1e 0%,#0a1628 30%,#0f2044 60%,#162d6b 100%) !important;
    border-radius: 22px !important;
    padding: 36px 44px 30px !important;
    color: #fff !important;
    margin-bottom: 6px !important;
    position: relative !important;
    overflow: hidden !important;
    box-shadow: 0 8px 40px rgba(0,0,0,0.32), 0 2px 8px rgba(0,0,0,0.2) !important;
}
.ta-hero * { color: inherit; }
.ta-hero-eyebrow { font-size: 0.67em !important; font-weight: 700 !important; letter-spacing: 0.15em !important; text-transform: uppercase !important; color: rgba(147,197,253,0.95) !important; margin-bottom: 5px !important; display: block; }
.ta-hero-title { font-size: 2.05em !important; font-weight: 800 !important; letter-spacing: -0.04em !important; line-height: 1.05 !important; background: linear-gradient(110deg,#fff 25%,#bfdbfe 70%,#93c5fd 100%) !important; -webkit-background-clip: text !important; -webkit-text-fill-color: transparent !important; background-clip: text !important; display: block; }
.ta-hero-sub { color: #cbd5e1 !important; font-size: 0.83em !important; margin-top: 6px !important; display: block; -webkit-text-fill-color: #cbd5e1 !important; }
.ta-hero-stat-n { font-size: 1.3em !important; font-weight: 800 !important; color: #fff !important; display: block; }
.ta-hero-stat-l { font-size: 0.67em !important; font-weight: 600 !important; color: #e2e8f0 !important; text-transform: uppercase !important; display: block; }
.ta-hero-stats { background: rgba(255,255,255,0.10) !important; border: 1px solid rgba(255,255,255,0.18) !important; border-radius: 12px !important; padding: 12px 20px !important; margin-bottom: 18px !important; display: flex !important; align-items: center !important; flex-wrap: wrap !important; gap: 0 !important; }
.ta-hero-stat { display: flex !important; flex-direction: column !important; align-items: center !important; flex: 1 !important; min-width: 60px !important; }
.ta-hero-stat-sep { width: 1px !important; height: 32px !important; background: rgba(255,255,255,0.2) !important; flex-shrink: 0 !important; }
.ta-hero-chips { display: flex !important; gap: 7px !important; flex-wrap: wrap !important; }
.ta-hero-chip { border-radius: 8px !important; padding: 4px 12px !important; font-size: 0.72em !important; font-weight: 600 !important; }
.ta-hc-blue { background: rgba(59,130,246,0.28) !important; border: 1px solid rgba(96,165,250,0.4) !important; color: #bfdbfe !important; }
.ta-hc-purple { background: rgba(139,92,246,0.25) !important; border: 1px solid rgba(167,139,250,0.4) !important; color: #ddd6fe !important; }
.ta-hc-indigo { background: rgba(99,102,241,0.28) !important; border: 1px solid rgba(129,140,248,0.4) !important; color: #c7d2fe !important; }
.ta-hero-header { display: flex !important; align-items: center !important; gap: 20px !important; margin-bottom: 22px !important; }
.ta-hero-icon-box { background: linear-gradient(135deg,rgba(255,255,255,0.13),rgba(255,255,255,0.04)) !important; border: 1px solid rgba(255,255,255,0.2) !important; border-radius: 18px !important; padding: 14px 16px !important; flex-shrink: 0 !important; }
.ta-hero-blob-tr, .ta-hero-blob-bl, .ta-hero-grid { position: absolute !important; pointer-events: none !important; }
.ta-hero-blob-tr { top: -70px !important; right: -50px !important; width: 320px !important; height: 320px !important; background: radial-gradient(circle,rgba(59,130,246,0.22) 0%,transparent 68%) !important; }
.ta-hero-blob-bl { bottom: -50px !important; left: 40px !important; width: 240px !important; height: 240px !important; background: radial-gradient(circle,rgba(99,102,241,0.14) 0%,transparent 65%) !important; }
.ta-hero-grid { inset: 0 !important; background-image: radial-gradient(rgba(255,255,255,0.055) 1px,transparent 1px) !important; background-size: 28px 28px !important; }
.ta-hero-inner { position: relative !important; }

/* ── Analyze button ── */
.ta-analyze-btn button {
    background: linear-gradient(135deg,#1d4ed8,#3b82f6) !important;
    color: #fff !important; font-size: 0.9em !important; font-weight: 700 !important;
    border: none !important; border-radius: 8px !important;
    padding: 8px 18px !important; width: 100% !important;
    box-shadow: 0 3px 12px rgba(29,78,216,0.35) !important;
    letter-spacing: 0.02em !important; transition: all 0.18s !important;
}
.ta-analyze-btn button:hover { transform: translateY(-1px) !important; box-shadow: 0 5px 18px rgba(29,78,216,0.48) !important; }

/* ── Stop button — tiny, flush right ── */
.ta-cancel-btn { flex: 0 0 34px !important; min-width: 34px !important; max-width: 34px !important; }
.ta-cancel-btn button { width: 34px !important; height: 34px !important; padding: 0 !important; border-radius: 7px !important; font-size: 0.82em !important; font-weight: 700 !important; line-height: 1 !important; box-shadow: none !important; }
.ta-status-bar { flex: 1 1 auto !important; min-width: 0 !important; }

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

/* ── Hero dark mode ── */
html.dark .ta-hero { border: 1px solid rgba(59,130,246,0.22) !important; box-shadow: 0 8px 48px rgba(0,0,0,0.6), 0 2px 8px rgba(0,0,0,0.4) !important; }
html.dark .ta-hero-blob-tr { background: radial-gradient(circle,rgba(59,130,246,0.28) 0%,transparent 68%) !important; }
html.dark .ta-hero-blob-bl { background: radial-gradient(circle,rgba(99,102,241,0.2) 0%,transparent 65%) !important; }
html.dark .ta-hero-stats { background: rgba(255,255,255,0.04) !important; border-color: rgba(255,255,255,0.08) !important; }
html.dark .ta-analyze-btn button { background: linear-gradient(135deg,#1e40af,#3b82f6) !important; color: #fff !important; border: none !important; }

/* ── Update banner ── */
.ta-update-banner {
    background: linear-gradient(135deg,#eff6ff,#dbeafe);
    border: 2px solid #3b82f6;
    border-radius: 14px;
    padding: 16px 20px;
    margin: 10px 0;
    font-family: sans-serif;
}
html.dark .ta-update-banner {
    background: linear-gradient(135deg,#1e3a5f,#1e40af22) !important;
    border-color: #60a5fa !important;
}
.ta-upd-btn {
    padding: 8px 16px; border-radius: 8px; border: none;
    cursor: pointer; font-weight: 700; font-size: 0.85em;
    transition: all 0.18s; white-space: nowrap;
}
.ta-upd-win { background: #1d4ed8; color: #fff; }
.ta-upd-win:hover { background: #1e40af; transform: translateY(-1px); }
.ta-upd-mac { background: #374151; color: #fff; }
.ta-upd-mac:hover { background: #1f2937; transform: translateY(-1px); }
.ta-upd-btn:disabled { opacity: 0.6; cursor: not-allowed; transform: none !important; }

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

_NOCHANGE = (gr.update(),) * 23   # yield this to keep connection alive without changes

def _out(status=gr.update(), summary=gr.update(), transcript=gr.update(),
         dialogue=gr.update(), profiles=gr.update(), analytics=gr.update(),
         combined=gr.update(), interview=gr.update(),
         dl_t=gr.update(), dl_s=gr.update(), dl_r=gr.update(),
         dl_c=gr.update(), dl_j=gr.update(), dl_p=gr.update(),
         dl_srt=gr.update(), dl_vtt=gr.update(), dl_docx=gr.update(),
         dl_acc=gr.update(), log=gr.update(), eta=gr.update(),
         net=gr.update(), stats=gr.update(), rs=None):
    return (status, summary, transcript, dialogue, profiles, analytics,
            combined, interview,
            dl_t, dl_s, dl_r, dl_c, dl_j, dl_p,
            dl_srt, dl_vtt, dl_docx,
            dl_acc, log, eta, net, stats, rs)


# Pricing: (input $/MTok, output $/MTok)
_MODEL_PRICING: dict[str, tuple[float, float]] = {
    # Claude
    "claude-opus-4-8":              (15.00, 75.00),
    "claude-sonnet-4-6":            ( 3.00, 15.00),
    "claude-haiku-4-5-20251001":    ( 0.80,  4.00),
    "claude-3-5-sonnet-20241022":   ( 3.00, 15.00),
    "claude-3-5-haiku-20241022":    ( 0.80,  4.00),
    "claude-3-opus-20240229":       (15.00, 75.00),
    # OpenAI
    "gpt-4.1":                      ( 2.00,  8.00),
    "gpt-4.1-mini":                 ( 0.40,  1.60),
    "gpt-4.1-nano":                 ( 0.10,  0.40),
    "gpt-4o":                       ( 2.50, 10.00),
    "gpt-4o-mini":                  ( 0.15,  0.60),
    "gpt-4-turbo":                  (10.00, 30.00),
    "o1":                           (15.00, 60.00),
    "o1-mini":                      ( 3.00, 12.00),
    "o3":                           (10.00, 40.00),
    "o3-mini":                      ( 1.10,  4.40),
    # Gemini
    "gemini-2.5-pro":               ( 1.25, 10.00),
    "gemini-2.5-flash":             ( 0.15,  0.60),
    "gemini-2.0-flash":             ( 0.075, 0.30),
    "gemini-2.5-pro-preview-05-06": ( 1.25,  5.00),
    "gemini-2.0-flash-exp":         ( 0.075, 0.30),
    "gemini-1.5-pro":               ( 1.25,  5.00),
    "gemini-1.5-flash":             ( 0.075, 0.30),
    "gemini-1.5-flash-8b":          ( 0.0375,0.15),
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
) -> str:
    """Translate raw transcript text to target_language."""
    _model = model or ("claude-sonnet-4-6" if provider == "anthropic" else "gpt-4o")
    client = LLMClient(provider=provider, api_key=api_key, model=_model, base_url=base_url)
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
    # ── Map stage → which sub-steps are done/active/waiting ──────────────────
    # Phases: p1 = Transcription, p2 = AI Analysis, p3 = Complete
    # Sub-steps per phase:
    #   p1: Load (📁), Extract (🔊), Transcribe (🎤)
    #   p2: Analyze (🤖)
    #   p3: Done (✅)

    def _ss(state):
        if state == "done":
            return "var(--ta-step-done-bg)", "var(--ta-step-done-bdr)", "var(--ta-step-done-clr)", "✓"
        if state == "active":
            return "var(--ta-step-act-bg)", "var(--ta-step-act-bdr)", "var(--ta-step-act-clr)", "●"
        return "var(--ta-step-wait-bg)", "var(--ta-step-wait-bdr)", "var(--ta-step-wait-clr)", "○"

    if done:
        p1_steps = ["done","done","done"]; p1_state = "done"; p1_hint = "Transcription complete"
        p2_state = "done"; p2_hint = "AI analysis complete"
        p3_state = "done"; p3_hint = "All done!"
        conn1 = conn2 = "var(--ta-conn-line-done)"
    elif stage in ("loading",):
        p1_steps = ["active","waiting","waiting"]; p1_state = "active"; p1_hint = "Loading file…"
        p2_state = "waiting"; p2_hint = "Waiting"
        p3_state = "waiting"; p3_hint = ""
        conn1 = conn2 = "var(--ta-conn-line-wait)"
    elif stage == "extracting":
        p1_steps = ["done","active","waiting"]; p1_state = "active"; p1_hint = "Extracting audio…"
        p2_state = "waiting"; p2_hint = "Waiting"
        p3_state = "waiting"; p3_hint = ""
        conn1 = conn2 = "var(--ta-conn-line-wait)"
    elif stage == "whisper":
        p1_steps = ["done","done","active"]; p1_state = "active"; p1_hint = "Converting speech…"
        p2_state = "waiting"; p2_hint = "Waiting"
        p3_state = "waiting"; p3_hint = ""
        conn1 = conn2 = "var(--ta-conn-line-wait)"
    elif stage in ("claude","interview"):
        p1_steps = ["done","done","done"]; p1_state = "done"; p1_hint = "Transcription complete"
        hint = "Reading & analyzing…" if stage == "claude" else "Scoring interview responses…"
        p2_state = "active"; p2_hint = hint
        p3_state = "waiting"; p3_hint = ""
        conn1 = "var(--ta-conn-line-done)"; conn2 = "var(--ta-conn-line-wait)"
    else:
        p1_steps = ["active","waiting","waiting"]; p1_state = "active"; p1_hint = "Starting…"
        p2_state = "waiting"; p2_hint = ""; p3_state = "waiting"; p3_hint = ""
        conn1 = conn2 = "var(--ta-conn-line-wait)"

    def _phase_box(phase_label, sub_labels, sub_states, phase_state, hint):
        if phase_state == "done":
            bb, bd, bt = "var(--ta-step-done-bg)", "var(--ta-step-done-bdr)", "var(--ta-step-done-clr)"
        elif phase_state == "active":
            bb, bd, bt = "var(--ta-step-act-bg)", "var(--ta-step-act-bdr)", "var(--ta-step-act-clr)"
        else:
            bb, bd, bt = "var(--ta-step-wait-bg)", "var(--ta-step-wait-bdr)", "var(--ta-step-wait-clr)"

        # sub-step dots
        dots_html = ""
        for j, (icon, label) in enumerate(sub_labels):
            state = sub_states[j] if j < len(sub_states) else "waiting"
            bg, bdr, clr, dot = _ss(state)
            dots_html += (
                f'<div style="display:flex;flex-direction:column;align-items:center;gap:3px;">'
                f'<div style="background:{bg};border:1.5px solid {bdr};border-radius:50%;'
                f'width:28px;height:28px;display:flex;align-items:center;justify-content:center;'
                f'font-size:0.85em;font-weight:800;color:{clr};transition:all 0.35s;">{dot}</div>'
                f'<div style="font-size:0.6em;font-weight:600;color:{clr};text-align:center;'
                f'white-space:nowrap;">{icon}</div>'
                f'</div>'
            )
            if j < len(sub_labels) - 1:
                ac = "var(--ta-conn-line-done)" if sub_states[j] == "done" else "var(--ta-conn-line-wait)"
                dots_html += (
                    f'<div style="height:1.5px;width:14px;background:{ac};'
                    f'margin-top:14px;flex-shrink:0;transition:background 0.35s;"></div>'
                )

        hint_html = (
            f'<div style="font-size:0.66em;color:{bt};margin-top:5px;font-weight:500;'
            f'letter-spacing:0.01em;min-height:10px;">{hint}</div>'
        ) if hint else '<div style="min-height:10px;"></div>'

        return (
            f'<div style="flex:1;background:{bb};border:1.5px solid {bd};border-radius:10px;'
            f'padding:8px 10px;min-width:0;transition:all 0.35s;">'
            f'<div style="font-size:0.63em;font-weight:800;text-transform:uppercase;'
            f'letter-spacing:0.1em;color:{bt};margin-bottom:7px;">{phase_label}</div>'
            f'<div style="display:flex;align-items:center;gap:0;">{dots_html}</div>'
            f'{hint_html}'
            f'</div>'
        )

    p1_box = _phase_box(
        "Step 1 · Transcription",
        [("📁","Load"), ("🔊","Extract"), ("🎤","Transcribe")],
        p1_steps, p1_state, p1_hint,
    )
    p2_box = _phase_box(
        "Step 2 · AI Analysis",
        [("🤖","Analyze")],
        [p2_state], p2_state, p2_hint,
    )
    p3_box = _phase_box(
        "Step 3 · Complete",
        [("✅","Done")],
        [p3_state], p3_state, p3_hint,
    )

    def _connector(color):
        return (
            f'<div style="width:18px;height:2px;background:{color};flex-shrink:0;'
            f'margin-top:28px;border-radius:2px;transition:background 0.4s;"></div>'
        )

    return (
        f'<div style="display:flex;align-items:flex-start;gap:0;margin-bottom:10px;">'
        f'{p1_box}{_connector(conn1)}{p2_box}{_connector(conn2)}{p3_box}'
        f'</div>'
    )


def _net_panel_html(direction: str, received: int, total: int,
                    speed_bps: float = 0, done: bool = False) -> str:
    if done:
        return ""
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
        "loading":    ("var(--ta-step-act-bdr)",  "Starting up…",        "Step 0 of 3", "var(--ta-step-act-clr)"),
        "extracting": ("var(--ta-step-done-bdr)", "Extracting audio…",   "Step 1 of 3", "var(--ta-step-done-clr)"),
        "whisper":    ("var(--ta-step-act-bdr)",  "Transcribing audio…", "Step 1 of 3", "var(--ta-step-act-clr)"),
        "claude":     ("#a855f7",                 "Analyzing with AI…",  "Step 2 of 3", "#c4b5fd"),
    }
    color, label, step, text_clr = stage_cfg.get(
        stage, ("var(--ta-card-border)", "Processing…", "", "var(--ta-card-sub)")
    )

    # ── Claude stage: elapsed-based simulated percentage ──────────────────────
    if stage == "claude":
        import math as _math, re as _re
        _m = _re.match(r'(?:(\d+)m\s*)?(\d+)s', elapsed or "")
        _elapsed_s = (int(_m.group(1) or 0) * 60 + int(_m.group(2) or 0)) if _m else 0
        # Asymptotic curve: grows fast → slows → caps at 92% until done
        ai_pct   = min(92, int(100 * (1 - _math.exp(-_elapsed_s / 40)))) if _elapsed_s > 0 else 5
        # Estimate remaining: assume average total AI time ~60 s, remaining ∝ (100-ai_pct)
        _est_rem = max(0, int((100 - ai_pct) * 0.6))
        eta_str  = _fmt_eta(_est_rem) if _est_rem > 3 else "Almost done…"

        def _stat(lbl, val):
            return (f'<div style="background:var(--ta-stat-bg);border-radius:8px;'
                    f'padding:8px 14px;flex:1;min-width:80px;text-align:center;">'
                    f'<div style="font-size:0.68em;font-weight:700;text-transform:uppercase;'
                    f'letter-spacing:0.08em;color:var(--ta-stat-label);">{lbl}</div>'
                    f'<div style="font-size:1.3em;font-weight:800;color:var(--ta-stat-val);'
                    f'font-family:monospace;">{val}</div></div>')

        return tracker + (
            '<div style="background:var(--ta-card-bg);border:2px solid #a855f7;'
            'border-radius:16px;padding:24px 28px;font-family:sans-serif;">'
            '<div style="font-size:0.72em;font-weight:700;text-transform:uppercase;'
            'letter-spacing:0.1em;color:#c4b5fd;margin-bottom:12px;">'
            f'Step 2 of 3 &nbsp;&mdash;&nbsp; Analyzing with AI</div>'
            '<div style="display:flex;align-items:flex-end;gap:4px;margin-bottom:14px;">'
            f'<div style="font-size:4.5em;font-weight:900;color:#c4b5fd;'
            f'font-family:monospace;line-height:1;letter-spacing:-0.04em;">{ai_pct}</div>'
            '<div style="font-size:2em;font-weight:700;color:#a855f7;margin-bottom:6px;">%</div>'
            '</div>'
            '<div style="background:var(--ta-step-wait-bg);border-radius:8px;height:14px;'
            'overflow:hidden;margin-bottom:10px;">'
            f'<div style="width:{ai_pct}%;height:100%;'
            'background:linear-gradient(90deg,#a855f7,#c4b5fd);'
            'border-radius:8px;transition:width 0.6s ease;"></div></div>'
            '<div style="display:flex;gap:10px;flex-wrap:wrap;">'
            + _stat("AI Progress", f"{ai_pct}%")
            + _stat("ETA", eta_str)
            + _stat("Elapsed", elapsed) +
            '</div>'
            '<div style="font-size:0.82em;color:#c4b5fd;margin-top:10px;">'
            '🤖 Reading transcript and writing your report…</div>'
            '</div>'
        )

    overlay_pct = ""
    if stage in ("loading", "extracting"):
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
    num_speakers,
    whisper_model,
    stt_engine,
    stt_api_key,
    stt_model,
    interview_mode,
    interview_deep,
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
    _total_dl_mb  = 0.0   # must be initialised before the URL-download section
    _peak_dl_speed = 0.0

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
                    f'<div><span style="color:#64748b;">[{ts}]</span> '
                    f'<span style="color:{color};{weight}">{text}</span></div>'
                )
            else:
                parts.append(
                    f'<div><span style="color:#64748b;">[{ts}]</span> '
                    f'<span style="color:{color};{weight}">{text}</span></div>'
                )
        scroll = '<div id="ta-log-end"></div><script>document.getElementById("ta-log-end")?.scrollIntoView();</script>'
        inner = "".join(parts) + scroll if parts else '<span style="color:#64748b;">Starting…</span>'
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
                        log=log, net=net_html,
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
                history_path=HISTORY_PATH,
                on_whisper_progress=on_whisper_progress if is_av else None,
                on_raw_transcript=on_raw_transcript if is_av else None,
                on_stage_change=on_stage_change if is_av else None,
                on_stt_done=lambda s: q.put(("stt_done", s)),
                on_token_usage=lambda i, o: q.put(("tokens", i, o)),
                on_log=on_log,
            )
            q.put(("done", result))
        except ImportError as e:
            # Missing optional SDK — give a clear install instruction
            pkg = str(e)
            q.put(("error", f"Missing package — run this in your terminal and restart:\n\n  pip install {pkg.split('pip install ')[-1].strip()}\n\n({pkg})"))
        except Exception as e:
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
    _tok_in         = 0       # accumulate token counts
    _tok_out        = 0
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

            # Strip any transcript/full-text section Claude may embed inside result.summary
            _summary_text = re.sub(
                r'\n*#{1,3}\s*(Full\s+)?Transcript[\s\S]*',
                '', result.summary, flags=re.IGNORECASE
            ).strip()
            summary_md = f"## Summary\n\n{_summary_text}"
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

            # ── Build Interview coaching tab ──────────────────────────────────
            ia = result.interview_analysis
            if ia and not ia.get("parse_error"):
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
                iv_html = (
                    f'<div style="padding:4px 0;">'
                    # ── Score hero card ──────────────────────────────────────
                    f'<div style="background:{_score_bg};border-radius:16px;'
                    f'padding:20px 24px;margin-bottom:20px;'
                    f'display:flex;align-items:center;gap:20px;">'
                    # Big score number
                    f'<div style="background:rgba(255,255,255,0.15);border-radius:12px;'
                    f'padding:10px 18px;text-align:center;min-width:80px;">'
                    f'<div style="font-size:2.6em;font-weight:900;color:#fff;line-height:1;">'
                    f'{_score_val}</div>'
                    f'<div style="font-size:0.75em;font-weight:700;color:rgba(255,255,255,0.75);'
                    f'letter-spacing:0.08em;text-transform:uppercase;">out of 10</div>'
                    f'</div>'
                    # Verdict text
                    f'<div>'
                    f'<div style="font-size:0.72em;font-weight:700;text-transform:uppercase;'
                    f'letter-spacing:0.1em;color:rgba(255,255,255,0.7);margin-bottom:4px;">'
                    f'🎯 Overall Score</div>'
                    f'<div style="font-size:1.3em;font-weight:800;color:#fff;">{_verdict}</div>'
                    f'</div>'
                    f'</div>'
                )
                _DEFLECT_STYLE = {
                    "partial": ("⚠️ Deflected", "#f59e0b", "#fffbeb", "#fde68a"),
                    "full":    ("🚫 Did Not Answer", "#ef4444", "#fef2f2", "#fecaca"),
                }
                for q in qs:
                    sc  = q.get("score","")
                    col = _SCORE_COLOR.get(sc, "#6b7280")
                    # Support both old field names and new
                    answer_said  = q.get("answer_said") or q.get("answer_summary","")
                    model_answer = q.get("model_answer") or q.get("ideal_answer","")
                    coaching_tip = q.get("coaching_tip","")
                    deflection   = (q.get("deflection") or "none").lower().strip()
                    defl_note    = q.get("deflection_note","")

                    # Build deflection badge HTML
                    defl_html = ""
                    if deflection in _DEFLECT_STYLE:
                        dlbl, dcol, dbg, dbdr = _DEFLECT_STYLE[deflection]
                        defl_html = (
                            f'<div style="background:{dbg};border:1px solid {dbdr};border-radius:8px;'
                            f'padding:7px 12px;margin-bottom:10px;display:flex;align-items:flex-start;gap:8px;">'
                            f'<span style="font-size:0.78em;font-weight:700;color:{dcol};white-space:nowrap;">{dlbl}</span>'
                            + (f'<span style="font-size:0.78em;color:#374151;">{defl_note}</span>' if defl_note else '')
                            + f'</div>'
                        )

                    iv_html += (
                        f'<div style="border:2px solid {col};border-radius:14px;'
                        f'padding:16px 18px;margin-bottom:20px;background:#fff;">'
                        # Q number chip + question text
                        f'<div style="display:flex;gap:10px;align-items:flex-start;margin-bottom:10px;">'
                        f'<span style="background:{col};color:#fff;font-size:0.78em;font-weight:800;'
                        f'padding:3px 10px;border-radius:20px;white-space:nowrap;flex-shrink:0;margin-top:2px;">'
                        f'Q{q.get("id","")}</span>'
                        f'<div style="font-weight:700;font-size:1em;color:#0f172a;line-height:1.5;">'
                        f'{q.get("question","")}</div>'
                        f'</div>'
                        # Score badge + reason on same row
                        f'<div style="display:flex;gap:8px;margin-bottom:12px;flex-wrap:wrap;align-items:center;">'
                        f'<span style="background:{col};color:#fff;font-size:0.8em;font-weight:800;'
                        f'padding:4px 14px;border-radius:20px;">{sc}</span>'
                        f'<span style="font-size:0.85em;color:#334155;font-weight:500;">'
                        f'{q.get("score_reason","")}</span>'
                        f'</div>'
                        + defl_html
                        # What they said
                        + f'<div style="background:#f1f5f9;border-left:4px solid #94a3b8;border-radius:0 8px 8px 0;'
                        f'padding:12px 14px;margin-bottom:10px;">'
                        f'<div style="font-size:0.75em;font-weight:800;text-transform:uppercase;'
                        f'letter-spacing:0.08em;color:#475569;margin-bottom:6px;">📝 What they said</div>'
                        f'<p style="font-size:0.88em;line-height:1.7;margin:0;color:#1e293b;">{answer_said}</p>'
                        f'</div>'
                        # What you could have said
                        f'<div style="background:#f0fdf4;border-left:4px solid #22c55e;border-radius:0 8px 8px 0;'
                        f'padding:12px 14px;margin-bottom:10px;">'
                        f'<div style="font-size:0.75em;font-weight:800;text-transform:uppercase;'
                        f'letter-spacing:0.08em;color:#15803d;margin-bottom:6px;">💬 What you could have said</div>'
                        f'<p style="font-size:0.88em;line-height:1.7;margin:0;color:#14532d;font-style:italic;">'
                        f'{model_answer}</p>'
                        f'</div>'
                        # Coaching tip
                        + (f'<div style="background:#faf5ff;border-left:4px solid #a855f7;border-radius:0 8px 8px 0;'
                        f'padding:12px 14px;">'
                        f'<div style="font-size:0.75em;font-weight:800;text-transform:uppercase;'
                        f'letter-spacing:0.08em;color:#7c3aed;margin-bottom:6px;">🏋️ Coaching Tip</div>'
                        f'<p style="font-size:0.88em;margin:0;color:#3b0764;">{coaching_tip}</p></div>'
                        if coaching_tip else '')
                        + f'</div>'
                    )
                # Deep mode extras
                if ia.get("advance_likelihood"):
                    iv_html += (
                        f'<div style="margin-top:12px;padding:12px 16px;background:#eff6ff;'
                        f'border:1px solid #bfdbfe;border-radius:12px;">'
                        f'<div style="font-weight:700;margin-bottom:4px;">🔬 Deep Analysis</div>'
                        f'<div>Deflection rate: <b>{ia.get("deflection_rate","—")}%</b> · '
                        f'Advance likelihood: <b>{ia.get("advance_likelihood","—")}%</b></div>'
                        f'<div style="font-size:0.82em;margin-top:4px;color:#475569;">'
                        f'{ia.get("advance_reasoning","")}</div>'
                        f'</div>'
                    )
                iv_html += '</div>'
            elif ia and ia.get("parse_error"):
                iv_html = f'<pre style="font-size:0.8em;overflow:auto;">{ia.get("raw","")}</pre>'
            else:
                iv_html = '<p style="color:#94a3b8;">Enable <b>Interview Mode</b> before analyzing to see coaching results here.</p>'

            # ── STT timing into the log ───────────────────────────────────────
            if result.stt_seconds > 0:
                eng_label = STT_ENGINES.get(result.stt_engine, result.stt_engine)
                log_text = _add_log(f"🎤 {eng_label} — {result.stt_seconds:.1f}s", "done")

            f_t    = str(job_dir / f"{stem}_transcript.txt")
            f_s    = str(job_dir / f"{stem}_speakers.txt")
            f_r    = str(job_dir / f"{stem}_report.md")
            f_c    = str(job_dir / f"{stem}_combined.txt")
            f_j    = str(job_dir / f"{stem}_full.json")
            f_srt  = str(job_dir / f"{stem}.srt")   if (job_dir / f"{stem}.srt").exists()  else None
            f_vtt  = str(job_dir / f"{stem}.vtt")   if (job_dir / f"{stem}.vtt").exists()  else None
            f_docx = str(job_dir / f"{stem}_report.docx") if (job_dir / f"{stem}_report.docx").exists() else None
            f_p_path = job_dir / f"{stem}_report.pdf"
            try:
                _generate_pdf(stem, combined_text, f_p_path)
                f_p = str(f_p_path)
            except Exception:
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
                    )
                    if result.speaker_dialogue:
                        _display_dialogue = _translate_transcript(
                            result.speaker_dialogue, _out_lang,
                            api_key, provider_type, model_name, base_url,
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
                stats=_stats_panel_html(total_elapsed, _tok_in, _tok_out,
                                        _total_dl_mb, _peak_dl_speed, done=True,
                                        model_name=model_name, provider_type=provider_type),
                rs={"stem": stem, "combined_text": combined_text,
                    "detected_language": result.detected_language,
                    "out_dir": str(job_dir)},
                log=log_text,
            )
            break

        elif kind == "error":
            log_text = _add_log(f"🚨 {msg[1]}", "error")
            yield _out(log=log_text)
            yield _err(f"Processing failed: {msg[1]}")
            break
    finally:
        _allow_sleep()


def toggle_speakers(is_panel):
    return gr.update(visible=is_panel)


_STT_MODELS = {
    "whisper_local":  [],   # handled by whisper_input dropdown
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


def toggle_stt_engine(engine, main_api_key=""):
    is_local = engine == "whisper_local"
    models   = _STT_MODELS.get(engine, [])

    # Build a single update for stt_key_input (visibility + optional auto-fill value)
    key_kw = {"visible": not is_local}
    if engine in _STT_AUTOFILL_PREFIX:
        prefix, _ = _STT_AUTOFILL_PREFIX[engine]
        if (main_api_key or "").startswith(prefix):
            key_kw["value"] = main_api_key

    # whisper_input visibility is handled by JS (ta-whisper-size) to avoid
    # a Gradio 6.15.x bug where dropdown visible updates corrupt the value.
    return (
        gr.update(**key_kw),                                                # stt_key_input
        gr.update(visible=not is_local and bool(models),                    # stt_model_input
                  choices=models, value=models[0] if models else None),
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
<div class="ta-hero">

  <!-- ambient glow blobs -->
  <div class="ta-hero-blob-tr"></div>
  <div class="ta-hero-blob-bl"></div>

  <!-- subtle dot-grid texture -->
  <div class="ta-hero-grid"></div>

  <div class="ta-hero-inner">

    <!-- logo + title row -->
    <div class="ta-hero-header">
      <div class="ta-hero-icon-box">
        <svg width="30" height="30" viewBox="0 0 30 30" fill="none">
          <!-- mic body -->
          <rect x="10" y="3" width="10" height="16" rx="5"
                fill="rgba(255,255,255,0.15)" stroke="rgba(255,255,255,0.85)" stroke-width="1.6"/>
          <!-- mic stand arc -->
          <path d="M5 17 Q5 25 15 25 Q25 25 25 17"
                stroke="rgba(255,255,255,0.7)" stroke-width="1.6"
                fill="none" stroke-linecap="round"/>
          <!-- mic stand stem -->
          <line x1="15" y1="25" x2="15" y2="29"
                stroke="rgba(255,255,255,0.7)" stroke-width="1.6" stroke-linecap="round"/>
          <!-- base line -->
          <line x1="10" y1="29" x2="20" y2="29"
                stroke="rgba(255,255,255,0.7)" stroke-width="1.6" stroke-linecap="round"/>
          <!-- waveform dots inside mic -->
          <circle cx="15" cy="9"  r="1.2" fill="rgba(255,255,255,0.7)"/>
          <circle cx="15" cy="13" r="1.2" fill="rgba(255,255,255,0.7)"/>
        </svg>
      </div>

      <div class="ta-hero-title-block">
        <div class="ta-hero-eyebrow">AI Transcription Platform</div>
        <div class="ta-hero-title">Transcript Agent</div>
        <div class="ta-hero-sub">
          Whisper transcription &nbsp;&middot;&nbsp;
          Multi-provider AI &nbsp;&middot;&nbsp;
          Speaker diarization
        </div>
      </div>
    </div>

    <!-- stats strip -->
    <div class="ta-hero-stats">
      <div class="ta-hero-stat">
        <span class="ta-hero-stat-n">8</span>
        <span class="ta-hero-stat-l">AI Providers</span>
      </div>
      <div class="ta-hero-stat-sep"></div>
      <div class="ta-hero-stat">
        <span class="ta-hero-stat-n">37+</span>
        <span class="ta-hero-stat-l">Languages</span>
      </div>
      <div class="ta-hero-stat-sep"></div>
      <div class="ta-hero-stat">
        <span class="ta-hero-stat-n">12</span>
        <span class="ta-hero-stat-l">File Formats</span>
      </div>
      <div class="ta-hero-stat-sep"></div>
      <div class="ta-hero-stat">
        <span class="ta-hero-stat-n">100%</span>
        <span class="ta-hero-stat-l">Private</span>
      </div>
    </div>

    <!-- feature chips -->
    <div class="ta-hero-chips">
      <span class="ta-hero-chip ta-hc-blue">🎵 Audio &amp; Video</span>
      <span class="ta-hero-chip ta-hc-blue">📄 Documents</span>
      <span class="ta-hero-chip ta-hc-purple">🗣️ Speaker Diarization</span>
      <span class="ta-hero-chip ta-hc-blue">📊 Speech Analytics</span>
      <span class="ta-hero-chip ta-hc-indigo">🌐 37+ Languages</span>
    </div>

  </div>
</div>
"""

_API_BANNER = """
<div id="api-banner" style="background:#fffcf0;border:1px solid #fde68a;
     border-radius:14px;padding:13px 18px;display:flex;align-items:center;gap:13px;
     transition:all 0.3s ease;margin-top:4px;box-shadow:0 1px 4px rgba(245,158,11,0.08);">
  <div id="api-banner-icon" style="font-size:1.3em;flex-shrink:0;transition:all 0.25s;">🔑</div>
  <div style="flex:1;min-width:0;">
    <div id="api-banner-title" style="font-weight:700;color:#92400e;font-size:0.85em;
         transition:color 0.25s;letter-spacing:0.01em;">API Key Required</div>
    <div id="api-banner-sub" style="color:#a16207;font-size:0.77em;margin-top:1px;transition:color 0.25s;">
      Enter your provider API key below — billed directly to your account, never stored on this server.
    </div>
  </div>
  <div id="api-banner-badge" style="display:none;background:linear-gradient(135deg,#22c55e,#16a34a);
       color:#fff;font-size:0.68em;font-weight:700;padding:4px 11px;border-radius:20px;
       letter-spacing:0.06em;flex-shrink:0;box-shadow:0 2px 8px rgba(34,197,94,0.3);">
    APPROVED ✓
  </div>
</div>
"""

_THEME_TOGGLE = """
<!-- ☀️🌙 Light / Dark toggle pill -->
<div id="ta-widget"
  style="position:fixed;top:14px;right:18px;z-index:9999;display:flex;align-items:center;
         background:rgba(255,255,255,0.96);backdrop-filter:blur(12px);
         border:1px solid #e2e8f0;border-radius:30px;padding:4px;
         box-shadow:0 2px 14px rgba(0,0,0,0.13);gap:2px;transition:background 0.3s,border-color 0.3s;">
  <button id="ta-btn-light"
    style="display:flex;align-items:center;gap:5px;padding:6px 14px;border-radius:24px;
           border:none;cursor:pointer;font-size:0.82em;font-weight:700;
           background:#3b82f6;color:#fff;transition:all 0.22s;box-shadow:0 2px 6px rgba(59,130,246,0.4);">
    ☀️ Light
  </button>
  <button id="ta-btn-dark"
    style="display:flex;align-items:center;gap:5px;padding:6px 14px;border-radius:24px;
           border:none;cursor:pointer;font-size:0.82em;font-weight:700;
           background:transparent;color:#64748b;transition:all 0.22s;">
    🌙 Dark
  </button>
</div>

<!-- ▶ Floating play button — dark/light aware -->
<div id="ta-float-wrap"
  style="position:fixed;bottom:28px;right:28px;z-index:9998;display:flex;flex-direction:column;
         align-items:center;gap:8px;">
  <div id="ta-float-label"
    style="font-size:0.7em;font-weight:700;letter-spacing:0.06em;text-transform:uppercase;
           color:#fff;background:rgba(29,78,216,0.85);backdrop-filter:blur(6px);
           padding:3px 10px;border-radius:12px;opacity:0;transition:opacity 0.2s;
           pointer-events:none;white-space:nowrap;">
    Analyze
  </div>
  <button id="ta-float-analyze"
    style="width:56px;height:56px;border-radius:50%;border:none;cursor:pointer;
           background:linear-gradient(135deg,#1d4ed8,#3b82f6);color:#fff;
           font-size:1.4em;display:flex;align-items:center;justify-content:center;
           box-shadow:0 4px 20px rgba(29,78,216,0.5);transition:all 0.2s;
           outline:none;">
    ▶
  </button>
</div>
"""

# ── Theme JS — injected via gr.Blocks(js=...) which is the guaranteed execution
# path. gr.HTML uses Svelte {#html} which deliberately does NOT run <script> tags.
_THEME_JS = """
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

(function(){
  window.__taThemeRan = true;
  var _dark = false;

  /* ── Inject toggle widget directly into <body> so Gradio can never remove it ─
     We do NOT use gr.HTML() for the buttons — Gradio 6 re-renders those
     components and strips IDs/styles. Injecting via JS is permanent.           */
  function _injectToggle() {
    if (document.getElementById('ta-widget')) return;
    var w = document.createElement('div');
    w.id = 'ta-widget';
    w.style.cssText = (
      'position:fixed;top:14px;right:18px;z-index:9999;display:flex;align-items:center;'
      + 'background:rgba(255,255,255,0.96);backdrop-filter:blur(12px);'
      + 'border:1px solid #e2e8f0;border-radius:30px;padding:4px;'
      + 'box-shadow:0 2px 14px rgba(0,0,0,0.13);gap:2px;'
    );
    w.innerHTML = (
      '<button id="ta-btn-light" style="display:flex;align-items:center;gap:5px;padding:6px 14px;'
      + 'border-radius:24px;border:none;cursor:pointer;font-size:0.82em;font-weight:700;'
      + 'background:#3b82f6;color:#fff;box-shadow:0 2px 6px rgba(59,130,246,0.4);">☀️ Light</button>'
      + '<button id="ta-btn-dark" style="display:flex;align-items:center;gap:5px;padding:6px 14px;'
      + 'border-radius:24px;border:none;cursor:pointer;font-size:0.82em;font-weight:700;'
      + 'background:transparent;color:#64748b;">🌙 Dark</button>'
    );
    document.body.appendChild(w);

    /* Also inject floating analyze button */
    if (!document.getElementById('ta-float-wrap')) {
      var fw = document.createElement('div');
      fw.id = 'ta-float-wrap';
      fw.style.cssText = (
        'position:fixed;bottom:28px;right:28px;z-index:9998;display:flex;flex-direction:column;'
        + 'align-items:center;gap:8px;'
      );
      fw.innerHTML = (
        '<div id="ta-float-label" style="font-size:0.7em;font-weight:700;letter-spacing:0.06em;'
        + 'text-transform:uppercase;color:#fff;background:rgba(29,78,216,0.85);backdrop-filter:blur(6px);'
        + 'padding:3px 10px;border-radius:12px;opacity:0;transition:opacity 0.2s;pointer-events:none;'
        + 'white-space:nowrap;">Analyze</div>'
        + '<button id="ta-float-analyze" style="width:56px;height:56px;border-radius:50%;border:none;'
        + 'cursor:pointer;background:linear-gradient(135deg,#1d4ed8,#3b82f6);color:#fff;'
        + 'font-size:1.4em;display:flex;align-items:center;justify-content:center;'
        + 'box-shadow:0 4px 20px rgba(29,78,216,0.5);outline:none;">▶</button>'
      );
      document.body.appendChild(fw);
    }
  }
  /* Run immediately and re-check periodically in case body isn't ready yet */
  document.body ? _injectToggle() : document.addEventListener('DOMContentLoaded', _injectToggle);
  setInterval(function(){ if (!document.getElementById('ta-widget')) _injectToggle(); }, 2000);

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
      '.tabs>.tab-nav button{font-weight:600!important;font-size:0.84em!important;padding:10px 16px!important;border-radius:8px 8px 0 0!important;letter-spacing:0.01em!important;transition:all 0.15s!important}',
      '.tabs>.tab-nav button.selected{color:#2563eb!important;border-bottom:2px solid #2563eb!important;margin-bottom:-2px!important}',
      /* ── Accordions ── */
      '.accordion,.details{border-radius:12px!important;border:1px solid #e8edf4!important}',
      /* ── Analyze button — compact, pill style ── */
      '.ta-analyze-btn button{background:linear-gradient(135deg,#1d4ed8,#3b82f6)!important;color:#fff!important;font-size:0.9em!important;font-weight:700!important;border:none!important;border-radius:8px!important;padding:8px 18px!important;box-shadow:0 3px 12px rgba(29,78,216,0.35)!important;letter-spacing:0.02em!important;transition:all 0.18s!important;width:100%!important}',
      '.ta-analyze-btn button:hover{transform:translateY(-1px)!important;box-shadow:0 5px 18px rgba(29,78,216,0.48)!important}',
      /* ── Cancel / stop button — tiny square ── */
      'button[aria-label="Stop / Cancel"],button.stop-btn{background:#fff!important;color:#dc2626!important;border:1.5px solid #fca5a5!important;border-radius:8px!important;font-size:0.85em!important;font-weight:700!important;padding:6px 10px!important;transition:all 0.15s!important;width:100%!important;margin-top:4px!important}',
      'button[aria-label="Stop / Cancel"]:hover,button.stop-btn:hover{background:#fef2f2!important;border-color:#ef4444!important}',
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
      /* ── Cancel button — tiny square in results panel ── */
      '.ta-cancel-btn{flex:0 0 34px!important;min-width:34px!important;max-width:34px!important}',
      '.ta-cancel-btn button{width:34px!important;height:34px!important;padding:0!important;border-radius:7px!important;font-size:0.82em!important;font-weight:700!important;line-height:1!important;box-shadow:none!important;flex-shrink:0!important}',
      /* Status bar fills remaining width */
      '.ta-status-bar{flex:1 1 auto!important;min-width:0!important}',
      /* ── Network monitor panel ── */
      '#ta-net-monitor{transition:all 0.3s}',
      'html.dark #ta-net-monitor .ta-net-card{background:#1e293b!important;border-color:rgba(59,130,246,0.25)!important}',
      '#live-log,#live-log>*{background:#0f172a!important;border-color:#1e3a5f!important}',
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
    'html.dark .ta-analyze-btn button{background:linear-gradient(135deg,#1e40af,#3b82f6)!important;color:#fff!important;border:none!important;box-shadow:0 3px 12px rgba(29,78,216,0.5)!important}',
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

    /* ── Toggle pill visuals ── */
    var bl = document.getElementById('ta-btn-light');
    var bd = document.getElementById('ta-btn-dark');
    var wg = document.getElementById('ta-widget');
    if (bl && bd) {
      /* Active pill: solid blue with shadow. Inactive: transparent, muted text */
      bl.style.cssText = 'display:flex;align-items:center;gap:5px;padding:6px 14px;border-radius:24px;border:none;cursor:pointer;font-size:0.82em;font-weight:700;transition:all 0.22s;'
        + (dark ? 'background:transparent;color:#64748b;box-shadow:none;'
                : 'background:#3b82f6;color:#fff;box-shadow:0 2px 6px rgba(59,130,246,0.4);');
      bd.style.cssText = 'display:flex;align-items:center;gap:5px;padding:6px 14px;border-radius:24px;border:none;cursor:pointer;font-size:0.82em;font-weight:700;transition:all 0.22s;'
        + (dark ? 'background:#3b82f6;color:#fff;box-shadow:0 2px 6px rgba(59,130,246,0.4);'
                : 'background:transparent;color:#64748b;box-shadow:none;');
    }
    if (wg) {
      wg.style.background  = dark ? 'rgba(15,23,42,0.96)' : 'rgba(255,255,255,0.96)';
      wg.style.borderColor = dark ? '#334155' : '#e2e8f0';
    }

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
      'ta-whisper-size':     { type:'dropdown', key:'ta-whisper-size' },
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
      restoreDropdown('ta-whisper-size',     localStorage.getItem('ta-whisper-size'));

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

  /* ── ▶ Floating play button — wired via event delegation, dark/light aware ──
     Button HTML lives in _THEME_TOGGLE so it renders before JS runs.
     Here we just wire the click + hover + label tooltip.                    */
  (function(){
    /* Hover: show/hide "Analyze" label above the button */
    function wireHover() {
      var btn   = document.getElementById('ta-float-analyze');
      var label = document.getElementById('ta-float-label');
      if (!btn) { setTimeout(wireHover, 600); return; }
      btn.addEventListener('mouseenter', function(){
        this.style.transform = 'scale(1.1)';
        if (label) label.style.opacity = '1';
      });
      btn.addEventListener('mouseleave', function(){
        this.style.transform = 'scale(1)';
        if (label) label.style.opacity = '0';
      });
    }

    /* Find the real Analyze sidebar button by its CSS class (more reliable than text) */
    function wireAnalyze() {
      var real = document.querySelector('.ta-analyze-btn button');
      if (!real) { setTimeout(wireAnalyze, 800); return; }
      if (real.dataset.taWired) return;
      real.dataset.taWired = '1';

      function doAnalyze() {
        real.click();
        setTimeout(function(){
          var target = document.getElementById('ta-status-bar') || document.querySelector('.ta-status-bar');
          if (target) target.scrollIntoView({ behavior: 'smooth', block: 'start' });
        }, 400);
      }

      /* Event delegation — survives DOM re-mounts */
      document.addEventListener('click', function(e){
        if (e.target && (e.target.id === 'ta-float-analyze' || e.target.closest('#ta-float-analyze')))
          doAnalyze();
      });

      /* Sidebar button also scrolls */
      real.addEventListener('click', function(){
        setTimeout(function(){
          var target = document.getElementById('ta-status-bar') || document.querySelector('.ta-status-bar');
          if (target) target.scrollIntoView({ behavior: 'smooth', block: 'start' });
        }, 400);
      });
    }

    wireHover();
    setTimeout(wireAnalyze, 1500);
  })();


  /* ── STT Engine → show/hide Whisper model size (JS bypass for Gradio bug) ─── */
  (function() {
    var _lastState = null;
    function syncWhisperSize() {
      var whisperEl = document.getElementById('ta-whisper-size');
      if (!whisperEl) return;
      var whisperBlock = whisperEl.closest('.block') || whisperEl.parentElement;
      if (!whisperBlock) return;

      /* In Gradio 6.x, all dropdown selected values live in input.border-none elements.
         Find the one adjacent to the "STT Engine" label. */
      var inputs = Array.from(document.querySelectorAll('input.border-none'));
      var sttInput = null;
      for (var i = 0; i < inputs.length; i++) {
        /* Walk up to find the block, then check if the block's label says "STT Engine" */
        var block = inputs[i].closest('.block');
        if (block && block.textContent.indexOf('STT Engine') >= 0) {
          sttInput = inputs[i];
          break;
        }
      }

      /* Fall back: look for input whose value looks like an STT engine name */
      if (!sttInput) {
        for (var j = 0; j < inputs.length; j++) {
          var v = (inputs[j].value || '').toLowerCase();
          if (v.indexOf('whisper') >= 0 || v.indexOf('deepgram') >= 0 ||
              v.indexOf('assemblyai') >= 0 || v.indexOf('elevenlabs') >= 0 ||
              v.indexOf('groq') >= 0 || v.indexOf('openai whisper') >= 0) {
            sttInput = inputs[j];
            break;
          }
        }
      }

      var isLocal = true;
      if (sttInput) {
        var val = (sttInput.value || '').toLowerCase();
        isLocal = val === '' || val.indexOf('whisper (local') >= 0 || val.indexOf('offline') >= 0;
      }

      if (isLocal === _lastState) return;
      _lastState = isLocal;
      whisperBlock.style.display = isLocal ? '' : 'none';
    }

    setInterval(syncWhisperSize, 300);
    setTimeout(syncWhisperSize, 2000);
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

    /* ── Periodic background ping — keeps display live when idle ── */
    (function pingLoop() {
      var t0 = performance.now();
      fetch(window.location.pathname + '?_spd=' + Date.now(), {
        cache: 'no-store',
        headers: { 'Range': 'bytes=0-8191' }
      }).then(function(r) {
        return r.arrayBuffer();
      }).then(function(buf) {
        _pingMs = Math.round(performance.now() - t0);
        if (buf.byteLength > 0) _pushRx(buf.byteLength);
      }).catch(function() { _pingMs = 0; })
        .finally(function() { setTimeout(pingLoop, 6000); });
    })();

    /* mini bar — 16 segments */
    function _bars(bps, color) {
      var SEGS = 16, MAX = 5*1048576;
      var fill = Math.min(SEGS, Math.round(bps / MAX * SEGS));
      var out = '';
      for (var i = 0; i < SEGS; i++) {
        out += '<span style="display:inline-block;width:3px;height:'
             + (i < fill ? 12 : 4) + 'px;background:'
             + (i < fill ? color : 'var(--ta-card-border,#e2e8f0)')
             + ';border-radius:2px;margin:0 1px;vertical-align:middle;'
             + 'transition:height 0.2s,background 0.2s;"></span>';
      }
      return out;
    }

    function _dot(color) {
      return '<span style="width:7px;height:7px;background:' + color + ';border-radius:50%;'
           + 'display:inline-block;box-shadow:0 0 4px ' + color + ';'
           + 'animation:tapulse 2s ease-in-out infinite;"></span>';
    }

    /* one row: icon | label | bars | dot+speed | session total */
    function _row(icon, label, bps, total, color, extraDetail) {
      var speedTxt = fmtSpeed(bps);
      var totalTxt = fmtSize(total);
      return '<div style="display:flex;align-items:center;gap:6px;font-size:0.82em;padding:3px 0;">'
           + '<span style="font-size:1em;line-height:1;">' + icon + '</span>'
           + '<span style="font-weight:700;color:var(--ta-card-text,#1e293b);min-width:72px;">' + label + '</span>'
           + '<span style="display:flex;align-items:center;gap:1px;">' + _bars(bps, color) + '</span>'
           + '<span style="display:inline-flex;align-items:center;gap:4px;'
               + 'font-weight:800;color:' + color + ';min-width:80px;justify-content:flex-end;">'
             + _dot(color) + '&nbsp;' + speedTxt
           + '</span>'
           + '<span style="margin-left:auto;font-size:0.78em;color:var(--ta-card-sub,#64748b);'
               + 'white-space:nowrap;">session:&nbsp;' + totalTxt + '</span>'
           + (extraDetail || '')
           + '</div>';
    }

    function render() {
      var p = document.getElementById('ta-net-monitor');
      if (!p) return;

      var rxBps = _speed(_rxLog);
      var txBps = _speed(_txLog);

      var rxColor = rxBps > 1048576 ? '#22c55e' : rxBps > 102400 ? '#3b82f6' : '#94a3b8';
      var txColor = txBps > 1048576 ? '#22c55e' : txBps > 102400 ? '#7c3aed' : '#94a3b8';

      /* upload progress bar when active */
      var upDetail = '';
      if (_upActive && _upTotal > 0) {
        var pct = Math.min(100, _upLoaded / _upTotal * 100);
        var eta = txBps > 0 && _upTotal > _upLoaded ? Math.round((_upTotal - _upLoaded) / txBps) : 0;
        upDetail = '<div style="margin-top:4px;padding-left:82px;">'
          + '<div style="height:4px;background:var(--ta-card-border,#e2e8f0);border-radius:2px;overflow:hidden;margin-bottom:3px;">'
          + '<div style="width:' + pct.toFixed(0) + '%;height:100%;background:#7c3aed;border-radius:2px;transition:width 0.3s;"></div>'
          + '</div>'
          + '<span style="font-size:0.75em;color:var(--ta-card-sub,#64748b);">'
          + (_upLoaded/1048576).toFixed(1) + ' / ' + (_upTotal/1048576).toFixed(1) + ' MB'
          + ' · ' + pct.toFixed(0) + '%'
          + (eta > 0 ? ' · ETA ' + eta + 's' : '') + '</span></div>';
      }

      /* ping note when fully idle */
      var pingNote = '';
      if (rxBps < 512 && txBps < 512 && _pingMs > 0) {
        pingNote = '<div style="font-size:0.74em;color:var(--ta-card-sub,#64748b);padding-top:3px;">'
                 + '🏓 ping ' + _pingMs + ' ms</div>';
      }

      p.innerHTML = (
        '<style>'
        + '@keyframes tapulse{0%,100%{opacity:1}50%{opacity:0.3}}'
        + '</style>'
        + '<div style="background:var(--ta-card-bg,#f8fafc);border:1px solid var(--ta-card-border,#e2e8f0);'
        + 'border-radius:10px;padding:10px 14px;margin-top:8px;">'
        + '<div style="font-size:0.72em;font-weight:700;text-transform:uppercase;letter-spacing:0.08em;'
        + 'color:var(--ta-card-sub,#64748b);margin-bottom:6px;">🌐 Network — Live</div>'
        + _row('⬇️', 'Download', rxBps, _rxTotal, rxColor)
        + '<div style="height:1px;background:var(--ta-card-border,#e2e8f0);margin:4px 0;"></div>'
        + _row('⬆️', 'Upload',   txBps, _txTotal, txColor, upDetail)
        + pingNote
        + '</div>'
      );
    }

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

    /* ── Fetch intercept: SSE/streaming responses (download) ── */
    var _origFetch = window.fetch;
    window.fetch = function(url, opts) {
      /* track request body size as upload */
      if (opts && opts.body) {
        var bSz = 0;
        if (typeof opts.body === 'string')      bSz = opts.body.length;
        else if (opts.body && opts.body.byteLength) bSz = opts.body.byteLength;
        if (bSz > 0) _pushTx(bSz);
      }
      var result = _origFetch.apply(this, arguments);
      result.then(function(resp) {
        if (!resp || !resp.body) return;
        try {
          /* Clone so the original body stream is not consumed — Gradio calls
             response.json() on the original and would fail if we read it first */
          var clone = resp.clone();
          var reader = clone.body.getReader();
          (function pump(){
            reader.read().then(function(chunk){
              if (chunk.done) return;
              if (chunk.value) _pushRx(chunk.value.byteLength);
              pump();
            }).catch(function(){});
          })();
        } catch(ex) {}
      }).catch(function(){});
      return result;
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
     border-radius:16px;padding:32px 24px;text-align:center;
     transition:border-color 0.3s,background 0.3s;">
  <div style="font-size:2.2em;margin-bottom:10px;opacity:0.45;line-height:1;">📂</div>
  <div style="color:var(--ta-card-text);font-size:0.97em;font-weight:700;
       letter-spacing:-0.01em;margin-bottom:5px;">Ready to process</div>
  <div style="color:var(--ta-card-sub);font-size:0.82em;line-height:1.5;">
    Upload a file on the left, then click<br>
    <strong style="color:var(--ta-step-act-clr);font-weight:700;">Analyze File</strong> to begin
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
<div style="display:flex;align-items:center;gap:10px;margin:18px 0 8px;">
  <div style="height:1px;background:var(--ta-card-border);flex:1;opacity:0.7;"></div>
  <div style="font-size:0.67em;font-weight:700;text-transform:uppercase;letter-spacing:0.13em;
       color:var(--ta-card-sub);white-space:nowrap;padding:0 2px;">{label}</div>
  <div style="height:1px;background:var(--ta-card-border);flex:1;opacity:0.7;"></div>
</div>
"""

# ── Changelog ────────────────────────────────────────────────────────────────
_RELEASES = [
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

APP_VERSION = "1.1"

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
_GH_RELEASES_REPO = "jayuan101/transcript-agent-releases"

def _check_github_update() -> str:
    """Poll GitHub releases API; return update banner HTML or empty string."""
    import urllib.request as _ur, json as _json
    try:
        req = _ur.Request(
            f"https://api.github.com/repos/{_GH_RELEASES_REPO}/releases/latest",
            headers={"User-Agent": f"TranscriptAgent/{APP_VERSION}",
                     "Accept": "application/vnd.github.v3+json"},
        )
        with _ur.urlopen(req, timeout=6) as r:
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
        return _build_update_banner(latest_tag, win_url, mac_url, html_url, notes)
    except Exception:
        return ""


def _build_update_banner(latest_tag, win_url, mac_url, html_url, notes=""):
    we = win_url.replace("'", "\\'")
    me = mac_url.replace("'", "\\'")
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
      <button onclick="taDoUpdate('{we}',this,'win')" class="ta-upd-btn ta-upd-win">
        🪟 Update Windows
      </button>
      <button onclick="taDoUpdate('{me}',this,'mac')" class="ta-upd-btn ta-upd-mac">
        🍎 Update Mac
      </button>
      <a href="{html_url}" target="_blank"
         style="font-size:0.78em;color:#3b82f6;white-space:nowrap;font-weight:600;">
        Release notes →
      </a>
    </div>
  </div>
</div>
"""

# ── Desktop download section ──────────────────────────────────────────────────
_HF_RAW = "https://huggingface.co/spaces/Coastline6/transcript-agent/resolve/main"

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

with gr.Blocks(title="Transcript Agent") as demo:

    gr.HTML(_HERO)
    gr.HTML(_API_BANNER)
    update_banner = gr.HTML(value="", elem_id="ta-update-banner-wrap")
    # Theme toggle pill — rendered as static HTML, styled to fixed top-right.
    # Click handlers wired below via .click(fn=None, js=...) which IS executed by Gradio 6.x.
    gr.HTML(_THEME_TOGGLE)

    # ── Browser-persisted settings (single BrowserState per setting) ───────────
    # stt_engine is intentionally excluded — restoring it via demo.load() triggers
    # stt_engine.change() → _toggle_and_save_stt → causes STT Model to disappear.
    bsr_whisper  = bsw_whisper  = gr.BrowserState("base",   storage_key="ta-bs-whisper")
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
                whisper_input = gr.Dropdown(
                    label="Whisper model size",
                    choices=_WHISPER_SIZES,
                    value="base",
                    info="tiny = fastest · turbo = large speed  |  large-v3 = most accurate",
                    visible=True,
                    elem_id="ta-whisper-size",
                )
                stt_model_input = gr.Dropdown(
                    label="STT Model",
                    choices=[],
                    value=None,
                    visible=False,
                    allow_custom_value=True,
                )
                stt_key_input = gr.Textbox(
                    label="STT API Key",
                    placeholder="API key for the selected cloud STT engine",
                    type="password",
                    info="🔒 Saved in your browser only — never stored on this server",
                    visible=False,
                )
                panel_toggle = gr.Checkbox(value=False, visible=False)

            with gr.Accordion("🎤 Interview Mode", open=True):
                interview_toggle = gr.Checkbox(
                    label="Enable Interview Mode",
                    value=True,
                    info="Extracts every question + scores each answer: Great / Good / Needs Improvement / Missed",
                    elem_id="ta-interview-toggle",
                )
                interview_deep = gr.Checkbox(
                    label="Deep Analysis",
                    value=True,
                    visible=True,
                    info="Adds deflection rate, advancement likelihood, and prep guide",
                    elem_id="ta-interview-deep",
                )

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
                transcript_output_lang = gr.Dropdown(
                    label="Transcript output language (translation)",
                    choices=_PDF_LANGUAGES,
                    value="Same as source",
                    info="Translate the transcript & report to a different language after transcription",
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

            gr.HTML("""
<div style="font-size:0.7em;font-weight:700;text-transform:uppercase;
     letter-spacing:0.1em;color:var(--ta-card-sub);margin:8px 0 4px;">
  Step 3 — Run
</div>""")
            process_btn = gr.Button(
                "▶  Analyze",
                variant="primary", size="sm",
                elem_classes=["ta-analyze-btn"],
            )

            result_state = gr.State(value=None)

            download_accordion = gr.Accordion("Download Outputs", open=False)
            with download_accordion:
                dl_transcript = gr.File(label="Transcript (.txt)")
                dl_speakers   = gr.File(label="Speaker Dialogue (.txt)")
                dl_report     = gr.File(label="Report (.md)")
                dl_pdf        = gr.File(label="Report (.pdf)")
                dl_docx       = gr.File(label="Report (.docx)")
                dl_combined   = gr.File(label="Combined Report (.txt)")
                dl_json       = gr.File(label="Raw Data (.json)")
                dl_srt        = gr.File(label="Subtitles (.srt)")
                dl_vtt        = gr.File(label="Subtitles (.vtt)")
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

            with gr.Row(equal_height=True):
                status_bar = gr.HTML(
                    value=_IDLE_STATUS,
                    elem_classes=["ta-status-bar"],
                    elem_id="ta-status-bar",
                )
                cancel_btn = gr.Button(
                    "■",
                    variant="stop",
                    size="sm",
                    min_width=36,
                    elem_classes=["ta-cancel-btn"],
                    elem_id="ta-cancel-btn",
                )

            eta_panel   = gr.HTML(value="", elem_id="ta-eta-panel")
            log_out     = gr.HTML(
                value='<div id="ta-log-wrap" style="'
                      'background:#0a0f1e;border:1px solid #1e3a5f;border-radius:10px;'
                      'padding:14px 18px;min-height:160px;max-height:320px;'
                      'overflow-y:auto;font-family:\'JetBrains Mono\',\'Courier New\',monospace;'
                      'font-size:0.81em;line-height:1.75;">'
                      '<span style="color:#475569;">Progress and logs appear here…</span>'
                      '</div>',
                elem_id="live-log",
                label="Live Processing Log",
            )
            stats_panel = gr.HTML(value="", elem_id="ta-stats-panel")
            net_monitor = gr.HTML(
                value=(
                    '<style>@keyframes tapulse{0%,100%{opacity:1}50%{opacity:0.35}}</style>'
                    '<div style="background:var(--ta-card-bg,#f8fafc);border:1px solid var(--ta-card-border,#e2e8f0);'
                    'border-radius:10px;padding:10px 14px;margin-top:8px;">'
                    '<div style="font-size:0.72em;font-weight:700;text-transform:uppercase;letter-spacing:0.08em;'
                    'color:var(--ta-card-sub,#64748b);margin-bottom:6px;">🌐 Network — Live</div>'
                    '<div style="display:flex;align-items:center;gap:6px;font-size:0.82em;padding:3px 0;">'
                    '<span>⬇️</span><span style="font-weight:700;color:var(--ta-card-text,#1e293b);min-width:72px;">Download</span>'
                    '<span style="margin-left:auto;display:flex;align-items:center;gap:5px;">'
                    '<span style="width:7px;height:7px;background:#22c55e;border-radius:50%;'
                    'animation:tapulse 2s ease-in-out infinite;"></span>'
                    '<span style="color:#22c55e;font-weight:600;font-size:0.82em;">Connecting…</span>'
                    '</span></div>'
                    '<div style="height:1px;background:var(--ta-card-border,#e2e8f0);margin:4px 0;"></div>'
                    '<div style="display:flex;align-items:center;gap:6px;font-size:0.82em;padding:3px 0;">'
                    '<span>⬆️</span><span style="font-weight:700;color:var(--ta-card-text,#1e293b);min-width:72px;">Upload</span>'
                    '<span style="margin-left:auto;color:var(--ta-card-sub,#64748b);font-size:0.78em;">—</span>'
                    '</div></div>'
                ),
                elem_id="ta-net-monitor",
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

                with gr.TabItem("🎤 Interview Coaching"):
                    interview_out = gr.HTML(
                        value='<p style="color:#94a3b8;padding:12px;">Enable <b>Interview Mode</b> in the sidebar, then analyze a recording to see question-by-question coaching here.</p>'
                    )

                with gr.TabItem("📂 History"):
                    with gr.Row():
                        history_refresh_btn = gr.Button("🔄 Refresh", size="sm", scale=1)
                        gr.Markdown(
                            "_Click any row to reload that session's summary._",
                            elem_classes=["ta-history-hint"],
                        )
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
        queue=False,
    )

    # STT engine → show/hide model/key + save to BrowserState (single handler, no race)
    # STT engine toggle — split into two separate handlers so the UI update
    # (toggle_stt_engine) is instant with no processing indicator, while the
    # BrowserState save fires independently in the background.
    stt_engine_input.change(
        fn=toggle_stt_engine,
        inputs=[stt_engine_input, user_api_key],
        outputs=[stt_key_input, stt_model_input],
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
            return "_No session data found._"
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
        return md

    history_refresh_btn.click(fn=refresh_history, outputs=history_table)
    history_table.select(fn=load_history_row, outputs=history_selected_summary)

    process_event = process_btn.click(
        fn=process_file,
        inputs=[
            file_input, path_input,
            panel_toggle, speakers_input, whisper_input,
            stt_engine_input, stt_key_input, stt_model_input,
            interview_toggle, interview_deep,
            language_input, language_variant,
            transcript_output_lang,
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
            interview_out,
            dl_transcript, dl_speakers, dl_report, dl_combined, dl_json, dl_pdf,
            dl_srt, dl_vtt, dl_docx,
            download_accordion,
            log_out,
            eta_panel,
            net_monitor,
            stats_panel,
            result_state,
        ],
    )
    cancel_btn.click(fn=None, cancels=[process_event])

    pdf_regen_btn.click(
        fn=generate_pdf_in_language,
        inputs=[result_state, pdf_lang_input, user_api_key, provider_dropdown, model_dropdown],
        outputs=[dl_pdf],
    )

    # ── Save settings → bsw_* (WRITE instances, never inputs to demo.load) ──────
    _id = lambda v: v
    whisper_input.change(   fn=_id, inputs=whisper_input,    outputs=bsw_whisper,  queue=False)
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

    # Note: stt_engine_input is intentionally excluded from demo.load() outputs —
    # updating it triggers stt_engine_input.change() → _toggle_and_save_stt, which
    # in turn causes timing conflicts that hide the STT Model dropdown. The STT
    # engine and Whisper model size are restored by the JS watcher instead.
    demo.load(
        fn=_restore_settings,
        inputs=[bsr_whisper, bsr_language, bsr_style, bsr_interview, bsr_deep,
                bsr_inc_sum, bsr_inc_kp, bsr_inc_ac, bsr_inc_tr, bsr_inc_pr, bsr_inc_an, bsr_speakers],
        outputs=[whisper_input, language_input, report_style, interview_toggle, interview_deep,
                 inc_summary, inc_key_points, inc_action, inc_transcript, inc_profiles, inc_analytics,
                 speakers_input],
        queue=False,
    )

    # Check for updates on page load (non-blocking, skipped on HF Spaces)
    if not bool(os.environ.get("SPACE_ID")):
        demo.load(fn=_check_github_update, outputs=[update_banner], queue=False)

    # Inject theme toggle + floating button via demo.load js= (guaranteed execution in Gradio 6.x)
    demo.load(fn=None, js=f"() => {{ {_THEME_JS} }}")



if __name__ == "__main__":
    _host   = os.environ.get("GRADIO_SERVER_NAME", "0.0.0.0")
    _port   = int(os.environ.get("GRADIO_SERVER_PORT", 7860))
    _docker = _host == "0.0.0.0"
    demo.queue(max_size=5, default_concurrency_limit=1)
    import inspect as _inspect
    _launch_kw = dict(
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
    # show_api was added in Gradio 4.15 — skip on older builds
    if "show_api" in _inspect.signature(demo.launch).parameters:
        _launch_kw["show_api"] = False
    demo.launch(**_launch_kw)
