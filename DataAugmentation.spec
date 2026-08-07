# -*- mode: python ; coding: utf-8 -*-

import os
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


def _gdal_datas():
    """Find GDAL installation and return MINIMAL data entries for bundling.
    
    Only bundles required executables, DLLs, and data files (~60MB instead of ~700MB).
    """
    import glob
    
    # Required GDAL executables
    REQUIRED_EXES = [
        "gdalinfo.exe",
        "gdal_translate.exe",
        "gdallocationinfo.exe",
    ]
    
    # Required DLLs (gdal.dll and common dependencies)
    REQUIRED_DLL_PATTERNS = [
        "gdal*.dll",
        "proj*.dll",
        "geos*.dll",
        "sqlite3.dll",
        "tiff*.dll", "libtiff*.dll",
        "jpeg*.dll", "turbojpeg*.dll",
        "png*.dll", "libpng*.dll",
        "zlib*.dll",
        "zstd*.dll", "libzstd*.dll",
        "lzma*.dll", "liblzma*.dll",
        "curl*.dll", "libcurl*.dll",
        "ssl*.dll", "libssl*.dll",
        "crypto*.dll", "libcrypto*.dll",
        "webp*.dll", "libwebp*.dll",
        "openjp*.dll",
        "xml*.dll", "libxml*.dll",
        "expat*.dll", "libexpat*.dll",
        "iconv*.dll",
        "charset*.dll",
        "kea*.dll",
        "hdf*.dll",
        "netcdf*.dll",
        "spatialite*.dll",
        "freexl*.dll",
        "xerces*.dll",
        "pcre*.dll",
        "blosc*.dll",
        "lerc*.dll",
        "deflate*.dll", "libdeflate*.dll",
        "arrow*.dll",
        "brotli*.dll",
        "snappy*.dll",
    ]
    
    def find_gdal_root():
        candidates = []
        
        env_dir = os.environ.get("DATA_AUGMENTATION_GDAL_DIR", "").strip()
        if env_dir:
            candidates.append(env_dir)
        
        conda_prefix = os.environ.get("CONDA_PREFIX", "").strip()
        if conda_prefix:
            candidates.append(os.path.join(conda_prefix, "Library"))
        
        candidates.extend([
            "gdal",
            os.path.join("vendor", "gdal"),
            r"C:\OSGeo4W",
            r"C:\OSGeo4W64",
        ])
        
        for candidate in candidates:
            if candidate and os.path.isdir(candidate):
                bin_dir = os.path.join(candidate, "bin")
                if os.path.isfile(os.path.join(bin_dir, "gdalinfo.exe")):
                    return candidate
        return None
    
    def find_gdal2tiles():
        """Find gdal2tiles.py in conda Scripts or elsewhere."""
        conda_prefix = os.environ.get("CONDA_PREFIX", "").strip()
        if conda_prefix:
            scripts_dir = os.path.join(conda_prefix, "Scripts")
            gdal2tiles = os.path.join(scripts_dir, "gdal2tiles.py")
            if os.path.isfile(gdal2tiles):
                return gdal2tiles
        
        import shutil
        return shutil.which("gdal2tiles.py") or shutil.which("gdal2tiles")
    
    gdal_root = find_gdal_root()
    if not gdal_root:
        raise RuntimeError(
            "GDAL not found. Install GDAL in your conda environment:\n"
            "    conda install -c conda-forge gdal\n"
            "Or set DATA_AUGMENTATION_GDAL_DIR to your GDAL folder."
        )
    
    print(f"[DataAugmentation.spec] Found GDAL at: {gdal_root}")
    
    bin_dir = os.path.join(gdal_root, "bin")
    share_gdal = os.path.join(gdal_root, "share", "gdal")
    share_proj = os.path.join(gdal_root, "share", "proj")
    
    datas = []
    
    # 1. Add required executables
    for exe in REQUIRED_EXES:
        exe_path = os.path.join(bin_dir, exe)
        if os.path.isfile(exe_path):
            datas.append((exe_path, os.path.join("gdal", "bin")))
            print(f"  + {exe}")
        else:
            raise RuntimeError(f"Required GDAL tool not found: {exe_path}")
    
    # 2. Add gdal2tiles.py
    gdal2tiles = find_gdal2tiles()
    if gdal2tiles:
        datas.append((gdal2tiles, os.path.join("gdal", "bin")))
        print(f"  + gdal2tiles.py")
    else:
        print("  ! gdal2tiles.py not found (tiling will fail)")
    
    # 3. Add required DLLs
    dll_count = 0
    added_dlls = set()
    for pattern in REQUIRED_DLL_PATTERNS:
        for dll_path in glob.glob(os.path.join(bin_dir, pattern)):
            dll_name = os.path.basename(dll_path).lower()
            if dll_name not in added_dlls:
                datas.append((dll_path, os.path.join("gdal", "bin")))
                added_dlls.add(dll_name)
                dll_count += 1
    print(f"  + {dll_count} DLLs")
    
    # 4. Add share/gdal data files (coordinate systems, etc.)
    if os.path.isdir(share_gdal):
        datas.append((share_gdal, os.path.join("gdal", "share", "gdal")))
        print(f"  + share/gdal")
    
    # 5. Add share/proj data files (projections)
    if os.path.isdir(share_proj):
        datas.append((share_proj, os.path.join("gdal", "share", "proj")))
        print(f"  + share/proj")
    
    # Calculate approximate bundle size
    total_size = 0
    for src, _ in datas:
        if os.path.isfile(src):
            total_size += os.path.getsize(src)
        elif os.path.isdir(src):
            for root, dirs, files in os.walk(src):
                for f in files:
                    total_size += os.path.getsize(os.path.join(root, f))
    print(f"  = GDAL bundle size: {total_size / (1024*1024):.1f} MB")
    
    return datas


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
    name='DataAugmentation_v2.9a',
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
    name='DataAugmentation_v2.9a',
)
