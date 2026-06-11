# ============================================================
# INPUT
# ============================================================

import os
import time
from pathlib import Path

import duckdb


start_tic = time.time()

db_path = r"J:/cms/Internal/Territories/EMEA/01 Regions/Italy-Iberia/08 Tools/Data Augmentation - Height/GRe Database by Region/CARTO/All_Italy/Italy_combined.duckdb"

csv_input = r"J:/cms/Internal/Clients/Unipol/2026/2523309_UnipolSai_2026_Renewal/05 Model Input/RMS Input/EQ/Loc/Augmentation/EQ_RMS_IND_Loc.csv"

# parquet_input = r"J:/cms/Internal/Territories/EMEA/01 Regions/Italy-Iberia/08 Tools/Data Augmentation - Height/GRe Database by Region/CARTO/All_Italy/Italy_combined.parquet"
parquet_input = None

output_file = r"EQ_RMS_IND_Loc_enriched_verifica.csv"

lat_col = "LATITUDE"
lon_col = "LONGITUDE"

mode = "inside_nearest"   # inside_nearest | inside | centroid
max_distance_m = 30

building_fields = [
    "building_id",
    "height_m",
    "occupancy_code",
    "floorspace_est_m2",
    "year_built",
    "construction",
    "STOREY",
    "CITY",
]


# ============================================================
# CONSTANTS
# ============================================================

NEAREST_CANDIDATE_LIMIT = 500
DEFAULT_QUADKEY_PREFIX_ZOOM = 6
OPTIMIZED_QUADKEY_PREFIX_ZOOM = 14

DEFAULT_BUILDING_COLUMNS = [
    "building_id", "source", "relation_id", "last_update",
    "centroid_lon", "centroid_lat", "footprint_area_m2",
    "height_raw", "occupancy_raw", "floorspace_obm_m2",
    "height_source_type", "height_m", "stories_exact",
    "stories_min", "stories_max", "height_quality",
    "occupancy_code", "occupancy_group", "occupancy_quality",
    "floorspace_est_m2", "attribute_completeness_score",
]


# ============================================================
# UTILS
# ============================================================

def sql_id(x):
    return '"' + str(x).replace('"', '""') + '"'


def sql_str(x):
    return "'" + str(x).replace("'", "''") + "'"


def normalize_existing_path(path):
    return str(Path(path).resolve())


def normalize_output_path(path):
    return str(Path(path).resolve())


Path(output_file).parent.mkdir(parents=True, exist_ok=True)


# ============================================================
# DB
# ============================================================

def open_db(db_path, read_only=True):
    con = duckdb.connect(database=db_path, read_only=read_only)

    try:
        con.execute("LOAD spatial;")
    except Exception:
        con.execute("INSTALL spatial;")
        con.execute("LOAD spatial;")

    return con


# ============================================================
# CSV ENCODING DETECTION + FALLBACK STILE PYTHON
# ============================================================

def detect_csv_encoding(csv_path, sample_size=1048576):
    with open(csv_path, "rb") as f:
        raw_sample = f.read(sample_size)

    if (
        len(raw_sample) >= 3
        and raw_sample[0] == 0xEF
        and raw_sample[1] == 0xBB
        and raw_sample[2] == 0xBF
    ):
        return "utf-8-sig"

    encodings = ["utf-8", "windows-1252", "latin1"]

    for enc in encodings:
        try:
            raw_sample.decode(enc)
            return enc
        except Exception:
            pass

    return "latin1"


def duckdb_csv_encoding(csv_path):
    enc = detect_csv_encoding(csv_path)

    enc_low = enc.lower()

    if enc_low in ["utf-8-sig", "utf8-sig"]:
        return "utf-8"

    if enc_low in ["windows-1252", "cp1252", "latin1", "latin-1"]:
        return "latin-1"

    return "utf-8"


def duckdb_csv_encoding_candidates(csv_path, csv_encoding_override=None):
    detected = duckdb_csv_encoding(csv_path)

    candidates = [
        csv_encoding_override,
        detected,
        "utf-8",
        "latin-1",
    ]

    out = []
    for x in candidates:
        if x is not None and str(x).strip() != "" and x not in out:
            out.append(x)

    return out


def csv_scan_options(encoding):
    return (
        "sample_size = 20480, "
        "ignore_errors = true, "
        "header = true, "
        "all_varchar = true, "
        f"encoding = {sql_str(encoding)}"
    )


def resolve_csv_scan_options(con, csv_path, csv_encoding_override=None):
    csv_file = sql_str(normalize_existing_path(csv_path))
    errors = []

    for enc in duckdb_csv_encoding_candidates(csv_path, csv_encoding_override):
        opts = csv_scan_options(enc)

        try:
            con.execute(
                f"""
                SELECT *
                FROM read_csv_auto({csv_file}, {opts})
                LIMIT 1
                """
            ).fetchdf()

            print(f"✅ Using encoding: {enc}")

            return {
                "options": opts,
                "encoding": enc,
            }

        except Exception as e:
            errors.append(f"{enc}: {str(e)}")

    raise RuntimeError(
        "Could not find working encoding. Tried: "
        + " | ".join(errors)
    )


# ============================================================
# INPUT SOURCE
# ============================================================
# REGOLE:
# - se c'è csv_input, uso SEMPRE il CSV come exposure come il Python
# - parquet_input resta consentito ma viene usato solo se csv_input è NULL
# - db_path è sempre il lookup DB
# ============================================================

def resolve_input_source(db_path, csv_input=None, parquet_input=None, csv_encoding=None):
    has_csv = csv_input is not None and str(csv_input).strip() != ""
    has_parquet = parquet_input is not None and str(parquet_input).strip() != ""

    if not has_csv and not has_parquet:
        raise RuntimeError("Devi valorizzare almeno uno tra csv_input e parquet_input.")

    if has_csv and not Path(csv_input).exists():
        raise FileNotFoundError(f"CSV file not found: {csv_input}")

    if has_parquet and not Path(parquet_input).exists():
        raise FileNotFoundError(f"Parquet file not found: {parquet_input}")

    # PRIORITA' AL CSV per replicare il Python
    if has_csv:
        con_tmp = open_db(db_path, read_only=True)

        try:
            scan_info = resolve_csv_scan_options(
                con=con_tmp,
                csv_path=csv_input,
                csv_encoding_override=csv_encoding,
            )
        finally:
            con_tmp.close()

        csv_norm = normalize_existing_path(csv_input)

        return {
            "type": "csv",
            "path": csv_norm,
            "relation_sql": f"""
                read_csv_auto(
                    {sql_str(csv_norm)},
                    {scan_info["options"]}
                )
            """,
            "encoding_used": scan_info["encoding"],
        }

    parquet_norm = normalize_existing_path(parquet_input)

    return {
        "type": "parquet",
        "path": parquet_norm,
        "relation_sql": f"read_parquet({sql_str(parquet_norm)})",
        "encoding_used": None,
    }


# ============================================================
# LOOKUP DB CHECKS
# ============================================================

def is_internal_lookup_column(column_name, data_type):
    name = str(column_name).lower()
    dtype = str(data_type).lower()

    return (
        "geometry" in dtype
        or name.startswith("geom")
        or name.startswith("bbox")
        or name.startswith("quadkey")
    )


def lookup_display_columns(con):
    rows = con.execute(
        """
        SELECT column_name, data_type
        FROM information_schema.columns
        WHERE table_name = 'buildings'
        ORDER BY ordinal_position
        """
    ).fetchdf()

    out = []

    for _, row in rows.iterrows():
        if not is_internal_lookup_column(row["column_name"], row["data_type"]):
            out.append(row["column_name"])

    return out


def check_lookup_db(con):
    tables = con.execute(
        """
        SELECT table_name
        FROM information_schema.tables
        WHERE table_name = 'buildings'
        """
    ).fetchdf()

    if len(tables) == 0:
        raise RuntimeError("Il DuckDB non contiene la tabella 'buildings'.")

    required = [
        "geom",
        "centroid_lon",
        "centroid_lat",
        "bbox_xmin",
        "bbox_ymin",
        "bbox_xmax",
        "bbox_ymax",
    ]

    cols = con.execute(
        """
        SELECT column_name
        FROM information_schema.columns
        WHERE table_name = 'buildings'
        """
    ).fetchdf()["column_name"].tolist()

    missing = [x for x in required if x not in cols]

    if len(missing) > 0:
        raise RuntimeError(
            "Lookup DB missing required columns: "
            + ", ".join(missing)
        )


def check_inside_nearest_columns(con):
    required = [
        "geom_3035",
        "bbox_3035_xmin",
        "bbox_3035_ymin",
        "bbox_3035_xmax",
        "bbox_3035_ymax",
    ]

    cols = con.execute(
        """
        SELECT column_name
        FROM information_schema.columns
        WHERE table_name = 'buildings'
        """
    ).fetchdf()["column_name"].tolist()

    missing = [x for x in required if x not in cols]

    if len(missing) > 0:
        raise RuntimeError(
            "inside_nearest richiede colonne projected. Missing: "
            + ", ".join(missing)
        )


def enrichment_quadkey_config(con):
    cols = con.execute(
        """
        SELECT column_name
        FROM information_schema.columns
        WHERE table_name = 'buildings'
        """
    ).fetchdf()["column_name"].tolist()

    if "quadkey_prefix_14" in cols:
        prefix_col = "quadkey_prefix_14"
        zoom = OPTIMIZED_QUADKEY_PREFIX_ZOOM
    elif "quadkey_prefix_6" in cols:
        prefix_col = "quadkey_prefix_6"
        zoom = DEFAULT_QUADKEY_PREFIX_ZOOM
    else:
        raise RuntimeError("Lookup DB needs either quadkey_prefix_14 or quadkey_prefix_6.")

    has_null_prefixes = con.execute(
        f"""
        SELECT EXISTS (
            SELECT 1
            FROM buildings
            WHERE {sql_id(prefix_col)} IS NULL
            LIMIT 1
        ) AS has_nulls
        """
    ).fetchone()[0]

    return {
        "prefix_col": prefix_col,
        "zoom": zoom,
        "has_null_prefixes": bool(has_null_prefixes),
    }


# ============================================================
# INPUT TABLE INSPECTION
# ============================================================

def input_columns(con, relation_sql):
    rows = con.execute(
        f"""
        DESCRIBE
        SELECT *
        FROM {relation_sql}
        """
    ).fetchdf()

    return rows["column_name"].tolist()


def input_row_count(con, relation_sql):
    return con.execute(
        f"""
        SELECT COUNT(*) AS n
        FROM {relation_sql}
        """
    ).fetchone()[0]


# ============================================================
# AUTO-RISOLUZIONE COORDINATE
# ============================================================

def resolve_coord_col(requested, available, candidates=None):
    if candidates is None:
        candidates = []

    available_trim = [str(x).strip() for x in available]
    available_low = [x.lower() for x in available_trim]

    requested_low = str(requested).strip().lower()

    # 1) match esatto case-insensitive
    if requested_low in available_low:
        idx = available_low.index(requested_low)
        return available_trim[idx]

    # 2) match su candidati noti
    cand_low = [str(x).lower() for x in candidates]

    for idx, val in enumerate(available_low):
        if val in cand_low:
            return available_trim[idx]

    # 3) pattern loose
    for idx, val in enumerate(available_low):
        if requested_low in val:
            return available_trim[idx]

    return None


def resolve_lat_lon_columns(in_cols, lat_col, lon_col):
    lat_candidates = [lat_col, "latitude", "lat", "latitudine", "y"]
    lon_candidates = [lon_col, "longitude", "lon", "long", "longitudine", "x"]

    lat_real = resolve_coord_col(lat_col, in_cols, lat_candidates)
    lon_real = resolve_coord_col(lon_col, in_cols, lon_candidates)

    if lat_real is None or lon_real is None:
        raise RuntimeError(
            "Non trovo le colonne coordinate.\n"
            + "Colonne disponibili: "
            + ", ".join(in_cols)
        )

    return {
        "lat": lat_real,
        "lon": lon_real,
    }


# ============================================================
# SQL HELPERS
# ============================================================

def exposure_select(columns):
    return ",\n        ".join([f"e.{sql_id(x)}" for x in columns])


def b_select(alias, columns):
    return ",\n                ".join(
        [f"{alias}.{sql_id(x)} AS {sql_id(x)}" for x in columns]
    )


def final_building_select(source, columns):
    if len(columns) == 0:
        return ""

    fields = [
        f"{source}.{sql_id(x)} AS {sql_id('building_' + x)}"
        for x in columns
    ]

    return ",\n        " + ",\n        ".join(fields)


def final_coalesced_building_select(columns):
    if len(columns) == 0:
        return ""

    fields = [
        f"COALESCE(i.{sql_id(x)}, n.{sql_id(x)}) AS {sql_id('building_' + x)}"
        for x in columns
    ]

    return ",\n        " + ",\n        ".join(fields)


def quadkey_prefix_sql(tile_x_sql, tile_y_sql, zoom):
    digits = []

    for level in range(zoom, 0, -1):
        mask = 1 << (level - 1)

        digit = (
            "CAST(("
            f"(CASE WHEN (({tile_x_sql}) & {mask}) != 0 THEN 1 ELSE 0 END)"
            " + "
            f"(CASE WHEN (({tile_y_sql}) & {mask}) != 0 THEN 2 ELSE 0 END)"
            ") AS VARCHAR)"
        )

        digits.append(digit)

    return "CONCAT(" + ", ".join(digits) + ")"


# ============================================================
# QUERY BUILDER
# ============================================================

def build_enrichment_query(
    relation_sql,
    lat_col,
    lon_col,
    mode,
    radius_m,
    original_cols,
    building_fields,
    prefix_col,
    prefix_zoom,
    allow_null_prefix,
):
    radius_sql = str(float(radius_m))
    tile_count = 1 << prefix_zoom
    max_tile = tile_count - 1

    quadkey_expr = quadkey_prefix_sql("tile_x", "tile_y", prefix_zoom)
    prefix_id = sql_id(prefix_col)

    join_on_quadkey = f"b.{prefix_id} = t.__quadkey_prefix"

    if allow_null_prefix:
        join_on_quadkey = (
            "("
            + join_on_quadkey
            + f" OR (b.{prefix_id} IS NULL AND t.__is_primary_tile))"
        )

    working_fields = []

    for x in ["building_id"] + list(building_fields):
        if x not in working_fields:
            working_fields.append(x)

    ranked_fields = ",\n                ".join([sql_id(x) for x in working_fields])

    nearest_fields = ",\n                ".join(
        [f"c.{sql_id(x)} AS {sql_id(x)}" for x in working_fields]
    )

    distance_columns = ""

    if mode in ["centroid", "inside_nearest"]:
        distance_columns = (
            f",\n            {radius_sql} / 111320.0 AS __lat_delta"
            f",\n            {radius_sql} / (111320.0 * __cos_lat) AS __lon_delta"
        )

    if mode == "inside_nearest":
        projected_ctes = """
    exposure_projected AS (
        SELECT
            *,
            CASE
                WHEN __valid_coordinates
                THEN ST_Transform(__pt, 'EPSG:4326', 'EPSG:3035', always_xy := true)
                ELSE NULL
            END AS __pt_m
        FROM exposure_base
    ),
    exposure AS (
        SELECT
            *,
            CASE WHEN __pt_m IS NOT NULL THEN ST_X(__pt_m) ELSE NULL END AS __pt_m_x,
            CASE WHEN __pt_m IS NOT NULL THEN ST_Y(__pt_m) ELSE NULL END AS __pt_m_y
        FROM exposure_projected
    )
    """
    else:
        projected_ctes = """
    exposure AS (
        SELECT *
        FROM exposure_base
    )
    """

    base_ctes = f"""
WITH exposure_raw AS (
    SELECT
        ROW_NUMBER() OVER () AS __exposure_row_id,
        *
    FROM {relation_sql}
),
exposure_parsed AS (
    SELECT
        *,
        TRY_CAST({sql_id(lon_col)} AS DOUBLE) AS __lon,
        TRY_CAST({sql_id(lat_col)} AS DOUBLE) AS __lat
    FROM exposure_raw
),
exposure_base AS (
    SELECT
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
            THEN LEAST(
                GREATEST(CAST(FLOOR((__lon + 180.0) / 360.0 * {tile_count}) AS BIGINT), 0),
                {max_tile}
            )
            ELSE NULL
        END AS __tile_x,

        CASE
            WHEN __lon BETWEEN -180 AND 180 AND __lat BETWEEN -90 AND 90
            THEN LEAST(
                GREATEST(
                    CAST(
                        FLOOR(
                            (
                                0.5
                                - LN(
                                    (
                                        1 + SIN(RADIANS(LEAST(GREATEST(__lat, -85.05112878), 85.05112878)))
                                    ) /
                                    (
                                        1 - SIN(RADIANS(LEAST(GREATEST(__lat, -85.05112878), 85.05112878)))
                                    )
                                ) / (4 * PI())
                            ) * {tile_count}
                        ) AS BIGINT
                    ),
                    0
                ),
                {max_tile}
            )
            ELSE NULL
        END AS __tile_y
        {distance_columns}
    FROM exposure_parsed
),
{projected_ctes},
exposure_tiles AS (
    SELECT
        t.__exposure_row_id,
        {quadkey_expr} AS __quadkey_prefix,
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
    WHERE t.tile_x BETWEEN 0 AND {max_tile}
      AND t.tile_y BETWEEN 0 AND {max_tile}
)
"""

    if mode == "centroid":
        return f"""
{base_ctes},
centroid_candidates AS (
    SELECT
        e.__exposure_row_id,
        ST_Distance_Sphere(ST_Point(b.centroid_lon, b.centroid_lat), e.__pt) AS distance_m,
        ROW_NUMBER() OVER (
            PARTITION BY e.__exposure_row_id
            ORDER BY ST_Distance_Sphere(ST_Point(b.centroid_lon, b.centroid_lat), e.__pt)
        ) AS candidate_rank,
        {b_select("b", working_fields)}
    FROM exposure e
    JOIN exposure_tiles t USING (__exposure_row_id)
    JOIN buildings b
      ON {join_on_quadkey}
     AND b.centroid_lon BETWEEN e.__lon - e.__lon_delta AND e.__lon + e.__lon_delta
     AND b.centroid_lat BETWEEN e.__lat - e.__lat_delta AND e.__lat + e.__lat_delta
),
centroid_ranked AS (
    SELECT
        __exposure_row_id,
        distance_m,
        ROW_NUMBER() OVER (
            PARTITION BY __exposure_row_id
            ORDER BY distance_m
        ) AS rn,
        {ranked_fields}
    FROM centroid_candidates
    WHERE candidate_rank <= {NEAREST_CANDIDATE_LIMIT}
      AND distance_m <= {radius_sql}
),
matches AS (
    SELECT *
    FROM centroid_ranked
    WHERE rn = 1
)
SELECT
    {exposure_select(original_cols)},
    e.__valid_coordinates AS coordinate_valid,
    CASE
        WHEN m.__exposure_row_id IS NOT NULL THEN 'nearest_centroid'
        ELSE 'none'
    END AS building_match_type,
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
inside_ranked AS (
    SELECT
        e.__exposure_row_id,
        ROW_NUMBER() OVER (
            PARTITION BY e.__exposure_row_id
            ORDER BY b.footprint_area_m2 ASC NULLS LAST
        ) AS rn,
        {b_select("b", working_fields)}
    FROM exposure e
    JOIN exposure_tiles t USING (__exposure_row_id)
    JOIN buildings b
      ON {join_on_quadkey}
     AND e.__lon BETWEEN b.bbox_xmin AND b.bbox_xmax
     AND e.__lat BETWEEN b.bbox_ymin AND b.bbox_ymax
     AND ST_Intersects(b.geom, e.__pt)
),
matches AS (
    SELECT *
    FROM inside_ranked
    WHERE rn = 1
)
SELECT
    {exposure_select(original_cols)},
    e.__valid_coordinates AS coordinate_valid,
    CASE
        WHEN m.__exposure_row_id IS NOT NULL THEN 'inside_polygon'
        ELSE 'none'
    END AS building_match_type,
    CASE
        WHEN m.__exposure_row_id IS NOT NULL THEN 0.0
        ELSE NULL
    END AS building_distance_m,
    CASE
        WHEN m.__exposure_row_id IS NOT NULL THEN 'high'
        ELSE 'none'
    END AS building_confidence
    {final_building_select("m", building_fields)}
FROM exposure e
LEFT JOIN matches m USING (__exposure_row_id)
ORDER BY e.__exposure_row_id
"""

    return f"""
{base_ctes},
inside_ranked AS (
    SELECT
        e.__exposure_row_id,
        ROW_NUMBER() OVER (
            PARTITION BY e.__exposure_row_id
            ORDER BY b.footprint_area_m2 ASC NULLS LAST
        ) AS rn,
        {b_select("b", working_fields)}
    FROM exposure e
    JOIN exposure_tiles t USING (__exposure_row_id)
    JOIN buildings b
      ON {join_on_quadkey}
     AND e.__lon BETWEEN b.bbox_xmin AND b.bbox_xmax
     AND e.__lat BETWEEN b.bbox_ymin AND b.bbox_ymax
     AND ST_Intersects(b.geom, e.__pt)
),
inside_matches AS (
    SELECT *
    FROM inside_ranked
    WHERE rn = 1
),
unmatched_exposure AS (
    SELECT e.*
    FROM exposure e
    LEFT JOIN inside_matches i USING (__exposure_row_id)
    WHERE e.__valid_coordinates
      AND i.__exposure_row_id IS NULL
),
nearest_candidates AS (
    SELECT
        e.__exposure_row_id,
        b.geom_3035 AS __geom_3035,
        ST_Distance_Sphere(ST_Point(b.centroid_lon, b.centroid_lat), e.__pt) AS centroid_distance_m,
        ROW_NUMBER() OVER (
            PARTITION BY e.__exposure_row_id
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
     AND b.bbox_3035_ymax >= e.__pt_m_y - {radius_sql}
),
nearest_ranked AS (
    SELECT
        c.__exposure_row_id,
        ST_Distance(c.__geom_3035, e.__pt_m) AS distance_m,
        ROW_NUMBER() OVER (
            PARTITION BY c.__exposure_row_id
            ORDER BY ST_Distance(c.__geom_3035, e.__pt_m)
        ) AS rn,
        {nearest_fields}
    FROM nearest_candidates c
    JOIN unmatched_exposure e USING (__exposure_row_id)
    WHERE c.candidate_rank <= {NEAREST_CANDIDATE_LIMIT}
      AND ST_DWithin(c.__geom_3035, e.__pt_m, {radius_sql})
),
nearest_matches AS (
    SELECT *
    FROM nearest_ranked
    WHERE rn = 1
)
SELECT
    {exposure_select(original_cols)},
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


# ============================================================
# SUMMARY
# ============================================================

def summarize_output(con, output_file):
    output_sql = sql_str(normalize_existing_path(output_file))

    row = con.execute(
        f"""
        WITH enriched AS (
            SELECT
                TRY_CAST(coordinate_valid AS BOOLEAN) AS coordinate_valid,
                CAST(building_match_type AS VARCHAR) AS building_match_type,
                TRY_CAST(building_distance_m AS DOUBLE) AS building_distance_m
            FROM read_csv_auto(
                {output_sql},
                header = true,
                sample_size = 20480,
                all_varchar = true
            )
        )
        SELECT
            COUNT(*) AS total_rows,
            COALESCE(SUM(CASE WHEN coordinate_valid THEN 1 ELSE 0 END), 0) AS valid_coordinate_rows,
            COALESCE(SUM(CASE WHEN building_match_type = 'inside_polygon' THEN 1 ELSE 0 END), 0) AS inside_polygon_matches,
            COALESCE(SUM(CASE WHEN building_match_type IN ('nearest_polygon', 'nearest_centroid') THEN 1 ELSE 0 END), 0) AS nearest_matches,
            COALESCE(SUM(CASE WHEN building_match_type = 'none' THEN 1 ELSE 0 END), 0) AS no_matches,
            AVG(
                CASE
                    WHEN building_match_type IN ('nearest_polygon', 'nearest_centroid')
                    THEN building_distance_m
                    ELSE NULL
                END
            ) AS average_nearest_distance_m
        FROM enriched
        """
    ).fetchdf()

    return row.iloc[0].to_dict()


# ============================================================
# MAIN
# ============================================================

def enrich_input(
    db_path,
    csv_input=None,
    parquet_input=None,
    output_file=None,
    lat_col=None,
    lon_col=None,
    mode="inside_nearest",
    max_distance_m=50,
    building_fields=None,
    csv_encoding=None,
):
    start_time = time.time()

    if not Path(db_path).exists():
        raise FileNotFoundError(f"DuckDB file not found: {db_path}")

    if mode not in ["inside_nearest", "inside", "centroid"]:
        raise RuntimeError("mode must be one of: inside_nearest, inside, centroid")

    source_info = resolve_input_source(
        db_path=db_path,
        csv_input=csv_input,
        parquet_input=parquet_input,
        csv_encoding=csv_encoding,
    )

    con = open_db(db_path, read_only=True)

    try:
        threads = max(os.cpu_count() or 1, 1)
        con.execute(f"SET threads = {threads};")

        check_lookup_db(con)

        if mode == "inside_nearest":
            check_inside_nearest_columns(con)

        in_cols = input_columns(con, source_info["relation_sql"])
        input_n = input_row_count(con, source_info["relation_sql"])

        coords = resolve_lat_lon_columns(
            in_cols=in_cols,
            lat_col=lat_col,
            lon_col=lon_col,
        )

        lat_real = coords["lat"]
        lon_real = coords["lon"]

        print(f"Using latitude column: {lat_real}")
        print(f"Using longitude column: {lon_real}")

        if source_info["encoding_used"] is not None:
            print(f"Encoding used: {source_info['encoding_used']}")

        print(f"Input rows read by DuckDB: {input_n}")

        available_fields = lookup_display_columns(con)

        if building_fields is None or len(building_fields) == 0:
            selected_fields = [
                x for x in DEFAULT_BUILDING_COLUMNS
                if x in available_fields
            ]
        else:
            selected_fields = []

            for x in building_fields:
                if x not in selected_fields:
                    selected_fields.append(x)

        unknown = [x for x in selected_fields if x not in available_fields]

        if len(unknown) > 0:
            raise RuntimeError(
                f"Unknown building field: {unknown[0]}\n"
                + "Available fields: "
                + ", ".join(available_fields)
            )

        qk = enrichment_quadkey_config(con)

        query = build_enrichment_query(
            relation_sql=source_info["relation_sql"],
            lat_col=lat_real,
            lon_col=lon_real,
            mode=mode,
            radius_m=max_distance_m,
            original_cols=in_cols,
            building_fields=selected_fields,
            prefix_col=qk["prefix_col"],
            prefix_zoom=qk["zoom"],
            allow_null_prefix=qk["has_null_prefixes"],
        )

        Path(output_file).parent.mkdir(parents=True, exist_ok=True)

        print("Running enrichment query...")

        output_norm = normalize_output_path(output_file)

        con.execute(
            f"""
            COPY ({query})
            TO {sql_str(output_norm)}
            (HEADER, DELIMITER ',')
            """
        )

        summary = summarize_output(con, output_file)

        summary["input_rows_read"] = input_n
        summary["row_delta"] = summary["total_rows"] - input_n
        summary["elapsed_seconds"] = round(time.time() - start_time, 2)
        summary["output_csv"] = normalize_output_path(output_file)
        summary["input_type"] = source_info["type"]
        summary["input_path"] = source_info["path"]
        summary["encoding_used"] = source_info["encoding_used"]
        summary["lat_used"] = lat_real
        summary["lon_used"] = lon_real

        return summary

    finally:
        con.close()


# ============================================================
# RUN
# ============================================================

print("Starting enrichment")
print(f"DuckDB: {db_path}")
print(f"CSV input: {'NULL' if csv_input is None else csv_input}")
print(f"Parquet input: {'NULL' if parquet_input is None else parquet_input}")
print(f"Output: {output_file}")
print(f"Latitude requested: {lat_col}")
print(f"Longitude requested: {lon_col}")
print(f"Mode: {mode}")
print(f"Radius: {max_distance_m} m")

result = enrich_input(
    db_path=db_path,
    csv_input=csv_input,
    parquet_input=parquet_input,
    output_file=output_file,
    lat_col=lat_col,
    lon_col=lon_col,
    mode=mode,
    max_distance_m=max_distance_m,
    building_fields=building_fields,
    csv_encoding=None,   # se vuoi forzare: "utf-8" oppure "latin-1"
)

print("\nDone")
print(result)

print(f"{round(time.time() - start_tic, 2)} sec elapsed")
