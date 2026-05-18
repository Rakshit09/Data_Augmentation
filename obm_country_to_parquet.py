import argparse
import gzip
import json
import logging
import math
import re
import shutil
import sqlite3
import time
import urllib.request
import zipfile
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional, Tuple

import duckdb
import pandas as pd
import requests


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger("obm_country_etl")


def _ensure_duckdb_extension(con: "duckdb.DuckDBPyConnection", name: str) -> None:
    """Install a DuckDB extension, falling back to a direct download if the CDN returns 403."""
    try:
        con.execute(f"INSTALL {name};")
    except Exception as install_err:
        if "403" not in str(install_err) and "HTTP" not in str(install_err):
            raise
        logger.warning(
            "INSTALL %s failed (%s). Attempting direct download with browser User-Agent.",
            name, install_err,
        )
        import duckdb as _duckdb
        version = _duckdb.__version__
        platform = "windows_amd64"
        url = f"http://extensions.duckdb.org/v{version}/{platform}/{name}.duckdb_extension.gz"
        dest_dir = Path.home() / ".duckdb" / "extensions" / f"v{version}" / platform
        dest_dir.mkdir(parents=True, exist_ok=True)
        gz_path = dest_dir / f"{name}.duckdb_extension.gz"
        ext_path = dest_dir / f"{name}.duckdb_extension"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=60) as resp, open(gz_path, "wb") as fh:
            shutil.copyfileobj(resp, fh)
        with gzip.open(gz_path, "rb") as gz, open(ext_path, "wb") as out:
            shutil.copyfileobj(gz, out)
        gz_path.unlink(missing_ok=True)
        logger.info("Downloaded %s extension to %s", name, ext_path)


@dataclass
class ETLConfig:
    """
    Step 1 ETL:
    OpenBuildingMap S3 -> clean country building-level Parquet dataset.

    Output is designed as a staging layer for a later MSSQL load.
    """

    output_dir: str = "./etl_output"

    # Source Cooperative OpenBuildingMap S3 path
    obm_s3: str = "s3://us-west-2.opendata.source.coop/tge-labs/openbuildingmap/*.parquet"

    # Official BKG VG250 GeoPackage ZIP, UTM32 / EPSG:25832
    bkg_boundary_zip_url: str = (
        "https://daten.gdz.bkg.bund.de/produkte/vg/vg250_ebenen_0101/"
        "aktuell/vg250_01-01.utm32s.gpkg.ebenen.zip"
    )

    # Coarse country bbox with a small margin.
    # Used only as cheap prefilter. Precise filtering is done by the boundary file.
    lon_min: float = 5.5
    lon_max: float = 15.5
    lat_min: float = 47.0
    lat_max: float = 55.3

    # DuckDB settings
    duckdb_file: str = "./etl_output/work_obm.duckdb"
    threads: int = 8
    memory_limit: str = "24GB"
    temp_directory: str = "./etl_output/duckdb_temp"

    # Output
    output_parquet: str = "./etl_output/buildings_de_cleaned.parquet"
    row_group_size: int = 100_000

    # Assumptions
    metres_per_storey: float = 3.0
    usable_floor_factor: float = 1.0

    # Behaviour
    force: bool = False
    sample_only: bool = False
    sample_limit: int = 100_000
    obm_quadkey_zoom: int = 6

    # Optional local boundary file (shapefile .zip or .shp, or GeoPackage .gpkg/.zip).
    # When set, the bkg_boundary_zip_url download is skipped.
    boundary_file: Optional[str] = None


class OpenBuildingMapCountryETL:
    def __init__(self, config: ETLConfig):
        self.cfg = config

        self.output_dir = Path(config.output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)

        self.boundary_dir = self.output_dir / "boundary"
        self.boundary_dir.mkdir(parents=True, exist_ok=True)

        self.profile_dir = self.output_dir / "profile"
        self.profile_dir.mkdir(parents=True, exist_ok=True)

        self.temp_dir = Path(config.temp_directory)
        self.temp_dir.mkdir(parents=True, exist_ok=True)

        self.con: Optional[duckdb.DuckDBPyConnection] = None

    # ---------------------------------------------------------------------
    # Lifecycle
    # ---------------------------------------------------------------------

    def run(self) -> Path:
        start = time.time()

        logger.info("Starting OpenBuildingMap country ETL")

        self._prepare_output_target()
        self._initialize_duckdb()

        boundary_gpkg, boundary_layer, boundary_epsg, boundary_geom_col = self._download_and_prepare_boundary()
        self._create_country_boundary_table(
            boundary_gpkg,
            boundary_layer,
            boundary_epsg,
            boundary_geom_col,
        )

        geom_expr = self._detect_obm_geometry_expression()
        self._extract_clean_parquet(geom_expr)

        self._profile_output()
        self._write_manifest(start)

        self.close()

        logger.info("ETL complete")
        logger.info("Output Parquet: %s", self.cfg.output_parquet)

        return Path(self.cfg.output_parquet)

    def close(self):
        if self.con is not None:
            self.con.close()
            self.con = None

    # ---------------------------------------------------------------------
    # Setup
    # ---------------------------------------------------------------------

    def _prepare_output_target(self):
        output_path = Path(self.cfg.output_parquet)

        if output_path.exists():
            if self.cfg.force:
                logger.info("Removing existing output file: %s", output_path)
                output_path.unlink()
            else:
                raise FileExistsError(
                    f"Output already exists: {output_path}. "
                    "Use --force to overwrite."
                )

        output_path.parent.mkdir(parents=True, exist_ok=True)

    def _initialize_duckdb(self):
        logger.info("Initializing DuckDB")

        self.con = duckdb.connect(self.cfg.duckdb_file)

        _ensure_duckdb_extension(self.con, "spatial")
        self.con.execute("LOAD spatial;")
        _ensure_duckdb_extension(self.con, "httpfs")
        self.con.execute("LOAD httpfs;")

        self.con.execute(f"SET threads = {self.cfg.threads};")
        self.con.execute(f"SET memory_limit = '{self.cfg.memory_limit}';")
        self.con.execute(f"SET temp_directory = '{Path(self.cfg.temp_directory).as_posix()}';")
        self.con.execute("SET s3_use_ssl = false;")
        self.con.execute("SET enable_curl_server_cert_verification = false;")

        # Public Source Cooperative S3 bucket.
        # This usually works without credentials; region is still needed.
        try:
            self.con.execute("""
                CREATE OR REPLACE SECRET sourcecoop (
                    TYPE s3,
                    PROVIDER config,
                    REGION 'us-west-2',
                    USE_SSL false,
                    SCOPE 's3://us-west-2.opendata.source.coop'
                );
            """)
        except Exception as exc:
            logger.warning("Could not create S3 secret. Continuing anyway. Error: %s", exc)

    # ---------------------------------------------------------------------
    # Boundary
    # ---------------------------------------------------------------------

    def _download_and_prepare_boundary(self) -> Tuple[Path, Optional[str], int, str]:
        """
        Resolves the country boundary file.

        If ``ETLConfig.boundary_file`` is set, uses that local file (supports
        shapefile .shp, a ZIP containing .shp files, or a .gpkg / ZIP containing
        .gpkg files). Otherwise downloads the default BKG VG250 Germany
        GeoPackage for backwards compatibility.

        Returns:
            boundary_path, layer_name_or_None, source_epsg, geometry_column

        ``layer_name_or_None`` is None for plain shapefiles (layer arg omitted).
        """

        if self.cfg.boundary_file:
            return self._prepare_local_boundary(Path(self.cfg.boundary_file))

        zip_path = self.boundary_dir / "vg250_bkg_boundary.zip"

        if not zip_path.exists():
            logger.info("Downloading BKG VG250 boundary ZIP")
            self._download_file(self.cfg.bkg_boundary_zip_url, zip_path)
        else:
            logger.info("BKG boundary ZIP already exists: %s", zip_path)

        extracted_marker = self.boundary_dir / ".extracted"

        if not extracted_marker.exists() or self.cfg.force:
            logger.info("Extracting BKG boundary ZIP")
            with zipfile.ZipFile(zip_path, "r") as zf:
                zf.extractall(self.boundary_dir)
            extracted_marker.write_text(datetime.now(timezone.utc).isoformat())

        gpkg_files = list(self.boundary_dir.rglob("*.gpkg"))

        if not gpkg_files:
            raise FileNotFoundError("No GeoPackage found in BKG boundary ZIP")

        gpkg_path = gpkg_files[0]

        layer_name, source_epsg, geometry_column = self._detect_gpkg_boundary_layer(gpkg_path)

        logger.info("Using boundary GeoPackage: %s", gpkg_path)
        logger.info("Using boundary layer: %s", layer_name)
        logger.info("Boundary source EPSG: %s", source_epsg)
        logger.info("Boundary geometry column: %s", geometry_column)

        return gpkg_path, layer_name, source_epsg, geometry_column

    def _prepare_local_boundary(self, boundary_file: Path) -> Tuple[Path, Optional[str], int, str]:
        """
        Handles a user-supplied boundary file.  Supports:
          - .shp  (or any GDAL-readable single-file vector)
          - .zip  containing .shp files  → extracted to boundary_dir
          - .gpkg
          - .zip  containing .gpkg files → extracted to boundary_dir
        """
        suffix = boundary_file.suffix.lower()

        if suffix == ".zip":
            extract_dir = self.boundary_dir / "user_boundary"
            extract_dir.mkdir(parents=True, exist_ok=True)
            logger.info("Extracting user boundary ZIP: %s", boundary_file)
            with zipfile.ZipFile(boundary_file, "r") as zf:
                zf.extractall(extract_dir)

            gpkg_files = list(extract_dir.rglob("*.gpkg"))
            shp_files = list(extract_dir.rglob("*.shp"))

            if gpkg_files:
                boundary_file = gpkg_files[0]
                suffix = ".gpkg"
            elif shp_files:
                boundary_file = shp_files[0]
                suffix = ".shp"
            else:
                raise FileNotFoundError(
                    "No .gpkg or .shp file found inside the uploaded boundary ZIP."
                )

        if suffix == ".gpkg":
            layer_name, source_epsg, geometry_column = self._detect_gpkg_boundary_layer(boundary_file)
            logger.info("Using user GeoPackage boundary: %s (layer=%s)", boundary_file, layer_name)
            return boundary_file, layer_name, source_epsg, geometry_column

        # Shapefile (or other GDAL single-layer source)
        source_epsg = self._detect_shapefile_epsg(boundary_file)
        geometry_column = self._detect_shapefile_geom_column(boundary_file)
        logger.info("Using user shapefile boundary: %s (epsg=%s, geom=%s)", boundary_file, source_epsg, geometry_column)
        return boundary_file, None, source_epsg, geometry_column

    @staticmethod
    def _detect_shapefile_epsg(shp_path: Path) -> int:
        """Reads EPSG from the companion .prj file, falling back to 4326."""
        prj_path = shp_path.with_suffix(".prj")
        if not prj_path.exists():
            logger.warning("No .prj file found for %s; assuming EPSG:4326", shp_path)
            return 4326
        try:
            prj_text = prj_path.read_text(encoding="utf-8", errors="replace")
            authority_match = re.search(
                r'AUTHORITY\s*\[\s*"EPSG"\s*,\s*"(\d+)"\s*\]',
                prj_text,
                flags=re.IGNORECASE,
            )
            if authority_match:
                return int(authority_match.group(1))

            epsg_match = re.search(r"EPSG[:\s,]+(\d+)", prj_text, flags=re.IGNORECASE)
            if epsg_match:
                return int(epsg_match.group(1))

            normalized_prj = prj_text.upper().replace(" ", "").replace("_", "")
            if "WGS1984" in normalized_prj or "WGS84" in normalized_prj:
                return 4326

            from pyproj import CRS
            crs = CRS.from_wkt(prj_text)
            epsg = crs.to_epsg()
            return int(epsg) if epsg else 4326
        except Exception as exc:
            logger.warning("Could not parse .prj for EPSG (%s); assuming 4326", exc)
            return 4326

    def _detect_shapefile_geom_column(self, shp_path: Path) -> str:
        """Returns the geometry column name DuckDB assigns when reading the shapefile."""
        try:
            path_sql = shp_path.as_posix().replace("'", "''")
            schema = self.con.execute(f"""
                DESCRIBE SELECT * FROM ST_Read('{path_sql}', keep_wkb = false) LIMIT 1
            """).df()
            geom_cols = schema[schema["column_type"].str.upper().str.contains("GEOMETRY")]["column_name"].tolist()
            return geom_cols[0] if geom_cols else "wkb_geometry"
        except Exception as exc:
            logger.warning("Could not detect shapefile geometry column (%s); using wkb_geometry", exc)
            return "wkb_geometry"

    @staticmethod
    def _download_file(url: str, target_path: Path):
        target_path.parent.mkdir(parents=True, exist_ok=True)

        with requests.get(url, stream=True, timeout=120) as response:
            response.raise_for_status()
            with open(target_path, "wb") as f:
                for chunk in response.iter_content(chunk_size=1024 * 1024):
                    if chunk:
                        f.write(chunk)

    @staticmethod
    def _detect_gpkg_boundary_layer(gpkg_path: Path) -> Tuple[str, int, str]:
        """
        Detects the boundary layer in a GeoPackage.

        BKG VG250 usually contains layer names such as vg250_sta, vg250_lan, etc.
        We prefer vg250_sta for the built-in Germany fallback; for other
        GeoPackages we use the first feature layer when no obvious national
        boundary layer is present.
        """

        with sqlite3.connect(gpkg_path) as conn:
            layers = conn.execute("""
                SELECT table_name
                FROM gpkg_contents
                WHERE data_type = 'features'
            """).fetchall()

            layer_names = [row[0] for row in layers]

            if not layer_names:
                raise ValueError(f"No feature layers found in GeoPackage: {gpkg_path}")

            preferred = None

            for candidate in ["vg250_sta", "VG250_STA"]:
                if candidate in layer_names:
                    preferred = candidate
                    break

            if preferred is None:
                sta_layers = [x for x in layer_names if "sta" in x.lower()]
                if sta_layers:
                    preferred = sta_layers[0]
                else:
                    preferred = layer_names[0]

            epsg_row = conn.execute("""
                SELECT srs_id, column_name
                FROM gpkg_geometry_columns
                WHERE table_name = ?
                LIMIT 1
            """, [preferred]).fetchone()

            source_epsg = int(epsg_row[0]) if epsg_row else 25832
            geometry_column = epsg_row[1] if epsg_row else "geom"

        return preferred, source_epsg, geometry_column

    def _create_country_boundary_table(
        self,
        gpkg_path: Path,
        layer_name: Optional[str],
        source_epsg: int,
        geometry_column: str,
    ):
        """
        Creates a single dissolved country boundary geometry in OGC:CRS84.

        ``layer_name`` may be None for single-layer sources such as shapefiles.
        """

        logger.info("Creating dissolved boundary table in DuckDB")

        file_path_sql = gpkg_path.as_posix().replace("'", "''")
        geom_col_sql = '"' + geometry_column.replace('"', '""') + '"'

        layer_clause = ""
        if layer_name is not None:
            layer_sql = layer_name.replace("'", "''")
            layer_clause = f"layer = '{layer_sql}',"

        if source_epsg == 4326:
            geom_sql = f"ST_SetCRS({geom_col_sql}, 'OGC:CRS84')"
        else:
            geom_sql = (
                f"ST_SetCRS("
                f"ST_Transform("
                f"{geom_col_sql}, "
                f"'EPSG:{source_epsg}', "
                f"'EPSG:4326', "
                f"always_xy := true"
                f"), "
                f"'OGC:CRS84'"
                f")"
            )

        self.con.execute(f"""
            CREATE OR REPLACE TABLE country_boundary AS
            SELECT ST_Union_Agg({geom_sql}) AS geom
            FROM ST_Read(
                '{file_path_sql}',
                {layer_clause}
                keep_wkb = false
            )
            WHERE {geom_col_sql} IS NOT NULL;
        """)

        boundary_check = self.con.execute("""
            SELECT
                ST_GeometryType(geom) AS geom_type,
                ST_IsValid(geom) AS is_valid
            FROM country_boundary;
        """).df()

        logger.info("Boundary check:\n%s", boundary_check)

    # ---------------------------------------------------------------------
    # OBM schema inspection
    # ---------------------------------------------------------------------

    @staticmethod
    def _lon_lat_to_tile(lon: float, lat: float, zoom: int) -> Tuple[int, int]:
        lat = max(min(lat, 85.05112878), -85.05112878)
        n = 2 ** zoom
        x = int((lon + 180.0) / 360.0 * n)
        sin_lat = math.sin(math.radians(lat))
        y = int((0.5 - math.log((1 + sin_lat) / (1 - sin_lat)) / (4 * math.pi)) * n)
        return max(0, min(n - 1, x)), max(0, min(n - 1, y))

    @staticmethod
    def _tile_to_quadkey(x: int, y: int, zoom: int) -> str:
        quadkey = []

        for level in range(zoom, 0, -1):
            digit = 0
            mask = 1 << (level - 1)

            if x & mask:
                digit += 1
            if y & mask:
                digit += 2

            quadkey.append(str(digit))

        return "".join(quadkey)

    def _obm_input_paths(self) -> List[str]:
        """
        OpenBuildingMap files are partitioned by zoom-6 quadkey. Reading only
        intersecting quadkey files avoids listing/scanning the full global prefix.
        """

        if not self.cfg.obm_s3.endswith("*.parquet"):
            return [self.cfg.obm_s3]

        zoom = int(self.cfg.obm_quadkey_zoom)
        x_min, y_max = self._lon_lat_to_tile(self.cfg.lon_min, self.cfg.lat_min, zoom)
        x_max, y_min = self._lon_lat_to_tile(self.cfg.lon_max, self.cfg.lat_max, zoom)

        quadkeys = sorted({
            self._tile_to_quadkey(x, y, zoom)
            for x in range(min(x_min, x_max), max(x_min, x_max) + 1)
            for y in range(min(y_min, y_max), max(y_min, y_max) + 1)
        })

        prefix = self.cfg.obm_s3[:-len("*.parquet")]
        paths = [f"{prefix}building.{quadkey}.parquet" for quadkey in quadkeys]

        logger.info("Using %s OBM quadkey Parquet file(s): %s", len(paths), ", ".join(paths))

        return paths

    @staticmethod
    def _duckdb_string_list(values: List[str]) -> str:
        quoted = ["'" + value.replace("'", "''") + "'" for value in values]
        return "[" + ", ".join(quoted) + "]"

    def _detect_obm_geometry_expression(self) -> str:
        """
        Detects how DuckDB sees the OBM geometry column.

        Returns SQL expression that yields a DuckDB GEOMETRY object.
        """

        logger.info("Inspecting OpenBuildingMap schema")
        obm_paths_sql = self._duckdb_string_list(self._obm_input_paths())

        schema_df = self.con.execute(f"""
            DESCRIBE SELECT *
            FROM read_parquet({obm_paths_sql}, union_by_name = true)
            LIMIT 1;
        """).df()

        schema_path = self.profile_dir / "obm_schema.csv"
        schema_df.to_csv(schema_path, index=False)
        logger.info("Saved OBM schema to: %s", schema_path)

        if "geometry" not in schema_df["column_name"].values:
            raise ValueError("OBM schema does not contain a 'geometry' column")

        geometry_type = schema_df.loc[
            schema_df["column_name"] == "geometry",
            "column_type"
        ].iloc[0]

        logger.info("OBM geometry column type according to DuckDB: %s", geometry_type)

        upper_type = str(geometry_type).upper()

        if "BLOB" in upper_type or "WKB" in upper_type or "BYTE" in upper_type:
            return "ST_GeomFromWKB(geometry)"

        if "GEOMETRY" in upper_type:
            return "geometry"

        # Conservative fallback
        logger.warning(
            "Unknown geometry type '%s'. Trying ST_GeomFromWKB(geometry).",
            geometry_type
        )
        return "ST_GeomFromWKB(geometry)"

    # ---------------------------------------------------------------------
    # Main extraction
    # ---------------------------------------------------------------------

    def _extract_clean_parquet(self, geom_expr: str):
        logger.info("Extracting and enriching country buildings to Parquet")

        output_path = Path(self.cfg.output_parquet).as_posix().replace("'", "''")
        obm_paths_sql = self._duckdb_string_list(self._obm_input_paths())

        sample_limit_sql = ""
        if self.cfg.sample_only:
            sample_limit_sql = f"LIMIT {int(self.cfg.sample_limit)}"

        query = f"""
        COPY (
            WITH candidates AS (
                SELECT
                    id,
                    source,
                    relation_id,
                    quadkey,
                    last_update,
                    occupancy,
                    height,
                    TRY_CAST(floorspace AS DOUBLE) AS floorspace_obm_m2,
                    {geom_expr} AS geom,
                    bbox
                FROM read_parquet(
                    {obm_paths_sql},
                    union_by_name = true,
                    filename = true
                )
                WHERE
                    bbox.xmax >= {self.cfg.lon_min}
                    AND bbox.xmin <= {self.cfg.lon_max}
                    AND bbox.ymax >= {self.cfg.lat_min}
                    AND bbox.ymin <= {self.cfg.lat_max}
                {sample_limit_sql}
            ),

            country_buildings AS (
                SELECT c.*
                FROM candidates c, country_boundary g
                WHERE
                    c.geom IS NOT NULL
                    AND ST_IsValid(c.geom)
                    AND ST_Intersects(c.geom, g.geom)
            ),

            height_parsed AS (
                SELECT
                    *,

                    TRY_CAST(
                        NULLIF(REGEXP_EXTRACT(height, 'HHT:([0-9.]+)', 1), '')
                        AS DOUBLE
                    ) AS height_direct_m,

                    TRY_CAST(
                        NULLIF(REGEXP_EXTRACT(height, '(^|\\\\+)H:([0-9]+)', 2), '')
                        AS INTEGER
                    ) AS stories_exact_parsed,

                    TRY_CAST(
                        NULLIF(REGEXP_EXTRACT(height, 'HBET:([0-9]+)-([0-9]+)', 1), '')
                        AS INTEGER
                    ) AS stories_min_parsed,

                    TRY_CAST(
                        NULLIF(REGEXP_EXTRACT(height, 'HBET:([0-9]+)-([0-9]+)', 2), '')
                        AS INTEGER
                    ) AS stories_max_parsed

                FROM country_buildings
            ),

            measured AS (
                SELECT
                    *,
                    ST_Area(
                        ST_Transform(
                            geom,
                            'EPSG:4326',
                            'EPSG:3035',
                            always_xy := true
                        )
                    ) AS footprint_area_m2
                FROM height_parsed
            ),

            enriched AS (
                SELECT
                    -- Stable identifiers
                    CAST(id AS VARCHAR) AS building_id,
                    source,
                    relation_id,
                    quadkey,
                    SUBSTR(CAST(quadkey AS VARCHAR), 1, 6) AS quadkey_prefix_6,
                    last_update,

                    -- Geometry staging for MSSQL
                    ST_AsWKB(geom) AS geom_wkb,

                    ST_X(ST_Centroid(geom)) AS centroid_lon,
                    ST_Y(ST_Centroid(geom)) AS centroid_lat,

                    bbox.xmin AS bbox_xmin,
                    bbox.ymin AS bbox_ymin,
                    bbox.xmax AS bbox_xmax,
                    bbox.ymax AS bbox_ymax,

                    footprint_area_m2,

                    -- Raw attributes
                    height AS height_raw,
                    occupancy AS occupancy_raw,
                    floorspace_obm_m2,

                    -- Height interpretation
                    CASE
                        WHEN height_direct_m IS NOT NULL
                            THEN 'exact_height_m'
                        WHEN stories_exact_parsed IS NOT NULL
                            THEN 'estimated_from_exact_storeys'
                        WHEN stories_min_parsed IS NOT NULL AND stories_max_parsed IS NOT NULL
                            THEN 'estimated_from_storey_range'
                        WHEN height IS NULL OR height = ''
                            THEN 'unknown'
                        ELSE 'other'
                    END AS height_source_type,

                    CASE
                        WHEN height_direct_m IS NOT NULL
                            THEN height_direct_m
                        WHEN stories_exact_parsed IS NOT NULL
                            THEN stories_exact_parsed * {self.cfg.metres_per_storey}
                        WHEN stories_min_parsed IS NOT NULL AND stories_max_parsed IS NOT NULL
                            THEN ((stories_min_parsed + stories_max_parsed) / 2.0)
                                 * {self.cfg.metres_per_storey}
                        ELSE NULL
                    END AS height_m,

                    stories_exact_parsed AS stories_exact,
                    stories_min_parsed AS stories_min,
                    stories_max_parsed AS stories_max,

                    CASE
                        WHEN height_direct_m IS NOT NULL
                            THEN 'high'
                        WHEN stories_exact_parsed IS NOT NULL
                            THEN 'medium'
                        WHEN stories_min_parsed IS NOT NULL AND stories_max_parsed IS NOT NULL
                            THEN 'low'
                        ELSE 'none'
                    END AS height_quality,

                    -- Occupancy interpretation
                    COALESCE(
                        NULLIF(SUBSTR(UPPER(occupancy), 1, 3), ''),
                        'UNK'
                    ) AS occupancy_code,

                    CASE
                        WHEN UPPER(occupancy) LIKE 'RES%' THEN 'Residential'
                        WHEN UPPER(occupancy) LIKE 'COM%' THEN 'Commercial'
                        WHEN UPPER(occupancy) LIKE 'MIX%' THEN 'Mixed'
                        WHEN UPPER(occupancy) LIKE 'IND%' THEN 'Industrial'
                        WHEN UPPER(occupancy) LIKE 'AGR%' THEN 'Agricultural'
                        WHEN UPPER(occupancy) LIKE 'ASS%' THEN 'Assembly'
                        WHEN UPPER(occupancy) LIKE 'GOV%' THEN 'Government'
                        WHEN UPPER(occupancy) LIKE 'EDU%' THEN 'Education'
                        ELSE 'Unknown'
                    END AS occupancy_group,

                    CASE
                        WHEN occupancy IS NULL OR occupancy = ''
                            THEN 'none'
                        WHEN LENGTH(occupancy) >= 3
                            THEN 'available'
                        ELSE 'low'
                    END AS occupancy_quality,

                    -- Estimated floorspace:
                    -- Prefer OBM floorspace if available.
                    -- Otherwise estimate from footprint x storeys if storeys are known.
                    CASE
                        WHEN floorspace_obm_m2 IS NOT NULL
                            THEN floorspace_obm_m2
                        WHEN stories_exact_parsed IS NOT NULL
                            THEN footprint_area_m2 * stories_exact_parsed
                                 * {self.cfg.usable_floor_factor}
                        WHEN stories_min_parsed IS NOT NULL AND stories_max_parsed IS NOT NULL
                            THEN footprint_area_m2 * ((stories_min_parsed + stories_max_parsed) / 2.0)
                                 * {self.cfg.usable_floor_factor}
                        ELSE NULL
                    END AS floorspace_est_m2

                FROM measured
            )

            SELECT
                *,
                (
                    CASE WHEN footprint_area_m2 IS NOT NULL THEN 0.25 ELSE 0.0 END +
                    CASE WHEN height_m IS NOT NULL THEN 0.30 ELSE 0.0 END +
                    CASE WHEN occupancy_group <> 'Unknown' THEN 0.30 ELSE 0.0 END +
                    CASE WHEN floorspace_est_m2 IS NOT NULL THEN 0.15 ELSE 0.0 END
                ) AS attribute_completeness_score
            FROM enriched
        )
        TO '{output_path}'
        (
            FORMAT PARQUET,
            COMPRESSION ZSTD,
            ROW_GROUP_SIZE {self.cfg.row_group_size}
        );
        """

        self.con.execute(query)

        logger.info("Parquet written: %s", output_path)

    # ---------------------------------------------------------------------
    # Profiling
    # ---------------------------------------------------------------------

    def _profile_output(self):
        logger.info("Profiling output Parquet")

        output = Path(self.cfg.output_parquet).as_posix().replace("'", "''")

        summary = {}

        summary["total_buildings"] = self.con.execute(f"""
            SELECT COUNT(*) FROM read_parquet('{output}');
        """).fetchone()[0]

        summary["min_area_m2"], summary["avg_area_m2"], summary["max_area_m2"] = self.con.execute(f"""
            SELECT
                MIN(footprint_area_m2),
                AVG(footprint_area_m2),
                MAX(footprint_area_m2)
            FROM read_parquet('{output}');
        """).fetchone()

        summary["unknown_occupancy_share"] = self.con.execute(f"""
            SELECT
                AVG(CASE WHEN occupancy_group = 'Unknown' THEN 1.0 ELSE 0.0 END)
            FROM read_parquet('{output}');
        """).fetchone()[0]

        summary["missing_height_share"] = self.con.execute(f"""
            SELECT
                AVG(CASE WHEN height_m IS NULL THEN 1.0 ELSE 0.0 END)
            FROM read_parquet('{output}');
        """).fetchone()[0]

        summary["missing_floorspace_share"] = self.con.execute(f"""
            SELECT
                AVG(CASE WHEN floorspace_est_m2 IS NULL THEN 1.0 ELSE 0.0 END)
            FROM read_parquet('{output}');
        """).fetchone()[0]

        summary["avg_attribute_completeness_score"] = self.con.execute(f"""
            SELECT AVG(attribute_completeness_score)
            FROM read_parquet('{output}');
        """).fetchone()[0]

        profile_json = self.profile_dir / "summary.json"
        profile_json.write_text(json.dumps(summary, indent=2, default=str))

        logger.info("Profile summary saved to: %s", profile_json)
        logger.info("Profile summary:\n%s", json.dumps(summary, indent=2, default=str))

        occupancy_df = self.con.execute(f"""
            SELECT
                occupancy_group,
                COUNT(*) AS building_count,
                COUNT(*) * 1.0 / SUM(COUNT(*)) OVER () AS share
            FROM read_parquet('{output}')
            GROUP BY occupancy_group
            ORDER BY building_count DESC;
        """).df()

        height_df = self.con.execute(f"""
            SELECT
                height_source_type,
                COUNT(*) AS building_count,
                COUNT(*) * 1.0 / SUM(COUNT(*)) OVER () AS share
            FROM read_parquet('{output}')
            GROUP BY height_source_type
            ORDER BY building_count DESC;
        """).df()

        source_df = self.con.execute(f"""
            SELECT
                source,
                COUNT(*) AS building_count,
                COUNT(*) * 1.0 / SUM(COUNT(*)) OVER () AS share
            FROM read_parquet('{output}')
            GROUP BY source
            ORDER BY building_count DESC;
        """).df()

        occupancy_df.to_csv(self.profile_dir / "occupancy_distribution.csv", index=False)
        height_df.to_csv(self.profile_dir / "height_source_distribution.csv", index=False)
        source_df.to_csv(self.profile_dir / "source_distribution.csv", index=False)

        logger.info("Detailed profile CSVs written to: %s", self.profile_dir)

    def _write_manifest(self, start_time: float):
        manifest = {
            "created_at_utc": datetime.now(timezone.utc).isoformat(),
            "runtime_seconds": round(time.time() - start_time, 2),
            "config": asdict(self.cfg),
            "output_parquet": str(Path(self.cfg.output_parquet).resolve()),
            "profile_dir": str(self.profile_dir.resolve()),
            "notes": [
                "Geometry is stored as WKB binary in geom_wkb.",
                "Coordinates are EPSG:4326.",
                "footprint_area_m2 is calculated in EPSG:3035.",
                "height_m is normalized from direct metres or estimated from storeys.",
                "attribute_completeness_score is not truth quality; it only measures attribute availability."
            ]
        }

        manifest_path = self.output_dir / "manifest.json"
        manifest_path.write_text(json.dumps(manifest, indent=2, default=str))

        logger.info("Manifest written to: %s", manifest_path)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Extract country buildings from OpenBuildingMap into clean Parquet."
    )

    parser.add_argument("--output-dir", default="./etl_output")
    parser.add_argument("--threads", type=int, default=8)
    parser.add_argument("--memory-limit", default="24GB")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--lon-min", type=float, default=ETLConfig.lon_min)
    parser.add_argument("--lon-max", type=float, default=ETLConfig.lon_max)
    parser.add_argument("--lat-min", type=float, default=ETLConfig.lat_min)
    parser.add_argument("--lat-max", type=float, default=ETLConfig.lat_max)

    parser.add_argument(
        "--sample-only",
        action="store_true",
        help="Run only on a limited sample for testing."
    )

    parser.add_argument(
        "--sample-limit",
        type=int,
        default=100_000,
        help="Number of candidate buildings to process in sample mode."
    )

    parser.add_argument(
        "--output-parquet",
        default=None,
        help="Optional custom output Parquet path."
    )
    parser.add_argument(
        "--boundary-file",
        default=None,
        help="Optional country boundary file: .gpkg, .shp, or .zip containing one."
    )

    return parser.parse_args()


def main():
    args = parse_args()

    output_dir = Path(args.output_dir)

    cfg = ETLConfig(
        output_dir=str(output_dir),
        duckdb_file=str(output_dir / "work_obm.duckdb"),
        temp_directory=str(output_dir / "duckdb_temp"),
        output_parquet=str(
            Path(args.output_parquet)
            if args.output_parquet
            else output_dir / "buildings_de_cleaned.parquet"
        ),
        threads=args.threads,
        memory_limit=args.memory_limit,
        lon_min=args.lon_min,
        lon_max=args.lon_max,
        lat_min=args.lat_min,
        lat_max=args.lat_max,
        force=args.force,
        sample_only=args.sample_only,
        sample_limit=args.sample_limit,
        boundary_file=args.boundary_file,
    )

    etl = OpenBuildingMapCountryETL(cfg)

    try:
        etl.run()
    finally:
        etl.close()


if __name__ == "__main__":
    main()


# Backwards-compatible alias for older scripts/imports.
OpenBuildingMapGermanyETL = OpenBuildingMapCountryETL
