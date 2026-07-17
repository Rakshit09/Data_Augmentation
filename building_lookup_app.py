
import os
import psutil
import argparse
import codecs
import csv
import hashlib
from genericpath import exists
from html import escape
import json
import math
import os
import re
import shutil
import tempfile
import subprocess
import sys
import time
import urllib.parse
import urllib.request
import uuid
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from threading import Event, Lock, Thread
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

import duckdb
import pandas as pd
from flask import Flask, Response, jsonify, render_template, request, send_file
from markupsafe import Markup
from werkzeug.utils import secure_filename

from country_boundary_catalog import DEFAULT_COUNTRY_BOUNDARY_CATALOG, list_catalog_countries
from custom_parquet_database import register_custom_parquet_routes
from exposure_map_cache import (
    RAW_POINT_ZOOM,
    build_exposure_multires_tables,
    build_exposure_row_table,
    ensure_exposure_multires_cache,
    exposure_row_table_is_current,
    lookup_exposure_row as lookup_exposure_csv_row,
    lookup_exposure_points_multires,
)
from layer_upload_routes import register_layer_upload_routes
from obm_country_to_parquet import ETLConfig, OpenBuildingMapCountryETL
from raster_intersections import register_raster_intersection_routes


DEFAULT_PARQUET = "etl_output/buildings_de_cleaned.parquet"
DEFAULT_DB = "etl_output/building_lookup.duckdb"
NEAREST_CANDIDATE_LIMIT = 64
DEFAULT_QUADKEY_PREFIX_ZOOM = 6
OPTIMIZED_QUADKEY_PREFIX_ZOOM = 14
DB_STAGE_COPY_MAX_BYTES = int(os.environ.get("OBM_DB_COPY_MAX_BYTES", str(8 * 1024 ** 3)))
DB_STAGE_COPY_CHUNK_BYTES = 32 * 1024 * 1024
DB_STAGE_MAX_QUADKEY_TILES = 16384
DB_STAGE_RANGES_PER_INSERT = 128
DB_STAGE_CACHE_MAX_FILES = 2
DB_STAGE_TEMP_MAX_AGE_SECONDS = 60 * 60
MAX_RETAINED_EXPOSURE_JOBS = 1
MAX_RETAINED_EXPOSURE_UPLOADS = 1
MAX_RETAINED_EXPOSURE_RESULTS = 1
EXPOSURE_ARTIFACT_MAX_AGE_SECONDS = 6 * 60 * 60
SUPPORTED_EXPOSURE_UPLOAD_EXTENSIONS = {".csv", ".xlsx"}
EXPOSURE_MAP_MAX_FEATURES = int(os.environ.get("EXPOSURE_MAP_MAX_FEATURES", "12000"))
EXPOSURE_MAP_CACHE_MAX_FILES = int(os.environ.get("EXPOSURE_MAP_CACHE_MAX_FILES", "3"))
MAX_BUILDING_FILTER_VALUES = 500
FILTER_VIEW_MIN_ZOOM = 11
FILTER_VIEW_MAX_TILE_ZOOM = 17
MAX_FILTER_VIEW_FEATURES_PER_TILE = 50000
MAX_FILTER_VIEW_SUMMARY_TILES = 256
FILTER_VIEW_ALL_VALUE = "__ALL__"
FILTER_VIEW_PALETTE = [
    "#e85d04",  # orange
    "#0067c5",  # blue
    "#b5175b",  # magenta
    "#008c95",  # teal
    "#6f42c1",  # purple
    "#c62828",  # red
    "#2e7d32",  # green
    "#b07800",  # amber
    "#3f51b5",  # indigo
    "#a63c06",  # rust
    "#00796b",  # emerald
    "#8e24aa",  # violet
    "#ad2831",  # brick
    "#0077a8",  # ocean
    "#667a00",  # olive
    "#d43d00",  # vermilion
    "#5d3f8c",  # plum
    "#00834f",  # jade
    "#8c2d55",  # wine
    "#52606d",  # slate
]
BUILDING_COLUMNS = [
    "building_id",
    "source",
    "relation_id",
    "quadkey",
    "quadkey_prefix_6",
    "last_update",
    "centroid_lon",
    "centroid_lat",
    "bbox_xmin",
    "bbox_ymin",
    "bbox_xmax",
    "bbox_ymax",
    "footprint_area_m2",
    "height_raw",
    "occupancy_raw",
    "floorspace_obm_m2",
    "height_source_type",
    "height_m",
    "stories_exact",
    "stories_min",
    "stories_max",
    "height_quality",
    "occupancy_code",
    "occupancy_group",
    "occupancy_quality",
    "floorspace_est_m2",
    "attribute_completeness_score",
]

DEFAULT_EXPOSURE_FIELD_CANDIDATES = [
    "building_id",
    "height_m",
    "occupancy_raw",
    "occupancy_code",
    "occupancy_group",
    "footprint_area_m2",
    "height_raw",
    "height_source_type",
    "source",
    "stories_exact",
    "stories_min",
    "stories_max",
]

HELP_CALLOUT_TITLES = {
    "important": "Important",
    "warning": "Warning",
    "note": "Note",
}


def _render_help_inline(text: str) -> str:
    rendered = escape(text.strip())
    rendered = re.sub(r"\*\*(.+?)\*\*", lambda match: f"<strong>{match.group(1)}</strong>", rendered)
    rendered = re.sub(r"`([^`]+)`", lambda match: f"<code>{match.group(1)}</code>", rendered)
    return rendered


def _render_help_markdown(text: str) -> str:
    html_parts: List[str] = []
    paragraph_lines: List[str] = []
    unordered_items: List[str] = []
    ordered_items: List[str] = []
    callout_lines: List[str] = []
    code_lines: List[str] = []
    callout_kind = "note"
    in_code_block = False

    def flush_paragraph() -> None:
        if paragraph_lines:
            html_parts.append(f"<p>{_render_help_inline(' '.join(paragraph_lines))}</p>")
            paragraph_lines.clear()

    def flush_unordered() -> None:
        if unordered_items:
            items = "".join(f"<li>{_render_help_inline(item)}</li>" for item in unordered_items)
            html_parts.append(f"<ul>{items}</ul>")
            unordered_items.clear()

    def flush_ordered() -> None:
        if ordered_items:
            items = "".join(f"<li>{_render_help_inline(item)}</li>" for item in ordered_items)
            html_parts.append(f"<ol>{items}</ol>")
            ordered_items.clear()

    def flush_callout() -> None:
        nonlocal callout_kind
        if callout_lines:
            body = _render_help_inline(" ".join(callout_lines))
            title = HELP_CALLOUT_TITLES.get(callout_kind, "Note")
            html_parts.append(
                f'<div class="help-callout help-callout--{callout_kind}">'
                f'<p class="help-callout-title">{title}</p>'
                f"<p>{body}</p>"
                "</div>"
            )
            callout_lines.clear()
            callout_kind = "note"

    def flush_code() -> None:
        nonlocal in_code_block
        if code_lines:
            html_parts.append(
                "<pre class=\"help-code\"><code>"
                + escape("\n".join(code_lines))
                + "</code></pre>"
            )
            code_lines.clear()
        in_code_block = False

    def flush_lists() -> None:
        flush_unordered()
        flush_ordered()

    def flush_all() -> None:
        flush_paragraph()
        flush_lists()
        flush_callout()

    for raw_line in text.splitlines():
        line = raw_line.rstrip()
        stripped = line.strip()

        if stripped.startswith("```"):
            if in_code_block:
                flush_code()
            else:
                flush_all()
                in_code_block = True
            continue

        if in_code_block:
            code_lines.append(line)
            continue

        if stripped.startswith(">"):
            flush_paragraph()
            flush_lists()
            quote_text = stripped[1:].strip()
            marker_match = re.fullmatch(r"\[!(IMPORTANT|WARNING|NOTE)\]", quote_text, re.IGNORECASE)
            if marker_match:
                callout_kind = marker_match.group(1).lower()
            elif quote_text:
                callout_lines.append(quote_text)
            continue

        flush_callout()

        if not stripped:
            flush_all()
            continue

        heading_match = re.match(r"^(#{1,3})\s+(.*)$", stripped)
        if heading_match:
            flush_all()
            level = len(heading_match.group(1))
            html_parts.append(f"<h{level}>{_render_help_inline(heading_match.group(2))}</h{level}>")
            continue

        if re.match(r"^[-*]\s+", stripped):
            flush_paragraph()
            flush_ordered()
            unordered_items.append(re.sub(r"^[-*]\s+", "", stripped, count=1))
            continue

        if re.match(r"^\d+\.\s+", stripped):
            flush_paragraph()
            flush_unordered()
            ordered_items.append(re.sub(r"^\d+\.\s+", "", stripped, count=1))
            continue

        paragraph_lines.append(stripped)

    if in_code_block:
        flush_code()

    flush_all()
    return "\n".join(html_parts)


def sql_string(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def sql_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def json_safe(value: Any) -> Any:
    if pd.isna(value):
        return None
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return value


def fetch_geocoder_json(url: str, user_agent: str) -> Any:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": user_agent,
            "Accept": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=10) as response:
        return json.loads(response.read().decode("utf-8"))


def geocode_with_nominatim(query: str, user_agent: str) -> List[Dict[str, Any]]:
    params = urllib.parse.urlencode({
        "q": query,
        "format": "jsonv2",
        "addressdetails": 1,
        "limit": 5,
    })
    raw_results = fetch_geocoder_json(
        f"https://nominatim.openstreetmap.org/search?{params}",
        user_agent,
    )
    if not isinstance(raw_results, list):
        raise ValueError("Nominatim returned an unexpected response.")

    return [
        {
            "label": item.get("display_name"),
            "lon": float(item["lon"]),
            "lat": float(item["lat"]),
            "type": item.get("type"),
            "provider": "Nominatim",
        }
        for item in raw_results
        if item.get("lat") and item.get("lon") and item.get("display_name")
    ]


def geocode_with_photon(query: str, user_agent: str) -> List[Dict[str, Any]]:
    params = urllib.parse.urlencode({
        "q": query,
        "limit": 5,
    })
    raw_results = fetch_geocoder_json(
        f"https://photon.komoot.io/api/?{params}",
        user_agent,
    )
    features = raw_results.get("features", []) if isinstance(raw_results, dict) else []
    results = []

    for feature in features:
        geometry = feature.get("geometry") or {}
        properties = feature.get("properties") or {}
        coordinates = geometry.get("coordinates") or []
        if len(coordinates) < 2:
            continue

        label_parts = [
            properties.get("name"),
            properties.get("street"),
            properties.get("city") or properties.get("county"),
            properties.get("state"),
            properties.get("country"),
        ]
        label = ", ".join(str(part) for part in label_parts if part)
        if not label:
            continue

        results.append({
            "label": label,
            "lon": float(coordinates[0]),
            "lat": float(coordinates[1]),
            "type": properties.get("osm_value"),
            "provider": "Photon",
        })

    return results


def detect_csv_encoding(csv_path: Path) -> str:
    sample = csv_path.read_bytes()[:1_048_576]
    if sample.startswith(codecs.BOM_UTF8):
        return "utf-8-sig"
    if sample.startswith(codecs.BOM_UTF16_LE) or sample.startswith(codecs.BOM_UTF16_BE):
        return "utf-16"
    if sample.startswith(codecs.BOM_UTF32_LE) or sample.startswith(codecs.BOM_UTF32_BE):
        return "utf-32"

    for encoding in ("utf-8", "cp1252", "latin1"):
        try:
            sample.decode(encoding)
            return encoding
        except UnicodeDecodeError:
            continue

    return "latin1"


def csv_encoding_candidates(csv_path: Path) -> List[str]:
    detected = detect_csv_encoding(csv_path)
    candidates = [detected]

    if detected == "utf-8":
        candidates.append("utf-8-sig")
    elif detected == "utf-8-sig":
        candidates.append("utf-8")

    candidates.extend(["cp1252", "latin1"])

    ordered_candidates: List[str] = []
    for candidate in candidates:
        if candidate not in ordered_candidates:
            ordered_candidates.append(candidate)
    return ordered_candidates


def normalize_csv_to_utf8_sig(csv_path: Path) -> str:
    last_error: Optional[UnicodeDecodeError] = None

    for encoding in csv_encoding_candidates(csv_path):
        if encoding in {"utf-8", "utf-8-sig"}:
            try:
                with csv_path.open("r", encoding=encoding, newline=""):
                    return encoding
            except UnicodeDecodeError as exc:
                last_error = exc
                continue

        temp_path = csv_path.with_name(f"{csv_path.name}.{uuid.uuid4().hex}.utf8tmp")
        try:
            try:
                with csv_path.open("r", encoding=encoding, newline="") as src, temp_path.open(
                    "w",
                    encoding="utf-8-sig",
                    newline="",
                ) as dst:
                    shutil.copyfileobj(src, dst, length=1_048_576)
            except UnicodeDecodeError as exc:
                last_error = exc
                continue

            os.replace(temp_path, csv_path)
            return "utf-8-sig"
        finally:
            temp_path.unlink(missing_ok=True)

    if last_error is not None:
        raise ValueError(f"Could not decode CSV using supported encodings: {last_error}") from last_error

    raise ValueError("Could not decode CSV using supported encodings.")


def ensure_utf8_bom(csv_path: Path) -> None:
    with csv_path.open("rb") as src:
        if src.read(3) == codecs.BOM_UTF8:
            return

    temp_path = csv_path.with_name(f"{csv_path.name}.{uuid.uuid4().hex}.bomtmp")
    try:
        with csv_path.open("rb") as src, temp_path.open("wb") as dst:
            dst.write(codecs.BOM_UTF8)
            shutil.copyfileobj(src, dst, length=1_048_576)
        os.replace(temp_path, csv_path)
    finally:
        temp_path.unlink(missing_ok=True)


def load_csv_dataframe(csv_path: Path) -> pd.DataFrame:
    if csv_path.suffix.lower() == ".xlsx":
        con = duckdb.connect()
        try:
            _load_excel_extension(con)
            xlsx_sql = _xlsx_read_sql(csv_path)
            return con.execute(f"SELECT * FROM {xlsx_sql};").fetchdf().astype(str)
        finally:
            con.close()

    errors: List[str] = []

    for encoding in csv_encoding_candidates(csv_path):
        try:
            return pd.read_csv(
                csv_path,
                dtype=str,
                encoding=encoding,
                keep_default_na=False,
                na_filter=False,
            )
        except UnicodeDecodeError as exc:
            errors.append(f"{encoding}: {exc}")

    raise ValueError(
        "Could not decode CSV using supported encodings. " + " | ".join(errors)
    )


def excel_cell_to_string(value: Any) -> str:
    if value is None:
        return ""
    return str(value)


def _load_excel_extension(con: duckdb.DuckDBPyConnection) -> None:
    try:
        con.execute("LOAD excel;")
    except duckdb.Error:
        con.execute("INSTALL excel;")
        con.execute("LOAD excel;")


def _xlsx_read_sql(xlsx_path: Path) -> str:
    return f"read_xlsx({sql_string(str(xlsx_path.resolve()))}, header := true, all_varchar := true)"


def preview_excel_file(excel_path: Path) -> tuple[List[str], List[Dict[str, Any]]]:
    con = duckdb.connect()
    try:
        _load_excel_extension(con)
        xlsx_sql = _xlsx_read_sql(excel_path)

        desc = con.execute(f"DESCRIBE SELECT * FROM {xlsx_sql};").fetchall()
        columns = [str(row[0]) for row in desc]
        if not columns:
            raise ValueError("Uploaded Excel file does not contain any readable columns.")

        rows = con.execute(f"SELECT * FROM {xlsx_sql} LIMIT 10;").fetchall()
        preview_rows: List[Dict[str, Any]] = []
        for row in rows:
            record = {
                columns[i]: json_safe("" if row[i] is None else str(row[i]))
                for i in range(len(columns))
            }
            preview_rows.append(record)

        return columns, preview_rows
    finally:
        con.close()


def convert_excel_to_csv(excel_path: Path, csv_path: Path) -> None:
    con = duckdb.connect()
    try:
        _load_excel_extension(con)
        xlsx_sql = _xlsx_read_sql(excel_path)
        csv_out = sql_string(str(csv_path.resolve()))
        con.execute(f"COPY (SELECT * FROM {xlsx_sql}) TO {csv_out} (HEADER, DELIMITER ',');")
    finally:
        con.close()


def prepare_exposure_upload(uploaded_file, upload_dir: Path, upload_id: str) -> tuple[Path, str]:
    original_filename = Path(uploaded_file.filename or "").name
    display_filename = secure_filename(original_filename) or f"{upload_id}.csv"
    extension = Path(display_filename).suffix.lower()

    if extension not in SUPPORTED_EXPOSURE_UPLOAD_EXTENSIONS:
        raise ValueError("Only CSV and Excel (.xlsx) files are supported.")

    if extension == ".csv":
        upload_path = upload_dir / f"{upload_id}_{display_filename}"
        saved_size = save_uploaded_file(uploaded_file, upload_path)
        if saved_size == 0:
            upload_path.unlink(missing_ok=True)
            raise ValueError(
                "The uploaded file was saved as 0 bytes. "
                f"Filename: {original_filename or display_filename}"
            )
        return upload_path, display_filename

    upload_path = upload_dir / f"{upload_id}_{display_filename}"
    saved_size = save_uploaded_file(uploaded_file, upload_path)
    if saved_size == 0:
        upload_path.unlink(missing_ok=True)
        raise ValueError(
            "The uploaded file was saved as 0 bytes. "
            f"Filename: {original_filename or display_filename}"
        )

    return upload_path, display_filename


def duckdb_csv_encoding(csv_path: Path) -> str:
    encoding = detect_csv_encoding(csv_path)
    if encoding == "utf-8-sig":
        return "utf-8"
    if encoding == "cp1252":
        return "latin-1"
    if encoding == "latin1":
        return "latin-1"
    return encoding


def duckdb_csv_encoding_candidates(csv_path: Path) -> List[str]:
    detected = duckdb_csv_encoding(csv_path)
    candidates = [detected, "utf-8", "latin-1"]
    ordered_candidates: List[str] = []
    for candidate in candidates:
        if candidate not in ordered_candidates:
            ordered_candidates.append(candidate)
    return ordered_candidates


def write_progress_snapshot(progress_path: Path, phase: str, percent: int) -> None:
    payload = {
        "phase": phase,
        "percent": max(0, min(99, int(percent))),
        "updated_at": time.time(),
    }
    progress_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with progress_path.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=True, default=json_safe)
    except (PermissionError, OSError):
        pass


def read_progress_snapshot(progress_path: Path) -> Optional[Dict[str, Any]]:
    try:
        with progress_path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return None

    if not isinstance(payload, dict):
        return None

    phase = payload.get("phase")
    try:
        percent = int(payload.get("percent"))
    except (TypeError, ValueError):
        return None

    if not isinstance(phase, str) or not phase:
        return None

    return {
        "phase": phase,
        "percent": max(0, min(99, percent)),
    }


def local_runtime_dir(name: str) -> Path:
    runtime_dir = Path(tempfile.gettempdir()) / "data_augmentation_runtime" / name
    runtime_dir.mkdir(parents=True, exist_ok=True)
    return runtime_dir


def _tile_xy(lon: float, lat: float, zoom: int) -> Tuple[int, int]:
    """Convert lon/lat to slippy-map tile x/y at the given zoom level."""
    n = 1 << zoom
    lat_rad = math.radians(min(max(lat, -85.05112878), 85.05112878))
    x = int(min(max((lon + 180.0) / 360.0 * n, 0), n - 1))
    y = int(min(max(
        (0.5 - math.log(math.tan(lat_rad) + 1.0 / math.cos(lat_rad)) / (2.0 * math.pi)) * n,
        0,
    ), n - 1))
    return x, y


def _tile_to_quadkey(x: int, y: int, zoom: int) -> str:
    """Convert tile x/y/zoom to a quadkey string."""
    chars: List[str] = []
    for level in range(zoom, 0, -1):
        digit = 0
        mask = 1 << (level - 1)
        if x & mask:
            digit += 1
        if y & mask:
            digit += 2
        chars.append(str(digit))
    return "".join(chars)


def tile_to_bounds(x: int, y: int, z: int) -> Tuple[float, float, float, float]:
    """Convert slippy-map tile coordinates to lon/lat bounds."""
    n = 1 << z
    west = x / n * 360.0 - 180.0
    east = (x + 1) / n * 360.0 - 180.0
    north = math.degrees(math.atan(math.sinh(math.pi * (1.0 - (2.0 * y) / n))))
    south = math.degrees(math.atan(math.sinh(math.pi * (1.0 - (2.0 * (y + 1)) / n))))
    return west, south, east, north


def _compute_covering_quadkeys(
    lons: Any,
    lats: Any,
    zoom: int,
) -> Set[str]:
    """Compute the set of quadkey prefixes covering all valid points plus their 8 neighbours."""
    quadkeys: Set[str] = set()
    max_tile = (1 << zoom) - 1
    for lon_raw, lat_raw in zip(lons, lats):
        try:
            lon_f = float(lon_raw)
            lat_f = float(lat_raw)
        except (TypeError, ValueError):
            continue
        if not (-180.0 <= lon_f <= 180.0 and -90.0 <= lat_f <= 90.0):
            continue
        tx, ty = _tile_xy(lon_f, lat_f, zoom)
        for dx in range(-1, 2):
            for dy in range(-1, 2):
                nx, ny = tx + dx, ty + dy
                if 0 <= nx <= max_tile and 0 <= ny <= max_tile:
                    quadkeys.add(_tile_to_quadkey(nx, ny, zoom))
    return quadkeys


def is_remote_storage_path(path_value: Path | str) -> bool:
    if os.environ.get("OBM_FORCE_REMOTE_DB", "").strip().lower() in {"1", "true", "yes"}:
        return True

    path = Path(path_value).expanduser()
    raw_path = str(path)
    if raw_path.startswith("\\\\") or raw_path.upper().startswith("J:"):
        return True

    if os.name != "nt":
        return False

    drive = path.drive
    if not drive:
        return False

    root = drive if drive.endswith("\\") else f"{drive}\\"
    try:
        import ctypes

        return ctypes.windll.kernel32.GetDriveTypeW(root) == 4
    except Exception:
        return False



def enrichment_thread_count(db_path: str) -> int:
    cpu_count = max(1, os.cpu_count() or 1)

    if is_remote_storage_path(db_path):
        # Single thread for network storage to avoid SMB I/O congestion
        return 6

    return max(1, cpu_count - 1)


def _point_quadkey_filter(
    lon: float,
    lat: float,
    prefix_column: str,
    zoom: int,
    allow_null_prefix: bool,
) -> str:
    qks = _compute_covering_quadkeys([lon], [lat], zoom)
    if not qks:
        return "FALSE"

    qk_sql = ", ".join(sql_string(qk) for qk in sorted(qks))
    col_sql = sql_identifier(prefix_column)

    if allow_null_prefix:
        return f"(b.{col_sql} IN ({qk_sql}) OR b.{col_sql} IS NULL)"

    return f"b.{col_sql} IN ({qk_sql})"


def _tile_quadkey_filter(
    x: int,
    y: int,
    z: int,
    prefix_column: str,
    prefix_zoom: int,
    allow_null_prefix: bool,
) -> str:
    if z >= prefix_zoom:
        west, south, east, north = tile_to_bounds(x, y, z)
        return _point_quadkey_filter(
            (west + east) / 2.0,
            (south + north) / 2.0,
            prefix_column,
            prefix_zoom,
            allow_null_prefix,
        )

    if z <= 0:
        return "TRUE"

    prefix = _tile_to_quadkey(x, y, z)
    range_start, range_end = merge_quadkey_prefix_ranges({prefix})[0]
    prefix_sql = sql_identifier(prefix_column)
    predicate = f"b.{prefix_sql} >= {sql_string(range_start)}"
    if range_end is not None:
        predicate += f" AND b.{prefix_sql} < {sql_string(range_end)}"
    predicate = f"({predicate})"

    if allow_null_prefix:
        return f"({predicate} OR b.{prefix_sql} IS NULL)"

    return predicate


def run_enrichment_worker(
    db_path: str,
    csv_path: Path,
    output_path: Path,
    lat_col: str,
    lon_col: str,
    mode: str,
    max_distance_m: float,
    appended_fields: List[str],
    progress_path: Optional[Path] = None,
) -> Dict[str, Any]:
    worker_path = Path(__file__).resolve().with_name("enrichment_worker.py")
    db_path_resolved = str(Path(db_path).resolve())
    csv_path_resolved = csv_path.resolve()
    output_path_resolved = output_path.resolve()
    summary_path = output_path_resolved.with_suffix(".summary.json")
    summary_path.unlink(missing_ok=True)

    if progress_path is not None:
        progress_path.unlink(missing_ok=True)

    stdout_path = None
    stderr_path = None

    command = [sys.executable]

    if getattr(sys, "frozen", False):
        command.append("--enrichment-worker")
    else:
        command.append(str(worker_path))

    command.extend([
        "--db-path",
        db_path_resolved,
        "--csv-path",
        str(csv_path_resolved),
        "--output-path",
        str(output_path_resolved),
        "--lat-col",
        lat_col,
        "--lon-col",
        lon_col,
        "--mode",
        mode,
        "--max-distance-m",
        str(float(max_distance_m)),
        "--appended-fields-json",
        json.dumps(appended_fields),
        "--summary-path",
        str(summary_path),
        "--progress-path",
        str(progress_path.resolve()) if progress_path is not None else "",
    ])

    try:
        with tempfile.NamedTemporaryFile(
            mode="w+",
            encoding="utf-8",
            suffix=".worker.stdout.log",
            delete=False,
        ) as stdout_handle, tempfile.NamedTemporaryFile(
            mode="w+",
            encoding="utf-8",
            suffix=".worker.stderr.log",
            delete=False,
        ) as stderr_handle:
            stdout_path = Path(stdout_handle.name)
            stderr_path = Path(stderr_handle.name)

            result = subprocess.run(
                command,
                cwd=str(worker_path.parent),
                stdin=subprocess.DEVNULL,
                stdout=stdout_handle,
                stderr=stderr_handle,
                text=True,
                check=False,
            )

        stdout = stdout_path.read_text(encoding="utf-8", errors="replace").strip() if stdout_path else ""
        stderr = stderr_path.read_text(encoding="utf-8", errors="replace").strip() if stderr_path else ""

        if result.returncode != 0:
            details = [f"Enrichment worker failed with exit code {result.returncode}."]

            if stderr:
                details.append(f"stderr:\n{stderr}")

            if stdout:
                details.append(f"stdout:\n{stdout}")

            raise RuntimeError("\n\n".join(details))

        if not summary_path.is_file():
            details = ["Enrichment worker completed without writing a summary file."]

            if stderr:
                details.append(f"stderr:\n{stderr}")

            if stdout:
                details.append(f"stdout:\n{stdout}")

            raise RuntimeError("\n\n".join(details))

        try:
            with summary_path.open("r", encoding="utf-8") as handle:
                summary = json.load(handle)
        finally:
            summary_path.unlink(missing_ok=True)

        if not isinstance(summary, dict):
            raise RuntimeError("Enrichment worker returned an invalid summary payload.")

        return summary

    finally:
        if progress_path is not None:
            progress_path.unlink(missing_ok=True)

        if stdout_path is not None:
            stdout_path.unlink(missing_ok=True)

        if stderr_path is not None:
            stderr_path.unlink(missing_ok=True)



def open_db(db_path: str, read_only: bool = False) -> duckdb.DuckDBPyConnection:
    resolved_db_path = Path(db_path).expanduser().resolve()
    con = duckdb.connect(str(resolved_db_path), read_only=read_only)
    con.execute("LOAD spatial;")
    con.execute(f"SET temp_directory = {sql_string(str(local_runtime_dir('duckdb_temp').resolve()))};")
    return con


def display_path(path: Path) -> str:
    try:
        return path.resolve().relative_to(Path.cwd().resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def find_local_files(suffix: str) -> List[str]:
    ignored_dirs = {".git", ".venv", "__pycache__"}
    matches = []
    for path in Path.cwd().rglob(f"*{suffix}"):
        if any(part in ignored_dirs for part in path.parts):
            continue
        if path.is_file():
            matches.append(display_path(path))
    return sorted(set(matches))


def validate_local_file(path_value: str, suffix: str, label: str) -> str:
    path = Path(path_value).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    if not path.exists() or not path.is_file():
        raise ValueError(f"{label} file does not exist: {path_value}")
    if path.suffix.lower() != suffix:
        raise ValueError(f"{label} file must end with {suffix}: {path_value}")
    return display_path(path)


def save_uploaded_file(uploaded_file, destination: Path) -> int:
    size_bytes = 0

    for _attempt in range(2):
        try:
            uploaded_file.stream.seek(0)
        except (AttributeError, OSError, ValueError):
            pass

        uploaded_file.save(destination)
        size_bytes = destination.stat().st_size if destination.exists() else 0
        if size_bytes > 0:
            return size_bytes

    return size_bytes


def derived_enrichment_download_name(upload_filename: str, suffix: str = "_enriched.csv") -> str:
    original_name = Path(upload_filename).name
    stem = Path(original_name).stem or "exposure"
    return secure_filename(f"{stem}{suffix}") or f"exposure{suffix}"


def browse_local_file(kind: str) -> Optional[str]:
    choices = {
        "parquet": (".parquet", "Parquet", "Select Parquet file"),
        "db": (".duckdb", "DuckDB", "Select DuckDB lookup database"),
    }
    if kind not in choices:
        raise ValueError("File type must be parquet or db.")

    suffix, label, title = choices[kind]

    if sys.platform == "darwin":
        script = f'POSIX path of (choose file with prompt "{title}")'
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode == 0:
            selected = result.stdout.strip()
            return validate_local_file(selected, suffix, label)
        if "User canceled" in result.stderr:
            return None
        raise RuntimeError(result.stderr.strip() or "macOS file picker failed.")

    import tkinter as tk
    from tkinter import filedialog

    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    try:
        selected = filedialog.askopenfilename(
            title=title,
            initialdir=str(Path.cwd()),
            filetypes=[(f"{label} files", f"*{suffix}"), ("All files", "*.*")],
        )
    finally:
        root.destroy()

    if not selected:
        return None
    return validate_local_file(selected, suffix, label)


def browse_local_folder() -> Optional[str]:
    title = "Select output folder"

    if sys.platform == "darwin":
        script = f'POSIX path of (choose folder with prompt "{title}")'
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode == 0:
            selected = Path(result.stdout.strip())
            return display_path(selected)
        if "User canceled" in result.stderr:
            return None
        raise RuntimeError(result.stderr.strip() or "macOS folder picker failed.")

    import tkinter as tk
    from tkinter import filedialog

    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    try:
        selected = filedialog.askdirectory(
            title=title,
            initialdir=str(Path.cwd()),
            mustexist=True,
        )
    finally:
        root.destroy()

    if not selected:
        return None
    return display_path(Path(selected))


def has_buildings_table(db_path: str) -> bool:
    con = open_db(db_path, read_only=True)
    try:
        return bool(con.execute("""
            SELECT COUNT(*)
            FROM information_schema.tables
            WHERE table_name = 'buildings';
        """).fetchone()[0])
    finally:
        con.close()


def lookup_db_path_for_parquet(parquet_path: str) -> str:
    parquet = Path(parquet_path)
    if not parquet.is_absolute():
        parquet = Path.cwd() / parquet

    name = parquet.stem
    for prefix in ("buildings_cleaned_", "buildings_de_cleaned", "buildings_cleaned"):
        if name == prefix:
            name = "building_lookup"
            break
        if name.startswith(prefix):
            name = "building_lookup_" + name[len(prefix):]
            break
    else:
        name = f"{name}_lookup"

    return display_path(parquet.with_name(f"{name}.duckdb"))


def workflow_duckdb_thread_count() -> int:
    configured = os.environ.get("OBM_WORKFLOW_THREADS", "").strip()
    if configured:
        try:
            return max(1, int(configured))
        except ValueError:
            pass

    return max(1, min(os.cpu_count() or 1, 2))


def prepare_index(
    parquet_path: str,
    db_path: str,
    force: bool = False,
    threads: int = 8,
    progress_callback: Optional[Callable[[str, int, Optional[str]], None]] = None,
) -> None:
    def report_progress_update(phase: str, percent: int, detail: Optional[str] = None) -> None:
        if progress_callback is None:
            return
        progress_callback(phase, max(0, min(99, int(percent))), detail)

    parquet = Path(parquet_path)
    if not parquet.exists():
        raise FileNotFoundError(f"Parquet file not found: {parquet}")

    db = Path(db_path)
    db.parent.mkdir(parents=True, exist_ok=True)

    if db.exists() and force:
        db.unlink()

    con = open_db(db_path)
    con.execute(f"SET threads = {int(threads)};")
    con.execute("SET preserve_insertion_order = false;")

    if not force:
        exists = con.execute("""
            SELECT COUNT(*)
            FROM information_schema.tables
            WHERE table_name = 'buildings';
        """).fetchone()[0]
        
        if exists:
            print(f"Index database already exists: {db_path}")
            print("Use --force to rebuild it.")
            report_progress_update("DuckDB lookup ready", 99, f"Index database already exists: {db_path}")
            con.close()
            return


    parquet_sql = sql_string(str(parquet))

    create_message = "Creating lookup table from Parquet. This is a one-time step. Generating 3035 Projections (May take a few minutes)..."
    print(create_message)
    report_progress_update("Creating DuckDB lookup table", 82, create_message)
    
    # Notice the new geom_3035 and bbox_3035_* columns!
    con.execute(f"""
        CREATE OR REPLACE TABLE buildings AS
        WITH raw_buildings AS (
            SELECT
                *,
                ST_GeomFromWKB(geom_wkb) AS geom
            FROM read_parquet({parquet_sql})
        ),
        projected_buildings AS (
            SELECT 
                *,
                ST_Transform(geom, 'EPSG:4326', 'EPSG:3035', always_xy := true) AS geom_3035
            FROM raw_buildings
        )
        SELECT
            building_id,
            source,
            relation_id,
            quadkey,
            quadkey_prefix_6,
            SUBSTR(CAST(quadkey AS VARCHAR), 1, 14) AS quadkey_prefix_14,
            CAST(last_update AS VARCHAR) AS last_update,
            centroid_lon,
            centroid_lat,
            bbox_xmin,
            bbox_ymin,
            bbox_xmax,
            bbox_ymax,
            footprint_area_m2,
            height_raw,
            occupancy_raw,
            floorspace_obm_m2,
            height_source_type,
            height_m,
            stories_exact,
            stories_min,
            stories_max,
            height_quality,
            occupancy_code,
            occupancy_group,
            occupancy_quality,
            floorspace_est_m2,
            attribute_completeness_score,
            geom,
            geom_3035,
            ST_XMin(geom_3035) AS bbox_3035_xmin,
            ST_YMin(geom_3035) AS bbox_3035_ymin,
            ST_XMax(geom_3035) AS bbox_3035_xmax,
            ST_YMax(geom_3035) AS bbox_3035_ymax
        FROM projected_buildings
        ;
    """)

    print("Creating spatial index.")
    report_progress_update("Creating spatial indexes", 92, "Creating spatial index.")
    con.execute("CREATE INDEX buildings_geom_rtree ON buildings USING RTREE (geom);")
    con.execute("CREATE INDEX buildings_geom_3035_rtree ON buildings USING RTREE (geom_3035);")
    con.execute("CREATE INDEX buildings_quadkey_prefix_14_idx ON buildings(quadkey_prefix_14);")

    row_count = con.execute("SELECT COUNT(*) FROM buildings;").fetchone()[0]
    con.close()
    ready_message = f"Ready: {db_path} ({row_count:,} buildings)"
    print(ready_message)
    report_progress_update("Finalizing DuckDB lookup table", 98, ready_message)

def create_app(
    db_path: str = DEFAULT_DB,
    nearest_radius_m: float = 50.0,
    upload_dir: Optional[str] = None,
    result_dir: Optional[str] = None,
) -> Flask:
    app = Flask(__name__)
    app.config["DB_PATH"] = db_path
    app.config["DB_CONN"] = None
    app.config["DB_CONN_PATH"] = ""
    app.config["PARQUET_PATH"] = DEFAULT_PARQUET
    app.config["NEAREST_RADIUS_M"] = float(nearest_radius_m)
    app.config["GEOCODER_USER_AGENT"] = "OBMBuildingLookup/0.1 local-development"
    app.config["UPLOAD_DIR"] = upload_dir or str(Path(__file__).resolve().parent / "etl_output" / "app_uploads")
    app.config["RESULT_DIR"] = result_dir or str(Path(__file__).resolve().parent / "etl_output" / "app_results")
    app.config["COUNTRY_BOUNDARY_CATALOG"] = str(DEFAULT_COUNTRY_BOUNDARY_CATALOG)
    geocode_cache: Dict[str, Any] = {}
    last_geocode_at = [0.0]
    jobs: Dict[str, Dict[str, Any]] = {}
    jobs_lock = Lock()
    db_conn_lock = Lock()
    filter_view_metadata_lock = Lock()
    filter_view_metadata_cache: Dict[
        str,
        Tuple[int, int, Set[str], Tuple[str, int, bool]],
    ] = {}
    exposure_map_conn_lock = Lock()
    exposure_map_connection: List[Optional[duckdb.DuckDBPyConnection]] = [None]
    exposure_map_connection_path = [""]
    enrichment_lock = Lock()
    exposure_map_cache_lock = Lock()
    latest_upload_id: List[Optional[str]] = [None]
    for _startup_dir in (app.config["UPLOAD_DIR"], app.config["RESULT_DIR"]):
        _dir = Path(_startup_dir)
        _dir.mkdir(parents=True, exist_ok=True)
        for _f in _dir.iterdir():
            if _f.is_file():
                _f.unlink(missing_ok=True)
    register_custom_parquet_routes(app)
    register_layer_upload_routes(app)
    register_raster_intersection_routes(
        app,
        find_upload=find_upload,
        prepare_exposure_map_cache=prepare_exposure_map_cache,
        open_db=open_db,
        convert_excel_to_csv=convert_excel_to_csv,
    )

    def set_job(job_id: str, **updates: Any) -> None:
        with jobs_lock:
            jobs.setdefault(job_id, {}).update(updates)

    def cached_readonly_db_connection(db_path: str) -> duckdb.DuckDBPyConnection:
        resolved_db_path = str(Path(db_path).expanduser().resolve())
        old_con: Optional[duckdb.DuckDBPyConnection] = None

        with db_conn_lock:
            cached_con = app.config.get("DB_CONN")
            cached_path = app.config.get("DB_CONN_PATH") or ""

            if cached_con is not None and cached_path == resolved_db_path:
                return cached_con

            old_con = cached_con
            new_con = open_db(resolved_db_path, read_only=True)
            app.config["DB_CONN"] = new_con
            app.config["DB_CONN_PATH"] = resolved_db_path

        if old_con is not None:
            try:
                old_con.close()
            except Exception:
                pass

        return new_con

    def cached_readonly_db_cursor(db_path: str) -> duckdb.DuckDBPyConnection:
        return cached_readonly_db_connection(db_path).cursor()

    def cached_filter_view_metadata(
        db_path: str,
        con: duckdb.DuckDBPyConnection,
    ) -> Tuple[Set[str], Tuple[str, int, bool]]:
        resolved_db_path = str(Path(db_path).expanduser().resolve())
        stat = Path(resolved_db_path).stat()

        with filter_view_metadata_lock:
            cached = filter_view_metadata_cache.get(resolved_db_path)
            if cached and cached[:2] == (stat.st_mtime_ns, stat.st_size):
                return cached[2], cached[3]

            available_columns = set(lookup_display_columns(con))
            quadkey_config = enrichment_quadkey_config(con)
            filter_view_metadata_cache[resolved_db_path] = (
                stat.st_mtime_ns,
                stat.st_size,
                available_columns,
                quadkey_config,
            )
            return available_columns, quadkey_config

    def cached_exposure_map_cursor(
        cache_path: Path,
    ) -> duckdb.DuckDBPyConnection:
        resolved_cache_path = str(cache_path.resolve())
        with exposure_map_conn_lock:
            cached_con = exposure_map_connection[0]
            if (
                cached_con is None
                or exposure_map_connection_path[0] != resolved_cache_path
            ):
                if cached_con is not None:
                    cached_con.close()
                cached_con = duckdb.connect(resolved_cache_path, read_only=True)
                exposure_map_connection[0] = cached_con
                exposure_map_connection_path[0] = resolved_cache_path
            return cached_con.cursor()

    def close_cached_exposure_map_connection() -> None:
        with exposure_map_conn_lock:
            cached_con = exposure_map_connection[0]
            if cached_con is not None:
                cached_con.close()
            exposure_map_connection[0] = None
            exposure_map_connection_path[0] = ""
    
    

    def log_flask_memory(label: str) -> None:
        import threading

        process = psutil.Process(os.getpid())
        memory_mb = round(process.memory_info().rss / 1024 / 1024, 1)

        print(
            f"[MEMORY] {label}: "
            f"Flask memory = {memory_mb} MB, "
            f"Python active threads = {threading.active_count()}, "
            f"OS threads = {process.num_threads()}",
            flush=True,
        )



    def prune_files(
        directory: Path,
        pattern: str,
        keep_names: Set[str],
        max_retained: int,
    ) -> None:
        now = time.time()
        files = sorted(
            (path for path in directory.glob(pattern) if path.is_file()),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )

        retained = 0
        for path in files:
            if path.name in keep_names:
                retained += 1
                continue

            try:
                age_seconds = now - path.stat().st_mtime
            except FileNotFoundError:
                continue

            if retained >= max_retained or age_seconds > EXPOSURE_ARTIFACT_MAX_AGE_SECONDS:
                path.unlink(missing_ok=True)
            else:
                retained += 1

    def cleanup_exposure_runtime() -> None:
        now = time.time()
        keep_upload_ids = {upload_id for upload_id in latest_upload_id if upload_id}
        keep_result_names: Set[str] = set()
        completed_jobs: List[Tuple[str, float]] = []

        with jobs_lock:
            for job_id, job in jobs.items():
                status = job.get("status")
                if status in {"queued", "running"}:
                    upload_id = job.get("upload_id")
                    output_filename = job.get("output_filename")
                    if isinstance(upload_id, str):
                        keep_upload_ids.add(upload_id)
                    if isinstance(output_filename, str):
                        keep_result_names.add(output_filename)
                    continue

                completed_at = float(job.get("completed_at") or job.get("created_at") or now)
                completed_jobs.append((job_id, completed_at))

            stale_job_ids = {
                job_id
                for job_id, completed_at in completed_jobs
                if now - completed_at > EXPOSURE_ARTIFACT_MAX_AGE_SECONDS
            }
            completed_jobs.sort(key=lambda item: item[1], reverse=True)
            stale_job_ids.update(
                job_id
                for job_id, _completed_at in completed_jobs[MAX_RETAINED_EXPOSURE_JOBS:]
            )

            for job_id in stale_job_ids:
                jobs.pop(job_id, None)

            for job in jobs.values():
                upload_id = job.get("upload_id")
                output_filename = job.get("output_filename")
                if isinstance(upload_id, str) and job.get("status") in {"queued", "running"}:
                    keep_upload_ids.add(upload_id)
                if isinstance(output_filename, str):
                    keep_result_names.add(output_filename)

        upload_keep_names = {
            path.name
            for upload_id in keep_upload_ids
            for path in Path(app.config["UPLOAD_DIR"]).glob(f"{upload_id}_*")
            if path.is_file() and path.suffix.lower() in SUPPORTED_EXPOSURE_UPLOAD_EXTENSIONS
        }
        prune_files(
            Path(app.config["UPLOAD_DIR"]),
            "*.csv",
            upload_keep_names,
            MAX_RETAINED_EXPOSURE_UPLOADS,
        )
        prune_files(
            Path(app.config["UPLOAD_DIR"]),
            "*.xlsx",
            upload_keep_names,
            MAX_RETAINED_EXPOSURE_UPLOADS,
        )
        prune_files(
            Path(app.config["RESULT_DIR"]),
            "enriched_*.csv",
            keep_result_names,
            MAX_RETAINED_EXPOSURE_RESULTS,
        )

    @app.route("/")
    def index():
        return render_template(
            "index.html",
            filter_view_min_zoom=FILTER_VIEW_MIN_ZOOM,
            filter_view_max_tile_zoom=FILTER_VIEW_MAX_TILE_ZOOM,
            exposure_raw_point_zoom=RAW_POINT_ZOOM,
        )

    @app.route("/help/readme")
    def open_readme_help():
        readme_path = Path(__file__).resolve().with_name("README.md")
        if not readme_path.is_file():
            return jsonify({"error": "README file not found."}), 404
        readme_text = readme_path.read_text(encoding="utf-8")
        manual_html = Markup(_render_help_markdown(readme_text))
        return render_template(
            "help_readme.html",
            page_title="Data Augmentation Tool User Manual",
            manual_html=manual_html,
        )

    
    @app.route("/api/health")
    def health():
        db_path = app.config.get("DB_PATH") or ""
        parquet_path = app.config.get("PARQUET_PATH") or ""

        db_exists = bool(db_path) and Path(db_path).is_file()
        parquet_exists = bool(parquet_path) and Path(parquet_path).is_file()

        return jsonify({
            "ok": db_exists,
            "db_path": db_path,
            "parquet_path": parquet_path,
            "db_exists": db_exists,
            "parquet_exists": parquet_exists,
        })



    @app.route("/api/data-source", methods=["GET", "POST"])
    def data_source():
        if request.method == "GET":
            return jsonify({
                "db_path": app.config["DB_PATH"],
                "db_files": find_local_files(".duckdb"),
            })

        payload = request.get_json(silent=True) or {}
        db_path = str(payload.get("db_path", "")).strip()

        if not db_path:
            return jsonify({"error": "DuckDB path is required."}), 400

        try:
            db_path = validate_local_file(db_path, ".duckdb", "DuckDB")
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400

        generated_lookup = False
        try:
            has_buildings = has_buildings_table(db_path)
        except Exception as exc:
            return jsonify({"error": f"Could not open DuckDB lookup database: {exc}"}), 400

        if not has_buildings:
            return jsonify({
                "error": "The selected DuckDB file is not a lookup database. Choose a DuckDB file that already contains the buildings table."
            }), 400

        try:
            cached_readonly_db_connection(db_path)
        except Exception as exc:
            return jsonify({"error": f"Could not keep DuckDB lookup database open: {exc}"}), 400

        app.config["DB_PATH"] = db_path
        return jsonify({
            "db_path": db_path,
            "status": "active",
            "generated_lookup": generated_lookup,
        })

    @app.route("/api/browse-file")
    def browse_file():
        kind = str(request.args.get("kind", "")).strip()
        try:
            selected_path = browse_local_file(kind)
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        except Exception as exc:
            return jsonify({
                "error": (
                    "Native file picker is not available from this Flask session. "
                    f"{exc}"
                )
            }), 501

        if selected_path is None:
            return jsonify({"cancelled": True})
        return jsonify({"path": selected_path})

    @app.route("/api/browse-folder")
    def browse_folder():
        try:
            selected_path = browse_local_folder()
        except Exception as exc:
            return jsonify({
                "error": (
                    "Native folder picker is not available from this Flask session. "
                    f"{exc}"
                )
            }), 501

        if selected_path is None:
            return jsonify({"cancelled": True})
        return jsonify({"path": selected_path})

    @app.route("/api/building-at")
    def building_at():
        try:
            lon = float(request.args["lon"])
            lat = float(request.args["lat"])
        except (KeyError, ValueError):
            return jsonify({"error": "Valid lon and lat query parameters are required."}), 400

        if not (-180 <= lon <= 180 and -90 <= lat <= 90):
            return jsonify({"error": "Coordinates are out of range."}), 400


        db_path = app.config.get("DB_PATH") or ""

        if not db_path or not Path(db_path).is_file():
            return jsonify({
                "error": "Lookup database has not been selected or prepared.",
                "hint": "Create/select a lookup database from the app first."
            }), 503


        con = cached_readonly_db_cursor(db_path)
        try:
            result = find_building(con, lon, lat, app.config["NEAREST_RADIUS_M"])
        finally:
            con.close()

        if result is None:
            return jsonify({
                "match_type": "none",
                "distance_m": None,
                "confidence": "none",
                "building": None,
            })

        return jsonify(result)

    @app.route("/api/building-fields")
    def building_fields():
        db_path = app.config.get("DB_PATH") or ""

        if not db_path or not Path(db_path).is_file():
            return jsonify({
                "fields": [],
                "error": "Lookup database has not been selected or prepared."
            }), 200

        con = cached_readonly_db_cursor(db_path)
        try:
            fields = lookup_display_columns(con)
            preferred_fields = preferred_display_fields(con)
        finally:
            con.close()

        return jsonify({
            "fields": fields,
            "filter_fields": fields,
            "preferred_fields": preferred_fields,
            "default_fields": default_enrichment_fields(fields, preferred_fields),
        })

    @app.route("/api/building-filter-values")
    def building_filter_values():
        column = str(request.args.get("column", "")).strip()

        if not column:
            return jsonify({"error": "A filter column is required."}), 400

        db_path = app.config.get("DB_PATH") or ""
        if not db_path or not Path(db_path).is_file():
            return jsonify({
                "values": [],
                "error": "Lookup database has not been selected or prepared."
            }), 200

        con = cached_readonly_db_cursor(db_path)
        try:
            values = lookup_filter_values(con, column)
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        finally:
            con.close()

        return jsonify({
            "column": column,
            "values": values,
        })

    @app.route("/api/building-filter-summary")
    def building_filter_summary():
        try:
            min_lon = float(request.args["min_lon"])
            min_lat = float(request.args["min_lat"])
            max_lon = float(request.args["max_lon"])
            max_lat = float(request.args["max_lat"])
            zoom = float(request.args["zoom"])
        except (KeyError, ValueError):
            return jsonify({
                "error": "Valid min_lon, min_lat, max_lon, max_lat, and zoom query parameters are required."
            }), 400

        column = str(request.args.get("column", "")).strip()
        value = str(request.args.get("value", "")).strip()

        if not column or not value:
            return jsonify({"error": "Both filter column and value are required."}), 400

        min_lon, max_lon = sorted((min_lon, max_lon))
        min_lat, max_lat = sorted((min_lat, max_lat))
            
        if not (
            -180 <= min_lon <= 180
            and -180 <= max_lon <= 180
            and -90 <= min_lat <= 90
            and -90 <= max_lat <= 90
            and math.isfinite(zoom)
        ):
            return jsonify({"error": "Viewport coordinates are out of range."}), 400

        if zoom < FILTER_VIEW_MIN_ZOOM:
            return jsonify({
                "count": 0,
                "legend": [],
                "below_min_zoom": True,
                "min_zoom": FILTER_VIEW_MIN_ZOOM,
            })

        db_path = app.config.get("DB_PATH") or ""
        if not db_path or not Path(db_path).is_file():
            return jsonify({
                "error": "Lookup database has not been selected or prepared.",
                "hint": "Create/select a lookup database from the app first."
            }), 503

        con = cached_readonly_db_cursor(db_path)
        try:
            available_columns, quadkey_config = cached_filter_view_metadata(db_path, con)
            summary = lookup_building_filter_summary(
                con,
                min_lon=min_lon,
                min_lat=min_lat,
                max_lon=max_lon,
                max_lat=max_lat,
                column=column,
                value=value,
                available_columns=available_columns,
                quadkey_config=quadkey_config,
            )
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        finally:
            con.close()

        return jsonify(summary)

    @app.route("/api/tiles/<int:z>/<int:x>/<int:y>.mvt")
    def building_filter_tiles(z: int, x: int, y: int):
        if z < 0:
            return jsonify({"error": "Tile zoom must be non-negative."}), 400
        if z > FILTER_VIEW_MAX_TILE_ZOOM:
            return jsonify({
                "error": f"Filter-view tiles are available through zoom {FILTER_VIEW_MAX_TILE_ZOOM}."
            }), 400

        tile_count = 1 << z
        if x < 0 or y < 0 or x >= tile_count or y >= tile_count:
            return jsonify({"error": "Tile coordinates are out of range."}), 400

        column = str(request.args.get("column", "")).strip()
        value = str(request.args.get("value", "")).strip()
        color = str(request.args.get("color", "")).strip() or None

        if not column or not value:
            return jsonify({"error": "Both filter column and value are required."}), 400

        if z < FILTER_VIEW_MIN_ZOOM:
            return Response(status=204)

        db_path = app.config.get("DB_PATH") or ""
        if not db_path or not Path(db_path).is_file():
            return jsonify({
                "error": "Lookup database has not been selected or prepared.",
                "hint": "Create/select a lookup database from the app first."
            }), 503

        con = cached_readonly_db_cursor(db_path)
        try:
            available_columns, quadkey_config = cached_filter_view_metadata(db_path, con)
            tile_blob = lookup_buildings_mvt(
                con,
                z=z,
                x=x,
                y=y,
                column=column,
                value=value,
                color=color,
                available_columns=available_columns,
                quadkey_config=quadkey_config,
            )
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        finally:
            con.close()

        response = Response(tile_blob, mimetype="application/vnd.mapbox-vector-tile")
        response.headers["Cache-Control"] = "public, max-age=3600"
        return response


    @app.route("/api/search-address")
    def search_address():
        query = request.args.get("q", "").strip()

        if len(query) < 3:
            return jsonify({"error": "Enter at least 3 characters."}), 400

        cache_key = query.casefold()
        if cache_key in geocode_cache:
            return jsonify({"results": geocode_cache[cache_key]})

        elapsed = time.time() - last_geocode_at[0]
        if elapsed < 1.0:
            time.sleep(1.0 - elapsed)

        errors = []
        results = []
        provider_succeeded = False
        for geocoder in (geocode_with_nominatim, geocode_with_photon):
            try:
                results = geocoder(query, app.config["GEOCODER_USER_AGENT"])
                provider_succeeded = True
            except Exception as exc:
                errors.append(f"{geocoder.__name__}: {exc}")
            finally:
                last_geocode_at[0] = time.time()

            if results:
                geocode_cache[cache_key] = results
                return jsonify({"results": results})

        if errors and not provider_succeeded:
            return jsonify({
                "error": "Address search failed. " + " | ".join(errors)
            }), 502

        return jsonify({"results": []})

    @app.route("/api/exposure/preview", methods=["POST"])
    def exposure_preview():
        file = request.files.get("file")

        if file is None or not file.filename:
            return jsonify({"error": "Upload a CSV or Excel (.xlsx) file."}), 400

        upload_id = uuid.uuid4().hex
        upload_path: Optional[Path] = None

        try:
            upload_path, filename = prepare_exposure_upload(
                file,
                Path(app.config["UPLOAD_DIR"]),
                upload_id,
            )
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400

        try:
            columns, rows = preview_uploaded_file(upload_path)
        except Exception as exc:
            upload_path.unlink(missing_ok=True)
            return jsonify({"error": f"Could not read file: {exc}"}), 400

        latest_upload_id[0] = upload_id
        cleanup_exposure_runtime()

        return jsonify({
            "upload_id": upload_id,
            "filename": filename,
            "columns": columns,
            "rows": rows,
        })

    @app.route("/api/exposure/map-points")
    def exposure_map_points():
        upload_id = str(request.args.get("upload_id", "")).strip()
        lat_col = str(request.args.get("lat_col", "")).strip()
        lon_col = str(request.args.get("lon_col", "")).strip()

        if not upload_id or not lat_col or not lon_col:
            return jsonify({"error": "Upload id, latitude column, and longitude column are required."}), 400

        upload_path = find_upload(Path(app.config["UPLOAD_DIR"]), upload_id)
        if upload_path is None:
            return jsonify({"error": "Uploaded file was not found. Upload it again."}), 404

        has_bounds = all(
            key in request.args
            for key in ("min_lon", "min_lat", "max_lon", "max_lat")
        )
        cache_path = exposure_map_cache_path(upload_path, upload_id, lat_col, lon_col)
        try:
            if has_bounds and cache_path.is_file():
                metadata = {}
            else:
                close_cached_exposure_map_connection()
                with exposure_map_cache_lock:
                    cache_path, metadata = prepare_exposure_map_cache(
                        upload_path,
                        upload_id,
                        lat_col,
                        lon_col,
                    )
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        except Exception as exc:
            return jsonify({"error": f"Could not prepare exposure map points: {exc}"}), 500

        if not has_bounds:
            return jsonify({
                **metadata,
                "type": "FeatureCollection",
                "features": [],
                "visible_count": 0,
                "returned_count": 0,
                "cell_count": 0,
            })

        try:
            min_lon = float(request.args["min_lon"])
            min_lat = float(request.args["min_lat"])
            max_lon = float(request.args["max_lon"])
            max_lat = float(request.args["max_lat"])
            width = int(float(request.args.get("width", 1200)))
            height = int(float(request.args.get("height", 800)))
            limit = int(float(request.args.get("limit", EXPOSURE_MAP_MAX_FEATURES)))
            zoom = float(request.args.get("zoom", 0))
        except (KeyError, TypeError, ValueError):
            return jsonify({
                "error": "Valid min_lon, min_lat, max_lon, max_lat, width, height, and zoom query parameters are required."
            }), 400

        if not (
            math.isfinite(min_lon)
            and math.isfinite(min_lat)
            and math.isfinite(max_lon)
            and math.isfinite(max_lat)
            and math.isfinite(zoom)
        ):
            return jsonify({"error": "Map bounds must be finite numeric values."}), 400

        point_con: Optional[duckdb.DuckDBPyConnection] = None
        try:
            point_con = cached_exposure_map_cursor(cache_path)
            points_payload = lookup_exposure_points_in_view(
                cache_path=cache_path,
                min_lon=min_lon,
                min_lat=min_lat,
                max_lon=max_lon,
                max_lat=max_lat,
                width=width,
                height=height,
                max_features=limit,
                zoom=zoom,
                con=point_con,
            )
        except Exception as exc:
            return jsonify({"error": f"Could not load exposure map points: {exc}"}), 500
        finally:
            if point_con is not None:
                point_con.close()
        if request.args.get("format") == "compact":
            compact_points = []
            for feature in points_payload.pop("features", []):
                coordinates = feature.get("geometry", {}).get("coordinates", [])
                properties = feature.get("properties", {})
                if len(coordinates) < 2:
                    continue
                compact_points.append([
                    coordinates[0],
                    coordinates[1],
                    properties.get("row_id", 0),
                    properties.get("csv_count", 1),
                    properties.get("csv_label", "1"),
                    properties.get("duplicate_count", 1),
                ])
            points_payload["points"] = compact_points

        return jsonify({**metadata, **points_payload})

    @app.route("/api/exposure/row")
    def exposure_row_detail():
        upload_id = str(request.args.get("upload_id", "")).strip()
        lat_col = str(request.args.get("lat_col", "")).strip()
        lon_col = str(request.args.get("lon_col", "")).strip()

        if not upload_id or not lat_col or not lon_col:
            return jsonify({"error": "Upload id, latitude column, and longitude column are required."}), 400

        try:
            row_id = int(float(request.args.get("row_id", "")))
        except (TypeError, ValueError):
            return jsonify({"error": "A valid CSV row id is required."}), 400

        if row_id < 1:
            return jsonify({"error": "CSV row id must be positive."}), 400

        upload_path = find_upload(Path(app.config["UPLOAD_DIR"]), upload_id)
        if upload_path is None:
            return jsonify({"error": "Uploaded file was not found. Upload it again."}), 404

        try:
            cache_path = exposure_map_cache_path(upload_path, upload_id, lat_col, lon_col)
            if cache_path.is_file() and exposure_map_cache_has_row_details(cache_path):
                metadata = read_exposure_map_cache_metadata(cache_path)
            else:
                with exposure_map_cache_lock:
                    cache_path, metadata = prepare_exposure_map_cache(upload_path, upload_id, lat_col, lon_col)
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        except Exception as exc:
            return jsonify({"error": f"Could not prepare exposure row details: {exc}"}), 500

        try:
            row_payload = lookup_exposure_csv_row(cache_path, row_id)
        except Exception as exc:
            return jsonify({"error": f"Could not load exposure row: {exc}"}), 500

        if row_payload is None:
            return jsonify({"error": "CSV row was not found."}), 404

        return jsonify({
            **metadata,
            "row": row_payload,
        })

    @app.route("/api/exposure/enrich", methods=["POST"])
    def exposure_enrich():
        payload = request.get_json(silent=True) or {}
        upload_id = str(payload.get("upload_id", ""))
        lat_col = str(payload.get("lat_col", ""))
        lon_col = str(payload.get("lon_col", ""))
        mode = str(payload.get("mode", "inside_nearest"))
        requested_fields = payload.get("appended_fields")

        try:
            max_distance_m = float(payload.get("max_distance_m", app.config["NEAREST_RADIUS_M"]))
        except (TypeError, ValueError):
            return jsonify({"error": "Max distance must be numeric."}), 400

        if not upload_id or not lat_col or not lon_col:
            return jsonify({"error": "Upload id, latitude column, and longitude column are required."}), 400

        if mode not in {"centroid", "inside", "inside_nearest"}:
            return jsonify({"error": "Unknown matching mode."}), 400

        
        db_path = app.config.get("DB_PATH") or ""

        if not db_path or not Path(db_path).is_file():
            return jsonify({
                "error": "Lookup database has not been selected or prepared. Create/select a lookup database first."
            }), 400

        con = open_db(db_path, read_only=True)

        try:
            available_fields = lookup_display_columns(con)
            preferred_fields = preferred_display_fields(con)
            default_fields = default_enrichment_fields(available_fields, preferred_fields)
        finally:
            con.close()

        if requested_fields is None:
            appended_fields = default_fields
        elif not isinstance(requested_fields, list):
            return jsonify({"error": "Appended fields must be a list."}), 400
        else:
            appended_fields = list(dict.fromkeys(str(field) for field in requested_fields))
            invalid_fields = [field for field in appended_fields if field not in available_fields]
            if invalid_fields:
                return jsonify({
                    "error": f"Unknown appended database field: {invalid_fields[0]}"
                }), 400

        
        upload_path = find_upload(Path(app.config["UPLOAD_DIR"]), upload_id)
        if upload_path is None:
            return jsonify({"error": "Uploaded file was not found. Upload it again."}), 404

        with jobs_lock:
            active_job = next(
                (
                    existing_job_id
                    for existing_job_id, job in jobs.items()
                    if job.get("status") in {"queued", "running"}
                ),
                None,
            )

        if active_job is not None:
            return jsonify({
                "error": "Another enrichment is already running. Please wait for it to finish.",
                "active_job_id": active_job,
            }), 409

        job_id = uuid.uuid4().hex
        original_upload_name = upload_path.name.partition("_")[2] or upload_path.name
        download_name = derived_enrichment_download_name(original_upload_name)

        output_path = Path(app.config["RESULT_DIR"]) / f"enriched_{job_id}.csv"
        progress_path = output_path.with_suffix(".progress.json")
        set_job(
            job_id,
            status="queued",
            phase="Queued",
            percent=1,
            download_url=None,
            summary=None,
            error=None,
            created_at=time.time(),
            upload_id=upload_id,
            output_filename=output_path.name,
            download_name=download_name,
            _progress_path=str(progress_path),
        )

        def run_job() -> None:
            try:
                log_flask_memory(f"Before enrichment job {job_id}")
                if not enrichment_lock.acquire(blocking=False):
                    raise RuntimeError("Another enrichment is already running.")

                try:
                    set_job(
                        job_id,
                        status="running",
                        phase="Starting enrichment worker",
                        percent=6,
                    )
                    summary = run_enrichment_worker(
                        db_path=db_path,
                        csv_path=upload_path,
                        output_path=output_path,
                        lat_col=lat_col,
                        lon_col=lon_col,
                        mode=mode,
                        max_distance_m=max_distance_m,
                        appended_fields=appended_fields,
                        progress_path=progress_path,
                    )
                    log_flask_memory(f"After enrichment job {job_id}")
                finally:
                    enrichment_lock.release()

                set_job(
                    job_id,
                    status="complete",
                    phase="Complete",
                    percent=100,
                    download_url=(
                        f"/api/exposure/download/{output_path.name}"
                        f"?download_name={urllib.parse.quote(download_name)}"
                    ),
                    download_name=download_name,
                    summary=summary,
                    completed_at=time.time(),
                    _progress_path=None,
                )
            except Exception as exc:
                output_path.unlink(missing_ok=True)
                progress_path.unlink(missing_ok=True)
                set_job(
                    job_id,
                    status="error",
                    phase="Error",
                    percent=100,
                    error=f"Enrichment failed: {exc}",
                    completed_at=time.time(),
                    _progress_path=None,
                )
            finally:
                cleanup_exposure_runtime()

        Thread(target=run_job, daemon=True).start()

        return jsonify({"job_id": job_id, "status": "queued"}), 202

    @app.route("/api/exposure/progress/<job_id>")
    def exposure_progress(job_id: str):
        with jobs_lock:
            stored_job = jobs.get(job_id)

        if stored_job is None:
            return jsonify({"error": "Job not found."}), 404

        job = dict(stored_job)
        progress_file = job.get("_progress_path")
        if job.get("status") in {"queued", "running"} and isinstance(progress_file, str) and progress_file:
            progress_update = read_progress_snapshot(Path(progress_file))
            if progress_update is not None:
                set_job(job_id, **progress_update)
                job.update(progress_update)

        job.pop("_progress_path", None)

        return jsonify(job)

    @app.route("/api/exposure/download/<path:filename>")
    def exposure_download(filename: str):
        safe_name = secure_filename(filename)
        output_path = Path(app.config["RESULT_DIR"]) / safe_name

        if not output_path.exists():
            return jsonify({"error": "Result file was not found."}), 404

        requested_download_name = secure_filename(str(request.args.get("download_name", "")).strip())
        download_name = requested_download_name or safe_name

        return send_file(output_path, as_attachment=True, download_name=download_name)

    # ------------------------------------------------------------------
    # ETL: Create OBM Database
    # ------------------------------------------------------------------

    etl_jobs: Dict[str, Dict[str, Any]] = {}
    etl_jobs_lock = Lock()

    def set_etl_job(job_id: str, **updates: Any) -> None:
        with etl_jobs_lock:
            etl_jobs.setdefault(job_id, {}).update(updates)

    @app.route("/api/etl/countries", methods=["GET"])
    def etl_country_catalog():
        try:
            countries = list_catalog_countries(app.config["COUNTRY_BOUNDARY_CATALOG"])
        except FileNotFoundError as exc:
            return jsonify({"error": str(exc)}), 404
        except Exception as exc:
            return jsonify({"error": f"Could not load country catalog: {exc}"}), 500

        return jsonify({"countries": countries})

    @app.route("/api/etl/create-database", methods=["POST"])
    def etl_create_database():
        # ---------- boundary file (optional) ----------
        boundary_file_path: Optional[str] = None
        country_key = request.form.get("country_key", "").strip() or None
        boundary_file = request.files.get("boundary_file")
        if boundary_file and boundary_file.filename:
            filename = secure_filename(boundary_file.filename)
            ext = Path(filename).suffix.lower()
            if ext not in {".zip", ".gpkg"}:
                return jsonify({
                    "error": "Boundary file must be a .gpkg or a .zip containing the shapefile sidecars."
                }), 400
            upload_dir = Path(app.config["UPLOAD_DIR"])
            saved_path = upload_dir / f"{uuid.uuid4().hex}_{filename}"
            boundary_file.save(saved_path)
            boundary_file_path = str(saved_path)

        # ---------- config fields ----------
        def _str(key: str, default: str) -> str:
            val = request.form.get(key, "").strip()
            return val if val else default

        output_dir = _str("output_dir", "./etl_output")

        def _output_path(key: str, default_filename: str, suffix: str, label: str) -> str:
            raw_value = request.form.get(key, "").strip()
            path = Path(raw_value) if raw_value else Path(output_dir) / default_filename
            if raw_value and not path.is_absolute() and path.parent == Path("."):
                path = Path(output_dir) / path
            if path.suffix.lower() != suffix:
                raise ValueError(f"{label} must end with {suffix}.")
            return path.as_posix()

        def _derived_work_duckdb_path(parquet_path: str) -> str:
            parquet = Path(parquet_path)
            return parquet.with_name(f"{parquet.stem}_obm.duckdb").as_posix()

        try:
            output_parquet = _output_path("output_parquet", "buildings_cleaned.parquet", ".parquet", "Parquet output")
            duckdb_file = _derived_work_duckdb_path(output_parquet)
            lookup_db_file = _output_path("lookup_db_file", "building_lookup.duckdb", ".duckdb", "DuckDB lookup table")
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400

        duckdb_abs = (Path.cwd() / duckdb_file).resolve() if not Path(duckdb_file).is_absolute() else Path(duckdb_file).resolve()
        lookup_abs = (Path.cwd() / lookup_db_file).resolve() if not Path(lookup_db_file).is_absolute() else Path(lookup_db_file).resolve()
        if duckdb_abs == lookup_abs:
            return jsonify({"error": "DuckDB work file and DuckDB lookup table must be different files."}), 400

        cfg = ETLConfig(
            output_dir=output_dir,
            output_parquet=output_parquet,
            duckdb_file=duckdb_file,
            threads=workflow_duckdb_thread_count(),
            temp_directory=f"{output_dir}/duckdb_temp",
            boundary_file=boundary_file_path,
            country_boundary_catalog=app.config["COUNTRY_BOUNDARY_CATALOG"],
            country_key=None if boundary_file_path else country_key,
            force=True,
        )

        job_id = uuid.uuid4().hex
        set_etl_job(
            job_id,
            status="running",
            phase="Starting ETL",
            percent=1,
            detail="Waiting for ETL worker",
            error=None,
            output_parquet=cfg.output_parquet,
            duckdb_file=cfg.duckdb_file,
            lookup_db_file=lookup_db_file,
        )

        def run_etl() -> None:
            def report_etl_progress(phase: str, percent: int, detail: Optional[str] = None) -> None:
                bounded_percent = max(0, min(99, int(percent)))
                with etl_jobs_lock:
                    job = etl_jobs.setdefault(job_id, {})
                    current_percent = int(job.get("percent") or 0)
                    if job.get("status") == "running":
                        bounded_percent = max(current_percent, bounded_percent)
                    job.update({
                        "status": "running",
                        "phase": phase,
                        "percent": bounded_percent,
                    })
                    if detail is not None:
                        job["detail"] = detail

            try:
                report_etl_progress("Preparing ETL workspace", 3, "Starting OpenBuildingMap country ETL")
                etl = OpenBuildingMapCountryETL(cfg)
                etl.progress_callback = report_etl_progress
                etl.run()
                set_etl_job(
                    job_id,
                    boundary_extent={
                        "lon_min": cfg.lon_min,
                        "lon_max": cfg.lon_max,
                        "lat_min": cfg.lat_min,
                        "lat_max": cfg.lat_max,
                    },
                )
                prepare_index(
                    cfg.output_parquet,
                    lookup_db_file,
                    force=True,
                    threads=workflow_duckdb_thread_count(),
                    progress_callback=report_etl_progress,
                )
                app.config["PARQUET_PATH"] = display_path(Path(cfg.output_parquet))
                app.config["DB_PATH"] = display_path(Path(lookup_db_file))
                set_etl_job(
                    job_id,
                    status="complete",
                    phase="Complete",
                    percent=100,
                    detail="Database created successfully.",
                )
            except Exception as exc:
                set_etl_job(job_id, status="error", phase="Error", percent=100, error=str(exc))

        Thread(target=run_etl, daemon=True).start()
        return jsonify({"job_id": job_id, "status": "running"}), 202

    @app.route("/api/etl/progress/<job_id>")
    def etl_progress(job_id: str):
        with etl_jobs_lock:
            job = etl_jobs.get(job_id)
        if job is None:
            return jsonify({"error": "Job not found."}), 404
        return jsonify(job)

    return app


def find_upload(upload_dir: Path, upload_id: str) -> Optional[Path]:
    matches = [
        path
        for path in upload_dir.glob(f"{upload_id}_*")
        if path.is_file() and path.suffix.lower() in SUPPORTED_EXPOSURE_UPLOAD_EXTENSIONS
    ]
    return matches[0] if matches else None


def preview_uploaded_file(upload_path: Path) -> tuple[List[str], List[Dict[str, Any]]]:
    if upload_path.stat().st_size == 0:
        raise ValueError("Uploaded file is empty.")

    if upload_path.suffix.lower() == ".xlsx":
        try:
            return preview_excel_file(upload_path)
        except ValueError:
            raise
        except Exception as exc:
            raise ValueError(f"Could not parse the uploaded Excel file: {exc}") from exc

    con = duckdb.connect()
    try:
        con.execute(f"SET temp_directory = {sql_string(str(local_runtime_dir('duckdb_temp').resolve()))};")
        scan_options_sql = resolve_csv_scan_options(con, upload_path)
        csv_sql = sql_string(str(upload_path.resolve()))
        result = con.execute(f"""
            SELECT *
            FROM read_csv_auto({csv_sql}, {scan_options_sql})
            LIMIT 10;
        """)
        columns = [str(description[0]) for description in (result.description or [])]
        if not columns:
            raise ValueError("Uploaded file does not contain any readable columns.")

        rows = [
            {
                column: json_safe(raw_row[index])
                for index, column in enumerate(columns)
            }
            for raw_row in result.fetchall()
        ]
        return columns, rows
    except (duckdb.Error, pd.errors.ParserError, ValueError) as exc:
        raise ValueError(f"Could not parse the uploaded file: {exc}") from exc
    finally:
        con.close()


def resolve_csv_scan_options(con: duckdb.DuckDBPyConnection, csv_path: Path) -> str:
    csv_sql = sql_string(str(csv_path.resolve()))
    errors: List[str] = []

    for encoding in duckdb_csv_encoding_candidates(csv_path):
        scan_options_sql = csv_scan_options(encoding)
        try:
            con.execute(f"""
                SELECT *
                FROM read_csv_auto({csv_sql}, {scan_options_sql})
                LIMIT 1;
            """)
            return scan_options_sql
        except duckdb.Error as exc:
            errors.append(f"{encoding}: {exc}")

    raise ValueError(
        "Could not read the CSV with the supported encodings. " + " | ".join(errors)
    )


def csv_columns(
    con: duckdb.DuckDBPyConnection,
    csv_path: Path,
    scan_options_sql: Optional[str] = None,
) -> List[str]:
    csv_sql = sql_string(str(csv_path.resolve()))
    resolved_scan_options_sql = scan_options_sql or resolve_csv_scan_options(con, csv_path)
    rows = con.execute(f"""
        DESCRIBE SELECT *
        FROM read_csv_auto({csv_sql}, {resolved_scan_options_sql});
    """).fetchall()
    return [row[0] for row in rows]


def exposure_map_cache_key(
    upload_path: Path,
    upload_id: str,
    lat_col: str,
    lon_col: str,
) -> str:
    stat = upload_path.stat()
    payload = {
        "upload_id": upload_id,
        "path": str(upload_path.resolve()),
        "size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
        "lat_col": lat_col,
        "lon_col": lon_col,
    }
    return hashlib.sha1(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()[:20]


def exposure_map_cache_path(
    upload_path: Path,
    upload_id: str,
    lat_col: str,
    lon_col: str,
) -> Path:
    digest = exposure_map_cache_key(upload_path, upload_id, lat_col, lon_col)
    return local_runtime_dir("exposure_map_cache") / f"{upload_id}_{digest}.duckdb"


def unlink_duckdb_file(path: Path) -> None:
    path.unlink(missing_ok=True)
    path.with_name(f"{path.name}.wal").unlink(missing_ok=True)


def prune_exposure_map_caches(keep_path: Optional[Path] = None) -> None:
    cache_dir = local_runtime_dir("exposure_map_cache")
    keep_name = keep_path.name if keep_path else ""
    now = time.time()
    caches = sorted(
        (path for path in cache_dir.glob("*.duckdb") if path.is_file()),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )

    retained = 0
    for path in caches:
        try:
            age_seconds = now - path.stat().st_mtime
        except FileNotFoundError:
            continue

        if path.name == keep_name:
            retained += 1
            continue

        if retained >= EXPOSURE_MAP_CACHE_MAX_FILES or age_seconds > EXPOSURE_ARTIFACT_MAX_AGE_SECONDS:
            unlink_duckdb_file(path)
        else:
            retained += 1

    for temp_path in cache_dir.glob("*.tmp.duckdb"):
        try:
            if now - temp_path.stat().st_mtime > 30 * 60:
                unlink_duckdb_file(temp_path)
        except FileNotFoundError:
            continue


def read_exposure_map_cache_metadata(cache_path: Path) -> Dict[str, Any]:
    con = duckdb.connect(str(cache_path), read_only=True)
    try:
        rows = con.execute("""
            SELECT key, value
            FROM metadata;
        """).fetchall()
    finally:
        con.close()

    metadata = {str(key): value for key, value in rows}
    for key in ("total_rows", "valid_rows"):
        metadata[key] = int(float(metadata.get(key) or 0))

    extent = None
    if metadata["valid_rows"]:
        extent = {
            "min_lon": float(metadata["min_lon"]),
            "min_lat": float(metadata["min_lat"]),
            "max_lon": float(metadata["max_lon"]),
            "max_lat": float(metadata["max_lat"]),
        }

    return {
        "upload_id": str(metadata.get("upload_id") or ""),
        "filename": str(metadata.get("filename") or ""),
        "lat_col": str(metadata.get("lat_col") or ""),
        "lon_col": str(metadata.get("lon_col") or ""),
        "total_rows": metadata["total_rows"],
        "valid_rows": metadata["valid_rows"],
        "extent": extent,
    }


def exposure_map_cache_has_row_details(cache_path: Path) -> bool:
    con = duckdb.connect(str(cache_path), read_only=True)
    try:
        return exposure_row_table_is_current(con)
    finally:
        con.close()


def prepare_exposure_map_cache(
    upload_path: Path,
    upload_id: str,
    lat_col: str,
    lon_col: str,
) -> Tuple[Path, Dict[str, Any]]:
    cache_path = exposure_map_cache_path(upload_path, upload_id, lat_col, lon_col)
    if cache_path.is_file():
        try:
            metadata = read_exposure_map_cache_metadata(cache_path)
            if not exposure_map_cache_has_row_details(cache_path):
                raise RuntimeError("Exposure row details are missing from the cache.")
            ensure_exposure_multires_cache(cache_path)
            prune_exposure_map_caches(cache_path)
            return cache_path, metadata
        except Exception:
            unlink_duckdb_file(cache_path)

    cache_dir = cache_path.parent
    tmp_cache_path = cache_dir / f"{cache_path.stem}.{uuid.uuid4().hex}.tmp.duckdb"
    is_xlsx = upload_path.suffix.lower() == ".xlsx"

    try:
        con = duckdb.connect(str(tmp_cache_path))
        try:
            con.execute(f"SET temp_directory = {sql_string(str(local_runtime_dir('duckdb_temp').resolve()))};")

            if is_xlsx:
                _load_excel_extension(con)
                source_from_sql = _xlsx_read_sql(upload_path)
                desc = con.execute(f"DESCRIBE SELECT * FROM {source_from_sql};").fetchall()
                columns = [str(row[0]) for row in desc]
                scan_options_sql = None
            else:
                scan_options_sql = resolve_csv_scan_options(con, upload_path)
                columns = csv_columns(con, upload_path, scan_options_sql)
                csv_sql = sql_string(str(upload_path.resolve()))
                source_from_sql = f"read_csv_auto({csv_sql}, {scan_options_sql})"

            if lat_col not in columns or lon_col not in columns:
                raise ValueError("Selected latitude/longitude columns were not found in the uploaded file.")

            lat_sql = f"source.{sql_identifier(lat_col)}"
            lon_sql = f"source.{sql_identifier(lon_col)}"

            con.execute(f"""
                CREATE TABLE source_points AS
                SELECT
                    row_number() OVER () AS row_id,
                    TRY_CAST(NULLIF(TRIM(CAST({lon_sql} AS VARCHAR)), '') AS DOUBLE) AS lon,
                    TRY_CAST(NULLIF(TRIM(CAST({lat_sql} AS VARCHAR)), '') AS DOUBLE) AS lat
                FROM {source_from_sql} AS source;
            """)

            total_rows, valid_rows, min_lon, min_lat, max_lon, max_lat = con.execute("""
                SELECT
                    COUNT(*) AS total_rows,
                    COALESCE(SUM(CASE
                        WHEN lon BETWEEN -180 AND 180 AND lat BETWEEN -90 AND 90 THEN 1
                        ELSE 0
                    END), 0) AS valid_rows,
                    MIN(lon) FILTER (WHERE lon BETWEEN -180 AND 180 AND lat BETWEEN -90 AND 90) AS min_lon,
                    MIN(lat) FILTER (WHERE lon BETWEEN -180 AND 180 AND lat BETWEEN -90 AND 90) AS min_lat,
                    MAX(lon) FILTER (WHERE lon BETWEEN -180 AND 180 AND lat BETWEEN -90 AND 90) AS max_lon,
                    MAX(lat) FILTER (WHERE lon BETWEEN -180 AND 180 AND lat BETWEEN -90 AND 90) AS max_lat
                FROM source_points;
            """).fetchone()

            con.execute("""
                CREATE TABLE points AS
                SELECT row_id, lon, lat
                FROM source_points
                WHERE lon BETWEEN -180 AND 180
                    AND lat BETWEEN -90 AND 90
                ORDER BY lon, lat;
            """)
            con.execute("DROP TABLE source_points;")
            con.execute("CREATE INDEX points_lon_idx ON points(lon);")
            con.execute("CREATE INDEX points_lat_idx ON points(lat);")
            build_exposure_row_table(con, upload_path, columns, scan_options_sql=scan_options_sql)
            build_exposure_multires_tables(con)
            con.execute("CREATE TABLE metadata(key VARCHAR PRIMARY KEY, value VARCHAR);")

            original_upload_name = upload_path.name.partition("_")[2] or upload_path.name
            metadata_rows = [
                ("upload_id", upload_id),
                ("filename", original_upload_name),
                ("lat_col", lat_col),
                ("lon_col", lon_col),
                ("total_rows", str(int(total_rows or 0))),
                ("valid_rows", str(int(valid_rows or 0))),
                ("min_lon", "" if min_lon is None else str(float(min_lon))),
                ("min_lat", "" if min_lat is None else str(float(min_lat))),
                ("max_lon", "" if max_lon is None else str(float(max_lon))),
                ("max_lat", "" if max_lat is None else str(float(max_lat))),
            ]
            con.executemany("INSERT INTO metadata VALUES (?, ?);", metadata_rows)
            con.execute("CHECKPOINT;")
        finally:
            con.close()

        os.replace(tmp_cache_path, cache_path)
        metadata = read_exposure_map_cache_metadata(cache_path)
        prune_exposure_map_caches(cache_path)
        return cache_path, metadata
    finally:
        unlink_duckdb_file(tmp_cache_path)


def exposure_view_grid(width: int, height: int, max_features: int) -> Tuple[int, int, int]:
    safe_width = max(320, min(3840, int(width or 1200)))
    safe_height = max(240, min(2160, int(height or 800)))
    safe_max = max(500, min(int(max_features or EXPOSURE_MAP_MAX_FEATURES), EXPOSURE_MAP_MAX_FEATURES))

    cols = max(24, min(260, math.ceil(safe_width / 8)))
    rows = max(18, min(200, math.ceil(safe_height / 8)))
    cell_count = cols * rows

    if cell_count > safe_max:
        scale = math.sqrt(safe_max / cell_count)
        cols = max(24, int(cols * scale))
        rows = max(18, int(rows * scale))

    return cols, rows, safe_max


def lookup_exposure_points_in_view(
    cache_path: Path,
    min_lon: float,
    min_lat: float,
    max_lon: float,
    max_lat: float,
    width: int,
    height: int,
    max_features: int = EXPOSURE_MAP_MAX_FEATURES,
    zoom: float = 0.0,
    con: Optional[duckdb.DuckDBPyConnection] = None,
) -> Dict[str, Any]:
    safe_max = max(500, min(int(max_features or EXPOSURE_MAP_MAX_FEATURES), EXPOSURE_MAP_MAX_FEATURES))
    return lookup_exposure_points_multires(
        cache_path=cache_path,
        min_lon=min_lon,
        min_lat=min_lat,
        max_lon=max_lon,
        max_lat=max_lat,
        width=width,
        height=height,
        max_features=safe_max,
        zoom=zoom,
        con=con,
    )


def csv_scan_options(encoding: str) -> str:
    encoding = sql_string(encoding)
    return f"sample_size = 20480, ignore_errors = true, header = true, all_varchar = true, encoding = {encoding}"


def b_select(alias: str = "b", columns: Optional[List[str]] = None) -> str:
    selected_columns = columns if columns is not None else BUILDING_COLUMNS
    return ",\n            ".join(f"{alias}.{sql_identifier(col)} AS {sql_identifier(col)}" for col in selected_columns)


def null_building_select() -> str:
    return ",\n            ".join(f"NULL AS {sql_identifier(col)}" for col in BUILDING_COLUMNS)


def final_building_select(source: str, columns: Optional[List[str]] = None) -> str:
    selected_columns = columns if columns is not None else BUILDING_COLUMNS
    return ",\n            ".join(
        f"{source}.{sql_identifier(col)} AS {sql_identifier('building_' + col)}"
        for col in selected_columns
    )


def final_coalesced_building_select(columns: Optional[List[str]] = None) -> str:
    selected_columns = columns if columns is not None else BUILDING_COLUMNS
    return ",\n            ".join(
        f"COALESCE(i.{sql_identifier(col)}, n.{sql_identifier(col)}) AS {sql_identifier('building_' + col)}"
        for col in selected_columns
    )


def appended_select(sql: str) -> str:
    return f",\n                {sql}" if sql else ""


def exposure_select(columns: List[str]) -> str:
    return ",\n            ".join(f"e.{sql_identifier(col)}" for col in columns)


def quadkey_prefix_sql(tile_x_sql: str, tile_y_sql: str, zoom: int) -> str:
    digits = []

    for level in range(zoom, 0, -1):
        mask = 1 << (level - 1)
        digits.append(
            "CAST(("
            f"(CASE WHEN (({tile_x_sql}) & {mask}) != 0 THEN 1 ELSE 0 END)"
            f" + (CASE WHEN (({tile_y_sql}) & {mask}) != 0 THEN 2 ELSE 0 END)"
            ") AS VARCHAR)"
        )

    return f"CONCAT({', '.join(digits)})"


def enrichment_quadkey_config(con: duckdb.DuckDBPyConnection) -> tuple[str, int, bool]:
    columns = {
        row[0]
        for row in con.execute("""
            SELECT column_name
            FROM information_schema.columns
            WHERE table_name = 'buildings';
        """).fetchall()
    }
    prefix_column = (
        "quadkey_prefix_14"
        if "quadkey_prefix_14" in columns
        else "quadkey_prefix_6"
    )
    zoom = OPTIMIZED_QUADKEY_PREFIX_ZOOM if prefix_column == "quadkey_prefix_14" else DEFAULT_QUADKEY_PREFIX_ZOOM
    has_null_prefixes = bool(con.execute(f"""
        SELECT EXISTS (
            SELECT 1
            FROM buildings
            WHERE {sql_identifier(prefix_column)} IS NULL
            LIMIT 1
        );
    """).fetchone()[0])
    return prefix_column, zoom, has_null_prefixes


def _quadkey_from_int(value: int, length: int) -> str:
    digits: List[str] = []
    for _ in range(length):
        digits.append(str(value & 3))
        value >>= 2
    return "".join(reversed(digits))


def merge_quadkey_prefix_ranges(quadkeys: Set[str]) -> List[Tuple[str, Optional[str]]]:
    """Collapse same-length quadkeys into sorted [start, end) prefix ranges.

    Quadkey digits are 0-3 and compare lexicographically, so every longer
    prefix that starts with quadkey q satisfies q <= prefix < successor(q),
    where successor(q) is q incremented as a base-4 number. An end of None
    means the range is unbounded above (q was the maximum quadkey).
    """
    length = len(next(iter(quadkeys)))
    values = sorted(int(quadkey, 4) for quadkey in quadkeys)

    runs: List[Tuple[int, int]] = []
    run_start = run_end = values[0]
    for value in values[1:]:
        if value == run_end + 1:
            run_end = value
            continue
        runs.append((run_start, run_end))
        run_start = run_end = value
    runs.append((run_start, run_end))

    max_value = (1 << (2 * length)) - 1
    return [
        (
            _quadkey_from_int(start, length),
            None if end >= max_value else _quadkey_from_int(end + 1, length),
        )
        for start, end in runs
    ]


def copy_remote_db_to_cache(
    source: Path,
    source_stat: os.stat_result,
    cache_dir: Path,
    report_progress,
) -> Path:
    cache_name = f"{source.stem}_{source_stat.st_size}_{int(source_stat.st_mtime)}.duckdb"
    target = cache_dir / cache_name

    if target.is_file() and target.stat().st_size == source_stat.st_size:
        report_progress("Reusing locally cached lookup database", 70)
        return target

    for stale_tmp in cache_dir.glob("*.tmp"):
        stale_tmp.unlink(missing_ok=True)

    cached = sorted(
        (path for path in cache_dir.glob("*.duckdb") if path.is_file()),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    for old in cached[max(DB_STAGE_CACHE_MAX_FILES - 1, 0):]:
        old.unlink(missing_ok=True)

    total_mb = max(source_stat.st_size // (1024 * 1024), 1)
    tmp_target = target.with_name(target.name + ".tmp")
    copied = 0
    report_progress("Copying lookup database to local cache", 8)
    with source.open("rb") as src_handle, tmp_target.open("wb") as dst_handle:
        while True:
            chunk = src_handle.read(DB_STAGE_COPY_CHUNK_BYTES)
            if not chunk:
                break
            dst_handle.write(chunk)
            copied += len(chunk)
            fraction = min(copied / max(source_stat.st_size, 1), 1.0)
            report_progress(
                f"Copying lookup database to local cache ({copied // (1024 * 1024)} / {total_mb} MB)",
                8 + int(62 * fraction),
            )

    os.replace(tmp_target, target)
    return target


def extract_remote_db_subset(
    source: Path,
    exposure_df: pd.DataFrame,
    lat_col: str,
    lon_col: str,
    report_progress,
) -> Path:
    report_progress("Reading CSV coordinates for staging", 8)
    coordinate_rows = [
        (row.get(lon_col), row.get(lat_col))
        for row in exposure_df[[lon_col, lat_col]].to_dict(orient="records")
    ]

    report_progress("Reading lookup index configuration", 10)
    remote_con = open_db(str(source), read_only=True)
    try:
        prefix_column, prefix_zoom, has_null_prefixes = enrichment_quadkey_config(remote_con)
    finally:
        remote_con.close()

    quadkeys = _compute_covering_quadkeys(
        [row[0] for row in coordinate_rows],
        [row[1] for row in coordinate_rows],
        prefix_zoom,
    )
    if not quadkeys:
        raise ValueError("The CSV does not contain any valid coordinates to stage against.")

    zoom = prefix_zoom
    while zoom > 1 and len(quadkeys) > DB_STAGE_MAX_QUADKEY_TILES:
        zoom -= 1
        quadkeys = {quadkey[:zoom] for quadkey in quadkeys}

    prefix_ranges = merge_quadkey_prefix_ranges(quadkeys)

    stage_dir = local_runtime_dir("duckdb_db_stage")
    now = time.time()
    for stale in stage_dir.glob("stage_*"):
        try:
            if now - stale.stat().st_mtime > DB_STAGE_TEMP_MAX_AGE_SECONDS:
                stale.unlink(missing_ok=True)
        except OSError:
            continue

    staged = stage_dir / f"stage_{uuid.uuid4().hex}.duckdb"
    con = duckdb.connect(str(staged))
    try:
        con.execute("LOAD spatial;")
        con.execute(f"SET temp_directory = {sql_string(str(local_runtime_dir('duckdb_temp').resolve()))};")
        con.execute(f"ATTACH {sql_string(str(source))} AS src_db (READ_ONLY);")
        con.execute("CREATE TABLE buildings AS SELECT * FROM src_db.buildings LIMIT 0;")

        prefix_sql = sql_identifier(prefix_column)
        range_batches = [
            prefix_ranges[batch_start:batch_start + DB_STAGE_RANGES_PER_INSERT]
            for batch_start in range(0, len(prefix_ranges), DB_STAGE_RANGES_PER_INSERT)
        ]
        total_steps = len(range_batches) + (1 if has_null_prefixes else 0)
        for index, batch in enumerate(range_batches):
            predicates = []
            for range_start, range_end in batch:
                predicate = f"b.{prefix_sql} >= {sql_string(range_start)}"
                if range_end is not None:
                    predicate += f" AND b.{prefix_sql} < {sql_string(range_end)}"
                predicates.append(f"({predicate})")
            con.execute(
                f"INSERT INTO buildings SELECT * FROM src_db.buildings b WHERE {' OR '.join(predicates)};"
            )
            report_progress(
                f"Extracting matching buildings to local disk ({index + 1}/{total_steps})",
                12 + int(58 * (index + 1) / max(total_steps, 1)),
            )

        if has_null_prefixes:
            con.execute(f"INSERT INTO buildings SELECT * FROM src_db.buildings b WHERE b.{prefix_sql} IS NULL;")
            report_progress(
                f"Extracting matching buildings to local disk ({total_steps}/{total_steps})",
                70,
            )

        try:
            con.execute("CREATE TABLE building_display_fields AS SELECT * FROM src_db.building_display_fields;")
        except duckdb.Error:
            pass

        con.execute("DETACH src_db;")
    except Exception:
        con.close()
        staged.unlink(missing_ok=True)
        Path(str(staged) + ".wal").unlink(missing_ok=True)
        raise

    con.close()
    return staged


def stage_remote_lookup_database(
    db_path: str,
    exposure_df: pd.DataFrame,
    lat_col: str,
    lon_col: str,
    report_progress,
) -> Tuple[str, str, Optional[Path]]:
    """Stage a high-latency network DuckDB onto fast local disk.

    Random spatial reads over SMB pay the network round trip per page, so the
    only fast option is to move the bytes once with sequential I/O and query
    locally. Small databases are copied whole and cached by name/size/mtime;
    larger ones get only the quadkey ranges covering the CSV extent extracted
    (the buildings table is sorted by quadkey prefix, so constant range
    predicates prune row groups via zonemaps).
    """
    source = Path(db_path).expanduser().resolve()
    source_stat = source.stat()
    cache_dir = local_runtime_dir("duckdb_db_cache")
    free_bytes = shutil.disk_usage(cache_dir).free

    if source_stat.st_size <= DB_STAGE_COPY_MAX_BYTES and source_stat.st_size * 1.2 < free_bytes:
        staged = copy_remote_db_to_cache(source, source_stat, cache_dir, report_progress)
        return str(staged), "file_copy", None

    staged = extract_remote_db_subset(
        source,
        exposure_df,
        lat_col,
        lon_col,
        report_progress,
    )
    return str(staged), "subset_extract", staged


def enrich_exposure_csv(
    db_path: str,
    csv_path: Path,
    output_path: Path,
    lat_col: str,
    lon_col: str,
    mode: str,
    max_distance_m: float,
    appended_fields: Optional[List[str]] = None,
    progress_callback=None,
) -> Dict[str, Any]:
    started_at = time.perf_counter()
    exposure_df = load_csv_dataframe(csv_path)

    last_tick = [time.perf_counter()]

    def log_step(label: str) -> None:
        now = time.perf_counter()
        step_seconds = now - last_tick[0]
        total_seconds = now - started_at
        last_tick[0] = now
        print(
            f"[TIMING] {label}: step={step_seconds:.3f}s total={total_seconds:.3f}s",
            flush=True,
        )

    def report_progress(phase: str, percent: int) -> None:
        if progress_callback:
            progress_callback(phase, percent)

    report_progress("Reading CSV columns", 6)
    columns = [str(column) for column in exposure_df.columns]
    log_step("Read CSV columns")

    if lat_col not in columns or lon_col not in columns:
        raise ValueError("Selected latitude/longitude columns were not found in the CSV.")

    use_remote = is_remote_storage_path(db_path)
    effective_db_path = db_path
    staging_mode = "none"
    staged_temp_db: Optional[Path] = None

    if use_remote:
        try:
            effective_db_path, staging_mode, staged_temp_db = stage_remote_lookup_database(
                db_path=db_path,
                exposure_df=exposure_df,
                lat_col=lat_col,
                lon_col=lon_col,
                report_progress=report_progress,
            )
            log_step(f"Staged remote lookup database locally ({staging_mode})")
        except Exception as exc:
            staging_mode = "row_by_row_fallback"
            print(
                f"[STAGING] Local staging failed ({exc}); falling back to row-by-row remote enrichment.",
                flush=True,
            )

    threads_configured = enrichment_thread_count(effective_db_path)
    report_progress("Opening lookup database", 72)
    con = open_db(effective_db_path, read_only=True)
    con.execute(f"SET threads = {threads_configured};")
    log_step("Opened database and configured threads")

    try:
        report_progress("Loading lookup fields", 74)
        available_fields = lookup_display_columns(con)
        log_step("Loaded lookup display columns")
        selected_fields = available_fields if appended_fields is None else appended_fields
        invalid_fields = [field for field in selected_fields if field not in available_fields]
        if invalid_fields:
            raise ValueError(f"Unknown appended database field: {invalid_fields[0]}")
        report_progress("Loading lookup index configuration", 76)
        quadkey_prefix_column, quadkey_prefix_zoom, allow_null_quadkey_prefix = enrichment_quadkey_config(con)
        log_step("Loaded quadkey config")

        output_sql = sql_string(str(output_path.resolve()))
        con.register("exposure_input_df", exposure_df)
        report_progress("Building enrichment SQL", 78)

        if staging_mode == "row_by_row_fallback":
            total_rows = len(exposure_df)
            log_step(f"Loaded CSV for row-by-row remote enrichment: {total_rows} rows")
            report_progress("Running spatial enrichment row-by-row", 78)

            enriched_rows: List[Dict[str, Any]] = []

            for row_idx in range(total_rows):
                if row_idx % 5 == 0 or row_idx == total_rows - 1:
                    progress = 78 + int(12 * (row_idx + 1) / max(total_rows, 1))
                    report_progress(f"Enriching row {row_idx + 1} of {total_rows}", progress)

                row_data = exposure_df.iloc[row_idx]
                lon_val = row_data.get(lon_col)
                lat_val = row_data.get(lat_col)

                lookup_result = lookup_exposure_row(
                    con=con,
                    lon=lon_val,
                    lat=lat_val,
                    mode=mode,
                    radius_m=max_distance_m,
                    quadkey_prefix_column=quadkey_prefix_column,
                    quadkey_prefix_zoom=quadkey_prefix_zoom,
                    allow_null_quadkey_prefix=allow_null_quadkey_prefix,
                )

                enriched_row: Dict[str, Any] = {}

                for col in columns:
                    enriched_row[col] = row_data.get(col)

                enriched_row["coordinate_valid"] = lookup_result["coordinate_valid"]
                enriched_row["building_match_type"] = lookup_result["building_match_type"]
                enriched_row["building_distance_m"] = lookup_result["building_distance_m"]
                enriched_row["building_confidence"] = lookup_result["building_confidence"]

                for field in selected_fields:
                    enriched_row[f"building_{field}"] = lookup_result.get(f"building_{field}")

                enriched_rows.append(enriched_row)

                if row_idx % 20 == 19:
                    log_step(f"Processed rows {row_idx - 18}-{row_idx + 1}")

            result_df = pd.DataFrame(enriched_rows)
            result_df.to_csv(output_path, index=False, encoding="utf-8-sig")
            log_step("Finished row-by-row remote enrichment and wrote CSV")

        else:
            select_sql = enrichment_select_sql(
                source_relation_sql="exposure_input_df",
                lat_sql=sql_identifier(lat_col),
                lon_sql=sql_identifier(lon_col),
                mode=mode,
                radius_sql=str(float(max_distance_m)),
                original_cols_sql=exposure_select(columns),
                appended_fields=selected_fields,
                quadkey_prefix_column=quadkey_prefix_column,
                quadkey_prefix_zoom=quadkey_prefix_zoom,
                allow_null_quadkey_prefix=allow_null_quadkey_prefix,
            )
            log_step("Built enrichment SQL")
            report_progress("Running spatial enrichment and writing CSV", 85)

            con.execute(f"""
                COPY (
                    {select_sql}
                ) TO {output_sql} (HEADER, DELIMITER ',');
            """)
            ensure_utf8_bom(output_path)
            log_step("Finished enrichment COPY")
        report_progress("Summarizing enriched output", 93)

        
        log_step("Starting summary query")
        summary = summarize_enriched_output(con, output_path, selected_fields)
        log_step("Finished summary query")
        report_progress("Finalizing enrichment summary", 97)

        summary["enrichment_elapsed_seconds"] = round(time.perf_counter() - started_at, 3)
        summary["engine_threads"] = int(threads_configured)
        summary["chunked_processing"] = staging_mode == "row_by_row_fallback"
        summary["remote_staging"] = staging_mode
        summary["lookup_quadkey_prefix_column"] = quadkey_prefix_column
        summary["lookup_quadkey_prefix_zoom"] = int(quadkey_prefix_zoom)
        summary["enrichment_mode"] = mode
        return summary
    finally:
        con.close()
        if staged_temp_db is not None:
            staged_temp_db.unlink(missing_ok=True)
            Path(str(staged_temp_db) + ".wal").unlink(missing_ok=True)


def summarize_enriched_output(
    con: duckdb.DuckDBPyConnection,
    output_path: Path,
    appended_fields: List[str],
) -> Dict[str, Any]:
    output_sql = sql_string(str(output_path.resolve()))
    occupancy_raw_sql = (
        "NULLIF(TRIM(CAST(building_occupancy_raw AS VARCHAR)), '')"
        if "occupancy_raw" in appended_fields
        else "NULL::VARCHAR"
    )
    occupancy_group_sql = (
        "NULLIF(TRIM(CAST(building_occupancy_group AS VARCHAR)), '')"
        if "occupancy_group" in appended_fields
        else "NULL::VARCHAR"
    )

    summary_rows = con.execute(f"""
        WITH enriched AS (
            SELECT
                TRY_CAST(coordinate_valid AS BOOLEAN) AS coordinate_valid,
                CAST(building_match_type AS VARCHAR) AS building_match_type,
                TRY_CAST(building_distance_m AS DOUBLE) AS building_distance_m,
                {occupancy_raw_sql} AS building_occupancy_raw,
                {occupancy_group_sql} AS building_occupancy_group
            FROM read_csv_auto({output_sql}, header = true, sample_size = 20480, all_varchar = true)
        ),
        rolled AS (
            SELECT
                building_occupancy_raw,
                building_occupancy_group,
            GROUPING(building_occupancy_raw) AS occupancy_raw_grouped,
            GROUPING(building_occupancy_group) AS occupancy_group_grouped,
                COUNT(*) AS row_count,
                COALESCE(SUM(CASE WHEN coordinate_valid THEN 1 ELSE 0 END), 0) AS valid_coordinate_rows,
                COALESCE(SUM(CASE WHEN building_match_type = 'inside_polygon' THEN 1 ELSE 0 END), 0) AS inside_polygon_matches,
                COALESCE(SUM(CASE WHEN building_match_type IN ('nearest_polygon', 'nearest_centroid') THEN 1 ELSE 0 END), 0) AS nearest_matches,
                COALESCE(SUM(CASE WHEN building_match_type = 'none' THEN 1 ELSE 0 END), 0) AS no_matches,
                COALESCE(SUM(CASE WHEN building_match_type IN ('nearest_polygon', 'nearest_centroid') THEN building_distance_m ELSE NULL END), 0.0) AS nearest_distance_total_m,
                COUNT(building_distance_m) FILTER (
                    WHERE building_match_type IN ('nearest_polygon', 'nearest_centroid')
                ) AS nearest_distance_count
            FROM enriched
            GROUP BY GROUPING SETS ((), (building_occupancy_raw), (building_occupancy_group))
        )
        SELECT
            CASE
                WHEN occupancy_raw_grouped = 0 THEN 'occupancy_raw'
                WHEN occupancy_group_grouped = 0 THEN 'occupancy_group'
                ELSE 'overall'
            END AS section,
            COALESCE(building_occupancy_raw, building_occupancy_group) AS name,
            row_count,
            valid_coordinate_rows,
            inside_polygon_matches,
            nearest_matches,
            no_matches,
            nearest_distance_total_m,
            nearest_distance_count
        FROM rolled
          WHERE (occupancy_raw_grouped = 1 AND occupancy_group_grouped = 1)
              OR (occupancy_raw_grouped = 0 AND building_occupancy_raw IS NOT NULL)
              OR (occupancy_group_grouped = 0 AND building_occupancy_group IS NOT NULL)
        ORDER BY
            CASE section WHEN 'overall' THEN 0 WHEN 'occupancy_raw' THEN 1 ELSE 2 END,
            row_count DESC,
            name;
    """).fetchall()

    overall = next(row for row in summary_rows if row[0] == "overall")
    detailed_occupancy = [
        {"name": row[1], "count": int(row[2])}
        for row in summary_rows
        if row[0] == "occupancy_raw" and row[1] is not None
    ]
    occupancy_group = [
        {"name": row[1], "count": int(row[2])}
        for row in summary_rows
        if row[0] == "occupancy_group" and row[1] is not None
    ]

    return {
        "total_rows": int(overall[2]),
        "valid_coordinate_rows": int(overall[3]),
        "inside_polygon_matches": int(overall[4]),
        "nearest_matches": int(overall[5]),
        "no_matches": int(overall[6]),
        "average_nearest_distance_m": (float(overall[7]) / int(overall[8])) if int(overall[8]) else None,
        "detailed_occupancy": detailed_occupancy,
        "occupancy_raw": detailed_occupancy,
        "occupancy_group": occupancy_group,
    }


def update_summary(summary: Dict[str, Any], enriched: pd.DataFrame) -> None:
    summary["total_rows"] += len(enriched)
    summary["valid_coordinate_rows"] += int(enriched["coordinate_valid"].fillna(False).sum())
    summary["inside_polygon_matches"] += int((enriched["building_match_type"] == "inside_polygon").sum())
    summary["nearest_matches"] += int(
        enriched["building_match_type"].isin(["nearest_polygon", "nearest_centroid"]).sum()
    )
    summary["no_matches"] += int((enriched["building_match_type"] == "none").sum())

    nearest_distances = enriched.loc[
        enriched["building_match_type"].isin(["nearest_polygon", "nearest_centroid"]),
        "building_distance_m",
    ].dropna()
    summary["nearest_distance_total_m"] += float(nearest_distances.sum())
    summary["nearest_distance_count"] += int(nearest_distances.count())

    add_distribution(summary["detailed_occupancy"], enriched["building_occupancy_raw"])
    add_distribution(summary["occupancy_group"], enriched["building_occupancy_group"])


def add_distribution(target: Dict[str, int], series: pd.Series) -> None:
    values = series.dropna().replace("", pd.NA).dropna().astype(str)

    for value, count in values.value_counts().items():
        target[value] = target.get(value, 0) + int(count)


def distribution_to_rows(distribution: Dict[str, int]) -> List[Dict[str, Any]]:
    return [
        {"name": name, "count": count}
        for name, count in sorted(distribution.items(), key=lambda item: item[1], reverse=True)
    ]


def prefixed_building_columns() -> List[str]:
    return [f"building_{column}" for column in BUILDING_COLUMNS]


def empty_lookup_result() -> Dict[str, Any]:
    result = {
        "coordinate_valid": False,
        "building_match_type": "none",
        "building_distance_m": None,
        "building_confidence": "none",
    }
    result.update({column: None for column in prefixed_building_columns()})
    return result



def lookup_exposure_row(
    con: duckdb.DuckDBPyConnection,
    lon: Any,
    lat: Any,
    mode: str,
    radius_m: float,
    quadkey_prefix_column: str,
    quadkey_prefix_zoom: int,
    allow_null_quadkey_prefix: bool,
) -> Dict[str, Any]:
    try:
        lon_value = float(lon)
        lat_value = float(lat)
    except (TypeError, ValueError):
        return empty_lookup_result()

    if not (-180 <= lon_value <= 180 and -90 <= lat_value <= 90):
        return empty_lookup_result()

    qk_filter = _point_quadkey_filter(
        lon_value,
        lat_value,
        quadkey_prefix_column,
        quadkey_prefix_zoom,
        allow_null_quadkey_prefix,
    )

    if mode == "centroid":
        row = lookup_nearest_centroid(con, lon_value, lat_value, radius_m, qk_filter)
        return row_to_enrichment_result(row, "nearest_centroid" if row else "none", row[0] if row else None)

    inside = lookup_inside_polygon(con, lon_value, lat_value, qk_filter)
    if inside:
        return row_to_enrichment_result(inside, "inside_polygon", 0.0)

    if mode == "inside":
        result = empty_lookup_result()
        result["coordinate_valid"] = True
        return result

    nearest = lookup_nearest_polygon(con, lon_value, lat_value, radius_m, qk_filter)
    return row_to_enrichment_result(nearest, "nearest_polygon" if nearest else "none", nearest[0] if nearest else None)




def lookup_inside_polygon(
    con: duckdb.DuckDBPyConnection,
    lon: float,
    lat: float,
    qk_filter: str,
) -> Optional[tuple]:
    return con.execute(f"""
        WITH point AS (
            SELECT ST_Point(?, ?) AS pt
        )
        SELECT
            {b_select("b")}
        FROM buildings b, point
        WHERE
            {qk_filter}
            AND ? BETWEEN b.bbox_xmin AND b.bbox_xmax
            AND ? BETWEEN b.bbox_ymin AND b.bbox_ymax
            AND ST_Intersects(b.geom, point.pt)
        ORDER BY b.footprint_area_m2 ASC NULLS LAST
        LIMIT 1;
    """, [lon, lat, lon, lat]).fetchone()




def lookup_nearest_centroid(
    con: duckdb.DuckDBPyConnection,
    lon: float,
    lat: float,
    radius_m: float,
    qk_filter: str,
) -> Optional[tuple]:
    lat_delta = radius_m / 111_320.0
    lon_delta = radius_m / (111_320.0 * max(math.cos(math.radians(lat)), 0.2))

    return con.execute(f"""
        WITH point AS (
            SELECT ST_Point(?, ?) AS pt
        )
        SELECT
            ST_Distance_Sphere(ST_Point(b.centroid_lon, b.centroid_lat), point.pt) AS distance_m,
            {b_select("b")}
        FROM buildings b, point
        WHERE
            {qk_filter}
            AND b.centroid_lon BETWEEN ? AND ?
            AND b.centroid_lat BETWEEN ? AND ?
            AND ST_Distance_Sphere(ST_Point(b.centroid_lon, b.centroid_lat), point.pt) <= ?
        ORDER BY distance_m
        LIMIT 1;
    """, [
        lon,
        lat,
        lon - lon_delta,
        lon + lon_delta,
        lat - lat_delta,
        lat + lat_delta,
        radius_m,
    ]).fetchone()



def lookup_nearest_polygon(
    con: duckdb.DuckDBPyConnection,
    lon: float,
    lat: float,
    radius_m: float,
    qk_filter: str,
) -> Optional[tuple]:
    candidate_radius_m = max(radius_m * 4.0, radius_m + 150.0)
    candidate_lat_delta = candidate_radius_m / 111_320.0
    candidate_lon_delta = candidate_radius_m / (111_320.0 * max(math.cos(math.radians(lat)), 0.2))

    return con.execute(f"""
        WITH point AS (
            SELECT
                ST_Point(?, ?) AS pt,
                ST_Transform(ST_Point(?, ?), 'EPSG:4326', 'EPSG:3035', always_xy := true) AS pt_m
        ),
        candidates AS MATERIALIZED (
            SELECT
                ST_Distance_Sphere(ST_Point(b.centroid_lon, b.centroid_lat), point.pt) AS centroid_distance_m,
                b.*
            FROM buildings b, point
            WHERE
                {qk_filter}
                AND b.centroid_lon BETWEEN ? AND ?
                AND b.centroid_lat BETWEEN ? AND ?
                AND ST_Distance_Sphere(ST_Point(b.centroid_lon, b.centroid_lat), point.pt) <= ?
            ORDER BY centroid_distance_m
            LIMIT 200
        )
        SELECT
            ST_Distance(b.geom_3035, point.pt_m) AS distance_m,
            {b_select("b")}
        FROM candidates b, point
        WHERE
            ST_DWithin(b.geom_3035, point.pt_m, ?)
        ORDER BY distance_m
        LIMIT 1;
    """, [
        lon,
        lat,
        lon,
        lat,
        lon - candidate_lon_delta,
        lon + candidate_lon_delta,
        lat - candidate_lat_delta,
        lat + candidate_lat_delta,
        candidate_radius_m,
        radius_m,
    ]).fetchone()


def row_to_enrichment_result(
    row: Optional[tuple],
    match_type: str,
    distance_m: Optional[float],
) -> Dict[str, Any]:
    result = empty_lookup_result()
    result["coordinate_valid"] = True
    result["building_match_type"] = match_type
    result["building_distance_m"] = distance_m

    if row is None:
        return result

    building_values = row[1:] if match_type in {"nearest_polygon", "nearest_centroid"} else row
    building = dict(zip(BUILDING_COLUMNS, building_values))

    if match_type == "inside_polygon":
        confidence = "high"
    elif distance_m is not None and distance_m <= 15:
        confidence = "medium"
    else:
        confidence = "low"

    result["building_confidence"] = confidence
    result.update({
        f"building_{column}": json_safe(value)
        for column, value in building.items()
    })
    return result


def chunk_lookup_sql(
    columns: List[str],
    lat_col: str,
    lon_col: str,
    mode: str,
    radius: float,
) -> str:
    original_cols_sql = exposure_select(columns)
    lat_sql = sql_identifier(lat_col)
    lon_sql = sql_identifier(lon_col)
    radius_sql = str(float(radius))

    exposure_cte = f"""
        WITH exposure AS (
            SELECT
                *,
                TRY_CAST({lon_sql} AS DOUBLE) AS __lon,
                TRY_CAST({lat_sql} AS DOUBLE) AS __lat,
                TRY_CAST({lon_sql} AS DOUBLE) BETWEEN -180 AND 180
                    AND TRY_CAST({lat_sql} AS DOUBLE) BETWEEN -90 AND 90 AS __valid_coordinates,
                ST_Point(TRY_CAST({lon_sql} AS DOUBLE), TRY_CAST({lat_sql} AS DOUBLE)) AS __pt,
                ST_Transform(
                    ST_Point(TRY_CAST({lon_sql} AS DOUBLE), TRY_CAST({lat_sql} AS DOUBLE)),
                    'EPSG:4326',
                    'EPSG:3035',
                    always_xy := true
                ) AS __pt_m,
                {radius_sql} / 111320.0 AS __lat_delta,
                {radius_sql} / (
                    111320.0 * GREATEST(COS(RADIANS(TRY_CAST({lat_sql} AS DOUBLE))), 0.2)
                ) AS __lon_delta
            FROM exposure_chunk_df
        )
    """

    if mode == "centroid":
        return f"""
            {exposure_cte}
            SELECT
                {original_cols_sql},
                e.__valid_coordinates AS coordinate_valid,
                CASE WHEN m.building_id IS NOT NULL THEN 'nearest_centroid' ELSE 'none' END AS building_match_type,
                m.distance_m AS building_distance_m,
                CASE
                    WHEN m.building_id IS NULL THEN 'none'
                    WHEN m.distance_m <= 15 THEN 'medium'
                    ELSE 'low'
                END AS building_confidence,
                {final_building_select("m")}
            FROM exposure e
            LEFT JOIN LATERAL (
                SELECT
                    ST_Distance_Sphere(ST_Point(b.centroid_lon, b.centroid_lat), e.__pt) AS distance_m,
                    {b_select("b")}
                FROM buildings b
                WHERE
                    e.__valid_coordinates
                    AND b.centroid_lon BETWEEN e.__lon - e.__lon_delta AND e.__lon + e.__lon_delta
                    AND b.centroid_lat BETWEEN e.__lat - e.__lat_delta AND e.__lat + e.__lat_delta
                    AND ST_Distance_Sphere(ST_Point(b.centroid_lon, b.centroid_lat), e.__pt) <= {radius_sql}
                ORDER BY distance_m
                LIMIT 1
            ) m ON TRUE
            ORDER BY e.__exposure_row_id
        """

    if mode == "inside":
        return f"""
            {exposure_cte}
            SELECT
                {original_cols_sql},
                e.__valid_coordinates AS coordinate_valid,
                CASE WHEN m.building_id IS NOT NULL THEN 'inside_polygon' ELSE 'none' END AS building_match_type,
                CASE WHEN m.building_id IS NOT NULL THEN 0.0 ELSE NULL END AS building_distance_m,
                CASE WHEN m.building_id IS NOT NULL THEN 'high' ELSE 'none' END AS building_confidence,
                {final_building_select("m")}
            FROM exposure e
            LEFT JOIN LATERAL (
                SELECT
                    {b_select("b")}
                FROM buildings b
                WHERE
                    e.__valid_coordinates
                    AND e.__lon BETWEEN b.bbox_xmin AND b.bbox_xmax
                    AND e.__lat BETWEEN b.bbox_ymin AND b.bbox_ymax
                    AND ST_Intersects(b.geom, e.__pt)
                ORDER BY b.footprint_area_m2 ASC NULLS LAST
                LIMIT 1
            ) m ON TRUE
            ORDER BY e.__exposure_row_id
        """

    return f"""
        {exposure_cte}
        SELECT
            {original_cols_sql},
            e.__valid_coordinates AS coordinate_valid,
            CASE
                WHEN i.building_id IS NOT NULL THEN 'inside_polygon'
                WHEN n.building_id IS NOT NULL THEN 'nearest_polygon'
                ELSE 'none'
            END AS building_match_type,
            CASE
                WHEN i.building_id IS NOT NULL THEN 0.0
                ELSE n.distance_m
            END AS building_distance_m,
            CASE
                WHEN i.building_id IS NOT NULL THEN 'high'
                WHEN n.building_id IS NULL THEN 'none'
                WHEN n.distance_m <= 15 THEN 'medium'
                ELSE 'low'
            END AS building_confidence,
            {final_coalesced_building_select()}
        FROM exposure e
        LEFT JOIN LATERAL (
            SELECT
                {b_select("b")}
            FROM buildings b
            WHERE
                e.__valid_coordinates
                AND e.__lon BETWEEN b.bbox_xmin AND b.bbox_xmax
                AND e.__lat BETWEEN b.bbox_ymin AND b.bbox_ymax
                AND ST_Intersects(b.geom, e.__pt)
            ORDER BY b.footprint_area_m2 ASC NULLS LAST
            LIMIT 1
        ) i ON TRUE
        LEFT JOIN LATERAL (
            SELECT
                ST_Distance(b.geom_3035, e.__pt_m) AS distance_m,
                {b_select("b")}
            FROM buildings b
            WHERE
                e.__valid_coordinates
                AND i.building_id IS NULL
                AND b.bbox_xmin <= e.__lon + e.__lon_delta
                AND b.bbox_xmax >= e.__lon - e.__lon_delta
                AND b.bbox_ymin <= e.__lat + e.__lat_delta
                AND b.bbox_ymax >= e.__lat - e.__lat_delta
                AND ST_DWithin(b.geom_3035, e.__pt_m, {radius_sql})
            ORDER BY distance_m
            LIMIT 1
        ) n ON TRUE
        ORDER BY e.__exposure_row_id
    """

def enrichment_select_sql(
    source_relation_sql: str,
    lat_sql: str,
    lon_sql: str,
    mode: str,
    radius_sql: str,
    original_cols_sql: str,
    appended_fields: List[str],
    quadkey_prefix_column: str = "quadkey_prefix_6",
    quadkey_prefix_zoom: int = DEFAULT_QUADKEY_PREFIX_ZOOM,
    allow_null_quadkey_prefix: bool = True,
    row_limit: Optional[int] = None,
    row_offset: int = 0,
) -> str:
    tile_count = 1 << quadkey_prefix_zoom
    tile_count_sql = str(tile_count)
    max_tile_sql = str(tile_count - 1)
    candidate_limit_sql = str(NEAREST_CANDIDATE_LIMIT)
    quadkey_expr_sql = quadkey_prefix_sql("tile_x", "tile_y", quadkey_prefix_zoom)
    working_building_columns = list(dict.fromkeys(["building_id", *appended_fields]))
    ranked_building_cols_sql = ",\n                    ".join(sql_identifier(col) for col in working_building_columns)
    nearest_building_cols_sql = ",\n                ".join(
        f"c.{sql_identifier(col)} AS {sql_identifier(col)}"
        for col in working_building_columns
    )
    final_select_sql = appended_select(final_building_select("m", appended_fields))
    final_coalesced_select_sql = appended_select(final_coalesced_building_select(appended_fields))
    prefix_identifier = sql_identifier(quadkey_prefix_column)
    quadkey_join_sql = f"b.{prefix_identifier} = t.__quadkey_prefix"
    if allow_null_quadkey_prefix:
        quadkey_join_sql = (
            f"({quadkey_join_sql} "
            f"OR (b.{prefix_identifier} IS NULL AND t.__is_primary_tile))"
        )

    if mode == "centroid":
        mode_base_cols = f""",
        {radius_sql} / 111320.0 AS __lat_delta,
        {radius_sql} / (
            111320.0 * __cos_lat
        ) AS __lon_delta
        """
        projected_ctes = """
        exposure AS MATERIALIZED (
            SELECT *
            FROM exposure_base
        )
        """
    elif mode == "inside_nearest":
        mode_base_cols = f""",
        {radius_sql} / 111320.0 AS __lat_delta,
        {radius_sql} / (
            111320.0 * __cos_lat
        ) AS __lon_delta
        """
        projected_ctes = """
        exposure_projected AS MATERIALIZED (
            SELECT
                *,
                CASE
                    WHEN __valid_coordinates
                    THEN ST_Transform(__pt, 'EPSG:4326', 'EPSG:3035', always_xy := true)
                    ELSE NULL
                END AS __pt_m
            FROM exposure_base
        ),
        exposure AS MATERIALIZED (
            SELECT
                *,
                CASE WHEN __pt_m IS NOT NULL THEN ST_X(__pt_m) ELSE NULL END AS __pt_m_x,
                CASE WHEN __pt_m IS NOT NULL THEN ST_Y(__pt_m) ELSE NULL END AS __pt_m_y
            FROM exposure_projected
        )
        """
    else:
        mode_base_cols = ""
        projected_ctes = """
        exposure AS MATERIALIZED (
            SELECT *
            FROM exposure_base
        )
        """

    limit_offset_clause = ""
    if row_limit is not None:
        limit_offset_clause = f"LIMIT {int(row_limit)} OFFSET {int(row_offset)}"

    exposure_ctes = f"""
        WITH exposure_raw AS MATERIALIZED (
            SELECT
                ROW_NUMBER() OVER () AS __exposure_row_id,
                *
            FROM {source_relation_sql}
            {limit_offset_clause}
        ),
        exposure_parsed AS MATERIALIZED (
            SELECT
                *,
                TRY_CAST({lon_sql} AS DOUBLE) AS __lon,
                TRY_CAST({lat_sql} AS DOUBLE) AS __lat
            FROM exposure_raw
        ),
        exposure_base AS MATERIALIZED (
            SELECT
                *,
                __lon BETWEEN -180 AND 180
                    AND __lat BETWEEN -90 AND 90 AS __valid_coordinates,
                LEAST(GREATEST(__lat, -85.05112878), 85.05112878) AS __lat_clamped,
                GREATEST(COS(RADIANS(__lat)), 0.2) AS __cos_lat,
                CASE
                    WHEN __lon BETWEEN -180 AND 180 AND __lat BETWEEN -90 AND 90 THEN ST_Point(__lon, __lat)
                    ELSE NULL
                END AS __pt,
                CASE
                    WHEN __lon BETWEEN -180 AND 180 AND __lat BETWEEN -90 AND 90
                    THEN LEAST(GREATEST(CAST(FLOOR((__lon + 180.0) / 360.0 * {tile_count_sql}) AS BIGINT), 0), {max_tile_sql})
                    ELSE NULL
                END AS __tile_x,
                CASE
                    WHEN __lon BETWEEN -180 AND 180 AND __lat BETWEEN -90 AND 90
                    THEN LEAST(
                        GREATEST(
                            CAST(FLOOR((0.5 - LN((1 + SIN(RADIANS(LEAST(GREATEST(__lat, -85.05112878), 85.05112878)))) / (1 - SIN(RADIANS(LEAST(GREATEST(__lat, -85.05112878), 85.05112878))))) / (4 * PI())) * {tile_count_sql}) AS BIGINT),
                            0
                        ),
                        {max_tile_sql}
                    )
                    ELSE NULL
                END AS __tile_y
                {mode_base_cols}
            FROM exposure_parsed
        ),
        {projected_ctes},
        exposure_tiles AS MATERIALIZED (
            SELECT
                t.__exposure_row_id,
                {quadkey_expr_sql} AS __quadkey_prefix,
                t.dx = 0 AND t.dy = 0 AS __is_primary_tile
            FROM (
                SELECT
                    e.__exposure_row_id,
                    e.__tile_x + dx AS tile_x,
                    e.__tile_y + dy AS tile_y,
                    dx,
                    dy
                FROM exposure e
                CROSS JOIN range(-1, 2) AS dx(dx)
                CROSS JOIN range(-1, 2) AS dy(dy)
                WHERE e.__valid_coordinates
            ) t
            WHERE t.tile_x BETWEEN 0 AND {max_tile_sql}
              AND t.tile_y BETWEEN 0 AND {max_tile_sql}
        )
    """

    if mode == "centroid":
        return f"""
            {exposure_ctes},
            centroid_candidates AS MATERIALIZED (
                SELECT
                    e.__exposure_row_id,
                    ST_Distance_Sphere(ST_Point(b.centroid_lon, b.centroid_lat), e.__pt) AS distance_m,
                    ROW_NUMBER() OVER (
                        PARTITION BY e.__exposure_row_id
                        ORDER BY ST_Distance_Sphere(ST_Point(b.centroid_lon, b.centroid_lat), e.__pt)
                    ) AS candidate_rank,
                    {b_select("b", working_building_columns)}
                FROM exposure e
                JOIN exposure_tiles t USING (__exposure_row_id)
                JOIN buildings b
                    ON {quadkey_join_sql}
                    AND b.centroid_lon BETWEEN e.__lon - e.__lon_delta AND e.__lon + e.__lon_delta
                    AND b.centroid_lat BETWEEN e.__lat - e.__lat_delta AND e.__lat + e.__lat_delta
            ),
            centroid_ranked AS MATERIALIZED (
                SELECT
                    __exposure_row_id,
                    distance_m,
                    ROW_NUMBER() OVER (
                        PARTITION BY __exposure_row_id
                        ORDER BY distance_m
                    ) AS rn,
                    {ranked_building_cols_sql}
                FROM centroid_candidates
                WHERE candidate_rank <= {candidate_limit_sql}
                  AND distance_m <= {radius_sql}
            ),
            matches AS MATERIALIZED (
                SELECT * FROM centroid_ranked WHERE rn = 1
            )
            SELECT
                {original_cols_sql},
                e.__valid_coordinates AS coordinate_valid,
                CASE WHEN m.__exposure_row_id IS NOT NULL THEN 'nearest_centroid' ELSE 'none' END AS building_match_type,
                m.distance_m AS building_distance_m,
                CASE
                    WHEN m.__exposure_row_id IS NULL THEN 'none'
                    WHEN m.distance_m <= 15 THEN 'medium'
                    ELSE 'low'
                END AS building_confidence
                {final_select_sql}
            FROM exposure e
            LEFT JOIN matches m USING (__exposure_row_id)
            ORDER BY e.__exposure_row_id
        """

    if mode == "inside":
        return f"""
            {exposure_ctes},
            inside_ranked AS MATERIALIZED (
                SELECT
                    e.__exposure_row_id,
                    ROW_NUMBER() OVER (
                        PARTITION BY e.__exposure_row_id
                        ORDER BY b.footprint_area_m2 ASC NULLS LAST
                    ) AS rn,
                    {b_select("b", working_building_columns)}
                FROM exposure e
                JOIN exposure_tiles t USING (__exposure_row_id)
                JOIN buildings b
                    ON {quadkey_join_sql}
                    AND e.__lon BETWEEN b.bbox_xmin AND b.bbox_xmax
                    AND e.__lat BETWEEN b.bbox_ymin AND b.bbox_ymax
                    AND ST_Intersects(b.geom, e.__pt)
            ),
            matches AS MATERIALIZED (
                SELECT * FROM inside_ranked WHERE rn = 1
            )
            SELECT
                {original_cols_sql},
                e.__valid_coordinates AS coordinate_valid,
                CASE WHEN m.__exposure_row_id IS NOT NULL THEN 'inside_polygon' ELSE 'none' END AS building_match_type,
                CASE WHEN m.__exposure_row_id IS NOT NULL THEN 0.0 ELSE NULL END AS building_distance_m,
                CASE WHEN m.__exposure_row_id IS NOT NULL THEN 'high' ELSE 'none' END AS building_confidence
                {final_select_sql}
            FROM exposure e
            LEFT JOIN matches m USING (__exposure_row_id)
            ORDER BY e.__exposure_row_id
        """

    # inside_nearest mode
    return f"""
        {exposure_ctes},
        inside_ranked AS MATERIALIZED (
            SELECT
                e.__exposure_row_id,
                ROW_NUMBER() OVER (
                    PARTITION BY e.__exposure_row_id
                    ORDER BY b.footprint_area_m2 ASC NULLS LAST
                ) AS rn,
                {b_select("b", working_building_columns)}
            FROM exposure e
            JOIN exposure_tiles t USING (__exposure_row_id)
            JOIN buildings b
                ON {quadkey_join_sql}
                AND e.__lon BETWEEN b.bbox_xmin AND b.bbox_xmax
                AND e.__lat BETWEEN b.bbox_ymin AND b.bbox_ymax
                AND ST_Intersects(b.geom, e.__pt)
        ),
        inside_matches AS MATERIALIZED (
            SELECT * FROM inside_ranked WHERE rn = 1
        ),
        unmatched_exposure AS MATERIALIZED (
            SELECT e.*
            FROM exposure e
            LEFT JOIN inside_matches i USING (__exposure_row_id)
            WHERE e.__valid_coordinates
              AND i.__exposure_row_id IS NULL
        ),
        nearest_candidates AS MATERIALIZED (
            SELECT
                e.__exposure_row_id,
                b.geom_3035 AS __geom_3035,
                ST_Distance_Sphere(ST_Point(b.centroid_lon, b.centroid_lat), e.__pt) AS centroid_distance_m,
                ROW_NUMBER() OVER (
                    PARTITION BY e.__exposure_row_id
                    ORDER BY ST_Distance_Sphere(ST_Point(b.centroid_lon, b.centroid_lat), e.__pt)
                ) AS candidate_rank,
                {b_select("b", working_building_columns)}
            FROM unmatched_exposure e
            JOIN exposure_tiles t USING (__exposure_row_id)
            JOIN buildings b
                ON {quadkey_join_sql}
                AND b.bbox_xmin <= e.__lon + e.__lon_delta
                AND b.bbox_xmax >= e.__lon - e.__lon_delta
                AND b.bbox_ymin <= e.__lat + e.__lat_delta
                AND b.bbox_ymax >= e.__lat - e.__lat_delta
                AND b.bbox_3035_xmin <= e.__pt_m_x + {radius_sql}
                AND b.bbox_3035_xmax >= e.__pt_m_x - {radius_sql}
                AND b.bbox_3035_ymin <= e.__pt_m_y + {radius_sql}
                AND b.bbox_3035_ymax >= e.__pt_m_y - {radius_sql}
        ),
        nearest_ranked AS MATERIALIZED (
            SELECT
                c.__exposure_row_id,
                ST_Distance(c.__geom_3035, e.__pt_m) AS distance_m,
                ROW_NUMBER() OVER (
                    PARTITION BY c.__exposure_row_id
                    ORDER BY ST_Distance(c.__geom_3035, e.__pt_m)
                ) AS rn,
                {nearest_building_cols_sql}
            FROM nearest_candidates c
            JOIN unmatched_exposure e USING (__exposure_row_id)
            WHERE c.candidate_rank <= {candidate_limit_sql}
              AND ST_DWithin(c.__geom_3035, e.__pt_m, {radius_sql})
        ),
        nearest_matches AS MATERIALIZED (
            SELECT * FROM nearest_ranked WHERE rn = 1
        )
        SELECT
            {original_cols_sql},
            e.__valid_coordinates AS coordinate_valid,
            CASE
                WHEN i.__exposure_row_id IS NOT NULL THEN 'inside_polygon'
                WHEN n.__exposure_row_id IS NOT NULL THEN 'nearest_polygon'
                ELSE 'none'
            END AS building_match_type,
            CASE
                WHEN i.__exposure_row_id IS NOT NULL THEN 0.0
                ELSE n.distance_m
            END AS building_distance_m,
            CASE
                WHEN i.__exposure_row_id IS NOT NULL THEN 'high'
                WHEN n.__exposure_row_id IS NULL THEN 'none'
                WHEN n.distance_m <= 15 THEN 'medium'
                ELSE 'low'
            END AS building_confidence
            {final_coalesced_select_sql}
        FROM exposure e
        LEFT JOIN inside_matches i USING (__exposure_row_id)
        LEFT JOIN nearest_matches n USING (__exposure_row_id)
        ORDER BY e.__exposure_row_id
    """

def find_building(
    con: duckdb.DuckDBPyConnection,
    lon: float,
    lat: float,
    nearest_radius_m: float,
) -> Optional[Dict[str, Any]]:
    quadkey_prefix_column, quadkey_prefix_zoom, allow_null_quadkey_prefix = enrichment_quadkey_config(con)
    qk_filter = _point_quadkey_filter(
        lon,
        lat,
        quadkey_prefix_column,
        quadkey_prefix_zoom,
        allow_null_quadkey_prefix,
    )
    display_columns = lookup_display_columns(con)
    display_select = ",\n            ".join(sql_identifier(column) for column in display_columns)
    inside = con.execute(f"""
        WITH click AS (
            SELECT ST_Point(?, ?) AS pt
        )
        SELECT
            'inside_polygon' AS match_type,
            0.0 AS distance_m,
            'high' AS confidence,
            {display_select},
            ST_AsGeoJSON(geom) AS geometry
        FROM buildings b, click
        WHERE
            {qk_filter}
            AND b.bbox_xmin <= ?
            AND b.bbox_xmax >= ?
            AND b.bbox_ymin <= ?
            AND b.bbox_ymax >= ?
            AND ST_Intersects(b.geom, pt)
        ORDER BY b.footprint_area_m2 ASC NULLS LAST
        LIMIT 1;
    """, [lon, lat, lon, lon, lat, lat]).fetchone()

    if inside:
        return row_to_response(inside, display_columns)

    lat_delta = nearest_radius_m / 111_320.0
    lon_delta = nearest_radius_m / (111_320.0 * max(math.cos(math.radians(lat)), 0.2))

    nearest = con.execute(f"""
        WITH click AS (
            SELECT ST_Point(?, ?) AS pt
        )
        SELECT
            'nearest' AS match_type,
            ST_Distance_Sphere(ST_Point(b.centroid_lon, b.centroid_lat), pt) AS distance_m,
            CASE
                WHEN ST_Distance_Sphere(ST_Point(b.centroid_lon, b.centroid_lat), pt) <= 15 THEN 'medium'
                ELSE 'low'
            END AS confidence,
            {display_select},
            ST_AsGeoJSON(b.geom) AS geometry
        FROM buildings b, click
        WHERE
            {qk_filter}
            AND b.centroid_lon BETWEEN ? AND ?
            AND b.centroid_lat BETWEEN ? AND ?
        ORDER BY distance_m ASC
        LIMIT 1;
    """, [
        lon,
        lat,
        lon - lon_delta,
        lon + lon_delta,
        lat - lat_delta,
        lat + lat_delta,
    ]).fetchone()

    if nearest is None or nearest[1] is None or nearest[1] > nearest_radius_m:
        return None

    return row_to_response(nearest, display_columns)


def lookup_display_columns(con: duckdb.DuckDBPyConnection) -> List[str]:
    columns = con.execute("""
        SELECT column_name, data_type
        FROM information_schema.columns
        WHERE table_name = 'buildings'
        ORDER BY ordinal_position;
    """).fetchall()

    return [
        str(column_name)
        for column_name, data_type in columns
        if not is_internal_lookup_column(str(column_name), str(data_type))
    ]


def lookup_filter_values(
    con: duckdb.DuckDBPyConnection,
    column: str,
) -> List[str]:
    available_columns = set(lookup_display_columns(con))
    if column not in available_columns:
        raise ValueError(f"Unknown lookup filter column: {column}")

    column_sql = sql_identifier(column)
    rows = con.execute(f"""
        SELECT DISTINCT CAST({column_sql} AS VARCHAR) AS filter_value
        FROM buildings
        WHERE {column_sql} IS NOT NULL
            AND CAST({column_sql} AS VARCHAR) <> ''
        ORDER BY filter_value
        LIMIT ?
    """, [MAX_BUILDING_FILTER_VALUES]).fetchall()

    return [str(row[0]) for row in rows if row and row[0] is not None]


def filter_value_color_sql(value_sql: str) -> str:
    color_cases = " ".join(
        f"WHEN {index} THEN {sql_string(color)}"
        for index, color in enumerate(FILTER_VIEW_PALETTE)
    )
    return (
        f"CASE (hash({value_sql}) % {len(FILTER_VIEW_PALETTE)}) "
        f"{color_cases} ELSE '#64748b' END"
    )


def _viewport_quadkey_filter(
    min_lon: float,
    min_lat: float,
    max_lon: float,
    max_lat: float,
    prefix_column: str,
    prefix_zoom: int,
    allow_null_prefix: bool,
) -> str:
    min_x, min_y = _tile_xy(min_lon, max_lat, prefix_zoom)
    max_x, max_y = _tile_xy(max_lon, min_lat, prefix_zoom)
    max_tile = (1 << prefix_zoom) - 1
    min_x = max(0, min_x - 1)
    min_y = max(0, min_y - 1)
    max_x = min(max_tile, max_x + 1)
    max_y = min(max_tile, max_y + 1)

    tile_count = (max_x - min_x + 1) * (max_y - min_y + 1)
    if tile_count > MAX_FILTER_VIEW_SUMMARY_TILES:
        raise ValueError(
            "Viewport is too large for a building-level filter summary. "
            "Zoom in and try again."
        )

    quadkeys = {
        _tile_to_quadkey(tile_x, tile_y, prefix_zoom)
        for tile_x in range(min_x, max_x + 1)
        for tile_y in range(min_y, max_y + 1)
    }
    ranges = merge_quadkey_prefix_ranges(quadkeys)
    prefix_sql = f"b.{sql_identifier(prefix_column)}"
    predicates = []
    for range_start, range_end in ranges:
        predicate = f"{prefix_sql} >= {sql_string(range_start)}"
        if range_end is not None:
            predicate += f" AND {prefix_sql} < {sql_string(range_end)}"
        predicates.append(f"({predicate})")

    predicate = f"({' OR '.join(predicates)})"
    if allow_null_prefix:
        return f"({predicate} OR {prefix_sql} IS NULL)"
    return predicate


def lookup_building_filter_summary(
    con: duckdb.DuckDBPyConnection,
    min_lon: float,
    min_lat: float,
    max_lon: float,
    max_lat: float,
    column: str,
    value: str,
    available_columns: Optional[Set[str]] = None,
    quadkey_config: Optional[Tuple[str, int, bool]] = None,
) -> Dict[str, Any]:
    if available_columns is None:
        available_columns = set(lookup_display_columns(con))
    if column not in available_columns:
        raise ValueError(f"Unknown lookup filter column: {column}")

    column_sql = sql_identifier(column)
    prefix_column, prefix_zoom, has_null_prefixes = (
        quadkey_config or enrichment_quadkey_config(con)
    )
    qk_filter = _viewport_quadkey_filter(
        min_lon,
        min_lat,
        max_lon,
        max_lat,
        prefix_column,
        prefix_zoom,
        has_null_prefixes,
    )
    bounds_params: List[Any] = [min_lon, max_lon, min_lat, max_lat]

    if value != FILTER_VIEW_ALL_VALUE:
        row = con.execute(f"""
            SELECT
                COUNT(*) AS shown_count,
                COUNT(*) FILTER (
                    WHERE CAST({column_sql} AS VARCHAR) = ?
                ) AS colored_count
            FROM buildings AS b
            WHERE
                {qk_filter}
                AND b.bbox_xmax >= ?
                AND b.bbox_xmin <= ?
                AND b.bbox_ymax >= ?
                AND b.bbox_ymin <= ?;
        """, [value, *bounds_params]).fetchone()
        shown_count = int(row[0] or 0)
        colored_count = int(row[1] or 0)
        return {
            "count": colored_count,
            "shown_count": shown_count,
            "colored_count": colored_count,
            "mode": "single",
            "legend": [],
        }

    filter_value_sql = f"CAST({column_sql} AS VARCHAR)"
    rows = con.execute(f"""
        WITH visible AS (
            SELECT
                {filter_value_sql} AS filter_value
            FROM buildings AS b
            WHERE
                {qk_filter}
                AND b.bbox_xmax >= ?
                AND b.bbox_xmin <= ?
                AND b.bbox_ymax >= ?
                AND b.bbox_ymin <= ?
        ),
        grouped AS (
            SELECT filter_value, COUNT(*) AS value_count
            FROM visible
            WHERE filter_value IS NOT NULL AND filter_value <> ''
            GROUP BY filter_value
        ),
        totals AS (
            SELECT COUNT(*) AS shown_count FROM visible
        )
        SELECT
            grouped.filter_value,
            grouped.value_count,
            COALESCE(SUM(grouped.value_count) OVER (), 0) AS colored_count,
            totals.shown_count
        FROM totals
        LEFT JOIN grouped ON true
        ORDER BY value_count DESC, filter_value
        LIMIT ?;
    """, [*bounds_params, MAX_BUILDING_FILTER_VALUES]).fetchall()

    shown_count = int(rows[0][3] or 0) if rows else 0
    colored_count = int(rows[0][2] or 0) if rows else 0
    return {
        "count": colored_count,
        "shown_count": shown_count,
        "colored_count": colored_count,
        "mode": "all",
        "legend": [
            {
                "value": str(filter_value),
                "count": int(value_count or 0),
                "color": FILTER_VIEW_PALETTE[index % len(FILTER_VIEW_PALETTE)],
            }
            for index, (filter_value, value_count, _colored_count, _shown_count) in enumerate(rows)
            if filter_value is not None
        ],
    }


def filter_value_color(value: str) -> str:
    digest = hashlib.sha1(str(value).encode("utf-8")).hexdigest()
    return FILTER_VIEW_PALETTE[int(digest[:8], 16) % len(FILTER_VIEW_PALETTE)]


def lookup_buildings_mvt(
    con: duckdb.DuckDBPyConnection,
    z: int,
    x: int,
    y: int,
    column: str,
    value: str,
    color: Optional[str],
    available_columns: Optional[Set[str]] = None,
    quadkey_config: Optional[Tuple[str, int, bool]] = None,
) -> bytes:
    if available_columns is None:
        available_columns = set(lookup_display_columns(con))
    if column not in available_columns:
        raise ValueError(f"Unknown lookup filter column: {column}")

    min_lon, min_lat, max_lon, max_lat = tile_to_bounds(x, y, z)
    tolerance = 360.0 / (4096.0 * (1 << z))
    column_sql = sql_identifier(column)
    quadkey_prefix_column, quadkey_prefix_zoom, allow_null_quadkey_prefix = (
        quadkey_config or enrichment_quadkey_config(con)
    )
    qk_filter = _tile_quadkey_filter(
        x,
        y,
        z,
        quadkey_prefix_column,
        quadkey_prefix_zoom,
        allow_null_quadkey_prefix,
    )

    if value == FILTER_VIEW_ALL_VALUE:
        color_sql = filter_value_color_sql(f"CAST({column_sql} AS VARCHAR)")
        value_sql = f"{column_sql} IS NOT NULL AND CAST({column_sql} AS VARCHAR) <> ''"
        params: List[Any] = [
            min_lon, min_lat, max_lon, max_lat, tolerance,
            min_lon, max_lon, min_lat, max_lat,
            MAX_FILTER_VIEW_FEATURES_PER_TILE,
        ]
    else:
        color_sql = sql_string(color or filter_value_color(value))
        value_sql = f"CAST({column_sql} AS VARCHAR) = ?"
        params = [
            min_lon, min_lat, max_lon, max_lat, tolerance,
            min_lon, max_lon, min_lat, max_lat, value,
            MAX_FILTER_VIEW_FEATURES_PER_TILE,
        ]

    tile_blob = con.execute(f"""
        WITH envelope AS (
            SELECT {{
                'min_x': ?,
                'min_y': ?,
                'max_x': ?,
                'max_y': ?
            }}::BOX_2D AS bounds
        ),
        filtered AS (
            SELECT
                ST_AsMVTGeom(
                    ST_Simplify(b.geom, ?),
                    envelope.bounds,
                    4096,
                    8,
                    true
                ) AS geom,
                b.building_id,
                CAST({column_sql} AS VARCHAR) AS filter_value,
                {color_sql} AS __color
            FROM buildings AS b, envelope
            WHERE
                {qk_filter}
                AND b.bbox_xmax >= ?
                AND b.bbox_xmin <= ?
                AND b.bbox_ymax >= ?
                AND b.bbox_ymin <= ?
                AND {value_sql}
            ORDER BY b.building_id
            LIMIT ?
        )
        SELECT ST_AsMVT(tile_rows, 'buildings', 4096, 'geom')
        FROM (
            SELECT geom, building_id, filter_value, __color
            FROM filtered
            WHERE geom IS NOT NULL
        ) AS tile_rows;
    """, params).fetchone()[0]

    if tile_blob is None:
        return b""

    return bytes(tile_blob)


def default_enrichment_fields(
    available_fields: List[str],
    preferred_fields: Optional[List[Dict[str, str]]] = None,
) -> List[str]:
    preferred_names = [
        str(item.get("field"))
        for item in (preferred_fields or [])
        if item.get("field") in available_fields
    ]
    if preferred_names:
        return preferred_names

    fallback = [
        field for field in DEFAULT_EXPOSURE_FIELD_CANDIDATES
        if field in available_fields
    ]
    if fallback:
        return fallback

    return available_fields[: min(len(available_fields), 8)]


def preferred_display_fields(con: duckdb.DuckDBPyConnection) -> List[Dict[str, str]]:
    try:
        rows = con.execute("""
            SELECT field_name, display_label
            FROM building_display_fields
            ORDER BY display_order, field_name;
        """).fetchall()
    except duckdb.Error:
        return []

    return [
        {"field": str(field_name), "label": str(display_label)}
        for field_name, display_label in rows
    ]


def is_internal_lookup_column(column_name: str, data_type: str) -> bool:
    normalized_name = column_name.casefold()
    return (
        "geometry" in data_type.casefold()
        or normalized_name.startswith("geom")
        or normalized_name.startswith("bbox")
        or normalized_name.startswith("quadkey")
    )


def row_to_response(row: tuple, building_columns: List[str]) -> Dict[str, Any]:
    columns = [
        "match_type",
        "distance_m",
        "confidence",
        *building_columns,
        "geometry",
    ]
    data = dict(zip(columns, row))
    geometry = json.loads(data.pop("geometry"))

    building = {key: json_safe(value) for key, value in data.items() if key not in {
        "match_type",
        "distance_m",
        "confidence",
    }}
    building["geometry"] = geometry

    return {
        "match_type": data["match_type"],
        "distance_m": json_safe(data["distance_m"]),
        "confidence": data["confidence"],
        "building": building,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Building lookup app over OBM Parquet.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    prepare = subparsers.add_parser("prepare-index")
    prepare.add_argument("--parquet", default=DEFAULT_PARQUET)
    prepare.add_argument("--db", default=DEFAULT_DB)
    prepare.add_argument("--threads", type=int, default=8)
    prepare.add_argument("--force", action="store_true")

    serve = subparsers.add_parser("serve")
    serve.add_argument("--db", default=DEFAULT_DB)
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    serve.add_argument("--nearest-radius-m", type=float, default=50.0)
    serve.add_argument("--debug", action="store_true")

    return parser.parse_args()


def main() -> None:
    args = parse_args()

    if args.command == "prepare-index":
        prepare_index(args.parquet, args.db, args.force, args.threads)
        return

    app = create_app(args.db, args.nearest_radius_m)

    if not Path(args.db).exists():
        raise SystemExit(
            f"Lookup database not found: {args.db}\n"
            "Create it first with:\n"
            "  python building_lookup_app.py prepare-index "
            "--parquet etl_output/buildings_de_cleaned.parquet "
            "--db etl_output/building_lookup.duckdb --force"
        )

    app.run(host=args.host, port=args.port, debug=args.debug, use_reloader=False)


if __name__ == "__main__":
    main()
