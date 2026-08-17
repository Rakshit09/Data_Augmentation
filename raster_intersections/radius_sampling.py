import math
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

from .utils import safe_json_value, threshold_matches


DEFAULT_RADIUS_AGGREGATION = "mean"
SUPPORTED_RADIUS_AGGREGATIONS = {"mean", "max", "min"}
DEFAULT_RADIUS_CHUNK_SIZE = 1000


def sample_candidates_with_radius(
    raster_path: Path,
    candidates: List[Dict[str, Any]],
    metadata: Dict[str, Any],
    threshold: Optional[float],
    threshold_operator: str,
    radius_m: float,
    aggregation: str = DEFAULT_RADIUS_AGGREGATION,
    chunk_size: int = DEFAULT_RADIUS_CHUNK_SIZE,
) -> List[Dict[str, Any]]:
    values = raw_radius_sample_values(
        raster_path=raster_path,
        candidates=candidates,
        metadata=metadata,
        radius_m=radius_m,
        aggregation=aggregation,
        chunk_size=chunk_size,
    )

    rows: List[Dict[str, Any]] = []
    for candidate, value in zip(candidates, values):
        if value is None or not threshold_matches(value, threshold, threshold_operator):
            continue
        rows.append(_sampled_row(candidate, value))
    return rows


def raw_radius_sample_values(
    raster_path: Path,
    candidates: List[Dict[str, Any]],
    metadata: Dict[str, Any],
    radius_m: float,
    aggregation: str = DEFAULT_RADIUS_AGGREGATION,
    chunk_size: int = DEFAULT_RADIUS_CHUNK_SIZE,
) -> List[Optional[float]]:
    if not candidates:
        return []

    radius_m = _validated_radius(radius_m)
    aggregation = _validated_aggregation(aggregation)

    try:
        import rasterio
        from pyproj import CRS, Transformer
    except Exception as exc:
        raise ImportError(str(exc)) from exc

    band_index = int(metadata.get("band_index") or 1)
    results: List[Optional[float]] = []

    with rasterio.open(raster_path) as dataset:
        if not dataset.crs:
            raise ValueError("Raster CRS is missing. Reproject the raster or upload a GeoTIFF with a CRS.")

        dataset_crs = CRS.from_user_input(dataset.crs)
        transformer = None
        if dataset.crs.to_string() not in {"EPSG:4326", "OGC:CRS84"}:
            transformer = Transformer.from_crs("EPSG:4326", dataset.crs, always_xy=True)

        radius_context = _build_radius_context(dataset_crs)
        nodata = dataset.nodatavals[band_index - 1] if dataset.nodatavals else metadata.get("nodata")

        for start in range(0, len(candidates), max(1, int(chunk_size or DEFAULT_RADIUS_CHUNK_SIZE))):
            chunk = candidates[start:start + max(1, int(chunk_size or DEFAULT_RADIUS_CHUNK_SIZE))]
            lon_lat = [(float(row["lon"]), float(row["lat"])) for row in chunk]
            if transformer:
                xs, ys = transformer.transform(
                    [coord[0] for coord in lon_lat],
                    [coord[1] for coord in lon_lat],
                )
                sample_coords = list(zip(xs, ys))
            else:
                sample_coords = lon_lat

            for x, y in sample_coords:
                results.append(
                    _radius_sample_value(
                        dataset=dataset,
                        band_index=band_index,
                        x=float(x),
                        y=float(y),
                        radius_m=radius_m,
                        nodata=nodata,
                        aggregation=aggregation,
                        radius_context=radius_context,
                    )
                )

    return results


def _radius_sample_value(
    dataset: Any,
    band_index: int,
    x: float,
    y: float,
    radius_m: float,
    nodata: Any,
    aggregation: str,
    radius_context: Dict[str, Any],
) -> Optional[float]:
    transform = dataset.transform
    width = int(dataset.width)
    height = int(dataset.height)

    if radius_context["mode"] == "geographic":
        meters_per_lon, meters_per_lat = _meters_per_degree(y)
        if meters_per_lon <= 0 or meters_per_lat <= 0:
            return None
        radius_x = radius_m / meters_per_lon
        radius_y = radius_m / meters_per_lat
    else:
        unit_to_meters = float(radius_context.get("unit_to_meters") or 1.0)
        radius_x = radius_m / unit_to_meters
        radius_y = radius_x

    window = _window_for_bounds(
        transform=transform,
        width=width,
        height=height,
        left=x - radius_x,
        bottom=y - radius_y,
        right=x + radius_x,
        top=y + radius_y,
    )
    if window is None:
        return None

    try:
        data = dataset.read(band_index, window=window, masked=True)
    except Exception:
        return None

    if data is None or not getattr(data, "size", 0):
        return None

    if np.ma.isMaskedArray(data):
        values = np.asarray(data.data)
        valid = ~np.ma.getmaskarray(data)
    else:
        values = np.asarray(data)
        valid = np.ones(values.shape, dtype=bool)

    if not values.size:
        return None

    if nodata not in (None, ""):
        try:
            nodata_value = float(nodata)
            if math.isfinite(nodata_value):
                valid &= values != nodata_value
        except (TypeError, ValueError):
            pass

    if np.issubdtype(values.dtype, np.floating):
        valid &= np.isfinite(values)

    px, py = _pixel_centres(transform, int(window.col_off), int(window.row_off), int(window.width), int(window.height))
    if radius_context["mode"] == "geographic":
        meters_per_lon, meters_per_lat = _meters_per_degree(y)
        dx = (px - x) * meters_per_lon
        dy = (py - y) * meters_per_lat
    else:
        dx = px - x
        dy = py - y
    valid &= (dx * dx + dy * dy) <= (radius_m * radius_m)

    if not valid.any():
        return None

    selected = values[valid].astype(np.float64, copy=False)
    if not selected.size:
        return None

    if aggregation == "max":
        result = float(np.nanmax(selected))
    elif aggregation == "min":
        result = float(np.nanmin(selected))
    else:
        result = float(np.nanmean(selected))
    if not math.isfinite(result):
        return None
    return result


def _build_radius_context(crs: Any) -> Dict[str, Any]:
    if getattr(crs, "is_geographic", False):
        return {"mode": "geographic"}

    unit_to_meters = 1.0
    axis_info = list(getattr(crs, "axis_info", []) or [])
    if axis_info:
        try:
            candidate = float(getattr(axis_info[0], "unit_conversion_factor", 1.0) or 1.0)
            if math.isfinite(candidate) and candidate > 0:
                unit_to_meters = candidate
        except (TypeError, ValueError):
            pass
    return {"mode": "projected", "unit_to_meters": unit_to_meters}


def _window_for_bounds(
    transform: Any,
    width: int,
    height: int,
    left: float,
    bottom: float,
    right: float,
    top: float,
) -> Optional[Any]:
    from rasterio.windows import Window

    inverse = ~transform
    col_left, row_top = inverse * (left, top)
    col_right, row_bottom = inverse * (right, bottom)

    col_off = max(0, int(math.floor(min(col_left, col_right))))
    row_off = max(0, int(math.floor(min(row_top, row_bottom))))
    col_end = min(width, int(math.ceil(max(col_left, col_right))))
    row_end = min(height, int(math.ceil(max(row_top, row_bottom))))
    if col_end <= col_off or row_end <= row_off:
        return None
    return Window(col_off=col_off, row_off=row_off, width=col_end - col_off, height=row_end - row_off)


def _pixel_centres(transform: Any, col_off: int, row_off: int, width: int, height: int) -> Tuple[np.ndarray, np.ndarray]:
    col_numbers = col_off + np.arange(width, dtype=np.float64) + 0.5
    row_numbers = row_off + np.arange(height, dtype=np.float64) + 0.5
    px = transform.c + col_numbers[np.newaxis, :] * transform.a + row_numbers[:, np.newaxis] * transform.b
    py = transform.f + col_numbers[np.newaxis, :] * transform.d + row_numbers[:, np.newaxis] * transform.e
    return px, py


def _meters_per_degree(latitude: float) -> Tuple[float, float]:
    radians = math.radians(float(latitude))
    meters_per_lat = (
        111132.92
        - 559.82 * math.cos(2.0 * radians)
        + 1.175 * math.cos(4.0 * radians)
        - 0.0023 * math.cos(6.0 * radians)
    )
    meters_per_lon = (
        111412.84 * math.cos(radians)
        - 93.5 * math.cos(3.0 * radians)
        + 0.118 * math.cos(5.0 * radians)
    )
    return max(1e-9, meters_per_lon), max(1e-9, meters_per_lat)


def _validated_radius(radius_m: float) -> float:
    try:
        value = float(radius_m)
    except (TypeError, ValueError) as exc:
        raise ValueError("Sampling radius must be a number.") from exc
    if not math.isfinite(value) or value <= 0:
        raise ValueError("Sampling radius must be greater than 0 metres.")
    return value


def _validated_aggregation(aggregation: str) -> str:
    value = str(aggregation or DEFAULT_RADIUS_AGGREGATION).strip().casefold()
    if value not in SUPPORTED_RADIUS_AGGREGATIONS:
        raise ValueError("Radius aggregation must be one of: mean, max, or min.")
    return value


def _sampled_row(candidate: Dict[str, Any], value: float) -> Dict[str, Any]:
    row = {key: safe_json_value(val) for key, val in candidate.items()}
    row["raster_sample_lon"] = safe_json_value(candidate.get("lon"))
    row["raster_sample_lat"] = safe_json_value(candidate.get("lat"))
    row["raster_value"] = value
    return row