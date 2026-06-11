import argparse
import codecs
import json
import os
import time
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import duckdb
import pandas as pd


NEAREST_CANDIDATE_LIMIT = 32
DEFAULT_QUADKEY_PREFIX_ZOOM = 6
OPTIMIZED_QUADKEY_PREFIX_ZOOM = 14

DEFAULT_BUILDING_COLUMNS = ["building_id", "source", "relation_id", "last_update", "centroid_lon", "centroid_lat", "footprint_area_m2",
                            "height_raw", "occupancy_raw", "floorspace_obm_m2", "height_source_type", "height_m", "stories_exact", "stories_min", "stories_max",
                            "height_quality", "occupancy_code", "occupancy_group", "occupancy_quality", "floorspace_est_m2", "attribute_completeness_score"]

DEFAULT_BUILDING_COLUMNS = ["construction", "year_built", "STOREY"]
def sql_string(value):
    return "'" + str(value).replace("'", "''") + "'"


def sql_identifier(value):
    return '"' + str(value).replace('"', '""') + '"'


def json_safe(value):
    if pd.isna(value):
        return None
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return value


def open_db(db_path, read_only=True):
    con = duckdb.connect(str(Path(db_path)), read_only=read_only)
    con.execute("LOAD spatial;")
    return con


def detect_csv_encoding(csv_path):
    sample = Path(csv_path).read_bytes()[:1_048_576]

    if sample.startswith(codecs.BOM_UTF8):
        return "utf-8-sig"

    for encoding in ["utf-8-sig", "utf-8", "cp1252", "latin1"]:
        try:
            sample.decode(encoding)
            return encoding
        except UnicodeDecodeError:
            pass

    return "latin1"


def duckdb_csv_encoding(csv_path):
    encoding = detect_csv_encoding(csv_path)

    if encoding == "utf-8-sig":
        return "utf-8"

    if encoding in ["cp1252", "latin1"]:
        return "latin-1"

    return encoding


def duckdb_csv_encoding_candidates(csv_path):
    detected = duckdb_csv_encoding(csv_path)
    candidates = [detected, "utf-8", "latin-1"]

    result = []
    for encoding in candidates:
        if encoding not in result:
            result.append(encoding)

    return result


def csv_scan_options(encoding):
    return ("sample_size = 20480, "
        "ignore_errors = true, "
        "header = true, "
        "all_varchar = true, "
        f"encoding = {sql_string(encoding)}")


def resolve_csv_scan_options(con, csv_path):
    csv_file = sql_string(str(Path(csv_path).resolve()))
    errors = []

    for encoding in duckdb_csv_encoding_candidates(csv_path):
        options = csv_scan_options(encoding)

        try:
            con.execute(f"""
                SELECT *
                FROM read_csv_auto({csv_file}, {options})
                LIMIT 1;
            """)
            return options

        except duckdb.Error as exc:
            errors.append(f"{encoding}: {exc}")

    raise ValueError("Could not read CSV. " + " | ".join(errors))


def csv_columns(con, csv_path, scan_options=None):
    csv_file = sql_string(str(Path(csv_path).resolve()))
    scan_options = scan_options or resolve_csv_scan_options(con, csv_path)

    rows = con.execute(f"""
        DESCRIBE SELECT *
        FROM read_csv_auto({csv_file}, {scan_options});
    """).fetchall()

    return [row[0] for row in rows]


def exposure_select(columns):
    return ",\n            ".join(f"e.{sql_identifier(col)}" for col in columns)


def b_select(alias, columns):
    return ",\n                    ".join(f"{alias}.{sql_identifier(col)} AS {sql_identifier(col)}"
        for col in columns)


def final_building_select(source, columns):
    if not columns:
        return ""

    fields = [f"{source}.{sql_identifier(col)} AS {sql_identifier('building_' + col)}"
        for col in columns]

    return ",\n                " + ",\n                ".join(fields)


def final_coalesced_building_select(columns):
    if not columns:
        return ""

    fields = [f"COALESCE(i.{sql_identifier(col)}, n.{sql_identifier(col)}) "
        f"AS {sql_identifier('building_' + col)}"
        for col in columns]

    return ",\n                " + ",\n                ".join(fields)


# ---------------------------------------------------------------------
# Lookup DB field handling
# ---------------------------------------------------------------------

def is_internal_lookup_column(column_name, data_type):
    name = column_name.casefold()
    dtype = data_type.casefold()

    return ("geometry" in dtype
        or name.startswith("geom")
        or name.startswith("bbox")
        or name.startswith("quadkey"))


def lookup_display_columns(con):
    rows = con.execute("""
        SELECT column_name, data_type
        FROM information_schema.columns
        WHERE table_name = 'buildings'
        ORDER BY ordinal_position;
    """).fetchall()

    return [str(column_name)
        for column_name, data_type in rows
        if not is_internal_lookup_column(str(column_name), str(data_type))]


def check_lookup_db(con):
    tables = con.execute("""
        SELECT table_name
        FROM information_schema.tables
        WHERE table_name = 'buildings';
    """).fetchall()

    if not tables:
        raise ValueError("The DuckDB file does not contain a 'buildings' lookup table.")

    required = {"geom",
        "centroid_lon",
        "centroid_lat",
        "bbox_xmin",
        "bbox_ymin",
        "bbox_xmax",
        "bbox_ymax",}

    columns = {row[0]
        for row in con.execute("""
            SELECT column_name
            FROM information_schema.columns
            WHERE table_name = 'buildings';
        """).fetchall()}

    missing = sorted(required - columns)
    if missing:
        raise ValueError(f"Lookup DB is missing required columns: {missing}")


def check_inside_nearest_columns(con):
    required = {"geom_3035",
        "bbox_3035_xmin",
        "bbox_3035_ymin",
        "bbox_3035_xmax",
        "bbox_3035_ymax",}

    columns = {row[0]
        for row in con.execute("""
            SELECT column_name
            FROM information_schema.columns
            WHERE table_name = 'buildings';
        """).fetchall()}

    missing = sorted(required - columns)
    if missing:
        raise ValueError("inside_nearest mode needs a lookup DB created with projected geometry. "
            f"Missing columns: {missing}")


def enrichment_quadkey_config(con):
    columns = {row[0]
        for row in con.execute("""
            SELECT column_name
            FROM information_schema.columns
            WHERE table_name = 'buildings';
        """).fetchall()}

    if "quadkey_prefix_14" in columns:
        prefix_col = "quadkey_prefix_14"
        zoom = OPTIMIZED_QUADKEY_PREFIX_ZOOM
    elif "quadkey_prefix_6" in columns:
        prefix_col = "quadkey_prefix_6"
        zoom = DEFAULT_QUADKEY_PREFIX_ZOOM
    else:
        raise ValueError("Lookup DB needs either quadkey_prefix_14 or quadkey_prefix_6.")

    has_null_prefixes = bool(con.execute(f"""
        SELECT EXISTS (SELECT 1
            FROM buildings
            WHERE {sql_identifier(prefix_col)} IS NULL
            LIMIT 1
        );
    """).fetchone()[0])

    return prefix_col, zoom, has_null_prefixes


# ---------------------------------------------------------------------
# Quadkey SQL
# ---------------------------------------------------------------------

def quadkey_prefix_sql(tile_x_sql, tile_y_sql, zoom):
    digits = []

    for level in range(zoom, 0, -1):
        mask = 1 << (level - 1)

        digit = ("CAST(("
            f"(CASE WHEN (({tile_x_sql}) & {mask}) != 0 THEN 1 ELSE 0 END)"
            " + "
            f"(CASE WHEN (({tile_y_sql}) & {mask}) != 0 THEN 2 ELSE 0 END)"
            ") AS VARCHAR)")

        digits.append(digit)

    return f"CONCAT({', '.join(digits)})"


# ---------------------------------------------------------------------
# Main SQL builder
# ---------------------------------------------------------------------

def enrichment_select_sql(csv_file, scan_options, lat_col, lon_col, mode,
                          radius_m, original_cols, building_fields,
                          prefix_col, prefix_zoom, allow_null_prefix):

    radius_sql = str(float(radius_m))
    tile_count = 1 << prefix_zoom
    max_tile = tile_count - 1

    quadkey_expr = quadkey_prefix_sql("tile_x", "tile_y", prefix_zoom)
    prefix_id = sql_identifier(prefix_col)

    join_on_quadkey = f"b.{prefix_id} = t.__quadkey_prefix"
    if allow_null_prefix:
        join_on_quadkey = (f"({join_on_quadkey} OR "
            f"(b.{prefix_id} IS NULL AND t.__is_primary_tile))")

    working_fields = list(dict.fromkeys(["building_id", *building_fields]))

    ranked_fields = ",\n                    ".join(sql_identifier(col) for col in working_fields)

    nearest_fields = ",\n                ".join(f"c.{sql_identifier(col)} AS {sql_identifier(col)}"
        for col in working_fields)

    if mode in ["centroid", "inside_nearest"]:
        distance_columns = f""",
                {radius_sql} / 111320.0 AS __lat_delta,
                {radius_sql} / (111320.0 * __cos_lat) AS __lon_delta
        """
    else:
        distance_columns = ""

    if mode == "inside_nearest":
        projected_ctes = """
        exposure_projected AS (SELECT
                *,
                CASE
                    WHEN __valid_coordinates
                    THEN ST_Transform(__pt, 'EPSG:4326', 'EPSG:3035', always_xy := true)
                    ELSE NULL
                END AS __pt_m
            FROM exposure_base),
        exposure AS (SELECT
                *,
                CASE WHEN __pt_m IS NOT NULL THEN ST_X(__pt_m) ELSE NULL END AS __pt_m_x,
                CASE WHEN __pt_m IS NOT NULL THEN ST_Y(__pt_m) ELSE NULL END AS __pt_m_y
            FROM exposure_projected)
        """
    else:
        projected_ctes = """
        exposure AS (SELECT *
            FROM exposure_base)
        """

    base_ctes = f"""
        WITH exposure_raw AS (SELECT
                ROW_NUMBER() OVER () AS __exposure_row_id,
                *
            FROM read_csv_auto({csv_file}, {scan_options})),
        exposure_parsed AS (SELECT
                *,
                TRY_CAST({sql_identifier(lon_col)} AS DOUBLE) AS __lon,
                TRY_CAST({sql_identifier(lat_col)} AS DOUBLE) AS __lat
            FROM exposure_raw),
        exposure_base AS (SELECT
                *,
                __lon BETWEEN -180 AND 180
                    AND __lat BETWEEN -90 AND 90 AS __valid_coordinates,

                LEAST(GREATEST(__lat, -85.05112878), 85.05112878) AS __lat_clamped,

                CASE
                    WHEN __lat BETWEEN -90 AND 90
                    THEN GREATEST(COS(RADIANS(__lat)), 0.2)
                    ELSE NULL
                END AS __cos_lat,

                CASE
                    WHEN __lon BETWEEN -180 AND 180 AND __lat BETWEEN -90 AND 90
                    THEN ST_Point(__lon, __lat)
                    ELSE NULL
                END AS __pt,

                CASE
                    WHEN __lon BETWEEN -180 AND 180 AND __lat BETWEEN -90 AND 90
                    THEN LEAST(GREATEST(
                            CAST(FLOOR((__lon + 180.0) / 360.0 * {tile_count}) AS BIGINT),
                            0),
                        {max_tile})
                    ELSE NULL
                END AS __tile_x,

                CASE
                    WHEN __lon BETWEEN -180 AND 180 AND __lat BETWEEN -90 AND 90
                    THEN LEAST(GREATEST(
                            CAST(FLOOR(
                                    (0.5
                                        - LN((
                                                1 + SIN(RADIANS(LEAST(GREATEST(__lat, -85.05112878), 85.05112878))))
                                            /
                                            (1 - SIN(RADIANS(LEAST(GREATEST(__lat, -85.05112878), 85.05112878))))
                                        ) / (4 * PI())
                                    ) * {tile_count}
                                ) AS BIGINT),
                            0),
                        {max_tile})
                    ELSE NULL
                END AS __tile_y
                {distance_columns}
            FROM exposure_parsed),
        {projected_ctes},
        exposure_tiles AS (SELECT
                t.__exposure_row_id,
                {quadkey_expr} AS __quadkey_prefix,
                t.dx = 0 AND t.dy = 0 AS __is_primary_tile
            FROM (SELECT
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
            WHERE t.tile_x BETWEEN 0 AND {max_tile}
              AND t.tile_y BETWEEN 0 AND {max_tile})
    """

    if mode == "centroid":
        return f"""
            {base_ctes},
            centroid_candidates AS (SELECT
                    e.__exposure_row_id,
                    ST_Distance_Sphere(ST_Point(b.centroid_lon, b.centroid_lat), e.__pt) AS distance_m,
                    ROW_NUMBER() OVER (PARTITION BY e.__exposure_row_id
                        ORDER BY ST_Distance_Sphere(ST_Point(b.centroid_lon, b.centroid_lat), e.__pt)
                    ) AS candidate_rank,
                    {b_select("b", working_fields)}
                FROM exposure e
                JOIN exposure_tiles t USING (__exposure_row_id)
                JOIN buildings b
                    ON {join_on_quadkey}
                    AND b.centroid_lon BETWEEN e.__lon - e.__lon_delta AND e.__lon + e.__lon_delta
                    AND b.centroid_lat BETWEEN e.__lat - e.__lat_delta AND e.__lat + e.__lat_delta),
            centroid_ranked AS (SELECT
                    __exposure_row_id,
                    distance_m,
                    ROW_NUMBER() OVER (PARTITION BY __exposure_row_id
                        ORDER BY distance_m
                    ) AS rn,
                    {ranked_fields}
                FROM centroid_candidates
                WHERE candidate_rank <= {NEAREST_CANDIDATE_LIMIT}
                  AND distance_m <= {radius_sql}),
            matches AS (SELECT *
                FROM centroid_ranked
                WHERE rn = 1)
            SELECT
                {original_cols},
                e.__valid_coordinates AS coordinate_valid,
                CASE WHEN m.__exposure_row_id IS NOT NULL THEN 'nearest_centroid' ELSE 'none' END AS building_match_type,
                m.distance_m AS building_distance_m,
                CASE
                    WHEN m.__exposure_row_id IS NULL THEN 'none'
                    WHEN m.distance_m <= 15 THEN 'medium'
                    ELSE 'low'
                END AS building_confidence
                {final_building_select("m", building_fields)}
            FROM exposure e
            LEFT JOIN matches m USING (__exposure_row_id)
            ORDER BY e.__exposure_row_id
        """

    if mode == "inside":
        return f"""
            {base_ctes},
            inside_ranked AS (SELECT
                    e.__exposure_row_id,
                    ROW_NUMBER() OVER (PARTITION BY e.__exposure_row_id
                        ORDER BY b.footprint_area_m2 ASC NULLS LAST
                    ) AS rn,
                    {b_select("b", working_fields)}
                FROM exposure e
                JOIN exposure_tiles t USING (__exposure_row_id)
                JOIN buildings b
                    ON {join_on_quadkey}
                    AND e.__lon BETWEEN b.bbox_xmin AND b.bbox_xmax
                    AND e.__lat BETWEEN b.bbox_ymin AND b.bbox_ymax
                    AND ST_Intersects(b.geom, e.__pt)),
            matches AS (SELECT *
                FROM inside_ranked
                WHERE rn = 1)
            SELECT
                {original_cols},
                e.__valid_coordinates AS coordinate_valid,
                CASE WHEN m.__exposure_row_id IS NOT NULL THEN 'inside_polygon' ELSE 'none' END AS building_match_type,
                CASE WHEN m.__exposure_row_id IS NOT NULL THEN 0.0 ELSE NULL END AS building_distance_m,
                CASE WHEN m.__exposure_row_id IS NOT NULL THEN 'high' ELSE 'none' END AS building_confidence
                {final_building_select("m", building_fields)}
            FROM exposure e
            LEFT JOIN matches m USING (__exposure_row_id)
            ORDER BY e.__exposure_row_id
        """

    # Default: inside first, then nearest polygon
    return f"""
        {base_ctes},
        inside_ranked AS (SELECT
                e.__exposure_row_id,
                ROW_NUMBER() OVER (PARTITION BY e.__exposure_row_id
                    ORDER BY b.footprint_area_m2 ASC NULLS LAST
                ) AS rn,
                {b_select("b", working_fields)}
            FROM exposure e
            JOIN exposure_tiles t USING (__exposure_row_id)
            JOIN buildings b
                ON {join_on_quadkey}
                AND e.__lon BETWEEN b.bbox_xmin AND b.bbox_xmax
                AND e.__lat BETWEEN b.bbox_ymin AND b.bbox_ymax
                AND ST_Intersects(b.geom, e.__pt)),
        inside_matches AS (SELECT *
            FROM inside_ranked
            WHERE rn = 1),
        unmatched_exposure AS (SELECT e.*
            FROM exposure e
            LEFT JOIN inside_matches i USING (__exposure_row_id)
            WHERE e.__valid_coordinates
              AND i.__exposure_row_id IS NULL),
        nearest_candidates AS (SELECT
                e.__exposure_row_id,
                b.geom_3035 AS __geom_3035,
                ST_Distance_Sphere(ST_Point(b.centroid_lon, b.centroid_lat), e.__pt) AS centroid_distance_m,
                ROW_NUMBER() OVER (PARTITION BY e.__exposure_row_id
                    ORDER BY ST_Distance_Sphere(ST_Point(b.centroid_lon, b.centroid_lat), e.__pt)
                ) AS candidate_rank,
                {b_select("b", working_fields)}
            FROM unmatched_exposure e
            JOIN exposure_tiles t USING (__exposure_row_id)
            JOIN buildings b
                ON {join_on_quadkey}
                AND b.bbox_xmin <= e.__lon + e.__lon_delta
                AND b.bbox_xmax >= e.__lon - e.__lon_delta
                AND b.bbox_ymin <= e.__lat + e.__lat_delta
                AND b.bbox_ymax >= e.__lat - e.__lat_delta
                AND b.bbox_3035_xmin <= e.__pt_m_x + {radius_sql}
                AND b.bbox_3035_xmax >= e.__pt_m_x - {radius_sql}
                AND b.bbox_3035_ymin <= e.__pt_m_y + {radius_sql}
                AND b.bbox_3035_ymax >= e.__pt_m_y - {radius_sql}),
        nearest_ranked AS (SELECT
                c.__exposure_row_id,
                ST_Distance(c.__geom_3035, e.__pt_m) AS distance_m,
                ROW_NUMBER() OVER (PARTITION BY c.__exposure_row_id
                    ORDER BY ST_Distance(c.__geom_3035, e.__pt_m)
                ) AS rn,
                {nearest_fields}
            FROM nearest_candidates c
            JOIN unmatched_exposure e USING (__exposure_row_id)
            WHERE c.candidate_rank <= {NEAREST_CANDIDATE_LIMIT}
              AND ST_DWithin(c.__geom_3035, e.__pt_m, {radius_sql})),
        nearest_matches AS (SELECT *
            FROM nearest_ranked
            WHERE rn = 1)
        SELECT
            {original_cols},
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
            {final_coalesced_building_select(building_fields)}
        FROM exposure e
        LEFT JOIN inside_matches i USING (__exposure_row_id)
        LEFT JOIN nearest_matches n USING (__exposure_row_id)
        ORDER BY e.__exposure_row_id
    """


# ---------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------

def summarize_output(con, output_path):
    output_file = sql_string(str(Path(output_path).resolve()))

    row = con.execute(f"""
        WITH enriched AS (SELECT
                TRY_CAST(coordinate_valid AS BOOLEAN) AS coordinate_valid,
                CAST(building_match_type AS VARCHAR) AS building_match_type,
                TRY_CAST(building_distance_m AS DOUBLE) AS building_distance_m
            FROM read_csv_auto({output_file}, header = true, sample_size = 20480, all_varchar = true))
        SELECT
            COUNT(*) AS total_rows,
            COALESCE(SUM(CASE WHEN coordinate_valid THEN 1 ELSE 0 END), 0) AS valid_coordinate_rows,
            COALESCE(SUM(CASE WHEN building_match_type = 'inside_polygon' THEN 1 ELSE 0 END), 0) AS inside_polygon_matches,
            COALESCE(SUM(CASE WHEN building_match_type IN ('nearest_polygon', 'nearest_centroid') THEN 1 ELSE 0 END), 0) AS nearest_matches,
            COALESCE(SUM(CASE WHEN building_match_type = 'none' THEN 1 ELSE 0 END), 0) AS no_matches,
            AVG(CASE
                    WHEN building_match_type IN ('nearest_polygon', 'nearest_centroid')
                    THEN building_distance_m
                    ELSE NULL
                END
            ) AS average_nearest_distance_m
        FROM enriched;
    """).fetchone()

    return {"total_rows": int(row[0]),
        "valid_coordinate_rows": int(row[1]),
        "inside_polygon_matches": int(row[2]),
        "nearest_matches": int(row[3]),
        "no_matches": int(row[4]),
        "average_nearest_distance_m": float(row[5]) if row[5] is not None else None,}


# ---------------------------------------------------------------------
# Main enrichment function
# ---------------------------------------------------------------------

def enrich_csv(db_path, csv_path, output_path, lat_col, lon_col,
               mode="inside_nearest", max_distance_m=50, fields=None):

    start = time.perf_counter()

    if mode not in {"inside_nearest", "inside", "centroid"}:
        raise ValueError("mode must be one of: inside_nearest, inside, centroid")

    csv_path = Path(csv_path)
    output_path = Path(output_path)

    con = open_db(db_path, read_only=True)
    con.execute(f"SET threads = {max(os.cpu_count() or 1, 1)};")

    try:
        check_lookup_db(con)

        if mode == "inside_nearest":
            check_inside_nearest_columns(con)

        scan_options = resolve_csv_scan_options(con, csv_path)
        csv_cols = csv_columns(con, csv_path, scan_options)

        if lat_col not in csv_cols:
            raise ValueError(f"Latitude column not found in CSV: {lat_col}")

        if lon_col not in csv_cols:
            raise ValueError(f"Longitude column not found in CSV: {lon_col}")

        available_fields = lookup_display_columns(con)

        if fields:
            selected_fields = list(dict.fromkeys(fields))
        else:
            selected_fields = [col for col in DEFAULT_BUILDING_COLUMNS
                if col in available_fields]

        unknown = [col for col in selected_fields if col not in available_fields]
        if unknown:
            raise ValueError(f"Unknown building field: {unknown[0]}\n"
                f"Available fields: {available_fields}")

        prefix_col, prefix_zoom, allow_null_prefix = enrichment_quadkey_config(con)

        query = enrichment_select_sql(csv_file=sql_string(str(csv_path.resolve())),
            scan_options=scan_options,
            lat_col=lat_col,
            lon_col=lon_col,
            mode=mode,
            radius_m=max_distance_m,
            original_cols=exposure_select(csv_cols),
            building_fields=selected_fields,
            prefix_col=prefix_col,
            prefix_zoom=prefix_zoom,
            allow_null_prefix=allow_null_prefix,)

        output_path.parent.mkdir(parents=True, exist_ok=True)

        print("Running enrichment query...")

        con.execute(f"""
            COPY ({query})
            TO {sql_string(str(output_path.resolve()))}
            (HEADER, DELIMITER ',');
        """)

        summary = summarize_output(con, output_path)
        summary["elapsed_seconds"] = round(time.perf_counter() - start, 2)
        summary["output_csv"] = str(output_path)

        return summary

    finally:
        con.close()


# ---------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------

def parse_fields(value):
    if not value:
        return None

    return [field.strip() for field in value.split(",") if field.strip()]


def main():
    parser = argparse.ArgumentParser(description="Enrich a CSV with OBM building attributes from a DuckDB lookup database.")

    parser.add_argument("--db", required=True, help="Prepared DuckDB lookup database")
    parser.add_argument("--csv", required=True, help="Input CSV")
    parser.add_argument("--out", required=True, help="Output enriched CSV")
    parser.add_argument("--lat-col", required=True, help="Latitude column name")
    parser.add_argument("--lon-col", required=True, help="Longitude column name")

    parser.add_argument("--mode",
        default="inside_nearest",
        choices=["inside_nearest", "inside", "centroid"],
        help="Matching method. Default: inside_nearest")

    parser.add_argument("--max-distance-m",
        type=float,
        default=50,
        help="Maximum nearest-building search distance in metres. Default: 50")

    parser.add_argument("--fields",
        default=None,
        help="Comma-separated building fields to append. If omitted, defaults are used.")

    args = parser.parse_args()

    if not Path(args.db).exists():
        raise SystemExit(f"DuckDB file not found: {args.db}")

    if not Path(args.csv).exists():
        raise SystemExit(f"CSV file not found: {args.csv}")

    print("Starting enrichment")
    print(f"Database: {args.db}")
    print(f"Input CSV: {args.csv}")
    print(f"Output CSV: {args.out}")
    print(f"Latitude column: {args.lat_col}")
    print(f"Longitude column: {args.lon_col}")
    print(f"Mode: {args.mode}")
    print(f"Radius: {args.max_distance_m} m")

    summary = enrich_csv(db_path=args.db,
        csv_path=args.csv,
        output_path=args.out,
        lat_col=args.lat_col,
        lon_col=args.lon_col,
        mode=args.mode,
        max_distance_m=args.max_distance_m,
        fields=parse_fields(args.fields),)

    print("\nDone")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
