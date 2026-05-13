# ── Transcript Agent — Docker Image ───────────────────────────────────────────
# Runs the Gradio app on port 7860.
# Build:   docker compose build
# Start:   docker compose up -d
# Stop:    docker compose down
# ─────────────────────────────────────────────────────────────────────────────

FROM python:3.11-slim

# System packages — ffmpeg for audio/video, git for Whisper download
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install PyTorch CPU-only first (smaller image, ~700 MB vs ~4 GB GPU)
# If you have an Nvidia GPU on the host, replace this line with:
#   RUN pip install --no-cache-dir torch --index-url https://download.pytorch.org/whl/cu118
RUN pip install --no-cache-dir torch --index-url https://download.pytorch.org/whl/cpu

# Install app requirements
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
RUN pip install --no-cache-dir python-dotenv imageio-ffmpeg

# Copy application source
COPY app.py transcript_agent.py launch.py proxy_manager.py api.py ./
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# Outputs directory (will be mounted as a volume so files persist)
RUN mkdir -p /app/outputs

# Whisper caches models here — mount as a volume so re-starts don't re-download
ENV XDG_CACHE_HOME=/app/.cache

# Tell Gradio to listen on all interfaces so Docker port mapping works
ENV GRADIO_SERVER_NAME=0.0.0.0
ENV GRADIO_SERVER_PORT=7860

EXPOSE 7860
EXPOSE 8000

ENTRYPOINT ["/entrypoint.sh"]
