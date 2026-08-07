# Bundling GDAL For Windows EXE Builds

End users should not install GDAL themselves. Bundle GDAL into the PyInstaller distribution.

The build should now fail immediately if it cannot find a bundleable GDAL runtime. That is intentional: it prevents shipping an EXE that only works on the packager's machine.

## Recommended Folder Layout

Before running PyInstaller, put a Windows GDAL runtime in one of these locations:

```text
vendor/gdal/
```

or:

```text
gdal/
```

The app will also honor:

```text
DATA_AUGMENTATION_GDAL_DIR=C:\path\to\gdal
```

or install GDAL into the build environment:

```text
conda install -n augment -c conda-forge gdal
```

The folder should contain GDAL executables in one of these layouts:

```text
vendor/gdal/bin/gdalinfo.exe
vendor/gdal/bin/gdal_translate.exe
vendor/gdal/bin/gdallocationinfo.exe
vendor/gdal/bin/gdal2tiles.py
vendor/gdal/share/gdal/
vendor/gdal/share/proj/
```

or OSGeo4W-style:

```text
vendor/gdal/bin/gdalinfo.exe
vendor/gdal/bin/gdal_translate.exe
vendor/gdal/bin/gdallocationinfo.exe
vendor/gdal/bin/gdal2tiles.bat
vendor/gdal/share/gdal/
vendor/gdal/share/proj/
```

## Build

```text
pyinstaller DataAugmentation.spec --clean --noconfirm
```

`DataAugmentation.spec` now searches these build-time sources in order and stops with an error if none of them contain the required GDAL tools:

1. `DATA_AUGMENTATION_GDAL_DIR`
2. active Conda environment (`CONDA_PREFIX`)
3. active Python environment root
4. `gdal`
5. `vendor/gdal`
6. `C:\OSGeo4W`
7. `C:\OSGeo4W64`

If a source is found, the build bundles the GDAL executables and data under `_internal/gdal`. At runtime, `layer_upload_routes.py` searches:

1. `DATA_AUGMENTATION_GDAL_DIR`
2. bundled `gdal` inside the PyInstaller app
3. `gdal` beside the executable
4. `vendor/gdal`
5. system `PATH`

Before distributing a zip, verify that this folder exists:

```text
dist/DataAugmentation_v*/_internal/gdal/
```

## User Experience

Users do not need to know about GDAL. They only upload a GeoTIFF in **Building Lookup > Add Layer**. If GDAL was bundled correctly, the app tiles and renders the raster automatically, and raster intersections can sample exposure points or building centroids without extra user setup.
