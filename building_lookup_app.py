import argparse
import json
import math
import subprocess
import sys
import time
import urllib.parse
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from threading import Lock, Thread, local
from typing import Any, Dict, List, Optional

import duckdb
import pandas as pd
from flask import Flask, jsonify, render_template, request, send_file
from werkzeug.utils import secure_filename

from obm_country_to_parquet import ETLConfig, OpenBuildingMapCountryETL


DEFAULT_PARQUET = "etl_output/buildings_de_cleaned.parquet"
DEFAULT_DB = "etl_output/building_lookup.duckdb"
ENRICHMENT_CHUNK_SIZE = 5000
ENRICHMENT_WORKERS = 8
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


def sql_string(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def sql_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def json_safe(value: Any) -> Any:
    if pd.isna(value):
        return None
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return value


def fetch_geocoder_json(url: str, user_agent: str) -> Any:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": user_agent,
            "Accept": "application/json",
        },
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
    params = urllib.parse.urlencode({
        "q": query,
        "limit": 5,
    })
    raw_results = fetch_geocoder_json(
        f"https://photon.komoot.io/api/?{params}",
        user_agent,
    )
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
        if not label:
            continue

        results.append({
            "label": label,
            "lon": float(coordinates[0]),
            "lat": float(coordinates[1]),
            "type": properties.get("osm_value"),
            "provider": "Photon",
        })

    return results


def detect_csv_encoding(csv_path: Path) -> str:
    for encoding in ("utf-8-sig", "utf-8", "cp1252", "latin1"):
        try:
            pd.read_csv(csv_path, nrows=5, encoding=encoding)
            return encoding
        except UnicodeDecodeError:
            continue

    return "latin1"


def open_db(db_path: str, read_only: bool = False) -> duckdb.DuckDBPyConnection:
    con = duckdb.connect(db_path, read_only=read_only)
    con.execute("LOAD spatial;")
    return con


def display_path(path: Path) -> str:
    try:
        return path.resolve().relative_to(Path.cwd().resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def find_local_files(suffix: str) -> List[str]:
    ignored_dirs = {".git", ".venv", "__pycache__"}
    matches = []
    for path in Path.cwd().rglob(f"*{suffix}"):
        if any(part in ignored_dirs for part in path.parts):
            continue
        if path.is_file():
            matches.append(display_path(path))
    return sorted(set(matches))


def validate_local_file(path_value: str, suffix: str, label: str) -> str:
    path = Path(path_value).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    if not path.exists() or not path.is_file():
        raise ValueError(f"{label} file does not exist: {path_value}")
    if path.suffix.lower() != suffix:
        raise ValueError(f"{label} file must end with {suffix}: {path_value}")
    return display_path(path)


def browse_local_file(kind: str) -> Optional[str]:
    choices = {
        "parquet": (".parquet", "Parquet", "Select Parquet file"),
        "db": (".duckdb", "DuckDB", "Select DuckDB lookup database"),
    }
    if kind not in choices:
        raise ValueError("File type must be parquet or db.")

    suffix, label, title = choices[kind]

    if sys.platform == "darwin":
        script = f'POSIX path of (choose file with prompt "{title}")'
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode == 0:
            selected = result.stdout.strip()
            return validate_local_file(selected, suffix, label)
        if "User canceled" in result.stderr:
            return None
        raise RuntimeError(result.stderr.strip() or "macOS file picker failed.")

    import tkinter as tk
    from tkinter import filedialog

    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    try:
        selected = filedialog.askopenfilename(
            title=title,
            initialdir=str(Path.cwd()),
            filetypes=[(f"{label} files", f"*{suffix}"), ("All files", "*.*")],
        )
    finally:
        root.destroy()

    if not selected:
        return None
    return validate_local_file(selected, suffix, label)


def browse_local_folder() -> Optional[str]:
    title = "Select output folder"

    if sys.platform == "darwin":
        script = f'POSIX path of (choose folder with prompt "{title}")'
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode == 0:
            selected = Path(result.stdout.strip())
            return display_path(selected)
        if "User canceled" in result.stderr:
            return None
        raise RuntimeError(result.stderr.strip() or "macOS folder picker failed.")

    import tkinter as tk
    from tkinter import filedialog

    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    try:
        selected = filedialog.askdirectory(
            title=title,
            initialdir=str(Path.cwd()),
            mustexist=True,
        )
    finally:
        root.destroy()

    if not selected:
        return None
    return display_path(Path(selected))


def has_buildings_table(db_path: str) -> bool:
    con = open_db(db_path, read_only=True)
    try:
        return bool(con.execute("""
            SELECT COUNT(*)
            FROM information_schema.tables
            WHERE table_name = 'buildings';
        """).fetchone()[0])
    finally:
        con.close()


def lookup_db_path_for_parquet(parquet_path: str) -> str:
    parquet = Path(parquet_path)
    if not parquet.is_absolute():
        parquet = Path.cwd() / parquet

    name = parquet.stem
    for prefix in ("buildings_cleaned_", "buildings_de_cleaned", "buildings_cleaned"):
        if name == prefix:
            name = "building_lookup"
            break
        if name.startswith(prefix):
            name = "building_lookup_" + name[len(prefix):]
            break
    else:
        name = f"{name}_lookup"

    return display_path(parquet.with_name(f"{name}.duckdb"))


def prepare_index(parquet_path: str, db_path: str, force: bool = False, threads: int = 8) -> None:
    parquet = Path(parquet_path)
    if not parquet.exists():
        raise FileNotFoundError(f"Parquet file not found: {parquet}")

    db = Path(db_path)
    db.parent.mkdir(parents=True, exist_ok=True)

    if db.exists() and force:
        db.unlink()

    con = open_db(db_path)
    con.execute(f"SET threads = {int(threads)};")

    if not force:
        exists = con.execute("""
            SELECT COUNT(*)
            FROM information_schema.tables
            WHERE table_name = 'buildings';
        """).fetchone()[0]
        if exists:
            print(f"Index database already exists: {db_path}")
            print("Use --force to rebuild it.")
            return

    parquet_sql = sql_string(parquet.as_posix())

    print("Creating lookup table from Parquet. This is a one-time step. Generating 3035 Projections (May take a few minutes)...")
    
    # Notice the new geom_3035 and bbox_3035_* columns!
    con.execute(f"""
        CREATE OR REPLACE TABLE buildings AS
        WITH raw_buildings AS (
            SELECT
                *,
                ST_GeomFromWKB(geom_wkb) AS geom
            FROM read_parquet({parquet_sql})
        ),
        projected_buildings AS (
            SELECT 
                *,
                ST_Transform(geom, 'EPSG:4326', 'EPSG:3035', always_xy := true) AS geom_3035
            FROM raw_buildings
        )
        SELECT
            building_id,
            source,
            relation_id,
            quadkey,
            quadkey_prefix_6,
            CAST(last_update AS VARCHAR) AS last_update,
            centroid_lon,
            centroid_lat,
            bbox_xmin,
            bbox_ymin,
            bbox_xmax,
            bbox_ymax,
            footprint_area_m2,
            height_raw,
            occupancy_raw,
            floorspace_obm_m2,
            height_source_type,
            height_m,
            stories_exact,
            stories_min,
            stories_max,
            height_quality,
            occupancy_code,
            occupancy_group,
            occupancy_quality,
            floorspace_est_m2,
            attribute_completeness_score,
            geom,
            geom_3035,
            ST_XMin(geom_3035) AS bbox_3035_xmin,
            ST_YMin(geom_3035) AS bbox_3035_ymin,
            ST_XMax(geom_3035) AS bbox_3035_xmax,
            ST_YMax(geom_3035) AS bbox_3035_ymax
        FROM projected_buildings
        ORDER BY quadkey_prefix_6, bbox_xmin, bbox_ymin;
    """)

    print("Creating spatial index.")
    con.execute("CREATE INDEX buildings_geom_rtree ON buildings USING RTREE (geom);")
    con.execute("CREATE INDEX buildings_geom_3035_rtree ON buildings USING RTREE (geom_3035);")

    row_count = con.execute("SELECT COUNT(*) FROM buildings;").fetchone()[0]
    con.close()
    print(f"Ready: {db_path} ({row_count:,} buildings)")


def create_app(db_path: str = DEFAULT_DB, nearest_radius_m: float = 50.0) -> Flask:
    app = Flask(__name__)
    app.config["DB_PATH"] = db_path
    app.config["PARQUET_PATH"] = DEFAULT_PARQUET
    app.config["NEAREST_RADIUS_M"] = float(nearest_radius_m)
    app.config["GEOCODER_USER_AGENT"] = "OBMBuildingLookup/0.1 local-development"
    app.config["UPLOAD_DIR"] = "etl_output/app_uploads"
    app.config["RESULT_DIR"] = "etl_output/app_results"
    geocode_cache: Dict[str, Any] = {}
    last_geocode_at = [0.0]
    jobs: Dict[str, Dict[str, Any]] = {}
    jobs_lock = Lock()
    Path(app.config["UPLOAD_DIR"]).mkdir(parents=True, exist_ok=True)
    Path(app.config["RESULT_DIR"]).mkdir(parents=True, exist_ok=True)

    def set_job(job_id: str, **updates: Any) -> None:
        with jobs_lock:
            jobs.setdefault(job_id, {}).update(updates)

    @app.route("/")
    def index():
        return render_template("index.html")

    @app.route("/api/health")
    def health():
        db = Path(app.config["DB_PATH"])
        parquet = Path(app.config["PARQUET_PATH"])
        return jsonify({
            "ok": db.exists(),
            "db_path": str(db),
            "parquet_path": str(parquet),
        })

    @app.route("/api/data-source", methods=["GET", "POST"])
    def data_source():
        if request.method == "GET":
            return jsonify({
                "parquet_path": app.config["PARQUET_PATH"],
                "db_path": app.config["DB_PATH"],
                "parquet_files": find_local_files(".parquet"),
                "db_files": find_local_files(".duckdb"),
            })

        payload = request.get_json(silent=True) or {}
        parquet_path = str(payload.get("parquet_path", "")).strip()
        db_path = str(payload.get("db_path", "")).strip()

        if not parquet_path or not db_path:
            return jsonify({"error": "Parquet and DuckDB paths are required."}), 400

        try:
            parquet_path = validate_local_file(parquet_path, ".parquet", "Parquet")
            db_path = validate_local_file(db_path, ".duckdb", "DuckDB")
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400

        generated_lookup = False
        try:
            has_buildings = has_buildings_table(db_path)
        except Exception as exc:
            return jsonify({"error": f"Could not open DuckDB lookup database: {exc}"}), 400

        if not has_buildings:
            generated_db_path = lookup_db_path_for_parquet(parquet_path)
            try:
                prepare_index(parquet_path, generated_db_path, force=True, threads=8)
                has_buildings = has_buildings_table(generated_db_path)
            except Exception as exc:
                return jsonify({
                    "error": (
                        "The selected DuckDB file is an ETL work database, not a lookup database, "
                        f"and creating a lookup database from the Parquet failed: {exc}"
                    )
                }), 400

            if not has_buildings:
                return jsonify({"error": "Could not create a buildings lookup table from the selected Parquet."}), 400

            db_path = generated_db_path
            generated_lookup = True

        app.config["PARQUET_PATH"] = parquet_path
        app.config["DB_PATH"] = db_path
        return jsonify({
            "parquet_path": parquet_path,
            "db_path": db_path,
            "status": "active",
            "generated_lookup": generated_lookup,
        })

    @app.route("/api/browse-file")
    def browse_file():
        kind = str(request.args.get("kind", "")).strip()
        try:
            selected_path = browse_local_file(kind)
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        except Exception as exc:
            return jsonify({
                "error": (
                    "Native file picker is not available from this Flask session. "
                    f"{exc}"
                )
            }), 501

        if selected_path is None:
            return jsonify({"cancelled": True})
        return jsonify({"path": selected_path})

    @app.route("/api/browse-folder")
    def browse_folder():
        try:
            selected_path = browse_local_folder()
        except Exception as exc:
            return jsonify({
                "error": (
                    "Native folder picker is not available from this Flask session. "
                    f"{exc}"
                )
            }), 501

        if selected_path is None:
            return jsonify({"cancelled": True})
        return jsonify({"path": selected_path})

    @app.route("/api/building-at")
    def building_at():
        try:
            lon = float(request.args["lon"])
            lat = float(request.args["lat"])
        except (KeyError, ValueError):
            return jsonify({"error": "Valid lon and lat query parameters are required."}), 400

        if not (-180 <= lon <= 180 and -90 <= lat <= 90):
            return jsonify({"error": "Coordinates are out of range."}), 400

        db_path = app.config["DB_PATH"]
        if not Path(db_path).exists():
            return jsonify({
                "error": "Lookup database has not been prepared.",
                "hint": "Run: python building_lookup_app.py prepare-index"
            }), 503

        con = open_db(db_path, read_only=True)
        try:
            result = find_building(con, lon, lat, app.config["NEAREST_RADIUS_M"])
        finally:
            con.close()

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
            return jsonify({
                "error": "Address search failed. " + " | ".join(errors)
            }), 502

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
                    db_path=app.config["DB_PATH"],
                    csv_path=upload_path,
                    output_path=output_path,
                    lat_col=lat_col,
                    lon_col=lon_col,
                    mode=mode,
                    max_distance_m=max_distance_m,
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

    # ------------------------------------------------------------------
    # ETL: Create OBM Database
    # ------------------------------------------------------------------

    etl_jobs: Dict[str, Dict[str, Any]] = {}
    etl_jobs_lock = Lock()

    def set_etl_job(job_id: str, **updates: Any) -> None:
        with etl_jobs_lock:
            etl_jobs.setdefault(job_id, {}).update(updates)

    @app.route("/api/etl/create-database", methods=["POST"])
    def etl_create_database():
        # ---------- boundary file (optional) ----------
        boundary_file_path: Optional[str] = None
        boundary_file = request.files.get("boundary_file")
        if boundary_file and boundary_file.filename:
            filename = secure_filename(boundary_file.filename)
            ext = Path(filename).suffix.lower()
            if ext not in {".zip", ".gpkg", ".shp"}:
                return jsonify({"error": "Boundary file must be a .zip, .gpkg, or .shp."}), 400
            upload_dir = Path(app.config["UPLOAD_DIR"])
            saved_path = upload_dir / f"{uuid.uuid4().hex}_{filename}"
            boundary_file.save(saved_path)
            boundary_file_path = str(saved_path)

        # ---------- config fields ----------
        def _float(key: str, default: float) -> float:
            try:
                return float(request.form.get(key, default))
            except (TypeError, ValueError):
                return default

        def _str(key: str, default: str) -> str:
            val = request.form.get(key, "").strip()
            return val if val else default

        output_dir = _str("output_dir", "./etl_output")

        def _output_path(key: str, default_filename: str, suffix: str, label: str) -> str:
            raw_value = request.form.get(key, "").strip()
            path = Path(raw_value) if raw_value else Path(output_dir) / default_filename
            if raw_value and not path.is_absolute() and path.parent == Path("."):
                path = Path(output_dir) / path
            if path.suffix.lower() != suffix:
                raise ValueError(f"{label} must end with {suffix}.")
            return path.as_posix()

        try:
            output_parquet = _output_path("output_parquet", "buildings_cleaned.parquet", ".parquet", "Parquet output")
            duckdb_file = _output_path("duckdb_file", "work_obm.duckdb", ".duckdb", "DuckDB work file")
            lookup_db_file = _output_path("lookup_db_file", "building_lookup.duckdb", ".duckdb", "DuckDB lookup table")
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400

        duckdb_abs = (Path.cwd() / duckdb_file).resolve() if not Path(duckdb_file).is_absolute() else Path(duckdb_file).resolve()
        lookup_abs = (Path.cwd() / lookup_db_file).resolve() if not Path(lookup_db_file).is_absolute() else Path(lookup_db_file).resolve()
        if duckdb_abs == lookup_abs:
            return jsonify({"error": "DuckDB work file and DuckDB lookup table must be different files."}), 400

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
            lookup_db_file=lookup_db_file,
        )

        def run_etl() -> None:
            try:
                set_etl_job(job_id, phase="Initialising DuckDB", percent=5)
                etl = OpenBuildingMapCountryETL(cfg)
                etl.run()
                set_etl_job(job_id, phase="Creating DuckDB lookup table", percent=85)
                prepare_index(cfg.output_parquet, lookup_db_file, force=True, threads=8)
                app.config["PARQUET_PATH"] = display_path(Path(cfg.output_parquet))
                app.config["DB_PATH"] = display_path(Path(lookup_db_file))
                set_etl_job(job_id, status="complete", phase="Complete", percent=100)
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


def find_upload(upload_dir: Path, upload_id: str) -> Optional[Path]:
    matches = list(upload_dir.glob(f"{upload_id}_*.csv"))
    return matches[0] if matches else None


def preview_csv(csv_path: Path) -> tuple[List[str], List[Dict[str, Any]]]:
    encoding = detect_csv_encoding(csv_path)
    frame = pd.read_csv(csv_path, nrows=10, encoding=encoding)
    columns = list(frame.columns)
    rows = [
        {column: json_safe(value) for column, value in row.items()}
        for row in frame.to_dict(orient="records")
    ]

    return columns, rows


def csv_columns(con: duckdb.DuckDBPyConnection, csv_path: Path) -> List[str]:
    encoding = detect_csv_encoding(csv_path)
    return list(pd.read_csv(csv_path, nrows=0, encoding=encoding).columns)


def b_select(alias: str = "b") -> str:
    return ",\n            ".join(f"{alias}.{sql_identifier(col)} AS {sql_identifier(col)}" for col in BUILDING_COLUMNS)


def null_building_select() -> str:
    return ",\n            ".join(f"NULL AS {sql_identifier(col)}" for col in BUILDING_COLUMNS)


def final_building_select(source: str) -> str:
    return ",\n            ".join(
        f"{source}.{sql_identifier(col)} AS {sql_identifier('building_' + col)}"
        for col in BUILDING_COLUMNS
    )


def final_coalesced_building_select() -> str:
    return ",\n            ".join(
        f"COALESCE(i.{sql_identifier(col)}, n.{sql_identifier(col)}) AS {sql_identifier('building_' + col)}"
        for col in BUILDING_COLUMNS
    )


def exposure_select(columns: List[str]) -> str:
    return ",\n            ".join(f"e.{sql_identifier(col)}" for col in columns)


def count_csv_rows(csv_path: Path) -> int:
    with csv_path.open("rb") as handle:
        line_count = sum(1 for _ in handle)

    return max(line_count - 1, 0)


def enrich_exposure_csv(
    db_path: str,
    csv_path: Path,
    output_path: Path,
    lat_col: str,
    lon_col: str,
    mode: str,
    max_distance_m: float,
    progress_callback=None,
) -> Dict[str, Any]:
    if progress_callback:
        progress_callback("Opening lookup database", 5)

    con = open_db(db_path, read_only=True)
    con.execute("SET threads = 1;")

    if progress_callback:
        progress_callback("Inspecting CSV columns", 10)

    columns = csv_columns(con, csv_path)
    if lat_col not in columns or lon_col not in columns:
        raise ValueError("Selected latitude/longitude columns were not found in the CSV.")

    radius = float(max_distance_m)
    encoding = detect_csv_encoding(csv_path)
    total_rows = count_csv_rows(csv_path)
    processed_rows = 0
    header_written = False

    if progress_callback:
        progress_callback(f"Running spatial enrichment: 0/{total_rows:,} rows", 15)

    summary = {
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
    con.close()
    worker_state = local()

    def worker_connection() -> duckdb.DuckDBPyConnection:
        if not hasattr(worker_state, "con"):
            worker_state.con = open_db(db_path, read_only=True)
            worker_state.con.execute("SET threads = 1;")
        return worker_state.con

    def enrich_record(index: int, record: Dict[str, Any]) -> tuple[int, Dict[str, Any]]:
        lookup = lookup_exposure_row(
            con=worker_connection(),
            lon=record.get(lon_col),
            lat=record.get(lat_col),
            mode=mode,
            radius_m=radius,
        )
        return index, {**record, **lookup}

    with ThreadPoolExecutor(max_workers=ENRICHMENT_WORKERS) as executor:
        for chunk in pd.read_csv(csv_path, chunksize=ENRICHMENT_CHUNK_SIZE, encoding=encoding):
            records = chunk.to_dict(orient="records")
            enriched_records: List[Optional[Dict[str, Any]]] = [None] * len(records)
            futures = [
                executor.submit(enrich_record, index, record)
                for index, record in enumerate(records)
            ]

            for future in as_completed(futures):
                index, enriched_record = future.result()
                enriched_records[index] = enriched_record
                processed_rows += 1

                if progress_callback and (processed_rows % 100 == 0 or processed_rows == total_rows):
                    percent = 15 + int((processed_rows / max(total_rows, 1)) * 75)
                    progress_callback(
                        f"Running spatial enrichment: {processed_rows:,}/{total_rows:,} rows",
                        min(percent, 90),
                    )

            enriched = pd.DataFrame([record for record in enriched_records if record is not None])
            enriched.to_csv(output_path, mode="a", index=False, header=not header_written)
            header_written = True

            update_summary(summary, enriched)

    if progress_callback:
        progress_callback("Finalizing summary", 95)

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


def update_summary(summary: Dict[str, Any], enriched: pd.DataFrame) -> None:
    summary["total_rows"] += len(enriched)
    summary["valid_coordinate_rows"] += int(enriched["coordinate_valid"].fillna(False).sum())
    summary["inside_polygon_matches"] += int((enriched["building_match_type"] == "inside_polygon").sum())
    summary["nearest_matches"] += int(
        enriched["building_match_type"].isin(["nearest_polygon", "nearest_centroid"]).sum()
    )
    summary["no_matches"] += int((enriched["building_match_type"] == "none").sum())

    nearest_distances = enriched.loc[
        enriched["building_match_type"].isin(["nearest_polygon", "nearest_centroid"]),
        "building_distance_m",
    ].dropna()
    summary["nearest_distance_total_m"] += float(nearest_distances.sum())
    summary["nearest_distance_count"] += int(nearest_distances.count())

    add_distribution(summary["detailed_occupancy"], enriched["building_occupancy_raw"])
    add_distribution(summary["occupancy_group"], enriched["building_occupancy_group"])


def add_distribution(target: Dict[str, int], series: pd.Series) -> None:
    values = series.dropna().replace("", pd.NA).dropna().astype(str)

    for value, count in values.value_counts().items():
        target[value] = target.get(value, 0) + int(count)


def distribution_to_rows(distribution: Dict[str, int]) -> List[Dict[str, Any]]:
    return [
        {"name": name, "count": count}
        for name, count in sorted(distribution.items(), key=lambda item: item[1], reverse=True)
    ]


def prefixed_building_columns() -> List[str]:
    return [f"building_{column}" for column in BUILDING_COLUMNS]


def empty_lookup_result() -> Dict[str, Any]:
    result = {
        "coordinate_valid": False,
        "building_match_type": "none",
        "building_distance_m": None,
        "building_confidence": "none",
    }
    result.update({column: None for column in prefixed_building_columns()})
    return result


def lookup_exposure_row(
    con: duckdb.DuckDBPyConnection,
    lon: Any,
    lat: Any,
    mode: str,
    radius_m: float,
) -> Dict[str, Any]:
    try:
        lon_value = float(lon)
        lat_value = float(lat)
    except (TypeError, ValueError):
        return empty_lookup_result()

    if not (-180 <= lon_value <= 180 and -90 <= lat_value <= 90):
        return empty_lookup_result()

    if mode == "centroid":
        row = lookup_nearest_centroid(con, lon_value, lat_value, radius_m)
        return row_to_enrichment_result(row, "nearest_centroid" if row else "none", row[0] if row else None)

    inside = lookup_inside_polygon(con, lon_value, lat_value)
    if inside:
        return row_to_enrichment_result(inside, "inside_polygon", 0.0)

    if mode == "inside":
        result = empty_lookup_result()
        result["coordinate_valid"] = True
        return result

    candidate_radius_m = max(radius_m * 4.0, radius_m + 150.0)
    if lookup_nearest_centroid(con, lon_value, lat_value, candidate_radius_m) is None:
        result = empty_lookup_result()
        result["coordinate_valid"] = True
        return result

    nearest = lookup_nearest_polygon(con, lon_value, lat_value, radius_m)
    return row_to_enrichment_result(nearest, "nearest_polygon" if nearest else "none", nearest[0] if nearest else None)


def lookup_inside_polygon(
    con: duckdb.DuckDBPyConnection,
    lon: float,
    lat: float,
) -> Optional[tuple]:
    return con.execute(f"""
        WITH point AS (
            SELECT ST_Point(?, ?) AS pt
        )
        SELECT
            {b_select("b")}
        FROM buildings b, point
        WHERE
            ? BETWEEN b.bbox_xmin AND b.bbox_xmax
            AND ? BETWEEN b.bbox_ymin AND b.bbox_ymax
            AND ST_Intersects(b.geom, point.pt)
        ORDER BY b.footprint_area_m2 ASC NULLS LAST
        LIMIT 1;
    """, [lon, lat, lon, lat]).fetchone()


def lookup_nearest_centroid(
    con: duckdb.DuckDBPyConnection,
    lon: float,
    lat: float,
    radius_m: float,
) -> Optional[tuple]:
    lat_delta = radius_m / 111_320.0
    lon_delta = radius_m / (111_320.0 * max(math.cos(math.radians(lat)), 0.2))

    return con.execute(f"""
        WITH point AS (
            SELECT ST_Point(?, ?) AS pt
        )
        SELECT
            ST_Distance_Sphere(ST_Point(b.centroid_lon, b.centroid_lat), point.pt) AS distance_m,
            {b_select("b")}
        FROM buildings b, point
        WHERE
            b.centroid_lon BETWEEN ? AND ?
            AND b.centroid_lat BETWEEN ? AND ?
            AND ST_Distance_Sphere(ST_Point(b.centroid_lon, b.centroid_lat), point.pt) <= ?
        ORDER BY distance_m
        LIMIT 1;
    """, [
        lon,
        lat,
        lon - lon_delta,
        lon + lon_delta,
        lat - lat_delta,
        lat + lat_delta,
        radius_m,
    ]).fetchone()


def lookup_nearest_polygon(
    con: duckdb.DuckDBPyConnection,
    lon: float,
    lat: float,
    radius_m: float,
) -> Optional[tuple]:
    candidate_radius_m = max(radius_m * 4.0, radius_m + 150.0)
    candidate_lat_delta = candidate_radius_m / 111_320.0
    candidate_lon_delta = candidate_radius_m / (111_320.0 * max(math.cos(math.radians(lat)), 0.2))

    return con.execute(f"""
        WITH point AS (
            SELECT
                ST_Point(?, ?) AS pt,
                ST_Transform(ST_Point(?, ?), 'EPSG:4326', 'EPSG:3035', always_xy := true) AS pt_m
        ),
        candidates AS MATERIALIZED (
            SELECT
                ST_Distance_Sphere(ST_Point(b.centroid_lon, b.centroid_lat), point.pt) AS centroid_distance_m,
                b.*
            FROM buildings b, point
            WHERE
                b.centroid_lon BETWEEN ? AND ?
                AND b.centroid_lat BETWEEN ? AND ?
                AND ST_Distance_Sphere(ST_Point(b.centroid_lon, b.centroid_lat), point.pt) <= ?
            ORDER BY centroid_distance_m
            LIMIT 500
        )
        SELECT
            ST_Distance(b.geom_3035, point.pt_m) AS distance_m,
            {b_select("b")}
        FROM candidates b, point
        WHERE
            ST_DWithin(b.geom_3035, point.pt_m, ?)
        ORDER BY distance_m
        LIMIT 1;
    """, [
        lon,
        lat,
        lon,
        lat,
        lon - candidate_lon_delta,
        lon + candidate_lon_delta,
        lat - candidate_lat_delta,
        lat + candidate_lat_delta,
        candidate_radius_m,
        radius_m,
    ]).fetchone()


def row_to_enrichment_result(
    row: Optional[tuple],
    match_type: str,
    distance_m: Optional[float],
) -> Dict[str, Any]:
    result = empty_lookup_result()
    result["coordinate_valid"] = True
    result["building_match_type"] = match_type
    result["building_distance_m"] = distance_m

    if row is None:
        return result

    building_values = row[1:] if match_type in {"nearest_polygon", "nearest_centroid"} else row
    building = dict(zip(BUILDING_COLUMNS, building_values))

    if match_type == "inside_polygon":
        confidence = "high"
    elif distance_m is not None and distance_m <= 15:
        confidence = "medium"
    else:
        confidence = "low"

    result["building_confidence"] = confidence
    result.update({
        f"building_{column}": json_safe(value)
        for column, value in building.items()
    })
    return result


def chunk_lookup_sql(
    columns: List[str],
    lat_col: str,
    lon_col: str,
    mode: str,
    radius: float,
) -> str:
    original_cols_sql = exposure_select(columns)
    lat_sql = sql_identifier(lat_col)
    lon_sql = sql_identifier(lon_col)
    radius_sql = str(float(radius))

    exposure_cte = f"""
        WITH exposure AS (
            SELECT
                *,
                TRY_CAST({lon_sql} AS DOUBLE) AS __lon,
                TRY_CAST({lat_sql} AS DOUBLE) AS __lat,
                TRY_CAST({lon_sql} AS DOUBLE) BETWEEN -180 AND 180
                    AND TRY_CAST({lat_sql} AS DOUBLE) BETWEEN -90 AND 90 AS __valid_coordinates,
                ST_Point(TRY_CAST({lon_sql} AS DOUBLE), TRY_CAST({lat_sql} AS DOUBLE)) AS __pt,
                ST_Transform(
                    ST_Point(TRY_CAST({lon_sql} AS DOUBLE), TRY_CAST({lat_sql} AS DOUBLE)),
                    'EPSG:4326',
                    'EPSG:3035',
                    always_xy := true
                ) AS __pt_m,
                {radius_sql} / 111320.0 AS __lat_delta,
                {radius_sql} / (
                    111320.0 * GREATEST(COS(RADIANS(TRY_CAST({lat_sql} AS DOUBLE))), 0.2)
                ) AS __lon_delta
            FROM exposure_chunk_df
        )
    """

    if mode == "centroid":
        return f"""
            {exposure_cte}
            SELECT
                {original_cols_sql},
                e.__valid_coordinates AS coordinate_valid,
                CASE WHEN m.building_id IS NOT NULL THEN 'nearest_centroid' ELSE 'none' END AS building_match_type,
                m.distance_m AS building_distance_m,
                CASE
                    WHEN m.building_id IS NULL THEN 'none'
                    WHEN m.distance_m <= 15 THEN 'medium'
                    ELSE 'low'
                END AS building_confidence,
                {final_building_select("m")}
            FROM exposure e
            LEFT JOIN LATERAL (
                SELECT
                    ST_Distance_Sphere(ST_Point(b.centroid_lon, b.centroid_lat), e.__pt) AS distance_m,
                    {b_select("b")}
                FROM buildings b
                WHERE
                    e.__valid_coordinates
                    AND b.centroid_lon BETWEEN e.__lon - e.__lon_delta AND e.__lon + e.__lon_delta
                    AND b.centroid_lat BETWEEN e.__lat - e.__lat_delta AND e.__lat + e.__lat_delta
                    AND ST_Distance_Sphere(ST_Point(b.centroid_lon, b.centroid_lat), e.__pt) <= {radius_sql}
                ORDER BY distance_m
                LIMIT 1
            ) m ON TRUE
            ORDER BY e.__exposure_row_id
        """

    if mode == "inside":
        return f"""
            {exposure_cte}
            SELECT
                {original_cols_sql},
                e.__valid_coordinates AS coordinate_valid,
                CASE WHEN m.building_id IS NOT NULL THEN 'inside_polygon' ELSE 'none' END AS building_match_type,
                CASE WHEN m.building_id IS NOT NULL THEN 0.0 ELSE NULL END AS building_distance_m,
                CASE WHEN m.building_id IS NOT NULL THEN 'high' ELSE 'none' END AS building_confidence,
                {final_building_select("m")}
            FROM exposure e
            LEFT JOIN LATERAL (
                SELECT
                    {b_select("b")}
                FROM buildings b
                WHERE
                    e.__valid_coordinates
                    AND e.__lon BETWEEN b.bbox_xmin AND b.bbox_xmax
                    AND e.__lat BETWEEN b.bbox_ymin AND b.bbox_ymax
                    AND ST_Intersects(b.geom, e.__pt)
                ORDER BY b.footprint_area_m2 ASC NULLS LAST
                LIMIT 1
            ) m ON TRUE
            ORDER BY e.__exposure_row_id
        """

    return f"""
        {exposure_cte}
        SELECT
            {original_cols_sql},
            e.__valid_coordinates AS coordinate_valid,
            CASE
                WHEN i.building_id IS NOT NULL THEN 'inside_polygon'
                WHEN n.building_id IS NOT NULL THEN 'nearest_polygon'
                ELSE 'none'
            END AS building_match_type,
            CASE
                WHEN i.building_id IS NOT NULL THEN 0.0
                ELSE n.distance_m
            END AS building_distance_m,
            CASE
                WHEN i.building_id IS NOT NULL THEN 'high'
                WHEN n.building_id IS NULL THEN 'none'
                WHEN n.distance_m <= 15 THEN 'medium'
                ELSE 'low'
            END AS building_confidence,
            {final_coalesced_building_select()}
        FROM exposure e
        LEFT JOIN LATERAL (
            SELECT
                {b_select("b")}
            FROM buildings b
            WHERE
                e.__valid_coordinates
                AND e.__lon BETWEEN b.bbox_xmin AND b.bbox_xmax
                AND e.__lat BETWEEN b.bbox_ymin AND b.bbox_ymax
                AND ST_Intersects(b.geom, e.__pt)
            ORDER BY b.footprint_area_m2 ASC NULLS LAST
            LIMIT 1
        ) i ON TRUE
        LEFT JOIN LATERAL (
            SELECT
                ST_Distance(b.geom_3035, e.__pt_m) AS distance_m,
                {b_select("b")}
            FROM buildings b
            WHERE
                e.__valid_coordinates
                AND i.building_id IS NULL
                AND b.bbox_xmin <= e.__lon + e.__lon_delta
                AND b.bbox_xmax >= e.__lon - e.__lon_delta
                AND b.bbox_ymin <= e.__lat + e.__lat_delta
                AND b.bbox_ymax >= e.__lat - e.__lat_delta
                AND ST_DWithin(b.geom_3035, e.__pt_m, {radius_sql})
            ORDER BY distance_m
            LIMIT 1
        ) n ON TRUE
        ORDER BY e.__exposure_row_id
    """

def enrichment_select_sql(
    csv_sql: str,
    lat_sql: str,
    lon_sql: str,
    mode: str,
    radius_sql: str,
    original_cols_sql: str,
) -> str:
    
    # 1. Base logic shared by ALL modes
    base_exposure_cols = f"""
        *,
        TRY_CAST({lon_sql} AS DOUBLE) AS __lon,
        TRY_CAST({lat_sql} AS DOUBLE) AS __lat,
        TRY_CAST({lon_sql} AS DOUBLE) BETWEEN -180 AND 180
            AND TRY_CAST({lat_sql} AS DOUBLE) BETWEEN -90 AND 90 AS __valid_coordinates,
        ST_Point(TRY_CAST({lon_sql} AS DOUBLE), TRY_CAST({lat_sql} AS DOUBLE)) AS __pt
    """
    
    # 2. Add specific pre-computations depending on the mode
    if mode == "centroid":
        mode_specific_cols = f""",
        {radius_sql} / 111320.0 AS __lat_delta,
        {radius_sql} / (
            111320.0 * GREATEST(COS(RADIANS(TRY_CAST({lat_sql} AS DOUBLE))), 0.2)
        ) AS __lon_delta
        """
    elif mode == "inside_nearest":
        mode_specific_cols = f""",
        CASE 
            WHEN TRY_CAST({lon_sql} AS DOUBLE) BETWEEN -180 AND 180 AND TRY_CAST({lat_sql} AS DOUBLE) BETWEEN -90 AND 90 
            THEN ST_Transform(ST_Point(TRY_CAST({lon_sql} AS DOUBLE), TRY_CAST({lat_sql} AS DOUBLE)), 'EPSG:4326', 'EPSG:3035', always_xy := true)
            ELSE NULL 
        END AS __pt_m,
        {radius_sql} / 111320.0 AS __lat_delta,
        {radius_sql} / (
            111320.0 * GREATEST(COS(RADIANS(TRY_CAST({lat_sql} AS DOUBLE))), 0.2)
        ) AS __lon_delta
        """
    else:
        mode_specific_cols = ""

    exposure_ctes = f"""
        WITH exposure_raw AS (
            SELECT
                ROW_NUMBER() OVER () AS __exposure_row_id,
                *
            FROM read_csv_auto({csv_sql}, sample_size = 20480, ignore_errors = true)
        ),
        exposure AS (
            SELECT 
                {base_exposure_cols}
                {mode_specific_cols}
            FROM exposure_raw
        )
    """

    if mode == "centroid":
        return f"""
            {exposure_ctes},
            centroid_ranked AS (
                SELECT
                    e.__exposure_row_id,
                    ST_Distance_Sphere(ST_Point(b.centroid_lon, b.centroid_lat), e.__pt) AS distance_m,
                    ROW_NUMBER() OVER (
                        PARTITION BY e.__exposure_row_id
                        ORDER BY ST_Distance_Sphere(ST_Point(b.centroid_lon, b.centroid_lat), e.__pt)
                    ) AS rn,
                    {b_select("b")}
                FROM exposure e
                JOIN buildings b
                    ON e.__valid_coordinates
                    AND b.centroid_lon BETWEEN e.__lon - e.__lon_delta AND e.__lon + e.__lon_delta
                    AND b.centroid_lat BETWEEN e.__lat - e.__lat_delta AND e.__lat + e.__lat_delta
                WHERE ST_Distance_Sphere(ST_Point(b.centroid_lon, b.centroid_lat), e.__pt) <= {radius_sql}
            ),
            matches AS (
                SELECT * FROM centroid_ranked WHERE rn = 1
            )
            SELECT
                {original_cols_sql},
                e.__valid_coordinates AS coordinate_valid,
                CASE WHEN m.__exposure_row_id IS NOT NULL THEN 'nearest_centroid' ELSE 'none' END AS building_match_type,
                m.distance_m AS building_distance_m,
                CASE
                    WHEN m.__exposure_row_id IS NULL THEN 'none'
                    WHEN m.distance_m <= 15 THEN 'medium'
                    ELSE 'low'
                END AS building_confidence,
                {final_building_select("m")}
            FROM exposure e
            LEFT JOIN matches m USING (__exposure_row_id)
            ORDER BY e.__exposure_row_id
        """

    if mode == "inside":
        return f"""
            {exposure_ctes},
            inside_ranked AS (
                SELECT
                    e.__exposure_row_id,
                    ROW_NUMBER() OVER (
                        PARTITION BY e.__exposure_row_id
                        ORDER BY b.footprint_area_m2 ASC NULLS LAST
                    ) AS rn,
                    {b_select("b")}
                FROM exposure e
                JOIN buildings b
                    ON e.__valid_coordinates
                    AND e.__lon BETWEEN b.bbox_xmin AND b.bbox_xmax
                    AND e.__lat BETWEEN b.bbox_ymin AND b.bbox_ymax
                    AND ST_Intersects(b.geom, e.__pt)
            ),
            matches AS (
                SELECT * FROM inside_ranked WHERE rn = 1
            )
            SELECT
                {original_cols_sql},
                e.__valid_coordinates AS coordinate_valid,
                CASE WHEN m.__exposure_row_id IS NOT NULL THEN 'inside_polygon' ELSE 'none' END AS building_match_type,
                CASE WHEN m.__exposure_row_id IS NOT NULL THEN 0.0 ELSE NULL END AS building_distance_m,
                CASE WHEN m.__exposure_row_id IS NOT NULL THEN 'high' ELSE 'none' END AS building_confidence,
                {final_building_select("m")}
            FROM exposure e
            LEFT JOIN matches m USING (__exposure_row_id)
            ORDER BY e.__exposure_row_id
        """

    # inside_nearest mode
    return f"""
        {exposure_ctes},
        inside_ranked AS (
            SELECT
                e.__exposure_row_id,
                ROW_NUMBER() OVER (
                    PARTITION BY e.__exposure_row_id
                    ORDER BY b.footprint_area_m2 ASC NULLS LAST
                ) AS rn,
                {b_select("b")}
            FROM exposure e
            JOIN buildings b
                ON e.__valid_coordinates
                AND e.__lon BETWEEN b.bbox_xmin AND b.bbox_xmax
                AND e.__lat BETWEEN b.bbox_ymin AND b.bbox_ymax
                AND ST_Intersects(b.geom, e.__pt)
        ),
        inside_matches AS (
            SELECT * FROM inside_ranked WHERE rn = 1
        ),
        nearest_candidates AS (
            SELECT
                e.__exposure_row_id,
                ST_Distance(b.geom_3035, e.__pt_m) AS distance_m,
                {b_select("b")}
            FROM exposure e
            LEFT JOIN inside_matches i USING (__exposure_row_id)
            JOIN buildings b
                ON e.__valid_coordinates
                AND i.__exposure_row_id IS NULL
                AND b.bbox_xmin <= e.__lon + e.__lon_delta
                AND b.bbox_xmax >= e.__lon - e.__lon_delta
                AND b.bbox_ymin <= e.__lat + e.__lat_delta
                AND b.bbox_ymax >= e.__lat - e.__lat_delta
                AND b.bbox_3035_xmin <= ST_X(e.__pt_m) + {radius_sql}
                AND b.bbox_3035_xmax >= ST_X(e.__pt_m) - {radius_sql}
                AND b.bbox_3035_ymin <= ST_Y(e.__pt_m) + {radius_sql}
                AND b.bbox_3035_ymax >= ST_Y(e.__pt_m) - {radius_sql}
                AND ST_DWithin(b.geom_3035, e.__pt_m, {radius_sql})
        ),
        nearest_ranked AS (
            SELECT
                *,
                ROW_NUMBER() OVER (
                    PARTITION BY __exposure_row_id
                    ORDER BY distance_m
                ) AS rn
            FROM nearest_candidates
            WHERE distance_m <= {radius_sql}
        ),
        nearest_matches AS (
            SELECT * FROM nearest_ranked WHERE rn = 1
        )
        SELECT
            {original_cols_sql},
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
            END AS building_confidence,
            {final_coalesced_building_select()}
        FROM exposure e
        LEFT JOIN inside_matches i USING (__exposure_row_id)
        LEFT JOIN nearest_matches n USING (__exposure_row_id)
        ORDER BY e.__exposure_row_id
    """

def find_building(
    con: duckdb.DuckDBPyConnection,
    lon: float,
    lat: float,
    nearest_radius_m: float,
) -> Optional[Dict[str, Any]]:
    inside = con.execute("""
        WITH click AS (
            SELECT ST_Point(?, ?) AS pt
        )
        SELECT
            'inside_polygon' AS match_type,
            0.0 AS distance_m,
            'high' AS confidence,
            building_id,
            source,
            relation_id,
            quadkey,
            last_update,
            centroid_lon,
            centroid_lat,
            footprint_area_m2,
            height_raw,
            height_source_type,
            height_m,
            height_quality,
            stories_exact,
            stories_min,
            stories_max,
            occupancy_raw,
            occupancy_code,
            occupancy_group,
            occupancy_quality,
            floorspace_obm_m2,
            floorspace_est_m2,
            attribute_completeness_score,
            ST_AsGeoJSON(geom) AS geometry
        FROM buildings, click
        WHERE
            bbox_xmin <= ?
            AND bbox_xmax >= ?
            AND bbox_ymin <= ?
            AND bbox_ymax >= ?
            AND ST_Intersects(geom, pt)
        ORDER BY footprint_area_m2 ASC NULLS LAST
        LIMIT 1;
    """, [lon, lat, lon, lon, lat, lat]).fetchone()

    if inside:
        return row_to_response(con, inside)

    lat_delta = nearest_radius_m / 111_320.0
    lon_delta = nearest_radius_m / (111_320.0 * max(math.cos(math.radians(lat)), 0.2))

    nearest = con.execute("""
        WITH click AS (
            SELECT ST_Point(?, ?) AS pt
        )
        SELECT
            'nearest' AS match_type,
            ST_Distance_Sphere(ST_Point(centroid_lon, centroid_lat), pt) AS distance_m,
            CASE
                WHEN ST_Distance_Sphere(ST_Point(centroid_lon, centroid_lat), pt) <= 15 THEN 'medium'
                ELSE 'low'
            END AS confidence,
            building_id,
            source,
            relation_id,
            quadkey,
            last_update,
            centroid_lon,
            centroid_lat,
            footprint_area_m2,
            height_raw,
            height_source_type,
            height_m,
            height_quality,
            stories_exact,
            stories_min,
            stories_max,
            occupancy_raw,
            occupancy_code,
            occupancy_group,
            occupancy_quality,
            floorspace_obm_m2,
            floorspace_est_m2,
            attribute_completeness_score,
            ST_AsGeoJSON(geom) AS geometry
        FROM buildings, click
        WHERE
            centroid_lon BETWEEN ? AND ?
            AND centroid_lat BETWEEN ? AND ?
        ORDER BY distance_m ASC
        LIMIT 1;
    """, [
        lon,
        lat,
        lon - lon_delta,
        lon + lon_delta,
        lat - lat_delta,
        lat + lat_delta,
    ]).fetchone()

    if nearest is None or nearest[1] is None or nearest[1] > nearest_radius_m:
        return None

    return row_to_response(con, nearest)


def row_to_response(con: duckdb.DuckDBPyConnection, row: tuple) -> Dict[str, Any]:
    columns = [
        "match_type",
        "distance_m",
        "confidence",
        "building_id",
        "source",
        "relation_id",
        "quadkey",
        "last_update",
        "centroid_lon",
        "centroid_lat",
        "footprint_area_m2",
        "height_raw",
        "height_source_type",
        "height_m",
        "height_quality",
        "stories_exact",
        "stories_min",
        "stories_max",
        "occupancy_raw",
        "occupancy_code",
        "occupancy_group",
        "occupancy_quality",
        "floorspace_obm_m2",
        "floorspace_est_m2",
        "attribute_completeness_score",
        "geometry",
    ]
    data = dict(zip(columns, row))
    geometry = json.loads(data.pop("geometry"))

    building = {key: json_safe(value) for key, value in data.items() if key not in {
        "match_type",
        "distance_m",
        "confidence",
    }}
    building["geometry"] = geometry

    return {
        "match_type": data["match_type"],
        "distance_m": json_safe(data["distance_m"]),
        "confidence": data["confidence"],
        "building": building,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Building lookup app over Germany OBM Parquet.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    prepare = subparsers.add_parser("prepare-index")
    prepare.add_argument("--parquet", default=DEFAULT_PARQUET)
    prepare.add_argument("--db", default=DEFAULT_DB)
    prepare.add_argument("--threads", type=int, default=8)
    prepare.add_argument("--force", action="store_true")

    serve = subparsers.add_parser("serve")
    serve.add_argument("--db", default=DEFAULT_DB)
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=5000)
    serve.add_argument("--nearest-radius-m", type=float, default=50.0)
    serve.add_argument("--debug", action="store_true")

    return parser.parse_args()


def main() -> None:
    args = parse_args()

    if args.command == "prepare-index":
        prepare_index(args.parquet, args.db, args.force, args.threads)
        return

    app = create_app(args.db, args.nearest_radius_m)

    if not Path(args.db).exists():
        raise SystemExit(
            f"Lookup database not found: {args.db}\n"
            "Create it first with:\n"
            "  python building_lookup_app.py prepare-index "
            "--parquet etl_output/buildings_de_cleaned.parquet "
            "--db etl_output/building_lookup.duckdb --force"
        )

    app.run(host=args.host, port=args.port, debug=True)


if __name__ == "__main__":
    main()
