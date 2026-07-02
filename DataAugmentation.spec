# -*- mode: python ; coding: utf-8 -*-

import os
import duckdb as _ddb

_ddb_version = _ddb.__version__
_ext_path = os.path.expanduser(
    f"~/.duckdb/extensions/v{_ddb_version}/windows_amd64/spatial.duckdb_extension"
)
_extra_datas = [(_ext_path, "duckdb_extensions")] if os.path.isfile(_ext_path) else []
_country_catalog_zip = os.path.join("etl_output", "boundary", "ne_10m_admin_0_countries.zip")
_boundary_datas = [(_country_catalog_zip, os.path.join("etl_output", "boundary"))] if os.path.isfile(_country_catalog_zip) else []
_readme_path = "README.md"
_readme_datas = [(_readme_path, ".")] if os.path.isfile(_readme_path) else []


def _gdal_datas():
    candidates = []
    env_dir = os.environ.get("DATA_AUGMENTATION_GDAL_DIR", "").strip()
    if env_dir:
        candidates.append(env_dir)

    candidates.extend([
        "gdal",
        os.path.join("vendor", "gdal"),
        r"C:\OSGeo4W",
    ])

    for candidate in candidates:
        if candidate and os.path.isdir(candidate):
            return [(candidate, "gdal")]

    return []


def _filter_packaged_datas(entries):
    filtered = []
    blocked_suffixes = (".parquet", ".duckdb")
    for source, target in entries:
        lower_source = str(source).lower()
        if lower_source.endswith(blocked_suffixes):
            continue
        filtered.append((source, target))
    return filtered


_packaged_datas = _filter_packaged_datas([
    ('templates', 'templates'),
    ('static', 'static'),
    ('favicon.ico', '.'),
] + _extra_datas + _boundary_datas + _readme_datas + _gdal_datas())


a = Analysis(
    ['run_app.py'],
    pathex=[],
    binaries=[],
    datas=_packaged_datas,
    
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
        'layer_upload_routes',
        'raster_intersections',
        'raster_intersections.routes',
        'raster_intersections.raster_metadata',
        'raster_intersections.sampling',
        'raster_intersections.duckdb_queries',
        'raster_intersections.results',
        'raster_intersections.exports',
        'raster_intersections.utils',
        'rasterio',
        'pyproj',
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
    name='DataAugmentation_v2.3',
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
    name='DataAugmentation_v2.3',
)
