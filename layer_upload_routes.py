import hashlib
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
import zipfile
from pathlib import Path
from threading import Lock
from typing import Any, Dict, List, Optional, Tuple

import duckdb
from flask import Flask, jsonify, request, send_from_directory
from werkzeug.utils import secure_filename


VECTOR_EXTENSIONS = {".gpkg", ".shp", ".shx", ".dbf", ".prj", ".cpg", ".zip", ".geojson", ".json"}
RASTER_EXTENSIONS = {".tif", ".tiff"}
SHAPEFILE_REQUIRED_EXTENSIONS = {".shp", ".shx", ".dbf"}
RASTER_PREVIEW_MAX_SIZE = int(os.environ.get("ADD_LAYER_RASTER_PREVIEW_MAX_SIZE", "4096"))
MAX_VECTOR_FEATURES = int(os.environ.get("ADD_LAYER_MAX_VECTOR_FEATURES", "15000"))
MAX_LAYER_UPLOAD_BYTES = int(os.environ.get("ADD_LAYER_MAX_UPLOAD_BYTES", str(2 * 1024 ** 3)))
MAX_EXTRACTED_FILES = 48
NUMERIC_FIELD_TYPES = {
    "integer",
    "integer64",
    "real",
    "float",
    "double",
    "decimal",
    "smallinteger",
    "bigint",
    "hugeint",
    "utinyint",
    "usmallint",
    "uinteger",
    "ubigint",
}
RESERVED_FEATURE_COLUMNS = {
    "feature_id",
    "geom",
    "geometry_type",
    "bbox_xmin",
    "bbox_ymin",
    "bbox_xmax",
    "bbox_ymax",
}
COLOR_MAPS = {
    "viridis": ["#440154", "#3b528b", "#21918c", "#5ec962", "#fde725"],
    "plasma": ["#0d0887", "#7e03a8", "#cc4778", "#f89540", "#f0f921"],
    "magma": ["#000004", "#3b0f70", "#8c2981", "#de4968", "#fcfdbf"],
    "cividis": ["#00204c", "#414d6b", "#7c7b78", "#b8ad6f", "#ffea46"],
    "hazard": ["#2c7bb6", "#abd9e9", "#ffffbf", "#fdae61", "#d7191c"],
    "reds": ["#fff5f0", "#fcbba1", "#fb6a4a", "#cb181d", "#67000d"],
    "blues": ["#f7fbff", "#c6dbef", "#6baed6", "#2171b5", "#08306b"],
    "categorical": ["#2563eb", "#16a34a", "#dc2626", "#9333ea", "#ea580c", "#0891b2", "#be123c", "#4d7c0f"],
}

LAYER_REGISTRY: Dict[str, Dict[str, Any]] = {}
LAYER_REGISTRY_LOCK = Lock()


def get_uploaded_layer(layer_id: str) -> Optional[Dict[str, Any]]:
    if not layer_id:
        return None
    with LAYER_REGISTRY_LOCK:
        return LAYER_REGISTRY.get(layer_id)


def register_layer_upload_routes(app: Flask) -> None:
    @app.route("/api/layers/upload", methods=["POST"])
    def upload_layer():
        uploaded_files = [file for file in request.files.getlist("file") if file and file.filename]
        if not uploaded_files:
            return jsonify({"error": "Upload a GeoPackage, zipped shapefile, shapefile sidecar set (.shp, .shx, .dbf), GeoJSON, or GeoTIFF."}), 400

        upload_items = _normalized_uploads(uploaded_files)
        replace_layer_id = str(request.form.get("replace_layer_id") or "").strip()
        try:
            upload_kind = _classify_uploads(upload_items)
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400

        layer_id = uuid.uuid4().hex
        work_dir = _runtime_dir("map_layers") / layer_id
        upload_dir = work_dir / "upload"
        upload_dir.mkdir(parents=True, exist_ok=True)

        try:
            original_name, upload_path = _save_upload_bundle(upload_items, upload_kind, upload_dir)
            if upload_kind == "raster":
                layer = _prepare_raster_layer(layer_id, original_name, upload_path, work_dir)
            else:
                layer = _prepare_vector_layer(layer_id, original_name, upload_path, work_dir)
        except ValueError as exc:
            shutil.rmtree(work_dir, ignore_errors=True)
            status = 501 if "GDAL" in str(exc) else 400
            return jsonify({"error": str(exc)}), status
        except Exception as exc:
            shutil.rmtree(work_dir, ignore_errors=True)
            return jsonify({"error": f"Could not prepare map layer: {exc}"}), 500

        replaced_layer = None
        with LAYER_REGISTRY_LOCK:
            LAYER_REGISTRY[layer_id] = layer
            if replace_layer_id and replace_layer_id != layer_id:
                replaced_layer = LAYER_REGISTRY.pop(replace_layer_id, None)

        if replaced_layer is not None:
            _cleanup_layer(replaced_layer)

        return jsonify(_public_layer_metadata(layer))

    @app.route("/api/layers/<layer_id>", methods=["DELETE"])
    def delete_layer(layer_id: str):
        with LAYER_REGISTRY_LOCK:
            layer = LAYER_REGISTRY.pop(layer_id, None)

        if layer is not None:
            _cleanup_layer(layer)
        return ("", 204)

    @app.route("/api/layers/<layer_id>/features")
    def layer_features(layer_id: str):
        with LAYER_REGISTRY_LOCK:
            layer = LAYER_REGISTRY.get(layer_id)

        if layer is None:
            return jsonify({"error": "Layer was not found. Upload it again."}), 404
        if layer.get("kind") != "vector":
            return jsonify({"error": "Feature queries are only available for vector layers."}), 400

        try:
            min_lon = float(request.args["min_lon"])
            min_lat = float(request.args["min_lat"])
            max_lon = float(request.args["max_lon"])
            max_lat = float(request.args["max_lat"])
            width = int(float(request.args.get("width", 1200)))
            height = int(float(request.args.get("height", 800)))
            zoom = float(request.args.get("zoom", 8))
        except (KeyError, TypeError, ValueError):
            return jsonify({"error": "Valid map bounds, width, height, and zoom are required."}), 400

        if not all(math.isfinite(value) for value in (min_lon, min_lat, max_lon, max_lat, zoom)):
            return jsonify({"error": "Map bounds must be finite numeric values."}), 400

        field = str(request.args.get("field", "")).strip()
        colormap = str(request.args.get("colormap", "viridis")).strip() or "viridis"

        try:
            payload = _query_vector_features(
                layer=layer,
                min_lon=min_lon,
                min_lat=min_lat,
                max_lon=max_lon,
                max_lat=max_lat,
                width=width,
                height=height,
                zoom=zoom,
                field=field,
                colormap=colormap,
            )
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400

        return jsonify(payload)

    @app.route("/api/layers/<layer_id>/tiles/<int:z>/<int:x>/<int:y>.png")
    def raster_tile(layer_id: str, z: int, x: int, y: int):
        with LAYER_REGISTRY_LOCK:
            layer = LAYER_REGISTRY.get(layer_id)

        if layer is None:
            return jsonify({"error": "Layer was not found. Upload it again."}), 404
        if layer.get("kind") != "raster":
            return jsonify({"error": "Tiles are only available for raster layers."}), 400

        tile_dir = Path(str(layer["tile_dir"]))
        tile_path = tile_dir / str(z) / str(x) / f"{y}.png"
        if not tile_path.is_file():
            return ("", 204)
        return send_from_directory(tile_path.parent, tile_path.name)

    @app.route("/api/layers/<layer_id>/preview.png")
    def raster_preview(layer_id: str):
        with LAYER_REGISTRY_LOCK:
            layer = LAYER_REGISTRY.get(layer_id)

        if layer is None:
            return jsonify({"error": "Layer was not found. Upload it again."}), 404
        if layer.get("kind") != "raster":
            return jsonify({"error": "Preview is only available for raster layers."}), 400

        preview_path = Path(str(layer.get("preview_path") or ""))
        if not preview_path.is_file():
            return jsonify({"error": "Raster preview was not found. Upload it again."}), 404
        return send_from_directory(preview_path.parent, preview_path.name)


def _runtime_dir(name: str) -> Path:
    runtime_dir = Path(tempfile.gettempdir()) / "data_augmentation_runtime" / name
    runtime_dir.mkdir(parents=True, exist_ok=True)
    return runtime_dir


def _sql_string(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _sql_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _save_upload_stream(uploaded_file: Any, output_path: Path) -> int:
    total = 0
    with output_path.open("wb") as handle:
        while True:
            chunk = uploaded_file.stream.read(1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_LAYER_UPLOAD_BYTES:
                raise ValueError("Layer upload is too large for this local session.")
            handle.write(chunk)
    return total


def _normalized_uploads(uploaded_files: List[Any]) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    for uploaded_file in uploaded_files:
        original_name = Path(uploaded_file.filename or "").name
        safe_name = secure_filename(original_name) or f"layer_{uuid.uuid4().hex}"
        extension = Path(safe_name).suffix.lower()
        items.append({
            "file": uploaded_file,
            "original_name": original_name,
            "safe_name": safe_name,
            "extension": extension,
            "stem": Path(safe_name).stem.casefold(),
        })
    return items


def _classify_uploads(upload_items: List[Dict[str, Any]]) -> str:
    invalid_extensions = sorted({item["extension"] for item in upload_items if item["extension"] not in VECTOR_EXTENSIONS and item["extension"] not in RASTER_EXTENSIONS})
    if invalid_extensions:
        raise ValueError("Supported layer files are .gpkg, .zip shapefile, .shp/.shx/.dbf sidecars, .geojson, .json, .tif, and .tiff.")

    extensions = {item["extension"] for item in upload_items}
    if extensions & RASTER_EXTENSIONS:
        if len(upload_items) != 1 or not extensions <= RASTER_EXTENSIONS:
            raise ValueError("GeoTIFF uploads must contain a single .tif or .tiff file.")
        return "raster"

    shapefile_extensions = SHAPEFILE_REQUIRED_EXTENSIONS | {".prj", ".cpg"}
    if extensions & shapefile_extensions:
        if ".zip" in extensions or ".gpkg" in extensions or ".geojson" in extensions or ".json" in extensions:
            raise ValueError("Upload a zipped shapefile, or select only the shapefile sidecar files together.")
        stems = {item["stem"] for item in upload_items}
        if len(stems) != 1:
            raise ValueError("All shapefile sidecar uploads must share the same base filename.")
        missing = sorted(SHAPEFILE_REQUIRED_EXTENSIONS - extensions)
        if missing:
            missing_text = ", ".join(missing)
            raise ValueError(f"A shapefile upload must include {missing_text} together with the matching sidecars.")
        return "vector"

    if len(upload_items) != 1:
        raise ValueError("Upload a single GeoPackage, zipped shapefile, or GeoJSON file, or select the full shapefile sidecar set.")
    return "vector"


def _save_upload_bundle(upload_items: List[Dict[str, Any]], upload_kind: str, upload_dir: Path) -> Tuple[str, Path]:
    total = 0
    saved_paths: List[Path] = []
    for item in upload_items:
        output_path = upload_dir / item["safe_name"]
        size = _save_upload_stream(item["file"], output_path)
        if size == 0:
            raise ValueError("Uploaded layer file is empty.")
        total += size
        if total > MAX_LAYER_UPLOAD_BYTES:
            raise ValueError("Layer upload is too large for this local session.")
        saved_paths.append(output_path)

    if upload_kind == "raster":
        return upload_items[0]["original_name"], saved_paths[0]

    shapefile_item = next((item for item in upload_items if item["extension"] == ".shp"), None)
    shapefile_path = next((path for path in saved_paths if path.suffix.lower() == ".shp"), None)
    if shapefile_item is not None and shapefile_path is not None:
        return shapefile_item["original_name"], shapefile_path
    return upload_items[0]["original_name"], saved_paths[0]


def _prepare_vector_layer(layer_id: str, original_name: str, upload_path: Path, work_dir: Path) -> Dict[str, Any]:
    vector_path = _resolve_vector_dataset(upload_path, work_dir)
    cache_path = work_dir / "layer.duckdb"
    source_info = _inspect_vector_source(vector_path)
    layer_name = source_info["layer_name"]
    geom_name = source_info["geometry_name"]
    source_crs = source_info["source_crs"]
    field_defs = source_info["fields"]

    con = duckdb.connect(str(cache_path))
    try:
        con.execute("LOAD spatial;")
        con.execute(f"SET temp_directory = {_sql_string(str(_runtime_dir('duckdb_temp').resolve()))};")

        source_sql = _st_read_sql(vector_path, layer_name)
        field_select_sql = _field_select_sql(field_defs)
        geom_expr = f"{_sql_identifier(geom_name)}"
        if source_crs and source_crs != "EPSG:4326":
            geom_expr = f"ST_Transform({_sql_identifier(geom_name)}, {_sql_string(source_crs)}, 'EPSG:4326', true)"

        con.execute(f"""
            CREATE TABLE features AS
            SELECT
                row_number() OVER () AS feature_id,
                {field_select_sql}
                CAST(ST_Force2D({geom_expr}) AS GEOMETRY) AS geom
            FROM {source_sql}
            WHERE {_sql_identifier(geom_name)} IS NOT NULL;
        """)
        con.execute("DELETE FROM features WHERE geom IS NULL;")
        con.execute("""
            ALTER TABLE features ADD COLUMN geometry_type VARCHAR;
            UPDATE features SET geometry_type = CAST(ST_GeometryType(geom) AS VARCHAR);
            ALTER TABLE features ADD COLUMN bbox_xmin DOUBLE;
            ALTER TABLE features ADD COLUMN bbox_ymin DOUBLE;
            ALTER TABLE features ADD COLUMN bbox_xmax DOUBLE;
            ALTER TABLE features ADD COLUMN bbox_ymax DOUBLE;
            UPDATE features SET
                bbox_xmin = ST_XMin(geom),
                bbox_ymin = ST_YMin(geom),
                bbox_xmax = ST_XMax(geom),
                bbox_ymax = ST_YMax(geom);
            CREATE INDEX features_geom_rtree ON features USING RTREE (geom);
        """)
        row = con.execute("""
            SELECT
                COUNT(*),
                MIN(bbox_xmin),
                MIN(bbox_ymin),
                MAX(bbox_xmax),
                MAX(bbox_ymax)
            FROM features;
        """).fetchone()
        field_summaries = _field_summaries(con, field_defs)
        con.execute("CHECKPOINT;")

        feature_count = int(row[0] or 0)
        if feature_count == 0:
            raise ValueError("The uploaded vector layer does not contain any readable geometries.")

        extent = {
            "min_lon": float(row[1]),
            "min_lat": float(row[2]),
            "max_lon": float(row[3]),
            "max_lat": float(row[4]),
        }
        if not _valid_lonlat_extent(extent):
            raise ValueError(
                "The uploaded vector layer could not be placed on the map. "
                "Check that the file has a readable CRS, or reproject it to EPSG:4326."
            )
        fields = [
            {
                **field,
                "numeric": field["name"] in field_summaries and field_summaries[field["name"]].get("numeric", False),
                "summary": field_summaries.get(field["name"], {}),
            }
            for field in field_defs
        ]

        return {
            "id": layer_id,
            "kind": "vector",
            "name": Path(original_name or upload_path.name).name,
            "cache_path": str(cache_path),
            "work_dir": str(work_dir),
            "connection": con,
            "connection_lock": Lock(),
            "feature_count": feature_count,
            "extent": extent,
            "fields": fields,
            "default_field": _default_display_field(fields),
            "source_crs": source_crs or "unknown",
            "created_at": time.time(),
        }
    except Exception:
        con.close()
        raise


def _valid_lonlat_extent(extent: Dict[str, float]) -> bool:
    values = [extent["min_lon"], extent["min_lat"], extent["max_lon"], extent["max_lat"]]
    if not all(math.isfinite(value) for value in values):
        return False
    return (
        -180 <= extent["min_lon"] <= 180
        and -180 <= extent["max_lon"] <= 180
        and -90 <= extent["min_lat"] <= 90
        and -90 <= extent["max_lat"] <= 90
    )


def _resolve_vector_dataset(upload_path: Path, work_dir: Path) -> Path:
    extension = upload_path.suffix.lower()
    if extension != ".zip":
        return upload_path

    extract_dir = work_dir / "unzipped"
    extract_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(upload_path) as archive:
        members = [member for member in archive.infolist() if not member.is_dir()]
        if len(members) > MAX_EXTRACTED_FILES:
            raise ValueError("The shapefile zip contains too many files.")

        for member in members:
            member_path = Path(member.filename)
            if member_path.is_absolute() or ".." in member_path.parts:
                raise ValueError("The shapefile zip contains an unsafe path.")
            target = extract_dir / member_path
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(member) as src, target.open("wb") as dst:
                shutil.copyfileobj(src, dst, length=1024 * 1024)

    shapefiles = sorted(extract_dir.rglob("*.shp"))
    if not shapefiles:
        raise ValueError("The zip file did not contain a .shp file.")
    return shapefiles[0]


def _inspect_vector_source(vector_path: Path) -> Dict[str, Any]:
    con = duckdb.connect()
    try:
        con.execute("LOAD spatial;")
        meta_row = con.execute(f"SELECT layers FROM ST_Read_Meta({_sql_string(str(vector_path.resolve()))});").fetchone()
        layers = meta_row[0] if meta_row else []
        if not layers:
            raise ValueError("No vector layers were found in the uploaded file.")

        layer_meta = next((layer for layer in layers if layer.get("geometry_fields")), layers[0])
        layer_name = str(layer_meta.get("name") or "")
        geometry_fields = layer_meta.get("geometry_fields") or []
        if not geometry_fields:
            raise ValueError("The selected layer does not contain geometries.")

        geom_meta = geometry_fields[0]
        geom_name = str(geom_meta.get("name") or "geom")
        crs_meta = geom_meta.get("crs") or {}
        auth_name = str(crs_meta.get("auth_name") or "").upper()
        auth_code = str(crs_meta.get("auth_code") or "")
        source_crs = f"{auth_name}:{auth_code}" if auth_name and auth_code else None

        describe_rows = con.execute(f"DESCRIBE SELECT * FROM {_st_read_sql(vector_path, layer_name)};").fetchall()
        geometry_names = {str(field.get("name") or "") for field in geometry_fields}
        fields = []
        meta_fields = {str(field.get("name")): field for field in layer_meta.get("fields") or []}
        for name, dtype, *_rest in describe_rows:
            name = str(name)
            if name in geometry_names or name == geom_name or name == "OGC_FID" or name.casefold() in RESERVED_FEATURE_COLUMNS:
                continue
            meta = meta_fields.get(name, {})
            fields.append({
                "name": name,
                "type": str(meta.get("type") or dtype),
            })
    finally:
        con.close()

    return {
        "layer_name": layer_name,
        "geometry_name": geom_name,
        "source_crs": source_crs,
        "fields": fields[:128],
    }


def _st_read_sql(vector_path: Path, layer_name: str) -> str:
    path_sql = _sql_string(str(vector_path.resolve()))
    if layer_name:
        return f"ST_Read({path_sql}, layer={_sql_string(layer_name)})"
    return f"ST_Read({path_sql})"


def _field_select_sql(fields: List[Dict[str, str]]) -> str:
    if not fields:
        return ""
    return "".join(f"{_sql_identifier(field['name'])},\n                " for field in fields)


def _field_summaries(con: duckdb.DuckDBPyConnection, fields: List[Dict[str, str]]) -> Dict[str, Dict[str, Any]]:
    summaries: Dict[str, Dict[str, Any]] = {}
    for field in fields[:64]:
        name = field["name"]
        dtype = field.get("type") or ""
        field_sql = _sql_identifier(name)
        is_numeric = dtype.casefold().replace(" ", "") in NUMERIC_FIELD_TYPES
        summary: Dict[str, Any] = {"numeric": is_numeric}

        if is_numeric:
            row = con.execute(f"""
                SELECT
                    MIN(TRY_CAST({field_sql} AS DOUBLE)),
                    MAX(TRY_CAST({field_sql} AS DOUBLE))
                FROM features
                WHERE TRY_CAST({field_sql} AS DOUBLE) IS NOT NULL;
            """).fetchone()
            summary["min"] = None if row[0] is None else float(row[0])
            summary["max"] = None if row[1] is None else float(row[1])
        else:
            rows = con.execute(f"""
                SELECT CAST({field_sql} AS VARCHAR) AS value, COUNT(*) AS count
                FROM features
                WHERE {field_sql} IS NOT NULL
                    AND CAST({field_sql} AS VARCHAR) <> ''
                GROUP BY value
                ORDER BY count DESC, value
                LIMIT 24;
            """).fetchall()
            summary["categories"] = [{"value": str(value), "count": int(count)} for value, count in rows]

        summaries[name] = summary
    return summaries


def _default_display_field(fields: List[Dict[str, Any]]) -> str:
    if not fields:
        return ""
    preferred = ("hazard", "risk", "score", "depth", "intensity", "class", "name", "id")
    normalized = {field["name"]: field["name"].casefold() for field in fields}
    for token in preferred:
        for name, lowered in normalized.items():
            if token in lowered:
                return name
    numeric = next((field["name"] for field in fields if field.get("numeric")), "")
    return numeric or fields[0]["name"]


def _query_vector_features(
    layer: Dict[str, Any],
    min_lon: float,
    min_lat: float,
    max_lon: float,
    max_lat: float,
    width: int,
    height: int,
    zoom: float,
    field: str,
    colormap: str,
) -> Dict[str, Any]:
    min_lon, max_lon = sorted((max(-180.0, min_lon), min(180.0, max_lon)))
    min_lat, max_lat = sorted((max(-90.0, min_lat), min(90.0, max_lat)))
    if min_lon >= max_lon or min_lat >= max_lat:
        return _empty_feature_payload()

    fields = {field_def["name"]: field_def for field_def in layer.get("fields", [])}
    selected_field = field if field in fields else ""
    selected_field_sql = _sql_identifier(selected_field) if selected_field else "NULL"
    tolerance = _simplify_tolerance(min_lon, min_lat, max_lon, max_lat, width, height, zoom)
    min_feature_size = tolerance * 3
    con = layer.get("connection")
    connection_lock = layer.get("connection_lock")
    if con is None or connection_lock is None:
        raise ValueError("The uploaded vector layer connection is no longer available. Upload it again.")

    with connection_lock:
        if layer.get("connection") is not con:
            raise ValueError("The uploaded vector layer connection is no longer available. Upload it again.")
        count_row = con.execute("""
            SELECT COUNT(*) FROM features
            WHERE ST_Intersects(geom, ST_MakeEnvelope(?, ?, ?, ?))
                AND (bbox_xmax - bbox_xmin) + (bbox_ymax - bbox_ymin) >= ?
        """, [min_lon, min_lat, max_lon, max_lat, min_feature_size]).fetchone()
        visible_count = int(count_row[0]) if count_row else 0

        rows = con.execute(f"""
            SELECT
                feature_id,
                geometry_type,
                display_value,
                numeric_value,
                ST_AsGeoJSON(
                    CASE
                        WHEN ? > 0 THEN ST_Simplify(geom, ?)
                        ELSE geom
                    END
                ) AS geometry
            FROM (
                SELECT
                    feature_id,
                    geometry_type,
                    CAST({selected_field_sql} AS VARCHAR) AS display_value,
                    TRY_CAST({selected_field_sql} AS DOUBLE) AS numeric_value,
                    geom
                FROM features
                WHERE ST_Intersects(geom, ST_MakeEnvelope(?, ?, ?, ?))
                    AND (bbox_xmax - bbox_xmin) + (bbox_ymax - bbox_ymin) >= ?
                ORDER BY feature_id
                LIMIT ?
            ) sub
        """, [
            tolerance,
            tolerance,
            min_lon,
            min_lat,
            max_lon,
            max_lat,
            min_feature_size,
            MAX_VECTOR_FEATURES,
        ]).fetchall()

    summary = fields.get(selected_field, {}).get("summary", {}) if selected_field else {}
    features = []
    for feature_id, geometry_type, display_value, numeric_value, geometry_json in rows:
        geometry = json.loads(str(geometry_json)) if geometry_json else None
        if not geometry:
            continue
        color = _feature_color(display_value, numeric_value, summary, colormap)
        features.append({
            "type": "Feature",
            "geometry": geometry,
            "properties": {
                "feature_id": int(feature_id),
                "geometry_type": str(geometry_type),
                "display_field": selected_field,
                "display_value": "" if display_value is None else str(display_value),
                "__color": color,
            },
        })

    return {
        "type": "FeatureCollection",
        "features": features,
        "visible_count": visible_count,
        "returned_count": len(features),
        "truncated": visible_count > len(features),
        "field": selected_field,
    }


def _empty_feature_payload() -> Dict[str, Any]:
    return {
        "type": "FeatureCollection",
        "features": [],
        "visible_count": 0,
        "returned_count": 0,
        "truncated": False,
    }


def _simplify_tolerance(
    min_lon: float,
    min_lat: float,
    max_lon: float,
    max_lat: float,
    width: int,
    height: int,
    zoom: float,
) -> float:
    safe_width = max(320, min(3840, int(width or 1200)))
    safe_height = max(240, min(2160, int(height or 800)))
    lon_per_pixel = abs(max_lon - min_lon) / safe_width
    lat_per_pixel = abs(max_lat - min_lat) / safe_height
    tolerance = max(lon_per_pixel, lat_per_pixel) * 0.65
    if zoom >= 16:
        return 0.0
    return max(0.0, tolerance)


def _feature_color(display_value: Any, numeric_value: Any, summary: Dict[str, Any], colormap: str) -> str:
    palette = COLOR_MAPS.get(colormap) or COLOR_MAPS["viridis"]
    if summary.get("numeric") and numeric_value is not None:
        min_value = summary.get("min")
        max_value = summary.get("max")
        try:
            value = float(numeric_value)
            lo = float(min_value)
            hi = float(max_value)
        except (TypeError, ValueError):
            return palette[0]
        ratio = 0.5 if hi <= lo else (value - lo) / (hi - lo)
        return _interpolate_palette(palette, max(0.0, min(1.0, ratio)))

    if display_value is None or str(display_value) == "":
        return "#64748b"
    categorical_palette = COLOR_MAPS["categorical"] if colormap != "hazard" else COLOR_MAPS["hazard"]
    digest = hashlib.sha1(str(display_value).encode("utf-8")).hexdigest()
    return categorical_palette[int(digest[:8], 16) % len(categorical_palette)]


def _interpolate_palette(palette: List[str], ratio: float) -> str:
    if ratio <= 0:
        return palette[0]
    if ratio >= 1:
        return palette[-1]
    position = ratio * (len(palette) - 1)
    index = int(math.floor(position))
    frac = position - index
    return _mix_hex(palette[index], palette[index + 1], frac)


def _mix_hex(left: str, right: str, ratio: float) -> str:
    l_rgb = _hex_to_rgb(left)
    r_rgb = _hex_to_rgb(right)
    mixed = [round(l_rgb[i] + (r_rgb[i] - l_rgb[i]) * ratio) for i in range(3)]
    return "#" + "".join(f"{value:02x}" for value in mixed)


def _hex_to_rgb(value: str) -> Tuple[int, int, int]:
    value = value.lstrip("#")
    return int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16)


def _prepare_raster_layer(layer_id: str, original_name: str, upload_path: Path, work_dir: Path) -> Dict[str, Any]:
    gdal_tools = _find_gdal_tools()
    if not gdal_tools:
        raise ValueError(
            "Raster upload needs GDAL tools bundled with this app. "
            "Ask the app distributor to include the GDAL folder in the Windows build."
        )

    normalized_path = _normalize_raster_for_tiling(upload_path, work_dir, gdal_tools)
    info = _run_json_command([gdal_tools["gdalinfo"], "-json", "-mm", str(normalized_path)], env=gdal_tools["env"])
    extent = _raster_extent(info)
    if not extent:
        raise ValueError("This raster does not expose a usable WGS84 extent.")
    tile_dir, min_zoom, max_zoom = _generate_raster_tiles(normalized_path, info, work_dir, gdal_tools)
    preview_path = _prepare_raster_preview_image(normalized_path, info, work_dir, gdal_tools)

    return {
        "id": layer_id,
        "kind": "raster",
        "name": Path(original_name or upload_path.name).name,
        "tile_url": f"/api/layers/{layer_id}/tiles/{{z}}/{{x}}/{{y}}.png",
        "tile_dir": str(tile_dir),
        "min_zoom": min_zoom,
        "max_zoom": max_zoom,
        "raster_path": str(normalized_path),
        "preview_path": str(preview_path),
        "source_upload_path": str(upload_path),
        "work_dir": str(work_dir),
        "extent": extent,
        "crs": _raster_crs_label(info),
        "bands": _raster_bands(info),
        "default_band": 1,
        "fields": [],
        "default_field": "",
        "created_at": time.time(),
    }


def _generate_raster_tiles(
    raster_path: Path, info: Dict[str, Any], work_dir: Path, gdal_tools: Dict[str, Any]
) -> Tuple[Path, int, int]:
    """Create a Byte-scaled TIF and generate XYZ map tiles via gdal2tiles."""
    display_tif = work_dir / "display_tiles.tif"
    tile_dir = work_dir / "tiles"

    command = [
        gdal_tools["gdal_translate"],
        "-of", "GTiff",
        "-ot", "Byte",
        "-co", "TILED=YES",
        "-co", "COMPRESS=DEFLATE",
    ]
    command.extend(_display_scale_arguments(info))
    nodata = _first_band_nodata(info)
    if nodata is not None:
        command.extend(["-a_nodata", "0"])
    command.extend([str(raster_path), str(display_tif)])
    _run_command(command, env=gdal_tools["env"], label="gdal_translate tile source")

    _run_command([
        gdal_tools["gdal2tiles"],
        "--xyz",
        "-w", "none",
        "-r", "average",
        str(display_tif),
        str(tile_dir),
    ], env=gdal_tools["env"], label="gdal2tiles")

    zoom_levels = sorted(
        int(d.name) for d in tile_dir.iterdir()
        if d.is_dir() and d.name.isdigit()
    )
    min_zoom = zoom_levels[0] if zoom_levels else 0
    max_zoom = zoom_levels[-1] if zoom_levels else 18
    return tile_dir, min_zoom, max_zoom


def _normalize_raster_for_tiling(upload_path: Path, work_dir: Path, gdal_tools: Dict[str, Any]) -> Path:
    info = _run_json_command([gdal_tools["gdalinfo"], "-json", str(upload_path)], env=gdal_tools["env"])
    if _raster_extent(info) and _has_real_georeference(info):
        return upload_path

    sidecar_extent = _extent_from_world_file(upload_path, info)
    if sidecar_extent is None:
        raise ValueError(
            "This raster is not georeferenced. Upload a true GeoTIFF with embedded CRS/geotransform. "
            "A plain TIFF plus .tfw sidecar will not work from the browser upload."
        )

    work_dir.mkdir(parents=True, exist_ok=True)
    normalized_path = work_dir / f"{upload_path.stem}_georeferenced.tif"
    _run_command([
        gdal_tools["gdal_translate"],
        "-a_srs",
        "EPSG:4326",
        "-a_ullr",
        str(sidecar_extent["min_lon"]),
        str(sidecar_extent["max_lat"]),
        str(sidecar_extent["max_lon"]),
        str(sidecar_extent["min_lat"]),
        str(upload_path),
        str(normalized_path),
    ], env=gdal_tools["env"], label="gdal_translate")
    return normalized_path


def _prepare_raster_preview_image(
    raster_path: Path,
    info: Dict[str, Any],
    work_dir: Path,
    gdal_tools: Dict[str, Any],
) -> Path:
    display_tif = work_dir / f"{raster_path.stem}_display_byte.tif"
    preview_png = work_dir / "preview.png"
    size = info.get("size") or []
    width = int(size[0] or RASTER_PREVIEW_MAX_SIZE) if len(size) >= 1 else RASTER_PREVIEW_MAX_SIZE
    height = int(size[1] or RASTER_PREVIEW_MAX_SIZE) if len(size) >= 2 else RASTER_PREVIEW_MAX_SIZE
    scale = min(1.0, RASTER_PREVIEW_MAX_SIZE / max(1, width), RASTER_PREVIEW_MAX_SIZE / max(1, height))
    out_width = max(1, int(width * scale))
    out_height = max(1, int(height * scale))

    command = [
        gdal_tools["gdal_translate"],
        "-of",
        "GTiff",
        "-ot",
        "Byte",
        "-outsize",
        str(out_width),
        str(out_height),
        "-co",
        "TILED=YES",
        "-co",
        "COMPRESS=DEFLATE",
    ]
    command.extend(_display_scale_arguments(info))
    nodata = _first_band_nodata(info)
    if nodata is not None:
        command.extend(["-a_nodata", "0"])
    command.extend([str(raster_path), str(display_tif)])
    _run_command(command, env=gdal_tools["env"], label="gdal_translate display raster")

    _run_command([
        gdal_tools["gdal_translate"],
        "-of",
        "PNG",
        str(display_tif),
        str(preview_png),
    ], env=gdal_tools["env"], label="gdal_translate raster preview")
    return preview_png


def _image_coordinates(extent: Dict[str, float]) -> List[List[float]]:
    return [
        [float(extent["min_lon"]), float(extent["max_lat"])],
        [float(extent["max_lon"]), float(extent["max_lat"])],
        [float(extent["max_lon"]), float(extent["min_lat"])],
        [float(extent["min_lon"]), float(extent["min_lat"])],
    ]


def _first_band_nodata(info: Dict[str, Any]) -> Optional[Any]:
    for band in info.get("bands") or []:
        if band.get("noDataValue") is not None:
            return band.get("noDataValue")
    return None


def _display_scale_arguments(info: Dict[str, Any]) -> List[str]:
    """Reserve display value zero for transparency, including constant bands."""
    arguments: List[str] = []
    for position, band in enumerate(info.get("bands") or [], start=1):
        minimum = band.get("computedMin", band.get("minimum"))
        maximum = band.get("computedMax", band.get("maximum"))
        try:
            minimum = float(minimum)
            maximum = float(maximum)
        except (TypeError, ValueError):
            return ["-scale"]
        if not math.isfinite(minimum) or not math.isfinite(maximum):
            return ["-scale"]
        arguments.extend([
            f"-scale_{position}",
            str(minimum),
            str(maximum),
            "1",
            "255",
        ])
    return arguments or ["-scale"]


def _has_real_georeference(info: Dict[str, Any]) -> bool:
    coordinate_system = info.get("coordinateSystem") or {}
    if coordinate_system:
        return True
    if info.get("gcps"):
        return True
    return False


def _extent_from_world_file(raster_path: Path, info: Dict[str, Any]) -> Optional[Dict[str, float]]:
    candidates = [
        raster_path.with_suffix(".tfw"),
        raster_path.with_suffix(".tifw"),
        raster_path.with_suffix(".wld"),
    ]
    world_file = next((path for path in candidates if path.is_file()), None)
    if world_file is None:
        return None

    try:
        values = [float(line.strip()) for line in world_file.read_text(encoding="utf-8").splitlines() if line.strip()]
    except (OSError, ValueError):
        return None
    if len(values) < 6:
        return None

    pixel_x, rot_y, rot_x, pixel_y, center_x, center_y = values[:6]
    if abs(rot_x) > 1e-12 or abs(rot_y) > 1e-12:
        return None

    size = info.get("size") or []
    if len(size) < 2:
        return None

    width, height = int(size[0]), int(size[1])
    min_lon = center_x - pixel_x / 2
    max_lat = center_y - pixel_y / 2
    max_lon = min_lon + pixel_x * width
    min_lat = max_lat + pixel_y * height
    return {
        "min_lon": min(min_lon, max_lon),
        "min_lat": min(min_lat, max_lat),
        "max_lon": max(min_lon, max_lon),
        "max_lat": max(min_lat, max_lat),
    }


def _find_gdal_tools() -> Optional[Dict[str, Any]]:
    roots = _candidate_gdal_roots()
    for root in roots:
        tools = _tools_from_gdal_root(root)
        if tools:
            return tools

    gdalinfo = shutil.which("gdalinfo") or shutil.which("gdalinfo.exe")
    gdal2tiles = (
        shutil.which("gdal2tiles.py")
        or shutil.which("gdal2tiles")
        or shutil.which("gdal2tiles.exe")
        or shutil.which("gdal2tiles.bat")
    )
    gdal_translate = shutil.which("gdal_translate") or shutil.which("gdal_translate.exe")
    if gdalinfo and gdal2tiles and gdal_translate:
        return {
            "gdalinfo": gdalinfo,
            "gdal2tiles": gdal2tiles,
            "gdal_translate": gdal_translate,
            "env": os.environ.copy(),
        }

    return None


def _candidate_gdal_roots() -> List[Path]:
    roots: List[Path] = []
    env_root = os.environ.get("DATA_AUGMENTATION_GDAL_DIR", "").strip()
    if env_root:
        roots.append(Path(env_root))

    if getattr(sys, "frozen", False):
        exe_dir = Path(sys.executable).resolve().parent
        roots.extend([
            exe_dir / "gdal",
            exe_dir / "_internal" / "gdal",
        ])

        meipass = getattr(sys, "_MEIPASS", "")
        if meipass:
            roots.append(Path(meipass) / "gdal")

    app_dir = Path(__file__).resolve().parent
    roots.extend([
        app_dir / "gdal",
        app_dir / "vendor" / "gdal",
        Path.cwd() / "gdal",
        Path.cwd() / "vendor" / "gdal",
    ])

    return list(dict.fromkeys(root for root in roots if root))


def _tools_from_gdal_root(root: Path) -> Optional[Dict[str, Any]]:
    if not root.exists():
        return None

    bin_dirs = [
        root,
        root / "bin",
        root / "apps",
        root / "Library" / "bin",
    ]
    gdalinfo = _first_existing_tool(bin_dirs, ["gdalinfo.exe", "gdalinfo"])
    gdal_translate = _first_existing_tool(bin_dirs, ["gdal_translate.exe", "gdal_translate"])
    gdal2tiles = _first_existing_tool(
        bin_dirs,
        ["gdal2tiles.exe", "gdal2tiles.bat", "gdal2tiles.py", "gdal2tiles"],
    )
    if not gdalinfo or not gdal2tiles or not gdal_translate:
        return None

    env = os.environ.copy()
    path_parts = [str(path) for path in bin_dirs if path.is_dir()]
    env["PATH"] = os.pathsep.join([*path_parts, env.get("PATH", "")])

    gdal_data = _first_existing_dir([
        root / "share" / "gdal",
        root / "data",
        root / "Library" / "share" / "gdal",
    ])
    proj_lib = _first_existing_dir([
        root / "share" / "proj",
        root / "projlib",
        root / "Library" / "share" / "proj",
    ])
    if gdal_data:
        env.setdefault("GDAL_DATA", str(gdal_data))
    if proj_lib:
        env.setdefault("PROJ_LIB", str(proj_lib))

    return {
        "gdalinfo": str(gdalinfo),
        "gdal2tiles": str(gdal2tiles),
        "gdal_translate": str(gdal_translate),
        "env": env,
    }


def _first_existing_tool(directories: List[Path], names: List[str]) -> Optional[Path]:
    for directory in directories:
        for name in names:
            path = directory / name
            if path.is_file():
                return path
    return None


def _first_existing_dir(paths: List[Path]) -> Optional[Path]:
    return next((path for path in paths if path.is_dir()), None)


def _run_json_command(command: List[str], env: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    result = _run_command(command, env=env, label=Path(command[0]).name)
    return json.loads(result.stdout)


def _run_command(command: List[str], env: Optional[Dict[str, str]] = None, label: str = "command") -> subprocess.CompletedProcess:
    try:
        return subprocess.run(command, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
    except subprocess.CalledProcessError as exc:
        details = (exc.stderr or exc.stdout or str(exc)).strip()
        if len(details) > 1400:
            details = details[-1400:]
        raise RuntimeError(f"{label} failed: {details}") from exc


def _raster_extent(info: Dict[str, Any]) -> Optional[Dict[str, float]]:
    coordinates = (((info.get("wgs84Extent") or {}).get("coordinates") or [[]])[0] or [])
    if not coordinates:
        return None
    lons = [float(point[0]) for point in coordinates]
    lats = [float(point[1]) for point in coordinates]
    return {
        "min_lon": min(lons),
        "min_lat": min(lats),
        "max_lon": max(lons),
        "max_lat": max(lats),
    }


def _raster_crs_label(info: Dict[str, Any]) -> str:
    coordinate_system = info.get("coordinateSystem") or {}
    wkt = str(coordinate_system.get("wkt") or "")
    authority = coordinate_system.get("dataAxisToSRSAxisMapping")
    if "EPSG" in wkt:
        for token in wkt.replace("[", ",").replace("]", ",").replace('"', "").split(","):
            cleaned = token.strip()
            if cleaned.isdigit():
                return f"EPSG:{cleaned}"
    if authority:
        return "projected"
    return "unknown"


def _raster_bands(info: Dict[str, Any]) -> List[Dict[str, Any]]:
    bands = []
    for band in info.get("bands") or []:
        band_number = int(band.get("band") or len(bands) + 1)
        description = str(band.get("description") or "").strip()
        bands.append({
            "index": band_number,
            "name": description or f"Band {band_number}",
            "type": str(band.get("type") or ""),
            "nodata": band.get("noDataValue"),
        })
    return bands or [{"index": 1, "name": "Band 1", "type": "", "nodata": None}]


def _public_layer_metadata(layer: Dict[str, Any]) -> Dict[str, Any]:
    return {
        key: value
        for key, value in layer.items()
        if key not in {
            "cache_path",
            "tile_dir",
            "raster_path",
            "preview_path",
            "source_upload_path",
            "work_dir",
            "connection",
            "connection_lock",
        }
    }


def _cleanup_layer(layer: Dict[str, Any]) -> None:
    connection_lock = layer.get("connection_lock")
    if connection_lock is not None:
        with connection_lock:
            connection = layer.pop("connection", None)
            if connection is not None:
                try:
                    connection.close()
                except Exception:
                    pass
    else:
        connection = layer.pop("connection", None)
        if connection is not None:
            try:
                connection.close()
            except Exception:
                pass

    work_dir_value = str(layer.get("work_dir") or "").strip()
    if not work_dir_value:
        return
    map_layers_dir = _runtime_dir("map_layers").resolve()
    try:
        resolved_work_dir = Path(work_dir_value).resolve()
        resolved_work_dir.relative_to(map_layers_dir)
    except (OSError, ValueError):
        return
    shutil.rmtree(resolved_work_dir, ignore_errors=True)
