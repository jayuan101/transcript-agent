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

def _find_free_port(preferred=7860):
    import socket
    for port in range(preferred, preferred + 20):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.bind(("127.0.0.1", port))
                return port
        except OSError:
            continue
    # Last resort: let the OS pick
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]

PORT = _find_free_port(7860)

# ── Environment setup ─────────────────────────────────────────────────────────
os.environ.setdefault("GRADIO_SERVER_NAME",        "127.0.0.1")
os.environ["GRADIO_SERVER_PORT"] = str(PORT)  # always override to match found port
os.environ.setdefault("GRADIO_ANALYTICS_ENABLED",  "False")
os.environ.setdefault("GRADIO_TELEMETRY_ENABLED",  "False")
os.environ.setdefault("PYTHONIOENCODING",           "utf-8:replace")
os.environ.setdefault("PYTHONUTF8",                "1")

# ── GPU auto-detection (no torch needed) ─────────────────────────────────────
def _detect_gpu():
    import platform, subprocess
    machine = platform.machine().lower()
    system  = platform.system()

    # Skip detection inside known VMs / hypervisors
    try:
        if system == "Windows":
            r = subprocess.run(
                ["wmic", "computersystem", "get", "model"],
                capture_output=True, text=True, timeout=3
            )
            vm_keywords = ("virtualbox","vmware","hyper-v","qemu","xen","kvm","parallels","virtual machine")
            if any(k in r.stdout.lower() for k in vm_keywords):
                return "cpu", "VM detected — using CPU"
        elif system == "Linux":
            r = subprocess.run(["systemd-detect-virt"], capture_output=True, text=True, timeout=2)
            if r.returncode == 0 and r.stdout.strip() not in ("none", ""):
                return "cpu", "VM detected — using CPU"
    except Exception:
        pass

    # Apple Silicon — MPS always available
    if system == "Darwin" and machine == "arm64":
        return "mps", "Apple Silicon MPS"

    # NVIDIA — try nvidia-smi
    if system in ("Windows", "Linux"):
        try:
            r = subprocess.run(
                ["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"],
                capture_output=True, text=True, timeout=5
            )
            if r.returncode == 0 and r.stdout.strip():
                return "cuda", f"NVIDIA CUDA — {r.stdout.strip().splitlines()[0]}"
        except Exception:
            pass

    # AMD / Intel on Windows — DirectML
    if system == "Windows":
        try:
            r = subprocess.run(
                ["wmic", "path", "win32_VideoController", "get", "Name"],
                capture_output=True, text=True, timeout=3
            )
            out = r.stdout.lower()
            if any(k in out for k in ("amd", "radeon", "rx ")):
                return "dml", "AMD DirectML"
            if any(k in out for k in ("intel", "arc", "iris", "uhd")):
                return "dml", "Intel DirectML"
        except Exception:
            pass

    return "cpu", "CPU only"

_gpu_device, _gpu_name = _detect_gpu()
os.environ.setdefault("TA_GPU_DEVICE",  _gpu_device)
os.environ.setdefault("TA_GPU_NAME",    _gpu_name)

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
