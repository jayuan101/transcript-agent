# ── Transcript Agent — Docker Image ───────────────────────────────────────────
# Build:  docker compose build
# Start:  docker compose up -d
# Stop:   docker compose down
# ─────────────────────────────────────────────────────────────────────────────

ARG PYTHON_VERSION=3.12
FROM python:${PYTHON_VERSION}-slim

ARG VERSION=dev
ARG BUILD_DATE=unknown
ARG GIT_SHA=unknown

LABEL org.opencontainers.image.title="Transcript Agent" \
      org.opencontainers.image.description="AI-powered transcription and analysis — Whisper + multi-provider LLM" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.created="${BUILD_DATE}" \
      org.opencontainers.image.revision="${GIT_SHA}"

# System packages — ffmpeg for audio/video processing
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install PyTorch CPU-only first (separate layer — changes rarely)
# GPU: replace with --index-url https://download.pytorch.org/whl/cu121
RUN pip install --no-cache-dir torch --index-url https://download.pytorch.org/whl/cpu

# Install app requirements (separate layer so code changes don't bust this cache)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
RUN pip install --no-cache-dir python-dotenv imageio-ffmpeg

# Copy application source
COPY app.py transcript_agent.py launch.py api.py job_db.py ./
COPY entrypoint.sh /entrypoint.sh
RUN sed -i 's/\r//' /entrypoint.sh && chmod +x /entrypoint.sh

# Outputs directory (mount as volume so files persist across restarts)
RUN mkdir -p /app/outputs

# Whisper model cache — mount as volume to avoid re-downloading on restart
ENV XDG_CACHE_HOME=/app/.cache

# Gradio — listen on all interfaces so Docker port mapping works
ENV GRADIO_SERVER_NAME=0.0.0.0
ENV GRADIO_SERVER_PORT=7860

EXPOSE 7860
EXPOSE 8000

ENTRYPOINT ["/entrypoint.sh"]
