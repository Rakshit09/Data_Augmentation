import argparse
import csv
import json
import os
import time
import urllib.parse
import urllib.request
import uuid
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from threading import Lock, Thread
from typing import Any, Dict, List, Optional

import pandas as pd
from flask import Flask, jsonify, render_template, request, send_file
from jinja2 import Environment, FileSystemLoader, StrictUndefined
from werkzeug.utils import secure_filename

from snowflake_loader import (
    DEFAULT_BUILDINGS_TABLE,
    DEFAULT_RAW_TABLE,
    get_snowflake_connection,
    quote_fqn,
    quote_identifier,
    sql_literal,
)


DEFAULT_PARQUET = "etl_output/buildings_de_cleaned.parquet"
DEFAULT_NEAREST_RADIUS_M = 50.0
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


def json_safe(value: Any) -> Any:
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return value


def detect_csv_encoding(csv_path: Path) -> str:
    for encoding in ("utf-8-sig", "utf-8", "cp1252", "latin1"):
        try:
            pd.read_csv(csv_path, nrows=5, encoding=encoding)
            return encoding
        except UnicodeDecodeError:
            continue
    return "latin1"


def render_sql(template_name: str, **context: Any) -> str:
    env = Environment(
        loader=FileSystemLoader(Path(__file__).resolve().parent / "sql"),
        undefined=StrictUndefined,
        autoescape=False,
        trim_blocks=True,
        lstrip_blocks=True,
    )
    return env.get_template(template_name).render(**context)


def building_select(alias: str = "b", prefix: str = "") -> str:
    return ",\n        ".join(
        f"{alias}.{column} AS {quote_identifier(prefix + column)}"
        for column in BUILDING_COLUMNS
    )


def coalesced_building_select() -> str:
    return ",\n    ".join(
        f"COALESCE(i.{quote_identifier(column)}, n.{quote_identifier(column)}) "
        f"AS {quote_identifier('building_' + column)}"
        for column in BUILDING_COLUMNS
    )


def exposure_select(columns: List[str], alias: str = "") -> str:
    prefix = f"{alias}." if alias else ""
    return ",\n        ".join(
        f"{prefix}{quote_identifier(column)} AS {quote_identifier(column)}"
        for column in columns
    )


def prefixed_building_columns() -> List[str]:
    return [f"building_{column}" for column in BUILDING_COLUMNS]


def empty_summary() -> Dict[str, Any]:
    return {
        "total_rows": 0,
        "valid_coordinate_rows": 0,
        "inside_polygon_matches": 0,
        "nearest_matches": 0,
        "no_matches": 0,
        "nearest_distance_total_m": 0.0,
        "nearest_distance_count": 0,
        "detailed_occupancy": {},
        "occupancy_group": {},
    }


def add_distribution(target: Dict[str, int], value: Any) -> None:
    if value is None or value == "":
        return
    key = str(value)
    target[key] = target.get(key, 0) + 1


def distribution_to_rows(distribution: Dict[str, int]) -> List[Dict[str, Any]]:
    return [
        {"name": name, "count": count}
        for name, count in sorted(distribution.items(), key=lambda item: item[1], reverse=True)
    ]


def finalize_summary(summary: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "total_rows": int(summary["total_rows"]),
        "valid_coordinate_rows": int(summary["valid_coordinate_rows"]),
        "inside_polygon_matches": int(summary["inside_polygon_matches"]),
        "nearest_matches": int(summary["nearest_matches"]),
        "no_matches": int(summary["no_matches"]),
        "average_nearest_distance_m": (
            summary["nearest_distance_total_m"] / summary["nearest_distance_count"]
            if summary["nearest_distance_count"]
            else None
        ),
        "detailed_occupancy": distribution_to_rows(summary["detailed_occupancy"]),
        "occupancy_raw": distribution_to_rows(summary["detailed_occupancy"]),
        "occupancy_group": distribution_to_rows(summary["occupancy_group"]),
    }


def update_summary_from_row(summary: Dict[str, Any], header: List[str], row: tuple) -> None:
    values = dict(zip(header, row))
    match_type = values.get("building_match_type")
    distance_m = values.get("building_distance_m")

    summary["total_rows"] += 1
    if values.get("coordinate_valid"):
        summary["valid_coordinate_rows"] += 1
    if match_type == "inside_polygon":
        summary["inside_polygon_matches"] += 1
    elif match_type in {"nearest_polygon", "nearest_centroid"}:
        summary["nearest_matches"] += 1
        if distance_m is not None:
            summary["nearest_distance_total_m"] += float(distance_m)
            summary["nearest_distance_count"] += 1
    elif match_type == "none":
        summary["no_matches"] += 1

    add_distribution(summary["detailed_occupancy"], values.get("building_occupancy_raw"))
    add_distribution(summary["occupancy_group"], values.get("building_occupancy_group"))


def fetch_geocoder_json(url: str, user_agent: str) -> Any:
    req = urllib.request.Request(
        url,
        headers={"User-Agent": user_agent, "Accept": "application/json"},
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
    params = urllib.parse.urlencode({"q": query, "limit": 5})
    raw_results = fetch_geocoder_json(f"https://photon.komoot.io/api/?{params}", user_agent)
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
        if label:
            results.append({
                "label": label,
                "lon": float(coordinates[0]),
                "lat": float(coordinates[1]),
                "type": properties.get("osm_value"),
                "provider": "Photon",
            })

    return results


def preview_csv(csv_path: Path) -> tuple[List[str], List[Dict[str, Any]]]:
    encoding = detect_csv_encoding(csv_path)
    frame = pd.read_csv(csv_path, nrows=10, encoding=encoding)
    columns = list(frame.columns)
    rows = [
        {column: json_safe(value) for column, value in row.items()}
        for row in frame.to_dict(orient="records")
    ]
    return columns, rows


def find_upload(upload_dir: Path, upload_id: str) -> Optional[Path]:
    matches = list(upload_dir.glob(f"{upload_id}_*.csv"))
    return matches[0] if matches else None


def create_temp_exposure_table(cur, csv_path: Path, columns: List[str], table_name: str) -> str:
    table_sql = quote_identifier(table_name)
    stage_sql = quote_identifier(f"{table_name}_STAGE")
    file_format_sql = quote_identifier(f"{table_name}_CSV_FORMAT")
    column_defs = ", ".join(f"{quote_identifier(column)} VARCHAR" for column in columns)
    file_uri = "file://" + csv_path.resolve().as_posix()

    cur.execute(f"CREATE TEMP TABLE {table_sql} ({column_defs})")
    cur.execute(
        f"""
        CREATE TEMP FILE FORMAT {file_format_sql}
        TYPE = CSV
        SKIP_HEADER = 1
        FIELD_OPTIONALLY_ENCLOSED_BY = '"'
        EMPTY_FIELD_AS_NULL = TRUE
        """
    )
    cur.execute(f"CREATE TEMP STAGE {stage_sql} FILE_FORMAT = {file_format_sql}")
    cur.execute(f"PUT {sql_literal(file_uri)} @{stage_sql} AUTO_COMPRESS = TRUE OVERWRITE = TRUE")
    cur.execute(
        f"""
        COPY INTO {table_sql}
        FROM @{stage_sql}
        FILE_FORMAT = (FORMAT_NAME = {file_format_sql})
        ON_ERROR = ABORT_STATEMENT
        """
    )
    return table_sql


def write_enrichment_result(cur, output_path: Path, progress_callback=None) -> Dict[str, Any]:
    header = [desc[0] for desc in cur.description]
    summary = empty_summary()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    written = 0

    with output_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(header)

        while True:
            rows = cur.fetchmany(10000)
            if not rows:
                break
            for row in rows:
                safe_row = [json_safe(value) for value in row]
                writer.writerow(safe_row)
                update_summary_from_row(summary, header, row)
                written += 1

            if progress_callback:
                progress_callback(f"Writing enriched CSV: {written:,} rows", 90)

    return finalize_summary(summary)


def enrich_exposure_csv(
    csv_path: Path,
    output_path: Path,
    lat_col: str,
    lon_col: str,
    mode: str,
    max_distance_m: float,
    buildings_table: str,
    progress_callback=None,
) -> Dict[str, Any]:
    if progress_callback:
        progress_callback("Inspecting CSV columns", 10)

    columns, _ = preview_csv(csv_path)
    if lat_col not in columns or lon_col not in columns:
        raise ValueError("Selected latitude/longitude columns were not found in the CSV.")

    temp_table_name = f"EXPOSURE_{uuid.uuid4().hex.upper()}"
    if progress_callback:
        progress_callback("Uploading exposure CSV to Snowflake", 25)

    with get_snowflake_connection() as con:
        cur = con.cursor()
        try:
            exposure_table = create_temp_exposure_table(cur, csv_path, columns, temp_table_name)
            if progress_callback:
                progress_callback("Running Snowflake spatial enrichment", 55)

            sql = render_sql(
                "enrichment.sql",
                exposure_table=exposure_table,
                buildings_table=quote_fqn(buildings_table),
                exposure_columns=exposure_select(columns),
                output_exposure_columns=exposure_select(columns, "e"),
                raw_building_columns=building_select("b"),
                coalesced_building_columns=coalesced_building_select(),
                lat_column=quote_identifier(lat_col),
                lon_column=quote_identifier(lon_col),
                radius_m=float(max_distance_m),
                mode=mode,
            )
            cur.execute(sql)
            return write_enrichment_result(cur, output_path, progress_callback)
        finally:
            cur.close()


def find_building(buildings_table: str, lon: float, lat: float, nearest_radius_m: float) -> Optional[Dict[str, Any]]:
    sql = render_sql(
        "lookup.sql",
        buildings_table=quote_fqn(buildings_table),
        building_columns=building_select("b"),
    )
    params = (lon, lat, nearest_radius_m, lon, lat)

    with get_snowflake_connection() as con:
        cur = con.cursor()
        try:
            cur.execute(sql, params)
            row = cur.fetchone()
            if row is None:
                return None
            columns = [desc[0] for desc in cur.description]
        finally:
            cur.close()

    return row_to_response(dict(zip(columns, row)))


def row_to_response(data: Dict[str, Any]) -> Dict[str, Any]:
    geometry_value = data.pop("geometry", None)
    if isinstance(geometry_value, str):
        geometry = json.loads(geometry_value)
    else:
        geometry = geometry_value

    building = {
        key: json_safe(value)
        for key, value in data.items()
        if key not in {"match_type", "distance_m", "confidence"}
    }
    building["geometry"] = geometry

    return {
        "match_type": data["match_type"],
        "distance_m": json_safe(data["distance_m"]),
        "confidence": data["confidence"],
        "building": building,
    }


def create_app(
    buildings_table: str = DEFAULT_BUILDINGS_TABLE,
    raw_table: str = DEFAULT_RAW_TABLE,
    nearest_radius_m: float = DEFAULT_NEAREST_RADIUS_M,
) -> Flask:
    app = Flask(__name__)
    app.config["PARQUET_PATH"] = os.getenv("OBM_PARQUET_PATH", DEFAULT_PARQUET)
    app.config["BUILDINGS_TABLE"] = buildings_table
    app.config["RAW_TABLE"] = raw_table
    app.config["NEAREST_RADIUS_M"] = float(nearest_radius_m)
    app.config["GEOCODER_USER_AGENT"] = "OBMBuildingLookup/0.2 snowflake"
    app.config["UPLOAD_DIR"] = "etl_output/app_uploads"
    app.config["RESULT_DIR"] = "etl_output/app_results"
    Path(app.config["UPLOAD_DIR"]).mkdir(parents=True, exist_ok=True)
    Path(app.config["RESULT_DIR"]).mkdir(parents=True, exist_ok=True)

    geocode_cache: Dict[str, Any] = {}
    last_geocode_at = [0.0]
    jobs: Dict[str, Dict[str, Any]] = {}
    jobs_lock = Lock()
    etl_jobs: Dict[str, Dict[str, Any]] = {}
    etl_jobs_lock = Lock()

    def set_job(job_id: str, **updates: Any) -> None:
        with jobs_lock:
            jobs.setdefault(job_id, {}).update(updates)

    def set_etl_job(job_id: str, **updates: Any) -> None:
        with etl_jobs_lock:
            etl_jobs.setdefault(job_id, {}).update(updates)

    @app.route("/")
    def index():
        return render_template("index.html")

    @app.route("/api/health")
    def health():
        try:
            with get_snowflake_connection() as con:
                cur = con.cursor()
                cur.execute(f"SELECT COUNT(*) FROM {quote_fqn(app.config['BUILDINGS_TABLE'])}")
                row_count = int(cur.fetchone()[0])
                cur.close()
            return jsonify({
                "ok": True,
                "backend": "snowflake",
                "buildings_table": app.config["BUILDINGS_TABLE"],
                "raw_table": app.config["RAW_TABLE"],
                "row_count": row_count,
            })
        except Exception as exc:
            return jsonify({
                "ok": False,
                "backend": "snowflake",
                "buildings_table": app.config["BUILDINGS_TABLE"],
                "error": str(exc),
            }), 503

    @app.route("/api/carto-config")
    def carto_config():
        access_token = os.getenv("CARTO_ACCESS_TOKEN", "")
        connection_name = os.getenv("CARTO_CONNECTION_NAME", "")
        if not access_token or not connection_name:
            return jsonify({
                "error": "Set CARTO_ACCESS_TOKEN and CARTO_CONNECTION_NAME.",
            }), 503

        table = quote_fqn(app.config["BUILDINGS_TABLE"])
        sql_query = f"""
            SELECT
                geom AS "geom",
                building_id AS "building_id",
                occupancy_group AS "occupancy_group",
                height_m AS "height_m",
                footprint_area_m2 AS "footprint_area_m2"
            FROM {table}
            WHERE geom IS NOT NULL
        """
        return jsonify({
            "accessToken": access_token,
            "connectionName": connection_name,
            "apiBaseUrl": os.getenv("CARTO_API_BASE_URL"),
            "sqlQuery": sql_query,
            "spatialDataColumn": "geom",
            "initialViewState": {
                "longitude": float(os.getenv("MAP_DEFAULT_LON", "10.45")),
                "latitude": float(os.getenv("MAP_DEFAULT_LAT", "51.16")),
                "zoom": float(os.getenv("MAP_DEFAULT_ZOOM", "5.4")),
                "pitch": 0,
                "bearing": 0,
            },
        })

    @app.route("/api/data-source", methods=["GET", "POST"])
    def data_source():
        if request.method == "GET":
            return jsonify({
                "parquet_path": app.config["PARQUET_PATH"],
                "db_path": app.config["BUILDINGS_TABLE"],
                "snowflake_buildings_table": app.config["BUILDINGS_TABLE"],
                "raw_table": app.config["RAW_TABLE"],
                "parquet_files": sorted(
                    str(path) for path in Path.cwd().rglob("*.parquet")
                    if ".git" not in path.parts and "__pycache__" not in path.parts
                ),
                "db_files": [],
            })

        payload = request.get_json(silent=True) or {}
        parquet_path = str(payload.get("parquet_path", "")).strip()
        buildings_table = str(
            payload.get("snowflake_buildings_table")
            or payload.get("db_path")
            or app.config["BUILDINGS_TABLE"]
        ).strip()

        if parquet_path:
            app.config["PARQUET_PATH"] = parquet_path
        if buildings_table:
            app.config["BUILDINGS_TABLE"] = buildings_table

        return jsonify({
            "parquet_path": app.config["PARQUET_PATH"],
            "db_path": app.config["BUILDINGS_TABLE"],
            "snowflake_buildings_table": app.config["BUILDINGS_TABLE"],
            "status": "active",
            "generated_lookup": False,
        })

    @app.route("/api/building-at")
    def building_at():
        try:
            lon = float(request.args["lon"])
            lat = float(request.args["lat"])
        except (KeyError, ValueError):
            return jsonify({"error": "Valid lon and lat query parameters are required."}), 400

        if not (-180 <= lon <= 180 and -90 <= lat <= 90):
            return jsonify({"error": "Coordinates are out of range."}), 400

        try:
            result = find_building(
                app.config["BUILDINGS_TABLE"],
                lon,
                lat,
                app.config["NEAREST_RADIUS_M"],
            )
        except Exception as exc:
            return jsonify({"error": f"Snowflake lookup failed: {exc}"}), 502

        if result is None:
            return jsonify({
                "match_type": "none",
                "distance_m": None,
                "confidence": "none",
                "building": None,
            })
        return jsonify(result)

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
            return jsonify({"error": "Address search failed. " + " | ".join(errors)}), 502
        return jsonify({"results": []})

    @app.route("/api/exposure/preview", methods=["POST"])
    def exposure_preview():
        file = request.files.get("file")
        if file is None or not file.filename:
            return jsonify({"error": "Upload a CSV file."}), 400

        filename = secure_filename(file.filename)
        if not filename.lower().endswith(".csv"):
            return jsonify({"error": "Only CSV files are supported."}), 400

        upload_id = uuid.uuid4().hex
        upload_path = Path(app.config["UPLOAD_DIR"]) / f"{upload_id}_{filename}"
        file.save(upload_path)

        try:
            columns, rows = preview_csv(upload_path)
        except Exception as exc:
            upload_path.unlink(missing_ok=True)
            return jsonify({"error": f"Could not read CSV: {exc}"}), 400

        return jsonify({
            "upload_id": upload_id,
            "filename": filename,
            "columns": columns,
            "rows": rows,
        })

    @app.route("/api/exposure/enrich", methods=["POST"])
    def exposure_enrich():
        payload = request.get_json(silent=True) or {}
        upload_id = str(payload.get("upload_id", ""))
        lat_col = str(payload.get("lat_col", ""))
        lon_col = str(payload.get("lon_col", ""))
        mode = str(payload.get("mode", "inside_nearest"))

        try:
            max_distance_m = float(payload.get("max_distance_m", app.config["NEAREST_RADIUS_M"]))
        except (TypeError, ValueError):
            return jsonify({"error": "Max distance must be numeric."}), 400

        if not upload_id or not lat_col or not lon_col:
            return jsonify({"error": "Upload id, latitude column, and longitude column are required."}), 400
        if mode not in {"centroid", "inside", "inside_nearest"}:
            return jsonify({"error": "Unknown matching mode."}), 400

        upload_path = find_upload(Path(app.config["UPLOAD_DIR"]), upload_id)
        if upload_path is None:
            return jsonify({"error": "Uploaded CSV was not found. Upload it again."}), 404

        job_id = uuid.uuid4().hex
        output_path = Path(app.config["RESULT_DIR"]) / f"enriched_{job_id}.csv"
        set_job(
            job_id,
            status="queued",
            phase="Queued",
            percent=1,
            download_url=None,
            summary=None,
            error=None,
        )

        def run_job() -> None:
            def progress(phase: str, percent: int) -> None:
                set_job(job_id, status="running", phase=phase, percent=percent)

            try:
                summary = enrich_exposure_csv(
                    csv_path=upload_path,
                    output_path=output_path,
                    lat_col=lat_col,
                    lon_col=lon_col,
                    mode=mode,
                    max_distance_m=max_distance_m,
                    buildings_table=app.config["BUILDINGS_TABLE"],
                    progress_callback=progress,
                )
                set_job(
                    job_id,
                    status="complete",
                    phase="Complete",
                    percent=100,
                    download_url=f"/api/exposure/download/{output_path.name}",
                    summary=summary,
                )
            except Exception as exc:
                output_path.unlink(missing_ok=True)
                set_job(
                    job_id,
                    status="error",
                    phase="Error",
                    percent=100,
                    error=f"Enrichment failed: {exc}",
                )

        Thread(target=run_job, daemon=True).start()
        return jsonify({"job_id": job_id, "status": "queued"}), 202

    @app.route("/api/exposure/progress/<job_id>")
    def exposure_progress(job_id: str):
        with jobs_lock:
            job = jobs.get(job_id)
        if job is None:
            return jsonify({"error": "Job not found."}), 404
        return jsonify(job)

    @app.route("/api/exposure/download/<path:filename>")
    def exposure_download(filename: str):
        safe_name = secure_filename(filename)
        output_path = Path(app.config["RESULT_DIR"]) / safe_name
        if not output_path.exists():
            return jsonify({"error": "Result file was not found."}), 404
        return send_file(output_path, as_attachment=True, download_name=safe_name)

    @app.route("/api/etl/create-database", methods=["POST"])
    def etl_create_database():
        from obm_country_to_parquet import ETLConfig, OpenBuildingMapCountryETL

        boundary_file_path: Optional[str] = None
        boundary_file = request.files.get("boundary_file")
        if boundary_file and boundary_file.filename:
            filename = secure_filename(boundary_file.filename)
            ext = Path(filename).suffix.lower()
            if ext not in {".zip", ".gpkg", ".shp"}:
                return jsonify({"error": "Boundary file must be a .zip, .gpkg, or .shp."}), 400
            saved_path = Path(app.config["UPLOAD_DIR"]) / f"{uuid.uuid4().hex}_{filename}"
            boundary_file.save(saved_path)
            boundary_file_path = str(saved_path)

        def _float(key: str, default: float) -> float:
            try:
                return float(request.form.get(key, default))
            except (TypeError, ValueError):
                return default

        def _str(key: str, default: str) -> str:
            value = request.form.get(key, "").strip()
            return value if value else default

        output_dir = _str("output_dir", "./etl_output")
        output_parquet = _str("output_parquet", f"{output_dir}/buildings_cleaned.parquet")
        duckdb_file = _str("duckdb_file", f"{output_dir}/work_obm.duckdb")
        snowflake_table = _str("snowflake_table", _str("lookup_db_file", app.config["BUILDINGS_TABLE"]))

        cfg = ETLConfig(
            output_dir=output_dir,
            output_parquet=output_parquet,
            duckdb_file=duckdb_file,
            temp_directory=f"{output_dir}/duckdb_temp",
            lon_min=_float("lon_min", 5.5),
            lon_max=_float("lon_max", 15.5),
            lat_min=_float("lat_min", 47.0),
            lat_max=_float("lat_max", 55.3),
            boundary_file=boundary_file_path,
            force=True,
        )

        job_id = uuid.uuid4().hex
        set_etl_job(
            job_id,
            status="running",
            phase="Starting ETL",
            percent=1,
            error=None,
            output_parquet=cfg.output_parquet,
            duckdb_file=cfg.duckdb_file,
            snowflake_table=snowflake_table,
        )

        def run_etl() -> None:
            try:
                set_etl_job(job_id, phase="Running OBM ETL", percent=5)
                etl = OpenBuildingMapCountryETL(cfg)
                etl.run()

                set_etl_job(job_id, phase="Loading Parquet into Snowflake", percent=80)
                from snowflake_loader import load_parquet_to_snowflake

                result = load_parquet_to_snowflake(
                    parquet_path=cfg.output_parquet,
                    raw_table=app.config["RAW_TABLE"],
                    buildings_table=snowflake_table,
                    force=True,
                )
                app.config["PARQUET_PATH"] = cfg.output_parquet
                app.config["BUILDINGS_TABLE"] = snowflake_table
                set_etl_job(
                    job_id,
                    status="complete",
                    phase="Complete",
                    percent=100,
                    row_count=result["row_count"],
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


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Building lookup app backed by Snowflake and CARTO.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    load = subparsers.add_parser("load-parquet")
    load.add_argument("--parquet", default=DEFAULT_PARQUET)
    load.add_argument("--raw-table", default=os.getenv("SNOWFLAKE_RAW_TABLE", DEFAULT_RAW_TABLE))
    load.add_argument("--buildings-table", default=os.getenv("SNOWFLAKE_BUILDINGS_TABLE", DEFAULT_BUILDINGS_TABLE))

    serve = subparsers.add_parser("serve")
    serve.add_argument("--buildings-table", default=os.getenv("SNOWFLAKE_BUILDINGS_TABLE", DEFAULT_BUILDINGS_TABLE))
    serve.add_argument("--raw-table", default=os.getenv("SNOWFLAKE_RAW_TABLE", DEFAULT_RAW_TABLE))
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=5000)
    serve.add_argument("--nearest-radius-m", type=float, default=DEFAULT_NEAREST_RADIUS_M)
    serve.add_argument("--debug", action="store_true")

    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.command == "load-parquet":
        from snowflake_loader import load_parquet_to_snowflake

        result = load_parquet_to_snowflake(
            parquet_path=args.parquet,
            raw_table=args.raw_table,
            buildings_table=args.buildings_table,
            force=True,
        )
        print(f"Loaded {result['row_count']:,} buildings into {result['buildings_table']}")
        return

    app = create_app(
        buildings_table=args.buildings_table,
        raw_table=args.raw_table,
        nearest_radius_m=args.nearest_radius_m,
    )
    app.run(host=args.host, port=args.port, debug=args.debug)


if __name__ == "__main__":
    main()
