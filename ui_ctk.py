"""
Transcript Agent — customtkinter native desktop UI
Replaces the Gradio/pywebview stack with a true Windows-native window.
"""
from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from tkinter import filedialog, messagebox

import customtkinter as ctk

# ── Paths ──────────────────────────────────────────────────────────────────────
_home         = Path.home()
_app_dir      = _home / ".transcript_agent"
_app_dir.mkdir(exist_ok=True)
OUT_DIR       = Path(os.environ.get("TRANSCRIPT_OUTPUT_DIR",
                                    str(_home / "TranscriptAgent" / "outputs")))
OUT_DIR.mkdir(parents=True, exist_ok=True)
SETTINGS_FILE = _app_dir / "settings.json"

# ── Theme ──────────────────────────────────────────────────────────────────────
ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")

C_BG      = "#0f172a"
C_PANEL   = "#1e293b"
C_CARD    = "#1e3a5f"
C_ACCENT  = "#3b82f6"
C_TEXT    = "#f1f5f9"
C_SUB     = "#94a3b8"
C_SUCCESS = "#22d3ee"
C_ERROR   = "#f87171"
C_WARN    = "#fbbf24"

# ── Provider data (mirrors app.py) ─────────────────────────────────────────────
_PROVIDERS = {
    "Claude (Anthropic)": {
        "type": "anthropic", "placeholder": "sk-ant-api03-…",
        "models": ["claude-opus-4-7", "claude-sonnet-4-6", "claude-haiku-4-5-20251001",
                   "claude-3-5-sonnet-20241022", "claude-3-5-haiku-20241022", "claude-3-opus-20240229"],
        "base_url": None,
    },
    "OpenAI": {
        "type": "openai", "placeholder": "sk-…",
        "models": ["gpt-4.1", "gpt-4.1-mini", "gpt-4o", "gpt-4o-mini",
                   "o3", "o3-mini", "o1", "gpt-4-turbo", "gpt-3.5-turbo"],
        "base_url": None,
    },
    "Google Gemini": {
        "type": "openai_compat", "placeholder": "AIzaSy…",
        "models": ["gemini-2.5-pro", "gemini-2.5-flash", "gemini-2.0-flash-exp",
                   "gemini-1.5-pro", "gemini-1.5-flash"],
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai/",
    },
    "Groq": {
        "type": "openai_compat", "placeholder": "gsk_…",
        "models": ["llama-3.3-70b-versatile", "llama-3.1-8b-instant", "mixtral-8x7b-32768"],
        "base_url": "https://api.groq.com/openai/v1",
    },
    "Mistral": {
        "type": "openai_compat", "placeholder": "…",
        "models": ["mistral-large-latest", "mistral-small-latest", "open-mixtral-8x7b"],
        "base_url": "https://api.mistral.ai/v1",
    },
    "Ollama (Local)": {
        "type": "openai_compat", "placeholder": "(no key needed)",
        "models": ["llama3.2", "llama3.1", "mistral", "phi3", "gemma2"],
        "base_url": "http://localhost:11434/v1",
    },
    "Custom (OpenAI-compat)": {
        "type": "openai_compat", "placeholder": "API key",
        "models": [],
        "base_url": "",
    },
}

_STT_PROVIDERS = {
    "Whisper (Local)": {
        "id": "whisper", "cloud": False,
        "models": ["tiny", "base", "small", "medium", "large"],
        "default": "base", "placeholder": None,
    },
    "Deepgram (Cloud)": {
        "id": "deepgram", "cloud": True,
        "models": ["nova-2", "nova-2-general", "nova-2-meeting", "nova", "enhanced", "base"],
        "default": "nova-2", "placeholder": "dg-…",
    },
    "AssemblyAI (Cloud)": {
        "id": "assemblyai", "cloud": True,
        "models": ["best", "nano"],
        "default": "best", "placeholder": "your_assemblyai_key",
    },
    "Groq Whisper (Cloud)": {
        "id": "groq_whisper", "cloud": True,
        "models": ["whisper-large-v3-turbo", "whisper-large-v3", "distil-whisper-large-v3-en"],
        "default": "whisper-large-v3-turbo", "placeholder": "gsk_…",
    },
    "OpenAI Whisper API (Cloud)": {
        "id": "openai_whisper", "cloud": True,
        "models": ["whisper-1", "gpt-4o-transcribe", "gpt-4o-mini-transcribe"],
        "default": "whisper-1", "placeholder": "sk-…",
    },
    "Google Cloud STT (Cloud)": {
        "id": "google_stt", "cloud": True,
        "models": ["latest_long", "latest_short", "command_and_search"],
        "default": "latest_long", "placeholder": "AIza…",
    },
    "Azure Speech (Cloud)": {
        "id": "azure_stt", "cloud": True,
        "models": ["conversation", "dictation", "command_and_search"],
        "default": "conversation", "placeholder": "KEY|region",
    },
    "ElevenLabs (Cloud)": {
        "id": "elevenlabs", "cloud": True,
        "models": ["scribe_v1"],
        "default": "scribe_v1", "placeholder": "sk_…",
    },
    "Rev.ai (Cloud)": {
        "id": "rev_ai", "cloud": True,
        "models": ["machine", "fusion"],
        "default": "machine", "placeholder": "your_rev_ai_token",
    },
}

_LANGUAGES = [
    "Auto-detect", "English", "Spanish", "French", "German", "Portuguese",
    "Italian", "Dutch", "Russian", "Chinese", "Japanese", "Korean",
    "Arabic", "Hindi", "Turkish", "Polish", "Swedish", "Norwegian",
]

# ── Settings helpers ───────────────────────────────────────────────────────────

def _load_settings() -> dict:
    try:
        return json.loads(SETTINGS_FILE.read_text())
    except Exception:
        return {}


def _save_settings(d: dict):
    try:
        SETTINGS_FILE.write_text(json.dumps(d, indent=2))
    except Exception:
        pass


# ── Scrollable log widget ──────────────────────────────────────────────────────

class LogBox(ctk.CTkTextbox):
    def append(self, msg: str, color: str = C_TEXT):
        ts = time.strftime("%H:%M:%S")
        self.configure(state="normal")
        self.insert("end", f"[{ts}]  {msg}\n")
        self.configure(state="disabled")
        self.see("end")

    def clear(self):
        self.configure(state="normal")
        self.delete("1.0", "end")
        self.configure(state="disabled")


# ── Main application ───────────────────────────────────────────────────────────

class TranscriptAgentApp(ctk.CTk):

    def __init__(self):
        super().__init__()
        self.title("Transcript Agent")
        self.geometry("1340x860")
        self.minsize(1000, 640)
        self.configure(fg_color=C_BG)

        # Set window icon if available
        try:
            if sys.platform == "win32":
                from PIL import Image, ImageTk
                _ico = _app_dir / "icon.ico"
                if _ico.exists():
                    self.iconbitmap(str(_ico))
        except Exception:
            pass

        self._settings  = _load_settings()
        self._job_dir: Path | None = None
        self._cancel_ev = threading.Event()
        self._running   = False

        self._build_ui()
        self._apply_settings()

        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ── Layout ─────────────────────────────────────────────────────────────────

    def _build_ui(self):
        self.grid_columnconfigure(0, weight=0, minsize=340)
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)

        # ── Left panel ──────────────────────────────────────────────────────────
        left = ctk.CTkScrollableFrame(self, width=330, fg_color=C_PANEL,
                                      corner_radius=0)
        left.grid(row=0, column=0, sticky="nsew")
        left.grid_columnconfigure(0, weight=1)
        self._build_left(left)

        # ── Right panel ─────────────────────────────────────────────────────────
        right = ctk.CTkFrame(self, fg_color=C_BG, corner_radius=0)
        right.grid(row=0, column=1, sticky="nsew", padx=(2, 0))
        right.grid_columnconfigure(0, weight=1)
        right.grid_rowconfigure(1, weight=1)
        self._build_right(right)

    def _section(self, parent, text: str) -> ctk.CTkFrame:
        ctk.CTkLabel(parent, text=text.upper(), font=ctk.CTkFont(size=10, weight="bold"),
                     text_color=C_SUB).pack(anchor="w", padx=14, pady=(14, 2))
        f = ctk.CTkFrame(parent, fg_color=C_CARD, corner_radius=8)
        f.pack(fill="x", padx=10, pady=(0, 4))
        return f

    # ── Left panel sections ────────────────────────────────────────────────────

    def _build_left(self, p):
        # Header
        hdr = ctk.CTkFrame(p, fg_color=C_CARD, corner_radius=8)
        hdr.pack(fill="x", padx=10, pady=(14, 4))
        ctk.CTkLabel(hdr, text="🎙  Transcript Agent",
                     font=ctk.CTkFont(size=16, weight="bold"),
                     text_color=C_TEXT).pack(pady=10)

        # ── Input section ──────────────────────────────────────────────────────
        f = self._section(p, "📁  Input")
        ctk.CTkLabel(f, text="File or URL", text_color=C_SUB,
                     font=ctk.CTkFont(size=11)).pack(anchor="w", padx=10, pady=(8, 2))

        row = ctk.CTkFrame(f, fg_color="transparent")
        row.pack(fill="x", padx=10, pady=(0, 4))
        row.grid_columnconfigure(0, weight=1)

        self._file_var = ctk.StringVar()
        self._file_entry = ctk.CTkEntry(row, textvariable=self._file_var,
                                        placeholder_text="Drop file here or click Browse…",
                                        height=34)
        self._file_entry.grid(row=0, column=0, sticky="ew", padx=(0, 4))
        ctk.CTkButton(row, text="Browse", width=64, height=34,
                      command=self._browse_file).grid(row=0, column=1)

        # ── Transcription engine ───────────────────────────────────────────────
        f2 = self._section(p, "🎤  Transcription Engine")
        ctk.CTkLabel(f2, text="STT Engine", text_color=C_SUB,
                     font=ctk.CTkFont(size=11)).pack(anchor="w", padx=10, pady=(8, 2))
        self._stt_var = ctk.StringVar(value="Whisper (Local)")
        self._stt_menu = ctk.CTkOptionMenu(f2, variable=self._stt_var,
                                           values=list(_STT_PROVIDERS),
                                           command=self._on_stt_change, height=32)
        self._stt_menu.pack(fill="x", padx=10, pady=(0, 6))

        ctk.CTkLabel(f2, text="Model", text_color=C_SUB,
                     font=ctk.CTkFont(size=11)).pack(anchor="w", padx=10, pady=(0, 2))
        self._stt_model_var = ctk.StringVar(value="base")
        self._stt_model_menu = ctk.CTkOptionMenu(f2, variable=self._stt_model_var,
                                                  values=["base"], height=32)
        self._stt_model_menu.pack(fill="x", padx=10, pady=(0, 6))

        self._stt_key_label = ctk.CTkLabel(f2, text="API Key", text_color=C_SUB,
                                            font=ctk.CTkFont(size=11))
        self._stt_key_var = ctk.StringVar()
        self._stt_key_entry = ctk.CTkEntry(f2, textvariable=self._stt_key_var,
                                            show="•", height=32,
                                            placeholder_text="Cloud key…")

        # ── AI Provider ────────────────────────────────────────────────────────
        f3 = self._section(p, "🤖  AI Analysis")
        ctk.CTkLabel(f3, text="Provider", text_color=C_SUB,
                     font=ctk.CTkFont(size=11)).pack(anchor="w", padx=10, pady=(8, 2))
        self._prov_var = ctk.StringVar(value="Claude (Anthropic)")
        self._prov_menu = ctk.CTkOptionMenu(f3, variable=self._prov_var,
                                            values=list(_PROVIDERS),
                                            command=self._on_provider_change, height=32)
        self._prov_menu.pack(fill="x", padx=10, pady=(0, 6))

        ctk.CTkLabel(f3, text="Model", text_color=C_SUB,
                     font=ctk.CTkFont(size=11)).pack(anchor="w", padx=10, pady=(0, 2))
        first_models = _PROVIDERS["Claude (Anthropic)"]["models"]
        self._model_var = ctk.StringVar(value=first_models[1])  # sonnet by default
        self._model_menu = ctk.CTkOptionMenu(f3, variable=self._model_var,
                                              values=first_models, height=32)
        self._model_menu.pack(fill="x", padx=10, pady=(0, 6))

        ctk.CTkLabel(f3, text="API Key", text_color=C_SUB,
                     font=ctk.CTkFont(size=11)).pack(anchor="w", padx=10, pady=(0, 2))
        self._api_key_var = ctk.StringVar()
        self._api_key_entry = ctk.CTkEntry(f3, textvariable=self._api_key_var,
                                            show="•", height=32,
                                            placeholder_text="sk-ant-api03-…")
        self._api_key_entry.pack(fill="x", padx=10, pady=(0, 4))

        self._base_url_label = ctk.CTkLabel(f3, text="Base URL", text_color=C_SUB,
                                             font=ctk.CTkFont(size=11))
        self._base_url_var = ctk.StringVar()
        self._base_url_entry = ctk.CTkEntry(f3, textvariable=self._base_url_var,
                                             height=32, placeholder_text="https://…")

        # ── Processing options ─────────────────────────────────────────────────
        f4 = self._section(p, "⚙️  Options")
        self._interview_var = ctk.BooleanVar(value=True)
        ctk.CTkCheckBox(f4, text="Interview Analysis", variable=self._interview_var,
                        text_color=C_TEXT).pack(anchor="w", padx=10, pady=(8, 2))

        self._deep_var = ctk.BooleanVar(value=True)
        ctk.CTkCheckBox(f4, text="Deep Analysis", variable=self._deep_var,
                        text_color=C_TEXT).pack(anchor="w", padx=10, pady=(0, 2))

        self._profiles_var = ctk.BooleanVar(value=True)
        ctk.CTkCheckBox(f4, text="Speaker Profiles", variable=self._profiles_var,
                        text_color=C_TEXT).pack(anchor="w", padx=10, pady=(0, 2))

        ctk.CTkLabel(f4, text="Language", text_color=C_SUB,
                     font=ctk.CTkFont(size=11)).pack(anchor="w", padx=10, pady=(6, 2))
        self._lang_var = ctk.StringVar(value="Auto-detect")
        ctk.CTkOptionMenu(f4, variable=self._lang_var, values=_LANGUAGES,
                          height=32).pack(fill="x", padx=10, pady=(0, 6))

        ctk.CTkLabel(f4, text="Speaker Names (optional, comma-separated)",
                     text_color=C_SUB, font=ctk.CTkFont(size=11)).pack(anchor="w",
                                                                         padx=10, pady=(0, 2))
        self._speaker_var = ctk.StringVar()
        ctk.CTkEntry(f4, textvariable=self._speaker_var, height=32,
                     placeholder_text="Alice, Bob, Charlie…").pack(fill="x", padx=10,
                                                                    pady=(0, 8))

        # ── Action buttons ─────────────────────────────────────────────────────
        fb = ctk.CTkFrame(p, fg_color="transparent")
        fb.pack(fill="x", padx=10, pady=8)
        fb.grid_columnconfigure((0, 1), weight=1)

        self._run_btn = ctk.CTkButton(fb, text="▶  Analyze File",
                                      fg_color=C_ACCENT, hover_color="#2563eb",
                                      font=ctk.CTkFont(size=13, weight="bold"),
                                      height=40, command=self._start_job)
        self._run_btn.grid(row=0, column=0, sticky="ew", padx=(0, 4))

        self._cancel_btn = ctk.CTkButton(fb, text="✕  Cancel",
                                          fg_color="#7f1d1d", hover_color="#991b1b",
                                          height=40, state="disabled",
                                          command=self._cancel_job)
        self._cancel_btn.grid(row=0, column=1, sticky="ew")

        # ── Downloads ──────────────────────────────────────────────────────────
        fd = self._section(p, "📥  Output Files")
        self._dl_frame = fd

        self._dl_btns: dict[str, ctk.CTkButton] = {}
        dl_specs = [
            ("PDF",        "📄"),
            ("DOCX",       "📝"),
            ("Transcript", "📃"),
            ("Speakers",   "👥"),
            ("Markdown",   "📋"),
            ("Combined",   "📄"),
            ("SRT",        "🎬"),
            ("VTT",        "🎬"),
            ("JSON",       "📊"),
        ]
        rows_frame = ctk.CTkFrame(fd, fg_color="transparent")
        rows_frame.pack(fill="x", padx=8, pady=6)
        for i, (label, icon) in enumerate(dl_specs):
            r, c = divmod(i, 3)
            btn = ctk.CTkButton(rows_frame, text=f"{icon} {label}",
                                width=90, height=28, state="disabled",
                                fg_color="#1e3a5f", hover_color=C_ACCENT,
                                font=ctk.CTkFont(size=11),
                                command=lambda lbl=label: self._download(lbl))
            btn.grid(row=r, column=c, padx=3, pady=3, sticky="ew")
            rows_frame.grid_columnconfigure(c, weight=1)
            self._dl_btns[label] = btn

        self._open_folder_btn = ctk.CTkButton(
            fd, text="📁  Open Output Folder", height=32, state="disabled",
            fg_color=C_PANEL, hover_color=C_CARD, border_width=1,
            border_color=C_ACCENT,
            command=self._open_folder)
        self._open_folder_btn.pack(fill="x", padx=10, pady=(4, 10))

    # ── Right panel ────────────────────────────────────────────────────────────

    def _build_right(self, p):
        # Status bar
        self._status_var = ctk.StringVar(value="Ready")
        status_bar = ctk.CTkLabel(p, textvariable=self._status_var,
                                   fg_color=C_PANEL, corner_radius=6,
                                   font=ctk.CTkFont(size=12),
                                   text_color=C_TEXT, anchor="w")
        status_bar.pack(fill="x", padx=10, pady=(10, 4), ipady=6)

        # Tab view
        self._tabs = ctk.CTkTabview(p, fg_color=C_PANEL,
                                     segmented_button_fg_color=C_CARD,
                                     segmented_button_selected_color=C_ACCENT,
                                     segmented_button_selected_hover_color="#2563eb",
                                     text_color=C_TEXT)
        self._tabs.pack(fill="both", expand=True, padx=10, pady=(0, 10))

        for tab in ("Log", "Summary", "Transcript", "Speakers",
                    "Profiles", "Interview", "Analytics", "Combined",
                    "History", "Settings"):
            self._tabs.add(tab)

        self._build_log_tab()
        self._build_text_tabs()
        self._build_history_tab()
        self._build_settings_tab()

    def _build_log_tab(self):
        f = self._tabs.tab("Log")
        f.grid_columnconfigure(0, weight=1)
        f.grid_rowconfigure(0, weight=1)
        self._log = LogBox(f, state="disabled", font=ctk.CTkFont(family="Courier New", size=12),
                           fg_color="#0a0f1a", text_color=C_TEXT,
                           wrap="word", corner_radius=6)
        self._log.grid(row=0, column=0, sticky="nsew", padx=4, pady=4)

        # Progress bar
        self._progress = ctk.CTkProgressBar(f, height=6, fg_color=C_CARD,
                                             progress_color=C_ACCENT)
        self._progress.set(0)
        self._progress.grid(row=1, column=0, sticky="ew", padx=4, pady=(0, 4))

    def _build_text_tabs(self):
        self._text_boxes: dict[str, ctk.CTkTextbox] = {}
        for tab in ("Summary", "Transcript", "Speakers", "Profiles",
                    "Interview", "Analytics", "Combined"):
            f = self._tabs.tab(tab)
            f.grid_columnconfigure(0, weight=1)
            f.grid_rowconfigure(0, weight=1)
            tb = ctk.CTkTextbox(f, font=ctk.CTkFont(family="Courier New", size=12),
                                 fg_color="#0a0f1a", text_color=C_TEXT,
                                 wrap="word", corner_radius=6, state="disabled")
            tb.grid(row=0, column=0, sticky="nsew", padx=4, pady=4)

            # Copy button
            ctk.CTkButton(f, text="Copy", width=70, height=26,
                          fg_color=C_CARD, hover_color=C_ACCENT,
                          command=lambda t=tab: self._copy_tab(t)).grid(
                              row=1, column=0, sticky="e", padx=4, pady=(0, 4))
            self._text_boxes[tab] = tb

    def _build_history_tab(self):
        f = self._tabs.tab("History")
        f.grid_columnconfigure(0, weight=1)
        f.grid_rowconfigure(1, weight=1)

        ctk.CTkButton(f, text="🔄  Refresh", width=100, height=30,
                      command=self._load_history).grid(row=0, column=0,
                                                        sticky="w", padx=4, pady=4)
        self._history_box = ctk.CTkTextbox(f, font=ctk.CTkFont(family="Courier New", size=11),
                                            fg_color="#0a0f1a", text_color=C_TEXT,
                                            wrap="none", corner_radius=6, state="disabled")
        self._history_box.grid(row=1, column=0, sticky="nsew", padx=4, pady=(0, 4))
        self._load_history()

    def _build_settings_tab(self):
        f = self._tabs.tab("Settings")
        f.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(f, text="API keys are saved locally to ~/.transcript_agent/settings.json",
                     text_color=C_SUB, font=ctk.CTkFont(size=11)).pack(pady=(8, 12))

        self._settings_entries: dict[str, ctk.CTkEntry] = {}
        saved_keys = [
            ("anthropic_key",   "Anthropic API Key",   "sk-ant-api03-…"),
            ("openai_key",      "OpenAI API Key",       "sk-…"),
            ("deepgram_key",    "Deepgram API Key",     "dg-…"),
            ("assemblyai_key",  "AssemblyAI API Key",   "your_key"),
            ("groq_key",        "Groq API Key",         "gsk_…"),
            ("elevenlabs_key",  "ElevenLabs API Key",   "sk_…"),
        ]
        for key, label, ph in saved_keys:
            ctk.CTkLabel(f, text=label, text_color=C_SUB,
                         font=ctk.CTkFont(size=11)).pack(anchor="w", padx=20)
            e = ctk.CTkEntry(f, show="•", height=32, placeholder_text=ph)
            e.pack(fill="x", padx=20, pady=(2, 8))
            val = self._settings.get(key, "")
            if val:
                e.insert(0, val)
            self._settings_entries[key] = e

        ctk.CTkButton(f, text="💾  Save Settings", height=36,
                      fg_color=C_ACCENT, hover_color="#2563eb",
                      command=self._save_settings_ui).pack(padx=20, pady=8, fill="x")

    # ── Event handlers ─────────────────────────────────────────────────────────

    def _on_stt_change(self, val):
        stt = _STT_PROVIDERS[val]
        models = stt["models"]
        self._stt_model_menu.configure(values=models if models else ["—"])
        self._stt_model_var.set(stt["default"] if stt["default"] else (models[0] if models else ""))

        if stt["cloud"]:
            ph = stt.get("placeholder") or "API key"
            self._stt_key_entry.configure(placeholder_text=ph)
            self._stt_key_label.pack(anchor="w", padx=10, pady=(0, 2))
            self._stt_key_entry.pack(fill="x", padx=10, pady=(0, 8))
            # Auto-fill from saved settings
            saved_map = {
                "deepgram": "deepgram_key", "assemblyai": "assemblyai_key",
                "groq_whisper": "groq_key", "elevenlabs": "elevenlabs_key",
                "openai_whisper": "openai_key",
            }
            stt_id = stt["id"]
            if stt_id in saved_map:
                self._stt_key_var.set(self._settings.get(saved_map[stt_id], ""))
        else:
            self._stt_key_label.pack_forget()
            self._stt_key_entry.pack_forget()

    def _on_provider_change(self, val):
        prov = _PROVIDERS[val]
        models = prov["models"]
        self._model_menu.configure(values=models if models else ["custom-model"])
        self._model_var.set(models[1] if len(models) > 1 else (models[0] if models else ""))
        self._api_key_entry.configure(placeholder_text=prov["placeholder"])

        # Auto-fill saved key
        key_map = {
            "Claude (Anthropic)": "anthropic_key",
            "OpenAI": "openai_key",
            "Groq": "groq_key",
        }
        if val in key_map:
            self._api_key_var.set(self._settings.get(key_map[val], ""))
        else:
            self._api_key_var.set("")

        base_url = prov.get("base_url")
        if base_url is not None:
            self._base_url_var.set(base_url)
            self._base_url_label.pack(anchor="w", padx=10, pady=(0, 2))
            self._base_url_entry.pack(fill="x", padx=10, pady=(0, 8))
        else:
            self._base_url_label.pack_forget()
            self._base_url_entry.pack_forget()

    def _browse_file(self):
        path = filedialog.askopenfilename(
            title="Select audio/video/document file",
            filetypes=[
                ("All supported", "*.mp3 *.wav *.m4a *.ogg *.aac *.mp4 *.mov "
                 "*.avi *.mkv *.webm *.srt *.vtt *.pdf *.docx *.txt *.md"),
                ("Audio", "*.mp3 *.wav *.m4a *.ogg *.aac"),
                ("Video", "*.mp4 *.mov *.avi *.mkv *.webm"),
                ("Documents", "*.pdf *.docx *.txt *.md *.srt *.vtt"),
                ("All files", "*.*"),
            ],
        )
        if path:
            self._file_var.set(path)

    def _copy_tab(self, tab: str):
        tb = self._text_boxes.get(tab)
        if not tb:
            return
        text = tb.get("1.0", "end").strip()
        self.clipboard_clear()
        self.clipboard_append(text)

    def _download(self, label: str):
        if not self._job_dir:
            return
        ext_map = {
            "PDF": ".pdf", "DOCX": ".docx", "Transcript": "_transcript.txt",
            "Speakers": "_speakers.txt", "Markdown": "_report.md",
            "Combined": "_combined.txt", "SRT": "_transcript.srt",
            "VTT": "_transcript.vtt", "JSON": "_full.json",
        }
        suffix = ext_map.get(label, "")
        if not suffix:
            return
        # Find matching file in job_dir
        candidates = list(self._job_dir.glob(f"*{suffix}"))
        if not candidates:
            messagebox.showinfo("Not found", f"No {label} file found for this job.")
            return
        src = candidates[0]
        dest = filedialog.asksaveasfilename(
            defaultextension=suffix.lstrip("_"),
            initialfile=src.name,
            filetypes=[(label, f"*{suffix}"), ("All files", "*.*")],
        )
        if dest:
            import shutil
            shutil.copy2(src, dest)

    def _open_folder(self):
        if not self._job_dir or not self._job_dir.exists():
            return
        if sys.platform == "win32":
            os.startfile(str(self._job_dir))
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(self._job_dir)])
        else:
            subprocess.Popen(["xdg-open", str(self._job_dir)])

    def _save_settings_ui(self):
        d = {k: e.get() for k, e in self._settings_entries.items()}
        self._settings.update(d)
        _save_settings(self._settings)
        messagebox.showinfo("Saved", "Settings saved.")

    def _apply_settings(self):
        s = self._settings
        if s.get("provider"):
            self._prov_var.set(s["provider"])
            self._on_provider_change(s["provider"])
        if s.get("model"):
            self._model_var.set(s["model"])
        if s.get("api_key"):
            self._api_key_var.set(s["api_key"])
        if s.get("stt_engine"):
            self._stt_var.set(s["stt_engine"])
            self._on_stt_change(s["stt_engine"])
        # Trigger initial stt setup
        self._on_stt_change(self._stt_var.get())
        self._on_provider_change(self._prov_var.get())

    # ── Job execution ──────────────────────────────────────────────────────────

    def _start_job(self):
        file_path = self._file_var.get().strip()
        if not file_path:
            messagebox.showwarning("No input", "Please select a file or enter a URL.")
            return

        if self._running:
            return

        self._cancel_ev.clear()
        self._running = True
        self._run_btn.configure(state="disabled")
        self._cancel_btn.configure(state="normal")
        self._progress.set(0)
        self._log.clear()
        self._status_var.set("🔄  Processing…")
        self._tabs.set("Log")

        # Reset text boxes
        for tb in self._text_boxes.values():
            tb.configure(state="normal")
            tb.delete("1.0", "end")
            tb.configure(state="disabled")

        # Disable download buttons
        for btn in self._dl_btns.values():
            btn.configure(state="disabled")
        self._open_folder_btn.configure(state="disabled")

        # Build job dir
        stem = Path(file_path).stem[:40] if Path(file_path).exists() else "url_job"
        job_id = uuid.uuid4().hex[:8]
        self._job_dir = OUT_DIR / f"{stem}_{job_id}"
        self._job_dir.mkdir(parents=True, exist_ok=True)

        # Collect settings
        prov_name = self._prov_var.get()
        prov_info = _PROVIDERS[prov_name]
        provider  = prov_info["type"]
        model     = self._model_var.get()
        api_key   = self._api_key_var.get().strip()
        base_url  = self._base_url_var.get().strip() or prov_info.get("base_url")

        stt_name    = self._stt_var.get()
        stt_info    = _STT_PROVIDERS[stt_name]
        stt_id      = stt_info["id"]
        stt_model   = self._stt_model_var.get()
        stt_key     = self._stt_key_var.get().strip() if stt_info["cloud"] else None

        lang_raw  = self._lang_var.get()
        lang_code = None if lang_raw == "Auto-detect" else lang_raw.lower()[:2]

        from transcript_agent import ReportConfig

        config = ReportConfig(
            include_summary=True,
            include_key_points=True,
            include_action_items=True,
            include_transcript=True,
            include_speaker_profiles=self._profiles_var.get(),
            include_speech_analytics=True,
            include_interview_mode=self._interview_var.get(),
            include_interview_deep=self._deep_var.get(),
        )

        # Save current settings
        self._settings.update({
            "provider": prov_name,
            "model": model,
            "stt_engine": stt_name,
        })
        _save_settings(self._settings)

        q: queue.Queue = queue.Queue()

        def on_whisper_progress(pct):
            q.put(("pct", pct))

        def on_raw_transcript(text):
            q.put(("transcript_preview", text))

        def on_stage_change(stage):
            q.put(("stage", stage))

        def on_log(msg):
            q.put(("log", msg))

        def background():
            try:
                from transcript_agent import run
                result = run(
                    file_path=file_path,
                    output_dir=str(self._job_dir),
                    whisper_model=stt_model,
                    panel_mode=self._profiles_var.get(),
                    num_speakers=None,
                    config=config,
                    api_key=api_key,
                    provider=provider,
                    model=model,
                    base_url=base_url or None,
                    language=lang_code,
                    language_variant=None,
                    speaker_names=self._speaker_var.get().strip() or None,
                    on_whisper_progress=on_whisper_progress,
                    on_raw_transcript=on_raw_transcript,
                    on_stage_change=on_stage_change,
                    on_log=on_log,
                    stt_provider=stt_id,
                    stt_api_key=stt_key,
                    stt_model=stt_model,
                    cancel_event=self._cancel_ev,
                )
                q.put(("done", result))
            except KeyboardInterrupt:
                q.put(("cancelled", None))
            except Exception as e:
                q.put(("error", str(e)))

        threading.Thread(target=background, daemon=True).start()
        self._poll_queue(q)

    def _poll_queue(self, q: queue.Queue):
        try:
            while True:
                msg_type, payload = q.get_nowait()
                if msg_type == "log":
                    self._log.append(str(payload))
                elif msg_type == "stage":
                    stage_labels = {
                        "extracting": "🔄  Extracting audio…",
                        "whisper":    "🎤  Transcribing…",
                        "claude":     "🤖  AI analysis…",
                        "deepgram":   "☁️  Deepgram STT…",
                    }
                    self._status_var.set(stage_labels.get(payload, f"🔄  {payload}"))
                elif msg_type == "pct":
                    self._progress.set(float(payload))
                elif msg_type == "transcript_preview":
                    self._set_text("Transcript", str(payload))
                elif msg_type == "done":
                    self._on_job_done(payload)
                    return
                elif msg_type == "cancelled":
                    self._on_job_cancelled()
                    return
                elif msg_type == "error":
                    self._on_job_error(str(payload))
                    return
        except queue.Empty:
            pass
        self.after(150, lambda: self._poll_queue(q))

    def _on_job_done(self, result):
        self._running = False
        self._run_btn.configure(state="normal")
        self._cancel_btn.configure(state="disabled")
        self._progress.set(1.0)
        self._status_var.set("✅  Done — all outputs ready")
        self._log.append("✅  Finished! Results in tabs above.")

        # Populate text tabs
        from transcript_agent import build_combined_report, stats_to_markdown
        self._set_text("Summary",    result.summary or "")
        self._set_text("Transcript", result.clean_transcript or "")
        self._set_text("Speakers",   result.speaker_dialogue or "")

        profiles_md = ""
        if hasattr(result, "speaker_profiles") and result.speaker_profiles:
            lines = []
            for spk, info in result.speaker_profiles.items():
                lines.append(f"## {spk}\n{info}")
            profiles_md = "\n\n".join(lines)
        self._set_text("Profiles", profiles_md)

        interview_md = ""
        if hasattr(result, "interview_questions") and result.interview_questions:
            lines = []
            for q in result.interview_questions:
                if isinstance(q, dict):
                    lines.append(f"**Q: {q.get('question','')}**\n{q.get('analysis','')}")
                else:
                    lines.append(str(q))
            interview_md = "\n\n".join(lines)
        self._set_text("Interview", interview_md)

        try:
            analytics_md = stats_to_markdown(result.speaker_stats)
        except Exception:
            analytics_md = ""
        self._set_text("Analytics", analytics_md)

        try:
            from transcript_agent import ReportConfig
            combined = build_combined_report(result, ReportConfig())
        except Exception:
            combined = result.clean_transcript or ""
        self._set_text("Combined", combined)

        # Enable download buttons
        self._open_folder_btn.configure(state="normal")
        dl_map = {
            "PDF":        "_report.pdf",
            "DOCX":       "_report.docx",
            "Transcript": "_transcript.txt",
            "Speakers":   "_speakers.txt",
            "Markdown":   "_report.md",
            "Combined":   "_combined.txt",
            "SRT":        "_transcript.srt",
            "VTT":        "_transcript.vtt",
            "JSON":       "_full.json",
        }
        for label, suffix in dl_map.items():
            found = list(self._job_dir.glob(f"*{suffix}"))
            if found:
                self._dl_btns[label].configure(state="normal")

        self._tabs.set("Transcript")

    def _on_job_cancelled(self):
        self._running = False
        self._run_btn.configure(state="normal")
        self._cancel_btn.configure(state="disabled")
        self._status_var.set("⚠️  Cancelled")
        self._log.append("⚠️  Job cancelled by user.")

    def _on_job_error(self, err: str):
        self._running = False
        self._run_btn.configure(state="normal")
        self._cancel_btn.configure(state="disabled")
        self._status_var.set("❌  Error")
        self._log.append(f"❌  ERROR: {err}")
        messagebox.showerror("Processing error", err[:500])

    def _cancel_job(self):
        self._cancel_ev.set()
        self._log.append("⚠️  Cancellation requested…")
        self._cancel_btn.configure(state="disabled")

    # ── History ────────────────────────────────────────────────────────────────

    def _load_history(self):
        self._history_box.configure(state="normal")
        self._history_box.delete("1.0", "end")
        jobs = []
        for status_file in sorted(OUT_DIR.rglob(".job_status.json"), reverse=True):
            try:
                data = json.loads(status_file.read_text())
                jobs.append(data)
            except Exception:
                pass
        if not jobs:
            self._history_box.insert("end", "No history found.\n")
        else:
            for job in jobs[:50]:
                stem      = job.get("stem", "?")
                status    = job.get("status", "?")
                completed = job.get("completed", job.get("started", ""))[:19]
                self._history_box.insert(
                    "end", f"{completed}  {status:8}  {stem}\n")
        self._history_box.configure(state="disabled")

    # ── Helpers ────────────────────────────────────────────────────────────────

    def _set_text(self, tab: str, text: str):
        tb = self._text_boxes.get(tab)
        if not tb:
            return
        tb.configure(state="normal")
        tb.delete("1.0", "end")
        tb.insert("1.0", text)
        tb.configure(state="disabled")

    def _on_close(self):
        if self._running:
            if not messagebox.askyesno("Quit?",
                                        "A job is running. Cancel it and quit?"):
                return
            self._cancel_ev.set()
        self.destroy()


# ── Entry point ────────────────────────────────────────────────────────────────

def main():
    app = TranscriptAgentApp()
    app.mainloop()


if __name__ == "__main__":
    main()
