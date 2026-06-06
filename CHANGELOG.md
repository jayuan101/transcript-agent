# Changelog

## v2.4.0 — 2026-06-06
- OTA auto-update built into every installer from day one — Desktop shortcut now launches a launcher that checks GitHub on every startup, downloads and installs any newer version silently, then starts the app
- Users never need to manually download again — every launch is an update check
- Added "Check for Updates" button in the app UI for manual on-demand update checks
- Installer now ships ta_launcher.ps1 and Launch-TranscriptAgent.bat alongside the zip

## v2.3.9 — 2026-06-06
- History tab: deleted records now go to Trash instead of being permanently removed
- Trash accordion at the bottom of History tab — select a row and click Restore to bring it back
- Empty Trash button for permanent deletion when you're sure
- Removed Export and Import buttons from History tab

## v2.3.8 — 2026-06-06
- Coding challenge candidate answer now shown in a full code block (amber/dark theme) — no more plain text, preserves every line of code and reasoning exactly as written
- Approach/Time/Space/Role context text now visible in dark mode — switched from hardcoded colors to CSS variables
- AI prompt updated to reproduce candidate code verbatim with newlines preserved, never summarise

## v2.3.7 — 2026-06-06
- Fixed "Cannot find empty port" crash on Windows exe — app now kills the stale process holding port 7860, waits 1 second, then falls back to ports 7861–7869 if still busy
- Fixed hidden update button showing "_upd" text in the UI — now fully invisible with display:none
- Fixed TranscriptAgent-Windows.zip reappearing in every release — removed from release.yml workflow permanently

## v2.3.6 — 2026-06-06
- Auto-updater for Windows exe: clicking "Update Now" downloads the new version in the background, shows a live progress bar, then silently relaunches the app on the new version — no manual steps
- Update writes a PowerShell helper script that waits for the app to close, extracts the new zip, and restarts automatically
- History and transcripts in %APPDATA%\TranscriptAgent\ are never touched during updates
- Mac and source installs retain previous update behaviour

## v2.3.5 — 2026-06-06
- Replaced corporate-sounding UI messages with friendlier language
- Update prompt now says "grab the latest version from the link above — takes 2 minutes!" instead of formal installer instructions
- No API key error now says "Add your API key at the top to get started"
- No file error now says "Drop a file, paste a file path, or paste a URL above to get started"

## v2.3.4 — 2026-06-06
- Stop button now fully resets the UI to initial state — no "Stopped" message, clears log, results, and downloads instantly
- Fixed ffmpeg not found on Windows exe build — imageio-ffmpeg binary is now properly bundled in PyInstaller package
- Fixed ffmpeg detection order: system ffmpeg checked first (better for Docker/Mac), imageio-ffmpeg as fallback (Windows)
- Added PayPal donation button to app footer (paypal.me/jay247616)
- Modernized README with badges, clean layout, and updated install/launch instructions
- HuggingFace space is now public
- Remotes consolidated — single `git push origin main` deploys to both GitHub and HuggingFace
- Shortened large file warning text
- Clarified Windows/Mac zip launch instructions: Desktop shortcut is the primary launch method after install

## v2.3.3 — 2026-06-06
- Removed standalone Video Delivery card (role dropdowns + Analyze Video button) from Interview Analysis tab — it was appearing before analysis ran and triggering the kGpuService/EGL error on CPU-only HuggingFace Spaces
- Video analysis now runs automatically as part of the main Analyze flow; role assignments are handled as hidden components

## v2.3.2 — 2026-06-06
- Fixed "Could not auto-detect faces" error on video upload when GPU/EGL is unavailable — FaceLandmarker and PoseLandmarker now automatically retry with CPU delegate if GPU init fails (e.g. headless Linux, missing EGL)
- Fixed a NameError in the frame processing loop where `use_gpu` was referenced but not passed through to the inner method
- Fixed app startup crash: `AttributeError: 'State' object has no attribute 'click'` — removed dead event wiring for `iv_analyze_btn` after it was replaced with a stub

## v2.3.1 — 2026-06-06
- Download Results section always open and visible below the Analyze button — no longer hidden in a collapsed accordion
- Shows "Run an analysis to generate your reports" placeholder listing PDF · DOCX · Transcript · SRT · JSON before analysis runs; placeholder hides when buttons appear
- HuggingFace sync now retries up to 5 times with 30s/60s/90s/120s backoff on 429 rate limit errors
- Fixed garbled emoji in README YAML header for HuggingFace

## v2.3.0 — 2026-06-06
- Coding Challenge Analysis: auto-detects coding/algorithm questions in any interview transcript
- Per challenge: candidate's full answer, score, and a complete working optimal solution
- Language detection: if Java/Kotlin/Python/SQL/Swift/Go/etc. was requested, solution uses ONLY that language
- Library detection: PySpark, Pandas, NumPy, TensorFlow, PyTorch, Jetpack Compose, Spring Boot, React, etc. used when asked
- Role detection: infers Data Engineer / Gen AI / Android / iOS / Backend / Frontend / DevOps / ML Engineer from the interview and picks the right default tech stack when no language was specified
- Scoring is based ONLY on the language/tech that was asked — no penalty for language choice when none was specified
- Coding score (0-10) shown separately from behavioral score
- Shown in Interview Mode tab (dark code block), PDF (dark code block with language header), DOCX (Courier New code block), and plain text exports
- Coaching tips explicitly marked informational — never affect any score
- No more summarising: answer_said now reproduces everything the candidate said verbatim; model_answer is a full detailed complete response, not a brief summary
- DOCX always generated (was silently skipped before); Video Delivery Analysis included in DOCX
- PDF and DOCX download buttons styled as primary with clear labels
- Build scripts (spec, zip builders) fixed for UTF-8 encoding on Windows CI runners

## v2.2.4 — 2026-06-06
- Single version source of truth: all build scripts, setup installers, run.bat, and CI now read the version directly from app.py — no more version drift across files
- setup_windows.bat and run.bat read version via findstr at runtime
- setup_mac.sh reads version via grep/sed at runtime
- build_win_zip.py, build_mac_zip.py, TranscriptAgent.spec read via regex at build time
- build-exe.yml fallback reads from app.py in CI shell step

## v2.2.3 — 2026-06-06
- In-app one-click update button: clicking "⬆ Update Now" in the browser runs git pull + pip install automatically — no terminal needed
- GPU mismatch detection: app shows orange "⚠️ GPU Mismatch" badge when NVIDIA/Apple Silicon GPU is present but PyTorch CPU-only build is installed
- Windows setup: new [5] Fix GPU option — uninstalls CPU torch, reinstalls correct CUDA 12.1/11.8 build based on driver version, verifies CUDA after
- Mac setup: new [5] Fix GPU option — detects Apple Silicon + CPU torch mismatch, reinstalls PyTorch with MPS support, verifies MPS after
- Mac setup: menu extended to [1-5], GPU mismatch warning shown automatically at launch
- All version strings synced to v2.2.3 (build scripts, spec, README, run.bat, CI workflow)

## v2.2.2 — 2026-06-06
- Setup: both Windows and Mac scripts now check GitHub on launch and show "UPDATE AVAILABLE: vX → vY" with a direct update button when a newer version exists
- Synced all version strings (run.bat, build scripts, spec, README) to current version

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
