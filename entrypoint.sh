#!/bin/sh
# Docker entrypoint — starts REST API + Gradio UI side by side.

echo "[entrypoint] Starting Transcript Agent..."

# ── Start REST API on port 8000 (background) ─────────────────────────────────
echo "[entrypoint] Starting REST API on port 8000..."
python /app/api.py &
API_PID=$!
echo "[entrypoint] REST API PID=$API_PID"

# ── Start Gradio UI on port 7860 (foreground) ────────────────────────────────
echo "[entrypoint] Starting Gradio UI on port 7860..."
exec python /app/app.py
