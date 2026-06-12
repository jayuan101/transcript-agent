#!/usr/bin/env python3
"""
Transcript Agent (New UI) — Application Launcher
Entry point for the PyInstaller bundle of the React/PrimeReact UI.
Opens the browser once the FastAPI server is ready.
"""
import os
import sys
import threading
import time
import webbrowser

def _find_free_port(preferred=8000):
    # api.py binds to 0.0.0.0, so check that — a port bound by another
    # process on 0.0.0.0 can otherwise look "free" when probed on 127.0.0.1.
    import socket
    for port in range(preferred, preferred + 20):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.bind(("0.0.0.0", port))
                return port
        except OSError:
            continue
    # Last resort: let the OS pick
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("0.0.0.0", 0))
        return s.getsockname()[1]

PORT = _find_free_port(8000)

# ── Environment setup ─────────────────────────────────────────────────────────
os.environ["API_PORT"] = str(PORT)
os.environ.setdefault("PYTHONIOENCODING", "utf-8:replace")
os.environ.setdefault("PYTHONUTF8", "1")

# ── Path resolution ───────────────────────────────────────────────────────────
if getattr(sys, "frozen", False):
    # Running inside a PyInstaller bundle
    BASE_DIR = os.path.dirname(sys.executable)
    BUNDLE_DIR = sys._MEIPASS if hasattr(sys, "_MEIPASS") else BASE_DIR
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
    BUNDLE_DIR = BASE_DIR

os.chdir(BASE_DIR)
if BUNDLE_DIR not in sys.path:
    sys.path.insert(0, BUNDLE_DIR)

# ── Fix: PyInstaller --noconsole sets sys.stdout/stderr to None ───────────────
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

# ── Launch the FastAPI app ─────────────────────────────────────────────────────
# api.py uses `if __name__ == "__main__"` guard so we must set __name__.
import runpy
runpy.run_path(os.path.join(BUNDLE_DIR, "api.py"), run_name="__main__")
