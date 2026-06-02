#!/bin/sh
# Docker entrypoint — REST API + Gradio UI served together on port 7860.
# (HuggingFace Spaces only exposes 7860, so both share the same port.)

echo "[entrypoint] Starting Transcript Agent on port 7860..."
exec python /app/app.py
