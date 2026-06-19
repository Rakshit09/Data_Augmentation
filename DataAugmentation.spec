# -*- mode: python ; coding: utf-8 -*-

import os
import duckdb as _ddb

_ddb_version = _ddb.__version__
_ext_path = os.path.expanduser(
    f"~/.duckdb/extensions/v{_ddb_version}/windows_amd64/spatial.duckdb_extension"
)
_extra_datas = [(_ext_path, "duckdb_extensions")] if os.path.isfile(_ext_path) else []


a = Analysis(
    ['run_app.py'],
    pathex=[],
    binaries=[],
    datas=[('templates', 'templates'), ('static', 'static'), ('favicon.ico', '.')] + _extra_datas,
    
    hiddenimports=[
        'duckdb',
        'psutil',
        'pandas',
        'pyarrow',
        'pyarrow.parquet',
        'waitress',
        'tkinter',
        'building_lookup_app',
        'enrichment_worker',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='DataAugmentation_v2.0',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=['favicon.ico'],
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='DataAugmentation_v2.0',
)
