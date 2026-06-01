#!/usr/bin/env python3
"""
Transcript Agent — Application Launcher
Entry point for PyInstaller bundle.
Opens the browser once the Gradio server is ready.
"""
import os
import sys
import threading
import time
import webbrowser

PORT = 7860

# ── Environment setup ─────────────────────────────────────────────────────────
os.environ.setdefault("GRADIO_SERVER_NAME",        "127.0.0.1")
os.environ.setdefault("GRADIO_SERVER_PORT",        str(PORT))
os.environ.setdefault("GRADIO_ANALYTICS_ENABLED",  "False")
os.environ.setdefault("GRADIO_TELEMETRY_ENABLED",  "False")
os.environ.setdefault("PYTHONIOENCODING",           "utf-8:replace")
os.environ.setdefault("PYTHONUTF8",                "1")

# ── Path resolution ───────────────────────────────────────────────────────────
if getattr(sys, "frozen", False):
    # Running inside a PyInstaller bundle
    BASE_DIR = os.path.dirname(sys.executable)
    # _MEIPASS is the temp dir where bundled files are extracted
    BUNDLE_DIR = sys._MEIPASS if hasattr(sys, "_MEIPASS") else BASE_DIR
else:
    BASE_DIR   = os.path.dirname(os.path.abspath(__file__))
    BUNDLE_DIR = BASE_DIR

os.chdir(BASE_DIR)
if BUNDLE_DIR not in sys.path:
    sys.path.insert(0, BUNDLE_DIR)

# ── Fix: PyInstaller --noconsole sets sys.stdout/stderr to None ───────────────
# uvicorn's DefaultFormatter calls stream.isatty() → AttributeError on None.
if sys.stdout is None or sys.stderr is None:
    _log_path = os.path.join(BASE_DIR, "app.log")
    _log = open(_log_path, "w", encoding="utf-8", errors="replace", buffering=1)
    if sys.stdout is None:
        sys.stdout = _log
    if sys.stderr is None:
        sys.stderr = _log

# ── Browser opener — polls until server responds ───────────────────────────────
def _open_browser():
    import urllib.request
    url = f"http://localhost:{PORT}"
    for _ in range(180):          # up to 90 seconds
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{PORT}", timeout=1)
            webbrowser.open(url)
            return
        except Exception:
            time.sleep(0.5)

threading.Thread(target=_open_browser, daemon=True).start()

# ── Launch the Gradio app ─────────────────────────────────────────────────────
# app.py uses `if __name__ == "__main__"` guard so we must set __name__.
import runpy
runpy.run_path(os.path.join(BUNDLE_DIR, "app.py"), run_name="__main__")
