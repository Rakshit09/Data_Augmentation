import re
import uuid
from pathlib import Path
from threading import Lock, Thread
from typing import Any, Dict, List, Optional

import duckdb
from flask import Flask, jsonify, request


REQUIRED_MAPPINGS = ("latitude", "longitude", "geometry", "occupancy")
OPTIONAL_MAPPINGS = ("height", "year_built", "construction", "roof_type", "basement")
EXTRA_FIELD_LIMIT = 10
EXTRA_FIELD_NAME_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
CUSTOM_BUILDING_RESERVED_COLUMNS = {
    "building_id",
    "source",
    "relation_id",
    "quadkey",
    "quadkey_prefix_6",
    "quadkey_prefix_14",
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
    "year_built",
    "construction",
    "roof_type",
    "basement",
    "geom",
    "geom_3035",
    "bbox_3035_xmin",
    "bbox_3035_ymin",
    "bbox_3035_xmax",
    "bbox_3035_ymax",
}
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
DISPLAY_FIELD_LABELS = {
    "latitude": ("centroid_lat", "Latitude"),
    "longitude": ("centroid_lon", "Longitude"),
    "occupancy": ("occupancy_raw", "Occupancy"),
    "height": ("height_raw", "Height"),
    "year_built": ("year_built", "Year built"),
    "construction": ("construction", "Construction"),
    "roof_type": ("roof_type", "Roof type"),
    "basement": ("basement", "Basement"),
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
            [str(parquet_path)],
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


def _quadkey_prefix_sql(lon_sql: str, lat_sql: str, zoom: int = 6) -> str:
    tile_count = 2 ** zoom
    tile_x = f"CAST(FLOOR((({lon_sql}) + 180.0) / 360.0 * {tile_count}) AS BIGINT)"
    clamped_lat = f"LEAST(GREATEST(({lat_sql}), -85.05112878), 85.05112878)"
    tile_y = (
        "CAST(FLOOR(("
        f"0.5 - LN((1 + SIN(RADIANS({clamped_lat}))) / (1 - SIN(RADIANS({clamped_lat})))) / (4 * PI())"
        f") * {tile_count}) AS BIGINT)"
    )
    digits = []
    for level in range(zoom, 0, -1):
        mask = 1 << (level - 1)
        digits.append(
            "CAST(("
            f"(CASE WHEN (({tile_x}) & {mask}) != 0 THEN 1 ELSE 0 END)"
            f" + (CASE WHEN (({tile_y}) & {mask}) != 0 THEN 2 ELSE 0 END)"
            ") AS VARCHAR)"
        )
    return f"CONCAT({', '.join(digits)})"


def _extra_field_raw_select_sql(extra_fields: List[tuple[str, str]]) -> str:
    if not extra_fields:
        return ""
    return ",\n                    " + ",\n                    ".join(
        f"TRY_CAST(src.{_sql_identifier(source_column)} AS VARCHAR) AS {_sql_identifier(field_name)}"
        for field_name, source_column in extra_fields
    )


def _extra_field_final_select_sql(extra_fields: List[tuple[str, str]]) -> str:
    if not extra_fields:
        return ""
    return ",\n                " + ",\n                ".join(
        _sql_identifier(field_name)
        for field_name, _ in extra_fields
    )


def _preferred_display_fields(
    mappings: Dict[str, Optional[str]],
    extra_fields: List[tuple[str, str]],
) -> List[tuple[str, str, int]]:
    display_fields: List[tuple[str, str, int]] = []
    order = 1

    for key in ("latitude", "longitude", "occupancy", "height", "year_built", "construction", "roof_type", "basement"):
        if mappings.get(key):
            field_name, label = DISPLAY_FIELD_LABELS[key]
            display_fields.append((field_name, label, order))
            order += 1

    for field_name, _ in extra_fields:
        display_fields.append((field_name, field_name, order))
        order += 1

    return display_fields


def prepare_custom_parquet_database(
    parquet_path: Path,
    db_path: Path,
    mappings: Dict[str, Optional[str]],
    columns: List[Dict[str, str]],
    extra_fields: Optional[List[tuple[str, str]]] = None,
    threads: int = 8,
) -> int:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    if db_path.exists():
        db_path.unlink()

    types = {column["name"]: column["type"] for column in columns}
    geometry_column = str(mappings["geometry"])
    geometry_sql = _geometry_sql(geometry_column, types[geometry_column])
    parquet_sql = _sql_string(str(parquet_path))
    latitude = _mapped_identifier(mappings, "latitude")
    longitude = _mapped_identifier(mappings, "longitude")
    occupancy = _mapped_identifier(mappings, "occupancy")
    height_column = mappings.get("height")
    height_m_sql = f"TRY_CAST({_sql_identifier(str(height_column))} AS DOUBLE)" if height_column else "NULL::DOUBLE"
    height_raw_sql = f"CAST({_sql_identifier(str(height_column))} AS VARCHAR)" if height_column else "NULL::VARCHAR"
    source = _sql_string(f"custom_parquet:{parquet_path.stem}")
    quadkey_prefix_6 = _quadkey_prefix_sql("centroid_lon", "centroid_lat", zoom=6)
    quadkey_prefix_14 = _quadkey_prefix_sql("centroid_lon", "centroid_lat", zoom=14)
    normalized_extra_fields = extra_fields or []
    extra_field_raw_sql = _extra_field_raw_select_sql(normalized_extra_fields)
    extra_field_final_sql = _extra_field_final_select_sql(normalized_extra_fields)
    preferred_display_fields = _preferred_display_fields(mappings, normalized_extra_fields)

    con = duckdb.connect(str(db_path))
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
                    {height_m_sql} AS height_m,
                    {height_raw_sql} AS height_raw,
                    {_optional_select(mappings, "year_built")} AS year_built,
                    {_optional_select(mappings, "construction")} AS construction,
                    {_optional_select(mappings, "roof_type")} AS roof_type,
                    {_optional_select(mappings, "basement")} AS basement,
                    {geometry_sql} AS geom{extra_field_raw_sql}
                FROM read_parquet({parquet_sql}) AS src
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
                {quadkey_prefix_6} AS quadkey_prefix_6,
                {quadkey_prefix_14} AS quadkey_prefix_14,
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
                CASE WHEN height_m IS NULL THEN NULL ELSE 'provided' END AS height_source_type,
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
                basement{extra_field_final_sql},
                geom,
                geom_3035,
                ST_XMin(geom_3035) AS bbox_3035_xmin,
                ST_YMin(geom_3035) AS bbox_3035_ymin,
                ST_XMax(geom_3035) AS bbox_3035_xmax,
                ST_YMax(geom_3035) AS bbox_3035_ymax
            FROM projected_buildings
            ORDER BY quadkey_prefix_14, centroid_lon, centroid_lat;
        """)
        con.execute("CREATE INDEX buildings_geom_rtree ON buildings USING RTREE (geom);")
        con.execute("CREATE INDEX buildings_geom_3035_rtree ON buildings USING RTREE (geom_3035);")
        con.execute("CREATE INDEX buildings_quadkey_prefix_14_idx ON buildings(quadkey_prefix_14);")
        con.execute("""
            CREATE TABLE building_display_fields (
                field_name VARCHAR PRIMARY KEY,
                display_label VARCHAR NOT NULL,
                display_order INTEGER NOT NULL
            );
        """)
        if preferred_display_fields:
            con.executemany(
                "INSERT INTO building_display_fields(field_name, display_label, display_order) VALUES (?, ?, ?)",
                preferred_display_fields,
            )
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
        extra_fields = payload.get("extra_fields") or []

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

            if not isinstance(extra_fields, list):
                raise ValueError("Additional mapped fields must be a list.")
            if len(extra_fields) > EXTRA_FIELD_LIMIT:
                raise ValueError(f"You can add up to {EXTRA_FIELD_LIMIT} additional mapped fields.")

            existing_output_names = {column.casefold() for column in CUSTOM_BUILDING_RESERVED_COLUMNS}

            normalized_extra_fields: List[tuple[str, str]] = []
            extra_field_names: set[str] = set()
            for index, field in enumerate(extra_fields, start=1):
                if not isinstance(field, dict):
                    raise ValueError("Each additional mapped field must include a name and source column.")

                field_name = str(field.get("name", "")).strip()
                source_column = str(field.get("column", "")).strip()
                if not field_name and not source_column:
                    continue
                if not field_name or not source_column:
                    raise ValueError(f"Additional field #{index} needs both a name and a source column.")
                if not EXTRA_FIELD_NAME_PATTERN.fullmatch(field_name):
                    raise ValueError(
                        f"Additional field '{field_name}' must start with a letter or underscore and use only letters, numbers, and underscores."
                    )
                if source_column not in column_names:
                    raise ValueError(f"Additional field source column does not exist in the Parquet file: {source_column}")
                if field_name.casefold() in existing_output_names or field_name.casefold() in extra_field_names:
                    raise ValueError(f"Additional field name is already in use: {field_name}")

                extra_field_names.add(field_name.casefold())
                normalized_extra_fields.append((field_name, source_column))
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
                    normalized_extra_fields,
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
