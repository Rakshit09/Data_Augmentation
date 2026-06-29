import math
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional

from .utils import find_gdal_tool, safe_json_value, threshold_matches


DEFAULT_SAMPLE_CHUNK_SIZE = 10000


def sample_candidates(
    raster_path: Path,
    candidates: List[Dict[str, Any]],
    metadata: Dict[str, Any],
    threshold: Optional[float],
    threshold_operator: str,
    chunk_size: int = DEFAULT_SAMPLE_CHUNK_SIZE,
) -> List[Dict[str, Any]]:
    if not candidates:
        return []

    try:
        return _sample_with_rasterio(
            raster_path=raster_path,
            candidates=candidates,
            metadata=metadata,
            threshold=threshold,
            threshold_operator=threshold_operator,
            chunk_size=chunk_size,
        )
    except ImportError:
        return _sample_with_gdal(
            raster_path=raster_path,
            candidates=candidates,
            metadata=metadata,
            threshold=threshold,
            threshold_operator=threshold_operator,
            chunk_size=chunk_size,
        )


def sampling_diagnostics(
    raster_path: Path,
    candidates: List[Dict[str, Any]],
    metadata: Dict[str, Any],
    limit: int = 10000,
) -> Dict[str, Any]:
    sample_rows = candidates[: max(1, min(limit, len(candidates)))]
    values = _raw_sample_values(raster_path, sample_rows, metadata)
    nodata = metadata.get("nodata")
    nodata_value = None
    try:
        if nodata not in (None, ""):
            nodata_value = float(nodata)
    except (TypeError, ValueError):
        nodata_value = None

    counts: Dict[str, int] = {}
    finite_count = 0
    nodata_count = 0
    non_nodata_count = 0
    for value in values:
        key = "blank" if value is None else str(value)
        counts[key] = counts.get(key, 0) + 1
        if value is None or not math.isfinite(value):
            continue
        finite_count += 1
        if nodata_value is not None and value == nodata_value:
            nodata_count += 1
        else:
            non_nodata_count += 1

    return {
        "sampled_count": len(sample_rows),
        "finite_count": finite_count,
        "nodata": nodata,
        "nodata_count": nodata_count,
        "non_nodata_count": non_nodata_count,
        "value_counts": dict(sorted(counts.items(), key=lambda item: item[1], reverse=True)[:8]),
    }


def _raw_sample_values(
    raster_path: Path,
    candidates: List[Dict[str, Any]],
    metadata: Dict[str, Any],
) -> List[Optional[float]]:
    try:
        return _raw_sample_values_rasterio(raster_path, candidates, metadata)
    except ImportError:
        return _raw_sample_values_gdal(raster_path, candidates, metadata)


def _raw_sample_values_rasterio(
    raster_path: Path,
    candidates: List[Dict[str, Any]],
    metadata: Dict[str, Any],
) -> List[Optional[float]]:
    try:
        import rasterio
        from pyproj import Transformer
    except Exception as exc:
        raise ImportError(str(exc)) from exc

    band_index = int(metadata.get("band_index") or 1)
    with rasterio.open(raster_path) as dataset:
        transformer = None
        if dataset.crs and dataset.crs.to_string() not in {"EPSG:4326", "OGC:CRS84"}:
            transformer = Transformer.from_crs("EPSG:4326", dataset.crs, always_xy=True)
        lon_lat = [(float(row["lon"]), float(row["lat"])) for row in candidates]
        if transformer:
            xs, ys = transformer.transform([coord[0] for coord in lon_lat], [coord[1] for coord in lon_lat])
            coords = list(zip(xs, ys))
        else:
            coords = lon_lat
        values = []
        for sample in dataset.sample(coords, indexes=band_index, masked=True):
            try:
                if hasattr(sample, "mask") and bool(sample.mask[0]):
                    values.append(None)
                    continue
            except Exception:
                pass
            try:
                values.append(float(sample[0]))
            except (TypeError, ValueError, IndexError):
                values.append(None)
        return values


def _raw_sample_values_gdal(
    raster_path: Path,
    candidates: List[Dict[str, Any]],
    metadata: Dict[str, Any],
) -> List[Optional[float]]:
    gdallocationinfo, env = find_gdal_tool("gdallocationinfo")
    if not gdallocationinfo:
        return []
    band_index = str(int(metadata.get("band_index") or 1))
    input_text = "\n".join(f"{float(row['lon'])} {float(row['lat'])}" for row in candidates) + "\n"
    result = subprocess.run(
        [gdallocationinfo, "-valonly", "-wgs84", "-b", band_index, str(raster_path)],
        input=input_text,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )
    if result.returncode != 0:
        return []
    values = []
    for line in result.stdout.splitlines():
        token = line.strip().split()[0] if line.strip() else ""
        try:
            values.append(float(token))
        except ValueError:
            values.append(None)
    return values


def _sample_with_rasterio(
    raster_path: Path,
    candidates: List[Dict[str, Any]],
    metadata: Dict[str, Any],
    threshold: Optional[float],
    threshold_operator: str,
    chunk_size: int,
) -> List[Dict[str, Any]]:
    try:
        import rasterio
        from pyproj import Transformer
    except Exception as exc:
        raise ImportError(str(exc)) from exc

    band_index = int(metadata.get("band_index") or 1)
    output: List[Dict[str, Any]] = []

    with rasterio.open(raster_path) as dataset:
        if not dataset.crs:
            raise ValueError("Raster CRS is missing. Reproject the raster or upload a GeoTIFF with a CRS.")
        transformer = None
        if dataset.crs.to_string() not in {"EPSG:4326", "OGC:CRS84"}:
            transformer = Transformer.from_crs("EPSG:4326", dataset.crs, always_xy=True)

        nodata = dataset.nodatavals[band_index - 1] if dataset.nodatavals else metadata.get("nodata")
        for start in range(0, len(candidates), chunk_size):
            chunk = candidates[start:start + chunk_size]
            lon_lat = [(float(row["lon"]), float(row["lat"])) for row in chunk]
            if transformer:
                xs, ys = transformer.transform(
                    [coord[0] for coord in lon_lat],
                    [coord[1] for coord in lon_lat],
                )
                sample_coords = list(zip(xs, ys))
            else:
                sample_coords = lon_lat

            for candidate, sample in zip(chunk, dataset.sample(sample_coords, indexes=band_index, masked=True)):
                value = _sample_to_float(sample, nodata)
                if value is None or not threshold_matches(value, threshold, threshold_operator):
                    continue
                output.append(_sampled_row(candidate, value))

    return output


def _sample_with_gdal(
    raster_path: Path,
    candidates: List[Dict[str, Any]],
    metadata: Dict[str, Any],
    threshold: Optional[float],
    threshold_operator: str,
    chunk_size: int,
) -> List[Dict[str, Any]]:
    gdallocationinfo, env = find_gdal_tool("gdallocationinfo")
    if not gdallocationinfo:
        raise ValueError(
            "Raster intersection needs rasterio/pyproj in the app environment, "
            "or bundled GDAL with gdallocationinfo."
        )

    band_index = str(int(metadata.get("band_index") or 1))
    nodata = metadata.get("nodata")
    output: List[Dict[str, Any]] = []

    for start in range(0, len(candidates), chunk_size):
        chunk = candidates[start:start + chunk_size]
        input_text = "\n".join(f"{float(row['lon'])} {float(row['lat'])}" for row in chunk) + "\n"
        result = subprocess.run(
            [gdallocationinfo, "-valonly", "-wgs84", "-b", band_index, str(raster_path)],
            input=input_text,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
        )
        if result.returncode != 0:
            details = (result.stderr or result.stdout or "").strip()
            raise ValueError(f"GDAL could not sample the raster: {details}")

        lines = result.stdout.splitlines()
        for index, candidate in enumerate(chunk):
            value = _parse_gdal_value(lines[index] if index < len(lines) else "", nodata)
            if value is None or not threshold_matches(value, threshold, threshold_operator):
                continue
            output.append(_sampled_row(candidate, value))

    return output


def _sample_to_float(sample: Any, nodata: Any) -> Optional[float]:
    try:
        if hasattr(sample, "mask") and bool(sample.mask[0]):
            return None
    except Exception:
        pass
    try:
        value = float(sample[0])
    except (TypeError, ValueError, IndexError):
        return None
    return _valid_value(value, nodata)


def _parse_gdal_value(raw: str, nodata: Any) -> Optional[float]:
    token = raw.strip().split()[0] if raw.strip() else ""
    if not token:
        return None
    try:
        value = float(token)
    except ValueError:
        return None
    return _valid_value(value, nodata)


def _valid_value(value: float, nodata: Any) -> Optional[float]:
    if not math.isfinite(value):
        return None
    if nodata not in (None, ""):
        try:
            nodata_value = float(nodata)
            if math.isfinite(nodata_value) and value == nodata_value:
                return None
        except (TypeError, ValueError):
            pass
    return value


def _sampled_row(candidate: Dict[str, Any], value: float) -> Dict[str, Any]:
    row = {key: safe_json_value(val) for key, val in candidate.items()}
    row["raster_sample_lon"] = safe_json_value(candidate.get("lon"))
    row["raster_sample_lat"] = safe_json_value(candidate.get("lat"))
    row["raster_value"] = value
    return row
