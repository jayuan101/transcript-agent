# Changelog

## v2.2.1 — 2026-06-06
- Setup: AMD/Intel GPU auto-selects DirectML without prompting — no wrong choice possible
- Setup: NVIDIA prompt now shows detected GPU model and driver CUDA version, tells you which option (1 or 2) is correct for your card, and defaults to the right one automatically
- Setup: fixed `ModuleNotFoundError: No module named 'pkg_resources'` crash when installing openai-whisper — setuptools is now re-installed explicitly before requirements and no longer silenced

## v2.2.0 — 2026-06-06
- GPU badge in sidebar: green card shows detected GPU name (NVIDIA/MPS/DirectML) or gray "CPU Mode" card when no GPU found
- Auto-detect GPU at startup via nvidia-smi / WMIC — no torch import required; result passed to app via TA_GPU_DEVICE/TA_GPU_NAME env vars
- Network monitor redesigned: side-by-side upload/download cards with large speed numbers, visible from page load in idle state
- Analyze button pulses red while running; Stop button is now clickable and cancels analysis immediately
- History and outputs now persist to user data dir instead of the bundle _internal/ folder — survives app updates
- MediaPipe and OpenCV binaries bundled; model cache persisted outside the bundle
- Mac: proper .app bundle on Desktop instead of a .command file
- App icon (.ico / .icns) added and wired into Windows/Mac shortcuts
- Free port finder at launch: automatically picks an available port if 7860 is in use
- CI: auto-build triggered on every push to main

## v2.1.4 — 2026-06-05
- Always-on sleep prevention: machine stays awake the entire time the app is running (not just during jobs)
  - Windows: SetThreadExecutionState blocks idle sleep + lid-close blocked from app startup
  - Mac: caffeinate -i -m -s subprocess keeps system awake on both battery and AC power

## v2.1.3 — 2026-06-05
- GPU: MediaPipe face/pose landmarkers now use Delegate.GPU when GPU is enabled (falls back to CPU if unsupported on current platform)
- GPU: Whisper on CUDA enables cudnn.benchmark and allow_tf32 for ~10% faster convolutions/matmul on Ampere+ GPUs
- GPU: TF_XLA_FLAGS auto-jit=2 enables XLA JIT compilation for TF/DeepFace at startup; TF_CPP_MIN_LOG_LEVEL=2 suppresses TF noise
- GPU: scan_faces() now respects use_gpu toggle and passes correct delegate

## v2.1.2 — 2026-06-05
- Fix: removed broken app_kwargs uvicorn timeout setting — was targeting FastAPI not uvicorn, had no effect and risked unexpected keyword error

## v2.1.1 — 2026-06-05
- Fix: GPU toggle was ignored for translation, video LLM call, and re-analysis with profile — all LLMClient sites now respect use_gpu so Ollama uses GPU layers correctly

## v2.1.0 — 2026-06-05
- GPU toggle preference persisted in browser localStorage — survives page reloads
- GPU detected at startup in run.bat (Windows) and setup_mac.sh (Mac); result passed to app via TA_GPU_DEVICE so the toggle is pre-selected without re-running detection in the browser
- GPU now accelerates DeepFace emotion analysis (tf.device context) and Ollama LLM (num_gpu=-1 forces all layers onto GPU) in addition to Whisper
- Merge two files into one transcript: paste a second file path in the new "Part 2" field — both files are transcribed sequentially, timestamps offset, then merged before AI analysis
- Large file upload: prominent warning banner directing files >500 MB to the path input; share tunnel disabled for local installs (was causing upload timeouts); max_file_size raised to 10 GB
- Fix: Whisper progress stalled at 99% — overrode tqdm close() to fire final 100% callback
- Fix: ffmpeg resolution now verifies the binary exists on disk before using it, then falls back to shutil.which — prevents silent WinError 2 on machines where the imageio_ffmpeg path is stale
- Fix: clear user-facing error when ffmpeg is missing ("re-run setup script") instead of cryptic WinError 2 in both Whisper and Deepgram paths

## v1.1.10 — 2026-06-01
- Interview Mode always on: checkboxes removed, mode and deep analysis permanently active
- Windows exe rebuilt and verified launching cleanly on Windows
- Docker workflow: continue-on-error on description step so build never fails on token scope

## v1.1.9 — 2026-06-01
- Stop button: tooltip "Stop transcription" on hover
- Stop button now cancels AI analysis immediately via threading.Event cancel flag
- Transcript checkpoint cache: re-submitting same file skips re-transcription
- ETA panel visible from page load with idle step tracker
- Est. Time stat shown for Loading and Extracting stages
- Network monitor ping reduced from 6 s to 2 s for always-live display

## v1.1.8 — 2026-06-01
- Fix: pandas and gradio_client now bundled in Windows exe

## v1.1.7 — 2026-06-01
- Fix: auto-collect version.txt from all Gradio micro-deps; groovy added to collect_all

## v1.1.6 — 2026-06-01
- Fix: collect_all for gradio and safehttpx data files in PyInstaller bundle

## v1.1.5 — 2026-06-01
- Fix: numpy now bundled — was excluded, caused crash on Windows startup

## v1.1.4 — 2026-06-01
- Fix: missing STT package error now tells user to switch to Whisper (Local) as the quick fix

## v1.1.3 — 2026-06-01
- Fix: Windows python311.dll error — installer extracts to %LOCALAPPDATA% and creates Desktop shortcut

## v1.1.2 — 2026-05-31
- Advancement likelihood % shown at top of Interview Coaching tab
- Translate output to: language dropdown above Analyze button

## v1.1.1 — 2026-05-31
- Fix: Summary, Transcript, and Speaker Dialogue tabs now always populate
- Fix: JSON schema reordered so summary survives token-limit cuts

## v1.1 — 2026-05-31
- GitHub OTA update checker: auto-detects new releases, shows Windows + Mac one-click download buttons
- Floating ▶ Analyze button: fixed click handler to use CSS class selector (works on all Gradio re-renders)
- AI analysis stage: live % progress bar with ETA estimate (elapsed-based asymptotic curve)
- Network monitor: always-on rendering from page load (retry loop instead of fixed timeout)
- Interview Q&A in History tab: shows candidate's exact words per question with score + deflection flag
- Transcript Output Language: translate transcript to any language after STT completes
- v1.1 changelog and version bump across app.py and TranscriptAgent.spec

## v1.0 — 2026-05-31
- 9 STT engines: Whisper (local/offline), OpenAI, Groq, Deepgram, AssemblyAI, Google, Azure, ElevenLabs, Rev.ai
- Interview Mode with per-question scoring and Deep Analysis
- Session History with tokens, cost, and interview score
- Live network monitor and session stats panel
- New exports: .srt, .vtt, .docx
- Floating ▶ Analyze button
- In-app update checker

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
