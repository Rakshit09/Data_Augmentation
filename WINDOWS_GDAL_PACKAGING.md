# Bundling GDAL For Windows EXE Builds

End users should not install GDAL themselves. Bundle GDAL into the PyInstaller distribution.

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

`DataAugmentation.spec` will automatically include `gdal` or `vendor/gdal` in the app bundle. At runtime, `layer_upload_routes.py` searches:

1. `DATA_AUGMENTATION_GDAL_DIR`
2. bundled `gdal` inside the PyInstaller app
3. `gdal` beside the executable
4. `vendor/gdal`
5. system `PATH`

## User Experience

Users do not need to know about GDAL. They only upload a GeoTIFF in **Building Lookup > Add Layer**. If GDAL was bundled correctly, the app tiles and renders the raster automatically, and raster intersections can sample exposure points or building centroids without extra user setup.
