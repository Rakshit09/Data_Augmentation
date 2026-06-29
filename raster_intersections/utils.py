import csv
import json
import math
import os
import shutil
import tempfile
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple


THRESHOLD_OPERATORS = {">", ">=", "<", "<=", "==", "!="}


def runtime_dir(name: str) -> Path:
    path = Path(tempfile.gettempdir()) / "data_augmentation_runtime" / name
    path.mkdir(parents=True, exist_ok=True)
    return path


def sql_string(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def sql_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def safe_json_value(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (int, str, bool)):
        return value
    if isinstance(value, float):
        if math.isfinite(value):
            return value
        return None
    try:
        import numpy as np

        if isinstance(value, np.generic):
            return safe_json_value(value.item())
    except Exception:
        pass
    return str(value)


def finite_float(value: Any, label: str) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be a number.") from exc
    if not math.isfinite(parsed):
        raise ValueError(f"{label} must be finite.")
    return parsed


def normalized_bounds(raw: Dict[str, Any]) -> Dict[str, float]:
    min_lon = finite_float(raw.get("min_lon"), "min_lon")
    min_lat = finite_float(raw.get("min_lat"), "min_lat")
    max_lon = finite_float(raw.get("max_lon"), "max_lon")
    max_lat = finite_float(raw.get("max_lat"), "max_lat")
    min_lon, max_lon = sorted((max(-180.0, min_lon), min(180.0, max_lon)))
    min_lat, max_lat = sorted((max(-90.0, min_lat), min(90.0, max_lat)))
    if min_lon >= max_lon or min_lat >= max_lat:
        raise ValueError("Map bounds are empty.")
    return {"min_lon": min_lon, "min_lat": min_lat, "max_lon": max_lon, "max_lat": max_lat}


def intersect_bounds(left: Dict[str, float], right: Dict[str, float]) -> Optional[Dict[str, float]]:
    min_lon = max(float(left["min_lon"]), float(right["min_lon"]))
    min_lat = max(float(left["min_lat"]), float(right["min_lat"]))
    max_lon = min(float(left["max_lon"]), float(right["max_lon"]))
    max_lat = min(float(left["max_lat"]), float(right["max_lat"]))
    if min_lon >= max_lon or min_lat >= max_lat:
        return None
    return {"min_lon": min_lon, "min_lat": min_lat, "max_lon": max_lon, "max_lat": max_lat}


def parse_threshold(payload: Dict[str, Any]) -> Tuple[Optional[float], str]:
    raw_threshold = payload.get("threshold")
    operator = str(payload.get("threshold_operator") or ">").strip()
    if operator not in THRESHOLD_OPERATORS:
        raise ValueError("Threshold operator must be one of >, >=, <, <=, ==, or !=.")
    if raw_threshold in (None, ""):
        return None, operator
    threshold = finite_float(raw_threshold, "threshold")
    return threshold, operator


def threshold_matches(value: float, threshold: Optional[float], operator: str) -> bool:
    if threshold is None:
        return True
    if operator == ">":
        return value > threshold
    if operator == ">=":
        return value >= threshold
    if operator == "<":
        return value < threshold
    if operator == "<=":
        return value <= threshold
    if operator == "==":
        return value == threshold
    if operator == "!=":
        return value != threshold
    return True


def unique_name(base: str, existing: Iterable[str]) -> str:
    taken = {str(item) for item in existing}
    if base not in taken:
        return base
    index = 2
    while f"{base}_{index}" in taken:
        index += 1
    return f"{base}_{index}"


def csv_read_options(encoding: str) -> str:
    return (
        "sample_size = 20480, ignore_errors = true, header = true, "
        f"all_varchar = true, encoding = {sql_string(encoding)}"
    )


def resolve_csv_scan_options(con: Any, csv_path: Path) -> str:
    errors: List[str] = []
    for encoding in ("utf-8", "latin-1"):
        options = csv_read_options(encoding)
        try:
            con.execute(f"SELECT * FROM read_csv_auto({sql_string(str(csv_path.resolve()))}, {options}) LIMIT 1;")
            return options
        except Exception as exc:
            errors.append(f"{encoding}: {exc}")
    raise ValueError("Could not read the CSV for raster intersection. " + " | ".join(errors))


def find_gdal_tool(name: str) -> Tuple[Optional[str], Dict[str, str]]:
    env = os.environ.copy()
    direct = shutil.which(name) or shutil.which(f"{name}.exe") or shutil.which(f"{name}.bat")
    if direct:
        return direct, env

    try:
        from layer_upload_routes import _find_gdal_tools

        tools = _find_gdal_tools()
    except Exception:
        tools = None

    if not tools:
        return None, env

    env = dict(tools.get("env") or env)
    search_dirs: List[Path] = []
    for tool_path in tools.values():
        if isinstance(tool_path, str):
            path = Path(tool_path)
            if path.parent:
                search_dirs.append(path.parent)

    for raw_part in env.get("PATH", "").split(os.pathsep):
        if raw_part:
            search_dirs.append(Path(raw_part))

    names = [name, f"{name}.exe", f"{name}.bat", f"{name}.py"]
    for directory in dict.fromkeys(search_dirs):
        for candidate_name in names:
            candidate = directory / candidate_name
            if candidate.is_file():
                return str(candidate), env

    return None, env


def write_json(path: Path, payload: Any) -> None:
    with path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False)


def write_csv(path: Path, columns: List[str], rows: List[Dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({column: safe_json_value(row.get(column)) for column in columns})
