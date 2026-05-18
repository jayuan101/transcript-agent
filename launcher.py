"""Entry point for the standalone .exe build."""
import sys
import os
import threading
import webbrowser
import time

# When frozen by PyInstaller, fix paths
if getattr(sys, "frozen", False):
    _base = sys._MEIPASS
    os.chdir(_base)
    sys.path.insert(0, _base)
    # Point Gradio temp dir to user's home so it persists across runs
    os.environ.setdefault(
        "GRADIO_TEMP_DIR",
        os.path.join(os.path.expanduser("~"), ".transcript_agent", "tmp"),
    )
    # Tell app.py where to write outputs
    os.environ.setdefault(
        "TRANSCRIPT_OUTPUT_DIR",
        os.path.join(os.path.expanduser("~"), "TranscriptAgent", "outputs"),
    )

PORT = int(os.environ.get("GRADIO_SERVER_PORT", "7860"))


def _open_browser():
    time.sleep(4)
    webbrowser.open(f"http://localhost:{PORT}")


threading.Thread(target=_open_browser, daemon=True).start()

# Import and launch the Gradio app
import app  # noqa: E402  (app.py is in the same directory)
