============================================
 TRANSCRIPT AGENT — Setup Instructions
============================================

WHAT YOU NEED
-------------
1. Docker Desktop (free): https://www.docker.com/products/docker-desktop
2. An Anthropic API key (free): https://console.anthropic.com
3. This folder

QUICK START (Windows)
---------------------
1. Install Docker Desktop, restart your computer
2. Double-click: setup.bat
3. Paste your Anthropic API key when asked
4. Browser opens automatically at http://localhost:7860

QUICK START (Mac / Linux)
--------------------------
1. Install Docker Desktop
2. Open Terminal in this folder
3. Run: chmod +x setup.sh && ./setup.sh
4. Paste your Anthropic API key when asked

WHAT IT CAN DO
--------------
- Transcribe audio files: .mp3 .wav .m4a .flac .ogg .aac
- Transcribe video files: .mp4 .mov .avi .mkv .webm
- Process documents:     .pdf .docx .txt .srt .vtt
- Speaker detection, accent analysis, speech rate
- 29 languages including all Spanish regional variants
- Download transcripts, reports, JSON exports

URLS
----
  App UI:   http://localhost:7860
  REST API: http://localhost:8000/docs

STOP / START
------------
  Stop:    docker compose down
  Start:   docker compose up -d
  Restart: docker compose down && docker compose up -d
