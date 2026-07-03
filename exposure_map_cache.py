import math
from pathlib import Path
from typing import Any, Dict, Iterable, List, Tuple

import duckdb


EXPOSURE_BIN_VERSION = "2"
EXPOSURE_ROW_TABLE_VERSION = "1"
EXPOSURE_BIN_ZOOMS: Tuple[int, ...] = (6, 8, 10, 12, 14, 16)
RAW_POINT_ZOOM = 14.5
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

    for zoom in EXPOSURE_BIN_ZOOMS:
        table_name = _bin_table_name(zoom)
        tile_count = 2 ** zoom
        max_tile = tile_count - 1

        con.execute(f"""
            CREATE TABLE {table_name} AS
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
        con.execute(f"CREATE INDEX {table_name}_xy_idx ON {table_name}(tile_x, tile_y);")

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
    csv_path: Path,
    scan_options_sql: str,
    columns: List[str],
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
    csv_sql = sql_string(str(csv_path.resolve()))
    con.execute(f"""
        CREATE TABLE csv_rows AS
        SELECT
            row_number() OVER () AS row_id,
            {", ".join(selected_columns)}
        FROM read_csv_auto({csv_sql}, {scan_options_sql}) AS source;
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

    if view_zoom >= 12:
        target_zoom = 16
    elif view_zoom >= 8:
        target_zoom = 14
    else:
        target_zoom = 12

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

    cols = max(24, min(260, math.ceil(safe_width / 8)))
    rows = max(18, min(200, math.ceil(safe_height / 8)))
    cell_count = cols * rows

    if cell_count > safe_max:
        scale = math.sqrt(safe_max / cell_count)
        cols = max(24, int(cols * scale))
        rows = max(18, int(rows * scale))

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


def _features_from_rows(
    rows: Iterable[Tuple[Any, ...]],
    zoom: float = 0.0,
    separate_duplicates: bool = False,
) -> List[Dict[str, Any]]:
    features: List[Dict[str, Any]] = []
    for row in rows:
        row_id, lon, lat, csv_count, *_extra = row
        display_lon = float(lon)
        display_lat = float(lat)
        duplicate_index = int(_extra[2]) if len(_extra) >= 4 else 0
        duplicate_count = int(_extra[3]) if len(_extra) >= 4 else 1
        if separate_duplicates:
            display_lon, display_lat = _offset_duplicate_coordinate(
                display_lon,
                display_lat,
                duplicate_index,
                duplicate_count,
                zoom,
            )

        count = int(csv_count or 0)
        features.append({
            "type": "Feature",
            "geometry": {
                "type": "Point",
                "coordinates": [display_lon, display_lat],
            },
            "properties": {
                "row_id": int(row_id),
                "csv_count": count,
                "csv_label": _count_label(count),
                "duplicate_count": duplicate_count,
            },
        })
    return features


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
) -> Dict[str, Any]:
    rows = con.execute(
        """
        WITH bounded AS (
            SELECT
                row_id,
                lon,
                lat,
                ROW_NUMBER() OVER (PARTITION BY lon, lat ORDER BY row_id) - 1 AS duplicate_index,
                COUNT(*) OVER (PARTITION BY lon, lat) AS duplicate_count
            FROM points
            WHERE lon BETWEEN ? AND ?
                AND lat BETWEEN ? AND ?
        ),
        ranked AS (
            SELECT
                row_id,
                lon,
                lat,
                1 AS csv_count,
                COUNT(*) OVER () AS visible_count,
                COUNT(*) OVER () AS cell_count,
                duplicate_index,
                duplicate_count
            FROM bounded
        )
        SELECT row_id, lon, lat, csv_count, visible_count, cell_count, duplicate_index, duplicate_count
        FROM ranked
        ORDER BY row_id
        LIMIT ?;
        """,
        [min_lon, max_lon, min_lat, max_lat, max_features],
    ).fetchall()

    visible_count = int(rows[0][4]) if rows else 0
    cell_count = int(rows[0][5]) if rows else 0
    features = _features_from_rows(rows, zoom=zoom, separate_duplicates=True)
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
) -> Dict[str, Any]:
    rows = con.execute(
        """
        WITH bounded AS (
            SELECT row_id, lon, lat
            FROM points
            WHERE lon BETWEEN ? AND ?
                AND lat BETWEEN ? AND ?
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
        ranked AS (
            SELECT
                row_id,
                lon,
                lat,
                csv_count,
                SUM(csv_count) OVER () AS visible_count,
                COUNT(*) OVER () AS cell_count
            FROM exact_points
        )
        SELECT row_id, lon, lat, csv_count, visible_count, cell_count
        FROM ranked
        ORDER BY csv_count DESC, row_id
        LIMIT ?;
        """,
        [min_lon, max_lon, min_lat, max_lat, max_features],
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
) -> Dict[str, Any]:
    table_name = _bin_table_name(source_zoom)
    grid_cols, grid_rows = _view_grid(width, height, max_features)
    lon_step = max((max_lon - min_lon) / grid_cols, 1e-12)
    lat_step = max((max_lat - min_lat) / grid_rows, 1e-12)
    min_x, max_x, min_y, max_y = _tile_range_for_bounds(min_lon, min_lat, max_lon, max_lat, source_zoom)
    rows = con.execute(
        f"""
        WITH source AS (
            SELECT row_id, lon, lat, csv_count
            FROM {table_name}
            WHERE tile_x BETWEEN ? AND ?
                AND tile_y BETWEEN ? AND ?
                AND lon BETWEEN ? AND ?
                AND lat BETWEEN ? AND ?
        ),
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
        ranked AS (
            SELECT
                row_id,
                lon,
                lat,
                csv_count,
                SUM(csv_count) OVER () AS visible_count,
                COUNT(*) OVER () AS cell_count
            FROM cells
        )
        SELECT row_id, lon, lat, csv_count, visible_count, cell_count
        FROM ranked
        ORDER BY csv_count DESC, row_id
        LIMIT ?;
        """,
        [
            min_x,
            max_x,
            min_y,
            max_y,
            min_lon,
            max_lon,
            min_lat,
            max_lat,
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
) -> Dict[str, Any]:
    min_lon, min_lat, max_lon, max_lat = _clamp_bounds(min_lon, min_lat, max_lon, max_lat)
    if min_lon >= max_lon or min_lat >= max_lat:
        return _empty_feature_collection()

    safe_max = max(500, int(max_features or 12000))
    con = duckdb.connect(str(cache_path), read_only=True)
    try:
        if zoom >= RAW_POINT_ZOOM:
            if zoom < DUPLICATE_SPREAD_ZOOM:
                return _query_exact_coordinate_points(con, min_lon, min_lat, max_lon, max_lat, safe_max)
            return _query_individual_points(con, min_lon, min_lat, max_lon, max_lat, safe_max, zoom)

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
        )
    finally:
        con.close()
