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

# ── Single-instance guard ─────────────────────────────────────────────────────
_is_first_instance = True
if sys.platform == "win32":
    import ctypes as _ct
    _mutex = _ct.windll.kernel32.CreateMutexW(None, False, "TranscriptAgent_SingleInstance")
    if _ct.windll.kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
        _is_first_instance = False

if not _is_first_instance:
    # Another copy is already running — open it in the browser and exit
    import webbrowser
    try:
        _ur.urlopen(APP_URL, timeout=2)
        webbrowser.open(APP_URL)
    except Exception:
        webbrowser.open(APP_URL)
    sys.exit(0)

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
def _start_server():
    import app
    app.main()

threading.Thread(target=_start_server, daemon=True).start()

# 2. Start tray
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

    def _on_closing():
        """Hide to tray on X — don't quit."""
        if tray:
            _win.hide()
            return False
        return True

    def _navigate_when_ready():
        """Poll until Gradio is up, then swap the loading page for the real app."""
        start = time.time()
        for _ in range(600):          # up to 5 minutes
            try:
                _ur.urlopen(APP_URL, timeout=1)
                _win.load_url(APP_URL)
                return
            except Exception:
                elapsed = int(time.time() - start)
                try:
                    _win.evaluate_js(
                        f"document.querySelector('p') && "
                        f"(document.querySelector('p').textContent = 'Starting up\\u2026 {elapsed}s')"
                    )
                except Exception:
                    pass
                time.sleep(0.5)
        _win.load_html(_ERROR_HTML)

    _win.events.closing += _on_closing

    threading.Thread(target=_navigate_when_ready, daemon=True).start()

    webview.start()

except ImportError:
    # No pywebview — fall back to browser
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
