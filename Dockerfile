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

# System packages — ffmpeg + full GL/EGL stack for mediapipe on headless Linux
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    git \
    libgl1 \
    libgles2 \
    libegl1 \
    libgbm1 \
    libglib2.0-0 \
    libsm6 \
    libxext6 \
    libxrender1 \
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
COPY app.py transcript_agent.py launch.py api.py video_analyzer.py ./
COPY entrypoint.sh /entrypoint.sh

# Download MediaPipe models at build time so they're baked into the image
# (models are ~9 MB total; not tracked in git so COPY won't work on HF)
RUN python3 -c "\
import urllib.request, os; \
os.makedirs('/app/.mediapipe_models', exist_ok=True); \
print('Downloading face_landmarker.task...'); \
urllib.request.urlretrieve( \
  'https://storage.googleapis.com/mediapipe-models/face_landmarker/face_landmarker/float16/1/face_landmarker.task', \
  '/app/.mediapipe_models/face_landmarker.task'); \
print('Downloading pose_landmarker_lite.task...'); \
urllib.request.urlretrieve( \
  'https://storage.googleapis.com/mediapipe-models/pose_landmarker/pose_landmarker_lite/float16/1/pose_landmarker_lite.task', \
  '/app/.mediapipe_models/pose_landmarker_lite.task'); \
print('Models ready.')"

RUN sed -i 's/\r//' /entrypoint.sh && chmod +x /entrypoint.sh

# Outputs directory (mount as volume so files persist across restarts)
RUN mkdir -p /app/outputs

# Non-root user required by HuggingFace Spaces
RUN useradd -m -u 1000 -s /bin/sh user && chown -R user:user /app /entrypoint.sh
USER user

# Whisper model cache — mount as volume to avoid re-downloading on restart
ENV XDG_CACHE_HOME=/app/.cache

# Force software GL rendering — mediapipe/OpenCV won't need hardware GPU drivers
ENV LIBGL_ALWAYS_SOFTWARE=1
ENV MESA_GL_VERSION_OVERRIDE=3.3
# Allow TensorFlow (DeepFace) to use GPU memory growth — avoids OOM if CUDA is available
ENV TF_FORCE_GPU_ALLOW_GROWTH=true

# Gradio — listen on all interfaces so Docker port mapping works
ENV GRADIO_SERVER_NAME=0.0.0.0
ENV GRADIO_SERVER_PORT=7860
# Disable Gradio analytics / version telemetry
ENV GRADIO_ANALYTICS_ENABLED=False
ENV GRADIO_TELEMETRY_ENABLED=False

EXPOSE 7860
EXPOSE 8000

ENTRYPOINT ["/entrypoint.sh"]
