#!/usr/bin/env python3
"""Transcript Agent — Gradio UI with drag-and-drop"""

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


def _download_url(url: str, dest_dir: Path) -> Path:
    """Download a URL to dest_dir; uses Content-Disposition to pick filename.

    Handles S3 application-level redirects (PermanentRedirect XML response with
    HTTP 200/301) by retrying against the correct endpoint.
    """
    import requests

    def _do_get(u: str):
        return requests.get(
            u,
            stream=True,
            timeout=300,
            headers={"User-Agent": "TranscriptAgent/1.0"},
            allow_redirects=True,
        )

    resp = _do_get(url)

    if resp.status_code == 401:
        raise ValueError(
            "The URL requires a login (401 Unauthorized). "
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
            parsed = urllib.parse.urlparse(url)
            new_host = m_ep.group(1).strip()
            # If the suggested endpoint is the bare regional/global host (e.g.
            # "s3.amazonaws.com"), keep the bucket on the path; otherwise use
            # the endpoint as-is (it already includes the bucket).
            bucket = m_bucket.group(1).strip() if m_bucket else ""
            if bucket and not new_host.startswith(bucket + "."):
                new_path = "/" + bucket.strip("/") + "/" + parsed.path.lstrip("/")
            else:
                new_path = parsed.path
            new_url = urllib.parse.urlunparse(
                (parsed.scheme or "https", new_host, new_path,
                 parsed.params, parsed.query, parsed.fragment)
            )
            resp.close()
            resp = _do_get(new_url)
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
    with open(dest, "wb") as f:
        if first:
            f.write(first)
            total += len(first)
        for chunk in chunks:
            f.write(chunk)
            total += len(chunk)

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
_ES_SYSTEM_REQUIRED = 0x00000001

def _prevent_sleep():
    if sys.platform == "win32":
        import ctypes
        ctypes.windll.kernel32.SetThreadExecutionState(
            _ES_CONTINUOUS | _ES_SYSTEM_REQUIRED
        )

def _allow_sleep():
    if sys.platform == "win32":
        import ctypes
        ctypes.windll.kernel32.SetThreadExecutionState(_ES_CONTINUOUS)

from transcript_agent import (
    run, ReportConfig, build_combined_report,
    AUDIO_EXTS, VIDEO_EXTS,
)

OUT_DIR = Path(__file__).parent / "outputs"
OUT_DIR.mkdir(exist_ok=True)



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
html.dark body { background: #0f172a !important; }

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

/* scrollable dropdowns (language list etc.) */
[role="listbox"] {
    max-height: 220px !important;
    overflow-y: auto !important;
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

SPANISH_VARIANTS = [
    ("🌎 Auto (General Spanish)",              "General Spanish"),
    ("🇨🇴 Colombian Spanish",                  "Colombian Spanish (es-CO)"),
    ("🇲🇽 Mexican Spanish",                    "Mexican Spanish (es-MX)"),
    ("🇪🇸 Spain Spanish (Castilian)",          "Castilian Spanish (es-ES)"),
    ("🇦🇷 Argentinian Spanish",                "Argentinian Spanish (es-AR)"),
    ("🇨🇱 Chilean Spanish",                    "Chilean Spanish (es-CL)"),
    ("🇻🇪 Venezuelan Spanish",                 "Venezuelan Spanish (es-VE)"),
    ("🇵🇪 Peruvian Spanish",                   "Peruvian Spanish (es-PE)"),
    ("🇨🇺 Cuban Spanish",                      "Cuban Spanish (es-CU)"),
    ("🇵🇷 Puerto Rican Spanish",               "Puerto Rican Spanish (es-PR)"),
    ("🇩🇴 Dominican Spanish",                  "Dominican Spanish (es-DO)"),
    ("🇬🇹 Guatemalan Spanish",                 "Guatemalan Spanish (es-GT)"),
    ("🇪🇨 Ecuadorian Spanish",                 "Ecuadorian Spanish (es-EC)"),
    ("🇧🇴 Bolivian Spanish",                   "Bolivian Spanish (es-BO)"),
    ("🇺🇾 Uruguayan Spanish",                  "Uruguayan Spanish (es-UY)"),
    ("🇵🇾 Paraguayan Spanish",                 "Paraguayan Spanish (es-PY)"),
    ("🇭🇳 Honduran Spanish",                   "Honduran Spanish (es-HN)"),
    ("🇸🇻 Salvadoran Spanish",                 "Salvadoran Spanish (es-SV)"),
    ("🇳🇮 Nicaraguan Spanish",                 "Nicaraguan Spanish (es-NI)"),
    ("🇨🇷 Costa Rican Spanish",                "Costa Rican Spanish (es-CR)"),
    ("🇵🇦 Panamanian Spanish",                 "Panamanian Spanish (es-PA)"),
    ("🇺🇸 US Latino Spanish",                  "US Latino Spanish (es-US)"),
]

FRENCH_VARIANTS = [
    ("🌍 Auto (General French)",               "General French"),
    ("🇫🇷 France French",                      "France French (fr-FR)"),
    ("🇨🇦 Canadian French (Québécois)",        "Canadian French (fr-CA)"),
    ("🇧🇪 Belgian French",                     "Belgian French (fr-BE)"),
    ("🇨🇭 Swiss French",                       "Swiss French (fr-CH)"),
    ("🌍 West African French",                 "West African French"),
    ("🌍 North African French",                "North African French"),
]

INDIAN_VARIANTS = [
    ("🔍 Auto (detect dialect)",               "Auto Indian"),
    ("🇮🇳 Standard Hindi (Delhi)",             "Standard Hindi"),
    ("🇮🇳 Mumbai Hindi",                       "Mumbai Hindi"),
    ("🇮🇳 Standard Bengali (Kolkata)",         "Standard Bengali"),
    ("🇮🇳 Standard Tamil (Chennai)",           "Standard Tamil"),
    ("🇮🇳 Standard Telugu (Hyderabad)",        "Standard Telugu"),
    ("🇮🇳 Indian English",                     "Indian English"),
]


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
#   0  status_bar       1  summary_out      2  transcript_out   3  dialogue_out
#   4  profiles_out     5  analytics_out    6  combined_out
#   7  dl_transcript    8  dl_speakers      9  dl_report
#   10 dl_combined      11 dl_json          12 download_accordion
#   13 log_out          14 eta_panel
# ---------------------------------------------------------------------------

_NOCHANGE = (gr.update(),) * 15   # yield this to keep connection alive without changes

def _out(status=gr.update(), summary=gr.update(), transcript=gr.update(),
         dialogue=gr.update(), profiles=gr.update(), analytics=gr.update(),
         combined=gr.update(), dl_t=gr.update(), dl_s=gr.update(),
         dl_r=gr.update(), dl_c=gr.update(), dl_j=gr.update(), dl_acc=gr.update(),
         log=gr.update(), eta=gr.update()):
    return (status, summary, transcript, dialogue, profiles, analytics,
            combined, dl_t, dl_s, dl_r, dl_c, dl_j, dl_acc, log, eta)


def _eta_panel_html(stage: str, pct: float = None, eta_secs: int = None,
                    elapsed: str = "", done: bool = False) -> str:
    import datetime as _dt

    _slide_css = (
        "<style>@keyframes pgslide{0%{left:-45%}100%{left:110%}}</style>"
    )

    # ── Done ──────────────────────────────────────────────────────────────────
    if done:
        return (
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

        return (
            '<div style="background:linear-gradient(135deg,#eff6ff,#dbeafe);'
            'border:2px solid #2563eb;border-radius:16px;padding:24px 28px;'
            'font-family:sans-serif;">'

            # Step label
            '<div style="font-size:0.72em;font-weight:700;text-transform:uppercase;'
            'letter-spacing:0.1em;color:#1e40af;margin-bottom:12px;">'
            'Step 1 of 2 &nbsp;&mdash;&nbsp; Transcribing Audio</div>'

            # Big percentage
            '<div style="display:flex;align-items:flex-end;gap:4px;margin-bottom:14px;">'
            f'<div style="font-size:4.5em;font-weight:900;color:#1d4ed8;'
            f'font-family:monospace;line-height:1;letter-spacing:-0.04em;">{pct_int}</div>'
            '<div style="font-size:2em;font-weight:700;color:#3b82f6;'
            'margin-bottom:6px;">%</div>'
            '</div>'

            # Progress bar
            '<div style="background:#bfdbfe;border-radius:8px;height:14px;'
            'overflow:hidden;margin-bottom:10px;">'
            f'<div style="width:{bar_fill};height:100%;'
            'background:linear-gradient(90deg,#1d4ed8,#3b82f6);'
            'border-radius:8px;transition:width 0.5s ease;"></div>'
            '</div>'

            # Stats row
            '<div style="display:flex;gap:16px;flex-wrap:wrap;">'
            '<div style="background:rgba(255,255,255,0.7);border-radius:8px;'
            'padding:8px 14px;flex:1;min-width:90px;text-align:center;">'
            '<div style="font-size:0.68em;font-weight:700;text-transform:uppercase;'
            'letter-spacing:0.08em;color:#1e40af;">Time Left</div>'
            f'<div style="font-size:1.3em;font-weight:800;color:#1d4ed8;'
            f'font-family:monospace;">{eta_str}</div>'
            '</div>'
            + (
                '<div style="background:rgba(255,255,255,0.7);border-radius:8px;'
                'padding:8px 14px;flex:1;min-width:90px;text-align:center;">'
                '<div style="font-size:0.68em;font-weight:700;text-transform:uppercase;'
                'letter-spacing:0.08em;color:#166534;">Done By</div>'
                f'<div style="font-size:1.3em;font-weight:800;color:#15803d;">{finish_str}</div>'
                '</div>' if finish_str else ""
            ) +
            '<div style="background:rgba(255,255,255,0.7);border-radius:8px;'
            'padding:8px 14px;flex:1;min-width:90px;text-align:center;">'
            '<div style="font-size:0.68em;font-weight:700;text-transform:uppercase;'
            'letter-spacing:0.08em;color:#6b7280;">Elapsed</div>'
            f'<div style="font-size:1.3em;font-weight:800;color:#374151;'
            f'font-family:monospace;">{elapsed}</div>'
            '</div>'
            '</div>'
            '</div>'
        )

    # ── Other stages (loading / extracting / claude) ──────────────────────────
    stage_cfg = {
        "loading":    ("#f59e0b", "#fef3c7", "#92400e", "Starting up…",          "Step 0 of 2"),
        "extracting": ("#8b5cf6", "#ede9fe", "#4c1d95", "Extracting audio…",     "Step 1 of 2"),
        "whisper":    ("#2563eb", "#dbeafe", "#1e40af", "Transcribing audio…",   "Step 1 of 2"),
        "claude":     ("#7c3aed", "#ede9fe", "#3b0764", "Analyzing with Claude…","Step 2 of 2"),
    }
    color, bg, text_dark, label, step = stage_cfg.get(
        stage, ("#6b7280", "#f1f5f9", "#1f2937", "Processing…", "")
    )

    # For claude, show an estimated overall % (50–99 range, indeterminate)
    overlay_pct = ""
    if stage == "claude":
        overlay_pct = (
            '<div style="display:flex;align-items:flex-end;gap:4px;margin-bottom:14px;">'
            f'<div style="font-size:4.5em;font-weight:900;color:{color};'
            f'font-family:monospace;line-height:1;letter-spacing:-0.04em;">50</div>'
            f'<div style="font-size:2em;font-weight:700;color:{color};margin-bottom:6px;">%+</div>'
            '</div>'
            f'<div style="font-size:0.82em;color:{text_dark};margin-bottom:12px;font-weight:500;">'
            'Claude is reading the transcript and writing your report…</div>'
        )
    elif stage in ("loading", "extracting"):
        overlay_pct = (
            '<div style="display:flex;align-items:flex-end;gap:4px;margin-bottom:14px;">'
            f'<div style="font-size:4.5em;font-weight:900;color:{color};'
            f'font-family:monospace;line-height:1;letter-spacing:-0.04em;">—</div>'
            '</div>'
        )

    return (
        f'<div style="background:linear-gradient(135deg,#f8fafc,{bg});'
        f'border:2px solid {color};border-radius:16px;padding:24px 28px;'
        f'font-family:sans-serif;">'
        f'<div style="font-size:0.72em;font-weight:700;text-transform:uppercase;'
        f'letter-spacing:0.1em;color:{color};margin-bottom:12px;">'
        f'{step} &nbsp;&mdash;&nbsp; {label}</div>'
        f'{overlay_pct}'
        f'{_slide_css}'
        f'<div style="background:#e2e8f0;border-radius:8px;height:14px;'
        f'overflow:hidden;position:relative;margin-bottom:10px;">'
        f'<div style="position:absolute;width:40%;height:100%;background:{color};'
        f'border-radius:8px;opacity:0.75;animation:pgslide 1.6s ease-in-out infinite;"></div>'
        f'</div>'
        f'<div style="display:flex;gap:8px;">'
        f'<div style="background:rgba(255,255,255,0.7);border-radius:8px;'
        f'padding:8px 14px;text-align:center;">'
        f'<div style="font-size:0.68em;font-weight:700;text-transform:uppercase;'
        f'letter-spacing:0.08em;color:#6b7280;">Elapsed</div>'
        f'<div style="font-size:1.3em;font-weight:800;color:#374151;'
        f'font-family:monospace;">{elapsed}</div>'
        f'</div>'
        f'</div>'
        f'</div>'
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
    language_input,
    spanish_variant,
    french_variant,
    indian_variant,
    report_style,
    inc_summary,
    inc_key_points,
    inc_action_items,
    inc_transcript,
    inc_profiles,
    inc_analytics,
    user_api_key,
):
    # ── validation (all errors shown inline, no popup) ────────────────────────
    api_key = (user_api_key or "").strip()
    if not api_key:
        yield _err("Please enter your Anthropic API key at the top of the page.")
        return
    _prevent_sleep()

    # prefer pasted path/URL (no upload wait) over drag-and-drop
    pasted = (path_input or "").strip().strip('"').strip("'")
    if pasted:
        uploaded_file = pasted

    if not uploaded_file:
        yield _err("Please drag a file, paste a file path, or paste a URL above.")
        return

    # Download remote file before anything else
    if isinstance(uploaded_file, str) and (
        uploaded_file.startswith("http://") or uploaded_file.startswith("https://")
    ):
        yield _out(
            status=_status_html(
                "⬇️", "Downloading file from URL…",
                subtitle="Fetching remote file — this may take a moment for large recordings…",
            ),
            eta=_eta_panel_html("loading", elapsed="0s"),
        )
        _dl_dir = Path(tempfile.mkdtemp(prefix="ta_dl_"))
        try:
            uploaded_file = str(_download_url(uploaded_file, _dl_dir))
        except Exception as _e:
            yield _err(f"Download failed: {_e}")
            return

    from pathlib import Path as _P
    if not _P(uploaded_file).exists():
        yield _err(f"File not found: {uploaded_file}")
        return

    # Initial yield so Gradio 6 always has a value before processing begins
    yield _out(
        status=_status_html("⏳", "Starting…", subtitle="Preparing your file for processing…"),
        eta=_eta_panel_html("loading", elapsed="0s"),
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
    speakers = int(num_speakers) if (num_speakers and num_speakers >= 2) else None
    lang_code = language_input if language_input and language_input != "auto" else None
    if language_input == "es" and spanish_variant and spanish_variant != "General Spanish":
        lang_variant = spanish_variant
    elif language_input == "fr" and french_variant and french_variant != "General French":
        lang_variant = french_variant
    elif language_input in _INDIAN_LANG_CODES and indian_variant and indian_variant != "Auto Indian":
        lang_variant = indian_variant
    else:
        lang_variant = None
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
                panel_mode=panel_mode,
                num_speakers=speakers,
                config=config,
                api_key=api_key,
                language=lang_code,
                language_variant=lang_variant,
                on_whisper_progress=on_whisper_progress if is_av else None,
                on_raw_transcript=on_raw_transcript if is_av else None,
                on_stage_change=on_stage_change if is_av else None,
                on_log=on_log,
            )
            q.put(("done", result))
        except Exception as e:
            q.put(("error", str(e)))

    t = threading.Thread(target=background, daemon=True)
    t.start()

    # ── live update loop ──────────────────────────────────────────────────────
    whisper_pct    = 0.0
    raw_shown      = False
    claude_started = False
    stage          = "loading"   # loading | extracting | whisper | claude
    start_time     = time.time()
    log_lines      = []          # accumulates all log messages

    def _elapsed():
        secs = int(time.time() - start_time)
        m, s = divmod(secs, 60)
        return f"{m}m {s:02d}s" if m else f"{s}s"

    def _ts():
        secs = int(time.time() - start_time)
        m, s = divmod(secs, 60)
        return f"[{m:02d}:{s:02d}]"

    def _eta_secs(pct):
        """Return remaining seconds as int, or None if not calculable."""
        if pct <= 0.01:
            return None
        return max(0, int((time.time() - start_time) * (1.0 - pct) / pct))

    def _eta_str(pct):
        s = _eta_secs(pct)
        if s is None:
            return ""
        em, es = divmod(s, 60)
        return f"~{em}m {es:02d}s" if em else f"~{es}s"

    def _add_log(msg):
        log_lines.append(f"{_ts()} {msg}")
        return "\n".join(log_lines)

    try:
     while True:
        try:
            msg = q.get(timeout=1.0)
        except Q.Empty:
            elapsed = _elapsed()
            if stage == "extracting":
                status  = _status_html("🎬", "Extracting audio from video…", elapsed=elapsed)
                eta_upd = _eta_panel_html("extracting", elapsed=elapsed)
            elif stage == "whisper" or (not raw_shown and is_av):
                eta_s   = _eta_secs(whisper_pct) if whisper_pct > 0 else None
                status  = _status_html("🎤", "Step 1/2 — Transcribing audio",
                                       subtitle="Whisper is processing your file…",
                                       elapsed=elapsed,
                                       pct=whisper_pct if whisper_pct > 0 else None,
                                       eta_secs=eta_s)
                eta_upd = _eta_panel_html("whisper", pct=whisper_pct if whisper_pct > 0 else None,
                                          eta_secs=eta_s, elapsed=elapsed)
            elif stage == "claude" or claude_started:
                status  = _status_html("🤖", "Step 2/2 — Analysing with Claude",
                                       subtitle="Summary, key points and speaker analysis coming up…",
                                       elapsed=elapsed)
                eta_upd = _eta_panel_html("claude", elapsed=elapsed)
            else:
                status  = _status_html("⏳", "Loading…", elapsed=elapsed)
                eta_upd = _eta_panel_html("loading", elapsed=elapsed)
            yield _out(status=status, eta=eta_upd)
            continue

        kind = msg[0]

        if kind == "log":
            log_text = _add_log(msg[1])
            yield _out(log=log_text)

        elif kind == "stage":
            stage = msg[1]
            elapsed = _elapsed()
            if stage == "extracting":
                yield _out(status=_status_html("🎬", "Extracting audio from video…", elapsed=elapsed),
                           eta=_eta_panel_html("extracting", elapsed=elapsed))
            elif stage == "whisper":
                yield _out(status=_status_html("🎤", "Step 1/2 — Starting Whisper transcription…", elapsed=elapsed),
                           eta=_eta_panel_html("whisper", elapsed=elapsed))
            elif stage == "claude":
                yield _out(status=_status_html("🤖", "Step 2/2 — Sending to Claude…", elapsed=elapsed),
                           eta=_eta_panel_html("claude", elapsed=elapsed))

        elif kind == "pct":
            whisper_pct = msg[1]
            elapsed     = _elapsed()
            eta_s       = _eta_secs(whisper_pct)
            eta         = _eta_str(whisper_pct)
            log_text    = _add_log(f"Whisper: {whisper_pct*100:.0f}%{('  ETA ' + eta) if eta else ''}")
            yield _out(
                status=_status_html("🎤", "Step 1/2 — Transcribing audio",
                                    subtitle="Whisper is processing your file…",
                                    elapsed=elapsed, pct=whisper_pct, eta_secs=eta_s),
                eta=_eta_panel_html("whisper", pct=whisper_pct, eta_secs=eta_s, elapsed=elapsed),
                log=log_text,
            )

        elif kind == "transcript":
            raw_shown = True
            elapsed   = _elapsed()
            log_text  = _add_log("Transcription complete! Sending to Claude for analysis…")
            yield _out(
                status=_status_html("🤖", "Step 2/2 — Whisper done! Analysing with Claude…",
                                    subtitle="Summary and speaker analysis coming shortly…",
                                    elapsed=elapsed),
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

            total_elapsed = _elapsed()
            log_text = _add_log(f"All done! Total time: {total_elapsed}")
            yield _out(
                status=_status_html("✅", "Done! All tabs are ready.", subtitle="Downloads opened below.", pct=1.0),
                eta=_eta_panel_html("done", elapsed=total_elapsed, done=True),
                summary=summary_md,
                transcript=result.clean_transcript,
                dialogue=result.speaker_dialogue,
                profiles=profiles_md,
                analytics=analytics_md,
                combined=combined_text,
                dl_t=f_t, dl_s=f_s, dl_r=f_r, dl_c=f_c, dl_j=f_j,
                dl_acc=gr.update(open=True),
                log=log_text,
            )
            break

        elif kind == "error":
            yield _err(f"Processing failed: {msg[1]}")
            break
    finally:
        _allow_sleep()


def toggle_speakers(is_panel):
    return gr.update(visible=is_panel)


_INDIAN_LANG_CODES = {"hi", "bn", "ta", "te", "gu", "kn", "ml", "mr", "pa", "ur"}

def toggle_language_variants(lang):
    return (
        gr.update(visible=(lang == "es")),
        gr.update(visible=(lang == "fr")),
        gr.update(visible=(lang in _INDIAN_LANG_CODES)),
    )


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
     border-radius:12px;padding:14px 20px;display:flex;align-items:center;gap:14px;transition:background 0.3s,border-color 0.3s;">
  <div style="font-size:1.6em;">🔑</div>
  <div>
    <div id="api-banner-title" style="font-weight:700;color:#92400e;font-size:0.9em;">API Key Required</div>
    <div id="api-banner-sub" style="color:#a16207;font-size:0.8em;margin-top:2px;">
      Enter your <strong>Anthropic API key</strong> below. Usage is billed directly to your account — nothing is stored here.
    </div>
  </div>
</div>
"""

_THEME_TOGGLE = """
<!-- pill switch — all inline styles so Gradio can't strip them -->
<div id="ta-widget" title="Toggle light / dark mode"
  style="position:fixed;top:14px;right:18px;z-index:9999;display:flex;align-items:center;
         gap:8px;background:rgba(255,255,255,0.93);backdrop-filter:blur(8px);
         border:1px solid #e2e8f0;border-radius:28px;padding:6px 14px;
         box-shadow:0 2px 10px rgba(0,0,0,0.13);cursor:pointer;transition:background 0.3s,border-color 0.3s;">
  <span style="font-size:0.95em;line-height:1;">☀️</span>
  <div id="ta-track"
    style="width:42px;height:24px;background:#cbd5e1;border-radius:24px;
           position:relative;transition:background 0.3s;flex-shrink:0;">
    <div id="ta-knob"
      style="position:absolute;width:18px;height:18px;background:#fff;border-radius:50%;
             top:3px;left:3px;box-shadow:0 1px 4px rgba(0,0,0,0.28);
             transition:transform 0.28s cubic-bezier(.4,0,.2,1);"></div>
  </div>
  <span style="font-size:0.95em;line-height:1;">🌙</span>
</div>

<script>
(function(){
  var _dark = false;

  /* inject override style tag into <head> */
  var st = document.createElement('style');
  st.id  = 'ta-override';
  document.head.appendChild(st);

  function applyTheme(dark){
    _dark = dark;

    /* toggle dark class on html + body */
    [document.documentElement, document.body].forEach(function(el){
      dark ? el.classList.add('dark') : el.classList.remove('dark');
    });

    /* sync Gradio localStorage keys */
    localStorage.setItem('ta-dark',      dark ? 'true'  : 'false');
    localStorage.setItem('theme',        dark ? 'dark'  : 'light');
    localStorage.setItem('gradio-theme', dark ? 'dark'  : 'light');

    /* force-override Gradio containers via injected style */
    st.textContent = dark
      ? 'body,html{background:#0f172a !important;}'
        + '.gradio-container{background:#0f172a !important;}'
      : 'body,html{background:#f1f5f9 !important;}'
        + '.gradio-container{background:#f1f5f9 !important;}';

    /* update switch visuals via inline style (100% reliable) */
    var widget = document.getElementById('ta-widget');
    var track  = document.getElementById('ta-track');
    var knob   = document.getElementById('ta-knob');
    if(widget){
      widget.style.background   = dark ? 'rgba(15,23,42,0.93)' : 'rgba(255,255,255,0.93)';
      widget.style.borderColor  = dark ? '#334155' : '#e2e8f0';
    }
    if(track) track.style.background  = dark ? '#3b82f6' : '#cbd5e1';
    if(knob)  knob.style.transform    = dark ? 'translateX(21px)' : 'translateX(0)';

    /* update amber API banner */
    var banner = document.getElementById('api-banner');
    var title  = document.getElementById('api-banner-title');
    var sub    = document.getElementById('api-banner-sub');
    if(banner){
      banner.style.background  = dark
        ? 'linear-gradient(135deg,#292107,#3b2d00)'
        : 'linear-gradient(135deg,#fffbeb,#fef3c7)';
      banner.style.borderColor = dark ? '#d97706' : '#f59e0b';
    }
    if(title) title.style.color = dark ? '#fbbf24' : '#92400e';
    if(sub)   sub.style.color   = dark ? '#fcd34d' : '#a16207';
  }

  /* wire the widget click */
  function init(){
    var widget = document.getElementById('ta-widget');
    if(!widget){ setTimeout(init, 250); return; }
    var saved = localStorage.getItem('ta-dark') === 'true';
    applyTheme(saved);
    widget.addEventListener('click', function(){ applyTheme(!_dark); });
  }
  setTimeout(init, 200);

  /* 👁 show/hide eye on password inputs */
  function addEyes(){
    document.querySelectorAll('input[type="password"]').forEach(function(inp){
      if(inp.dataset.eye) return;
      inp.dataset.eye = '1';
      var eye = document.createElement('button');
      eye.type = 'button';
      eye.textContent = '👁';
      eye.style.cssText = 'position:absolute;right:10px;top:50%;transform:translateY(-50%);'
        + 'background:none;border:none;cursor:pointer;font-size:1em;opacity:0.5;z-index:20;padding:2px;';
      eye.addEventListener('click', function(){
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
  setTimeout(addEyes, 1600);
  setTimeout(addEyes, 3200);
})();
</script>
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
<div style="background:#f8fafc;border:1px solid #e2e8f0;border-radius:10px;padding:12px 16px;
     font-size:0.78em;color:#64748b;line-height:1.8;">
  <strong style="color:#334155;">Audio:</strong> mp3 &nbsp;wav &nbsp;m4a &nbsp;flac &nbsp;ogg &nbsp;aac &nbsp;&nbsp;
  <strong style="color:#334155;">Video:</strong> mp4 &nbsp;mov &nbsp;avi &nbsp;mkv &nbsp;webm<br>
  <strong style="color:#334155;">Docs:</strong> pdf &nbsp;docx &nbsp;txt &nbsp;md &nbsp;srt &nbsp;vtt
</div>
"""

_SECTION = lambda label: f"""
<div style="font-size:0.7em;font-weight:700;text-transform:uppercase;letter-spacing:0.1em;
     color:#94a3b8;margin:4px 0 2px;">{label}</div>
"""

# ── UI ──────────────────────────────────────────────────────────────────────────

with gr.Blocks(title="Transcript Agent") as demo:

    gr.HTML(_THEME_TOGGLE)
    gr.HTML(_HERO)
    gr.HTML(_API_BANNER)

    user_api_key = gr.Textbox(
        label="Anthropic API Key",
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
                panel_toggle = gr.Checkbox(
                    label="Panel Mode (multiple speakers)",
                    value=False,
                    info="Enables speaker diarization. Requires HF_TOKEN env var.",
                )
                speakers_input = gr.Number(
                    label="Number of speakers (optional, 2–20)",
                    minimum=2, maximum=20, step=1,
                    value=2, visible=False,
                )
                whisper_input = gr.Dropdown(
                    label="Whisper model",
                    choices=["tiny", "base", "small", "medium", "large"],
                    value="base",
                    info="tiny = fastest   |   large = most accurate",
                )

            with gr.Accordion("Language", open=False):
                language_input = gr.Dropdown(
                    label="Transcript language",
                    choices=LANGUAGES,
                    value="auto",
                )
                spanish_variant = gr.Dropdown(
                    label="Spanish regional variant",
                    choices=SPANISH_VARIANTS,
                    value="General Spanish",
                    visible=False,
                )
                french_variant = gr.Dropdown(
                    label="French regional variant",
                    choices=FRENCH_VARIANTS,
                    value="General French",
                    visible=False,
                )
                indian_variant = gr.Dropdown(
                    label="Indian dialect / accent hint",
                    choices=INDIAN_VARIANTS,
                    value="Auto Indian",
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
                gr.HTML(_SECTION("Include in report"))
                with gr.Row():
                    with gr.Column(min_width=120):
                        inc_summary    = gr.Checkbox(label="Summary",          value=True)
                        inc_key_points = gr.Checkbox(label="Key points",       value=True)
                        inc_action     = gr.Checkbox(label="Action items",     value=True)
                    with gr.Column(min_width=120):
                        inc_transcript = gr.Checkbox(label="Full transcript",  value=True)
                        inc_profiles   = gr.Checkbox(label="Speaker profiles", value=True)
                        inc_analytics  = gr.Checkbox(label="Speech analytics", value=True)

            gr.HTML(_SECTION("Step 3 — Run"))
            process_btn = gr.Button(
                "Analyze File",
                variant="primary", size="lg",
                elem_classes=["big-btn"],
            )

            download_accordion = gr.Accordion("Download Outputs", open=False)
            with download_accordion:
                dl_transcript = gr.File(label="Transcript (.txt)")
                dl_speakers   = gr.File(label="Speaker Dialogue (.txt)")
                dl_report     = gr.File(label="Report (.md)")
                dl_combined   = gr.File(label="Combined Report (.txt)")
                dl_json       = gr.File(label="Raw Data (.json)")

        # ── results panel ─────────────────────────────────────────────────────
        with gr.Column(scale=2):

            status_bar = gr.HTML(value=_IDLE_STATUS)
            eta_panel  = gr.HTML(value="")
            log_out    = gr.Textbox(
                label="Live Processing Log",
                lines=7, max_lines=7,
                placeholder="Progress appears here once you click Analyze File…",
                interactive=False,
                elem_id="live-log",
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
    panel_toggle.change(fn=toggle_speakers, inputs=panel_toggle, outputs=speakers_input)
    language_input.change(
        fn=toggle_language_variants,
        inputs=language_input,
        outputs=[spanish_variant, french_variant, indian_variant],
    )

    process_btn.click(
        fn=process_file,
        inputs=[
            file_input, path_input,
            panel_toggle, speakers_input, whisper_input,
            language_input, spanish_variant, french_variant, indian_variant,
            report_style,
            inc_summary, inc_key_points, inc_action,
            inc_transcript, inc_profiles, inc_analytics,
            user_api_key,
        ],
        outputs=[
            status_bar,
            summary_out, transcript_out, dialogue_out,
            profiles_out, analytics_out, combined_out,
            dl_transcript, dl_speakers, dl_report, dl_combined, dl_json,
            download_accordion,
            log_out,
            eta_panel,
        ],
    )


if __name__ == "__main__":
    _host   = os.environ.get("GRADIO_SERVER_NAME", "0.0.0.0")
    _port   = int(os.environ.get("GRADIO_SERVER_PORT", 7860))
    _docker = _host == "0.0.0.0"
    demo.queue(max_size=5, default_concurrency_limit=1)
    demo.launch(
        server_name=_host,
        server_port=_port,
        theme=_THEME,
        css=CSS,
        allowed_paths=[str(OUT_DIR), tempfile.gettempdir()],
        max_file_size="4gb",
        inbrowser=not _docker,
        show_error=True,
        share=not _docker,
        strict_cors=not _docker,
    )
