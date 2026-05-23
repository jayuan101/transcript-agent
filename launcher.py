"""Standalone desktop launcher — native window, system tray, splash screen."""
import sys
import os
import threading
import time
import urllib.request as _ur

# ── PyInstaller path fix ──────────────────────────────────────────────────────
if getattr(sys, "frozen", False):
    _base = sys._MEIPASS
    os.chdir(_base)
    sys.path.insert(0, _base)

_home    = os.path.expanduser("~")
_app_dir = os.path.join(_home, ".transcript_agent")
os.makedirs(_app_dir, exist_ok=True)

os.environ.setdefault("GRADIO_TEMP_DIR",        os.path.join(_app_dir, "tmp"))
os.environ.setdefault("TRANSCRIPT_OUTPUT_DIR",  os.path.join(_home, "TranscriptAgent", "outputs"))

PORT     = int(os.environ.get("GRADIO_SERVER_PORT", "7860"))
APP_URL  = f"http://127.0.0.1:{PORT}"
APP_NAME = "Transcript Agent"

os.environ["GRADIO_SERVER_NAME"]        = "127.0.0.1"
os.environ["GRADIO_SERVER_PORT"]        = str(PORT)
os.environ["TRANSCRIPT_AGENT_WINDOWED"] = "1"

# ── In-window loading page (shown while Gradio starts) ────────────────────────
_LOADING_HTML = """\
<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<style>
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body {
    background: #1e3a5f;
    display: flex; flex-direction: column;
    align-items: center; justify-content: center;
    height: 100vh;
    font-family: -apple-system, Helvetica, sans-serif;
    color: #ffffff;
  }
  h1  { font-size: 2em; font-weight: 700; margin-bottom: 10px; letter-spacing: -0.5px; }
  p   { color: #94a3b8; font-size: 1.05em; }
  .bar-wrap {
    width: 300px; height: 5px;
    background: #2d4a6a; border-radius: 3px;
    margin-top: 28px; overflow: hidden;
  }
  .bar {
    width: 40%; height: 100%;
    background: #3b82f6; border-radius: 3px;
    animation: slide 1.5s ease-in-out infinite;
  }
  @keyframes slide { 0%{margin-left:-40%} 100%{margin-left:100%} }
</style>
</head>
<body>
  <h1>Transcript Agent</h1>
  <p>Starting up&hellip;</p>
  <div class="bar-wrap"><div class="bar"></div></div>
</body>
</html>"""

_ERROR_HTML = """\
<!DOCTYPE html>
<html>
<head><meta charset="utf-8">
<style>
  body { background:#1a0a0a; display:flex; align-items:center; justify-content:center;
         height:100vh; font-family:Helvetica,sans-serif; color:#fff; flex-direction:column; }
  h2 { color:#f87171; margin-bottom:10px; }
  p  { color:#94a3b8; font-size:0.95em; }
</style></head>
<body>
  <h2>Failed to start</h2>
  <p>The server didn't respond in time. Please close and reopen the app.</p>
</body>
</html>"""

# ── Icon ──────────────────────────────────────────────────────────────────────
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


# ── Tkinter splash (shown while pywebview initialises) ────────────────────────
def _show_splash():
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


# ── System tray ───────────────────────────────────────────────────────────────
_win = None


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


# ── Boot sequence ─────────────────────────────────────────────────────────────

# 1. Start Gradio server in background
threading.Thread(target=lambda: __import__("app"), daemon=True).start()

# 2. Show tkinter splash immediately
splash = _show_splash()

# 3. Start tray
tray = None
try:
    tray = _build_tray()
    threading.Thread(target=tray.run, daemon=True).start()
except Exception:
    pass

# 4. Open pywebview with loading HTML — never shows "page can't be reached"
try:
    import webview

    _win = webview.create_window(
        APP_NAME,
        html=_LOADING_HTML,        # loading screen shown immediately
        width=1280,
        height=900,
        min_size=(900, 600),
    )

    def _on_shown():
        """Dismiss tkinter splash the moment the native window appears."""
        if splash:
            try:
                splash.destroy()
            except Exception:
                pass

    def _on_closing():
        """Hide to tray on X — don't quit."""
        if tray:
            _win.hide()
            return False
        return True

    def _navigate_when_ready():
        """Poll until Gradio is up, then swap the loading page for the real app."""
        for _ in range(360):          # up to 3 minutes
            try:
                _ur.urlopen(APP_URL, timeout=1)
                _win.load_url(APP_URL)
                return
            except Exception:
                time.sleep(0.5)
        # Server never came up — show error inside the window
        _win.load_html(_ERROR_HTML)

    _win.events.shown   += _on_shown
    _win.events.closing += _on_closing

    threading.Thread(target=_navigate_when_ready, daemon=True).start()

    webview.start()

except ImportError:
    # No pywebview — fall back to browser
    if splash:
        try:
            splash.destroy()
        except Exception:
            pass
    for _ in range(120):
        try:
            _ur.urlopen(APP_URL, timeout=1)
            break
        except Exception:
            time.sleep(0.5)
    import webbrowser
    webbrowser.open(APP_URL)
    while True:
        time.sleep(1)
