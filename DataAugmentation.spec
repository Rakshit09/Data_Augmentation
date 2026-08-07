# -*- mode: python ; coding: utf-8 -*-

import os
import sys
import duckdb as _ddb

_ddb_version = _ddb.__version__
_ext_path = os.path.expanduser(
    f"~/.duckdb/extensions/v{_ddb_version}/windows_amd64/spatial.duckdb_extension"
)
_excel_ext_path = os.path.expanduser(
    f"~/.duckdb/extensions/v{_ddb_version}/windows_amd64/excel.duckdb_extension"
)
_extra_datas = []
if os.path.isfile(_ext_path):
    _extra_datas.append((_ext_path, "duckdb_extensions"))
if os.path.isfile(_excel_ext_path):
    _extra_datas.append((_excel_ext_path, "duckdb_extensions"))
_country_catalog_zip = os.path.join("etl_output", "boundary", "ne_10m_admin_0_countries.zip")
_boundary_datas = [(_country_catalog_zip, os.path.join("etl_output", "boundary"))] if os.path.isfile(_country_catalog_zip) else []
_readme_path = "README.md"
_readme_datas = [(_readme_path, ".")] if os.path.isfile(_readme_path) else []


def _unique_existing_dirs(paths):
    seen = set()
    existing = []
    for path in paths:
        if not path:
            continue
        normalized = os.path.abspath(path)
        if normalized in seen or not os.path.isdir(normalized):
            continue
        seen.add(normalized)
        existing.append(normalized)
    return existing


def _first_existing_file(paths):
    for path in paths:
        if path and os.path.isfile(path):
            return path
    return ""


def _first_existing_dir(paths):
    for path in paths:
        if path and os.path.isdir(path):
            return path
    return ""


def _candidate_gdal_roots():
    candidates = []
    env_dir = os.environ.get("DATA_AUGMENTATION_GDAL_DIR", "").strip()
    if env_dir:
        candidates.append(env_dir)

    conda_prefix = os.environ.get("CONDA_PREFIX", "").strip()
    if conda_prefix:
        candidates.append(conda_prefix)

    python_root = os.path.dirname(sys.executable)
    if python_root:
        candidates.append(python_root)

    candidates.extend([
        "gdal",
        os.path.join("vendor", "gdal"),
        r"C:\OSGeo4W",
        r"C:\OSGeo4W64",
    ])

    return _unique_existing_dirs(candidates)


def _gdal_datas_for_root(root):
    bin_dir = _first_existing_dir([
        os.path.join(root, "bin"),
        os.path.join(root, "Library", "bin"),
        root,
    ])
    if not bin_dir:
        return []

    gdalinfo = _first_existing_file([
        os.path.join(bin_dir, "gdalinfo.exe"),
        os.path.join(bin_dir, "gdalinfo"),
    ])
    gdal_translate = _first_existing_file([
        os.path.join(bin_dir, "gdal_translate.exe"),
        os.path.join(bin_dir, "gdal_translate"),
    ])
    gdal2tiles = _first_existing_file([
        os.path.join(bin_dir, "gdal2tiles.exe"),
        os.path.join(bin_dir, "gdal2tiles.bat"),
        os.path.join(bin_dir, "gdal2tiles.py"),
        os.path.join(bin_dir, "gdal2tiles"),
        os.path.join(root, "Scripts", "gdal2tiles.py"),
    ])

    if not gdalinfo or not gdal_translate or not gdal2tiles:
        return []

    datas = [(bin_dir, os.path.join("gdal", "bin"))]
    gdal2tiles_dir = os.path.dirname(gdal2tiles)
    if gdal2tiles_dir != bin_dir:
        datas.append((gdal2tiles, os.path.join("gdal", "bin")))

    gdal_data = _first_existing_dir([
        os.path.join(root, "share", "gdal"),
        os.path.join(root, "data"),
        os.path.join(root, "Library", "share", "gdal"),
    ])
    proj_data = _first_existing_dir([
        os.path.join(root, "share", "proj"),
        os.path.join(root, "projlib"),
        os.path.join(root, "Library", "share", "proj"),
    ])

    if gdal_data:
        datas.append((gdal_data, os.path.join("gdal", "share", "gdal")))
    if proj_data:
        datas.append((proj_data, os.path.join("gdal", "share", "proj")))

    return datas


def _gdal_datas():
    for candidate in _candidate_gdal_roots():
        datas = _gdal_datas_for_root(candidate)
        if datas:
            return datas

    raise SystemExit(
        "GDAL runtime not found for Windows packaging. "
        "Install GDAL in the active build environment, set DATA_AUGMENTATION_GDAL_DIR, "
        "or stage a bundle under vendor/gdal before running PyInstaller."
    )


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
        'openpyxl',
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
    name='DataAugmentation_v2.8b',
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
    name='DataAugmentation_v2.8b',
)
