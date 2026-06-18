# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec for the one-time model export tool.
#
# Build: make release-export-exe   (or: pyinstaller vab_export.spec --distpath dist)
#
# This bundle intentionally includes torch + transformers + optimum so the
# user does not need a Python environment to export models.  The result is
# significantly larger (~1-2 GB) than the main vab bundle, which is why this
# is a separate binary rather than a subcommand of vab.exe.
#
# Output: dist/vab_export/vab_export.exe

from PyInstaller.utils.hooks import collect_all, collect_data_files

datas = []
binaries = []
hiddenimports = []

for pkg in ('torch', 'transformers', 'optimum', 'onnx', 'onnxruntime', 'tokenizers', 'huggingface_hub'):
    d, b, h = collect_all(pkg)
    datas += d
    binaries += b
    hiddenimports += h

datas += collect_data_files('huggingface_hub')
datas += collect_data_files('transformers')

a = Analysis(
    ['vab_export.py'],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='vab_export',
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
    name='vab_export',
)
