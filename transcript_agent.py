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

# ensure stdout/stderr use UTF-8 on Windows so emoji/special chars don't crash prints
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
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
            kw = {}
            if api_key:
                kw["api_key"] = api_key
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
            resp = self._client.chat.completions.create(
                model=self.model,
                max_tokens=max_tokens,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
            )
            return resp.choices[0].message.content or ""


# ── resolve bundled ffmpeg (works on Windows without a system ffmpeg install) ──
import subprocess as _sp
import numpy as _np

try:
    import imageio_ffmpeg as _iff
    FFMPEG_EXE = _iff.get_ffmpeg_exe()   # full path to bundled binary
except ImportError:
    FFMPEG_EXE = "ffmpeg"                 # fall back to system ffmpeg

# patch Whisper's internal audio loader to use the resolved binary
try:
    import whisper.audio as _wa

    def _load_audio(file: str, sr: int = _wa.SAMPLE_RATE) -> _np.ndarray:
        cmd = [FFMPEG_EXE, "-nostdin", "-threads", "0", "-i", file,
               "-f", "s16le", "-ac", "1", "-acodec", "pcm_s16le", "-ar", str(sr), "-"]
        out = _sp.run(cmd, capture_output=True, check=True).stdout
        return _np.frombuffer(out, _np.int16).flatten().astype(_np.float32) / 32768.0

    _wa.load_audio = _load_audio
except Exception:
    pass

# ── live Whisper progress: patch tqdm so we can read % in real-time ───────────
import threading as _threading
_progress_lock  = _threading.Lock()
_progress_cb    = None   # set to a callable(float) before each transcribe call

try:
    import tqdm as _tqdm_mod
    import whisper.transcribe as _wt

    class _TrackingTqdm(_tqdm_mod.tqdm):
        def update(self, n=1):
            super().update(n)
            with _progress_lock:
                cb = _progress_cb
            if cb and self.total:
                cb(min(self.n / self.total, 1.0))

    _wt.tqdm = _TrackingTqdm
except Exception:
    pass


# ── optional dependency imports ───────────────────────────────────────────────

try:
    import whisper as openai_whisper
    WHISPER_AVAILABLE = True
except ImportError:
    WHISPER_AVAILABLE = False

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
        print(f"  {m}")
        if on_log: on_log(m)

    global _progress_cb
    if not WHISPER_AVAILABLE:
        raise ImportError("Run: pip install openai-whisper")

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
        print(f"  {m}")
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
        print("  Extracting audio from video...")
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
    print(f"  Format detected: {ext or 'text'}")

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


def build_panel_prompt(content, fmt, num_speakers, style, speech_data, language=None, language_variant=None):
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

    return f"""\
Format: {fmt}
{speakers_hint}
Style: {style_note}
{speech_section}{lang_section}
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
  "action_items": ["action 1", ...]
}}

For accent_indicators: analyze vocabulary, syntax, idiomatic expressions, and regional phrases.
Always state confidence level (low/medium/high)."""


def build_standard_prompt(content, fmt, style, overall_stats, language=None, language_variant=None):
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

    return f"""\
Format: {fmt}
Style: {style_note}
{stats_note}{lang_note}

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
  "action_items": []
}}"""


# ── processing ────────────────────────────────────────────────────────────────

def _parse_json(raw: str) -> dict:
    raw = re.sub(r"^```(?:json)?\s*", "", raw.strip())
    raw = re.sub(r"\s*```$", "", raw)
    return json.loads(raw)


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
    print("  Merging summaries...")
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
    on_log=None,
) -> TranscriptResult:
    def _log(m):
        print(f"  {m}")
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
            prompt = build_panel_prompt(chunk, fmt, num_speakers, config.style, audio_speech_data, language, language_variant)
        else:
            sys_prompt = STANDARD_SYSTEM_PROMPT
            prompt = build_standard_prompt(chunk, fmt, config.style, overall_text_stats, language, language_variant)

        if n > 1:
            prompt = f"[Part {i} of {n}]\n\n" + prompt

        raw = client.chat(
            system=sys_prompt,
            user=prompt,
            max_tokens=16000,
            thinking=(client.provider == "anthropic"),
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
    on_whisper_progress=None,       # callable(pct: float) — live Whisper % updates
    on_raw_transcript=None,         # callable(text: str) — fired the moment Whisper finishes
    on_stage_change=None,           # callable(stage: str) — "extracting" | "whisper" | "claude"
    on_log=None,                    # callable(msg: str) — human-readable step log
) -> TranscriptResult:
    def _log(m):
        print(f"  {m}")
        if on_log: on_log(m)

    config = config or ReportConfig()
    print(f"\nTranscript Agent {'(Panel)' if panel_mode else ''}")
    print("=" * 50)

    fname = Path(file_path).name
    ext   = Path(file_path).suffix.lower()
    _log(f"File: {fname}")
    if language:
        _log(f"Language: {language_variant or language}")

    raw_whisperx = {}
    _detected_lang = ""

    if panel_mode and ext in (AUDIO_EXTS | VIDEO_EXTS):
        _log("Mode: Panel (multi-speaker diarization)")
        if on_stage_change: on_stage_change("extracting")
        raw_text, raw_whisperx = load_audio_video_panel(
            file_path, whisper_model, num_speakers, language=language, on_log=on_log
        )
        _detected_lang = raw_whisperx.get("language", "")
        fmt = "panel audio/video (diarized)"
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
    result = process_transcript(
        client, raw_text, fmt,
        panel_mode=panel_mode,
        num_speakers=num_speakers,
        config=config,
        raw_whisperx=raw_whisperx,
        language=language,
        language_variant=language_variant,
        on_log=on_log,
    )

    # Resolve display language: prefer user-specified variant label, then ISO code,
    # then Whisper-detected code, fall back to "Auto-detected".
    result.detected_language = language_variant or language or _detected_lang or "Auto-detected"

    paths = save_results(result, config, output_dir, Path(file_path).stem)
    _log(f"Outputs saved to: {output_dir}/")

    print(f"\n✓ Done! Outputs saved to: {output_dir}/")
    print(f"\n{'─'*50}\nSUMMARY\n{'─'*50}")
    print(result.summary)
    if result.key_points:
        print(f"\nKEY POINTS (first 5 of {len(result.key_points)})")
        for p in result.key_points[:5]:
            print(f"  • {p}")

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
