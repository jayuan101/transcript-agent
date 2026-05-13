#!/bin/sh
# Docker entrypoint — sets up proxy, then starts REST API + Gradio UI side by side.

echo "[entrypoint] Starting Transcript Agent..."

# ── ProxyScrape proxy setup ───────────────────────────────────────────────────
if [ -n "$PROXYSCRAPE_API_KEY" ]; then
    echo "[entrypoint] Finding working proxy via ProxyScrape..."
    PROXY=$(python /app/proxy_manager.py 2>/dev/null)
    if [ -n "$PROXY" ]; then
        export HTTP_PROXY="$PROXY"
        export HTTPS_PROXY="$PROXY"
        export http_proxy="$PROXY"
        export https_proxy="$PROXY"
        echo "[entrypoint] Proxy set: $PROXY"
    else
        echo "[entrypoint] No proxy found — running without proxy."
        unset HTTP_PROXY HTTPS_PROXY http_proxy https_proxy
    fi
else
    echo "[entrypoint] No PROXYSCRAPE_API_KEY — running without proxy."
fi

# ── Start REST API on port 8000 (background) ─────────────────────────────────
echo "[entrypoint] Starting REST API on port 8000..."
python /app/api.py &
API_PID=$!
echo "[entrypoint] REST API PID=$API_PID"

# ── Start Gradio UI on port 7860 (foreground) ────────────────────────────────
echo "[entrypoint] Starting Gradio UI on port 7860..."
exec python /app/app.py
