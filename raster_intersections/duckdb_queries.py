from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import duckdb

from .utils import resolve_csv_scan_options, runtime_dir, safe_json_value, sql_identifier, sql_string, unique_name


EXPOSURE_SI_CANDIDATES = [
    "RepValBldg",
    "FLCV1VAL",
    "FLCV2VAL",
    "TIV",
    "TotalInsuredValue",
    "sum_insured",
    "suminsured",
    "insured_value",
    "building_value",
]

DATABASE_SI_CANDIDATES = [
    "estimated_si",
    "building_estimated_si",
    "sum_insured",
    "replacement_value",
    "building_value",
]

DATABASE_CONTEXT_FIELDS = [
    "building_id",
    "centroid_lon",
    "centroid_lat",
    "occupancy_group",
    "occupancy_code",
    "height_m",
    "stories_exact",
    "footprint_area_m2",
    "floorspace_est_m2",
    "attribute_completeness_score",
]


def query_exposure_candidates(
    cache_path: Path,
    bounds: Dict[str, float],
    max_candidates: int,
) -> Tuple[List[Dict[str, Any]], int]:
    con = duckdb.connect(str(cache_path), read_only=True)
    try:
        count = int(con.execute(
            """
            SELECT COUNT(*)
            FROM points
            WHERE lon BETWEEN ? AND ?
                AND lat BETWEEN ? AND ?;
            """,
            [bounds["min_lon"], bounds["max_lon"], bounds["min_lat"], bounds["max_lat"]],
        ).fetchone()[0] or 0)
        if count > max_candidates:
            raise ValueError(
                f"The selected area contains {count:,} exposure locations. "
                "Zoom in or keep Area set to Visible map area to keep the analysis smooth."
            )
        rows = con.execute(
            """
            SELECT row_id, lon, lat
            FROM points
            WHERE lon BETWEEN ? AND ?
                AND lat BETWEEN ? AND ?
            ORDER BY row_id;
            """,
            [bounds["min_lon"], bounds["max_lon"], bounds["min_lat"], bounds["max_lat"]],
        ).fetchall()
    finally:
        con.close()

    return [
        {"row_id": int(row_id), "lon": float(lon), "lat": float(lat)}
        for row_id, lon, lat in rows
    ], count


def query_database_candidates(
    con: duckdb.DuckDBPyConnection,
    bounds: Dict[str, float],
    max_candidates: int,
) -> Tuple[List[Dict[str, Any]], int, List[str]]:
    columns = table_columns(con, "buildings")
    if "centroid_lon" not in columns or "centroid_lat" not in columns:
        raise ValueError("The building database does not contain centroid_lon and centroid_lat columns.")

    selected_columns = [column for column in DATABASE_CONTEXT_FIELDS if column in columns]
    selected_columns.extend(column for column in DATABASE_SI_CANDIDATES if column in columns and column not in selected_columns)
    selected_columns = list(dict.fromkeys(selected_columns))
    select_sql = ", ".join(sql_identifier(column) for column in selected_columns)

    rows = con.execute(
        f"""
        SELECT {select_sql}
        FROM buildings
        WHERE centroid_lon BETWEEN ? AND ?
            AND centroid_lat BETWEEN ? AND ?
        LIMIT ?;
        """,
        [bounds["min_lon"], bounds["max_lon"], bounds["min_lat"], bounds["max_lat"], max_candidates + 1],
    ).fetchall()

    if len(rows) > max_candidates:
        raise ValueError(
            f"The selected area contains more than {max_candidates:,} building centroids. "
            "Zoom in or keep Area set to Visible map area to keep the analysis smooth."
        )

    candidates: List[Dict[str, Any]] = []
    for raw_row in rows:
        row = {
            column: safe_json_value(raw_row[index])
            for index, column in enumerate(selected_columns)
        }
        row["lon"] = float(row["centroid_lon"])
        row["lat"] = float(row["centroid_lat"])
        candidates.append(row)
    return candidates, len(candidates), selected_columns


def intersect_vector_candidates(
    layer: Dict[str, Any],
    candidates: List[Dict[str, Any]],
    field: str,
    max_candidates: int,
) -> List[Dict[str, Any]]:
    if not candidates:
        return []
    if len(candidates) > max_candidates:
        raise ValueError(
            f"The selected area contains more than {max_candidates:,} candidate locations. "
            "Zoom in or keep Area set to Visible map area to keep the analysis smooth."
        )

    cache_path = Path(str(layer.get("cache_path") or ""))
    if not cache_path.is_file():
        raise ValueError("The uploaded vector layer cache is no longer available. Upload it again.")

    available_fields = {str(item.get("name") or "") for item in layer.get("fields", [])}
    selected_field = field if field in available_fields else ""
    display_field_sql = sql_identifier(selected_field) if selected_field else "CAST(feature_id AS VARCHAR)"
    numeric_field_sql = f"TRY_CAST({sql_identifier(selected_field)} AS DOUBLE)" if selected_field else "NULL"

    con = layer.get("connection")
    connection_lock = layer.get("connection_lock")
    if con is None or connection_lock is None:
        raise ValueError("The uploaded vector layer connection is no longer available. Upload it again.")

    with connection_lock:
        if layer.get("connection") is not con:
            raise ValueError("The uploaded vector layer connection is no longer available. Upload it again.")
        con.execute("DROP TABLE IF EXISTS candidate_points;")
        con.execute("""
            CREATE TEMP TABLE candidate_points(
                candidate_index BIGINT,
                lon DOUBLE,
                lat DOUBLE
            );
        """)
        con.executemany(
            "INSERT INTO candidate_points VALUES (?, ?, ?);",
            [
                (index, float(row["lon"]), float(row["lat"]))
                for index, row in enumerate(candidates)
            ],
        )
        try:
            rows = con.execute(f"""
                WITH matches AS (
                    SELECT
                        c.candidate_index,
                        f.feature_id,
                        CAST({display_field_sql} AS VARCHAR) AS vector_value,
                        {numeric_field_sql} AS vector_numeric_value,
                        ROW_NUMBER() OVER (
                            PARTITION BY c.candidate_index
                            ORDER BY f.feature_id
                        ) AS rn
                    FROM candidate_points c
                    JOIN features f
                        ON f.bbox_xmax >= c.lon
                        AND f.bbox_xmin <= c.lon
                        AND f.bbox_ymax >= c.lat
                        AND f.bbox_ymin <= c.lat
                        AND ST_Intersects(f.geom, ST_Point(c.lon, c.lat))
                )
                SELECT candidate_index, feature_id, vector_value, vector_numeric_value
                FROM matches
                WHERE rn = 1
                ORDER BY candidate_index;
            """).fetchall()
        finally:
            con.execute("DROP TABLE IF EXISTS candidate_points;")

    output: List[Dict[str, Any]] = []
    for candidate_index, feature_id, vector_value, vector_numeric_value in rows:
        candidate = dict(candidates[int(candidate_index)])
        candidate["raster_sample_lon"] = safe_json_value(candidate.get("lon"))
        candidate["raster_sample_lat"] = safe_json_value(candidate.get("lat"))
        candidate["vector_feature_id"] = int(feature_id)
        candidate["vector_field"] = selected_field or "feature_id"
        candidate["vector_value"] = safe_json_value(vector_value)
        candidate["raster_value"] = safe_json_value(vector_numeric_value)
        output.append(candidate)
    return output


def append_exposure_source_columns(
    upload_path: Path,
    sampled_rows: List[Dict[str, Any]],
    convert_excel_to_csv: Optional[Callable[[Path, Path], None]] = None,
) -> Tuple[List[str], List[Dict[str, Any]], Optional[str]]:
    if not sampled_rows:
        return [], [], None

    csv_path = upload_path
    converted_path: Optional[Path] = None
    if upload_path.suffix.lower() == ".xlsx":
        if convert_excel_to_csv is None:
            return _sampled_columns(sampled_rows), sampled_rows, "Excel source rows could not be joined; only sampled coordinates were returned."
        converted_path = runtime_dir("raster_intersections") / f"{upload_path.stem}_intersection_source.csv"
        convert_excel_to_csv(upload_path, converted_path)
        csv_path = converted_path

    con = duckdb.connect()
    try:
        con.execute(f"SET temp_directory = {sql_string(str(runtime_dir('duckdb_temp').resolve()))};")
        scan_options = resolve_csv_scan_options(con, csv_path)
        source_columns = csv_columns(con, csv_path, scan_options)
        include_vector = any("vector_feature_id" in row or "vector_value" in row for row in sampled_rows)
        exposure_row_id_name = unique_name("exposure_row_id", source_columns)
        raster_lon_name = unique_name("raster_sample_lon", [*source_columns, exposure_row_id_name])
        raster_lat_name = unique_name("raster_sample_lat", [*source_columns, exposure_row_id_name, raster_lon_name])
        raster_value_name = unique_name("raster_value", [*source_columns, exposure_row_id_name, raster_lon_name, raster_lat_name])
        vector_feature_id_name = unique_name("vector_feature_id", [*source_columns, exposure_row_id_name, raster_lon_name, raster_lat_name, raster_value_name])
        vector_field_name = unique_name("vector_field", [*source_columns, exposure_row_id_name, raster_lon_name, raster_lat_name, raster_value_name, vector_feature_id_name])
        vector_value_name = unique_name("vector_value", [*source_columns, exposure_row_id_name, raster_lon_name, raster_lat_name, raster_value_name, vector_feature_id_name, vector_field_name])

        con.execute("""
            CREATE TEMP TABLE sampled(
                row_id BIGINT,
                raster_sample_lon DOUBLE,
                raster_sample_lat DOUBLE,
                raster_value DOUBLE,
                vector_feature_id BIGINT,
                vector_field VARCHAR,
                vector_value VARCHAR
            );
        """)
        con.executemany(
            "INSERT INTO sampled VALUES (?, ?, ?, ?, ?, ?, ?);",
            [
                (
                    int(row["row_id"]),
                    float(row["raster_sample_lon"]),
                    float(row["raster_sample_lat"]),
                    _optional_float(row.get("raster_value")),
                    _optional_int(row.get("vector_feature_id")),
                    None if row.get("vector_field") is None else str(row.get("vector_field")),
                    None if row.get("vector_value") is None else str(row.get("vector_value")),
                )
                for row in sampled_rows
            ],
        )

        source_select = ",\n                ".join(
            f"source.{sql_identifier(column)} AS {sql_identifier(column)}"
            for column in source_columns
        )
        vector_select = ""
        if include_vector:
            vector_select = f""",
                sampled.vector_feature_id AS {sql_identifier(vector_feature_id_name)},
                sampled.vector_field AS {sql_identifier(vector_field_name)},
                sampled.vector_value AS {sql_identifier(vector_value_name)}"""
        csv_sql = sql_string(str(csv_path.resolve()))
        result = con.execute(f"""
            WITH source AS (
                SELECT
                    row_number() OVER () AS __source_row_id,
                    *
                FROM read_csv_auto({csv_sql}, {scan_options})
            )
            SELECT
                sampled.row_id AS {sql_identifier(exposure_row_id_name)},
                {source_select},
                sampled.raster_sample_lon AS {sql_identifier(raster_lon_name)},
                sampled.raster_sample_lat AS {sql_identifier(raster_lat_name)},
                sampled.raster_value AS {sql_identifier(raster_value_name)}
                {vector_select}
            FROM sampled
            JOIN source ON source.__source_row_id = sampled.row_id
            ORDER BY sampled.row_id;
        """)
        columns = [str(description[0]) for description in (result.description or [])]
        rows = [
            {
                column: safe_json_value(raw_row[index])
                for index, column in enumerate(columns)
            }
            for raw_row in result.fetchall()
        ]
        return columns, rows, None
    except Exception as exc:
        return _sampled_columns(sampled_rows), sampled_rows, f"Source rows could not be joined; sampled coordinate rows were returned instead. {exc}"
    finally:
        con.close()
        if converted_path is not None:
            converted_path.unlink(missing_ok=True)


def table_columns(con: duckdb.DuckDBPyConnection, table_name: str) -> List[str]:
    rows = con.execute(f"DESCRIBE {sql_identifier(table_name)};").fetchall()
    return [str(row[0]) for row in rows]


def csv_columns(con: duckdb.DuckDBPyConnection, csv_path: Path, scan_options: str) -> List[str]:
    rows = con.execute(f"""
        DESCRIBE SELECT *
        FROM read_csv_auto({sql_string(str(csv_path.resolve()))}, {scan_options});
    """).fetchall()
    return [str(row[0]) for row in rows]


def _sampled_columns(rows: List[Dict[str, Any]]) -> List[str]:
    columns: List[str] = []
    for row in rows[:32]:
        for key in row.keys():
            if key not in columns:
                columns.append(key)
    return columns


def _optional_float(value: Any) -> Optional[float]:
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


def _optional_int(value: Any) -> Optional[int]:
    try:
        return None if value is None else int(value)
    except (TypeError, ValueError):
        return None
