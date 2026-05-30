# -*- mode: python ; coding: utf-8 -*-
"""
Transcript Agent — PyInstaller spec
Build: pyinstaller TranscriptAgent.spec
Output: dist/TranscriptAgent/  (onedir — fast startup, no extraction delay)
"""

import sys
from pathlib import Path

block_cipher = None
HERE = Path(SPECPATH)

a = Analysis(
    [str(HERE / 'launcher.py')],
    pathex=[str(HERE)],
    binaries=[],
    datas=[
        # Bundle the main application files
        (str(HERE / 'app.py'),              '.'),
        (str(HERE / 'transcript_agent.py'), '.'),
        (str(HERE / 'CHANGELOG.md'),        '.'),
        # Bundle setup scripts for the update flow
        (str(HERE / 'setup_windows.bat'),   '.'),
        (str(HERE / 'setup_mac.sh'),        '.'),
        (str(HERE / 'run.bat'),             '.'),
    ],
    hiddenimports=[
        # Gradio and web framework
        'gradio', 'gradio.themes', 'gradio.themes.soft',
        'fastapi', 'uvicorn', 'uvicorn.logging', 'uvicorn.loops',
        'uvicorn.loops.auto', 'uvicorn.protocols',
        'uvicorn.protocols.http', 'uvicorn.protocols.http.auto',
        'starlette', 'starlette.middleware',
        'python_multipart', 'aiofiles', 'httpx',
        # AI providers
        'anthropic', 'openai', 'groq',
        # Document processing
        'pdfplumber', 'fpdf2', 'docx', 'python_docx',
        # Cloud STT (optional — lazy-imported)
        'deepgram', 'assemblyai', 'elevenlabs', 'rev_ai',
        # Utilities
        'PIL', 'PIL.Image', 'requests', 'urllib3',
        'packaging', 'typing_extensions',
        'orjson', 'anyio', 'sniffio',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # Exclude heavy ML deps — installed separately by setup scripts
        # Users who want local Whisper run setup_windows.bat / setup_mac.sh first
        'torch', 'torchvision', 'torchaudio',
        'whisper', 'openai_whisper',
        'numpy', 'scipy', 'sklearn',
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
            'CFBundleVersion':           '3.48',
            'CFBundleShortVersionString':'3.48',
            'NSHighResolutionCapable':   True,
            'LSBackgroundOnly':          False,
        },
    )
