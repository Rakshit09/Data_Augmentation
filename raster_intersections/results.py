import hashlib
import math
import shutil
import time
import uuid
from pathlib import Path
from threading import Lock
from typing import Any, Dict, List, Optional

from .duckdb_queries import DATABASE_SI_CANDIDATES, EXPOSURE_SI_CANDIDATES
from .exports import write_exports
from .utils import runtime_dir, safe_json_value, threshold_matches


MAX_RETAINED_JOBS = 8
PREVIEW_LIMIT = 500
MAP_FEATURE_LIMIT = 5000

JOBS: Dict[str, Dict[str, Any]] = {}
JOBS_LOCK = Lock()


def create_result_job(
    source_type: str,
    layer_name: str,
    columns: List[str],
    rows: List[Dict[str, Any]],
    summary: Dict[str, Any],
) -> Dict[str, Any]:
    job_id = uuid.uuid4().hex
    job_dir = runtime_dir("raster_intersections") / job_id
    paths = write_exports(job_dir, columns, rows)
    download_urls = {
        key: f"/api/raster-intersections/{job_id}/download.{key}"
        for key, path in paths.items()
        if path is not None
    }

    job = {
        "job_id": job_id,
        "source_type": source_type,
        "layer_name": layer_name,
        "created_at": time.time(),
        "columns": columns,
        "preview_rows": rows[:PREVIEW_LIMIT],
        "summary": summary,
        "paths": {key: str(path) for key, path in paths.items() if path is not None},
        "download_urls": download_urls,
        "map_features": make_map_features(rows, MAP_FEATURE_LIMIT),
    }
    with JOBS_LOCK:
        JOBS[job_id] = job
        _prune_locked()
    return job


def build_summary(
    rows: List[Dict[str, Any]],
    source_type: str,
    candidate_count: int,
    threshold: Optional[float],
    threshold_operator: str,
    si_field: Optional[str],
    elapsed_seconds: float,
    raster_band: int,
    bounds: Dict[str, float],
) -> Dict[str, Any]:
    values = [_float_or_none(row.get("raster_value")) for row in rows]
    values = [value for value in values if value is not None]
    si_field = _find_si_field(rows, source_type, si_field)

    total_si = None
    si_matching_threshold = None
    if si_field:
        si_values = [_float_or_none(row.get(si_field)) for row in rows]
        valid_si = [value for value in si_values if value is not None]
        if valid_si:
            total_si = float(sum(valid_si))
            if threshold is not None:
                si_matching_threshold = float(sum(
                    si_value
                    for row, si_value in zip(rows, si_values)
                    if si_value is not None
                    and _float_or_none(row.get("raster_value")) is not None
                    and threshold_matches(float(row["raster_value"]), threshold, threshold_operator)
                ))

    matching_threshold = None
    if threshold is not None:
        matching_threshold = sum(1 for value in values if threshold_matches(value, threshold, threshold_operator))

    return {
        "source_type": source_type,
        "candidate_count": int(candidate_count),
        "matched_count": len(rows),
        "raster_value_min": min(values) if values else None,
        "raster_value_mean": (sum(values) / len(values)) if values else None,
        "raster_value_max": max(values) if values else None,
        "threshold": threshold,
        "threshold_operator": threshold_operator,
        "count_above_threshold": matching_threshold,
        "count_matching_threshold": matching_threshold,
        "si_field": si_field,
        "total_si": total_si,
        "si_above_threshold": si_matching_threshold,
        "elapsed_seconds": round(elapsed_seconds, 3),
        "raster_band": raster_band,
        "bounds": bounds,
        "preview_row_limit": PREVIEW_LIMIT,
        "map_feature_limit": MAP_FEATURE_LIMIT,
    }


def build_vector_summary(
    rows: List[Dict[str, Any]],
    source_type: str,
    candidate_count: int,
    field: str,
    si_field: Optional[str],
    elapsed_seconds: float,
    bounds: Dict[str, float],
) -> Dict[str, Any]:
    numeric_values = [_float_or_none(row.get("raster_value")) for row in rows]
    numeric_values = [value for value in numeric_values if value is not None]
    si_field = _find_si_field(rows, source_type, si_field)
    total_si = None
    if si_field:
        si_values = [_float_or_none(row.get(si_field)) for row in rows]
        valid_si = [value for value in si_values if value is not None]
        if valid_si:
            total_si = float(sum(valid_si))

    return {
        "source_type": source_type,
        "candidate_count": int(candidate_count),
        "matched_count": len(rows),
        "raster_value_min": min(numeric_values) if numeric_values else None,
        "raster_value_mean": (sum(numeric_values) / len(numeric_values)) if numeric_values else None,
        "raster_value_max": max(numeric_values) if numeric_values else None,
        "threshold": None,
        "threshold_operator": None,
        "count_above_threshold": None,
        "count_matching_threshold": None,
        "si_field": si_field,
        "total_si": total_si,
        "si_above_threshold": None,
        "elapsed_seconds": round(elapsed_seconds, 3),
        "raster_band": None,
        "vector_field": field or "feature_id",
        "bounds": bounds,
        "preview_row_limit": PREVIEW_LIMIT,
        "map_feature_limit": MAP_FEATURE_LIMIT,
    }


def get_job(job_id: str) -> Optional[Dict[str, Any]]:
    with JOBS_LOCK:
        return JOBS.get(job_id)


def make_map_features(rows: List[Dict[str, Any]], limit: int) -> Dict[str, Any]:
    features = []
    values = [_float_or_none(row.get("raster_value")) for row in rows]
    values = [value for value in values if value is not None]
    min_value = min(values) if values else 0.0
    max_value = max(values) if values else 1.0
    for row in rows[:limit]:
        lon = _float_or_none(row.get("raster_sample_lon"))
        lat = _float_or_none(row.get("raster_sample_lat"))
        if lon is None or lat is None:
            continue
        value = _float_or_none(row.get("raster_value"))
        vector_value = row.get("vector_value")
        features.append({
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [lon, lat]},
            "properties": {
                "raster_value": value,
                "__color": _value_color(value, min_value, max_value, vector_value),
                **_compact_properties(row),
            },
        })
    return {
        "type": "FeatureCollection",
        "features": features,
        "returned_count": len(features),
        "truncated": len(rows) > len(features),
        "total_count": len(rows),
    }


def _compact_properties(row: Dict[str, Any]) -> Dict[str, Any]:
    preferred = [
        "exposure_row_id",
        "row_id",
        "building_id",
        "raster_value",
        "vector_field",
        "vector_value",
        "vector_feature_id",
        "occupancy_group",
        "occupancy_code",
        "height_m",
        "floorspace_est_m2",
    ]
    props: Dict[str, Any] = {}
    for key in preferred:
        if key in row:
            props[key] = safe_json_value(row.get(key))
    return props


def _value_color(value: Optional[float], min_value: float, max_value: float, category: Any = None) -> str:
    if value is None:
        if category not in (None, ""):
            palette = ["#2563eb", "#16a34a", "#dc2626", "#9333ea", "#ea580c", "#0891b2", "#be123c", "#4d7c0f"]
            digest = hashlib.sha1(str(category).encode("utf-8")).hexdigest()
            return palette[int(digest[:8], 16) % len(palette)]
        return "#64748b"
    ratio = 0.5 if max_value <= min_value else (value - min_value) / (max_value - min_value)
    ratio = max(0.0, min(1.0, ratio))
    palette = ["#2c7bb6", "#abd9e9", "#ffffbf", "#fdae61", "#d7191c"]
    position = ratio * (len(palette) - 1)
    index = min(len(palette) - 2, int(math.floor(position)))
    frac = position - index
    left = _hex_to_rgb(palette[index])
    right = _hex_to_rgb(palette[index + 1])
    mixed = [round(left[channel] + (right[channel] - left[channel]) * frac) for channel in range(3)]
    return "#" + "".join(f"{part:02x}" for part in mixed)


def _hex_to_rgb(value: str) -> List[int]:
    value = value.lstrip("#")
    return [int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16)]


def _find_si_field(rows: List[Dict[str, Any]], source_type: str, requested_field: Optional[str] = None) -> Optional[str]:
    if not rows:
        return None
    fields = set(rows[0].keys())
    if requested_field:
        normalized_requested = requested_field.casefold()
        for field in fields:
            if field.casefold() == normalized_requested:
                return field
    candidates = EXPOSURE_SI_CANDIDATES if "exposure" in source_type else DATABASE_SI_CANDIDATES
    normalized = {field.casefold(): field for field in fields}
    for candidate in candidates:
        if candidate in fields:
            return candidate
        if candidate.casefold() in normalized:
            return normalized[candidate.casefold()]
    return None


def _float_or_none(value: Any) -> Optional[float]:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    if math.isfinite(parsed):
        return parsed
    return None


def _prune_locked() -> None:
    if len(JOBS) <= MAX_RETAINED_JOBS:
        return
    stale = sorted(JOBS.values(), key=lambda job: float(job.get("created_at") or 0))
    for job in stale[: max(0, len(JOBS) - MAX_RETAINED_JOBS)]:
        JOBS.pop(str(job.get("job_id")), None)
        first_path = next(iter((job.get("paths") or {}).values()), "")
        if first_path:
            shutil.rmtree(Path(first_path).parent, ignore_errors=True)
