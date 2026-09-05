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
from typing import Callable, List, Optional, Set, Tuple

import duckdb
import pandas as pd
import requests

from country_boundary_catalog import (
    DEFAULT_COUNTRY_BOUNDARY_CATALOG,
    prepare_country_boundary,
)


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

    # Initial fallback bbox. Once the boundary is loaded, its WGS84 extent
    # replaces these values for tile selection and the cheap spatial prefilter.
    lon_min: float = 5.5
    lon_max: float = 15.5
    lat_min: float = 47.0
    lat_max: float = 55.3

    # DuckDB settings
    duckdb_file: str = "./etl_output/work_obm.duckdb"
    threads: int = 4
    memory_limit: str = "12GB"
    temp_directory: str = "./etl_output/duckdb_temp"

    # Output
    output_parquet: str = "./etl_output/buildings_de_cleaned.parquet"
    row_group_size: int = 100_000

    # Assumptions
    metres_per_storey: float = 3.0
    usable_floor_factor: float = 1.0

    # Overlap resolution: when a smaller footprint is substantially covered by a
    # larger one (e.g. a nested/duplicate polygon), drop the smaller footprint
    # and keep only the larger one.
    dedup_overlapping_footprints: bool = True
    overlap_area_ratio_threshold: float = 0.5

    # Behaviour
    force: bool = False
    sample_only: bool = False
    sample_limit: int = 100_000
    obm_quadkey_zoom: int = 6
    chunk_size: int = 2  # Minimum number of quadkey files to process per batch

    # Optional local boundary file (shapefile .zip or .shp, or GeoPackage .gpkg/.zip).
    # When set, the bkg_boundary_zip_url download is skipped.
    boundary_file: Optional[str] = None

    # Optional Natural Earth admin-0 catalog and selected country key.
    country_boundary_catalog: Optional[str] = str(DEFAULT_COUNTRY_BOUNDARY_CATALOG)
    country_key: Optional[str] = None


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
        self._obm_existing_paths: Optional[Set[str]] = None
        self._obm_existing_paths_loaded = False
        self._obm_boundary_quadkeys: Optional[List[str]] = None
        self._obm_input_paths_cache: Optional[List[str]] = None
        self.progress_callback: Optional[Callable[[str, int, Optional[str]], None]] = None

    # ---------------------------------------------------------------------
    # Lifecycle
    # ---------------------------------------------------------------------

    def run(self) -> Path:
        start = time.time()

        logger.info("Starting OpenBuildingMap country ETL")

        self._report_progress("Preparing ETL workspace", 3, "Starting OpenBuildingMap country ETL")
        self._prepare_output_target()
        self._report_progress("Initialising DuckDB", 5, f"DuckDB work file: {Path(self.cfg.duckdb_file).name}")
        self._initialize_duckdb()

        self._report_progress("Loading country boundary", 8, "Resolving selected country boundary")
        boundary_gpkg, boundary_layer, boundary_epsg, boundary_geom_col, boundary_where_sql = self._download_and_prepare_boundary()
        self._report_progress("Building country boundary", 12, f"Boundary source: {Path(boundary_gpkg).name}")
        self._create_country_boundary_table(
            boundary_gpkg,
            boundary_layer,
            boundary_epsg,
            boundary_geom_col,
            boundary_where_sql,
        )
        self._set_bbox_from_country_boundary()

        self._report_progress("Inspecting OpenBuildingMap schema", 18, "Inspecting selected OBM parquet tiles")
        geom_expr = self._detect_obm_geometry_expression()
        self._extract_clean_parquet(geom_expr)

        self._report_progress("Profiling ETL output", 74, f"Profiling {Path(self.cfg.output_parquet).name}")
        self._profile_output()
        self._report_progress("Writing ETL manifest", 78, "Saving ETL summary and profile outputs")
        self._write_manifest(start)

        self.close()

        logger.info("ETL complete")
        logger.info("Output Parquet: %s", self.cfg.output_parquet)

        return Path(self.cfg.output_parquet)

    def close(self):
        if self.con is not None:
            self.con.close()
            self.con = None

    def _report_progress(self, phase: str, percent: int, detail: Optional[str] = None) -> None:
        callback = self.progress_callback
        if callback is None:
            return
        try:
            callback(phase, max(0, min(99, int(percent))), detail)
        except Exception:
            pass

    @staticmethod
    def _chunk_progress_percent(chunk_index: int, total_chunks: int, stage_fraction: float) -> int:
        start = 22
        end = 68
        if total_chunks <= 0:
            return end
        completed = min(float(total_chunks), max(0.0, float(chunk_index) + float(stage_fraction)))
        return max(start, min(end, int(round(start + (end - start) * completed / float(total_chunks)))))

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

        self.con = duckdb.connect(str(Path(self.cfg.duckdb_file)))

        _ensure_duckdb_extension(self.con, "spatial")
        self.con.execute("LOAD spatial;")
        _ensure_duckdb_extension(self.con, "httpfs")
        self.con.execute("LOAD httpfs;")

        self.con.execute(f"SET threads = {self.cfg.threads};")
        self.con.execute(f"SET memory_limit = '{self.cfg.memory_limit}';")
        self.con.execute("SET preserve_insertion_order = false;")
        self.con.execute(f"SET temp_directory = '{str(Path(self.cfg.temp_directory))}';")
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

    def _download_and_prepare_boundary(self) -> Tuple[Path, Optional[str], int, str, Optional[str]]:
        """
        Resolves the country boundary file.

        If ``ETLConfig.boundary_file`` is set, uses that local file (supports
        shapefile .shp, a ZIP containing .shp files, or a .gpkg / ZIP containing
        .gpkg files). Otherwise downloads the default BKG VG250 Germany
        GeoPackage for backwards compatibility.

        Returns:
            boundary_path, layer_name_or_None, source_epsg, geometry_column, where_sql

        ``layer_name_or_None`` is None for plain shapefiles (layer arg omitted).
        """

        if self.cfg.boundary_file:
            boundary_path, layer_name, source_epsg, geometry_column = self._prepare_local_boundary(Path(self.cfg.boundary_file))
            return boundary_path, layer_name, source_epsg, geometry_column, None

        if self.cfg.country_key:
            return self._prepare_catalog_boundary(self.cfg.country_key)

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

        return gpkg_path, layer_name, source_epsg, geometry_column, None

    def _prepare_catalog_boundary(self, country_key: str) -> Tuple[Path, Optional[str], int, str, Optional[str]]:
        catalog_path = Path(self.cfg.country_boundary_catalog or DEFAULT_COUNTRY_BOUNDARY_CATALOG).expanduser()
        if not catalog_path.is_absolute():
            catalog_path = (Path.cwd() / catalog_path).resolve()

        prepared = prepare_country_boundary(catalog_path, country_key, self.boundary_dir / "catalog_cache")
        source_epsg = self._detect_shapefile_epsg(prepared.boundary_file)
        geometry_column = self._detect_shapefile_geom_column(prepared.boundary_file)

        logger.info(
            "Using catalog country boundary: %s (%s) from %s",
            prepared.country_name,
            prepared.country_code,
            prepared.boundary_file,
        )
        return prepared.boundary_file, None, source_epsg, geometry_column, prepared.where_sql

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
            if extract_dir.exists():
                shutil.rmtree(extract_dir)
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

            from pyproj import CRS
            crs = CRS.from_wkt(prj_text)
            epsg = crs.to_epsg()
            if epsg:
                return int(epsg)

            normalized_prj = prj_text.upper().replace(" ", "").replace("_", "")
            if "WGS1984" in normalized_prj or "WGS84" in normalized_prj:
                return 4326

            return 4326
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
        where_sql: Optional[str] = None,
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
            geom_sql = f"ST_Transform({geom_col_sql}, 'EPSG:4326', 'OGC:CRS84', always_xy := true)"
        else:
            geom_sql = (
                f"ST_Transform("
                f"{geom_col_sql}, "
                f"'EPSG:{source_epsg}', "
                f"'OGC:CRS84', "
                f"always_xy := true"
                f")"
            )

        where_clause = f"WHERE {geom_col_sql} IS NOT NULL"
        if where_sql:
            where_clause += f" AND ({where_sql})"

        self.con.execute(f"""
            CREATE OR REPLACE TABLE country_boundary AS
            SELECT ST_Union_Agg({geom_sql}) AS geom
            FROM ST_Read(
                '{file_path_sql}',
                {layer_clause}
                keep_wkb = false
            )
            {where_clause};
        """)

        boundary_check = self.con.execute("""
            SELECT
                ST_GeometryType(geom) AS geom_type,
                ST_IsValid(geom) AS is_valid
            FROM country_boundary;
        """).df()

        logger.info("Boundary check:\n%s", boundary_check)

    def _set_bbox_from_country_boundary(self) -> None:
        """Uses the dissolved WGS84 boundary extent for OBM tile selection."""
        bounds = self.con.execute("""
            SELECT
                ST_XMin(geom),
                ST_YMin(geom),
                ST_XMax(geom),
                ST_YMax(geom)
            FROM country_boundary
            WHERE geom IS NOT NULL;
        """).fetchone()

        if bounds is None or any(value is None or not math.isfinite(float(value)) for value in bounds):
            raise ValueError("Could not determine a valid extent from the boundary file.")

        lon_min, lat_min, lon_max, lat_max = map(float, bounds)
        if not (-180 <= lon_min < lon_max <= 180 and -90 <= lat_min < lat_max <= 90):
            raise ValueError(
                "Boundary extent is outside valid WGS84 longitude/latitude ranges: "
                f"{lon_min}, {lat_min}, {lon_max}, {lat_max}"
            )

        self.cfg.lon_min = lon_min
        self.cfg.lon_max = lon_max
        self.cfg.lat_min = lat_min
        self.cfg.lat_max = lat_max
        logger.info(
            "Using boundary-derived WGS84 extent: lon %.6f to %.6f, lat %.6f to %.6f",
            lon_min,
            lon_max,
            lat_min,
            lat_max,
        )

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

    @staticmethod
    def _tile_bounds(x: int, y: int, zoom: int) -> Tuple[float, float, float, float]:
        n = 2 ** zoom
        lon_min = x / n * 360.0 - 180.0
        lon_max = (x + 1) / n * 360.0 - 180.0
        lat_max = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * y / n))))
        lat_min = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * (y + 1) / n))))
        return lon_min, lat_min, lon_max, lat_max

    def _bbox_covering_quadkeys(self, zoom: int) -> List[str]:
        x_min, y_max = self._lon_lat_to_tile(self.cfg.lon_min, self.cfg.lat_min, zoom)
        x_max, y_min = self._lon_lat_to_tile(self.cfg.lon_max, self.cfg.lat_max, zoom)

        return sorted({
            self._tile_to_quadkey(x, y, zoom)
            for x in range(min(x_min, x_max), max(x_min, x_max) + 1)
            for y in range(min(y_min, y_max), max(y_min, y_max) + 1)
        })

    def _boundary_intersecting_quadkeys(self, zoom: int) -> List[str]:
        if self._obm_boundary_quadkeys is not None:
            return list(self._obm_boundary_quadkeys)

        bbox_quadkeys = self._bbox_covering_quadkeys(zoom)
        if not bbox_quadkeys or self.con is None:
            self._obm_boundary_quadkeys = bbox_quadkeys
            return list(bbox_quadkeys)

        x_min, y_max = self._lon_lat_to_tile(self.cfg.lon_min, self.cfg.lat_min, zoom)
        x_max, y_min = self._lon_lat_to_tile(self.cfg.lon_max, self.cfg.lat_max, zoom)

        candidate_tiles: List[Tuple[int, int, str, float, float, float, float]] = []
        for x in range(min(x_min, x_max), max(x_min, x_max) + 1):
            for y in range(min(y_min, y_max), max(y_min, y_max) + 1):
                lon_min, lat_min, lon_max, lat_max = self._tile_bounds(x, y, zoom)
                candidate_tiles.append((
                    x,
                    y,
                    self._tile_to_quadkey(x, y, zoom),
                    lon_min,
                    lat_min,
                    lon_max,
                    lat_max,
                ))

        values_sql = ",\n                    ".join(
            (
                f"({tile_x}, {tile_y}, '{quadkey}', "
                f"{lon_min:.15f}, {lat_min:.15f}, {lon_max:.15f}, {lat_max:.15f})"
            )
            for tile_x, tile_y, quadkey, lon_min, lat_min, lon_max, lat_max in candidate_tiles
        )

        try:
            rows = self.con.execute(f"""
                WITH candidate_tiles(tile_x, tile_y, quadkey, lon_min, lat_min, lon_max, lat_max) AS (
                    VALUES
                    {values_sql}
                )
                SELECT quadkey
                FROM candidate_tiles t
                CROSS JOIN country_boundary g
                WHERE COALESCE(
                    TRY(
                        ST_Intersects(
                            ST_MakeEnvelope(t.lon_min, t.lat_min, t.lon_max, t.lat_max),
                            g.geom
                        )
                    ),
                    FALSE
                )
                ORDER BY quadkey;
            """).fetchall()
        except Exception as exc:
            logger.warning(
                "Could not compute precise country/tile intersections; falling back to bbox-derived OBM tiles. Error: %s",
                exc,
            )
            self._obm_boundary_quadkeys = bbox_quadkeys
            return list(bbox_quadkeys)

        quadkeys = [str(row[0]) for row in rows if row and row[0]]
        if not quadkeys:
            logger.warning(
                "Precise country/tile intersection returned no OBM tiles; falling back to bbox-derived OBM tiles."
            )
            quadkeys = bbox_quadkeys

        logger.info(
            "Country/tile intersection kept %s of %s bbox-derived OBM tile(s)",
            len(quadkeys),
            len(bbox_quadkeys),
        )
        self._obm_boundary_quadkeys = quadkeys
        return list(quadkeys)

    def _list_existing_obm_paths(self) -> Optional[Set[str]]:
        if self._obm_existing_paths_loaded:
            return self._obm_existing_paths

        self._obm_existing_paths_loaded = True

        if self.con is None or not self.cfg.obm_s3.endswith("*.parquet"):
            return None

        try:
            cursor = self.con.execute("SELECT * FROM glob(?)", [self.cfg.obm_s3])
            rows = cursor.fetchall()
        except Exception as exc:
            logger.warning(
                "Could not enumerate OBM parquet objects for %s; falling back to synthesized paths. Error: %s",
                self.cfg.obm_s3,
                exc,
            )
            return None

        self._obm_existing_paths = {
            str(row[0])
            for row in rows
            if row and row[0]
        }
        logger.info(
            "Discovered %s OBM parquet object(s) from remote listing",
            len(self._obm_existing_paths),
        )
        return self._obm_existing_paths

    def _obm_input_paths(self) -> List[str]:
        """
        OpenBuildingMap files are partitioned by zoom-6 quadkey. Reading only
        intersecting quadkey files avoids listing/scanning the full global prefix.
        """

        if self._obm_input_paths_cache is not None:
            return list(self._obm_input_paths_cache)

        if not self.cfg.obm_s3.endswith("*.parquet"):
            self._obm_input_paths_cache = [self.cfg.obm_s3]
            return list(self._obm_input_paths_cache)

        zoom = int(self.cfg.obm_quadkey_zoom)
        quadkeys = self._boundary_intersecting_quadkeys(zoom)

        prefix = self.cfg.obm_s3[:-len("*.parquet")]
        candidate_paths = [f"{prefix}building.{quadkey}.parquet" for quadkey in quadkeys]
        existing_paths = self._list_existing_obm_paths()

        if existing_paths is None:
            paths = candidate_paths
        else:
            paths = [path for path in candidate_paths if path in existing_paths]
            missing_count = len(candidate_paths) - len(paths)
            if missing_count:
                logger.info(
                    "Skipped %s synthesized OBM quadkey file(s) that are absent from the remote listing",
                    missing_count,
                )
            if not paths:
                raise FileNotFoundError(
                    "No existing OBM parquet files matched the boundary-derived quadkeys. "
                    "The remote catalog did not contain any of the expected tiles."
                )

        preview = ", ".join(paths[:12])
        if len(paths) > 12:
            preview += f", ... (+{len(paths) - 12} more)"
        logger.info("Using %s OBM quadkey Parquet file(s): %s", len(paths), preview)

        self._obm_input_paths_cache = paths
        self._report_progress(
            "Selecting OBM tiles",
            19,
            f"Selected {len(paths):,} OBM quadkey parquet file(s) for the country boundary",
        )
        return list(paths)

    def _effective_chunk_size(self, path_count: int) -> int:
        configured = max(1, int(self.cfg.chunk_size))
        if path_count <= configured:
            return path_count

        # Large countries become dominated by repeated temp-table and boundary
        # intersection overhead if we keep the historical 2-file batch size.
        target_chunk_count = 12
        auto_scaled = max(configured, math.ceil(path_count / target_chunk_count))
        return min(path_count, min(auto_scaled, 32))

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

        output_path = Path(self.cfg.output_parquet)
        obm_paths = self._obm_input_paths()

        # Process in chunks to avoid OOM on large countries
        chunk_size = self._effective_chunk_size(len(obm_paths))
        logger.info(
            "Processing %s OBM file(s) with effective chunk size %s",
            len(obm_paths),
            chunk_size,
        )
        chunks = [obm_paths[i:i + chunk_size] for i in range(0, len(obm_paths), chunk_size)]
        self._report_progress(
            "Processing OBM chunks",
            22,
            f"{len(obm_paths):,} parquet file(s) across {len(chunks):,} chunk(s)",
        )
        chunk_dir = Path(self.cfg.temp_directory) / "chunks"
        chunk_dir.mkdir(parents=True, exist_ok=True)

        chunk_files: List[str] = []

        for chunk_idx, chunk_paths in enumerate(chunks):
            chunk_file = chunk_dir / f"chunk_{chunk_idx:04d}.parquet"
            chunk_paths_sql = self._duckdb_string_list(chunk_paths)
            chunk_label = f"Chunk {chunk_idx + 1}/{len(chunks)}"

            logger.info(
                "Processing chunk %d/%d (%d file(s)): %s",
                chunk_idx + 1, len(chunks), len(chunk_paths),
                ", ".join(chunk_paths),
            )
            self._report_progress(
                "Reading OBM chunk",
                self._chunk_progress_percent(chunk_idx, len(chunks), 0.05),
                f"{chunk_label}: reading {len(chunk_paths):,} parquet file(s)",
            )

            sample_limit_sql = ""
            if self.cfg.sample_only:
                sample_limit_sql = f"LIMIT {int(self.cfg.sample_limit)}"

            # -----------------------------------------------------------
            # STEP 1: Load bbox-filtered candidates into a temp table.
            # This is a cheap filter that avoids holding geometry objects
            # for the entire global dataset in memory.
            # -----------------------------------------------------------
            self.con.execute("DROP TABLE IF EXISTS _tmp_candidates;")
            self.con.execute(f"""
                CREATE TEMP TABLE _tmp_candidates AS
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
                    {chunk_paths_sql},
                    union_by_name = true
                )
                WHERE
                    bbox.xmax >= {self.cfg.lon_min}
                    AND bbox.xmin <= {self.cfg.lon_max}
                    AND bbox.ymax >= {self.cfg.lat_min}
                    AND bbox.ymin <= {self.cfg.lat_max}
                {sample_limit_sql};
            """)

            cand_count = self.con.execute(
                "SELECT COUNT(*) FROM _tmp_candidates;"
            ).fetchone()[0]
            logger.info("Chunk %d: %d bbox-filtered candidates", chunk_idx + 1, cand_count)
            self._report_progress(
                "Filtering candidate buildings",
                self._chunk_progress_percent(chunk_idx, len(chunks), 0.4),
                f"{chunk_label}: {cand_count:,} bbox-filtered candidates",
            )

            if cand_count == 0:
                self.con.execute("DROP TABLE IF EXISTS _tmp_candidates;")
                continue

            # -----------------------------------------------------------
            # STEP 2: Spatial intersection against country boundary.
            # Writing to a table lets DuckDB spill to disk.
            # -----------------------------------------------------------
            self.con.execute("DROP TABLE IF EXISTS _tmp_country;")
            self.con.execute("""
                CREATE TEMP TABLE _tmp_country AS
                SELECT c.*
                FROM _tmp_candidates c, country_boundary g
                WHERE
                    c.geom IS NOT NULL
                    AND COALESCE(TRY(ST_Intersects(c.geom, g.geom)), FALSE);
            """)

            country_count = self.con.execute(
                "SELECT COUNT(*) FROM _tmp_country;"
            ).fetchone()[0]
            logger.info("Chunk %d: %d buildings inside boundary", chunk_idx + 1, country_count)
            self._report_progress(
                "Clipping buildings to country boundary",
                self._chunk_progress_percent(chunk_idx, len(chunks), 0.72),
                f"{chunk_label}: {country_count:,} buildings inside boundary",
            )

            # Free candidates memory
            self.con.execute("DROP TABLE IF EXISTS _tmp_candidates;")

            if country_count == 0:
                self.con.execute("DROP TABLE IF EXISTS _tmp_country;")
                continue

            # -----------------------------------------------------------
            # STEP 2.5: Resolve overlapping footprints, keeping the larger
            # polygon whenever a smaller footprint is mostly covered by it.
            # -----------------------------------------------------------
            self.con.execute("DROP TABLE IF EXISTS _tmp_country_sized;")
            self.con.execute("""
                CREATE TEMP TABLE _tmp_country_sized AS
                SELECT
                    *,
                    ROW_NUMBER() OVER () AS __overlap_row_id,
                    ST_Transform(geom, 'OGC:CRS84', 'EPSG:3035', always_xy := true) AS geom_3035
                FROM _tmp_country;
            """)
            self.con.execute("ALTER TABLE _tmp_country_sized ADD COLUMN footprint_area_m2 DOUBLE;")
            self.con.execute("UPDATE _tmp_country_sized SET footprint_area_m2 = ST_Area(geom_3035);")
            self.con.execute("DROP TABLE IF EXISTS _tmp_country;")

            self.con.execute("DROP TABLE IF EXISTS _tmp_country_deduped;")
            if self.cfg.dedup_overlapping_footprints:
                self.con.execute("DROP TABLE IF EXISTS _tmp_country_overlap_candidates;")
                self.con.execute("DROP TABLE IF EXISTS _tmp_country_overlapped;")
                self.con.execute("""
                    CREATE TEMP TABLE _tmp_country_overlap_candidates AS
                    SELECT
                        s.__overlap_row_id AS smaller_row_id,
                        o.__overlap_row_id AS larger_row_id
                    FROM _tmp_country_sized s
                    JOIN _tmp_country_sized o
                        ON ST_Intersects(o.geom, s.geom);
                """)
                self.con.execute(f"""
                    CREATE TEMP TABLE _tmp_country_overlapped AS
                    SELECT DISTINCT s.__overlap_row_id
                    FROM _tmp_country_overlap_candidates c
                    JOIN _tmp_country_sized s ON s.__overlap_row_id = c.smaller_row_id
                    JOIN _tmp_country_sized o ON o.__overlap_row_id = c.larger_row_id
                    WHERE o.id <> s.id
                        AND (
                            o.footprint_area_m2 > s.footprint_area_m2
                            OR (o.footprint_area_m2 = s.footprint_area_m2 AND o.id > s.id)
                        )
                        AND COALESCE(TRY(
                            ST_Area(ST_Intersection(o.geom_3035, s.geom_3035))
                                / NULLIF(s.footprint_area_m2, 0)
                        ), 0) >= {self.cfg.overlap_area_ratio_threshold};
                """)
                self.con.execute("DROP TABLE IF EXISTS _tmp_country_overlap_candidates;")
                self.con.execute("""
                    CREATE TEMP TABLE _tmp_country_deduped AS
                    SELECT s.* EXCLUDE (__overlap_row_id)
                    FROM _tmp_country_sized s
                    ANTI JOIN _tmp_country_overlapped d
                        ON d.__overlap_row_id = s.__overlap_row_id;
                """)
                self.con.execute("DROP TABLE IF EXISTS _tmp_country_overlapped;")
            else:
                self.con.execute("""
                    CREATE TEMP TABLE _tmp_country_deduped AS
                    SELECT * EXCLUDE (__overlap_row_id) FROM _tmp_country_sized;
                """)
            self.con.execute("DROP TABLE IF EXISTS _tmp_country_sized;")

            deduped_count = self.con.execute(
                "SELECT COUNT(*) FROM _tmp_country_deduped;"
            ).fetchone()[0]
            dropped_count = country_count - deduped_count
            logger.info(
                "Chunk %d: dropped %d smaller overlapping footprint(s), %d remain",
                chunk_idx + 1, dropped_count, deduped_count,
            )
            self._report_progress(
                "Resolving overlapping footprints",
                self._chunk_progress_percent(chunk_idx, len(chunks), 0.76),
                f"{chunk_label}: dropped {dropped_count:,} overlapping footprint(s), {deduped_count:,} remain",
            )

            # -----------------------------------------------------------
            # STEP 3: Enrich and write to chunk Parquet file.
            # -----------------------------------------------------------
            chunk_output_sql = chunk_file.as_posix().replace("'", "''")

            self.con.execute(f"""
            COPY (
                WITH height_parsed AS (
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

                    FROM _tmp_country_deduped
                ),

                measured AS (
                    SELECT
                        *,
                        ST_Centroid(geom) AS centroid_geom
                    FROM height_parsed
                ),

                enriched AS (
                    SELECT
                        CAST(id AS VARCHAR) AS building_id,
                        source,
                        relation_id,
                        quadkey,
                        SUBSTR(CAST(quadkey AS VARCHAR), 1, 6) AS quadkey_prefix_6,
                        last_update,

                        ST_AsWKB(geom) AS geom_wkb,

                        CASE WHEN centroid_geom IS NOT NULL THEN ST_X(centroid_geom) ELSE NULL END AS centroid_lon,
                        CASE WHEN centroid_geom IS NOT NULL THEN ST_Y(centroid_geom) ELSE NULL END AS centroid_lat,

                        bbox.xmin AS bbox_xmin,
                        bbox.ymin AS bbox_ymin,
                        bbox.xmax AS bbox_xmax,
                        bbox.ymax AS bbox_ymax,

                        footprint_area_m2,

                        height AS height_raw,
                        occupancy AS occupancy_raw,
                        floorspace_obm_m2,

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
            TO '{chunk_output_sql}'
            (
                FORMAT PARQUET,
                COMPRESSION ZSTD,
                ROW_GROUP_SIZE {self.cfg.row_group_size}
            );
            """)

            self.con.execute("DROP TABLE IF EXISTS _tmp_country_deduped;")
            chunk_files.append(chunk_file.as_posix())
            logger.info("Chunk %d written: %s", chunk_idx + 1, chunk_file)
            self._report_progress(
                "Writing chunk parquet",
                self._chunk_progress_percent(chunk_idx, len(chunks), 1.0),
                f"{chunk_label}: wrote {chunk_file.name}",
            )

        # Combine all chunk files into the final output via streaming read
        if not chunk_files:
            raise RuntimeError("No buildings matched the country boundary.")

        final_output_sql = output_path.as_posix().replace("'", "''")
        chunk_list_sql = self._duckdb_string_list(chunk_files)

        logger.info("Combining %d chunk file(s) into final output: %s", len(chunk_files), output_path)
        self._report_progress(
            "Combining chunk parquet files",
            70,
            f"Combining {len(chunk_files):,} chunk file(s) into {output_path.name}",
        )
        self.con.execute(f"""
            COPY (
                SELECT * FROM read_parquet({chunk_list_sql})
            )
            TO '{final_output_sql}'
            (
                FORMAT PARQUET,
                COMPRESSION ZSTD,
                ROW_GROUP_SIZE {self.cfg.row_group_size}
            );
        """)

        # Clean up chunk files
        for cf in chunk_files:
            try:
                Path(cf).unlink(missing_ok=True)
            except Exception:
                pass
        try:
            chunk_dir.rmdir()
        except Exception:
            pass

        logger.info("Parquet written: %s", output_path)
        self._report_progress("Wrote ETL parquet", 72, f"Parquet written: {output_path.name}")

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
    parser.add_argument("--threads", type=int, default=4)
    parser.add_argument("--memory-limit", default="12GB")
    parser.add_argument("--force", action="store_true")
    parser.add_argument(
        "--chunk-size",
        type=int,
        default=2,
        help="Minimum number of quadkey files to process per batch. Large runs auto-scale upward to reduce chunk overhead."
    )

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
    parser.add_argument(
        "--country-boundary-catalog",
        default=str(DEFAULT_COUNTRY_BOUNDARY_CATALOG),
        help="Path to a Natural Earth admin-0 all-countries zip used for dropdown country selection."
    )
    parser.add_argument(
        "--country-key",
        default=None,
        help="Selected country key from the boundary catalog, for example ADM0_A3:GBR."
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
        force=args.force,
        sample_only=args.sample_only,
        sample_limit=args.sample_limit,
        boundary_file=args.boundary_file,
        country_boundary_catalog=args.country_boundary_catalog,
        country_key=args.country_key,
        chunk_size=args.chunk_size,
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
