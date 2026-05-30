import uuid
from pathlib import Path
from threading import Lock, Thread
from typing import Any, Dict, List, Optional

import duckdb
from flask import Flask, jsonify, request


REQUIRED_MAPPINGS = ("latitude", "longitude", "geometry", "occupancy", "height")
OPTIONAL_MAPPINGS = ("year_built", "construction", "roof_type", "basement")
MAPPING_GUESSES = {
    "latitude": ("lat", "latitude", "centroid_lat", "y"),
    "longitude": ("lon", "lng", "longitude", "centroid_lon", "x"),
    "geometry": ("geom", "geometry", "geom_wkb", "wkb"),
    "occupancy": ("occ", "occupancy", "occupancy_raw", "use"),
    "height": ("gre_height_mod", "height_m", "height", "height_raw"),
    "year_built": ("year_built", "yearbuilt", "construction_year"),
    "construction": ("con", "construction", "construction_type"),
    "roof_type": ("roof_type", "rooftype", "roof"),
    "basement": ("basement", "has_basement"),
}


def _sql_string(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _sql_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _resolve_local_path(path_value: str, suffix: str, label: str, must_exist: bool) -> Path:
    path = Path(path_value).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    path = path.resolve()

    if path.suffix.lower() != suffix:
        raise ValueError(f"{label} must end with {suffix}: {path_value}")
    if must_exist and (not path.exists() or not path.is_file()):
        raise ValueError(f"{label} does not exist: {path_value}")
    return path


def _display_path(path: Path) -> str:
    try:
        return path.resolve().relative_to(Path.cwd().resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def _parquet_columns(parquet_path: Path) -> List[Dict[str, str]]:
    con = duckdb.connect()
    try:
        con.execute("SET enable_geoparquet_conversion = false;")
        rows = con.execute(
            "DESCRIBE SELECT * FROM read_parquet(?)",
            [parquet_path.as_posix()],
        ).fetchall()
    finally:
        con.close()

    return [{"name": str(row[0]), "type": str(row[1])} for row in rows]


def _guess_mappings(columns: List[Dict[str, str]]) -> Dict[str, Optional[str]]:
    names = [column["name"] for column in columns]
    normalized = {
        name: "".join(character for character in name.casefold() if character.isalnum())
        for name in names
    }
    guesses: Dict[str, Optional[str]] = {}

    for mapping, candidates in MAPPING_GUESSES.items():
        normalized_candidates = [
            "".join(character for character in candidate.casefold() if character.isalnum())
            for candidate in candidates
        ]
        guess = next(
            (name for name in names if normalized[name] in normalized_candidates),
            None,
        )
        if guess is None:
            guess = next(
                (
                    name
                    for candidate in normalized_candidates
                    for name in names
                    if candidate in normalized[name]
                ),
                None,
            )
        guesses[mapping] = guess

    return guesses


def _mapped_identifier(mappings: Dict[str, Optional[str]], key: str) -> str:
    return _sql_identifier(str(mappings[key]))


def _optional_select(mappings: Dict[str, Optional[str]], key: str) -> str:
    column = mappings.get(key)
    return f"CAST({_sql_identifier(column)} AS VARCHAR)" if column else "NULL::VARCHAR"


def _geometry_sql(column: str, column_type: str) -> str:
    identifier = _sql_identifier(column)
    normalized_type = column_type.upper()
    if "GEOMETRY" in normalized_type:
        return identifier
    if normalized_type in {"VARCHAR", "TEXT", "STRING"}:
        return f"ST_GeomFromText({identifier})"
    return f"ST_GeomFromWKB({identifier})"


def prepare_custom_parquet_database(
    parquet_path: Path,
    db_path: Path,
    mappings: Dict[str, Optional[str]],
    columns: List[Dict[str, str]],
    threads: int = 8,
) -> int:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    if db_path.exists():
        db_path.unlink()

    types = {column["name"]: column["type"] for column in columns}
    geometry_column = str(mappings["geometry"])
    geometry_sql = _geometry_sql(geometry_column, types[geometry_column])
    parquet_sql = _sql_string(parquet_path.as_posix())
    latitude = _mapped_identifier(mappings, "latitude")
    longitude = _mapped_identifier(mappings, "longitude")
    occupancy = _mapped_identifier(mappings, "occupancy")
    height = _mapped_identifier(mappings, "height")
    source = _sql_string(f"custom_parquet:{parquet_path.stem}")

    con = duckdb.connect(db_path.as_posix())
    try:
        con.execute("LOAD spatial;")
        con.execute("SET enable_geoparquet_conversion = false;")
        con.execute(f"SET threads = {int(threads)};")
        con.execute(f"""
            CREATE TABLE buildings AS
            WITH raw_buildings AS (
                SELECT
                    ROW_NUMBER() OVER () AS source_row_number,
                    TRY_CAST({longitude} AS DOUBLE) AS centroid_lon,
                    TRY_CAST({latitude} AS DOUBLE) AS centroid_lat,
                    CAST({occupancy} AS VARCHAR) AS occupancy_raw,
                    TRY_CAST({height} AS DOUBLE) AS height_m,
                    CAST({height} AS VARCHAR) AS height_raw,
                    {_optional_select(mappings, "year_built")} AS year_built,
                    {_optional_select(mappings, "construction")} AS construction,
                    {_optional_select(mappings, "roof_type")} AS roof_type,
                    {_optional_select(mappings, "basement")} AS basement,
                    {geometry_sql} AS geom
                FROM read_parquet({parquet_sql})
            ),
            projected_buildings AS (
                SELECT
                    *,
                    ST_Transform(geom, 'EPSG:4326', 'EPSG:3035', always_xy := true) AS geom_3035
                FROM raw_buildings
                WHERE
                    geom IS NOT NULL
                    AND centroid_lon BETWEEN -180 AND 180
                    AND centroid_lat BETWEEN -90 AND 90
            )
            SELECT
                'custom_' || LPAD(CAST(source_row_number AS VARCHAR), 12, '0') AS building_id,
                {source} AS source,
                NULL::DOUBLE AS relation_id,
                NULL::VARCHAR AS quadkey,
                NULL::VARCHAR AS quadkey_prefix_6,
                NULL::VARCHAR AS last_update,
                centroid_lon,
                centroid_lat,
                ST_XMin(geom) AS bbox_xmin,
                ST_YMin(geom) AS bbox_ymin,
                ST_XMax(geom) AS bbox_xmax,
                ST_YMax(geom) AS bbox_ymax,
                ST_Area(geom_3035) AS footprint_area_m2,
                height_raw,
                occupancy_raw,
                NULL::DOUBLE AS floorspace_obm_m2,
                'provided'::VARCHAR AS height_source_type,
                height_m,
                NULL::INTEGER AS stories_exact,
                NULL::INTEGER AS stories_min,
                NULL::INTEGER AS stories_max,
                CASE WHEN height_m IS NULL THEN NULL ELSE 'provided' END AS height_quality,
                occupancy_raw AS occupancy_code,
                occupancy_raw AS occupancy_group,
                CASE WHEN occupancy_raw IS NULL THEN NULL ELSE 'provided' END AS occupancy_quality,
                NULL::DOUBLE AS floorspace_est_m2,
                (
                    CAST(height_m IS NOT NULL AS INTEGER)
                    + CAST(occupancy_raw IS NOT NULL AS INTEGER)
                ) / 2.0 AS attribute_completeness_score,
                year_built,
                construction,
                roof_type,
                basement,
                geom,
                geom_3035,
                ST_XMin(geom_3035) AS bbox_3035_xmin,
                ST_YMin(geom_3035) AS bbox_3035_ymin,
                ST_XMax(geom_3035) AS bbox_3035_xmax,
                ST_YMax(geom_3035) AS bbox_3035_ymax
            FROM projected_buildings
            ORDER BY centroid_lon, centroid_lat;
        """)
        con.execute("CREATE INDEX buildings_geom_rtree ON buildings USING RTREE (geom);")
        con.execute("CREATE INDEX buildings_geom_3035_rtree ON buildings USING RTREE (geom_3035);")
        return int(con.execute("SELECT COUNT(*) FROM buildings;").fetchone()[0])
    finally:
        con.close()


def register_custom_parquet_routes(app: Flask) -> None:
    jobs: Dict[str, Dict[str, Any]] = {}
    jobs_lock = Lock()

    def set_job(job_id: str, **updates: Any) -> None:
        with jobs_lock:
            jobs.setdefault(job_id, {}).update(updates)

    @app.route("/api/custom-parquet/inspect", methods=["POST"])
    def custom_parquet_inspect():
        payload = request.get_json(silent=True) or {}
        try:
            parquet_path = _resolve_local_path(
                str(payload.get("parquet_path", "")).strip(),
                ".parquet",
                "Parquet file",
                must_exist=True,
            )
            columns = _parquet_columns(parquet_path)
        except (ValueError, duckdb.Error) as exc:
            return jsonify({"error": f"Could not inspect Parquet file: {exc}"}), 400

        default_db_path = parquet_path.with_name(f"{parquet_path.stem}_lookup.duckdb")
        return jsonify({
            "parquet_path": _display_path(parquet_path),
            "default_db_path": _display_path(default_db_path),
            "columns": columns,
            "suggested_mappings": _guess_mappings(columns),
        })

    @app.route("/api/custom-parquet/create-database", methods=["POST"])
    def custom_parquet_create_database():
        payload = request.get_json(silent=True) or {}
        mappings = payload.get("mappings") or {}

        try:
            parquet_path = _resolve_local_path(
                str(payload.get("parquet_path", "")).strip(),
                ".parquet",
                "Parquet file",
                must_exist=True,
            )
            db_path = _resolve_local_path(
                str(payload.get("db_path", "")).strip(),
                ".duckdb",
                "DuckDB output",
                must_exist=False,
            )
            columns = _parquet_columns(parquet_path)
            column_names = {column["name"] for column in columns}

            normalized_mappings: Dict[str, Optional[str]] = {}
            for key in REQUIRED_MAPPINGS + OPTIONAL_MAPPINGS:
                raw_value = mappings.get(key)
                value = (str(raw_value).strip() or None) if raw_value is not None else None
                if key in REQUIRED_MAPPINGS and value is None:
                    raise ValueError(f"Select a {key.replace('_', ' ')} column.")
                if value is not None and value not in column_names:
                    raise ValueError(f"Mapped column does not exist in the Parquet file: {value}")
                normalized_mappings[key] = value
        except (ValueError, duckdb.Error) as exc:
            return jsonify({"error": str(exc)}), 400

        job_id = uuid.uuid4().hex
        temp_db_path = db_path.with_name(f".{db_path.name}.{job_id}.tmp")
        set_job(
            job_id,
            status="running",
            phase="Creating DuckDB lookup table",
            percent=10,
            error=None,
            parquet_path=_display_path(parquet_path),
            db_path=_display_path(db_path),
        )

        def run_job() -> None:
            try:
                row_count = prepare_custom_parquet_database(
                    parquet_path,
                    temp_db_path,
                    normalized_mappings,
                    columns,
                )
                temp_db_path.replace(db_path)
                app.config["PARQUET_PATH"] = _display_path(parquet_path)
                app.config["DB_PATH"] = _display_path(db_path)
                set_job(
                    job_id,
                    status="complete",
                    phase="Complete",
                    percent=100,
                    row_count=row_count,
                )
            except Exception as exc:
                temp_db_path.unlink(missing_ok=True)
                set_job(
                    job_id,
                    status="error",
                    phase="Error",
                    percent=100,
                    error=str(exc),
                )

        Thread(target=run_job, daemon=True).start()
        return jsonify({"job_id": job_id, "status": "running"}), 202

    @app.route("/api/custom-parquet/progress/<job_id>")
    def custom_parquet_progress(job_id: str):
        with jobs_lock:
            job = jobs.get(job_id)
        if job is None:
            return jsonify({"error": "Job not found."}), 404
        return jsonify(job)
