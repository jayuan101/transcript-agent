"""Entry point for the standalone desktop app build."""
import sys
import os
import threading
import time

# When frozen by PyInstaller, fix paths
if getattr(sys, "frozen", False):
    _base = sys._MEIPASS
    os.chdir(_base)
    sys.path.insert(0, _base)
    os.environ.setdefault(
        "GRADIO_TEMP_DIR",
        os.path.join(os.path.expanduser("~"), ".transcript_agent", "tmp"),
    )
    os.environ.setdefault(
        "TRANSCRIPT_OUTPUT_DIR",
        os.path.join(os.path.expanduser("~"), "TranscriptAgent", "outputs"),
    )

PORT = int(os.environ.get("GRADIO_SERVER_PORT", "7860"))
# Bind to loopback only — no browser needed, pywebview talks directly
os.environ["GRADIO_SERVER_NAME"] = "127.0.0.1"
os.environ["GRADIO_SERVER_PORT"] = str(PORT)
os.environ["TRANSCRIPT_AGENT_WINDOWED"] = "1"  # tells app.py not to open a browser

# Start Gradio server in background thread
def _start_server():
    import app  # noqa: F401

threading.Thread(target=_start_server, daemon=True).start()

# Wait for server to be ready (up to 30s)
import urllib.request as _ur
for _ in range(60):
    try:
        _ur.urlopen(f"http://127.0.0.1:{PORT}", timeout=1)
        break
    except Exception:
        time.sleep(0.5)

# Open in a native desktop window via pywebview
try:
    import webview
    webview.create_window(
        "Transcript Agent",
        f"http://127.0.0.1:{PORT}",
        width=1280,
        height=900,
        min_size=(900, 600),
    )
    webview.start()
except ImportError:
    # Fallback: open in browser if webview unavailable
    import webbrowser
    webbrowser.open(f"http://127.0.0.1:{PORT}")
    while True:
        time.sleep(1)
