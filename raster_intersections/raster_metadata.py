import json
import subprocess
from pathlib import Path
from typing import Any, Dict, Optional

from .utils import find_gdal_tool


def read_raster_metadata(raster_path: Path, layer: Optional[Dict[str, Any]] = None, band_index: int = 1) -> Dict[str, Any]:
    try:
        import rasterio
    except Exception:
        return _read_with_gdalinfo(raster_path, layer, band_index)

    with rasterio.open(raster_path) as dataset:
        if not dataset.crs:
            raise ValueError("Raster CRS is missing. Reproject the raster or upload a GeoTIFF with a CRS.")
        if band_index < 1 or band_index > dataset.count:
            raise ValueError(f"Band {band_index} is not available in this raster.")

        nodata_values = list(dataset.nodatavals or [])
        descriptions = list(dataset.descriptions or [])
        bands = []
        for index in range(1, dataset.count + 1):
            description = descriptions[index - 1] if index - 1 < len(descriptions) else ""
            nodata = nodata_values[index - 1] if index - 1 < len(nodata_values) else None
            bands.append({
                "index": index,
                "name": description or f"Band {index}",
                "nodata": nodata,
            })

        extent = (layer or {}).get("extent")
        return {
            "path": str(raster_path),
            "crs": dataset.crs.to_string(),
            "band_index": band_index,
            "band_count": dataset.count,
            "nodata": nodata_values[band_index - 1] if band_index - 1 < len(nodata_values) else None,
            "bands": bands,
            "extent": extent,
            "width": int(dataset.width),
            "height": int(dataset.height),
        }


def _read_with_gdalinfo(raster_path: Path, layer: Optional[Dict[str, Any]], band_index: int) -> Dict[str, Any]:
    gdalinfo, env = find_gdal_tool("gdalinfo")
    if not gdalinfo:
        raise ValueError("Raster metadata requires rasterio or bundled GDAL gdalinfo.")

    result = subprocess.run(
        [gdalinfo, "-json", str(raster_path)],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )
    info = json.loads(result.stdout)
    bands_raw = info.get("bands") or []
    if band_index < 1 or band_index > max(1, len(bands_raw)):
        raise ValueError(f"Band {band_index} is not available in this raster.")

    coordinate_system = info.get("coordinateSystem") or {}
    crs = coordinate_system.get("wkt") or (layer or {}).get("crs")
    if not crs or crs == "unknown":
        raise ValueError("Raster CRS is missing. Reproject the raster or upload a GeoTIFF with a CRS.")

    bands = []
    for index, band in enumerate(bands_raw, start=1):
        bands.append({
            "index": index,
            "name": str(band.get("description") or f"Band {index}"),
            "nodata": band.get("noDataValue"),
        })

    selected_band = bands_raw[band_index - 1] if bands_raw else {}
    size = info.get("size") or [None, None]
    return {
        "path": str(raster_path),
        "crs": crs,
        "band_index": band_index,
        "band_count": len(bands) or 1,
        "nodata": selected_band.get("noDataValue"),
        "bands": bands or [{"index": 1, "name": "Band 1", "nodata": None}],
        "extent": (layer or {}).get("extent"),
        "width": int(size[0] or 0),
        "height": int(size[1] or 0),
    }
