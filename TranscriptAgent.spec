# -*- mode: python ; coding: utf-8 -*-
"""
Transcript Agent — PyInstaller spec
Build: pyinstaller TranscriptAgent.spec
Output: dist/TranscriptAgent/  (onedir — fast startup, no extraction delay)

Version: bump APP_VERSION here — it propagates to Mac .app bundle info.
"""

import sys
from pathlib import Path
from PyInstaller.utils.hooks import collect_data_files, collect_all

APP_VERSION = "1.1.5"

block_cipher = None
HERE = Path(SPECPATH)

# Collect data files from packages that embed resources at runtime
_gradio_datas,    _gradio_bins,    _gradio_hidden    = collect_all('gradio')
_safehttpx_datas, _safehttpx_bins, _safehttpx_hidden = collect_all('safehttpx')

a = Analysis(
    [str(HERE / 'launcher.py')],
    pathex=[str(HERE)],
    binaries=[] + _gradio_bins + _safehttpx_bins,
    datas=[
        # Bundle the main application files
        (str(HERE / 'app.py'),              '.'),
        (str(HERE / 'transcript_agent.py'), '.'),
        (str(HERE / 'CHANGELOG.md'),        '.'),
        # Bundle setup scripts for the update flow
        (str(HERE / 'setup_windows.bat'),   '.'),
        (str(HERE / 'setup_mac.sh'),        '.'),
        (str(HERE / 'run.bat'),             '.'),
    ] + _gradio_datas + _safehttpx_datas,
    hiddenimports=[
        # Gradio and web framework
        'gradio', 'gradio.themes', 'gradio.themes.soft',
        'gradio._vendor.aiofiles', 'gradio._vendor.aiofiles.base',
        'gradio._vendor.aiofiles.os', 'gradio._vendor.aiofiles.threadpool',
        'fastapi', 'uvicorn', 'uvicorn.logging', 'uvicorn.loops',
        'uvicorn.loops.auto', 'uvicorn.loops.asyncio',
        'uvicorn.protocols', 'uvicorn.protocols.http',
        'uvicorn.protocols.http.auto', 'uvicorn.protocols.http.h11_impl',
        'uvicorn.protocols.websockets', 'uvicorn.protocols.websockets.auto',
        'uvicorn.protocols.websockets.websockets_impl',
        'starlette', 'starlette.middleware', 'starlette.middleware.cors',
        'python_multipart', 'httpx',
        'exceptiongroup',           # anyio/starlette on Python < 3.11 path
        'watchfiles',               # uvicorn reload
        # AI providers
        'anthropic', 'openai', 'groq',
        # Document processing
        'pdfplumber', 'fpdf',       # fpdf2 package imports as 'fpdf'
        'docx',                     # python-docx imports as 'docx'
        # Cloud STT (optional — lazy-imported)
        'deepgram', 'assemblyai', 'elevenlabs', 'rev_ai',
        # Utilities
        'numpy', 'numpy.core', 'numpy.lib',
        'safehttpx',
        'PIL', 'PIL.Image', 'requests', 'urllib3',
        'packaging', 'typing_extensions',
        'orjson', 'anyio', 'sniffio',
        'multiprocessing',
    ] + _gradio_hidden + _safehttpx_hidden,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # Exclude heavy ML deps — installed separately by setup scripts
        # Users who want local Whisper run setup_windows.bat / setup_mac.sh first
        'torch', 'torchvision', 'torchaudio',
        'whisper', 'openai_whisper',
        # numpy is intentionally NOT excluded — gradio requires it
        'scipy', 'sklearn',
        'matplotlib', 'pandas',
        # Test/dev tooling
        'pytest', 'IPython', 'notebook',
        'tkinter',  # not needed for Gradio web UI
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
    exclude_binaries=True,          # onedir mode — no extraction needed = FAST
    name='TranscriptAgent',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,                  # no terminal window
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,                      # add icon.ico / icon.icns here if available
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='TranscriptAgent',
)

# ── Mac .app bundle ────────────────────────────────────────────────────────────
if sys.platform == 'darwin':
    app = BUNDLE(
        coll,
        name='TranscriptAgent.app',
        icon=None,
        bundle_identifier='com.transcriptagent.app',
        info_plist={
            'CFBundleName':              'Transcript Agent',
            'CFBundleDisplayName':       'Transcript Agent',
            'CFBundleVersion':           APP_VERSION,
            'CFBundleShortVersionString':APP_VERSION,
            'NSHighResolutionCapable':   True,
            'LSBackgroundOnly':          False,
        },
    )
