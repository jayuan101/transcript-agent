#!/usr/bin/env python3
"""
Transcript Agent
Formats:  .mp3 .wav .m4a .ogg .aac (audio)
          .mp4 .mov .avi .mkv .webm (video)
          .srt .vtt (subtitles)
          .pdf .docx .txt .md (documents)
Outputs:  clean transcript, speaker-labelled dialogue, summary, key points,
          speech analytics (WPM + accent detection)
"""

import os
import sys

# ── Force UTF-8 everywhere so no Unicode character can crash transcription ──────
# Set env var BEFORE any library imports so subprocesses inherit it too
os.environ.setdefault("PYTHONIOENCODING", "utf-8:replace")
os.environ.setdefault("PYTHONUTF8", "1")

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

def _safe_print(*args, **kwargs):
    """print() that never raises on encoding errors."""
    try:
        print(*args, **kwargs)
    except (UnicodeEncodeError, UnicodeDecodeError):
        safe = " ".join(str(a).encode("ascii", errors="replace").decode("ascii") for a in args)
        try:
            print(safe, **{k: v for k, v in kwargs.items() if k != "file"})
        except Exception:
            pass
import json
import re
import tempfile
from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional
# ── LLM client abstraction ───────────────────────────────────────────────────

class LLMClient:
    """Thin wrapper normalising Anthropic and OpenAI-compatible provider SDKs."""

    def __init__(self, provider: str, api_key: str, model: str, base_url: str = None):
        self.provider = provider  # "anthropic" | "openai" | "openai_compat"
        self.model = model
        if provider == "anthropic":
            import anthropic as _ant
            self._client = _ant.Anthropic(api_key=api_key) if api_key else _ant.Anthropic()
        else:
            import openai as _oai
            # Always pass api_key — OpenAI SDK v2.x requires it even with a
            # custom base_url. Use "none" for local models (Ollama) that ignore it.
            kw = {"api_key": api_key if api_key else "none"}
            if base_url:
                kw["base_url"] = base_url
            self._client = _oai.OpenAI(**kw)

    def chat(self, system: str, user: str, max_tokens: int, thinking: bool = False) -> str:
        if self.provider == "anthropic":
            kw = {}
            if thinking:
                kw["thinking"] = {"type": "adaptive"}
            resp = self._client.messages.create(
                model=self.model,
                max_tokens=max_tokens,
                system=system,
                messages=[{"role": "user", "content": user}],
                **kw,
            )
            return next((b.text for b in resp.content if b.type == "text"), "")
        else:
            try:
                resp = self._client.chat.completions.create(
                    model=self.model,
                    max_tokens=max_tokens,
                    messages=[
                        {"role": "system", "content": system},
                        {"role": "user", "content": user},
                    ],
                )
                return resp.choices[0].message.content or ""
            except Exception as e:
                err = str(e)
                if "authentication" in err.lower() or "api_key" in err.lower() or "credentials" in err.lower() or "401" in err:
                    raise ValueError(
                        f"API key rejected by {self.provider} ({self.model}). "
                        "Check that you pasted the correct key for the selected provider. "
                        f"Original error: {err}"
                    )
                raise


# ── resolve bundled ffmpeg (works on Windows without a system ffmpeg install) ──
import subprocess as _sp
import numpy as _np
import threading as _threading

try:
    import imageio_ffmpeg as _iff
    FFMPEG_EXE = _iff.get_ffmpeg_exe()
except ImportError:
    FFMPEG_EXE = "ffmpeg"

_progress_lock = _threading.Lock()
_progress_cb   = None

# Fast availability check — no actual import, just inspects installed packages
import importlib.util as _importlib_util
WHISPER_AVAILABLE = _importlib_util.find_spec("whisper") is not None

# ── tqdm tracking class (needs tqdm only, not torch/whisper) ─────────────────
try:
    import tqdm as _tqdm_mod

    class _TrackingTqdm(_tqdm_mod.tqdm):
        def update(self, n=1):
            # Do NOT call super().update() — it renders Unicode progress chars
            # (e.g. ▼ U+25BC) to stderr which crashes ASCII-encoded terminals.
            if self.n is None:
                self.n = 0
            self.n += n
            with _progress_lock:
                cb = _progress_cb
            if cb and self.total and self.total > 0:
                cb(min(self.n / self.total, 1.0))
except Exception:
    _TrackingTqdm = None

# ── lazy Whisper loader — defers the heavy torch import until first use ───────
_whisper_init_lock = _threading.Lock()
_whisper_init_done = False
openai_whisper     = None  # assigned inside _ensure_whisper_loaded()


def _ensure_whisper_loaded():
    """Import whisper + torch and apply patches. Safe to call multiple times."""
    global _whisper_init_done, openai_whisper
    if _whisper_init_done:
        return
    with _whisper_init_lock:
        if _whisper_init_done:
            return
        import whisper as _wmod
        openai_whisper = _wmod

        # Patch audio loader to use the resolved ffmpeg binary
        try:
            import whisper.audio as _wa
            def _patched_load_audio(file: str, sr: int = 16000):
                cmd = [FFMPEG_EXE, "-nostdin", "-threads", "0", "-i", file,
                       "-f", "s16le", "-ac", "1", "-acodec", "pcm_s16le", "-ar", str(sr), "-"]
                out = _sp.run(cmd, capture_output=True, check=True).stdout
                return _np.frombuffer(out, _np.int16).flatten().astype(_np.float32) / 32768.0
            _wa.load_audio = _patched_load_audio
        except Exception:
            pass

        # Patch whisper.transcribe's tqdm so we get live progress callbacks
        try:
            if _TrackingTqdm is not None:
                import sys as _sys2
                import whisper.transcribe  # noqa: F401 — side-effect: loads module
                _sys2.modules["whisper.transcribe"].tqdm.tqdm = _TrackingTqdm
        except Exception:
            pass

        _whisper_init_done = True


# Preload whisper in background — ready by the time the user uploads a file
if WHISPER_AVAILABLE:
    _threading.Thread(target=_ensure_whisper_loaded, daemon=True).start()


# ── optional dependency imports ───────────────────────────────────────────────

try:
    from docx import Document as DocxDocument
    DOCX_AVAILABLE = True
except ImportError:
    DOCX_AVAILABLE = False

try:
    import pdfplumber
    PDF_AVAILABLE = True
except ImportError:
    PDF_AVAILABLE = False


# ── format constants ──────────────────────────────────────────────────────────

AUDIO_EXTS = {".mp3", ".wav", ".m4a", ".flac", ".ogg", ".aac", ".opus"}
VIDEO_EXTS = {".mp4", ".mov", ".avi", ".mkv", ".webm", ".m4v"}

# Approximate CPU realtime speed multiplier for each Whisper model size.
# e.g. "base" processes ~16 minutes of audio per minute of wall-clock time.
WHISPER_SPEED = {"tiny": 32, "base": 16, "small": 6, "medium": 2, "large": 1}


def _get_audio_duration(path: str) -> float:
    """Return audio/video duration in seconds, or 0.0 if unavailable.

    Uses ffprobe (same directory as ffmpeg) with an ffmpeg -i fallback.
    The old str.replace("ffmpeg","ffprobe") replaced every occurrence including
    the directory name, producing a non-existent path on Windows imageio bundles.
    """
    # ── Strategy 1: ffprobe in the same directory as the ffmpeg binary ────────
    try:
        _p = Path(FFMPEG_EXE)
        ffprobe = str(_p.with_name(_p.name.replace("ffmpeg", "ffprobe")))
        result = _sp.run(
            [ffprobe, "-v", "quiet", "-print_format", "json", "-show_format", path],
            capture_output=True, text=True, timeout=10,
        )
        data = json.loads(result.stdout)
        return float(data["format"]["duration"])
    except Exception:
        pass

    # ── Strategy 2: parse duration from ffmpeg -i stderr output ───────────────
    try:
        result = _sp.run(
            [FFMPEG_EXE, "-i", path],
            capture_output=True, text=True, timeout=10,
        )
        m = re.search(r"Duration:\s*(\d+):(\d+):(\d+\.?\d*)", result.stderr)
        if m:
            return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
    except Exception:
        pass

    return 0.0


def _fmt_duration(secs: float) -> str:
    secs = int(secs)
    h, rem = divmod(secs, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h {m:02d}m"
    if m:
        return f"{m}m {s:02d}s"
    return f"{s}s"


# ── dataclasses ───────────────────────────────────────────────────────────────

@dataclass
class SpeakerStats:
    name: str = ""
    words_per_minute: float = 0.0
    total_words: int = 0
    speaking_time_seconds: float = 0.0
    speaking_percentage: float = 0.0
    pace_label: str = ""          # Slow / Normal / Fast / Very Fast
    accent_indicators: str = ""   # Claude-inferred accent markers
    accent_confidence: str = ""   # low / medium / high


@dataclass
class ReportConfig:
    style: str = "formal"         # formal | casual | executive | bullet
    include_summary: bool = True
    include_key_points: bool = True
    include_action_items: bool = True
    include_transcript: bool = True
    include_speaker_profiles: bool = True
    include_speech_analytics: bool = True
    include_interview_mode: bool = False
    include_interview_deep: bool = False   # deflection + % + prep guide (optional)
    interview_resume_context: str = ""     # user's resume/narratives for context
    analysis_depth: str = "balanced"       # fast | balanced | deep


@dataclass
class TranscriptResult:
    clean_transcript: str = ""
    speaker_dialogue: str = ""
    summary: str = ""
    key_points: list = field(default_factory=list)
    action_items: list = field(default_factory=list)
    speaker_map: dict = field(default_factory=dict)
    speaker_profiles: dict = field(default_factory=dict)
    speaker_stats: list = field(default_factory=list)
    detected_language: str = ""   # human-readable, e.g. "English" or "Spanish (es-CO)"
    interview_questions: list = field(default_factory=list)
    round_advance_probability: int = -1   # -1 = not calculated (deep mode off)
    prep_guide: list = field(default_factory=list)


# ── file loaders ──────────────────────────────────────────────────────────────

def _fmt_ts(seconds: float) -> str:
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    return f"{h:02d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def load_audio_video(path: str, model_size: str = "base", on_progress=None,
                     on_stage_change=None, language: str = None, on_log=None) -> str:
    """Transcribe audio/video using OpenAI Whisper with timestamps.
    on_progress(pct: float) — live 0.0-1.0 progress updates.
    on_log(msg: str)        — human-readable step-by-step log messages.
    language: ISO-639-1 code (e.g. "es", "en") or None for auto-detect.
    """
    def _log(m):
        _safe_print(f"  {m}")
        if on_log: on_log(m)

    global _progress_cb
    if not WHISPER_AVAILABLE:
        raise ImportError("Run: pip install openai-whisper")
    _ensure_whisper_loaded()

    # ── detect file duration for ETA estimate ─────────────────────────────────
    dur_secs = _get_audio_duration(path)
    dur_note = f" ({_fmt_duration(dur_secs)} audio)" if dur_secs > 0 else ""
    speed    = WHISPER_SPEED.get(model_size, 8)
    if dur_secs > 0:
        est_secs = dur_secs / speed
        _log(f"File duration: {_fmt_duration(dur_secs)}  |  Est. transcription time: {_fmt_duration(est_secs)} (Whisper '{model_size}')")
    else:
        _log(f"File duration: unknown  |  Loading Whisper '{model_size}' model…")

    if dur_secs > 0:
        _log(f"Loading Whisper '{model_size}' model…")
    model = openai_whisper.load_model(model_size)
    _log(f"Model loaded.")

    with _progress_lock:
        _progress_cb = on_progress

    audio_path = path
    tmp_path = None
    if Path(path).suffix.lower() in VIDEO_EXTS:
        _log("Extracting audio track from video…")
        tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
        tmp_path = tmp.name
        tmp.close()
        # -vn: ignore video, -sn: ignore subtitles, force pcm_s16le mono 16k for Whisper
        proc = _sp.run(
            [FFMPEG_EXE, "-y", "-i", path,
             "-vn", "-sn", "-acodec", "pcm_s16le",
             "-ar", "16000", "-ac", "1",
             tmp_path],
            capture_output=True,
        )
        if proc.returncode != 0:
            err = (proc.stderr or b"").decode("utf-8", errors="replace").strip()
            # keep the error short but informative
            tail = "\n".join(err.splitlines()[-8:]) if err else "(no stderr output)"
            raise RuntimeError(
                "ffmpeg failed extracting audio from video.\n"
                f"ffmpeg path: {FFMPEG_EXE}\n"
                f"input: {path}\n"
                f"stderr (tail):\n{tail}"
            )
        _log("Audio extraction complete.")
        audio_path = tmp_path

    if on_stage_change: on_stage_change("whisper")
    lang_note = f" (language: {language})" if language else " (language: auto-detect)"
    _log(f"Starting transcription{lang_note}…  This is the longest step.")
    transcribe_kwargs = {"verbose": False}
    if language:
        transcribe_kwargs["language"] = language
    try:
        result = model.transcribe(audio_path, **transcribe_kwargs)
    finally:
        with _progress_lock:
            _progress_cb = None

    total_words = sum(len(s["text"].split()) for s in result.get("segments", []))
    detected = result.get("language", "")
    _log(f"Transcription complete! ~{total_words:,} words detected{dur_note}."
         + (f"  Language: {detected}" if detected and not language else ""))

    if tmp_path:
        os.unlink(tmp_path)

    lines = []
    for seg in result["segments"]:
        lines.append(f"[{_fmt_ts(seg['start'])}] {seg['text'].strip()}")
    return "\n".join(lines), detected


def load_audio_video_panel(
    path: str, model_size: str = "base", num_speakers: int = None,
    language: str = None, on_log=None
) -> tuple:
    """
    Transcribe + diarize with WhisperX.
    Returns (timestamped_transcript, raw_whisperx_result).
    language: ISO-639-1 code or None for auto-detect.
    """
    def _log(m):
        _safe_print(f"  {m}")
        if on_log: on_log(m)

    try:
        import whisperx
        import torch
    except ImportError:
        _log("WhisperX not installed — falling back to standard Whisper (no diarization)")
        text, _ = load_audio_video(path, model_size, language=language, on_log=on_log)
        return text, {}

    hf_token = os.environ.get("HF_TOKEN", "")
    if not hf_token:
        _log("HF_TOKEN not set — falling back to standard Whisper (no diarization)")
        text, _ = load_audio_video(path, model_size, language=language, on_log=on_log)
        return text, {}

    audio_path = path
    tmp_path = None
    if Path(path).suffix.lower() in VIDEO_EXTS:
        _safe_print("  Extracting audio from video...")
        tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
        tmp_path = tmp.name
        tmp.close()
        _sp.run([FFMPEG_EXE, "-i", path, "-ar", "16000", "-ac", "1", "-y", tmp_path, "-loglevel", "error"],
                capture_output=True)
        audio_path = tmp_path

    device = "cuda" if __import__("torch").cuda.is_available() else "cpu"
    compute_type = "float16" if device == "cuda" else "int8"

    lang_note = f" (language: {language})" if language else " (language: auto-detect)"
    dur_secs = _get_audio_duration(audio_path)
    if dur_secs > 0:
        speed = WHISPER_SPEED.get(model_size, 8)
        _log(f"File duration: {_fmt_duration(dur_secs)}  |  Est. WhisperX time: {_fmt_duration(dur_secs / speed)}")
    _log(f"Transcribing with WhisperX ({model_size}){lang_note}…")
    model = whisperx.load_model(model_size, device, compute_type=compute_type, language=language)
    audio = whisperx.load_audio(audio_path)
    transcribe_kwargs = {"batch_size": 16}
    if language:
        transcribe_kwargs["language"] = language
    result = model.transcribe(audio, **transcribe_kwargs)

    _log("Aligning word timestamps…")
    model_a, metadata = whisperx.load_align_model(
        language_code=result["language"], device=device
    )
    result = whisperx.align(result["segments"], model_a, metadata, audio, device)

    _log(f"Running speaker diarization{(' (' + str(num_speakers) + ' speakers)') if num_speakers else ''}…")
    diarize_model = whisperx.DiarizationPipeline(use_auth_token=hf_token, device=device)
    diarize_kwargs = {"num_speakers": num_speakers} if num_speakers else {}
    diarize_segments = diarize_model(audio_path, **diarize_kwargs)
    result = whisperx.assign_word_speakers(diarize_segments, result)
    _log("Diarization complete.")

    if tmp_path:
        os.unlink(tmp_path)

    lines = []
    for seg in result["segments"]:
        ts = _fmt_ts(seg["start"])
        speaker = seg.get("speaker", "UNKNOWN")
        lines.append(f"[{ts}] {speaker}: {seg['text'].strip()}")

    return "\n".join(lines), result


def transcribe_with_deepgram(
    path: str,
    api_key: str,
    model: str = "nova-2",
    language: str = None,
    diarize: bool = False,
    on_log=None,
) -> tuple:
    """Transcribe audio/video using Deepgram REST API.
    Returns (timestamped_transcript_str, detected_language_str).
    """
    import requests as _req

    def _log(m):
        _safe_print(f"  {m}")
        if on_log: on_log(m)

    audio_path = path
    tmp_path = None
    if Path(path).suffix.lower() in VIDEO_EXTS:
        _log("Extracting audio track from video...")
        tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
        tmp_path = tmp.name
        tmp.close()
        proc = _sp.run(
            [FFMPEG_EXE, "-y", "-i", path,
             "-vn", "-sn", "-acodec", "pcm_s16le", "-ar", "16000", "-ac", "1", tmp_path],
            capture_output=True,
        )
        if proc.returncode != 0:
            err = (proc.stderr or b"").decode("utf-8", errors="replace").strip()
            raise RuntimeError(f"ffmpeg failed extracting audio.\nstderr: {err[-400:]}")
        _log("Audio extraction complete.")
        audio_path = tmp_path

    dur_secs = _get_audio_duration(audio_path)
    dur_note = f" ({_fmt_duration(dur_secs)})" if dur_secs > 0 else ""
    _log(f"Sending to Deepgram ({model}){dur_note}...")

    params = {"model": model, "punctuate": "true", "smart_format": "true", "utterances": "true"}
    if diarize:
        params["diarize"] = "true"
    if language and language != "auto":
        params["language"] = language

    headers = {"Authorization": f"Token {api_key}"}
    ct_map = {
        "mp3": "audio/mpeg", "mp4": "audio/mp4", "wav": "audio/wav",
        "m4a": "audio/mp4", "ogg": "audio/ogg", "flac": "audio/flac",
        "webm": "audio/webm", "aac": "audio/aac", "wma": "audio/x-ms-wma",
    }
    content_type = ct_map.get(Path(audio_path).suffix.lower().lstrip("."), "audio/wav")

    # Quick connectivity check before uploading audio
    import socket as _sock
    try:
        _sock.setdefaulttimeout(5)
        _sock.socket(_sock.AF_INET, _sock.SOCK_STREAM).connect(("api.deepgram.com", 443))
    except Exception:
        raise RuntimeError(
            "Cannot reach api.deepgram.com:443 — your network or firewall is blocking "
            "outbound HTTPS to Deepgram. Try switching to Whisper (Local) in the "
            "Transcription Engine settings, or check your VPN/proxy/firewall settings."
        )

    try:
        with open(audio_path, "rb") as f:
            audio_bytes = f.read()
        resp = _req.post(
            "https://api.deepgram.com/v1/listen",
            params=params,
            headers={**headers, "Content-Type": content_type},
            data=audio_bytes,
            timeout=300,
        )
        resp.raise_for_status()
        data = resp.json()
    except _req.exceptions.HTTPError as e:
        body = ""
        try: body = e.response.text[:300]
        except Exception: pass
        raise RuntimeError(f"Deepgram API error {e.response.status_code}: {body}")
    except _req.exceptions.ConnectionError:
        raise RuntimeError(
            "Lost connection to api.deepgram.com mid-upload. Check your network "
            "stability, or switch to Whisper (Local) transcription."
        )
    except _req.exceptions.Timeout:
        raise RuntimeError(
            "Deepgram request timed out (>5 min). The file may be too large, "
            "or your connection is too slow. Try Whisper (Local) transcription instead."
        )
    except _req.exceptions.RequestException as e:
        raise RuntimeError(f"Deepgram connection error: {e}")
    finally:
        if tmp_path:
            try: os.unlink(tmp_path)
            except Exception: pass

    results = data.get("results", {})
    detected_lang = (data.get("metadata", {}).get("detected_language") or "")
    lines = []

    if diarize:
        utterances = results.get("utterances") or []
        if utterances:
            for u in utterances:
                ts = _fmt_ts(u.get("start", 0))
                speaker = f"SPEAKER_{int(u.get('speaker', 0)):02d}"
                text = (u.get("transcript") or "").strip()
                if text:
                    lines.append(f"[{ts}] {speaker}: {text}")
        else:
            words = (results.get("channels", [{}])[0]
                            .get("alternatives", [{}])[0]
                            .get("words", []))
            cur_spk, cur_start, cur_words = None, 0.0, []
            for w in words:
                spk = int(w.get("speaker", 0))
                word = w.get("punctuated_word") or w.get("word", "")
                if spk != cur_spk:
                    if cur_words:
                        lines.append(f"[{_fmt_ts(cur_start)}] SPEAKER_{cur_spk:02d}: {' '.join(cur_words)}")
                    cur_spk, cur_start, cur_words = spk, w.get("start", 0.0), [word]
                else:
                    cur_words.append(word)
            if cur_words:
                lines.append(f"[{_fmt_ts(cur_start)}] SPEAKER_{cur_spk:02d}: {' '.join(cur_words)}")
    else:
        words = (results.get("channels", [{}])[0]
                        .get("alternatives", [{}])[0]
                        .get("words", []))
        if words:
            chunk_start = words[0].get("start", 0.0)
            chunk_words = []
            for w in words:
                chunk_words.append(w.get("punctuated_word") or w.get("word", ""))
                if w.get("end", 0) - chunk_start >= 30:
                    lines.append(f"[{_fmt_ts(chunk_start)}] {' '.join(chunk_words)}")
                    chunk_start = w.get("end", 0.0)
                    chunk_words = []
            if chunk_words:
                lines.append(f"[{_fmt_ts(chunk_start)}] {' '.join(chunk_words)}")
        else:
            full = (results.get("channels", [{}])[0]
                           .get("alternatives", [{}])[0]
                           .get("transcript", ""))
            if full:
                lines.append(full)

    total_words = sum(len(ln.split()) for ln in lines)
    _log(f"Deepgram transcription complete! ~{total_words:,} words."
         + (f"  Language: {detected_lang}" if detected_lang else ""))

    return "\n".join(lines), detected_lang


def transcribe_with_assemblyai(
    path: str,
    api_key: str,
    model: str = "best",
    language: str = None,
    diarize: bool = False,
    on_log=None,
) -> tuple:
    """AssemblyAI REST API. Returns (transcript_str, detected_lang)."""
    import requests as _req
    import time as _t_aai

    def _log(m):
        _safe_print(f"  {m}")
        if on_log: on_log(m)

    headers = {"authorization": api_key}
    audio_path = path
    tmp_path = None
    if Path(path).suffix.lower() in VIDEO_EXTS:
        _log("Extracting audio from video...")
        tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
        tmp_path = tmp.name; tmp.close()
        proc = _sp.run(
            [FFMPEG_EXE, "-y", "-i", path, "-vn", "-sn",
             "-acodec", "pcm_s16le", "-ar", "16000", "-ac", "1", tmp_path],
            capture_output=True,
        )
        if proc.returncode != 0:
            raise RuntimeError("ffmpeg audio extraction failed for AssemblyAI.")
        audio_path = tmp_path

    dur_secs = _get_audio_duration(audio_path)
    dur_note = f" ({_fmt_duration(dur_secs)})" if dur_secs > 0 else ""
    _log(f"Uploading to AssemblyAI{dur_note}...")

    import socket as _sock_aai
    try:
        _sock_aai.setdefaulttimeout(5)
        _sock_aai.socket(_sock_aai.AF_INET, _sock_aai.SOCK_STREAM).connect(("api.assemblyai.com", 443))
    except Exception:
        raise RuntimeError(
            "Cannot reach api.assemblyai.com:443 — your network or firewall is blocking "
            "outbound HTTPS to AssemblyAI. Switch to Whisper (Local) or check your VPN/firewall."
        )

    data = {}
    try:
        with open(audio_path, "rb") as f:
            up = _req.post("https://api.assemblyai.com/v2/upload",
                           headers=headers, data=f, timeout=300)
        up.raise_for_status()
        audio_url = up.json()["upload_url"]

        payload: dict = {
            "audio_url": audio_url,
            "speaker_labels": diarize,
            "language_detection": True,
        }
        if model == "nano":
            payload["speech_model"] = "nano"
        if language and language != "auto":
            payload["language_code"] = language
            payload["language_detection"] = False

        sub = _req.post("https://api.assemblyai.com/v2/transcript",
                        headers=headers, json=payload, timeout=30)
        sub.raise_for_status()
        job_id = sub.json()["id"]
        _log(f"AssemblyAI job queued (id={job_id[:8]}...), processing...")

        poll_url = f"https://api.assemblyai.com/v2/transcript/{job_id}"
        while True:
            _t_aai.sleep(3)
            r = _req.get(poll_url, headers=headers, timeout=30)
            r.raise_for_status()
            data = r.json()
            status = data.get("status", "")
            if status == "completed":
                break
            if status == "error":
                raise RuntimeError(f"AssemblyAI error: {data.get('error', 'unknown')}")
            _log(f"AssemblyAI: {status}...")
    finally:
        if tmp_path:
            try: os.unlink(tmp_path)
            except Exception: pass

    detected_lang = data.get("language_code", "")
    lines = []
    if diarize and data.get("utterances"):
        for u in data["utterances"]:
            ts = _fmt_ts((u.get("start") or 0) / 1000.0)
            spk = f"SPEAKER_{u.get('speaker', 'A')}"
            txt = (u.get("text") or "").strip()
            if txt:
                lines.append(f"[{ts}] {spk}: {txt}")
    else:
        words = data.get("words") or []
        if words:
            chunk_start = (words[0].get("start") or 0) / 1000.0
            chunk_words: list = []
            for w in words:
                chunk_words.append(w.get("text", ""))
                if (w.get("end") or 0) / 1000.0 - chunk_start >= 30:
                    lines.append(f"[{_fmt_ts(chunk_start)}] {' '.join(chunk_words)}")
                    chunk_start = (w.get("end") or 0) / 1000.0
                    chunk_words = []
            if chunk_words:
                lines.append(f"[{_fmt_ts(chunk_start)}] {' '.join(chunk_words)}")
        else:
            txt = data.get("text", "")
            if txt:
                lines.append(txt)

    total_words = sum(len(ln.split()) for ln in lines)
    _log(f"AssemblyAI complete! ~{total_words:,} words."
         + (f"  Language: {detected_lang}" if detected_lang else ""))
    return "\n".join(lines), detected_lang


def transcribe_with_groq_whisper(
    path: str,
    api_key: str,
    model: str = "whisper-large-v3-turbo",
    language: str = None,
    on_log=None,
) -> tuple:
    """Groq Whisper cloud API. Returns (transcript_str, detected_lang)."""
    import requests as _req

    def _log(m):
        _safe_print(f"  {m}")
        if on_log: on_log(m)

    audio_path = path
    tmp_path = None
    if Path(path).suffix.lower() in VIDEO_EXTS:
        _log("Extracting audio from video...")
        tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
        tmp_path = tmp.name; tmp.close()
        proc = _sp.run(
            [FFMPEG_EXE, "-y", "-i", path, "-vn", "-sn",
             "-acodec", "pcm_s16le", "-ar", "16000", "-ac", "1", tmp_path],
            capture_output=True,
        )
        if proc.returncode != 0:
            raise RuntimeError("ffmpeg audio extraction failed for Groq Whisper.")
        audio_path = tmp_path

    dur_secs = _get_audio_duration(audio_path)
    dur_note = f" ({_fmt_duration(dur_secs)})" if dur_secs > 0 else ""
    _log(f"Sending to Groq Whisper ({model}){dur_note}...")

    import socket as _sock_groq
    try:
        _sock_groq.setdefaulttimeout(5)
        _sock_groq.socket(_sock_groq.AF_INET, _sock_groq.SOCK_STREAM).connect(("api.groq.com", 443))
    except Exception:
        raise RuntimeError(
            "Cannot reach api.groq.com:443 — your network or firewall is blocking "
            "outbound HTTPS to Groq. Switch to Whisper (Local) or check your VPN/firewall."
        )

    try:
        with open(audio_path, "rb") as f:
            audio_bytes = f.read()
        fields: dict = {
            "model": model,
            "response_format": "verbose_json",
            "timestamp_granularities[]": "segment",
        }
        if language and language != "auto":
            fields["language"] = language
        resp = _req.post(
            "https://api.groq.com/openai/v1/audio/transcriptions",
            headers={"Authorization": f"Bearer {api_key}"},
            files={"file": (Path(audio_path).name, audio_bytes, "audio/wav")},
            data=fields,
            timeout=300,
        )
        resp.raise_for_status()
    except _req.exceptions.HTTPError as e:
        body = ""
        try: body = e.response.text[:300]
        except Exception: pass
        raise RuntimeError(f"Groq Whisper error {e.response.status_code}: {body}")
    finally:
        if tmp_path:
            try: os.unlink(tmp_path)
            except Exception: pass

    result = resp.json()
    detected_lang = result.get("language", "")
    segments = result.get("segments") or []
    lines = []
    if segments:
        for seg in segments:
            ts = _fmt_ts(seg.get("start", 0))
            txt = (seg.get("text") or "").strip()
            if txt:
                lines.append(f"[{ts}] {txt}")
    else:
        txt = result.get("text", "")
        if txt:
            lines.append(txt)

    total_words = sum(len(ln.split()) for ln in lines)
    _log(f"Groq Whisper complete! ~{total_words:,} words."
         + (f"  Language: {detected_lang}" if detected_lang else ""))
    return "\n".join(lines), detected_lang


def transcribe_with_openai_whisper_api(
    path: str,
    api_key: str,
    model: str = "whisper-1",
    language: str = None,
    on_log=None,
) -> tuple:
    """OpenAI Whisper cloud API. Returns (transcript_str, detected_lang)."""
    import requests as _req

    def _log(m):
        _safe_print(f"  {m}")
        if on_log: on_log(m)

    audio_path = path
    tmp_path = None
    if Path(path).suffix.lower() in VIDEO_EXTS:
        _log("Extracting audio from video...")
        tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
        tmp_path = tmp.name; tmp.close()
        proc = _sp.run(
            [FFMPEG_EXE, "-y", "-i", path, "-vn", "-sn",
             "-acodec", "pcm_s16le", "-ar", "16000", "-ac", "1", tmp_path],
            capture_output=True,
        )
        if proc.returncode != 0:
            raise RuntimeError("ffmpeg audio extraction failed for OpenAI Whisper API.")
        audio_path = tmp_path

    dur_secs = _get_audio_duration(audio_path)
    dur_note = f" ({_fmt_duration(dur_secs)})" if dur_secs > 0 else ""
    _log(f"Sending to OpenAI Whisper API ({model}){dur_note}...")

    import socket as _sock_oai
    try:
        _sock_oai.setdefaulttimeout(5)
        _sock_oai.socket(_sock_oai.AF_INET, _sock_oai.SOCK_STREAM).connect(("api.openai.com", 443))
    except Exception:
        raise RuntimeError(
            "Cannot reach api.openai.com:443 — your network or firewall is blocking "
            "outbound HTTPS to OpenAI. Switch to Whisper (Local) or check your VPN/firewall."
        )

    try:
        with open(audio_path, "rb") as f:
            audio_bytes = f.read()
        fields: dict = {
            "model": model,
            "response_format": "verbose_json",
            "timestamp_granularities[]": "segment",
        }
        if language and language != "auto":
            fields["language"] = language
        resp = _req.post(
            "https://api.openai.com/v1/audio/transcriptions",
            headers={"Authorization": f"Bearer {api_key}"},
            files={"file": (Path(audio_path).name, audio_bytes, "audio/wav")},
            data=fields,
            timeout=300,
        )
        resp.raise_for_status()
    except _req.exceptions.HTTPError as e:
        body = ""
        try: body = e.response.text[:300]
        except Exception: pass
        raise RuntimeError(f"OpenAI Whisper API error {e.response.status_code}: {body}")
    finally:
        if tmp_path:
            try: os.unlink(tmp_path)
            except Exception: pass

    result = resp.json()
    detected_lang = result.get("language", "")
    segments = result.get("segments") or []
    lines = []
    if segments:
        for seg in segments:
            ts = _fmt_ts(seg.get("start", 0))
            txt = (seg.get("text") or "").strip()
            if txt:
                lines.append(f"[{ts}] {txt}")
    else:
        txt = result.get("text", "")
        if txt:
            lines.append(txt)

    total_words = sum(len(ln.split()) for ln in lines)
    _log(f"OpenAI Whisper API complete! ~{total_words:,} words."
         + (f"  Language: {detected_lang}" if detected_lang else ""))
    return "\n".join(lines), detected_lang


def transcribe_with_google_stt(
    path: str,
    api_key: str,
    model: str = "latest_long",
    language: str = None,
    on_log=None,
) -> tuple:
    """Google Cloud Speech-to-Text REST API. Returns (transcript_str, detected_lang)."""
    import requests as _req
    import base64 as _b64
    import time as _t_gcp

    def _log(m):
        _safe_print(f"  {m}")
        if on_log: on_log(m)

    audio_path = path
    tmp_path = None
    if Path(path).suffix.lower() != ".wav" or True:
        _log("Converting audio to 16kHz WAV for Google STT...")
        tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
        tmp_path = tmp.name; tmp.close()
        proc = _sp.run(
            [FFMPEG_EXE, "-y", "-i", path, "-vn", "-sn",
             "-acodec", "pcm_s16le", "-ar", "16000", "-ac", "1", tmp_path],
            capture_output=True,
        )
        if proc.returncode != 0:
            raise RuntimeError("ffmpeg conversion failed for Google STT.")
        audio_path = tmp_path

    dur_secs = _get_audio_duration(audio_path)
    dur_note = f" ({_fmt_duration(dur_secs)})" if dur_secs > 0 else ""
    _log(f"Sending to Google Cloud STT ({model}){dur_note}...")

    import socket as _sock_gcp
    try:
        _sock_gcp.setdefaulttimeout(5)
        _sock_gcp.socket(_sock_gcp.AF_INET, _sock_gcp.SOCK_STREAM).connect(("speech.googleapis.com", 443))
    except Exception:
        raise RuntimeError(
            "Cannot reach speech.googleapis.com:443 — your network or firewall is blocking "
            "outbound HTTPS to Google Cloud STT. Switch to Whisper (Local) or check your VPN/firewall."
        )

    lang_code = "en-US"
    if language and language != "auto":
        lang_code = language if "-" in language else language

    data = {}
    try:
        with open(audio_path, "rb") as f:
            audio_b64 = _b64.b64encode(f.read()).decode()

        config = {
            "encoding": "LINEAR16",
            "sampleRateHertz": 16000,
            "languageCode": lang_code,
            "model": model,
            "enableAutomaticPunctuation": True,
            "enableWordTimeOffsets": True,
        }
        body = {"config": config, "audio": {"content": audio_b64}}

        resp = _req.post(
            f"https://speech.googleapis.com/v1/speech:longrunningrecognize?key={api_key}",
            json=body, timeout=60,
        )
        resp.raise_for_status()
        op_name = resp.json().get("name", "")
        _log(f"Google STT job submitted, processing...")

        op_url = f"https://speech.googleapis.com/v1/operations/{op_name}?key={api_key}"
        while True:
            _t_gcp.sleep(5)
            r = _req.get(op_url, timeout=30)
            r.raise_for_status()
            data = r.json()
            if data.get("done"):
                break
            pct = data.get("metadata", {}).get("progressPercent", 0)
            _log(f"Google STT: {pct}% complete...")
    except _req.exceptions.HTTPError as e:
        body_txt = ""
        try: body_txt = e.response.text[:400]
        except Exception: pass
        raise RuntimeError(f"Google STT error {e.response.status_code}: {body_txt}")
    finally:
        if tmp_path:
            try: os.unlink(tmp_path)
            except Exception: pass

    if "error" in data:
        raise RuntimeError(f"Google STT error: {data['error'].get('message', 'unknown')}")

    results = (data.get("response") or {}).get("results") or []
    lines = []
    for res in results:
        alt = (res.get("alternatives") or [{}])[0]
        words = alt.get("words") or []
        if words:
            chunk_start = float((words[0].get("startTime") or "0s").rstrip("s"))
            chunk_words: list = []
            for w in words:
                chunk_words.append(w.get("word", ""))
                end = float((w.get("endTime") or "0s").rstrip("s"))
                if end - chunk_start >= 30:
                    lines.append(f"[{_fmt_ts(chunk_start)}] {' '.join(chunk_words)}")
                    chunk_start = end
                    chunk_words = []
            if chunk_words:
                lines.append(f"[{_fmt_ts(chunk_start)}] {' '.join(chunk_words)}")
        else:
            txt = alt.get("transcript", "").strip()
            if txt:
                lines.append(txt)

    total_words = sum(len(ln.split()) for ln in lines)
    _log(f"Google STT complete! ~{total_words:,} words.")
    return "\n".join(lines), lang_code


def transcribe_with_elevenlabs(
    path: str,
    api_key: str,
    model: str = "scribe_v1",
    language: str = None,
    on_log=None,
) -> tuple:
    """ElevenLabs Scribe STT. Returns (transcript_str, detected_lang)."""
    import requests as _req

    def _log(m):
        _safe_print(f"  {m}")
        if on_log: on_log(m)

    audio_path = path
    tmp_path = None
    _SUPPORTED = {".mp3", ".wav", ".m4a", ".mp4", ".webm", ".ogg", ".flac", ".aac"}
    if Path(path).suffix.lower() not in _SUPPORTED:
        _log("Converting audio for ElevenLabs...")
        tmp = tempfile.NamedTemporaryFile(suffix=".mp3", delete=False)
        tmp_path = tmp.name; tmp.close()
        proc = _sp.run(
            [FFMPEG_EXE, "-y", "-i", path, "-vn", "-ar", "16000", "-ac", "1", "-q:a", "4", tmp_path],
            capture_output=True,
        )
        if proc.returncode != 0:
            raise RuntimeError("ffmpeg conversion failed for ElevenLabs.")
        audio_path = tmp_path

    dur_secs = _get_audio_duration(audio_path)
    dur_note = f" ({_fmt_duration(dur_secs)})" if dur_secs > 0 else ""
    _log(f"Sending to ElevenLabs Scribe ({model}){dur_note}...")

    try:
        with open(audio_path, "rb") as f:
            form = {"model_id": (None, model)}
            if language and language != "auto":
                form["language_code"] = (None, language)
            resp = _req.post(
                "https://api.elevenlabs.io/v1/speech-to-text",
                headers={"xi-api-key": api_key},
                files={"audio": f},
                data={"model_id": model, **({"language_code": language} if language and language != "auto" else {})},
                timeout=300,
            )
        resp.raise_for_status()
    except _req.exceptions.HTTPError as e:
        body = ""
        try: body = e.response.text[:300]
        except Exception: pass
        raise RuntimeError(f"ElevenLabs API error {e.response.status_code}: {body}")
    except _req.exceptions.ConnectionError:
        raise RuntimeError("Cannot reach api.elevenlabs.io — check your network or firewall.")
    except _req.exceptions.Timeout:
        raise RuntimeError("ElevenLabs request timed out (>5 min). File may be too large.")
    finally:
        if tmp_path:
            try: os.unlink(tmp_path)
            except Exception: pass

    data = resp.json()
    text = data.get("text", "").strip()
    detected_lang = data.get("language_code", "")
    if not text:
        raise RuntimeError("ElevenLabs returned an empty transcript.")

    total_words = len(text.split())
    _log(f"ElevenLabs complete! ~{total_words:,} words." + (f"  Language: {detected_lang}" if detected_lang else ""))
    return text, detected_lang


def transcribe_with_rev_ai(
    path: str,
    api_key: str,
    model: str = "machine",
    language: str = None,
    on_log=None,
) -> tuple:
    """Rev.ai STT (async). Returns (transcript_str, detected_lang)."""
    import requests as _req
    import time as _t_rev

    def _log(m):
        _safe_print(f"  {m}")
        if on_log: on_log(m)

    audio_path = path
    tmp_path = None
    _SUPPORTED = {".mp3", ".wav", ".m4a", ".mp4", ".ogg", ".flac", ".aac"}
    if Path(path).suffix.lower() not in _SUPPORTED:
        _log("Converting audio for Rev.ai...")
        tmp = tempfile.NamedTemporaryFile(suffix=".mp3", delete=False)
        tmp_path = tmp.name; tmp.close()
        proc = _sp.run(
            [FFMPEG_EXE, "-y", "-i", path, "-vn", "-ar", "16000", "-ac", "1", "-q:a", "4", tmp_path],
            capture_output=True,
        )
        if proc.returncode != 0:
            raise RuntimeError("ffmpeg conversion failed for Rev.ai.")
        audio_path = tmp_path

    dur_secs = _get_audio_duration(audio_path)
    dur_note = f" ({_fmt_duration(dur_secs)})" if dur_secs > 0 else ""
    _log(f"Uploading to Rev.ai ({model}){dur_note}...")

    headers = {"Authorization": f"Bearer {api_key}"}
    job_id = None
    try:
        with open(audio_path, "rb") as f:
            submit_data = {}
            if model:
                submit_data["transcriber"] = model
            if language and language != "auto":
                submit_data["language"] = language
            resp = _req.post(
                "https://api.rev.ai/speechtotext/v1/jobs",
                headers=headers,
                files={"media": (Path(audio_path).name, f)},
                data={"options": str(submit_data).replace("'", '"')} if submit_data else {},
                timeout=300,
            )
        resp.raise_for_status()
        job_id = resp.json()["id"]
        _log(f"Rev.ai job submitted (id={job_id[:8]}...), processing...")

        while True:
            _t_rev.sleep(5)
            r = _req.get(f"https://api.rev.ai/speechtotext/v1/jobs/{job_id}", headers=headers, timeout=30)
            r.raise_for_status()
            status = r.json().get("status", "")
            if status == "transcribed":
                break
            if status in ("failed", "deleted"):
                fail = r.json().get("failure_detail", "unknown error")
                raise RuntimeError(f"Rev.ai job failed: {fail}")
            _log(f"Rev.ai: {status}...")

        tr = _req.get(
            f"https://api.rev.ai/speechtotext/v1/jobs/{job_id}/transcript",
            headers={**headers, "Accept": "text/plain"},
            timeout=60,
        )
        tr.raise_for_status()
        text = tr.text.strip()
    except _req.exceptions.HTTPError as e:
        body = ""
        try: body = e.response.text[:300]
        except Exception: pass
        raise RuntimeError(f"Rev.ai API error {e.response.status_code}: {body}")
    except _req.exceptions.ConnectionError:
        raise RuntimeError("Cannot reach api.rev.ai — check your network or firewall.")
    finally:
        if tmp_path:
            try: os.unlink(tmp_path)
            except Exception: pass

    total_words = len(text.split())
    _log(f"Rev.ai complete! ~{total_words:,} words.")
    return text, ""


def load_srt(path: str) -> str:
    text = Path(path).read_text(encoding="utf-8", errors="replace")
    lines, ts, buf = [], "", []
    for line in text.splitlines():
        line = line.strip()
        if re.match(r"^\d+$", line):
            if buf:
                lines.append(f"[{ts}] {' '.join(buf)}")
            buf = []
        elif "-->" in line:
            ts = line.split("-->")[0].strip()[:8]
        elif line:
            buf.append(line)
    if buf:
        lines.append(f"[{ts}] {' '.join(buf)}")
    return "\n".join(lines)


def load_vtt(path: str) -> str:
    text = Path(path).read_text(encoding="utf-8", errors="replace")
    lines, ts, buf = [], "", []
    for line in text.splitlines():
        line = line.strip()
        if line.startswith(("WEBVTT", "NOTE", "STYLE", "REGION")):
            continue
        if "-->" in line:
            if buf:
                lines.append(f"[{ts}] {' '.join(buf)}")
            ts = line.split("-->")[0].strip()[:8]
            buf = []
        elif line:
            buf.append(line)
    if buf:
        lines.append(f"[{ts}] {' '.join(buf)}")
    return "\n".join(lines)


def load_docx(path: str) -> str:
    if not DOCX_AVAILABLE:
        raise ImportError("Run: pip install python-docx")
    doc = DocxDocument(path)
    return "\n".join(p.text for p in doc.paragraphs if p.text.strip())


def load_pdf(path: str) -> str:
    if not PDF_AVAILABLE:
        raise ImportError("Run: pip install pdfplumber")
    parts = []
    with pdfplumber.open(path) as pdf:
        for i, page in enumerate(pdf.pages, 1):
            text = page.extract_text() or ""
            if text.strip():
                parts.append(f"[Page {i}]\n{text}")
    return "\n\n".join(parts)


def load_file(path: str, whisper_model: str = "base") -> tuple:
    """Returns (raw_text, format_label)."""
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"File not found: {path}")

    ext = p.suffix.lower()
    _safe_print(f"  Format detected: {ext or 'text'}")

    if ext in AUDIO_EXTS:
        return load_audio_video(path, whisper_model), "audio recording"
    if ext in VIDEO_EXTS:
        return load_audio_video(path, whisper_model), "video recording"
    if ext == ".srt":
        return load_srt(path), "SRT subtitle file"
    if ext == ".vtt":
        return load_vtt(path), "WebVTT subtitle file"
    if ext == ".docx":
        return load_docx(path), "Word document"
    if ext == ".pdf":
        return load_pdf(path), "PDF document"
    return p.read_text(encoding="utf-8", errors="replace"), "plain text"


# ── speech analytics ──────────────────────────────────────────────────────────

def calculate_speaker_stats_from_segments(segments: list) -> dict:
    """Compute per-speaker WPM from WhisperX diarized segments."""
    raw = {}
    for seg in segments:
        speaker = seg.get("speaker", "UNKNOWN")
        words = len(seg["text"].split())
        duration = seg["end"] - seg["start"]
        if speaker not in raw:
            raw[speaker] = {"words": 0, "duration": 0.0}
        raw[speaker]["words"] += words
        raw[speaker]["duration"] += duration

    total_duration = sum(v["duration"] for v in raw.values()) or 1.0
    result = {}
    for speaker, data in raw.items():
        wpm = (data["words"] / data["duration"] * 60) if data["duration"] > 0 else 0.0
        result[speaker] = {
            "wpm": round(wpm, 1),
            "total_words": data["words"],
            "speaking_time": round(data["duration"], 1),
            "speaking_pct": round(data["duration"] / total_duration * 100, 1),
            "pace": (
                "Slow"      if wpm < 120 else
                "Normal"    if wpm < 150 else
                "Fast"      if wpm < 180 else
                "Very Fast"
            ),
        }
    return result


def calculate_overall_stats_from_text(text: str) -> dict:
    """Estimate overall WPM from timestamped lines like [00:01:23] text."""
    matches = list(re.finditer(
        r'\[(\d{1,2}):(\d{2})(?::(\d{2}))?\]\s*(?:\S+:\s*)?(.*)', text
    ))
    if len(matches) < 2:
        return {}

    def to_secs(m):
        h, mi, s = m.group(1), m.group(2), m.group(3) or "0"
        return int(h) * 3600 + int(mi) * 60 + int(s)

    t0 = to_secs(matches[0])
    t1 = to_secs(matches[-1])
    total_words = sum(len(m.group(4).split()) for m in matches)
    duration = t1 - t0
    if duration <= 0:
        return {}
    return {
        "overall_wpm": round(total_words / (duration / 60), 1),
        "total_words": total_words,
        "duration_secs": duration,
    }


# ── Claude prompts ────────────────────────────────────────────────────────────

STYLE_INSTRUCTIONS = {
    "formal":    "Use professional, formal language with clear section headings.",
    "casual":    "Use clear, conversational language. Keep it friendly and approachable.",
    "executive": "Be extremely concise. Summary max 3 sentences. Key points max 5 bullets. Skip minor details.",
    "bullet":    "Use bullet points for nearly everything. Minimize prose paragraphs.",
}

PANEL_SYSTEM_PROMPT = """\
You are an expert at processing panel discussion transcripts with multiple speakers.
Analyze the transcript and return ONLY a valid JSON object — no markdown, no extra text."""

STANDARD_SYSTEM_PROMPT = """\
You are an expert transcript processor.
Analyze the transcript and return ONLY a valid JSON object — no markdown, no extra text."""


_INTERVIEW_JSON_FIELD = '''\
  "interview_questions": [
    {
      "question": "Exact question asked",
      "your_answer_summary": "3-5 sentences capturing what the candidate actually said — include the specific points, examples, or stories they mentioned. Be faithful to what they said, not a vague paraphrase.",
      "ideal_answer": "Write this in natural first-person as if you ARE the candidate speaking out loud — confident, human, conversational. No bullet points, no AI-style structure. Sound like a well-prepared person answering in real life. Start with the direct answer, then back it up with a concrete example or story. Example style: 'I spent three years managing a distributed team across two time zones. The biggest challenge was...' NOT: 'A strong candidate should demonstrate...'",
      "verdict": "strong | acceptable | weak | missed",
      "feedback": "Specific coaching — what was missing, what landed well, how to improve"
    }
  ]'''

_INTERVIEW_JSON_FIELD_DEEP = '''\
  "interview_questions": [
    {
      "question": "Exact question asked",
      "your_answer_summary": "3-5 sentences capturing what the candidate actually said — include the specific points, examples, or stories they mentioned. Be faithful to what they said, not a vague paraphrase. If they used resume context provided, reference it.",
      "ideal_answer": "Write this in natural first-person as if you ARE the candidate speaking out loud — confident, human, conversational. No bullet points, no AI-style structure. Sound like a well-prepared person answering in real life. Start with the direct answer, then back it up with a concrete example or story. Use the candidate's resume/background context if provided to make it personal. Example style: 'I spent three years managing a distributed team across two time zones. The biggest challenge was...' NOT: 'A strong candidate should demonstrate...'",
      "verdict": "strong | acceptable | weak | missed",
      "deflection_detected": false,
      "deflection_note": "Only populate if deflection_detected is true — describe how the candidate deflected or stalled",
      "feedback": "Specific coaching — what was missing, what landed well, how to improve"
    }
  ],
  "round_advance_probability": 72,
  "prep_guide": [
    {
      "question": "Repeat of a weak or missed question",
      "why_it_matters": "Why interviewers ask this and what they are really probing for",
      "suggested_answer": "A strong, concrete answer the candidate could give next time"
    }
  ]'''

_INTERVIEW_INSTRUCTION = """\
INTERVIEW MODE: Extract every distinct interview question from the transcript.
For each question:
- your_answer_summary: Capture in 3-5 sentences what the candidate actually said, with the specific points or examples they gave. Be faithful — do not paraphrase vaguely.
- ideal_answer: Write as if YOU are the candidate speaking naturally in first person. Confident, human, conversational — no bullet points, no AI-style phrasing. A real person's well-prepared answer.
- verdict: strong | acceptable | weak | missed
- feedback: specific coaching on what landed, what was missing, how to improve.
Only include real interview questions — ignore small talk or off-topic exchanges."""

_INTERVIEW_INSTRUCTION_DEEP = """\
INTERVIEW MODE (Deep Analysis): Extract every distinct interview question from the transcript.
For each question:
- your_answer_summary: Capture in 3-5 sentences what the candidate actually said, with the specific points or examples they gave. Be faithful — do not paraphrase vaguely.
- ideal_answer: Write as if YOU are the candidate speaking naturally in first person. Confident, human, conversational — no bullet points, no AI-style phrasing. A real well-prepared person's answer. Use any resume/background context provided to make it personal and specific.
- verdict: strong | acceptable | weak | missed
- deflection_detected: true if the candidate used filler phrases, stalled, or answered around the question without directly addressing it
- deflection_note: only if deflection_detected is true — briefly describe the deflection behaviour
- feedback: specific, actionable coaching

After scoring all questions, set round_advance_probability (0-100) based on the overall quality of answers.
Rough guide: 80+ = strong candidate, 60-79 = competitive, 40-59 = borderline, <40 = unlikely.

Also produce a prep_guide: for each question with verdict weak or missed, explain why interviewers ask it
and provide a strong suggested answer the candidate can practise. Only include weak/missed questions in prep_guide.

Only include real interview questions — ignore small talk or off-topic exchanges."""


def build_panel_prompt(content, fmt, num_speakers, style, speech_data, language=None, language_variant=None, interview_mode=False, interview_deep=False, resume_context=""):
    style_note = STYLE_INSTRUCTIONS.get(style, STYLE_INSTRUCTIONS["formal"])
    speakers_hint = (
        f"Expected number of speakers: {num_speakers}"
        if num_speakers else "Number of speakers: detect automatically"
    )
    speech_section = ""
    if speech_data:
        stats_lines = "\n".join(
            f"  {spk}: {d['wpm']} WPM ({d['pace']}), {d['speaking_pct']}% of conversation"
            for spk, d in speech_data.items()
        )
        speech_section = f"\nPre-calculated speech rates:\n{stats_lines}\nUse these exact WPM values.\n"

    lang_section = ""
    if language_variant:
        lang_section = f"\nLanguage/dialect: {language_variant}. Focus accent analysis on markers specific to this regional variety (vocabulary, phonology hints, idiomatic phrases, grammar patterns).\n"
    elif language and language != "auto":
        lang_section = f"\nTranscript language: {language}. Tailor accent analysis accordingly.\n"

    if interview_mode and interview_deep:
        _instr = _INTERVIEW_INSTRUCTION_DEEP
        _field = _INTERVIEW_JSON_FIELD_DEEP
        _resume_section = f"\n<candidate_context>\n{resume_context.strip()}\n</candidate_context>\n" if resume_context and resume_context.strip() else ""
    elif interview_mode:
        _instr = _INTERVIEW_INSTRUCTION
        _field = _INTERVIEW_JSON_FIELD
        _resume_section = ""
    else:
        _instr = _field = _resume_section = ""
    interview_section = f"\n{_instr}\n" if _instr else ""
    interview_field   = f",\n{_field}" if _field else ""

    return f"""\
Format: {fmt}
{speakers_hint}
Style: {style_note}
{speech_section}{lang_section}{_resume_section}{interview_section}
<transcript>
{content}
</transcript>

Return JSON with exactly these keys:
{{
  "speaker_map": {{"SPEAKER_00": "name or role", ...}},
  "clean_transcript": "Full cleaned transcript with resolved speaker names and timestamps",
  "speaker_dialogue": "Readable dialogue with speaker labels",
  "summary": "Executive summary",
  "key_points": ["point 1", ...],
  "speaker_profiles": {{"Name": "2-3 sentence profile of contributions"}},
  "speaker_stats": [
    {{
      "name": "resolved name",
      "words_per_minute": 142.5,
      "pace_label": "Normal",
      "speaking_percentage": 35.2,
      "accent_indicators": "Likely British English — uses 'whilst', 'cheers'. Confidence: medium.",
      "accent_confidence": "medium"
    }}
  ],
  "action_items": ["action 1", ...]{interview_field}
}}

For accent_indicators: analyze vocabulary, syntax, idiomatic expressions, and regional phrases.
Always state confidence level (low/medium/high)."""


def build_standard_prompt(content, fmt, style, overall_stats, language=None, language_variant=None, interview_mode=False, interview_deep=False, resume_context=""):
    style_note = STYLE_INSTRUCTIONS.get(style, STYLE_INSTRUCTIONS["formal"])
    stats_note = ""
    if overall_stats and overall_stats.get("overall_wpm"):
        stats_note = (
            f"\nOverall speech rate: {overall_stats['overall_wpm']} WPM "
            f"({overall_stats['total_words']} words over "
            f"{overall_stats['duration_secs']//60} min)"
        )

    lang_note = ""
    if language_variant:
        lang_note = f"\nLanguage/dialect: {language_variant}. Focus accent analysis on markers specific to this regional variety (vocabulary, phonology hints, idiomatic phrases, grammar patterns)."
    elif language and language != "auto":
        lang_note = f"\nTranscript language: {language}. Tailor accent analysis accordingly."

    if interview_mode and interview_deep:
        _instr = _INTERVIEW_INSTRUCTION_DEEP
        _field = _INTERVIEW_JSON_FIELD_DEEP
        _resume_section = f"\n<candidate_context>\n{resume_context.strip()}\n</candidate_context>" if resume_context and resume_context.strip() else ""
    elif interview_mode:
        _instr = _INTERVIEW_INSTRUCTION
        _field = _INTERVIEW_JSON_FIELD
        _resume_section = ""
    else:
        _instr = _field = _resume_section = ""
    interview_section = f"\n{_instr}\n" if _instr else ""
    interview_field   = f",\n{_field}" if _field else ""

    return f"""\
Format: {fmt}
Style: {style_note}
{stats_note}{lang_note}{_resume_section}{interview_section}

<transcript>
{content}
</transcript>

Return JSON with exactly these keys:
{{
  "clean_transcript": "Full cleaned transcript with timestamps preserved",
  "speaker_dialogue": "Same content with speaker labels",
  "summary": "Executive summary",
  "key_points": ["point 1", ...],
  "speaker_stats": [
    {{
      "name": "Single Speaker or identified name",
      "words_per_minute": 0,
      "pace_label": "Normal",
      "speaking_percentage": 100,
      "accent_indicators": "Inferred accent from vocabulary and expressions. Confidence: low/medium/high.",
      "accent_confidence": "medium"
    }}
  ],
  "action_items": []{interview_field}
}}"""


# ── processing ────────────────────────────────────────────────────────────────

def _repair_truncated_json(raw: str) -> dict:
    """Close any open strings/braces left by a token-limit cutoff and re-parse."""
    in_string = False
    escape = False
    stack = []
    for ch in raw:
        if escape:
            escape = False
        elif in_string:
            if ch == '\\':
                escape = True
            elif ch == '"':
                in_string = False
        else:
            if ch == '"':
                in_string = True
            elif ch in '{[':
                stack.append(ch)
            elif ch in '}]' and stack:
                stack.pop()
    suffix = ('"' if in_string else '') + ''.join(
        '}' if c == '{' else ']' for c in reversed(stack)
    )
    return json.loads(raw + suffix)


def _parse_json(raw: str) -> dict:
    raw = re.sub(r"^```(?:json)?\s*", "", raw.strip())
    raw = re.sub(r"\s*```$", "", raw)
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return _repair_truncated_json(raw)


def _chunk(text: str, max_chars: int = 600_000) -> list:
    if len(text) <= max_chars:
        return [text]
    chunks, buf, buf_len = [], [], 0
    for line in text.split("\n"):
        line_len = len(line) + 1
        if buf_len + line_len > max_chars and buf:
            chunks.append("\n".join(buf))
            buf, buf_len = [line], line_len
        else:
            buf.append(line)
            buf_len += line_len
    if buf:
        chunks.append("\n".join(buf))
    return chunks


def _merge_summaries(client, results: list) -> str:
    _safe_print("  Merging summaries...")
    combined = "\n".join(f"- {r.get('summary', '')}" for r in results)
    return client.chat(
        system="You are a helpful assistant.",
        user=f"Combine into one 3-5 sentence executive summary:\n{combined}",
        max_tokens=512,
    ) or combined


def process_transcript(
    client,
    raw_text: str,
    fmt: str,
    panel_mode: bool = False,
    num_speakers: int = None,
    config: ReportConfig = None,
    raw_whisperx: dict = None,
    language: str = None,
    language_variant: str = None,
    speaker_names: str = None,
    on_log=None,
) -> TranscriptResult:
    def _log(m):
        _safe_print(f"  {m}")
        if on_log: on_log(m)

    config = config or ReportConfig()
    chunks = _chunk(raw_text)
    n = len(chunks)

    audio_speech_data = None
    if raw_whisperx and raw_whisperx.get("segments"):
        audio_speech_data = calculate_speaker_stats_from_segments(raw_whisperx["segments"])

    overall_text_stats = calculate_overall_stats_from_text(raw_text) if not audio_speech_data else None

    if n > 1:
        _log(f"Transcript is large — splitting into {n} chunks for Claude…")

    results = []
    for i, chunk in enumerate(chunks, 1):
        label = f"chunk {i}/{n}" if n > 1 else "transcript"
        _log(f"Sending {label} to Claude for analysis… (summary, key points, accent detection)")

        if panel_mode:
            sys_prompt = PANEL_SYSTEM_PROMPT
            prompt = build_panel_prompt(chunk, fmt, num_speakers, config.style, audio_speech_data, language, language_variant, interview_mode=config.include_interview_mode, interview_deep=config.include_interview_deep, resume_context=config.interview_resume_context)
        else:
            sys_prompt = STANDARD_SYSTEM_PROMPT
            prompt = build_standard_prompt(chunk, fmt, config.style, overall_text_stats, language, language_variant, interview_mode=config.include_interview_mode, interview_deep=config.include_interview_deep, resume_context=config.interview_resume_context)

        if speaker_names:
            # If it looks like a count ("2 speakers"), use generic labels;
            # otherwise treat as real names.
            import re as _re
            if _re.fullmatch(r'\d+\s+speakers?', speaker_names.strip(), _re.I):
                prompt = (
                    f"There are {speaker_names} in this recording.\n"
                    f"Label each speaker distinctly as Speaker 1, Speaker 2, etc.\n\n"
                ) + prompt
            else:
                prompt = (
                    f"The people in this recording are: {speaker_names}\n"
                    f"Use these names when identifying who said what in the transcript.\n\n"
                ) + prompt

        if n > 1:
            prompt = f"[Part {i} of {n}]\n\n" + prompt

        _depth = getattr(config, "analysis_depth", "balanced")
        _depth_tokens = {"fast": 4096, "balanced": 16000, "deep": 24000}
        raw = client.chat(
            system=sys_prompt,
            user=prompt,
            max_tokens=_depth_tokens.get(_depth, 16000),
            thinking=(client.provider == "anthropic" and _depth == "deep"),
        )
        results.append(_parse_json(raw))
        _log(f"Claude analysis complete{f' ({i}/{n})' if n > 1 else ''}.")

    r = results[0] if n == 1 else None

    merged = TranscriptResult(
        clean_transcript=r.get("clean_transcript", "") if r else "\n\n".join(x.get("clean_transcript", "") for x in results),
        speaker_dialogue=r.get("speaker_dialogue", "") if r else "\n\n".join(x.get("speaker_dialogue", "") for x in results),
        summary=r.get("summary", "") if r else _merge_summaries(client, results),
        key_points=r.get("key_points", []) if r else [p for x in results for p in x.get("key_points", [])],
        action_items=r.get("action_items", []) if r else [a for x in results for a in x.get("action_items", [])],
        speaker_map=(r or results[0]).get("speaker_map", {}),
        speaker_profiles=r.get("speaker_profiles", {}) if r else {k: v for x in results for k, v in x.get("speaker_profiles", {}).items()},
        interview_questions=r.get("interview_questions", []) if r else [q for x in results for q in x.get("interview_questions", [])],
        round_advance_probability=r.get("round_advance_probability", -1) if r else (results[0].get("round_advance_probability", -1) if results else -1),
        prep_guide=r.get("prep_guide", []) if r else [g for x in results for g in x.get("prep_guide", [])],
    )

    raw_stats = (r or results[0]).get("speaker_stats", [])
    for s in raw_stats:
        spk_key = s.get("name", "")
        if audio_speech_data:
            matched = next(
                (v for k, v in audio_speech_data.items() if k in spk_key or spk_key in k), None
            )
            if matched:
                s["words_per_minute"] = matched["wpm"]
                s["pace_label"] = matched["pace"]
                s["speaking_percentage"] = matched["speaking_pct"]

        merged.speaker_stats.append(SpeakerStats(
            name=s.get("name", ""),
            words_per_minute=s.get("words_per_minute", 0.0),
            pace_label=s.get("pace_label", ""),
            speaking_percentage=s.get("speaking_percentage", 0.0),
            accent_indicators=s.get("accent_indicators", ""),
            accent_confidence=s.get("accent_confidence", ""),
        ))

    return merged


# ── combined report builder ───────────────────────────────────────────────────

def build_combined_report(result: TranscriptResult, config: ReportConfig) -> str:
    divider = "=" * 60
    thin = "-" * 40
    sections = []

    if result.detected_language:
        sections += ["DOCUMENT INFO", thin,
                     f"  Language : {result.detected_language}", ""]

    if config.include_summary:
        sections += [divider, "SUMMARY", divider, result.summary, ""]

    if config.include_key_points and result.key_points:
        sections += ["KEY POINTS", thin]
        sections += [f"  • {p}" for p in result.key_points]
        sections.append("")

    if config.include_action_items and result.action_items:
        sections += ["ACTION ITEMS", thin]
        sections += [f"  ☐  {a}" for a in result.action_items]
        sections.append("")

    if config.include_speech_analytics and result.speaker_stats:
        sections += ["SPEECH ANALYTICS", thin]
        for s in result.speaker_stats:
            wpm_str = f"{s.words_per_minute} WPM" if s.words_per_minute else "N/A"
            pct_str = f"{s.speaking_percentage}% of conversation" if s.speaking_percentage else ""
            sections.append(f"  {s.name}")
            sections.append(f"    Speech rate : {wpm_str} — {s.pace_label}  {pct_str}")
            if s.accent_indicators:
                sections.append(f"    Accent      : {s.accent_indicators}")
        sections.append("")

    if config.include_speaker_profiles and result.speaker_profiles:
        sections += ["SPEAKER PROFILES", thin]
        for name, profile in result.speaker_profiles.items():
            sections += [f"  {name}", f"  {profile}", ""]

    if config.include_interview_mode and result.interview_questions:
        verdict_icon = {"strong": "✅", "acceptable": "🟡", "weak": "⚠️", "missed": "❌"}
        sections += [divider, "INTERVIEW ANALYSIS", divider]

        if result.round_advance_probability >= 0:
            prob = result.round_advance_probability
            if prob >= 80:
                prob_label = "Strong"
            elif prob >= 60:
                prob_label = "Competitive"
            elif prob >= 40:
                prob_label = "Borderline"
            else:
                prob_label = "Unlikely"
            sections.append(f"  Likelihood of advancing : {prob}% — {prob_label}")
            sections.append("")

        for i, q in enumerate(result.interview_questions, 1):
            icon = verdict_icon.get(q.get("verdict", "").lower(), "•")
            sections.append(f"Q{i}: {q.get('question', '')}")
            sections.append(f"  Verdict : {icon} {q.get('verdict', '').upper()}")
            if q.get("deflection_detected"):
                sections.append(f"  ⚡ Deflection : {q.get('deflection_note', 'Candidate deflected or stalled')}")
            if q.get("your_answer_summary"):
                sections.append(f"  Your answer : {q['your_answer_summary']}")
            if q.get("ideal_answer"):
                sections.append(f"  What you could have said : {q['ideal_answer']}")
            if q.get("feedback"):
                sections.append(f"  Coaching : {q['feedback']}")
            sections.append("")

        if config.include_interview_deep and result.prep_guide:
            sections += [divider, "PREP GUIDE — Questions to Practise", divider]
            for i, g in enumerate(result.prep_guide, 1):
                sections.append(f"P{i}: {g.get('question', '')}")
                if g.get("why_it_matters"):
                    sections.append(f"  Why they ask it : {g['why_it_matters']}")
                if g.get("suggested_answer"):
                    sections.append(f"  Suggested answer : {g['suggested_answer']}")
                sections.append("")

    if config.include_transcript:
        sections += [divider, "FULL TRANSCRIPT", divider, result.clean_transcript, ""]

    return "\n".join(sections)


# ── save outputs ──────────────────────────────────────────────────────────────

def save_results(result: TranscriptResult, config: ReportConfig, output_dir: str, stem: str) -> dict:
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)

    paths = {}
    paths["transcript"] = str(out / f"{stem}_transcript.txt")
    paths["speakers"]   = str(out / f"{stem}_speakers.txt")
    paths["combined"]   = str(out / f"{stem}_combined.txt")
    paths["report"]     = str(out / f"{stem}_report.md")
    paths["json"]       = str(out / f"{stem}_full.json")

    Path(paths["transcript"]).write_text(result.clean_transcript, encoding="utf-8")
    Path(paths["speakers"]).write_text(result.speaker_dialogue, encoding="utf-8")
    Path(paths["combined"]).write_text(build_combined_report(result, config), encoding="utf-8")

    report_lines = [f"# {stem}", ""]
    if result.detected_language:
        report_lines += [f"**Language:** {result.detected_language}", ""]
    report_lines += ["## Summary", result.summary, ""]
    if result.key_points:
        report_lines += ["## Key Points", *[f"- {p}" for p in result.key_points], ""]
    if result.action_items:
        report_lines += ["## Action Items", *[f"- [ ] {a}" for a in result.action_items], ""]
    if result.speaker_stats:
        report_lines += ["## Speech Analytics", ""]
        for s in result.speaker_stats:
            report_lines += [
                f"### {s.name}",
                f"- **Speech rate:** {s.words_per_minute} WPM — {s.pace_label}",
                f"- **Speaking time:** {s.speaking_percentage}% of conversation",
                f"- **Accent:** {s.accent_indicators or 'N/A'}",
                "",
            ]
    if result.speaker_profiles:
        report_lines += ["## Speaker Profiles", ""]
        for name, profile in result.speaker_profiles.items():
            report_lines += [f"### {name}", profile, ""]

    Path(paths["report"]).write_text("\n".join(report_lines), encoding="utf-8")

    with open(paths["json"], "w", encoding="utf-8") as f:
        json.dump(result.__dict__, f, indent=2, ensure_ascii=False, default=str)

    return paths


# ── main entry point ──────────────────────────────────────────────────────────

def run(
    file_path: str,
    output_dir: str = "transcript_output",
    whisper_model: str = "base",
    panel_mode: bool = False,
    num_speakers: int = None,
    config: ReportConfig = None,
    api_key: str = None,
    provider: str = "anthropic",    # "anthropic" | "openai" | "openai_compat"
    model: str = None,              # defaults per-provider if None
    base_url: str = None,           # base URL for openai_compat providers
    language: str = None,           # ISO-639-1 code e.g. "es", "en", or None for auto
    language_variant: str = None,   # e.g. "Colombian Spanish (es-CO)"
    speaker_names: str = None,      # e.g. "Jay Pendley, John Smith (Interviewer)"
    on_whisper_progress=None,       # callable(pct: float) — live Whisper % updates
    on_raw_transcript=None,         # callable(text: str) — fired the moment Whisper finishes
    on_stage_change=None,           # callable(stage: str) — "extracting" | "whisper" | "claude"
    on_log=None,                    # callable(msg: str) — human-readable step log
    checkpoint_text: str = None,    # pre-saved Whisper text (skips transcription step)
    checkpoint_json: str = None,    # pre-saved WhisperX JSON string
    on_whisper_done=None,           # callable(text, json_str) — fired after Whisper to save checkpoint
    stt_provider: str = "whisper",  # "whisper"|"deepgram"|"assemblyai"|"groq_whisper"|"openai_whisper"|"google_stt"|"elevenlabs"|"rev_ai"
    deepgram_api_key: str = None,   # Deepgram API key (backward compat)
    deepgram_model: str = "nova-2", # Deepgram model (backward compat)
    stt_api_key: str = None,        # API key for any cloud STT provider
    stt_model: str = None,          # Model name for any cloud STT provider
) -> TranscriptResult:
    def _log(m):
        _safe_print(f"  {m}")
        if on_log: on_log(m)

    config = config or ReportConfig()
    print(f"\nTranscript Agent {'(Panel)' if panel_mode else ''}")
    _safe_print("=" * 50)

    fname = Path(file_path).name
    ext   = Path(file_path).suffix.lower()
    _log(f"File: {fname}")
    if language:
        _log(f"Language: {language_variant or language}")

    raw_whisperx = {}
    _detected_lang = ""

    _CLOUD_STT = {"deepgram", "assemblyai", "groq_whisper", "openai_whisper", "google_stt", "elevenlabs", "rev_ai"}
    _use_cloud_stt = stt_provider in _CLOUD_STT and ext in (AUDIO_EXTS | VIDEO_EXTS)
    # Resolve generic key/model (stt_api_key overrides legacy deepgram_api_key for new providers)
    _cloud_key = stt_api_key or deepgram_api_key
    _cloud_model = stt_model or deepgram_model or ""

    if checkpoint_text:
        # Resume: skip transcription, use the saved transcript
        raw_text = checkpoint_text
        if checkpoint_json:
            import json as _json
            try:
                raw_whisperx = _json.loads(checkpoint_json)
                _detected_lang = raw_whisperx.get("language", "")
            except Exception:
                raw_whisperx = {}
        fmt = "panel audio/video (diarized)" if panel_mode else "audio/video"
        _log(f"Resumed from checkpoint: ~{len(raw_text.split()):,} words (transcription skipped)")
    elif _use_cloud_stt:
        if not _cloud_key:
            raise ValueError(f"API key is required for {stt_provider} transcription.")
        lang_arg = language if language and language != "auto" else None
        _log(f"Mode: {'Panel diarized' if panel_mode else 'Standard'} | Transcription: {stt_provider} {_cloud_model}")
        if on_stage_change: on_stage_change("whisper")
        if stt_provider == "deepgram":
            raw_text, _detected_lang = transcribe_with_deepgram(
                file_path, _cloud_key,
                model=_cloud_model or "nova-2",
                language=lang_arg, diarize=panel_mode, on_log=on_log,
            )
        elif stt_provider == "assemblyai":
            raw_text, _detected_lang = transcribe_with_assemblyai(
                file_path, _cloud_key,
                model=_cloud_model or "best",
                language=lang_arg, diarize=panel_mode, on_log=on_log,
            )
        elif stt_provider == "groq_whisper":
            raw_text, _detected_lang = transcribe_with_groq_whisper(
                file_path, _cloud_key,
                model=_cloud_model or "whisper-large-v3-turbo",
                language=lang_arg, on_log=on_log,
            )
        elif stt_provider == "openai_whisper":
            raw_text, _detected_lang = transcribe_with_openai_whisper_api(
                file_path, _cloud_key,
                model=_cloud_model or "whisper-1",
                language=lang_arg, on_log=on_log,
            )
        elif stt_provider == "google_stt":
            raw_text, _detected_lang = transcribe_with_google_stt(
                file_path, _cloud_key,
                model=_cloud_model or "latest_long",
                language=lang_arg, on_log=on_log,
            )
        elif stt_provider == "elevenlabs":
            raw_text, _detected_lang = transcribe_with_elevenlabs(
                file_path, _cloud_key,
                model=_cloud_model or "scribe_v1",
                language=lang_arg, on_log=on_log,
            )
        elif stt_provider == "rev_ai":
            raw_text, _detected_lang = transcribe_with_rev_ai(
                file_path, _cloud_key,
                model=_cloud_model or "machine",
                language=lang_arg, on_log=on_log,
            )
        else:
            raise ValueError(f"Unknown STT provider: {stt_provider}")
        fmt = "panel audio/video (diarized)" if panel_mode else "audio/video"
        if on_whisper_done:
            try:
                on_whisper_done(raw_text, "")
            except Exception:
                pass
    elif panel_mode and ext in (AUDIO_EXTS | VIDEO_EXTS):
        _log("Mode: Panel (multi-speaker diarization)")
        if on_stage_change: on_stage_change("extracting")
        raw_text, raw_whisperx = load_audio_video_panel(
            file_path, whisper_model, num_speakers, language=language, on_log=on_log
        )
        _detected_lang = raw_whisperx.get("language", "")
        fmt = "panel audio/video (diarized)"
        if on_whisper_done:
            import json as _json
            try:
                on_whisper_done(raw_text, _json.dumps(raw_whisperx))
            except Exception:
                pass
    elif ext in (AUDIO_EXTS | VIDEO_EXTS):
        _log(f"Mode: Standard audio/video  |  Whisper model: {whisper_model}")
        if on_stage_change: on_stage_change("extracting")
        raw_text, _detected_lang = load_audio_video(
            file_path, whisper_model,
            on_progress=on_whisper_progress,
            on_stage_change=on_stage_change,
            language=language,
            on_log=on_log,
        )
        fmt = "audio/video"
        if on_whisper_done:
            try:
                on_whisper_done(raw_text, "")
            except Exception:
                pass
    else:
        _log(f"Mode: Document  ({ext or 'text'})")
        raw_text, fmt = load_file(file_path, whisper_model)
        _log(f"Document loaded: ~{len(raw_text.split()):,} words")

    # fire immediately so the UI shows the raw transcript before Claude starts
    if on_raw_transcript:
        on_raw_transcript(raw_text)

    _log(f"Text ready: ~{len(raw_text.split()):,} words  |  Passing to AI…")

    if on_stage_change: on_stage_change("claude")
    _model = model or ("claude-opus-4-7" if provider == "anthropic" else "gpt-4o")
    client = LLMClient(provider=provider, api_key=api_key, model=_model, base_url=base_url)
    if speaker_names:
        _log(f"Speaker count provided: {speaker_names}")
    result = process_transcript(
        client, raw_text, fmt,
        panel_mode=panel_mode,
        num_speakers=num_speakers,
        config=config,
        raw_whisperx=raw_whisperx,
        language=language,
        language_variant=language_variant,
        speaker_names=speaker_names,
        on_log=on_log,
    )

    # Resolve display language: prefer user-specified variant label, then ISO code,
    # then Whisper-detected code, fall back to "Auto-detected".
    result.detected_language = language_variant or language or _detected_lang or "Auto-detected"

    paths = save_results(result, config, output_dir, Path(file_path).stem)
    _log(f"Outputs saved to: {output_dir}/")

    print(f"\n✓ Done! Outputs saved to: {output_dir}/")
    print(f"\n{'─'*50}\nSUMMARY\n{'─'*50}")
    _safe_print(result.summary)
    if result.key_points:
        print(f"\nKEY POINTS (first 5 of {len(result.key_points)})")
        for p in result.key_points[:5]:
            _safe_print(f"  • {p}")

    return result


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Transcript Agent")
    parser.add_argument("file", help="Input file path")
    parser.add_argument("--output", default="transcript_output")
    parser.add_argument("--whisper", default="base",
                        choices=["tiny", "base", "small", "medium", "large"])
    parser.add_argument("--panel", action="store_true")
    parser.add_argument("--speakers", type=int, default=None)
    parser.add_argument("--style", default="formal",
                        choices=["formal", "casual", "executive", "bullet"])
    args = parser.parse_args()

    run(args.file, args.output, args.whisper, args.panel, args.speakers,
        ReportConfig(style=args.style))
