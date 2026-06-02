#!/bin/sh
# Docker entrypoint — Gradio UI + REST API on port 7860.
# REST routes (/health, /api/*) are grafted onto Gradio's FastAPI app at startup.

echo "[entrypoint] Starting Transcript Agent..."
exec python /app/app.py
