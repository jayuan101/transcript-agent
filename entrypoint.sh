#!/bin/sh
# Docker entrypoint.
#
# Default (UI_MODE=react): serve the React + Bootstrap UI *and* the REST API
#   from FastAPI (api.py) on port 7860. The React UI is at "/", API under
#   "/api/*", "/health", and docs at "/docs".
#
# Legacy (UI_MODE=gradio): run the original Gradio app (app.py), which also
#   grafts the REST API routes onto its own server on port 7860.

PORT="${PORT:-7860}"
UI_MODE="${UI_MODE:-react}"

if [ "$UI_MODE" = "gradio" ]; then
  echo "[entrypoint] Starting Transcript Agent (Gradio UI) on :$PORT ..."
  exec python /app/app.py
else
  echo "[entrypoint] Starting Transcript Agent (React UI + API) on :$PORT ..."
  export API_PORT="$PORT"
  exec python /app/api.py
fi
