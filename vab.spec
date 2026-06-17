# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec for Visual Asset Browser
#
# Build:  make release-exe   (or: pyinstaller vab.spec --distpath dist)
# Prereq: make build         (populates ui/dist/ first)
#
# Output: dist/vab/vab.exe  (distribute as a zip; models/ dir excluded — downloaded on first run)
# The output directory contains everything; do not move vab.exe out of it.

from PyInstaller.utils.hooks import collect_all, collect_data_files

datas = [
    ('ui/dist', 'ui_dist'),  # pre-built UI; served by FastAPI at runtime
    ('src', '.'),            # Python source modules on sys.path via pathex
    ('.figures.json', '.'),  # static figure-name reference data for ETL
]
binaries = []
hiddenimports = [
    # uvicorn uses string-based dynamic imports for its protocol/loop backends
    'uvicorn.logging',
    'uvicorn.loops.auto',
    'uvicorn.loops.asyncio',
    'uvicorn.protocols.http.auto',
    'uvicorn.protocols.http.h11_impl',
    'uvicorn.protocols.websockets.auto',
    'uvicorn.lifespan.on',
    'uvicorn.lifespan.off',
    # pydantic v1 shim used internally by several packages
    'pydantic.v1',
    # fastapi/starlette submodules not auto-detected by PyInstaller
    'fastapi.staticfiles',
    'starlette.staticfiles',
    'starlette.middleware.cors',
    'starlette.responses',
    # aiofiles is required by starlette StaticFiles for async file serving
    'aiofiles',
    'aiofiles.os',
    'aiofiles.threadpool',
]

# transformers and optimum are only needed for the one-time ONNX export
# (export_model.py), never at server runtime. Excluding them removes ~2.8 GB
# of torch + transformers from the bundle.
for pkg in ('chromadb', 'onnxruntime', 'tokenizers'):
    d, b, h = collect_all(pkg)
    datas += d
    binaries += b
    hiddenimports += h

datas += collect_data_files('huggingface_hub')

a = Analysis(
    ['vab.py'],
    pathex=['src'],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        'torch', 'torchvision', 'torchaudio',
        'transformers', 'optimum',
        'bitsandbytes',
        'datasets', 'hf_xet',
        'tensorflow', 'jax',
    ],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='vab',
    debug=False,
    strip=False,
    upx=True,
    console=True,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='vab',
)
