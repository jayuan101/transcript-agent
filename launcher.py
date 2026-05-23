"""Standalone desktop launcher — native window, system tray, splash screen."""
import sys
import os
import threading
import time

# ── PyInstaller path fix ──────────────────────────────────────────────────────
if getattr(sys, "frozen", False):
    _base = sys._MEIPASS
    os.chdir(_base)
    sys.path.insert(0, _base)

_home = os.path.expanduser("~")
_app_dir = os.path.join(_home, ".transcript_agent")
os.makedirs(_app_dir, exist_ok=True)

os.environ.setdefault("GRADIO_TEMP_DIR", os.path.join(_app_dir, "tmp"))
os.environ.setdefault("TRANSCRIPT_OUTPUT_DIR", os.path.join(_home, "TranscriptAgent", "outputs"))

PORT     = int(os.environ.get("GRADIO_SERVER_PORT", "7860"))
APP_URL  = f"http://127.0.0.1:{PORT}"
APP_NAME = "Transcript Agent"

os.environ["GRADIO_SERVER_NAME"]        = "127.0.0.1"
os.environ["GRADIO_SERVER_PORT"]        = str(PORT)
os.environ["TRANSCRIPT_AGENT_WINDOWED"] = "1"

# ── Icon (drawn with PIL, cached to disk) ─────────────────────────────────────
def _draw_icon(size):
    from PIL import Image, ImageDraw
    img  = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    r    = max(4, size // 8)
    draw.rounded_rectangle([0, 0, size - 1, size - 1], radius=r, fill=(30, 58, 95))
    bar_w  = max(2, size // 16)
    gap    = max(2, size // 14)
    heights = [0.28, 0.52, 0.72, 0.52, 0.28]
    n      = len(heights)
    total_w = n * bar_w + (n - 1) * gap
    x      = size // 2 - total_w // 2
    cy     = size // 2
    for h in heights:
        bh = int(size * h)
        by = cy - bh // 2
        draw.rounded_rectangle(
            [x, by, x + bar_w, by + bh],
            radius=max(1, bar_w // 2),
            fill=(255, 255, 255),
        )
        x += bar_w + gap
    return img


def _icon_path():
    ext  = "ico" if sys.platform == "win32" else "png"
    path = os.path.join(_app_dir, f"icon.{ext}")
    if not os.path.exists(path):
        try:
            from PIL import Image
            if ext == "ico":
                sizes = [16, 32, 48, 64, 128, 256]
                imgs  = [_draw_icon(s) for s in sizes]
                imgs[0].save(path, format="ICO",
                             sizes=[(s, s) for s in sizes],
                             append_images=imgs[1:])
            else:
                _draw_icon(256).save(path, format="PNG")
        except Exception:
            return None
    return path


# ── Splash screen (stdlib tkinter — zero extra deps) ─────────────────────────
def _splash():
    try:
        import tkinter as tk
        from tkinter import ttk
        root = tk.Tk()
        root.title(APP_NAME)
        root.overrideredirect(True)
        root.configure(bg="#1e3a5f")
        root.attributes("-alpha", 0.96)
        w, h = 400, 185
        sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
        root.geometry(f"{w}x{h}+{(sw - w) // 2}+{(sh - h) // 2}")
        root.lift()
        root.attributes("-topmost", True)
        tk.Label(root, text=APP_NAME, fg="#ffffff", bg="#1e3a5f",
                 font=("Helvetica", 22, "bold")).pack(pady=(36, 6))
        tk.Label(root, text="Starting up…", fg="#94a3b8", bg="#1e3a5f",
                 font=("Helvetica", 11)).pack()
        bar = ttk.Progressbar(root, mode="indeterminate", length=300)
        bar.pack(pady=18)
        bar.start(10)
        root.update()
        return root
    except Exception:
        return None


# ── Gradio server ─────────────────────────────────────────────────────────────
def _start_server():
    import app  # noqa: F401

threading.Thread(target=_start_server, daemon=True).start()

splash = _splash()

import urllib.request as _ur
for _ in range(120):
    try:
        _ur.urlopen(APP_URL, timeout=1)
        break
    except Exception:
        time.sleep(0.5)
        if splash:
            try:
                splash.update()
            except Exception:
                pass

if splash:
    try:
        splash.destroy()
    except Exception:
        pass

# ── System tray ───────────────────────────────────────────────────────────────
_win = None   # pywebview window, set below


def _build_tray():
    import pystray

    tray_img = _draw_icon(64)

    def on_open(icon, item):
        if _win:
            _win.show()

    def on_quit(icon, item):
        icon.stop()
        os._exit(0)

    menu = pystray.Menu(
        pystray.MenuItem("Open " + APP_NAME, on_open, default=True),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Quit", on_quit),
    )
    return pystray.Icon(APP_NAME, tray_img, APP_NAME, menu)


tray = None
try:
    tray = _build_tray()
    threading.Thread(target=tray.run, daemon=True).start()
except Exception:
    pass

# ── Native desktop window (pywebview) ─────────────────────────────────────────
try:
    import webview

    _win = webview.create_window(
        APP_NAME,
        APP_URL,
        width=1280,
        height=900,
        min_size=(900, 600),
    )

    def _on_closing():
        """Hide to tray instead of quitting when the X button is clicked."""
        if tray:
            _win.hide()
            return False   # cancel OS close
        return True

    _win.events.closing += _on_closing

    webview.start(icon=_icon_path())

except ImportError:
    # No pywebview — fall back to browser
    import webbrowser
    webbrowser.open(APP_URL)
    while True:
        time.sleep(1)
