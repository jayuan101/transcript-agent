# -*- mode: python ; coding: utf-8 -*-
"""
Transcript Agent (New UI) — PyInstaller spec
Build: pyinstaller TranscriptAgentUI.spec
Output: dist/TranscriptAgentUI/  (onedir — fast startup, no extraction delay)

Packages the React/PrimeReact UI (frontend/dist) + FastAPI backend (api.py)
as a standalone desktop app. Build the frontend first:
    cd frontend && npm run build

Version: bump APP_VERSION in app.py — it propagates to Mac .app bundle info.
"""

import re, sys
from pathlib import Path
from PyInstaller.utils.hooks import collect_data_files, collect_all

HERE = Path(SPECPATH)
APP_VERSION = re.search(r'APP_VERSION\s*=\s*"([^"]+)"', (HERE / "app.py").read_text(encoding="utf-8")).group(1)

block_cipher = None

_mediapipe_datas, _mediapipe_bins, _mediapipe_hidden = collect_all('mediapipe')
_cv2_datas,       _cv2_bins,       _cv2_hidden       = collect_all('cv2')
_imageio_datas,   _imageio_bins,   _imageio_hidden   = collect_all('imageio_ffmpeg')

# Bundle the built React UI (frontend/dist) as data files
_FRONTEND_DIST = HERE / "frontend" / "dist"
_frontend_datas = []
if _FRONTEND_DIST.is_dir():
    for f in _FRONTEND_DIST.rglob("*"):
        if f.is_file():
            rel_dir = f.parent.relative_to(HERE)
            _frontend_datas.append((str(f), str(rel_dir)))

a = Analysis(
    [str(HERE / 'launcher_new_ui.py')],
    pathex=[str(HERE)],
    binaries=[] + _mediapipe_bins + _cv2_bins + _imageio_bins,
    datas=[
        (str(HERE / 'app.py'),              '.'),
        (str(HERE / 'transcript_agent.py'), '.'),
        (str(HERE / 'video_analyzer.py'),   '.'),
        (str(HERE / 'api.py'),              '.'),
        (str(HERE / 'CHANGELOG.md'),        '.'),
    ] + _mediapipe_datas + _cv2_datas + _imageio_datas + _frontend_datas,
    hiddenimports=[
        'fastapi', 'fastapi.middleware', 'fastapi.middleware.cors',
        'fastapi.staticfiles', 'fastapi.responses',
        'uvicorn', 'uvicorn.logging', 'uvicorn.loops',
        'uvicorn.loops.auto', 'uvicorn.loops.asyncio',
        'uvicorn.protocols', 'uvicorn.protocols.http',
        'uvicorn.protocols.http.auto', 'uvicorn.protocols.http.h11_impl',
        'uvicorn.protocols.websockets', 'uvicorn.protocols.websockets.auto',
        'uvicorn.protocols.websockets.websockets_impl',
        'starlette', 'starlette.middleware', 'starlette.middleware.cors',
        'python_multipart', 'httpx',
        'exceptiongroup',
        'anthropic', 'openai', 'groq',
        'pdfplumber', 'fpdf',
        'docx',
        'deepgram', 'assemblyai', 'elevenlabs', 'rev_ai',
        'numpy', 'numpy.core', 'numpy.lib',
        'pandas', 'pandas.core', 'pandas.io',
        'PIL', 'PIL.Image', 'requests', 'urllib3',
        'imageio_ffmpeg',
        'packaging', 'typing_extensions',
        'orjson', 'anyio', 'sniffio',
        'multiprocessing',
        'cv2', 'mediapipe', 'mediapipe.tasks', 'mediapipe.tasks.python',
        'mediapipe.tasks.python.vision',
    ] + _mediapipe_hidden + _cv2_hidden,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        'torch', 'torchvision', 'torchaudio',
        'whisper', 'openai_whisper',
        'scipy', 'sklearn', 'matplotlib',
        'pytest', 'IPython', 'notebook',
        'tkinter',
        'gradio', 'gradio_client', 'safehttpx', 'groovy',
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='TranscriptAgentUI',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon='icon.ico' if sys.platform == 'win32' else 'icon.icns',
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='TranscriptAgentUI',
)

if sys.platform == 'darwin':
    app = BUNDLE(
        coll,
        name='TranscriptAgentUI.app',
        icon='icon.icns',
        bundle_identifier='com.transcriptagent.uiapp',
        info_plist={
            'CFBundleName':              'Transcript Agent UI',
            'CFBundleDisplayName':       'Transcript Agent UI',
            'CFBundleVersion':           APP_VERSION,
            'CFBundleShortVersionString':APP_VERSION,
            'NSHighResolutionCapable':   True,
            'LSBackgroundOnly':          False,
        },
    )
