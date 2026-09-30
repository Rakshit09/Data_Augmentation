import math
import re
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import duckdb


EXPOSURE_BIN_VERSION = "3"
EXPOSURE_ROW_TABLE_VERSION = "2"
EXPOSURE_BIN_ZOOMS: Tuple[int, ...] = (6, 8, 10, 12, 14, 16)
RAW_POINT_ZOOM = 10.0
DUPLICATE_SPREAD_ZOOM = 18.0
MAX_MERCATOR_LAT = 85.05112878
EARTH_RADIUS_EQUATOR_PX = 156543.03392


def _table_exists(con: duckdb.DuckDBPyConnection, table_name: str) -> bool:
    row = con.execute(
        """
        SELECT COUNT(*)
        FROM information_schema.tables
        WHERE table_schema = 'main'
            AND table_name = ?;
        """,
        [table_name],
    ).fetchone()
    return bool(row and row[0])


def sql_string(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def sql_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _bin_table_name(zoom: int) -> str:
    return f"exposure_bins_z{zoom}"


def _cache_is_current(con: duckdb.DuckDBPyConnection) -> bool:
    if not _table_exists(con, "exposure_multires_metadata"):
        return False

    row = con.execute(
        "SELECT value FROM exposure_multires_metadata WHERE key = 'version';"
    ).fetchone()
    if not row or str(row[0]) != EXPOSURE_BIN_VERSION:
        return False

    return all(_table_exists(con, _bin_table_name(zoom)) for zoom in EXPOSURE_BIN_ZOOMS)


def build_exposure_multires_tables(con: duckdb.DuckDBPyConnection) -> None:
    """Build fixed WebMercator bins once so viewport queries stay cheap."""
    if _cache_is_current(con):
        return

    for zoom in EXPOSURE_BIN_ZOOMS:
        con.execute(f"DROP TABLE IF EXISTS {_bin_table_name(zoom)};")

    con.execute("CREATE INDEX IF NOT EXISTS points_lon_lat_idx ON points(lon, lat);")

    # Project the raw coordinates only once at the finest resolution. Coarser
    # tables are exact count-preserving rollups of that table, avoiding five
    # additional full scans and repeated trigonometry on large uploads.
    finest_zoom = EXPOSURE_BIN_ZOOMS[-1]
    finest_table = _bin_table_name(finest_zoom)
    tile_count = 2 ** finest_zoom
    max_tile = tile_count - 1
    con.execute(f"""
        CREATE TABLE {finest_table} AS
        WITH projected AS (
            SELECT
                row_id,
                lon,
                lat,
                LEAST(
                    GREATEST(
                        CAST(FLOOR(((lon + 180.0) / 360.0) * {tile_count}) AS BIGINT),
                        0
                    ),
                    {max_tile}
                ) AS tile_x,
                LEAST(
                    GREATEST(
                        CAST(FLOOR((
                            0.5 - LN((1.0 + sin_lat) / (1.0 - sin_lat)) / (4.0 * PI())
                        ) * {tile_count}) AS BIGINT),
                        0
                    ),
                    {max_tile}
                ) AS tile_y
            FROM (
                SELECT
                    row_id,
                    lon,
                    lat,
                    SIN(RADIANS(LEAST(GREATEST(lat, {-MAX_MERCATOR_LAT}), {MAX_MERCATOR_LAT}))) AS sin_lat
                FROM points
            ) AS p
        )
        SELECT
            tile_x,
            tile_y,
            MIN(row_id) AS row_id,
            ARG_MIN(lon, row_id) AS lon,
            ARG_MIN(lat, row_id) AS lat,
            COUNT(*) AS csv_count
        FROM projected
        GROUP BY tile_x, tile_y;
    """)
    con.execute(f"CREATE INDEX {finest_table}_xy_idx ON {finest_table}(tile_x, tile_y);")

    source_zoom = finest_zoom
    for zoom in reversed(EXPOSURE_BIN_ZOOMS[:-1]):
        table_name = _bin_table_name(zoom)
        source_table = _bin_table_name(source_zoom)
        scale = 2 ** (source_zoom - zoom)
        con.execute(f"""
            CREATE TABLE {table_name} AS
            WITH rolled AS (
                SELECT
                    CAST(FLOOR(tile_x / {scale}) AS BIGINT) AS tile_x,
                    CAST(FLOOR(tile_y / {scale}) AS BIGINT) AS tile_y,
                    row_id,
                    lon,
                    lat,
                    csv_count
                FROM {source_table}
            )
            SELECT
                tile_x,
                tile_y,
                MIN(row_id) AS row_id,
                ARG_MIN(lon, row_id) AS lon,
                ARG_MIN(lat, row_id) AS lat,
                SUM(csv_count) AS csv_count
            FROM rolled
            GROUP BY tile_x, tile_y;
        """)
        con.execute(f"CREATE INDEX {table_name}_xy_idx ON {table_name}(tile_x, tile_y);")
        source_zoom = zoom

    con.execute("""
        CREATE TABLE IF NOT EXISTS exposure_multires_metadata(
            key VARCHAR PRIMARY KEY,
            value VARCHAR
        );
    """)
    con.execute("DELETE FROM exposure_multires_metadata;")
    con.executemany(
        "INSERT INTO exposure_multires_metadata VALUES (?, ?);",
        [
            ("version", EXPOSURE_BIN_VERSION),
            ("zooms", ",".join(str(zoom) for zoom in EXPOSURE_BIN_ZOOMS)),
        ],
    )


def build_exposure_row_table(
    con: duckdb.DuckDBPyConnection,
    source_path: Path,
    columns: List[str],
    scan_options_sql: str | None = None,
) -> None:
    con.execute("DROP TABLE IF EXISTS csv_rows;")
    con.execute("DROP TABLE IF EXISTS csv_row_columns;")
    con.execute("""
        CREATE TABLE csv_row_columns(
            position INTEGER,
            column_name VARCHAR,
            storage_name VARCHAR
        );
    """)

    storage_rows = []
    selected_columns = []
    for index, column in enumerate(columns):
        storage_name = f"c{index}"
        storage_rows.append((index, str(column), storage_name))
        selected_columns.append(
            f"CAST(source.{sql_identifier(str(column))} AS VARCHAR) AS {sql_identifier(storage_name)}"
        )

    con.executemany("INSERT INTO csv_row_columns VALUES (?, ?, ?);", storage_rows)

    if source_path.suffix.lower() == ".xlsx":
        from_sql = f"read_xlsx({sql_string(str(source_path.resolve()))}, header := true, all_varchar := true)"
    else:
        csv_sql = sql_string(str(source_path.resolve()))
        if scan_options_sql is None:
            raise ValueError("scan_options_sql is required for CSV files")
        from_sql = f"read_csv_auto({csv_sql}, {scan_options_sql})"

    con.execute(f"""
        CREATE TABLE csv_rows AS
        SELECT
            row_number() OVER () AS row_id,
            {", ".join(selected_columns)}
        FROM {from_sql} AS source;
    """)
    con.execute("CREATE UNIQUE INDEX csv_rows_row_id_idx ON csv_rows(row_id);")

    con.execute("""
        CREATE TABLE IF NOT EXISTS exposure_row_metadata(
            key VARCHAR PRIMARY KEY,
            value VARCHAR
        );
    """)
    con.execute("DELETE FROM exposure_row_metadata;")
    con.executemany(
        "INSERT INTO exposure_row_metadata VALUES (?, ?);",
        [("version", EXPOSURE_ROW_TABLE_VERSION)],
    )


def exposure_row_table_is_current(con: duckdb.DuckDBPyConnection) -> bool:
    if not _table_exists(con, "csv_rows") or not _table_exists(con, "csv_row_columns"):
        return False
    if not _table_exists(con, "exposure_row_metadata"):
        return False

    row = con.execute(
        "SELECT value FROM exposure_row_metadata WHERE key = 'version';"
    ).fetchone()
    return bool(row and str(row[0]) == EXPOSURE_ROW_TABLE_VERSION)


def lookup_exposure_row(cache_path: Path, row_id: int) -> Dict[str, Any] | None:
    con = duckdb.connect(str(cache_path), read_only=True)
    try:
        if not exposure_row_table_is_current(con):
            raise RuntimeError("Exposure row details are not available for this cache.")

        column_rows = con.execute("""
            SELECT column_name, storage_name
            FROM csv_row_columns
            ORDER BY position;
        """).fetchall()
        storage_names = [str(storage_name) for _column_name, storage_name in column_rows]
        select_sql = ", ".join(sql_identifier(name) for name in storage_names)
        row = con.execute(
            f"SELECT {select_sql} FROM csv_rows WHERE row_id = ?;",
            [int(row_id)],
        ).fetchone()
        if row is None:
            return None

        point_row = con.execute(
            "SELECT lon, lat FROM points WHERE row_id = ?;",
            [int(row_id)],
        ).fetchone()
        metadata_rows = con.execute("SELECT key, value FROM metadata;").fetchall()
    finally:
        con.close()

    values = {
        str(column_name): "" if row[index] is None else str(row[index])
        for index, (column_name, _storage_name) in enumerate(column_rows)
    }
    metadata = {str(key): value for key, value in metadata_rows}
    lon = float(point_row[0]) if point_row else None
    lat = float(point_row[1]) if point_row else None
    return {
        "row_id": int(row_id),
        "filename": str(metadata.get("filename") or ""),
        "lon": lon,
        "lat": lat,
        "values": values,
    }


def ensure_exposure_multires_cache(cache_path: Path) -> None:
    con = duckdb.connect(str(cache_path))
    try:
        build_exposure_multires_tables(con)
        con.execute("CHECKPOINT;")
    finally:
        con.close()


def _clamp_bounds(
    min_lon: float,
    min_lat: float,
    max_lon: float,
    max_lat: float,
) -> Tuple[float, float, float, float]:
    clamped_min_lon = max(-180.0, min(180.0, min_lon))
    clamped_max_lon = max(-180.0, min(180.0, max_lon))
    clamped_min_lat = max(-90.0, min(90.0, min_lat))
    clamped_max_lat = max(-90.0, min(90.0, max_lat))
    min_lon, max_lon = sorted((clamped_min_lon, clamped_max_lon))
    min_lat, max_lat = sorted((clamped_min_lat, clamped_max_lat))
    return min_lon, min_lat, max_lon, max_lat


def _lon_lat_to_tile(lon: float, lat: float, zoom: int) -> Tuple[int, int]:
    tile_count = 2 ** zoom
    max_tile = tile_count - 1
    clamped_lat = max(-MAX_MERCATOR_LAT, min(MAX_MERCATOR_LAT, lat))
    x = math.floor(((lon + 180.0) / 360.0) * tile_count)
    sin_lat = math.sin(math.radians(clamped_lat))
    y = math.floor((0.5 - math.log((1.0 + sin_lat) / (1.0 - sin_lat)) / (4.0 * math.pi)) * tile_count)
    return max(0, min(max_tile, x)), max(0, min(max_tile, y))


def _tile_range_for_bounds(
    min_lon: float,
    min_lat: float,
    max_lon: float,
    max_lat: float,
    zoom: int,
) -> Tuple[int, int, int, int]:
    min_x, max_y = _lon_lat_to_tile(min_lon, min_lat, zoom)
    max_x, min_y = _lon_lat_to_tile(max_lon, max_lat, zoom)
    min_x, max_x = sorted((min_x, max_x))
    min_y, max_y = sorted((min_y, max_y))
    return min_x, max_x, min_y, max_y


def _select_source_zoom(
    view_zoom: float,
    min_lon: float,
    min_lat: float,
    max_lon: float,
    max_lat: float,
    max_features: int,
) -> int:
    if not math.isfinite(view_zoom):
        view_zoom = 0.0

    # A bin five zoom levels above the map is roughly 16 screen pixels wide.
    # That is detailed enough to look like the point layer while keeping a
    # whole viewport to a few thousand stable representatives.
    target_zoom = min(EXPOSURE_BIN_ZOOMS[-1], max(0, int(math.floor(view_zoom + 5))))

    candidates = [zoom for zoom in EXPOSURE_BIN_ZOOMS if zoom <= target_zoom]
    tile_budget = max(25_000, int(max_features) * 8)

    for zoom in reversed(candidates):
        min_x, max_x, min_y, max_y = _tile_range_for_bounds(min_lon, min_lat, max_lon, max_lat, zoom)
        tile_span = (max_x - min_x + 1) * (max_y - min_y + 1)
        if tile_span <= tile_budget:
            return zoom

    return EXPOSURE_BIN_ZOOMS[0]


def _view_grid(width: int, height: int, max_features: int) -> Tuple[int, int]:
    safe_width = max(320, min(3840, int(width or 1200)))
    safe_height = max(240, min(2160, int(height or 800)))
    safe_max = max(500, int(max_features or 12000))
    safe_max = min(safe_max, 3500)

    # Keep the overview visibly aggregated. A coarser 18 px cell and a lower
    # feature cap stop low-zoom views from looking like the raw point layer.
    cols = max(18, min(180, math.ceil(safe_width / 18)))
    rows = max(14, min(140, math.ceil(safe_height / 18)))
    cell_count = cols * rows

    if cell_count > safe_max:
        scale = math.sqrt(safe_max / cell_count)
        cols = max(18, int(cols * scale))
        rows = max(14, int(rows * scale))

    return cols, rows


def _count_label(value: int) -> str:
    if value < 1000:
        return str(value)
    if value < 1_000_000:
        label = f"{value / 1000:.1f}".rstrip("0").rstrip(".")
        return f"{label}k"
    label = f"{value / 1_000_000:.1f}".rstrip("0").rstrip(".")
    return f"{label}m"


def _offset_duplicate_coordinate(
    lon: float,
    lat: float,
    duplicate_index: int,
    duplicate_count: int,
    zoom: float,
) -> Tuple[float, float]:
    if duplicate_count <= 1 or duplicate_index <= 0 or not math.isfinite(zoom):
        return lon, lat

    lat_rad = math.radians(max(-MAX_MERCATOR_LAT, min(MAX_MERCATOR_LAT, lat)))
    meters_per_pixel = EARTH_RADIUS_EQUATOR_PX * max(0.1, math.cos(lat_rad)) / (2 ** max(0.0, zoom))
    radius_m = min(2.0, max(0.15, meters_per_pixel * 7.0))
    angle = (2.0 * math.pi * (duplicate_index - 1)) / max(1, duplicate_count - 1)

    lat_degree_m = 111_320.0
    lon_degree_m = max(1.0, lat_degree_m * max(0.1, math.cos(lat_rad)))
    return (
        lon + (math.cos(angle) * radius_m / lon_degree_m),
        lat + (math.sin(angle) * radius_m / lat_degree_m),
    )


_DECIMAL_TAG_PATTERN = re.compile(r"^[+-]?(?:\d+\.\d*|\.\d+|\d+(?=[eE]))(?:[eE][+-]?\d+)?$")


def _format_tag_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        number = value
    else:
        text = str(value).strip()
        # Plain integer strings (IDs, postcodes with leading zeros) are left untouched.
        if not _DECIMAL_TAG_PATTERN.match(text):
            return str(value)
        number = float(text)
    if not math.isfinite(number):
        return str(value)
    formatted = f"{number:.2f}".rstrip("0").rstrip(".")
    return "0" if formatted in ("-0", "") else formatted


MAX_STACKED_TAGS = 8


def _stacked_tag_labels(
    con: duckdb.DuckDBPyConnection,
    rows: List[Tuple[Any, ...]],
    tag_storage_name: str | None,
    row_filter_sql: str = "",
) -> Dict[Tuple[float, float], str]:
    """Newline-joined tag lists for coordinates shared by more than one row."""
    if not tag_storage_name:
        return {}
    targets = {
        (float(row[1]), float(row[2]))
        for row in rows
        if int(row[3] or 0) > 1 or int(row[7] or 1) > 1
    }
    if not targets:
        return {}

    tag_sql = f"csv_row.{sql_identifier(tag_storage_name)}"
    lons, lats = zip(*targets)
    tag_rows = con.execute(
        f"""
        WITH targets AS (
            SELECT UNNEST(?::DOUBLE[]) AS lon, UNNEST(?::DOUBLE[]) AS lat
        ),
        matched AS (
            SELECT row_id, lon, lat
            FROM points
            JOIN targets USING (lon, lat)
            WHERE TRUE{row_filter_sql}
        ),
        ranked AS (
            SELECT
                matched.lon,
                matched.lat,
                matched.row_id,
                {tag_sql} AS tag,
                ROW_NUMBER() OVER (PARTITION BY matched.lon, matched.lat ORDER BY matched.row_id) AS tag_rank,
                COUNT(*) OVER (PARTITION BY matched.lon, matched.lat) AS tag_total
            FROM matched
            JOIN csv_rows AS csv_row ON csv_row.row_id = matched.row_id
            WHERE NULLIF(TRIM({tag_sql}), '') IS NOT NULL
        )
        SELECT lon, lat, tag, tag_total
        FROM ranked
        WHERE tag_rank <= ?
        ORDER BY lon, lat, row_id;
        """,
        [list(lons), list(lats), MAX_STACKED_TAGS],
    ).fetchall()

    lines: Dict[Tuple[float, float], List[str]] = {}
    totals: Dict[Tuple[float, float], int] = {}
    for lon, lat, tag, tag_total in tag_rows:
        key = (float(lon), float(lat))
        lines.setdefault(key, []).append(_format_tag_value(tag))
        totals[key] = int(tag_total)

    labels: Dict[Tuple[float, float], str] = {}
    for key, values in lines.items():
        total = totals[key]
        if total <= 1:
            continue
        if total > len(values):
            values.append(f"+{total - len(values)} more")
        labels[key] = "\n".join(values)
    return labels


def _features_from_rows(
    rows: Iterable[Tuple[Any, ...]],
    zoom: float = 0.0,
    separate_duplicates: bool = False,
    stacked_tags: Dict[Tuple[float, float], str] | None = None,
) -> List[Dict[str, Any]]:
    stacked_tags = stacked_tags or {}
    # Only one feature per shared coordinate carries the stacked label.
    stack_anchor_index: Dict[Tuple[float, float], int] = {}
    if stacked_tags:
        for row in rows:
            key = (float(row[1]), float(row[2]))
            if key in stacked_tags:
                index = int(row[6] or 0)
                stack_anchor_index[key] = min(index, stack_anchor_index.get(key, index))

    features: List[Dict[str, Any]] = []
    for row in rows:
        row_id, lon, lat, csv_count, _visible_count, _cell_count, duplicate_index, duplicate_count, tag_value, color_match = row
        display_lon = float(lon)
        display_lat = float(lat)
        duplicate_index = int(duplicate_index or 0)
        duplicate_count = int(duplicate_count or 1)
        coordinate_key = (display_lon, display_lat)
        if coordinate_key in stacked_tags:
            is_anchor = stack_anchor_index.get(coordinate_key) == duplicate_index
            tag_value = stacked_tags[coordinate_key] if is_anchor else ""
        else:
            tag_value = _format_tag_value(tag_value)
        color_match = 1 if int(color_match or 0) > 0 else 0
        if separate_duplicates:
            display_lon, display_lat = _offset_duplicate_coordinate(
                display_lon,
                display_lat,
                duplicate_index,
                duplicate_count,
                zoom,
            )

        count = int(csv_count or 0)
        properties = {
            "row_id": int(row_id),
            "csv_count": count,
            "csv_label": _count_label(count),
            "duplicate_count": duplicate_count,
            "csv_color_match": color_match,
        }
        if tag_value:
            properties["csv_tag"] = tag_value
        features.append({
            "type": "Feature",
            "geometry": {
                "type": "Point",
                "coordinates": [display_lon, display_lat],
            },
            "properties": properties,
        })
    return features


def _csv_row_storage_name(
    con: duckdb.DuckDBPyConnection,
    column_name: str | None,
) -> Optional[str]:
    if not column_name:
        return None
    if not exposure_row_table_is_current(con):
        raise ValueError("Exposure row details are not available for map tags.")

    row = con.execute(
        "SELECT storage_name FROM csv_row_columns WHERE column_name = ?;",
        [str(column_name)],
    ).fetchone()
    if row is None or not row[0]:
        raise ValueError(f"Exposure column '{column_name}' was not found.")
    return str(row[0])


def _point_metadata_sql(
    source_alias: str,
    tag_storage_name: str | None = None,
    color_storage_name: str | None = None,
    color_value: str | None = None,
) -> Tuple[str, str]:
    if not tag_storage_name and not color_storage_name:
        return ", '' AS csv_tag, 0 AS csv_color_match", ""

    join_sql = f"LEFT JOIN csv_rows AS csv_row ON csv_row.row_id = {source_alias}.row_id"
    tag_sql = (
        f"csv_row.{sql_identifier(tag_storage_name)} AS csv_tag"
        if tag_storage_name
        else "'' AS csv_tag"
    )

    if color_storage_name and color_value:
        normalized_value = sql_string(str(color_value).strip().lower())
        color_sql = (
            "CASE WHEN LOWER(TRIM(COALESCE("
            f"csv_row.{sql_identifier(color_storage_name)}, ''"
            f"))) = {normalized_value} THEN 1 ELSE 0 END AS csv_color_match"
        )
    else:
        color_sql = "0 AS csv_color_match"

    return f", {tag_sql}, {color_sql}", join_sql


EXPOSURE_FILTER_OPERATORS = {"gt": ">", "lt": "<", "eq": "="}
MAX_TOP_N = 100_000
_NUMERIC_FILTER_PATTERN = re.compile(r"^[+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?$")


def _row_filter_sql(
    storage_name: str | None,
    operator: str | None,
    value: str | None,
    rank_storage_name: str | None = None,
    rank_limit: int | None = None,
) -> str:
    conditions: List[str] = []
    text = str(value or "").strip()
    if storage_name and text:
        sql_operator = EXPOSURE_FILTER_OPERATORS.get(str(operator or "").strip().lower())
        if sql_operator is None:
            raise ValueError("Filter operator must be one of >, <, or =.")

        column_sql = f"TRIM(filter_row.{sql_identifier(storage_name)})"
        number = float(text) if _NUMERIC_FILTER_PATTERN.match(text) else None
        if number is not None and math.isfinite(number):
            conditions.append(
                f"TRY_CAST({column_sql} AS DOUBLE) {sql_operator} CAST({sql_string(repr(number))} AS DOUBLE)"
            )
        elif sql_operator == "=":
            conditions.append(f"LOWER({column_sql}) = {sql_string(text.lower())}")
        else:
            raise ValueError("Greater-than and less-than filters need a numeric value.")

    order_sql = ""
    if rank_storage_name and rank_limit:
        limit = int(rank_limit)
        if limit < 1 or limit > MAX_TOP_N:
            raise ValueError(f"Top N must be between 1 and {MAX_TOP_N}.")
        rank_sql = f"TRY_CAST(TRIM(filter_row.{sql_identifier(rank_storage_name)}) AS DOUBLE)"
        conditions.append(f"{rank_sql} IS NOT NULL")
        # Rank only mappable rows so the map shows exactly N locations.
        conditions.append("filter_row.row_id IN (SELECT row_id FROM points)")
        order_sql = f" ORDER BY {rank_sql} DESC, filter_row.row_id LIMIT {limit}"

    if not conditions:
        return ""
    where_sql = " AND ".join(conditions)
    return f" AND row_id IN (SELECT filter_row.row_id FROM csv_rows AS filter_row WHERE {where_sql}{order_sql})"


def _empty_feature_collection(mode: str = "empty") -> Dict[str, Any]:
    return {
        "type": "FeatureCollection",
        "features": [],
        "visible_count": 0,
        "returned_count": 0,
        "cell_count": 0,
        "mode": mode,
    }


def _query_individual_points(
    con: duckdb.DuckDBPyConnection,
    min_lon: float,
    min_lat: float,
    max_lon: float,
    max_lat: float,
    max_features: int,
    zoom: float,
    tag_storage_name: str | None = None,
    color_storage_name: str | None = None,
    color_value: str | None = None,
    row_filter_sql: str = "",
) -> Dict[str, Any]:
    metadata_select, metadata_join = _point_metadata_sql(
        "picked",
        tag_storage_name=tag_storage_name,
        color_storage_name=color_storage_name,
        color_value=color_value,
    )
    rows = con.execute(
        f"""
        WITH bounded AS (
            SELECT
                row_id,
                lon,
                lat,
                ROW_NUMBER() OVER (PARTITION BY lon, lat ORDER BY row_id) - 1 AS duplicate_index,
                COUNT(*) OVER (PARTITION BY lon, lat) AS duplicate_count
            FROM points
            WHERE lon BETWEEN ? AND ?
                AND lat BETWEEN ? AND ?{row_filter_sql}
        ),
        picked AS (
            SELECT
                bounded.row_id,
                bounded.lon,
                bounded.lat,
                1 AS csv_count,
                COUNT(*) OVER () AS visible_count,
                COUNT(*) OVER () AS cell_count,
                bounded.duplicate_index,
                bounded.duplicate_count
            FROM bounded
            ORDER BY bounded.row_id
            LIMIT ?
        )
        SELECT
            picked.row_id, picked.lon, picked.lat, picked.csv_count, picked.visible_count,
            picked.cell_count, picked.duplicate_index, picked.duplicate_count
            {metadata_select}
        FROM picked
        {metadata_join}
        ORDER BY picked.row_id;
        """,
        [min_lon, max_lon, min_lat, max_lat, max_features],
    ).fetchall()

    visible_count = int(rows[0][4]) if rows else 0
    cell_count = int(rows[0][5]) if rows else 0
    features = _features_from_rows(
        rows,
        zoom=zoom,
        separate_duplicates=True,
        stacked_tags=_stacked_tag_labels(con, rows, tag_storage_name, row_filter_sql),
    )
    return {
        "type": "FeatureCollection",
        "features": features,
        "visible_count": visible_count,
        "returned_count": len(features),
        "cell_count": cell_count,
        "mode": "raw",
    }


def _query_exact_coordinate_points(
    con: duckdb.DuckDBPyConnection,
    min_lon: float,
    min_lat: float,
    max_lon: float,
    max_lat: float,
    max_features: int,
    tag_storage_name: str | None = None,
    color_storage_name: str | None = None,
    color_value: str | None = None,
    row_filter_sql: str = "",
) -> Dict[str, Any]:
    metadata_select, metadata_join = _point_metadata_sql(
        "picked",
        tag_storage_name=tag_storage_name,
        color_storage_name=color_storage_name,
        color_value=color_value,
    )
    rows = con.execute(
        f"""
        WITH bounded AS (
            SELECT row_id, lon, lat
            FROM points
            WHERE lon BETWEEN ? AND ?
                AND lat BETWEEN ? AND ?{row_filter_sql}
        ),
        exact_points AS (
            SELECT
                MIN(row_id) AS row_id,
                lon,
                lat,
                COUNT(*) AS csv_count
            FROM bounded
            GROUP BY lon, lat
        ),
        picked AS (
            SELECT
                exact_points.row_id,
                exact_points.lon,
                exact_points.lat,
                exact_points.csv_count,
                SUM(exact_points.csv_count) OVER () AS visible_count,
                COUNT(*) OVER () AS cell_count,
                0 AS duplicate_index,
                1 AS duplicate_count
            FROM exact_points
            ORDER BY exact_points.csv_count DESC, exact_points.row_id
            LIMIT ?
        )
        SELECT
            picked.row_id, picked.lon, picked.lat, picked.csv_count, picked.visible_count,
            picked.cell_count, picked.duplicate_index, picked.duplicate_count
            {metadata_select}
        FROM picked
        {metadata_join}
        ORDER BY picked.csv_count DESC, picked.row_id;
        """,
        [min_lon, max_lon, min_lat, max_lat, max_features],
    ).fetchall()

    visible_count = int(rows[0][4]) if rows else 0
    cell_count = int(rows[0][5]) if rows else 0
    features = _features_from_rows(
        rows,
        stacked_tags=_stacked_tag_labels(con, rows, tag_storage_name, row_filter_sql),
    )
    return {
        "type": "FeatureCollection",
        "features": features,
        "visible_count": visible_count,
        "returned_count": len(features),
        "cell_count": cell_count,
        "mode": "raw",
    }


def _query_view_grid_points(
    con: duckdb.DuckDBPyConnection,
    min_lon: float,
    min_lat: float,
    max_lon: float,
    max_lat: float,
    max_features: int,
    source_zoom: int,
    width: int,
    height: int,
    tag_storage_name: str | None = None,
    color_storage_name: str | None = None,
    color_value: str | None = None,
    row_filter_sql: str = "",
) -> Dict[str, Any]:
    table_name = _bin_table_name(source_zoom)
    grid_cols, grid_rows = _view_grid(width, height, max_features)
    lon_step = max((max_lon - min_lon) / grid_cols, 1e-12)
    lat_step = max((max_lat - min_lat) / grid_rows, 1e-12)
    min_x, max_x, min_y, max_y = _tile_range_for_bounds(min_lon, min_lat, max_lon, max_lat, source_zoom)
    metadata_select, metadata_join = _point_metadata_sql(
        "picked",
        tag_storage_name=tag_storage_name,
        color_storage_name=color_storage_name,
        color_value=color_value,
    )
    if row_filter_sql:
        # Pre-aggregated bins cannot be filtered per row, so grid filtered points directly.
        source_sql = f"""
            SELECT row_id, lon, lat, 1 AS csv_count
            FROM points
            WHERE lon BETWEEN ? AND ?
                AND lat BETWEEN ? AND ?{row_filter_sql}
        """
        source_params = [min_lon, max_lon, min_lat, max_lat]
    else:
        source_sql = f"""
            SELECT row_id, lon, lat, csv_count
            FROM {table_name}
            WHERE tile_x BETWEEN ? AND ?
                AND tile_y BETWEEN ? AND ?
                AND lon BETWEEN ? AND ?
                AND lat BETWEEN ? AND ?
        """
        source_params = [min_x, max_x, min_y, max_y, min_lon, max_lon, min_lat, max_lat]
    rows = con.execute(
        f"""
        WITH source AS ({source_sql}),
        gridded AS (
            SELECT
                row_id,
                lon,
                lat,
                csv_count,
                LEAST(GREATEST(CAST(FLOOR((lon - ?) / ?) AS BIGINT), 0), ?) AS grid_x,
                LEAST(GREATEST(CAST(FLOOR((lat - ?) / ?) AS BIGINT), 0), ?) AS grid_y
            FROM source
        ),
        cells AS (
            SELECT
                MIN(row_id) AS row_id,
                ARG_MIN(lon, row_id) AS lon,
                ARG_MIN(lat, row_id) AS lat,
                SUM(csv_count) AS csv_count
            FROM gridded
            GROUP BY grid_x, grid_y
        ),
        picked AS (
            SELECT
                cells.row_id,
                cells.lon,
                cells.lat,
                cells.csv_count,
                SUM(cells.csv_count) OVER () AS visible_count,
                COUNT(*) OVER () AS cell_count,
                0 AS duplicate_index,
                1 AS duplicate_count
            FROM cells
            ORDER BY cells.csv_count DESC, cells.row_id
            LIMIT ?
        )
        SELECT
            picked.row_id, picked.lon, picked.lat, picked.csv_count, picked.visible_count,
            picked.cell_count, picked.duplicate_index, picked.duplicate_count
            {metadata_select}
        FROM picked
        {metadata_join}
        ORDER BY picked.csv_count DESC, picked.row_id;
        """,
        [
            *source_params,
            min_lon,
            lon_step,
            grid_cols - 1,
            min_lat,
            lat_step,
            grid_rows - 1,
            max_features,
        ],
    ).fetchall()

    visible_count = int(rows[0][4]) if rows else 0
    cell_count = int(rows[0][5]) if rows else 0
    features = _features_from_rows(rows)
    return {
        "type": "FeatureCollection",
        "features": features,
        "visible_count": visible_count,
        "returned_count": len(features),
        "cell_count": cell_count,
        "mode": "grid",
        "grid": {
            "source_zoom": source_zoom,
            "cols": grid_cols,
            "rows": grid_rows,
            "tile_min_x": min_x,
            "tile_max_x": max_x,
            "tile_min_y": min_y,
            "tile_max_y": max_y,
        },
    }


def lookup_exposure_points_multires(
    cache_path: Path,
    min_lon: float,
    min_lat: float,
    max_lon: float,
    max_lat: float,
    width: int,
    height: int,
    max_features: int,
    zoom: float = 0.0,
    tag_column: str | None = None,
    color_column: str | None = None,
    color_value: str | None = None,
    filter_column: str | None = None,
    filter_operator: str | None = None,
    filter_value: str | None = None,
    rank_column: str | None = None,
    rank_limit: int | None = None,
    con: duckdb.DuckDBPyConnection | None = None,
) -> Dict[str, Any]:
    min_lon, min_lat, max_lon, max_lat = _clamp_bounds(min_lon, min_lat, max_lon, max_lat)
    if min_lon >= max_lon or min_lat >= max_lat:
        return _empty_feature_collection()

    safe_max = max(500, int(max_features or 12000))
    owns_connection = con is None
    if con is None:
        con = duckdb.connect(str(cache_path), read_only=True)
    try:
        tag_storage_name = _csv_row_storage_name(con, tag_column)
        color_storage_name = _csv_row_storage_name(con, color_column)
        row_filter_sql = ""
        has_rule = bool(filter_column and str(filter_value or "").strip())
        has_rank = bool(rank_column and rank_limit)
        if has_rule or has_rank:
            row_filter_sql = _row_filter_sql(
                _csv_row_storage_name(con, filter_column) if has_rule else None,
                filter_operator,
                filter_value,
                rank_storage_name=_csv_row_storage_name(con, rank_column) if has_rank else None,
                rank_limit=rank_limit if has_rank else None,
            )
        if zoom >= RAW_POINT_ZOOM:
            if zoom < DUPLICATE_SPREAD_ZOOM:
                return _query_exact_coordinate_points(
                    con,
                    min_lon,
                    min_lat,
                    max_lon,
                    max_lat,
                    safe_max,
                    tag_storage_name=tag_storage_name,
                    color_storage_name=color_storage_name,
                    color_value=color_value,
                    row_filter_sql=row_filter_sql,
                )
            return _query_individual_points(
                con,
                min_lon,
                min_lat,
                max_lon,
                max_lat,
                safe_max,
                zoom,
                tag_storage_name=tag_storage_name,
                color_storage_name=color_storage_name,
                color_value=color_value,
                row_filter_sql=row_filter_sql,
            )

        source_zoom = _select_source_zoom(zoom, min_lon, min_lat, max_lon, max_lat, safe_max)
        return _query_view_grid_points(
            con,
            min_lon,
            min_lat,
            max_lon,
            max_lat,
            safe_max,
            source_zoom,
            width,
            height,
            tag_storage_name=tag_storage_name,
            color_storage_name=color_storage_name,
            color_value=color_value,
            row_filter_sql=row_filter_sql,
        )
    finally:
        if owns_connection:
            con.close()
