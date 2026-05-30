# Changelog

## v3.48 — 2025-05-30
- 9 STT engines: Whisper (local), OpenAI Whisper API, Groq Whisper, Deepgram, AssemblyAI, Google Cloud STT, Azure Speech, ElevenLabs Scribe, Rev.ai
- Interview Mode on by default: extracts every question, scores answers Great / Good / Needs Improvement / Missed
- Deep Analysis on by default: deflection rate, advancement likelihood, prep guide
- Interview Coaching tab with color-coded per-question score cards, ideal answers, coaching tips
- History tab: every session saved locally, click any row to reload summary
- New exports: .srt subtitles, .vtt subtitles, .docx Word document
- .wma audio format now supported
- Floating ▶ Analyze button (bottom-right) wired via event delegation — always works
- Grouped 3-phase step tracker: [Transcription] → [AI Analysis] → [Complete] with live hint text
- In-app update checker for desktop/local installs — blue banner with one-click download
- Download accordion and changelog accordion removed from UI (cleaner)
- HF Spaces website: "View changelog on GitHub" link in footer only
- STT API key always visible, no jarring show/hide
- Language variant dropdown always rendered, no flash
- Indian language variant fix — Gradio value validation conflict resolved
- Deep Analysis and Interview Mode enabled by default
- STT timing moved into the processing log

## v2.3 — 2025-05-30
- Windows & Mac native installers (no Docker required)
- Cancel/Stop button in results panel
- API key remembered per-provider in browser localStorage
- AI provider & model remembered across sessions
- Pace reference redesigned as visual legend with colored chips

## v2.2 — 2025-05-29
- Professional UI redesign: hero section, cards, tabs, buttons
- Full dark mode support across all elements
- Hero: mic icon, dot-grid overlay, stats row (8 providers · 37+ languages), feature chips
- Changelog and download buttons removed from site

## v2.1 — 2025-05-15
- Multi-provider LLM: OpenAI, Gemini, Groq, Mistral, Together AI, Perplexity, Ollama
- Claude model selector (Haiku / Sonnet / Opus)
- PDF report export with per-language translation
- 37+ languages with regional dialect variants
- 3-step processing tracker and live ETA panel

## v2.0 — 2025-05-01
- Speaker diarization (Panel Mode) via WhisperX
- Speech analytics: WPM, pace label, accent detection
- Docker deployment on Hugging Face Spaces
