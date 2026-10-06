"""Workflow 3: supplement a lookup DuckDB with columns from a GeoPackage or Parquet source.

The heavy lifting runs in a worker subprocess (``prepare`` then ``commit``) so the
Flask app stays responsive and an in-place update can take an exclusive lock.
"""

import argparse
import json
import math
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import uuid
from collections import Counter
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.parse import quote

import duckdb

from custom_parquet_database import (
    CUSTOM_BUILDING_RESERVED_COLUMNS,
    _normalize_mapped_drive_path,
    _quadkey_prefix_sql,
    _sql_identifier,
    _sql_string,
)


SOURCE_SUFFIXES = {".gpkg", ".parquet"}
OUTPUT_TYPES = {
    "auto": None,
    "double": "DOUBLE",
    "integer": "BIGINT",
    "text": "VARCHAR",
    "boolean": "BOOLEAN",
}
NUMERIC_TYPE_TOKENS = ("INT", "DOUBLE", "FLOAT", "REAL", "DECIMAL", "NUMERIC", "HUGEINT")
FILTER_OPS = {"in", "not_in", "prefix", "not_prefix", "between"}
POINT_AGGREGATES = {"closest", "mean", "max", "min", "sum"}
NAME_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,62}$")
CRS_PATTERN = re.compile(r"^[A-Za-z]+:[A-Za-z0-9]+$")
HIDDEN_NAME_PREFIXES = ("geom", "bbox", "quadkey")
GENERIC_PREFIX_WORDS = {"data", "tmp", "temp", "input", "output", "source", "files", "file", "new", "copy"}
WKB_COLUMN_NAMES = {"geom", "geometry", "geom_wkb", "wkb", "wkb_geometry", "shape", "the_geom"}
LON_GUESSES = ("lon", "lng", "longitude", "centroid_lon", "x")
LAT_GUESSES = ("lat", "latitude", "centroid_lat", "y")

MAX_SELECTED_COLUMNS = 64
MAX_FILTER_VALUES = 500
SAMPLE_ROWS_TOTAL = 2400
SAMPLE_LAYERS_MAX = 8
SAMPLE_BLOCK_POSITIONS = (0.05, 0.35, 0.65, 0.92)
GPKG_FETCH_ROWS = 50_000
GPKG_RANGE_ROWS = 150_000
GPKG_READERS = 4
PREVIEW_READERS = 8
PREVIEW_ID_CHUNK = 400
CELL_SIZE_M = 5_000.0
BATCH_TARGET_ROWS = 1_500_000
PREVIEW_MAX_AREA_KM2 = 60.0
PREVIEW_MAX_FEATURES = 6_000
COPY_CHUNK_BYTES = 16 * 1024 * 1024
DEFAULT_RADIUS_M = 5.0
DEFAULT_MIN_IOU = 0.5
SUPPLEMENT_LOG_TABLE = "supplement_log"
DISPLAY_FIELDS_TABLE = "building_display_fields"
WORKER_FLAG = "--supplement-worker"


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _sqlite_ident(name: str) -> str:
    return '"' + str(name).replace('"', '""') + '"'


def _resolve_path(value: Any, label: str) -> Path:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{label} is required.")
    path = Path(text).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    return _normalize_mapped_drive_path(path.resolve())


def _display(path: Path) -> str:
    try:
        return path.resolve().relative_to(Path.cwd().resolve()).as_posix()
    except ValueError:
        return str(path)


def _is_remote_path(path: Path) -> bool:
    text = str(path)
    if text.startswith("\\\\") or text.startswith("//"):
        return True
    if os.name != "nt" or not path.drive:
        return False
    try:
        import ctypes

        return ctypes.windll.kernel32.GetDriveTypeW(path.drive + "\\") == 4
    except Exception:
        return False


def _free_bytes(path: Path) -> Optional[int]:
    probe = path
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    try:
        return int(shutil.disk_usage(probe).free)
    except OSError:
        return None


def _file_signature(path: Path) -> List[int]:
    stat = path.stat()
    return [int(stat.st_mtime_ns), int(stat.st_size)]


def _is_numeric_type(type_name: str) -> bool:
    upper = str(type_name or "").upper()
    return any(token in upper for token in NUMERIC_TYPE_TOKENS)


def _default_prefix(path: Path) -> str:
    for candidate in (path.parent.name, path.stem):
        words = re.findall(r"[A-Za-z][A-Za-z0-9]*", candidate)
        if words and len(words[0]) >= 3 and words[0].lower() not in GENERIC_PREFIX_WORDS:
            return words[0].lower()[:12] + "_"
    return "supp_"


def _match_column_names(prefix: str, geometry_kind: str) -> List[Tuple[str, str]]:
    columns = [
        (f"{prefix}match_type", "VARCHAR"),
        (f"{prefix}match_confidence", "VARCHAR"),
        (f"{prefix}match_distance_m", "DOUBLE"),
    ]
    if geometry_kind == "polygon":
        columns += [(f"{prefix}match_iou", "DOUBLE"), (f"{prefix}match_shared", "INTEGER")]
    else:
        columns += [(f"{prefix}match_count", "INTEGER")]
    return columns


def _value_text(value: Any) -> str:
    if isinstance(value, float):
        return repr(value) if math.isfinite(value) else str(value)
    if isinstance(value, (bytes, bytearray, memoryview)):
        return "<binary>"
    return str(value)


def _looks_int(text: str) -> bool:
    return bool(re.fullmatch(r"[+-]?\d{1,18}", text.strip()))


def _looks_float(text: str) -> bool:
    # Python's float() accepts "31001_1000" (digit separators), so match the shape explicitly.
    return bool(re.fullmatch(r"[+-]?(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?", text.strip()))


def _profile_fields(fields: List[Dict[str, str]], rows: Sequence[Sequence[Any]]) -> List[Dict[str, Any]]:
    profiles: List[Dict[str, Any]] = []
    total = len(rows)
    for index, field in enumerate(fields):
        present = [
            row[index]
            for row in rows
            if row[index] is not None and not (isinstance(row[index], str) and not row[index].strip())
        ]
        texts = [_value_text(value) for value in present]
        counter = Counter(texts)
        numeric = _is_numeric_type(field["type"])
        suggested = "auto"
        if not numeric and texts and field["type"].upper() in {"VARCHAR", "TEXT"}:
            if all(_looks_int(text) for text in texts):
                suggested = "integer"
            elif all(_looks_float(text) for text in texts):
                suggested = "double"
        numbers = []
        if numeric or suggested != "auto":
            for value in present:
                try:
                    numbers.append(float(value))
                except (TypeError, ValueError):
                    continue
        profiles.append({
            "name": field["name"],
            "type": field["type"],
            "suggested_type": suggested,
            "numeric": numeric or suggested != "auto",
            "null_pct": round(100.0 * (total - len(present)) / total, 1) if total else None,
            "samples": [text for text, _count in counter.most_common(4)],
            "top_values": [{"value": text, "count": count} for text, count in counter.most_common(40)],
            "distinct_in_sample": len(counter),
            "min": min(numbers) if numbers else None,
            "max": max(numbers) if numbers else None,
        })
    return profiles


def _geometry_kind(type_name: str) -> str:
    upper = str(type_name or "").upper().replace(" ", "")
    if "POINT" in upper:
        return "point"
    if "POLYGON" in upper or "SURFACE" in upper:
        return "polygon"
    if "LINE" in upper or "CURVE" in upper:
        return "line"
    return "unknown"


# ---------------------------------------------------------------------------
# GeoPackage access (read with sqlite3: fast metadata, no GDAL scans over SMB)
# ---------------------------------------------------------------------------

def _gpkg_connect(path: Path) -> sqlite3.Connection:
    posix = path.as_posix()
    if not posix.startswith("/"):
        posix = "/" + posix
    uri = "file://" + quote(posix, safe="/:") + "?mode=ro&immutable=1"
    return sqlite3.connect(uri, uri=True, check_same_thread=False)


def _sqlite_decl_to_duck(declared: str) -> str:
    upper = str(declared or "").upper()
    if "INT" in upper or "BOOL" in upper:
        return "BIGINT"
    if any(token in upper for token in ("REAL", "FLOA", "DOUB", "NUMERIC", "DECIMAL")):
        return "DOUBLE"
    return "VARCHAR"


def _crs_string(organization: Any, code: Any, srs_id: Any) -> Optional[str]:
    org = str(organization or "").strip().upper()
    try:
        code_int = int(code)
    except (TypeError, ValueError):
        code_int = 0
    if org and org != "NONE" and code_int > 0:
        return f"{org}:{code_int}"
    try:
        srs = int(srs_id)
    except (TypeError, ValueError):
        srs = 0
    return f"EPSG:{srs}" if srs > 0 else None


def _gpkg_wkb_sql(column: str) -> str:
    col = _sqlite_ident(column)
    flags = (
        f"((instr('0123456789ABCDEF', substr(hex(substr({col}, 4, 1)), 1, 1)) - 1) * 16"
        f" + (instr('0123456789ABCDEF', substr(hex(substr({col}, 4, 1)), 2, 1)) - 1))"
    )
    # GeoPackage blobs carry an 8-byte header plus an optional envelope before the WKB.
    return (
        f"substr({col}, 9 + CASE (({flags} >> 1) & 7) "
        "WHEN 1 THEN 32 WHEN 2 THEN 48 WHEN 3 THEN 48 WHEN 4 THEN 64 ELSE 0 END)"
    )


def _sqlite_value_sql(name: str, duck_type: str) -> str:
    col = _sqlite_ident(name)
    if duck_type == "BIGINT":
        return f"CASE typeof({col}) WHEN 'integer' THEN {col} WHEN 'real' THEN CAST({col} AS INTEGER) ELSE NULL END"
    if duck_type == "DOUBLE":
        return f"CASE WHEN typeof({col}) IN ('integer', 'real') THEN CAST({col} AS REAL) ELSE NULL END"
    return f"CAST({col} AS TEXT)"


def gpkg_layers(path: Path) -> List[Dict[str, Any]]:
    con = _gpkg_connect(path)
    try:
        rows = con.execute("""
            SELECT c.table_name, g.column_name, g.geometry_type_name, c.srs_id,
                   s.organization, s.organization_coordsys_id
            FROM gpkg_contents c
            JOIN gpkg_geometry_columns g ON g.table_name = c.table_name
            LEFT JOIN gpkg_spatial_ref_sys s ON s.srs_id = c.srs_id
            WHERE lower(c.data_type) = 'features'
            ORDER BY c.table_name
        """).fetchall()
        rtree_tables = {
            str(row[0])
            for row in con.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name LIKE 'rtree_%'")
        }
        layers: List[Dict[str, Any]] = []
        for table, geom_col, geom_type, srs_id, organization, code in rows:
            info = con.execute(f"PRAGMA table_info({_sqlite_ident(table)})").fetchall()
            fields = []
            for _cid, name, declared, _notnull, _default, is_pk in info:
                if name == geom_col:
                    continue
                if is_pk and "INT" in str(declared or "").upper():
                    continue
                fields.append({"name": str(name), "type": _sqlite_decl_to_duck(declared)})
            # Separate ORDER BY/LIMIT probes: a combined min()/max() makes SQLite scan the whole table.
            table_sql = _sqlite_ident(table)
            low_row = con.execute(f"SELECT rowid FROM {table_sql} ORDER BY rowid ASC LIMIT 1").fetchone()
            high_row = con.execute(f"SELECT rowid FROM {table_sql} ORDER BY rowid DESC LIMIT 1").fetchone()
            low = low_row[0] if low_row else None
            high = high_row[0] if high_row else None
            rtree_name = f"rtree_{table}_{geom_col}"
            layers.append({
                "name": str(table),
                "geometry_column": str(geom_col),
                "geometry_type": str(geom_type or "GEOMETRY").upper(),
                "kind": _geometry_kind(geom_type),
                "crs": _crs_string(organization, code, srs_id),
                "fields": fields,
                "rowid_min": int(low) if low is not None else 0,
                "rowid_max": int(high) if high is not None else -1,
                "feature_estimate": int(high - low + 1) if low is not None and high is not None else 0,
                "rtree": rtree_name if rtree_name in rtree_tables else None,
            })
        return layers
    finally:
        con.close()


def _layer_signature(layer: Dict[str, Any]) -> Tuple[Any, ...]:
    return (tuple((field["name"], field["type"]) for field in layer["fields"]), layer["kind"], layer["crs"])


def _gpkg_sample(path: Path, layers: List[Dict[str, Any]]) -> List[Tuple[Any, ...]]:
    fields = layers[0]["fields"]
    if not fields:
        return []
    # A few contiguous rowid blocks per layer: one seek each, cheap even over SMB.
    picked = sorted(layers, key=lambda layer: -layer["feature_estimate"])[:SAMPLE_LAYERS_MAX]
    block = max(10, SAMPLE_ROWS_TOTAL // max(1, len(picked) * len(SAMPLE_BLOCK_POSITIONS)))
    select_sql = ", ".join(_sqlite_ident(field["name"]) for field in fields)
    rows: List[Tuple[Any, ...]] = []
    con = _gpkg_connect(path)
    try:
        for layer in picked:
            low, high = layer["rowid_min"], layer["rowid_max"]
            if high < low:
                continue
            for position in SAMPLE_BLOCK_POSITIONS:
                start = low + int((high - low) * position)
                rows.extend(con.execute(
                    f"SELECT {select_sql} FROM {_sqlite_ident(layer['name'])} WHERE rowid >= ? ORDER BY rowid LIMIT ?",
                    [start, block],
                ).fetchall())
    finally:
        con.close()
    return rows


def inspect_gpkg(path: Path) -> Dict[str, Any]:
    layers = gpkg_layers(path)
    if not layers:
        raise ValueError("The GeoPackage does not contain any feature layers.")

    groups: Dict[Tuple[Any, ...], List[Dict[str, Any]]] = {}
    for layer in layers:
        groups.setdefault(_layer_signature(layer), []).append(layer)

    group_payloads = []
    for index, (_signature, members) in enumerate(sorted(groups.items(), key=lambda item: -sum(m["feature_estimate"] for m in item[1]))):
        sample = _gpkg_sample(path, members)
        group_payloads.append({
            "id": f"g{index}",
            "layers": [member["name"] for member in members],
            "kind": members[0]["kind"],
            "crs": members[0]["crs"],
            "feature_estimate": sum(member["feature_estimate"] for member in members),
            "fields": _profile_fields(members[0]["fields"], sample),
            "sample_rows": len(sample),
        })

    return {
        "kind": "gpkg",
        "layers": [
            {
                "name": layer["name"],
                "geometry_type": layer["geometry_type"],
                "kind": layer["kind"],
                "crs": layer["crs"],
                "feature_estimate": layer["feature_estimate"],
                "has_spatial_index": bool(layer["rtree"]),
                "group": next(group["id"] for group in group_payloads if layer["name"] in group["layers"]),
            }
            for layer in layers
        ],
        "groups": group_payloads,
    }


# ---------------------------------------------------------------------------
# Parquet access
# ---------------------------------------------------------------------------

def _parquet_geo_metadata(con: duckdb.DuckDBPyConnection, path: Path) -> Dict[str, Any]:
    try:
        rows = con.execute("SELECT key, value FROM parquet_kv_metadata(?)", [str(path)]).fetchall()
    except duckdb.Error:
        return {}
    for key, value in rows:
        key_text = key.decode("utf-8", "ignore") if isinstance(key, (bytes, bytearray)) else str(key)
        if key_text != "geo":
            continue
        try:
            text = value.decode("utf-8", "ignore") if isinstance(value, (bytes, bytearray)) else str(value)
            return json.loads(text)
        except (ValueError, TypeError):
            return {}
    return {}


def _geoparquet_crs(geo: Dict[str, Any], column: Optional[str]) -> Optional[str]:
    if not geo or not column:
        return None
    meta = (geo.get("columns") or {}).get(column)
    if meta is None:
        return None
    if "crs" not in meta or meta.get("crs") is None:
        return "EPSG:4326"
    crs = meta.get("crs")
    if isinstance(crs, str):
        return crs if CRS_PATTERN.match(crs) else None
    if isinstance(crs, dict):
        ident = crs.get("id") or {}
        authority = str(ident.get("authority") or "").upper()
        code = str(ident.get("code") or "")
        if authority == "OGC" and code.upper() in {"CRS84", "CRS84H"}:
            return "EPSG:4326"
        if authority and code:
            return f"{authority}:{code}"
    return None


def parquet_columns(con: duckdb.DuckDBPyConnection, path: Path) -> List[Tuple[str, str]]:
    rows = con.execute("DESCRIBE SELECT * FROM read_parquet(?)", [str(path)]).fetchall()
    return [(str(row[0]), str(row[1])) for row in rows]


def _parquet_geometry_candidates(columns: List[Tuple[str, str]], geo: Dict[str, Any]) -> List[str]:
    geo_columns = set((geo.get("columns") or {}).keys()) if geo else set()
    candidates = []
    for name, type_name in columns:
        upper = type_name.upper()
        if "GEOMETRY" in upper or name in geo_columns:
            candidates.append(name)
        elif upper == "BLOB" and name.lower() in WKB_COLUMN_NAMES:
            candidates.append(name)
        elif upper == "VARCHAR" and name.lower() in {"wkt", "geometry_wkt", "geom_wkt"}:
            candidates.append(name)
    primary = geo.get("primary_column") if geo else None
    if primary in candidates:
        candidates.remove(primary)
        candidates.insert(0, primary)
    return candidates


def _parquet_geometry_sql(column: str, type_name: str) -> str:
    ident = _sql_identifier(column)
    upper = type_name.upper()
    if "GEOMETRY" in upper:
        return ident
    if upper in {"VARCHAR", "TEXT"}:
        return f"TRY(ST_GeomFromText({ident}))"
    return f"TRY(ST_GeomFromWKB({ident}))"


def _guess_column(names: Iterable[str], candidates: Sequence[str]) -> Optional[str]:
    normalized = {name: re.sub(r"[^a-z0-9]", "", name.lower()) for name in names}
    for candidate in candidates:
        for name, cleaned in normalized.items():
            if cleaned == candidate:
                return name
    return None


def inspect_parquet(path: Path) -> Dict[str, Any]:
    con = duckdb.connect()
    try:
        con.execute("LOAD spatial;")
        columns = parquet_columns(con, path)
        geo = _parquet_geo_metadata(con, path)
        geometry_candidates = _parquet_geometry_candidates(columns, geo)
        types = dict(columns)
        scalar_fields = [
            {"name": name, "type": type_name}
            for name, type_name in columns
            if name not in geometry_candidates
            and not any(token in type_name.upper() for token in ("STRUCT", "MAP", "[]", "LIST", "UNION", "BLOB"))
        ]
        try:
            feature_count = int(con.execute(
                "SELECT COALESCE(SUM(num_rows), 0) FROM parquet_file_metadata(?)",
                [str(path)],
            ).fetchone()[0])
        except duckdb.Error:
            feature_count = 0

        sample_rows: List[Tuple[Any, ...]] = []
        if scalar_fields:
            select_sql = ", ".join(_sql_identifier(field["name"]) for field in scalar_fields)
            sample_rows = con.execute(f"SELECT {select_sql} FROM read_parquet(?) LIMIT 2000", [str(path)]).fetchall()

        kind = "unknown"
        if geometry_candidates:
            geom_sql = _parquet_geometry_sql(geometry_candidates[0], types[geometry_candidates[0]])
            type_rows = con.execute(
                f"SELECT ST_GeometryType(g) AS t, COUNT(*) FROM (SELECT {geom_sql} AS g FROM read_parquet(?) LIMIT 500) WHERE g IS NOT NULL GROUP BY t ORDER BY 2 DESC",
                [str(path)],
            ).fetchall()
            if type_rows:
                kind = _geometry_kind(str(type_rows[0][0]))

        names = [name for name, _type in columns]
        lon_guess = _guess_column(names, LON_GUESSES)
        lat_guess = _guess_column(names, LAT_GUESSES)
        crs = _geoparquet_crs(geo, geometry_candidates[0]) if geometry_candidates else None
        if not geometry_candidates and lon_guess and lat_guess:
            kind = "point"
            crs = "EPSG:4326"
    finally:
        con.close()

    group = {
        "id": "g0",
        "layers": [path.stem],
        "kind": kind,
        "crs": crs,
        "feature_estimate": feature_count,
        "fields": _profile_fields(scalar_fields, sample_rows),
        "sample_rows": len(sample_rows),
    }
    return {
        "kind": "parquet",
        "layers": [{
            "name": path.stem,
            "geometry_type": kind.upper(),
            "kind": kind,
            "crs": crs,
            "feature_estimate": feature_count,
            "has_spatial_index": False,
            "group": "g0",
        }],
        "groups": [group],
        "geometry_columns": [{"name": name, "type": types[name]} for name in geometry_candidates],
        "numeric_columns": [name for name, type_name in columns if _is_numeric_type(type_name)],
        "lon_guess": lon_guess,
        "lat_guess": lat_guess,
    }


# ---------------------------------------------------------------------------
# Target lookup database
# ---------------------------------------------------------------------------

def _table_exists(con: duckdb.DuckDBPyConnection, table: str, catalog: Optional[str] = None) -> bool:
    catalog_sql = "?" if catalog else "current_database()"
    params = [catalog, table] if catalog else [table]
    return bool(con.execute(
        f"SELECT COUNT(*) FROM information_schema.tables WHERE table_catalog = {catalog_sql} AND table_name = ?",
        params,
    ).fetchone()[0])


def inspect_target(db_path: Path) -> Dict[str, Any]:
    if not db_path.is_file():
        raise ValueError(f"Target database does not exist: {db_path}")
    con = duckdb.connect(str(db_path), read_only=True)
    try:
        if not _table_exists(con, "buildings"):
            raise ValueError("The target DuckDB file does not contain a buildings table.")
        columns = [
            {"name": str(row[0]), "type": str(row[1])}
            for row in con.execute("""
                SELECT column_name, data_type FROM information_schema.columns
                WHERE table_name = 'buildings' ORDER BY ordinal_position
            """).fetchall()
        ]
        row_count = int(con.execute("SELECT COUNT(*) FROM buildings").fetchone()[0])
        history: List[Dict[str, Any]] = []
        if _table_exists(con, SUPPLEMENT_LOG_TABLE):
            for row in con.execute(f"""
                SELECT supplement_id, created_at, source_path, prefix, columns_json, stats_json
                FROM {SUPPLEMENT_LOG_TABLE} ORDER BY created_at DESC LIMIT 20
            """).fetchall():
                history.append({
                    "id": row[0],
                    "created_at": str(row[1]),
                    "source_path": row[2],
                    "prefix": row[3],
                    "columns": json.loads(row[4] or "[]"),
                    "stats": json.loads(row[5] or "{}"),
                })
    finally:
        con.close()

    names = {column["name"] for column in columns}
    return {
        "db_path": _display(db_path),
        "rows": row_count,
        "columns": columns,
        "size_bytes": db_path.stat().st_size,
        "has_geom_3035": "geom_3035" in names,
        "history": history,
    }


def inspect_source(path: Path) -> Dict[str, Any]:
    if path.suffix.lower() not in SOURCE_SUFFIXES:
        raise ValueError("Supplement source must be a .gpkg or .parquet file.")
    if not path.is_file():
        raise ValueError(f"Supplement source does not exist: {path}")
    inspected = inspect_gpkg(path) if path.suffix.lower() == ".gpkg" else inspect_parquet(path)
    inspected.update({
        "path": _display(path),
        "size_bytes": path.stat().st_size,
        "is_remote": _is_remote_path(path),
        "default_prefix": _default_prefix(path),
    })
    return inspected


# ---------------------------------------------------------------------------
# Request validation -> worker config
# ---------------------------------------------------------------------------

def _float_in_range(value: Any, default: float, low: float, high: float, label: str) -> float:
    if value is None or value == "":
        return default
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{label} must be a number.")
    if not (low <= number <= high):
        raise ValueError(f"{label} must be between {low:g} and {high:g}.")
    return number


def _validate_bbox(value: Any) -> List[float]:
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        raise ValueError("Preview needs the current map bounds.")
    west, south, east, north = (float(item) for item in value)
    if not (-180 <= west < east <= 180 and -90 <= south < north <= 90):
        raise ValueError("Preview map bounds are invalid.")
    mid_lat = math.radians((south + north) / 2.0)
    area_km2 = (east - west) * 111.32 * math.cos(mid_lat) * (north - south) * 110.57
    if area_km2 > PREVIEW_MAX_AREA_KM2:
        raise ValueError(
            f"Zoom in to preview: the current view covers about {area_km2:,.0f} km² "
            f"(preview limit {PREVIEW_MAX_AREA_KM2:,.0f} km²)."
        )
    return [west, south, east, north]


def build_job_config(
    payload: Dict[str, Any],
    *,
    preview: bool,
    job_id: str,
    active_db_path: str,
    display_seed_fields: List[str],
    preview_root: Path,
) -> Dict[str, Any]:
    target_path = _resolve_path(payload.get("target_db_path") or active_db_path, "Target database")
    if target_path.suffix.lower() != ".duckdb":
        raise ValueError("Target database must be a .duckdb file.")
    target_info = inspect_target(target_path)
    target_columns = {column["name"].casefold(): column["name"] for column in target_info["columns"]}
    if not target_info["has_geom_3035"] and "geom" not in {name.casefold() for name in target_columns}:
        raise ValueError("The target buildings table has no geometry column.")

    source_path = _resolve_path(payload.get("source_path"), "Supplement source")
    if source_path.suffix.lower() not in SOURCE_SUFFIXES:
        raise ValueError("Supplement source must be a .gpkg or .parquet file.")
    if not source_path.is_file():
        raise ValueError(f"Supplement source does not exist: {source_path}")

    source_kind = "gpkg" if source_path.suffix.lower() == ".gpkg" else "parquet"
    crs_override = str(payload.get("source_crs") or "").strip().upper()
    if crs_override and not CRS_PATTERN.match(crs_override):
        raise ValueError("Source CRS must look like EPSG:25832.")

    source_cfg: Dict[str, Any] = {"kind": source_kind, "path": str(source_path)}
    available_fields: Dict[str, str] = {}

    if source_kind == "gpkg":
        layers = {layer["name"]: layer for layer in gpkg_layers(source_path)}
        requested = payload.get("layers") or []
        if not isinstance(requested, list) or not requested:
            raise ValueError("Select at least one GeoPackage layer.")
        missing = [name for name in requested if name not in layers]
        if missing:
            raise ValueError(f"Unknown layer(s): {', '.join(map(str, missing[:5]))}")
        chosen = [layers[name] for name in requested]
        signature = _layer_signature(chosen[0])[:2]
        if any(_layer_signature(layer)[:2] != signature for layer in chosen[1:]):
            raise ValueError("Selected layers must share the same columns and geometry type.")
        geometry_kind = chosen[0]["kind"]
        for layer in chosen:
            layer_crs = crs_override or layer["crs"]
            if not layer_crs:
                raise ValueError(f"Layer {layer['name']} has no CRS. Enter the source CRS manually.")
            layer["crs"] = layer_crs
        source_cfg["layers"] = [
            {
                "name": layer["name"],
                "geometry_column": layer["geometry_column"],
                "crs": layer["crs"],
                "rowid_min": layer["rowid_min"],
                "rowid_max": layer["rowid_max"],
                "feature_estimate": layer["feature_estimate"],
                "rtree": layer["rtree"],
            }
            for layer in chosen
        ]
        available_fields = {field["name"]: field["type"] for field in chosen[0]["fields"]}
        source_cfg["feature_estimate"] = sum(layer["feature_estimate"] for layer in chosen)
    else:
        con = duckdb.connect()
        try:
            con.execute("LOAD spatial;")
            columns = parquet_columns(con, source_path)
            geo = _parquet_geo_metadata(con, source_path)
            try:
                feature_count = int(con.execute(
                    "SELECT COALESCE(SUM(num_rows), 0) FROM parquet_file_metadata(?)",
                    [str(source_path)],
                ).fetchone()[0])
            except duckdb.Error:
                feature_count = 0
        finally:
            con.close()
        types = dict(columns)
        geometry = payload.get("geometry") or {}
        geometry_column = str(geometry.get("column") or "").strip()
        lon_column = str(geometry.get("lon") or "").strip()
        lat_column = str(geometry.get("lat") or "").strip()
        if geometry_column:
            if geometry_column not in types:
                raise ValueError(f"Geometry column does not exist: {geometry_column}")
            source_cfg["geometry_sql"] = _parquet_geometry_sql(geometry_column, types[geometry_column])
            source_cfg["geometry_column"] = geometry_column
            crs = crs_override or _geoparquet_crs(geo, geometry_column) or "EPSG:4326"
            geometry_kind = str(payload.get("geometry_kind") or "").strip()
        elif lon_column and lat_column:
            for column in (lon_column, lat_column):
                if column not in types:
                    raise ValueError(f"Coordinate column does not exist: {column}")
            source_cfg["geometry_sql"] = (
                f"ST_Point(TRY_CAST({_sql_identifier(lon_column)} AS DOUBLE), "
                f"TRY_CAST({_sql_identifier(lat_column)} AS DOUBLE))"
            )
            source_cfg["lon"] = lon_column
            source_cfg["lat"] = lat_column
            crs = crs_override or "EPSG:4326"
            geometry_kind = "point"
        else:
            raise ValueError("Choose a geometry column or a longitude/latitude pair for the Parquet source.")
        if geometry_kind not in {"point", "polygon"}:
            raise ValueError("Parquet geometry must contain points or polygons.")
        source_cfg["layers"] = [{"name": source_path.stem, "crs": crs, "feature_estimate": feature_count}]
        source_cfg["feature_estimate"] = feature_count
        available_fields = {
            name: type_name for name, type_name in columns
            if name not in {geometry_column, ""}
        }

    if geometry_kind not in {"point", "polygon"}:
        raise ValueError("Only point and polygon sources are supported.")
    source_cfg["geometry_kind"] = geometry_kind

    prefix = str(payload.get("prefix") or "").strip()
    if prefix and not prefix.endswith("_"):
        prefix += "_"
    if not prefix or not NAME_PATTERN.match(prefix) or len(prefix) > 24:
        raise ValueError("Column prefix must start with a letter and use only letters, numbers and underscores.")

    raw_columns = payload.get("columns") or []
    if not isinstance(raw_columns, list) or not raw_columns:
        raise ValueError("Select at least one column to add.")
    if len(raw_columns) > MAX_SELECTED_COLUMNS:
        raise ValueError(f"Select at most {MAX_SELECTED_COLUMNS} columns.")

    replace_existing = bool(payload.get("replace_existing"))
    reserved = {name.casefold() for name in CUSTOM_BUILDING_RESERVED_COLUMNS}
    match_columns = _match_column_names(prefix, geometry_kind)
    seen: Dict[str, str] = {}
    columns_cfg: List[Dict[str, Any]] = []
    for item in raw_columns:
        if not isinstance(item, dict):
            raise ValueError("Each selected column needs a source and an output name.")
        source_name = str(item.get("source") or "")
        output = str(item.get("output") or "").strip()
        out_type = str(item.get("type") or "auto").strip().lower()
        if source_name not in available_fields:
            raise ValueError(f"Column does not exist in the source: {source_name}")
        if out_type not in OUTPUT_TYPES:
            raise ValueError(f"Unsupported output type for {output}: {out_type}")
        columns_cfg.append({
            "source": source_name,
            "output": output,
            "type": out_type,
            "native_type": available_fields[source_name],
        })

    all_outputs = [column["output"] for column in columns_cfg] + [name for name, _type in match_columns]
    conflicts: List[str] = []
    for output in all_outputs:
        folded = output.casefold()
        if not NAME_PATTERN.match(output):
            raise ValueError(f"Invalid output column name: {output!r}")
        if folded.startswith(HIDDEN_NAME_PREFIXES):
            raise ValueError(f"Output column {output} cannot start with geom, bbox or quadkey (the app hides those).")
        if folded in reserved:
            raise ValueError(f"Output column {output} would overwrite a core lookup field.")
        if folded in seen:
            raise ValueError(f"Output column {output} is used twice.")
        seen[folded] = output
        if folded in target_columns:
            conflicts.append(target_columns[folded])
    if conflicts and not replace_existing:
        raise ValueError(
            "These columns already exist in the target database: "
            + ", ".join(conflicts[:12])
            + ". Change the prefix or tick 'Replace existing columns'."
        )

    filter_cfg = None
    raw_filter = payload.get("filter") or {}
    if isinstance(raw_filter, dict) and raw_filter.get("column"):
        column = str(raw_filter.get("column"))
        op = str(raw_filter.get("op") or "in")
        values = raw_filter.get("values") or []
        if column not in available_fields:
            raise ValueError(f"Filter column does not exist: {column}")
        if op not in FILTER_OPS:
            raise ValueError(f"Unsupported filter operator: {op}")
        if not isinstance(values, list) or not values:
            raise ValueError("Add at least one filter value, or clear the filter column.")
        if len(values) > MAX_FILTER_VALUES:
            raise ValueError(f"Use at most {MAX_FILTER_VALUES} filter values.")
        if op == "between":
            if len(values) != 2:
                raise ValueError("A range filter needs a minimum and a maximum.")
            values = [float(values[0]), float(values[1])]
        else:
            values = [str(value) for value in values]
        filter_cfg = {"column": column, "op": op, "values": values, "native_type": available_fields[column]}

    match = payload.get("match") or {}
    match_cfg = {
        "radius_m": _float_in_range(match.get("radius_m"), DEFAULT_RADIUS_M, 0.0, 250.0, "Search radius"),
        "min_iou": _float_in_range(match.get("min_iou"), DEFAULT_MIN_IOU, 0.0, 1.0, "Minimum overlap"),
        "point_aggregate": str(match.get("point_aggregate") or "closest"),
    }
    if match_cfg["point_aggregate"] not in POINT_AGGREGATES:
        raise ValueError("Unsupported point aggregation.")

    output = payload.get("output") or {}
    output_mode = str(output.get("mode") or "new")
    if output_mode not in {"new", "inplace"}:
        raise ValueError("Output mode must be 'new' or 'inplace'.")
    output_path: Optional[Path] = None
    if output_mode == "new" and not preview:
        output_path = _resolve_path(output.get("path"), "New database path")
        if output_path.suffix.lower() != ".duckdb":
            raise ValueError("New database path must end with .duckdb.")
        if output_path == target_path:
            raise ValueError("The new database path must differ from the target database. Use 'Update in place' instead.")
        if active_db_path and output_path == _resolve_path(active_db_path, "Active database"):
            raise ValueError("The new database path is the database currently open in the app. Choose another name.")

    if preview:
        work_dir = preview_root
        bbox = _validate_bbox(payload.get("bbox"))
    else:
        work_dir = _resolve_path(payload.get("work_dir") or (target_path.parent / ".supplement_work"), "Working folder")
        bbox = None
    work_dir.mkdir(parents=True, exist_ok=True)

    copy_local = bool(payload.get("copy_source_local")) and not preview
    if not preview:
        source_size = source_path.stat().st_size
        target_size = target_path.stat().st_size
        destination_dir = (output_path or target_path).parent
        destination_dir.mkdir(parents=True, exist_ok=True)
        needs: Dict[str, int] = {}
        work_need = int(0.6 * source_size + 0.5 * target_size) + (source_size if copy_local else 0)
        for folder, need in ((work_dir, work_need), (destination_dir, int(1.05 * target_size))):
            anchor = (Path(folder).anchor or str(folder)).upper()
            needs[anchor] = needs.get(anchor, 0) + need
            free = _free_bytes(Path(folder))
            if free is not None and free < needs[anchor]:
                raise ValueError(
                    f"Not enough free space on {anchor}: about {needs[anchor] / 1e9:.1f} GB is needed "
                    f"for the working files and the new database, {free / 1e9:.1f} GB is free. "
                    "Choose a working folder or output path on a larger drive."
                )

    return {
        "job_id": job_id,
        "preview": preview,
        "target_db": str(target_path),
        "target_signature": _file_signature(target_path),
        "target_columns": [column["name"] for column in target_info["columns"]],
        "target_rows": target_info["rows"],
        "source": source_cfg,
        "columns": columns_cfg,
        "match_columns": match_columns,
        "replace_columns": sorted(set(conflicts)),
        "filter": filter_cfg,
        "match": match_cfg,
        "prefix": prefix,
        "add_new_buildings": bool(payload.get("add_new_buildings")) and geometry_kind == "polygon",
        "output": {
            "mode": output_mode,
            "path": str(output_path) if output_path else None,
            "activate": bool(output.get("activate", True)),
            "keep_backup": bool(output.get("keep_backup")),
        },
        "work_dir": str(work_dir),
        "work_db": str(work_dir / f"supplement_{job_id}.duckdb"),
        "copy_source_local": copy_local,
        "bbox": bbox,
        "display_seed_fields": list(display_seed_fields),
        "threads": max(1, (os.cpu_count() or 2) - 1),
    }


# ---------------------------------------------------------------------------
# Worker: shared plumbing
# ---------------------------------------------------------------------------

class ProgressWriter:
    def __init__(self, path: Optional[Path], start: float = 0.0, end: float = 100.0) -> None:
        self.path = path
        self.start = start
        self.end = end
        self._lock = threading.Lock()
        self._last_write = 0.0
        self._phase = ""

    def __call__(self, phase: str, fraction: float, detail: str = "", force: bool = False) -> None:
        if self.path is None:
            return
        now = time.time()
        with self._lock:
            if not force and phase == self._phase and now - self._last_write < 0.4:
                return
            self._phase = phase
            self._last_write = now
            percent = self.start + (self.end - self.start) * max(0.0, min(1.0, float(fraction)))
            payload = {"phase": phase, "percent": round(percent, 1), "detail": detail, "updated_at": now}
            tmp_path = self.path.with_suffix(".tmp")
            try:
                tmp_path.write_text(json.dumps(payload), encoding="utf-8")
                os.replace(tmp_path, self.path)
            except OSError:
                pass

    def span(self, phase: str, low: float, high: float) -> Callable[[float, str], None]:
        def report(fraction: float, detail: str = "") -> None:
            self(phase, low + (high - low) * max(0.0, min(1.0, fraction)), detail)
        return report


def _connect(db_path: Path, work_dir: Path, threads: int, *, read_only: bool = False) -> duckdb.DuckDBPyConnection:
    last_error: Optional[Exception] = None
    for _attempt in range(20):
        try:
            con = duckdb.connect(str(db_path), read_only=read_only)
            break
        except duckdb.IOException as exc:
            # The app may still be releasing its read-only handle.
            last_error = exc
            time.sleep(1.5)
    else:
        raise RuntimeError(
            f"Could not open {db_path}: it is locked by another process. Close other app windows "
            "or running enrichments and try again."
        ) from last_error
    con.execute("LOAD spatial;")
    temp_dir = work_dir / "duckdb_tmp"
    temp_dir.mkdir(parents=True, exist_ok=True)
    con.execute(f"SET temp_directory = {_sql_string(str(temp_dir))};")
    con.execute(f"SET threads = {int(threads)};")
    for setting in ("SET enable_progress_bar = true;", "SET enable_progress_bar_print = false;"):
        try:
            con.execute(setting)
        except duckdb.Error:
            pass
    return con


def _execute_tracked(
    con: duckdb.DuckDBPyConnection,
    sql: str,
    params: Optional[Sequence[Any]] = None,
    on_progress: Optional[Callable[[float, str], None]] = None,
    detail: str = "",
) -> None:
    if on_progress is None:
        con.execute(sql, list(params or []))
        return
    errors: List[BaseException] = []
    done = threading.Event()

    def run() -> None:
        try:
            con.execute(sql, list(params or []))
        except BaseException as exc:  # re-raised on the calling thread
            errors.append(exc)
        finally:
            done.set()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    while not done.wait(0.5):
        try:
            percent = float(con.query_progress())
        except Exception:
            percent = -1.0
        if percent >= 0:
            on_progress(min(percent, 100.0) / 100.0, detail)
    thread.join()
    if errors:
        raise errors[0]
    on_progress(1.0, detail)


def _cast_sql(expr: str, out_type: str) -> str:
    duck_type = OUTPUT_TYPES.get(out_type)
    if duck_type is None:
        return expr
    return f"TRY_CAST({expr} AS {duck_type})"


def _duck_filter_sql(filter_cfg: Optional[Dict[str, Any]]) -> Tuple[str, List[Any]]:
    if not filter_cfg:
        return "TRUE", []
    column = _sql_identifier(filter_cfg["column"])
    values = filter_cfg["values"]
    op = filter_cfg["op"]
    text = f"CAST({column} AS VARCHAR)"
    if op == "between":
        return f"(TRY_CAST({column} AS DOUBLE) BETWEEN ? AND ?)", list(values)
    if op in {"in", "not_in"}:
        clause = f"{text} IN ({', '.join('?' for _ in values)})"
    else:
        clause = "(" + " OR ".join(f"starts_with({text}, ?)" for _ in values) + ")"
    if op.startswith("not_"):
        return f"({column} IS NULL OR NOT {clause})", list(values)
    return clause, list(values)


def _sqlite_filter_sql(filter_cfg: Optional[Dict[str, Any]]) -> Tuple[str, List[Any]]:
    if not filter_cfg:
        return "1", []
    column = _sqlite_ident(filter_cfg["column"])
    values = filter_cfg["values"]
    op = filter_cfg["op"]
    text = f"CAST({column} AS TEXT)"
    if op == "between":
        return f"(typeof({column}) IN ('integer', 'real') AND {column} BETWEEN ? AND ?)", list(values)
    if op in {"in", "not_in"}:
        clause = f"{text} IN ({', '.join('?' for _ in values)})"
        params: List[Any] = list(values)
    else:
        clause = "(" + " OR ".join(f"substr({text}, 1, ?) = ?" for _ in values) + ")"
        params = []
        for value in values:
            params.extend([len(value), value])
    if op.startswith("not_"):
        return f"({column} IS NULL OR NOT {clause})", params
    return clause, params


def _bbox_in_crs(con: duckdb.DuckDBPyConnection, bbox: Sequence[float], crs: str) -> Tuple[float, float, float, float]:
    west, south, east, north = bbox
    if crs.upper() in {"EPSG:4326", "OGC:CRS84"}:
        return west, south, east, north
    points = []
    for step in range(6):
        fraction = step / 5.0
        x = west + (east - west) * fraction
        y = south + (north - south) * fraction
        points += [(x, south), (x, north), (west, y), (east, y)]
    values = ", ".join(f"({x!r}, {y!r})" for x, y in points)
    row = con.execute(f"""
        SELECT MIN(ST_X(p)), MIN(ST_Y(p)), MAX(ST_X(p)), MAX(ST_Y(p))
        FROM (
            SELECT ST_Transform(ST_Point(x, y), 'EPSG:4326', {_sql_string(crs)}, always_xy := true) AS p
            FROM (VALUES {values}) AS v(x, y)
        )
    """).fetchone()
    return float(row[0]), float(row[1]), float(row[2]), float(row[3])


def _copy_with_progress(source: Path, destination: Path, report: Callable[[float, str], None], label: str) -> None:
    total = max(1, source.stat().st_size)
    copied = 0
    tmp = destination.with_name(destination.name + ".part")
    with source.open("rb") as reader, tmp.open("wb") as writer:
        while True:
            chunk = reader.read(COPY_CHUNK_BYTES)
            if not chunk:
                break
            writer.write(chunk)
            copied += len(chunk)
            report(copied / total, f"{label}: {copied / 1e9:.1f} / {total / 1e9:.1f} GB")
    os.replace(tmp, destination)


# ---------------------------------------------------------------------------
# Worker: stage the source
# ---------------------------------------------------------------------------

def _gpkg_rtree_ids(path: Path, rtree: str, box: Tuple[float, float, float, float]) -> List[int]:
    """R-tree lookups are latency-bound over SMB, so query the 4 quadrants on separate connections."""
    from concurrent.futures import ThreadPoolExecutor

    x0, y0, x1, y1 = box
    xm, ym = (x0 + x1) / 2.0, (y0 + y1) / 2.0
    quadrants = [(x0, y0, xm, ym), (xm, y0, x1, ym), (x0, ym, xm, y1), (xm, ym, x1, y1)]

    def query(quadrant: Tuple[float, float, float, float]) -> List[int]:
        con = _gpkg_connect(path)
        try:
            return [row[0] for row in con.execute(
                f"SELECT id FROM {_sqlite_ident(rtree)} WHERE maxx >= ? AND minx <= ? AND maxy >= ? AND miny <= ?",
                [quadrant[0], quadrant[2], quadrant[1], quadrant[3]],
            ).fetchall()]
        finally:
            con.close()

    with ThreadPoolExecutor(max_workers=4) as pool:
        found = set()
        for ids in pool.map(query, quadrants):
            found.update(ids)
    return sorted(found)


def _stage_gpkg(con: duckdb.DuckDBPyConnection, cfg: Dict[str, Any], read_path: Path, report: Callable[[float, str], None]) -> None:
    import queue

    import pyarrow as pa

    source = cfg["source"]
    columns = cfg["columns"]
    raw_types = [column["native_type"] for column in columns]
    arrow_types = {"BIGINT": pa.int64(), "DOUBLE": pa.float64()}
    raw_defs = ", ".join(f"c{index} {raw_types[index]}" for index in range(len(columns)))
    con.execute(f"CREATE TABLE src_raw (src_id BIGINT, layer_idx SMALLINT, {raw_defs}, wkb BLOB)")

    filter_sql, filter_params = _sqlite_filter_sql(cfg["filter"])
    value_sql = ", ".join(
        _sqlite_value_sql(column["source"], raw_types[index]) for index, column in enumerate(columns)
    )
    tasks: List[Tuple[int, str, List[Any], int]] = []
    for layer_idx, layer in enumerate(source["layers"]):
        base = (
            f"SELECT rowid, {value_sql}, {_gpkg_wkb_sql(layer['geometry_column'])} "
            f"FROM {_sqlite_ident(layer['name'])} WHERE {filter_sql}"
        )
        if cfg["bbox"]:
            if layer.get("rtree"):
                box = _bbox_in_crs(con, cfg["bbox"], layer["crs"])
                ids = _gpkg_rtree_ids(read_path, layer["rtree"], box)
                # Small id chunks let several readers overlap the random seeks (latency-bound on SMB).
                for offset in range(0, len(ids), PREVIEW_ID_CHUNK):
                    chunk = ids[offset:offset + PREVIEW_ID_CHUNK]
                    tasks.append((
                        layer_idx,
                        base + f" AND rowid IN ({', '.join('?' for _ in chunk)})",
                        list(filter_params) + chunk,
                        len(chunk),
                    ))
            else:
                tasks.append((layer_idx, base, list(filter_params), layer["feature_estimate"]))
            continue
        low, high = layer["rowid_min"], layer["rowid_max"]
        start = low
        while start <= high:
            end = min(high, start + GPKG_RANGE_ROWS - 1)
            tasks.append((
                layer_idx,
                base + " AND rowid BETWEEN ? AND ?",
                list(filter_params) + [start, end],
                end - start + 1,
            ))
            start = end + 1

    total = max(1, sum(task[3] for task in tasks))
    task_queue: "queue.Queue[Tuple[int, str, List[Any], int]]" = queue.Queue()
    for task in tasks:
        task_queue.put(task)
    batches: "queue.Queue[Optional[Any]]" = queue.Queue(maxsize=8)
    errors: List[BaseException] = []
    stop = threading.Event()
    progress_lock = threading.Lock()
    state = {"scanned": 0}
    names = ["rid"] + [f"c{index}" for index in range(len(columns))] + ["wkb"]
    insert_sql = (
        "INSERT INTO src_raw SELECT (CAST(? AS BIGINT) << 40) + rid, ?, "
        + ", ".join(f"c{index}" for index in range(len(columns)))
        + ", wkb FROM gpkg_batch"
    )

    def put(item: Any) -> None:
        while not stop.is_set():
            try:
                batches.put(item, timeout=0.5)
                return
            except queue.Full:
                continue

    def producer() -> None:
        try:
            gpkg = _gpkg_connect(read_path)
            try:
                while not stop.is_set():
                    try:
                        layer_idx, sql, params, weight = task_queue.get_nowait()
                    except queue.Empty:
                        break
                    layer_name = source["layers"][layer_idx]["name"]
                    cursor = gpkg.execute(sql, params)
                    while not stop.is_set():
                        rows = cursor.fetchmany(GPKG_FETCH_ROWS)
                        if not rows:
                            break
                        values = list(zip(*rows))
                        arrays = [pa.array(values[0], pa.int64())]
                        for index in range(len(columns)):
                            arrays.append(pa.array(values[index + 1], arrow_types.get(raw_types[index], pa.string())))
                        arrays.append(pa.array(values[-1], pa.binary()))
                        put((layer_idx, pa.Table.from_arrays(arrays, names=names)))
                    with progress_lock:
                        state["scanned"] += weight
                        scanned = state["scanned"]
                    report(min(0.99, scanned / total), f"Reading {layer_name}: {scanned:,} of ~{total:,} features scanned")
            finally:
                gpkg.close()
        except BaseException as exc:
            errors.append(exc)
            stop.set()
        finally:
            try:
                batches.put(None, timeout=5)
            except queue.Full:
                pass

    reader_count = max(1, min(PREVIEW_READERS if cfg["bbox"] else GPKG_READERS, len(tasks)))
    readers = [threading.Thread(target=producer, daemon=True) for _ in range(reader_count)]
    for reader in readers:
        reader.start()
    cursor = con.cursor()
    finished = 0
    try:
        while finished < reader_count:
            try:
                item = batches.get(timeout=0.5)
            except queue.Empty:
                if errors:
                    break
                continue
            if item is None:
                finished += 1
                continue
            layer_idx, table = item
            cursor.register("gpkg_batch", table)
            cursor.execute(insert_sql, [layer_idx, layer_idx])
            cursor.unregister("gpkg_batch")
    except BaseException:
        stop.set()
        raise
    finally:
        cursor.close()
        stop.set()
        for reader in readers:
            reader.join(timeout=5)
    if errors:
        raise errors[0]


def _stage_parquet(con: duckdb.DuckDBPyConnection, cfg: Dict[str, Any], read_path: Path, report: Callable[[float, str], None]) -> None:
    source = cfg["source"]
    filter_sql, params = _duck_filter_sql(cfg["filter"])
    select_cols = ", ".join(
        f"{_sql_identifier(column['source'])} AS c{index}" for index, column in enumerate(cfg["columns"])
    )
    where = [filter_sql]
    params = list(params)
    if cfg["bbox"]:
        crs = source["layers"][0]["crs"]
        box = _bbox_in_crs(con, cfg["bbox"], crs)
        if source.get("lon"):
            where.append(
                f"TRY_CAST({_sql_identifier(source['lon'])} AS DOUBLE) BETWEEN ? AND ? "
                f"AND TRY_CAST({_sql_identifier(source['lat'])} AS DOUBLE) BETWEEN ? AND ?"
            )
            params += [box[0], box[2], box[1], box[3]]
        else:
            where.append(f"ST_Intersects({source['geometry_sql']}, ST_MakeEnvelope(?, ?, ?, ?))")
            params += [box[0], box[1], box[2], box[3]]
    sql = f"""
        CREATE TABLE src_raw AS
        SELECT file_row_number AS src_id, CAST(0 AS SMALLINT) AS layer_idx, {select_cols},
               {source['geometry_sql']} AS g
        FROM read_parquet({_sql_string(str(read_path))}, file_row_number = true)
        WHERE {' AND '.join(where)}
    """
    _execute_tracked(con, sql, params, report, "Reading Parquet source")


def _normalize_source(con: duckdb.DuckDBPyConnection, cfg: Dict[str, Any], report: Callable[[float, str], None]) -> None:
    kind = cfg["source"]["geometry_kind"]
    layers = cfg["source"]["layers"]
    crs_values = {layer["crs"].upper() for layer in layers}
    if crs_values == {"EPSG:3035"}:
        transform = "g"
    elif len(crs_values) == 1:
        crs = next(iter(crs_values))
        transform = f"ST_Transform(g, {_sql_string(crs)}, 'EPSG:3035', always_xy := true)"
    else:
        branches = " ".join(
            f"WHEN {index} THEN "
            + ("g" if layer["crs"].upper() == "EPSG:3035"
               else f"ST_Transform(g, {_sql_string(layer['crs'])}, 'EPSG:3035', always_xy := true)")
            for index, layer in enumerate(layers)
        )
        transform = f"CASE layer_idx {branches} END"

    if kind == "polygon":
        # No blanket ST_IsValid pass: it dominates reprojection time, and overlap maths is already TRY-guarded.
        fix = "g"
        point = "COALESCE(TRY(ST_PointOnSurface(g)), ST_Centroid(g))"
        area = "ST_Area(g)"
    else:
        fix = "g"
        point = "CASE WHEN ST_GeometryType(g) = 'POINT' THEN g ELSE ST_Centroid(g) END"
        area = "0.0"
    cell = CELL_SIZE_M
    raw_geometry = "TRY(ST_GeomFromWKB(wkb))" if cfg["source"]["kind"] == "gpkg" else "g"
    raw_geometry_col = "wkb" if cfg["source"]["kind"] == "gpkg" else "g"
    sql = f"""
        CREATE TABLE src AS
        WITH flat AS (
            SELECT * EXCLUDE ({raw_geometry_col}), ST_Force2D({raw_geometry}) AS g
            FROM src_raw WHERE {raw_geometry_col} IS NOT NULL
        ), fixed AS (
            SELECT * EXCLUDE (g), {fix} AS g FROM flat WHERE g IS NOT NULL AND NOT ST_IsEmpty(g)
        ), projected AS (
            SELECT * EXCLUDE (g), {transform} AS g FROM fixed WHERE g IS NOT NULL
        ), pointed AS (
            SELECT *, {point} AS pt FROM projected WHERE g IS NOT NULL AND NOT ST_IsEmpty(g)
        )
        SELECT *, {area} AS area,
            ST_XMin(g) AS bx0, ST_YMin(g) AS by0, ST_XMax(g) AS bx1, ST_YMax(g) AS by1,
            CAST(floor(ST_X(pt) / {cell}) AS INTEGER) AS cx,
            CAST(floor(ST_Y(pt) / {cell}) AS INTEGER) AS cy
        FROM pointed
        WHERE pt IS NOT NULL
        ORDER BY cy, cx
    """
    _execute_tracked(con, sql, None, report, "Reprojecting and repairing source geometries")
    con.execute("DROP TABLE src_raw")


def _stage_targets(con: duckdb.DuckDBPyConnection, cfg: Dict[str, Any], report: Callable[[float, str], None]) -> None:
    con.execute(f"ATTACH {_sql_string(cfg['target_db'])} AS tdb (READ_ONLY)")
    names = set(cfg["target_columns"])
    if "geom_3035" in names:
        geom = "geom_3035"
    else:
        geom = "ST_Transform(geom, 'EPSG:4326', 'EPSG:3035', always_xy := true)"
    area = "CAST(footprint_area_m2 AS DOUBLE)" if "footprint_area_m2" in names else "CAST(NULL AS DOUBLE)"
    where = "TRUE"
    params: List[Any] = []
    if cfg["bbox"]:
        west, south, east, north = cfg["bbox"]
        if {"centroid_lon", "centroid_lat"} <= names:
            where = "centroid_lon BETWEEN ? AND ? AND centroid_lat BETWEEN ? AND ?"
            params = [west, east, south, north]
        else:
            where = "ST_Intersects(geom, ST_MakeEnvelope(?, ?, ?, ?))"
            params = [west, south, east, north]
    preview_geom = ", geom AS g4" if cfg["preview"] and "geom" in names else ""
    cell = CELL_SIZE_M
    sql = f"""
        CREATE TABLE tgt AS
        WITH b AS (
            SELECT rowid AS rid, {geom} AS g, {area} AS area0{preview_geom}
            FROM tdb.buildings WHERE {where}
        ), p AS (
            SELECT *, COALESCE(TRY(ST_PointOnSurface(g)), ST_Centroid(g)) AS pt
            FROM b WHERE g IS NOT NULL AND NOT ST_IsEmpty(g)
        )
        SELECT * EXCLUDE (area0), COALESCE(area0, ST_Area(g)) AS area,
            ST_XMin(g) AS bx0, ST_YMin(g) AS by0, ST_XMax(g) AS bx1, ST_YMax(g) AS by1,
            CAST(floor(ST_X(pt) / {cell}) AS INTEGER) AS cx,
            CAST(floor(ST_Y(pt) / {cell}) AS INTEGER) AS cy
        FROM p
        WHERE pt IS NOT NULL
        ORDER BY cy, cx
    """
    _execute_tracked(con, sql, params, report, "Staging target buildings")


# ---------------------------------------------------------------------------
# Worker: matching
# ---------------------------------------------------------------------------

def _cell_batches(con: duckdb.DuckDBPyConnection, table: str, cap: int) -> List[Dict[str, Any]]:
    rows = con.execute(f"""
        SELECT cy, cx, COUNT(*), MIN(bx0), MIN(by0), MAX(bx1), MAX(by1)
        FROM {table} GROUP BY cy, cx ORDER BY cy, cx
    """).fetchall()
    batches: List[Dict[str, Any]] = []
    for cy, cx, count, x0, y0, x1, y1 in rows:
        current = batches[-1] if batches else None
        if current and current["n"] + count <= cap:
            current["end"] = [cy, cx]
            current["n"] += count
            current["ext"] = [min(current["ext"][0], x0), min(current["ext"][1], y0),
                              max(current["ext"][2], x1), max(current["ext"][3], y1)]
        else:
            batches.append({"start": [cy, cx], "end": [cy, cx], "n": int(count), "ext": [x0, y0, x1, y1]})
    return batches


def _cell_range(batch: Dict[str, Any]) -> Tuple[str, List[Any]]:
    (cy0, cx0), (cy1, cx1) = batch["start"], batch["end"]
    return (
        "(cy > ? OR (cy = ? AND cx >= ?)) AND (cy < ? OR (cy = ? AND cx <= ?))",
        [cy0, cy0, cx0, cy1, cy1, cx1],
    )


def _match_polygons(con: duckdb.DuckDBPyConnection, cfg: Dict[str, Any], report: Callable[[float, str], None]) -> None:
    radius = float(cfg["match"]["radius_m"])
    con.execute("""
        CREATE TABLE matches (rid BIGINT, src_id BIGINT, inside BOOLEAN, distance_m DOUBLE, iou DOUBLE, area_ratio DOUBLE)
    """)
    batches = _cell_batches(con, "tgt", BATCH_TARGET_ROWS)
    total = max(1, sum(batch["n"] for batch in batches))
    processed = 0
    for batch in batches:
        x0, y0, x1, y1 = batch["ext"]
        range_sql, range_params = _cell_range(batch)
        con.execute(
            "CREATE OR REPLACE TEMP TABLE bt AS SELECT rid, g, pt, area, bx0, by0, bx1, by1 "
            f"FROM tgt WHERE {range_sql}",
            range_params,
        )
        con.execute(
            "CREATE OR REPLACE TEMP TABLE bs AS SELECT src_id, g, area FROM src "
            "WHERE bx1 >= ? AND bx0 <= ? AND by1 >= ? AND by0 <= ?",
            [x0 - radius, x1 + radius, y0 - radius, y1 + radius],
        )
        if con.execute("SELECT COUNT(*) FROM bs").fetchone()[0]:
            con.execute(f"""
                CREATE OR REPLACE TEMP TABLE bp AS
                WITH ins AS (
                    SELECT bt.rid, bs.src_id, TRUE AS inside
                    FROM bt JOIN bs ON ST_Intersects(bs.g, bt.pt)
                ), un AS (
                    SELECT bt.* FROM bt ANTI JOIN (SELECT DISTINCT rid FROM ins) i USING (rid)
                ), nr AS (
                    SELECT un.rid, bs.src_id, FALSE AS inside
                    FROM un JOIN bs ON ST_Intersects(
                        bs.g,
                        ST_MakeEnvelope(un.bx0 - {radius}, un.by0 - {radius}, un.bx1 + {radius}, un.by1 + {radius})
                    )
                )
                SELECT * FROM ins UNION ALL SELECT * FROM nr
            """)
            con.execute(f"""
                INSERT INTO matches
                WITH sc AS (
                    SELECT bp.rid, bp.src_id, bp.inside,
                        CASE WHEN bp.inside THEN 0.0 ELSE ST_Distance(bt.g, bs.g) END AS distance_m,
                        bt.g AS tg, bs.g AS sg, bt.area AS ta, bs.area AS sa
                    FROM bp JOIN bt USING (rid) JOIN bs USING (src_id)
                ), kept AS (
                    SELECT * FROM sc WHERE inside OR distance_m <= {radius}
                ), ov AS (
                    SELECT rid, src_id, inside, distance_m, ta, sa,
                        CASE
                            WHEN NOT (inside OR distance_m = 0) THEN 0.0
                            ELSE COALESCE(TRY(ST_Area(ST_Intersection(tg, sg))), 0.0)
                        END AS inter
                    FROM kept
                )
                SELECT rid, src_id, inside, distance_m,
                    inter / NULLIF(ta + sa - inter, 0) AS iou,
                    sa / NULLIF(ta, 0) AS area_ratio
                FROM ov
                QUALIFY row_number() OVER (
                    PARTITION BY rid ORDER BY inside DESC, iou DESC NULLS LAST, distance_m, src_id
                ) = 1
            """)
        processed += batch["n"]
        report(processed / total, f"Matched {processed:,} of {total:,} target buildings")
    for table in ("bt", "bs", "bp"):
        con.execute(f"DROP TABLE IF EXISTS {table}")


def _match_points(con: duckdb.DuckDBPyConnection, cfg: Dict[str, Any], report: Callable[[float, str], None]) -> None:
    radius = float(cfg["match"]["radius_m"])
    con.execute("CREATE TABLE pmatches (src_id BIGINT, rid BIGINT, inside BOOLEAN, distance_m DOUBLE)")
    batches = _cell_batches(con, "src", BATCH_TARGET_ROWS)
    total = max(1, sum(batch["n"] for batch in batches))
    processed = 0
    for batch in batches:
        x0, y0, x1, y1 = batch["ext"]
        range_sql, range_params = _cell_range(batch)
        con.execute(
            f"CREATE OR REPLACE TEMP TABLE bs AS SELECT src_id, pt FROM src WHERE {range_sql}",
            range_params,
        )
        con.execute(
            "CREATE OR REPLACE TEMP TABLE bt AS SELECT rid, g, area FROM tgt "
            "WHERE bx1 >= ? AND bx0 <= ? AND by1 >= ? AND by0 <= ?",
            [x0 - radius, x1 + radius, y0 - radius, y1 + radius],
        )
        if con.execute("SELECT COUNT(*) FROM bt").fetchone()[0]:
            con.execute(f"""
                INSERT INTO pmatches
                WITH ins AS (
                    SELECT bs.src_id, bt.rid, bt.area
                    FROM bs JOIN bt ON ST_Intersects(bt.g, bs.pt)
                ), ins1 AS (
                    SELECT src_id, rid FROM ins
                    QUALIFY row_number() OVER (PARTITION BY src_id ORDER BY area, rid) = 1
                ), un AS (
                    SELECT bs.* FROM bs ANTI JOIN ins1 USING (src_id)
                ), nr AS (
                    SELECT un.src_id, bt.rid, bt.area, ST_Distance(bt.g, un.pt) AS distance_m
                    FROM un JOIN bt ON ST_Intersects(
                        bt.g,
                        ST_MakeEnvelope(ST_X(un.pt) - {radius}, ST_Y(un.pt) - {radius},
                                        ST_X(un.pt) + {radius}, ST_Y(un.pt) + {radius})
                    )
                ), nr1 AS (
                    SELECT src_id, rid, distance_m FROM nr WHERE distance_m <= {radius}
                    QUALIFY row_number() OVER (PARTITION BY src_id ORDER BY distance_m, area, rid) = 1
                )
                SELECT src_id, rid, TRUE, 0.0 FROM ins1
                UNION ALL
                SELECT src_id, rid, FALSE, distance_m FROM nr1
            """)
        processed += batch["n"]
        report(processed / total, f"Matched {processed:,} of {total:,} source points")
    for table in ("bt", "bs"):
        con.execute(f"DROP TABLE IF EXISTS {table}")


def _find_new_buildings(con: duckdb.DuckDBPyConnection, report: Callable[[float, str], None]) -> None:
    con.execute("CREATE TABLE new_src (src_id BIGINT)")
    con.execute("""
        CREATE TABLE unmatched_src AS
        SELECT s.src_id, s.pt, s.cx, s.cy,
            ST_X(s.pt) AS bx0, ST_Y(s.pt) AS by0, ST_X(s.pt) AS bx1, ST_Y(s.pt) AS by1
        FROM src s ANTI JOIN (SELECT DISTINCT src_id FROM matches) m USING (src_id)
        ORDER BY s.cy, s.cx
    """)
    batches = _cell_batches(con, "unmatched_src", BATCH_TARGET_ROWS)
    total = max(1, sum(batch["n"] for batch in batches))
    processed = 0
    for batch in batches:
        x0, y0, x1, y1 = batch["ext"]
        range_sql, range_params = _cell_range(batch)
        con.execute(
            f"CREATE OR REPLACE TEMP TABLE bu AS SELECT src_id, pt FROM unmatched_src WHERE {range_sql}",
            range_params,
        )
        con.execute(
            "CREATE OR REPLACE TEMP TABLE bt AS SELECT g FROM tgt "
            "WHERE bx1 >= ? AND bx0 <= ? AND by1 >= ? AND by0 <= ?",
            [x0, x1, y0, y1],
        )
        con.execute("""
            INSERT INTO new_src
            SELECT bu.src_id FROM bu
            ANTI JOIN (SELECT DISTINCT bu.src_id FROM bu JOIN bt ON ST_Intersects(bt.g, bu.pt)) hit USING (src_id)
        """)
        processed += batch["n"]
        report(processed / total, f"Checked {processed:,} of {total:,} unmatched source features")
    for table in ("bt", "bu"):
        con.execute(f"DROP TABLE IF EXISTS {table}")
    con.execute("DROP TABLE unmatched_src")


def _output_select(cfg: Dict[str, Any], alias: str) -> List[str]:
    return [
        f"{_cast_sql(f'{alias}.c{index}', column['type'])} AS {_sql_identifier(column['output'])}"
        for index, column in enumerate(cfg["columns"])
    ]


def _build_supplement_tables(con: duckdb.DuckDBPyConnection, cfg: Dict[str, Any], report: Callable[[float, str], None]) -> None:
    prefix = cfg["prefix"]
    radius = float(cfg["match"]["radius_m"])
    min_iou = float(cfg["match"]["min_iou"])
    outputs = _output_select(cfg, "s")
    ident = _sql_identifier

    if cfg["source"]["geometry_kind"] == "polygon":
        sql = f"""
            CREATE TABLE supp AS
            SELECT m.rid,
                {', '.join(outputs)},
                CASE WHEN m.inside THEN 'inside' WHEN m.distance_m = 0 THEN 'overlap' ELSE 'nearest' END
                    AS {ident(prefix + 'match_type')},
                CASE
                    WHEN m.inside AND COALESCE(m.iou, 0) >= {min_iou} THEN 'high'
                    WHEN m.inside OR m.distance_m = 0 THEN 'medium'
                    ELSE 'low'
                END AS {ident(prefix + 'match_confidence')},
                round(m.distance_m, 2) AS {ident(prefix + 'match_distance_m')},
                round(m.iou, 3) AS {ident(prefix + 'match_iou')},
                CAST(COUNT(*) OVER (PARTITION BY m.src_id) AS INTEGER) AS {ident(prefix + 'match_shared')}
            FROM matches m JOIN src s USING (src_id)
            ORDER BY m.rid
        """
    else:
        aggregate = cfg["match"]["point_aggregate"]
        agg_parts = []
        best_parts = []
        for index, column in enumerate(cfg["columns"]):
            name = ident(column["output"])
            numeric = column["type"] in {"double", "integer"} or (
                column["type"] == "auto" and _is_numeric_type(column["native_type"])
            )
            if aggregate != "closest" and numeric:
                fn = {"mean": "AVG", "max": "MAX", "min": "MIN", "sum": "SUM"}[aggregate]
                agg_parts.append(f"{fn}(TRY_CAST(j.c{index} AS DOUBLE)) AS a{index}")
                cast_type = OUTPUT_TYPES.get(column["type"])
                expr = f"a.a{index}" if aggregate == "mean" or not cast_type else f"TRY_CAST(a.a{index} AS {cast_type})"
                best_parts.append(f"{expr} AS {name}")
            else:
                best_parts.append(f"{_cast_sql(f'b.c{index}', column['type'])} AS {name}")
        agg_sql = (", " + ", ".join(agg_parts)) if agg_parts else ""
        raw_cols = ", ".join(f"s.c{index}" for index in range(len(cfg["columns"])))
        sql = f"""
            CREATE TABLE supp AS
            WITH j AS (
                SELECT p.rid, p.src_id, p.inside, p.distance_m, {raw_cols}
                FROM pmatches p JOIN src s USING (src_id)
            ), b AS (
                SELECT * FROM j
                QUALIFY row_number() OVER (PARTITION BY rid ORDER BY inside DESC, distance_m, src_id) = 1
            ), a AS (
                SELECT rid, COUNT(*) AS n{agg_sql} FROM j GROUP BY rid
            )
            SELECT b.rid,
                {', '.join(best_parts)},
                CASE WHEN b.inside THEN 'inside' ELSE 'nearest' END AS {ident(prefix + 'match_type')},
                CASE WHEN b.inside THEN 'high' WHEN b.distance_m <= {radius / 2.0} THEN 'medium' ELSE 'low' END
                    AS {ident(prefix + 'match_confidence')},
                round(b.distance_m, 2) AS {ident(prefix + 'match_distance_m')},
                CAST(a.n AS INTEGER) AS {ident(prefix + 'match_count')}
            FROM b JOIN a USING (rid)
            ORDER BY b.rid
        """
    _execute_tracked(con, sql, None, report, "Assembling supplement columns")

    if not cfg["add_new_buildings"]:
        return

    names = set(cfg["target_columns"])
    quadkey = _quadkey_prefix_sql("lon", "lat", zoom=18)
    core = {
        "building_id": f"{_sql_string(prefix + 'new_')} || CAST(src_id AS VARCHAR)",
        "source": _sql_string(f"supplement:{Path(cfg['source']['path']).stem}"),
        "geom": "g4",
        "geom_3035": "g",
        "centroid_lon": "lon",
        "centroid_lat": "lat",
        "bbox_xmin": "ST_XMin(g4)",
        "bbox_ymin": "ST_YMin(g4)",
        "bbox_xmax": "ST_XMax(g4)",
        "bbox_ymax": "ST_YMax(g4)",
        "bbox_3035_xmin": "bx0",
        "bbox_3035_ymin": "by0",
        "bbox_3035_xmax": "bx1",
        "bbox_3035_ymax": "by1",
        "footprint_area_m2": "area",
        "quadkey": "qk",
        "quadkey_prefix_6": "substr(qk, 1, 6)",
        "quadkey_prefix_14": "substr(qk, 1, 14)",
    }
    core_select = [f"{expr} AS {ident(name)}" for name, expr in core.items() if name in names]
    match_select = [
        f"'new_building' AS {ident(prefix + 'match_type')}",
        f"'source_only' AS {ident(prefix + 'match_confidence')}",
        f"CAST(NULL AS DOUBLE) AS {ident(prefix + 'match_distance_m')}",
        f"CAST(NULL AS DOUBLE) AS {ident(prefix + 'match_iou')}",
        f"CAST(NULL AS INTEGER) AS {ident(prefix + 'match_shared')}",
    ]
    order = "quadkey_prefix_14" if "quadkey_prefix_14" in names else ("quadkey_prefix_6" if "quadkey_prefix_6" in names else "centroid_lon")
    order_sql = ident(order) if order in names else "1"
    sql = f"""
        CREATE TABLE new_rows AS
        WITH n AS (
            SELECT s.* FROM src s SEMI JOIN new_src USING (src_id)
        ), w AS (
            SELECT n.*, ST_Transform(n.g, 'EPSG:3035', 'EPSG:4326', always_xy := true) AS g4 FROM n
        ), c AS (
            SELECT w.*, ST_X(ST_Centroid(g4)) AS lon, ST_Y(ST_Centroid(g4)) AS lat FROM w
        ), q AS (
            SELECT c.*, {quadkey} AS qk FROM c
        )
        SELECT {', '.join(core_select + _output_select(cfg, 'q') + match_select)}
        FROM q
        ORDER BY {order_sql}
    """
    _execute_tracked(con, sql, None, report, "Preparing new buildings")


def _collect_stats(con: duckdb.DuckDBPyConnection, cfg: Dict[str, Any], started: float) -> Dict[str, Any]:
    prefix = cfg["prefix"]
    type_col = _sql_identifier(prefix + "match_type")
    conf_col = _sql_identifier(prefix + "match_confidence")
    targets = int(con.execute("SELECT COUNT(*) FROM tgt").fetchone()[0])
    sources = int(con.execute("SELECT COUNT(*) FROM src").fetchone()[0])
    matched = int(con.execute("SELECT COUNT(*) FROM supp").fetchone()[0])
    by_type = {str(k): int(v) for k, v in con.execute(f"SELECT {type_col}, COUNT(*) FROM supp GROUP BY 1").fetchall()}
    by_conf = {str(k): int(v) for k, v in con.execute(f"SELECT {conf_col}, COUNT(*) FROM supp GROUP BY 1").fetchall()}
    stats: Dict[str, Any] = {
        "targets": targets,
        "sources": sources,
        "matched_targets": matched,
        "unmatched_targets": max(0, targets - matched),
        "by_type": by_type,
        "by_confidence": by_conf,
        "geometry_kind": cfg["source"]["geometry_kind"],
        "columns": [column["output"] for column in cfg["columns"]] + [name for name, _t in cfg["match_columns"]],
        "elapsed_seconds": round(time.time() - started, 1),
    }
    if cfg["source"]["geometry_kind"] == "polygon":
        row = con.execute("""
            SELECT COUNT(DISTINCT src_id), AVG(iou) FILTER (WHERE inside)
            FROM matches
        """).fetchone()
        stats["sources_matched"] = int(row[0] or 0)
        stats["mean_iou_inside"] = round(float(row[1]), 3) if row[1] is not None else None
        stats["new_buildings"] = int(con.execute("SELECT COUNT(*) FROM new_rows").fetchone()[0]) if cfg["add_new_buildings"] else 0
    else:
        stats["sources_matched"] = int(con.execute("SELECT COUNT(*) FROM pmatches").fetchone()[0])
        stats["new_buildings"] = 0
    return stats


def _preview_geojson(con: duckdb.DuckDBPyConnection, cfg: Dict[str, Any]) -> Dict[str, Any]:
    prefix = cfg["prefix"]
    shown = [column["output"] for column in cfg["columns"]][:4]
    prop_sql = "".join(f", s.{_sql_identifier(name)}" for name in shown)
    target_rows = con.execute(f"""
        SELECT ST_AsGeoJSON(t.g4), s.{_sql_identifier(prefix + 'match_type')},
               s.{_sql_identifier(prefix + 'match_confidence')}, s.{_sql_identifier(prefix + 'match_distance_m')}
               {(', s.' + _sql_identifier(prefix + 'match_iou')) if cfg['source']['geometry_kind'] == 'polygon' else ', NULL'}
               {prop_sql}
        FROM tgt t LEFT JOIN supp s USING (rid)
        WHERE t.g4 IS NOT NULL
        LIMIT {PREVIEW_MAX_FEATURES}
    """).fetchall()
    features = []
    for row in target_rows:
        props = {
            "layer": "target",
            "match_type": row[1] or "none",
            "confidence": row[2] or "none",
            "distance_m": row[3],
            "iou": row[4],
        }
        for offset, name in enumerate(shown):
            value = row[5 + offset]
            props[name] = value if isinstance(value, (int, float, str)) or value is None else str(value)
        features.append({"type": "Feature", "geometry": json.loads(row[0]), "properties": props})

    if cfg["source"]["geometry_kind"] == "polygon":
        new_join = "LEFT JOIN new_src n USING (src_id)" if cfg["add_new_buildings"] else ""
        new_flag = "n.src_id IS NOT NULL" if cfg["add_new_buildings"] else "FALSE"
        source_rows = con.execute(f"""
            SELECT ST_AsGeoJSON(ST_Transform(s.g, 'EPSG:3035', 'EPSG:4326', always_xy := true)),
                   {new_flag}, m.src_id IS NOT NULL
            FROM src s {new_join}
            LEFT JOIN (SELECT DISTINCT src_id FROM matches) m USING (src_id)
            LIMIT {PREVIEW_MAX_FEATURES}
        """).fetchall()
    else:
        source_rows = con.execute(f"""
            SELECT ST_AsGeoJSON(ST_Transform(s.pt, 'EPSG:3035', 'EPSG:4326', always_xy := true)),
                   FALSE, p.src_id IS NOT NULL
            FROM src s LEFT JOIN pmatches p USING (src_id)
            LIMIT {PREVIEW_MAX_FEATURES}
        """).fetchall()
    for geometry, is_new, is_matched in source_rows:
        features.append({
            "type": "Feature",
            "geometry": json.loads(geometry),
            "properties": {
                "layer": "source",
                "state": "new" if is_new else ("matched" if is_matched else "unmatched"),
            },
        })
    return {"type": "FeatureCollection", "features": features, "shown_fields": shown}


def prepare_supplement(cfg: Dict[str, Any], progress: ProgressWriter) -> Dict[str, Any]:
    started = time.time()
    work_dir = Path(cfg["work_dir"])
    work_db = Path(cfg["work_db"])
    for stale in (work_db, Path(str(work_db) + ".wal")):
        stale.unlink(missing_ok=True)
    con = _connect(work_db, work_dir, cfg["threads"])
    timings: Dict[str, float] = {}
    mark = [time.time()]

    def lap(name: str) -> None:
        now = time.time()
        timings[name] = round(now - mark[0], 1)
        mark[0] = now

    try:
        con.execute("SET preserve_insertion_order = false;")
        source_path = Path(cfg["source"]["path"])
        read_path = source_path
        phase_start = 0.0
        if cfg["copy_source_local"]:
            read_path = work_dir / f"source_{cfg['job_id']}{source_path.suffix.lower()}"
            _copy_with_progress(source_path, read_path, progress.span("Copying source to working folder", 0.0, 0.12), "Copying")
            phase_start = 0.12
            lap("copy_source")

        stage_span = progress.span("Reading source", phase_start, 0.42)
        if cfg["source"]["kind"] == "gpkg":
            _stage_gpkg(con, cfg, read_path, stage_span)
        else:
            _stage_parquet(con, cfg, read_path, stage_span)
        if read_path != source_path:
            read_path.unlink(missing_ok=True)
        lap("read_source")

        _normalize_source(con, cfg, progress.span("Reprojecting source", 0.42, 0.52))
        lap("reproject_source")
        source_count = int(con.execute("SELECT COUNT(*) FROM src").fetchone()[0])
        if source_count == 0:
            raise ValueError(
                "No source features were loaded. Check the layer selection, the filter, and (for previews) the map view."
            )

        _stage_targets(con, cfg, progress.span("Staging target buildings", 0.52, 0.62))
        lap("stage_targets")
        if cfg["source"]["geometry_kind"] == "polygon":
            _match_polygons(con, cfg, progress.span("Matching footprints", 0.62, 0.9))
            lap("match")
            if cfg["add_new_buildings"]:
                _find_new_buildings(con, progress.span("Finding new buildings", 0.9, 0.95))
                lap("new_buildings")
        else:
            _match_points(con, cfg, progress.span("Matching points to buildings", 0.62, 0.93))
            lap("match")

        _build_supplement_tables(con, cfg, progress.span("Assembling supplement", 0.95, 0.99))
        lap("assemble")
        stats = _collect_stats(con, cfg, started)
        stats["timings"] = timings
        result: Dict[str, Any] = {"stats": stats}
        if cfg["preview"]:
            result["geojson"] = _preview_geojson(con, cfg)
        else:
            for table in ("tgt", "matches", "pmatches", "new_src"):
                con.execute(f"DROP TABLE IF EXISTS {table}")
            con.execute("CHECKPOINT")
        progress("Matching complete", 1.0, force=True)
        return result
    finally:
        con.close()


# ---------------------------------------------------------------------------
# Worker: commit
# ---------------------------------------------------------------------------

def _update_display_fields(con: duckdb.DuckDBPyConnection, cfg: Dict[str, Any]) -> None:
    building_columns = {
        str(row[0]) for row in con.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_catalog = current_database() AND table_name = 'buildings'"
        ).fetchall()
    }
    if not _table_exists(con, DISPLAY_FIELDS_TABLE):
        con.execute(f"""
            CREATE TABLE {DISPLAY_FIELDS_TABLE} (
                field_name VARCHAR PRIMARY KEY,
                display_label VARCHAR NOT NULL,
                display_order INTEGER NOT NULL
            )
        """)
        seed = [name for name in cfg["display_seed_fields"] if name in building_columns]
        for order, name in enumerate(seed, start=1):
            con.execute(f"INSERT INTO {DISPLAY_FIELDS_TABLE} VALUES (?, ?, ?)", [name, name, order])
    next_order = int(con.execute(f"SELECT COALESCE(MAX(display_order), 0) FROM {DISPLAY_FIELDS_TABLE}").fetchone()[0]) + 1
    new_fields = [column["output"] for column in cfg["columns"]] + [cfg["prefix"] + "match_confidence"]
    for name in new_fields:
        con.execute(f"DELETE FROM {DISPLAY_FIELDS_TABLE} WHERE field_name = ?", [name])
        con.execute(f"INSERT INTO {DISPLAY_FIELDS_TABLE} VALUES (?, ?, ?)", [name, name, next_order])
        next_order += 1


def _write_supplement_log(con: duckdb.DuckDBPyConnection, cfg: Dict[str, Any], stats: Dict[str, Any]) -> None:
    con.execute(f"""
        CREATE TABLE IF NOT EXISTS {SUPPLEMENT_LOG_TABLE} (
            supplement_id VARCHAR PRIMARY KEY,
            created_at TIMESTAMP,
            source_path VARCHAR,
            source_layers VARCHAR,
            prefix VARCHAR,
            columns_json VARCHAR,
            settings_json VARCHAR,
            stats_json VARCHAR,
            output_mode VARCHAR
        )
    """)
    settings = {
        "match": cfg["match"],
        "filter": cfg["filter"],
        "add_new_buildings": cfg["add_new_buildings"],
        "geometry_kind": cfg["source"]["geometry_kind"],
    }
    con.execute(
        f"INSERT INTO {SUPPLEMENT_LOG_TABLE} VALUES (?, now(), ?, ?, ?, ?, ?, ?, ?)",
        [
            cfg["job_id"],
            cfg["source"]["path"],
            ", ".join(layer["name"] for layer in cfg["source"]["layers"]),
            cfg["prefix"],
            json.dumps(cfg["columns"]),
            json.dumps(settings, default=str),
            json.dumps(stats, default=str),
            cfg["output"]["mode"],
        ],
    )


def staged_output_path(cfg: Dict[str, Any]) -> Path:
    final = Path(cfg["target_db"]) if cfg["output"]["mode"] == "inplace" else Path(cfg["output"]["path"])
    return final.with_name(f".{final.stem}.{cfg['job_id']}.tmp.duckdb")


def _build_output_db(out_path: Path, cfg: Dict[str, Any], stats: Dict[str, Any], report: Callable[[float, str], None]) -> Dict[str, Any]:
    """Write a complete, re-indexed lookup DB; the original stays untouched until it is swapped in."""
    for path in (out_path, Path(str(out_path) + ".wal")):
        path.unlink(missing_ok=True)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    con = _connect(out_path, Path(cfg["work_dir"]), cfg["threads"])
    warnings: List[str] = []
    try:
        con.execute("SET preserve_insertion_order = true;")
        con.execute(f"ATTACH {_sql_string(cfg['target_db'])} AS tdb (READ_ONLY)")
        con.execute(f"ATTACH {_sql_string(cfg['work_db'])} AS w (READ_ONLY)")
        target_columns = [str(row[0]) for row in con.execute("DESCRIBE tdb.buildings").fetchall()]
        supp_columns = [str(row[0]) for row in con.execute("DESCRIBE w.supp").fetchall() if str(row[0]) != "rid"]
        replaced = {name.casefold() for name in supp_columns}
        keep = [name for name in target_columns if name.casefold() not in replaced]
        select_sql = ", ".join(
            [f"b.{_sql_identifier(name)}" for name in keep] + [f"s.{_sql_identifier(name)}" for name in supp_columns]
        )
        # ORDER BY rowid keeps the original spatial row order, which the app's zone-map filters rely on.
        _execute_tracked(
            con,
            f"CREATE TABLE buildings AS SELECT {select_sql} FROM tdb.buildings b "
            "LEFT JOIN w.supp s ON s.rid = b.rowid ORDER BY b.rowid",
            None,
            lambda fraction, detail: report(0.6 * fraction, detail),
            "Writing buildings with supplement columns",
        )

        inserted = 0
        if cfg["add_new_buildings"] and _table_exists(con, "new_rows", "w"):
            inserted = int(con.execute("SELECT COUNT(*) FROM w.new_rows").fetchone()[0])
            if inserted:
                _execute_tracked(
                    con,
                    "INSERT INTO buildings BY NAME SELECT * FROM w.new_rows",
                    None,
                    lambda fraction, detail: report(0.6 + 0.05 * fraction, detail),
                    f"Appending {inserted:,} new buildings",
                )

        other_tables = [
            str(row[0]) for row in con.execute("""
                SELECT table_name FROM information_schema.tables
                WHERE table_catalog = 'tdb' AND table_schema = 'main'
                  AND table_type = 'BASE TABLE' AND table_name <> 'buildings'
            """).fetchall()
        ]
        for table in other_tables:
            con.execute(f"CREATE TABLE {_sql_identifier(table)} AS SELECT * FROM tdb.main.{_sql_identifier(table)}")
        report(0.66, "Copied auxiliary tables")

        index_sql = [
            str(row[0]) for row in con.execute("""
                SELECT sql FROM duckdb_indexes()
                WHERE database_name = 'tdb' AND table_name = 'buildings' AND sql IS NOT NULL
            """).fetchall()
        ]
        for position, sql in enumerate(index_sql):
            low = 0.66 + 0.3 * position / max(1, len(index_sql))
            high = 0.66 + 0.3 * (position + 1) / max(1, len(index_sql))
            try:
                _execute_tracked(
                    con,
                    sql,
                    None,
                    lambda fraction, detail, low=low, high=high: report(low + (high - low) * fraction, detail),
                    f"Rebuilding index {position + 1} of {len(index_sql)}",
                )
            except duckdb.Error as exc:
                warnings.append(f"Index not rebuilt ({sql.strip()[:80]}): {exc}")

        _update_display_fields(con, cfg)
        _write_supplement_log(con, cfg, stats)
        con.execute("DETACH w")
        con.execute("DETACH tdb")
        report(0.97, "Checkpointing database")
        con.execute("CHECKPOINT")
        total_rows = int(con.execute("SELECT COUNT(*) FROM buildings").fetchone()[0])
        report(1.0, "Database written")
        return {
            "rows_total": total_rows,
            "rows_updated": int(stats.get("matched_targets") or 0),
            "rows_inserted": inserted,
            "columns_added": supp_columns,
            "warnings": warnings,
        }
    finally:
        con.close()


def commit_supplement(cfg: Dict[str, Any], progress: ProgressWriter) -> Dict[str, Any]:
    if _file_signature(Path(cfg["target_db"])) != list(cfg["target_signature"]):
        raise RuntimeError("The target database changed after matching. Run the supplement again.")
    staged = staged_output_path(cfg)
    try:
        result = _build_output_db(staged, cfg, cfg.get("stats") or {}, progress.span("Writing database", 0.0, 1.0))
    except BaseException:
        for path in (staged, Path(str(staged) + ".wal")):
            path.unlink(missing_ok=True)
        raise
    result["staged_path"] = str(staged)
    progress("Database written", 1.0, force=True)
    return result


def replace_file(source: Path, destination: Path, attempts: int = 40) -> None:
    for attempt in range(attempts):
        try:
            Path(str(destination) + ".wal").unlink(missing_ok=True)
            os.replace(source, destination)
            return
        except PermissionError:
            if attempt == attempts - 1:
                raise
            time.sleep(0.5)


def cleanup_work_files(cfg: Dict[str, Any], *, remove_staged: bool) -> None:
    work_db = Path(cfg["work_db"])
    candidates = [work_db, Path(str(work_db) + ".wal")]
    source_copy = Path(cfg["work_dir"]) / f"source_{cfg['job_id']}{Path(cfg['source']['path']).suffix.lower()}"
    candidates += [source_copy, source_copy.with_name(source_copy.name + ".part")]
    if remove_staged and not cfg["preview"]:
        staged = staged_output_path(cfg)
        candidates += [staged, Path(str(staged) + ".wal")]
    for path in candidates:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
    shutil.rmtree(Path(cfg["work_dir"]) / "duckdb_tmp", ignore_errors=True)


def worker_main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Supplement worker")
    parser.add_argument("--config", required=True)
    parser.add_argument("--stage", choices=("prepare", "commit"), required=True)
    parser.add_argument("--result", required=True)
    parser.add_argument("--progress", default="")
    parser.add_argument("--progress-start", type=float, default=0.0)
    parser.add_argument("--progress-end", type=float, default=100.0)
    args = parser.parse_args(argv)

    cfg = json.loads(Path(args.config).read_text(encoding="utf-8"))
    progress = ProgressWriter(Path(args.progress) if args.progress else None, args.progress_start, args.progress_end)
    try:
        if args.stage == "prepare":
            payload = {"ok": True, **prepare_supplement(cfg, progress)}
        else:
            payload = {"ok": True, **commit_supplement(cfg, progress)}
        code = 0
    except BaseException as exc:
        payload = {"ok": False, "error": f"{exc}", "traceback": traceback.format_exc()[-6000:]}
        code = 1
    Path(args.result).write_text(json.dumps(payload, default=str), encoding="utf-8")
    return code


# ---------------------------------------------------------------------------
# Flask routes and job runner
# ---------------------------------------------------------------------------

def _worker_command(args: List[str]) -> List[str]:
    if getattr(sys, "frozen", False):
        return [sys.executable, WORKER_FLAG, *args]
    return [sys.executable, str(Path(__file__).resolve()), *args]


def register_supplement_routes(
    app,
    *,
    release_db_connection: Callable[[str], None],
    display_seed_fields: List[str],
) -> None:
    from flask import jsonify, request

    jobs: Dict[str, Dict[str, Any]] = {}
    jobs_lock = threading.Lock()
    runtime_root = Path(tempfile.gettempdir()) / "data_augmentation_runtime" / "supplement"
    runtime_root.mkdir(parents=True, exist_ok=True)

    def set_job(job_id: str, **updates: Any) -> None:
        with jobs_lock:
            jobs.setdefault(job_id, {}).update(updates)

    def get_job(job_id: str) -> Optional[Dict[str, Any]]:
        with jobs_lock:
            job = jobs.get(job_id)
            return dict(job) if job else None

    def running_job() -> Optional[str]:
        with jobs_lock:
            for job_id, job in jobs.items():
                if job.get("status") in {"queued", "running"}:
                    return job_id
        return None

    def run_stage(job_id: str, cfg_path: Path, stage: str, start: float, end: float) -> Dict[str, Any]:
        progress_path = runtime_root / f"{job_id}.{stage}.progress.json"
        result_path = runtime_root / f"{job_id}.{stage}.result.json"
        log_path = runtime_root / f"{job_id}.{stage}.log"
        for path in (progress_path, result_path):
            path.unlink(missing_ok=True)
        command = _worker_command([
            "--config", str(cfg_path),
            "--stage", stage,
            "--result", str(result_path),
            "--progress", str(progress_path),
            "--progress-start", str(start),
            "--progress-end", str(end),
        ])
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        with log_path.open("w", encoding="utf-8", errors="replace") as log_handle:
            process = subprocess.Popen(
                command,
                cwd=str(Path(__file__).resolve().parent),
                stdin=subprocess.DEVNULL,
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                creationflags=creationflags,
            )
            set_job(job_id, pid=process.pid, stage=stage)
            with jobs_lock:
                jobs[job_id]["_process"] = process
            while process.poll() is None:
                time.sleep(0.5)
                try:
                    snapshot = json.loads(progress_path.read_text(encoding="utf-8"))
                    set_job(
                        job_id,
                        phase=snapshot.get("phase"),
                        percent=snapshot.get("percent"),
                        detail=snapshot.get("detail"),
                    )
                except (OSError, ValueError):
                    pass
        with jobs_lock:
            jobs[job_id].pop("_process", None)
            cancelled = jobs[job_id].get("cancel_requested")
        if cancelled:
            raise InterruptedError("Cancelled")
        try:
            result = json.loads(result_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            tail = log_path.read_text(encoding="utf-8", errors="replace")[-3000:] if log_path.exists() else ""
            raise RuntimeError(f"Supplement worker exited with code {process.returncode}. {tail}".strip())
        finally:
            for path in (progress_path, result_path, progress_path.with_suffix(".tmp")):
                path.unlink(missing_ok=True)
        if not result.get("ok"):
            raise RuntimeError(result.get("error") or "Supplement worker failed.")
        log_path.unlink(missing_ok=True)
        return result

    def finalize(cfg: Dict[str, Any], staged: Path) -> Tuple[Path, bool, Optional[str]]:
        if cfg["output"]["mode"] == "new":
            output = Path(cfg["output"]["path"])
            replace_file(staged, output)
            if cfg["output"]["activate"]:
                app.config["DB_PATH"] = _display(output)
                return output, True, None
            return output, False, None

        target = Path(cfg["target_db"])
        if _file_signature(target) != list(cfg["target_signature"]):
            raise RuntimeError("The target database changed while the supplement was running. Nothing was replaced.")
        active = app.config.get("DB_PATH") or ""
        is_active = bool(active) and _resolve_path(active, "Active database") == target
        backup_path: Optional[Path] = None
        if is_active:
            app.config["DB_PATH"] = ""
        try:
            release_db_connection(str(target))
            if cfg["output"].get("keep_backup"):
                backup_path = target.with_name(f"{target.stem}.before_{cfg['prefix'].rstrip('_')}_{time.strftime('%Y%m%d_%H%M%S')}.duckdb")
                replace_file(target, backup_path)
            replace_file(staged, target)
        except BaseException:
            if backup_path is not None and backup_path.exists() and not target.exists():
                os.replace(backup_path, target)
            raise
        finally:
            if is_active:
                app.config["DB_PATH"] = active
        return target, is_active, _display(backup_path) if backup_path else None

    def run_job(job_id: str, cfg: Dict[str, Any]) -> None:
        cfg_path = runtime_root / f"{job_id}.config.json"
        preview = cfg["preview"]
        committed_ok = False
        try:
            cfg_path.write_text(json.dumps(cfg), encoding="utf-8")
            prepared = run_stage(job_id, cfg_path, "prepare", 0.0, 100.0 if preview else 60.0)
            if preview:
                set_job(job_id, status="complete", phase="Preview ready", percent=100,
                        stats=prepared.get("stats"), geojson=prepared.get("geojson"), completed_at=time.time())
                return

            cfg["stats"] = prepared.get("stats") or {}
            cfg_path.write_text(json.dumps(cfg), encoding="utf-8")
            set_job(job_id, stats=cfg["stats"], phase="Matching complete", percent=60)

            committed = run_stage(job_id, cfg_path, "commit", 60.0, 99.0)
            set_job(job_id, cancellable=False, phase="Swapping in the new database", percent=99)
            output_path, activated, backup = finalize(cfg, Path(committed["staged_path"]))
            committed_ok = True
            committed.pop("staged_path", None)
            committed.pop("ok", None)
            set_job(
                job_id,
                status="complete",
                phase="Complete",
                percent=100,
                result={
                    **committed,
                    "output_path": _display(output_path),
                    "activated": activated,
                    "backup_path": backup,
                    "mode": cfg["output"]["mode"],
                },
                completed_at=time.time(),
            )
        except InterruptedError:
            set_job(job_id, status="cancelled", phase="Cancelled", percent=100, completed_at=time.time())
        except Exception as exc:
            set_job(job_id, status="error", phase="Error", error=str(exc), completed_at=time.time())
        finally:
            cleanup_work_files(cfg, remove_staged=not committed_ok)
            cfg_path.unlink(missing_ok=True)

    def prune_jobs() -> None:
        cutoff = time.time() - 3600
        with jobs_lock:
            for job_id in [key for key, job in jobs.items() if (job.get("completed_at") or time.time()) < cutoff]:
                jobs.pop(job_id, None)

    def start_job(preview: bool):
        payload = request.get_json(silent=True) or {}
        busy = running_job()
        if busy:
            return jsonify({"error": "Another supplement job is still running.", "job_id": busy}), 409
        prune_jobs()
        job_id = uuid.uuid4().hex
        try:
            cfg = build_job_config(
                payload,
                preview=preview,
                job_id=job_id,
                active_db_path=app.config.get("DB_PATH") or "",
                display_seed_fields=display_seed_fields,
                preview_root=runtime_root / "preview",
            )
        except (ValueError, duckdb.Error, sqlite3.Error, OSError) as exc:
            return jsonify({"error": str(exc)}), 400
        set_job(
            job_id,
            kind="preview" if preview else "run",
            status="running",
            phase="Starting worker",
            percent=0,
            detail="",
            cancellable=True,
            created_at=time.time(),
            output_mode=cfg["output"]["mode"],
        )
        threading.Thread(target=run_job, args=(job_id, cfg), daemon=True).start()
        return jsonify({"job_id": job_id, "status": "running"}), 202

    @app.route("/api/supplement/inspect", methods=["POST"])
    def supplement_inspect():
        payload = request.get_json(silent=True) or {}
        try:
            source_path = _resolve_path(payload.get("source_path"), "Supplement source")
            source = inspect_source(source_path)
            target_value = payload.get("target_db_path") or app.config.get("DB_PATH") or ""
            target = inspect_target(_resolve_path(target_value, "Target database")) if target_value else None
        except (ValueError, duckdb.Error, sqlite3.Error, OSError) as exc:
            return jsonify({"error": f"Could not inspect: {exc}"}), 400

        defaults: Dict[str, Any] = {"prefix": source["default_prefix"]}
        if target:
            target_path = _resolve_path(target["db_path"], "Target database")
            stem = source["default_prefix"].rstrip("_")
            defaults["output_path"] = _display(target_path.with_name(f"{target_path.stem}_{stem}.duckdb"))
            defaults["work_dir"] = _display(target_path.parent / ".supplement_work")
        return jsonify({"source": source, "target": target, "defaults": defaults})

    @app.route("/api/supplement/space", methods=["POST"])
    def supplement_space():
        payload = request.get_json(silent=True) or {}
        result: Dict[str, Any] = {}
        for value in (payload.get("paths") or [])[:6]:
            try:
                path = _resolve_path(value, "Path")
            except ValueError:
                continue
            result[str(value)] = {"free_bytes": _free_bytes(path), "is_remote": _is_remote_path(path)}
        return jsonify(result)

    @app.route("/api/supplement/preview", methods=["POST"])
    def supplement_preview():
        return start_job(preview=True)

    @app.route("/api/supplement/run", methods=["POST"])
    def supplement_run():
        return start_job(preview=False)

    @app.route("/api/supplement/progress/<job_id>")
    def supplement_progress(job_id: str):
        job = get_job(job_id)
        if job is None:
            return jsonify({"error": "Job not found."}), 404
        job.pop("_process", None)
        return jsonify(job)

    @app.route("/api/supplement/cancel/<job_id>", methods=["POST"])
    def supplement_cancel(job_id: str):
        with jobs_lock:
            job = jobs.get(job_id)
            if job is None:
                return jsonify({"error": "Job not found."}), 404
            if job.get("status") not in {"queued", "running"}:
                return jsonify({"status": job.get("status")})
            if not job.get("cancellable", True):
                return jsonify({"error": "The database is being written and can no longer be cancelled safely."}), 409
            job["cancel_requested"] = True
            process = job.get("_process")
        if process is not None:
            try:
                process.terminate()
            except OSError:
                pass
        return jsonify({"status": "cancelling"})


if __name__ == "__main__":
    sys.exit(worker_main())
