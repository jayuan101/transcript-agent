"""Standalone desktop launcher — native customtkinter window + system tray."""
import sys
import os
import threading

# ── PyInstaller path fix ──────────────────────────────────────────────────────
if getattr(sys, "frozen", False):
    _base = getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
    os.chdir(_base)
    sys.path.insert(0, _base)

_home    = os.path.expanduser("~")
_app_dir = os.path.join(_home, ".transcript_agent")
os.makedirs(_app_dir, exist_ok=True)

os.environ.setdefault("TRANSCRIPT_OUTPUT_DIR",
                       os.path.join(_home, "TranscriptAgent", "outputs"))

APP_NAME = "Transcript Agent"

# ── Single-instance guard ─────────────────────────────────────────────────────
if sys.platform == "win32":
    import ctypes as _ct
    _mutex = _ct.windll.kernel32.CreateMutexW(None, False, "TranscriptAgent_SingleInstance")
    if _ct.windll.kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
        _ct.windll.user32.MessageBoxW(0,
            "Transcript Agent is already running.\nCheck your system tray.",
            "Already running", 0x40)
        sys.exit(0)


# ── Icon helpers (used by tray) ───────────────────────────────────────────────
def _draw_icon(size):
    from PIL import Image, ImageDraw
    img  = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    r    = max(4, size // 8)
    draw.rounded_rectangle([0, 0, size - 1, size - 1], radius=r, fill=(30, 58, 95))
    bar_w   = max(2, size // 16)
    gap     = max(2, size // 14)
    heights = [0.28, 0.52, 0.72, 0.52, 0.28]
    n       = len(heights)
    total_w = n * bar_w + (n - 1) * gap
    x       = size // 2 - total_w // 2
    cy      = size // 2
    for h in heights:
        bh = int(size * h)
        by = cy - bh // 2
        draw.rounded_rectangle([x, by, x + bar_w, by + bh],
                                radius=max(1, bar_w // 2), fill=(255, 255, 255))
        x += bar_w + gap
    return img


# ── System tray ───────────────────────────────────────────────────────────────
_app_ref = None


def _build_tray():
    try:
        import pystray
        tray_img = _draw_icon(64)

        def on_open(icon, item):
            if _app_ref:
                _app_ref.after(0, _app_ref.deiconify)

        def on_quit(icon, item):
            icon.stop()
            os._exit(0)

        menu = pystray.Menu(
            pystray.MenuItem("Open " + APP_NAME, on_open, default=True),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit", on_quit),
        )
        icon = pystray.Icon(APP_NAME, tray_img, APP_NAME, menu)
        threading.Thread(target=icon.run, daemon=True).start()
        return icon
    except Exception:
        return None


# ── Launch ────────────────────────────────────────────────────────────────────
def main():
    global _app_ref

    # Start system tray before UI (non-blocking thread)
    tray = _build_tray()

    from ui_ctk import TranscriptAgentApp

    app = TranscriptAgentApp()
    _app_ref = app

    # Minimise to tray instead of closing
    if tray:
        def _on_close():
            app.withdraw()
        app.protocol("WM_DELETE_WINDOW", _on_close)

    app.mainloop()


if __name__ == "__main__":
    main()
