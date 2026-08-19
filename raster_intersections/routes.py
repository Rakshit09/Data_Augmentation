import time
from pathlib import Path
from threading import Thread
from typing import Any, Callable, Dict, Optional

import duckdb
from flask import Flask, jsonify, request, send_file

from layer_upload_routes import get_uploaded_layer

from .duckdb_queries import (
    append_exposure_source_columns,
    intersect_vector_candidates,
    query_database_candidates,
    query_exposure_candidates,
)
from .raster_metadata import read_raster_metadata
from .results import (
    PREVIEW_LIMIT,
    build_summary,
    build_vector_summary,
    complete_result_job,
    create_pending_job,
    create_result_job,
    fail_job,
    get_job,
    update_job,
)
from .sampling import sample_candidates, sampling_diagnostics
from .utils import finite_float, intersect_bounds, normalized_bounds, parse_threshold, safe_json_value, sql_string


MAX_CANDIDATES = 250000


def register_raster_intersection_routes(
    app: Flask,
    *,
    find_upload: Callable[[Path, str], Optional[Path]],
    prepare_exposure_map_cache: Callable[[Path, str, str, str], Any],
    open_db: Callable[[str, bool], duckdb.DuckDBPyConnection],
    convert_excel_to_csv: Optional[Callable[[Path, Path], None]] = None,  # deprecated, no longer used
) -> None:
    @app.route("/api/raster-intersections/exposure", methods=["POST"])
    def raster_intersect_exposure():
        try:
            payload = request.get_json(silent=True) or {}
            layer, raster_path, band_index, bounds, threshold, threshold_operator, sample_radius_m, radius_aggregation = _analysis_context(payload)

            upload_id = str(payload.get("upload_id") or "").strip()
            lat_col = str(payload.get("lat_col") or "").strip()
            lon_col = str(payload.get("lon_col") or "").strip()
            si_field = str(payload.get("si_field") or "").strip()
            if not upload_id or not lat_col or not lon_col:
                raise ValueError("Upload an exposure CSV and choose latitude/longitude columns first.")

            upload_dir = Path(str(app.config.get("UPLOAD_DIR") or ""))
            upload_path = find_upload(upload_dir, upload_id)
            if upload_path is None:
                raise ValueError("The uploaded exposure file was not found. Upload it again.")
            layer_name = str(layer.get("name") or "Raster")
            job = create_pending_job(
                "exposure",
                layer_name,
                phase="Queued",
                percent=1,
                detail="Waiting for raster sampling worker.",
            )
            job_id = str(job["job_id"])

            def run_job() -> None:
                started_at = time.perf_counter()
                sampling_phase = "Sampling depth values" if sample_radius_m is not None else "Sampling raster values"
                try:
                    update_job(
                        job_id,
                        status="running",
                        phase="Preparing raster analysis",
                        percent=5,
                        detail="Loading raster metadata and exposure inputs.",
                    )
                    cache_path, _metadata = prepare_exposure_map_cache(upload_path, upload_id, lat_col, lon_col)
                    raster_metadata = read_raster_metadata(raster_path, layer, band_index)

                    update_job(
                        job_id,
                        status="running",
                        phase="Loading candidate locations",
                        percent=18,
                        detail="Selecting exposure points inside the analysis area.",
                    )
                    candidates, candidate_count = query_exposure_candidates(cache_path, bounds, MAX_CANDIDATES)
                    if not candidates:
                        raise ValueError("No exposure points are inside the selected raster/map area.")

                    update_job(
                        job_id,
                        status="running",
                        phase=sampling_phase,
                        percent=35,
                        detail=f"Sampling {len(candidates):,} candidate locations.",
                    )
                    sampled = sample_candidates(
                        raster_path,
                        candidates,
                        raster_metadata,
                        threshold,
                        threshold_operator,
                        sample_radius_m=sample_radius_m,
                        radius_aggregation=radius_aggregation,
                        progress_callback=_scaled_sampling_progress(job_id, sampling_phase, 35, 90),
                    )
                    if not sampled:
                        raise ValueError(_empty_sample_message(candidates, raster_path, raster_metadata, threshold, threshold_operator, sample_radius_m, radius_aggregation))

                    update_job(
                        job_id,
                        status="running",
                        phase="Preparing results",
                        percent=93,
                        detail="Building preview, summary, and downloads.",
                    )
                    columns, rows, source_warning = append_exposure_source_columns(upload_path, sampled)
                    summary = build_summary(
                        rows=rows,
                        source_type="exposure",
                        candidate_count=candidate_count,
                        threshold=threshold,
                        threshold_operator=threshold_operator,
                        si_field=si_field or None,
                        elapsed_seconds=time.perf_counter() - started_at,
                        raster_band=band_index,
                        sample_radius_m=sample_radius_m,
                        sample_radius_aggregation=radius_aggregation,
                        bounds=bounds,
                    )
                    if source_warning:
                        summary["warning"] = source_warning

                    complete_result_job(
                        job_id,
                        "exposure",
                        layer_name,
                        columns,
                        rows,
                        summary,
                        detail="Raster intersection complete.",
                    )
                except ValueError as exc:
                    fail_job(job_id, str(exc))
                except Exception as exc:
                    fail_job(job_id, f"Could not intersect exposure with raster: {exc}")

            Thread(target=run_job, daemon=True).start()
            return jsonify({"ok": True, "job_id": job_id, "status": "queued"}), 202
        except ValueError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
        except Exception as exc:
            return jsonify({"ok": False, "error": f"Could not intersect exposure with raster: {exc}"}), 500

    @app.route("/api/raster-intersections/database", methods=["POST"])
    def raster_intersect_database():
        try:
            payload = request.get_json(silent=True) or {}
            layer, raster_path, band_index, bounds, threshold, threshold_operator, sample_radius_m, radius_aggregation = _analysis_context(payload)
            db_path = str(app.config.get("DB_PATH") or "")
            if not db_path:
                raise ValueError("No building database is selected.")
            if not Path(db_path).is_file():
                raise ValueError("The selected building database was not found.")
            layer_name = str(layer.get("name") or "Raster")
            job = create_pending_job(
                "database",
                layer_name,
                phase="Queued",
                percent=1,
                detail="Waiting for raster sampling worker.",
            )
            job_id = str(job["job_id"])

            def run_job() -> None:
                started_at = time.perf_counter()
                sampling_phase = "Sampling depth values" if sample_radius_m is not None else "Sampling raster values"
                try:
                    update_job(
                        job_id,
                        status="running",
                        phase="Preparing raster analysis",
                        percent=5,
                        detail="Loading raster metadata and building candidates.",
                    )
                    raster_metadata = read_raster_metadata(raster_path, layer, band_index)
                    update_job(
                        job_id,
                        status="running",
                        phase="Loading candidate locations",
                        percent=18,
                        detail="Selecting building centroids inside the analysis area.",
                    )
                    con = open_db(db_path, True)
                    try:
                        candidates, candidate_count, _selected_columns = query_database_candidates(con, bounds, MAX_CANDIDATES)
                    finally:
                        con.close()

                    if not candidates:
                        raise ValueError("No building centroids are inside the selected raster/map area.")

                    update_job(
                        job_id,
                        status="running",
                        phase=sampling_phase,
                        percent=35,
                        detail=f"Sampling {len(candidates):,} candidate locations.",
                    )
                    rows = sample_candidates(
                        raster_path,
                        candidates,
                        raster_metadata,
                        threshold,
                        threshold_operator,
                        sample_radius_m=sample_radius_m,
                        radius_aggregation=radius_aggregation,
                        progress_callback=_scaled_sampling_progress(job_id, sampling_phase, 35, 90),
                    )
                    if not rows:
                        raise ValueError(_empty_sample_message(candidates, raster_path, raster_metadata, threshold, threshold_operator, sample_radius_m, radius_aggregation))

                    update_job(
                        job_id,
                        status="running",
                        phase="Preparing results",
                        percent=93,
                        detail="Building preview, summary, and downloads.",
                    )
                    columns = _columns_from_rows(rows)
                    summary = build_summary(
                        rows=rows,
                        source_type="database",
                        candidate_count=candidate_count,
                        threshold=threshold,
                        threshold_operator=threshold_operator,
                        elapsed_seconds=time.perf_counter() - started_at,
                        raster_band=band_index,
                        sample_radius_m=sample_radius_m,
                        sample_radius_aggregation=radius_aggregation,
                        bounds=bounds,
                    )
                    complete_result_job(
                        job_id,
                        "database",
                        layer_name,
                        columns,
                        rows,
                        summary,
                        detail="Raster intersection complete.",
                    )
                except ValueError as exc:
                    fail_job(job_id, str(exc))
                except Exception as exc:
                    fail_job(job_id, f"Could not intersect database with raster: {exc}")

            Thread(target=run_job, daemon=True).start()
            return jsonify({"ok": True, "job_id": job_id, "status": "queued"}), 202
        except ValueError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
        except Exception as exc:
            return jsonify({"ok": False, "error": f"Could not intersect database with raster: {exc}"}), 500

    @app.route("/api/vector-intersections/exposure", methods=["POST"])
    def vector_intersect_exposure():
        started_at = time.perf_counter()
        try:
            payload = request.get_json(silent=True) or {}
            layer, bounds, field = _vector_analysis_context(payload)
            upload_id = str(payload.get("upload_id") or "").strip()
            lat_col = str(payload.get("lat_col") or "").strip()
            lon_col = str(payload.get("lon_col") or "").strip()
            si_field = str(payload.get("si_field") or "").strip()
            if not upload_id or not lat_col or not lon_col:
                raise ValueError("Upload an exposure CSV and choose latitude/longitude columns first.")

            upload_dir = Path(str(app.config.get("UPLOAD_DIR") or ""))
            upload_path = find_upload(upload_dir, upload_id)
            if upload_path is None:
                raise ValueError("The uploaded exposure file was not found. Upload it again.")

            cache_path, _metadata = prepare_exposure_map_cache(upload_path, upload_id, lat_col, lon_col)
            candidates, candidate_count = query_exposure_candidates(cache_path, bounds, MAX_CANDIDATES)
            if not candidates:
                raise ValueError("No exposure points are inside the selected vector/map area.")

            matched = intersect_vector_candidates(layer, candidates, field, MAX_CANDIDATES)
            if not matched:
                raise ValueError("Exposure points were found, but none intersected the selected vector layer.")

            columns, rows, source_warning = append_exposure_source_columns(upload_path, matched)
            summary = build_vector_summary(
                rows=rows,
                source_type="vector_exposure",
                candidate_count=candidate_count,
                field=field,
                si_field=si_field or None,
                elapsed_seconds=time.perf_counter() - started_at,
                bounds=bounds,
            )
            if source_warning:
                summary["warning"] = source_warning
            job = create_result_job("vector_exposure", str(layer.get("name") or "Vector"), columns, rows, summary)
            return jsonify(_job_payload(job))
        except ValueError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
        except Exception as exc:
            return jsonify({"ok": False, "error": f"Could not intersect exposure with vector layer: {exc}"}), 500

    @app.route("/api/vector-intersections/database", methods=["POST"])
    def vector_intersect_database():
        started_at = time.perf_counter()
        try:
            payload = request.get_json(silent=True) or {}
            layer, bounds, field = _vector_analysis_context(payload)
            db_path = str(app.config.get("DB_PATH") or "")
            if not db_path:
                raise ValueError("No building database is selected.")
            if not Path(db_path).is_file():
                raise ValueError("The selected building database was not found.")

            con = open_db(db_path, True)
            try:
                candidates, candidate_count, _selected_columns = query_database_candidates(con, bounds, MAX_CANDIDATES)
            finally:
                con.close()
            if not candidates:
                raise ValueError("No building centroids are inside the selected vector/map area.")

            rows = intersect_vector_candidates(layer, candidates, field, MAX_CANDIDATES)
            if not rows:
                raise ValueError("Building centroids were found, but none intersected the selected vector layer.")

            columns = _columns_from_rows(rows)
            summary = build_vector_summary(
                rows=rows,
                source_type="vector_database",
                candidate_count=candidate_count,
                field=field,
                elapsed_seconds=time.perf_counter() - started_at,
                bounds=bounds,
            )
            job = create_result_job("vector_database", str(layer.get("name") or "Vector"), columns, rows, summary)
            return jsonify(_job_payload(job))
        except ValueError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
        except Exception as exc:
            return jsonify({"ok": False, "error": f"Could not intersect database with vector layer: {exc}"}), 500

    @app.route("/api/raster-intersections/<job_id>/preview")
    def raster_intersection_preview(job_id: str):
        job = get_job(job_id)
        if job is None:
            return jsonify({"ok": False, "error": "Intersection job was not found."}), 404
        try:
            limit = max(1, min(2000, int(request.args.get("limit", PREVIEW_LIMIT))))
            offset = max(0, int(request.args.get("offset", 0)))
        except ValueError:
            return jsonify({"ok": False, "error": "Preview limit and offset must be numbers."}), 400
        columns, rows = _preview_rows(job, limit, offset)
        return jsonify({"ok": True, "job_id": job_id, "columns": columns, "rows": rows})

    @app.route("/api/raster-intersections/<job_id>/summary")
    def raster_intersection_summary(job_id: str):
        job = get_job(job_id)
        if job is None:
            return jsonify({"ok": False, "error": "Intersection job was not found."}), 404
        return jsonify({"ok": True, "job_id": job_id, "summary": job.get("summary", {})})

    @app.route("/api/raster-intersections/progress/<job_id>")
    def raster_intersection_progress(job_id: str):
        job = get_job(job_id)
        if job is None:
            return jsonify({"ok": False, "error": "Intersection job was not found."}), 404
        return jsonify(_job_payload(job))

    @app.route("/api/raster-intersections/<job_id>/download.<extension>")
    def raster_intersection_download(job_id: str, extension: str):
        job = get_job(job_id)
        if job is None:
            return jsonify({"ok": False, "error": "Intersection job was not found."}), 404
        paths = job.get("paths") or {}
        path = paths.get(extension)
        if not path or not Path(path).is_file():
            return jsonify({"ok": False, "error": f"{extension.upper()} download is not available."}), 404
        mimetype = {
            "csv": "text/csv",
            "parquet": "application/octet-stream",
            "geojson": "application/geo+json",
        }.get(extension, "application/octet-stream")
        return send_file(path, as_attachment=True, download_name=f"raster_intersection_{job_id}.{extension}", mimetype=mimetype)


def _analysis_context(payload: Dict[str, Any]):
    layer_id = str(payload.get("layer_id") or "").strip()
    if not layer_id:
        raise ValueError("No raster layer selected.")
    layer = get_uploaded_layer(layer_id)
    if layer is None or layer.get("kind") != "raster":
        raise ValueError("Select an uploaded raster layer first.")

    raster_path = Path(str(layer.get("raster_path") or ""))
    if not raster_path.is_file():
        raise ValueError("The uploaded raster file is no longer available. Upload it again.")

    try:
        band_index = int(payload.get("band_index") or layer.get("default_band") or 1)
    except (TypeError, ValueError) as exc:
        raise ValueError("Raster band must be a number.") from exc

    threshold, threshold_operator = parse_threshold(payload)
    sample_radius_m = _parse_sample_radius(payload)
    radius_aggregation = _parse_radius_aggregation(payload)
    bounds = _analysis_bounds(payload, layer)
    return layer, raster_path, band_index, bounds, threshold, threshold_operator, sample_radius_m, radius_aggregation


def _parse_sample_radius(payload: Dict[str, Any]) -> Optional[float]:
    raw_radius = payload.get("sample_radius_m")
    if raw_radius in (None, ""):
        return None
    radius_m = finite_float(raw_radius, "sample radius")
    if radius_m <= 0:
        raise ValueError("Sample radius must be greater than 0 metres.")
    return radius_m


def _parse_radius_aggregation(payload: Dict[str, Any]) -> str:
    value = str(payload.get("sample_radius_aggregation") or "mean").strip().casefold()
    if value not in {"mean", "max", "min"}:
        raise ValueError("Radius aggregation must be mean, max, or min.")
    return value


def _vector_analysis_context(payload: Dict[str, Any]):
    layer_id = str(payload.get("layer_id") or "").strip()
    if not layer_id:
        raise ValueError("No vector layer selected.")
    layer = get_uploaded_layer(layer_id)
    if layer is None or layer.get("kind") != "vector":
        raise ValueError("Select an uploaded GeoPackage, shapefile, or GeoJSON layer first.")

    field = str(payload.get("field") or "").strip()
    bounds = _analysis_bounds(payload, layer)
    return layer, bounds, field


def _analysis_bounds(payload: Dict[str, Any], layer: Dict[str, Any]) -> Dict[str, float]:
    extent = layer.get("extent")
    if not extent:
        raise ValueError("Raster extent is missing.")
    raster_bounds = normalized_bounds(extent)
    area_mode = str(payload.get("area_mode") or "visible").strip()
    if area_mode == "raster":
        return raster_bounds

    raw_bounds = payload.get("bounds") or {}
    view_bounds = normalized_bounds(raw_bounds)
    bounds = intersect_bounds(view_bounds, raster_bounds)
    if bounds is None:
        raise ValueError("The visible map area does not overlap the raster.")
    return bounds


def _columns_from_rows(rows: Any) -> list[str]:
    columns: list[str] = []
    for row in rows[:64]:
        for key in row.keys():
            if key not in columns:
                columns.append(key)
    return columns


def _preview_rows(job: Dict[str, Any], limit: int, offset: int) -> tuple[list[str], list[Dict[str, Any]]]:
    csv_path = (job.get("paths") or {}).get("csv")
    if not csv_path or not Path(csv_path).is_file():
        return job.get("columns", []), job.get("preview_rows", [])[offset:offset + limit]

    con = duckdb.connect()
    try:
        result = con.execute(f"""
            SELECT *
            FROM read_csv_auto({sql_string(str(Path(csv_path).resolve()))}, header = true, ignore_errors = true)
            LIMIT ?
            OFFSET ?;
        """, [limit, offset])
        columns = [str(description[0]) for description in (result.description or [])]
        rows = [
            {
                column: safe_json_value(raw_row[index])
                for index, column in enumerate(columns)
            }
            for raw_row in result.fetchall()
        ]
        return columns, rows
    finally:
        con.close()


def _empty_sample_message(
    candidates: Any,
    raster_path: Path,
    raster_metadata: Dict[str, Any],
    threshold: Any,
    threshold_operator: str,
    sample_radius_m: Optional[float],
    radius_aggregation: str,
) -> str:
    diagnostics = sampling_diagnostics(
        raster_path,
        candidates,
        raster_metadata,
        sample_radius_m=sample_radius_m,
        radius_aggregation=radius_aggregation,
    )
    sampled = int(diagnostics.get("sampled_count") or 0)
    nodata = diagnostics.get("nodata")
    nodata_count = int(diagnostics.get("nodata_count") or 0)
    non_nodata_count = int(diagnostics.get("non_nodata_count") or 0)
    value_counts = diagnostics.get("value_counts") or {}
    value_copy = ", ".join(f"{key}: {value}" for key, value in value_counts.items()) or "no readable values"
    sample_scope = (
        f"within {sample_radius_m:g} m of each candidate location using {radius_aggregation} aggregation"
        if sample_radius_m is not None
        else "at those exact locations"
    )

    if sampled and nodata_count == sampled:
        return (
            f"All {sampled:,} sampled candidate locations hit raster nodata/background ({nodata}). "
            f"Sampled values: {value_copy}. Move/zoom to pixels with valid raster values, or use a raster where background is not marked as nodata."
        )
    if sampled and non_nodata_count == 0:
        return (
            f"Candidate locations were inside the raster bounding box, but no valid pixels were found {sample_scope}. "
            f"Sampled values: {value_copy}."
        )
    if threshold is not None:
        return (
            f"Candidate locations had raster values, but none matched the threshold {threshold_operator} {threshold}. "
            f"Sampled values: {value_copy}."
        )
    return (
        "Candidates were found, but none had a valid raster value after nodata filtering. "
        f"Sampled values: {value_copy}."
    )


def _job_payload(job: Dict[str, Any]) -> Dict[str, Any]:
    payload = {
        "ok": True,
        "job_id": job["job_id"],
        "status": job.get("status", "complete"),
        "phase": job.get("phase", "Complete"),
        "percent": job.get("percent", 100),
        "detail": job.get("detail"),
        "error": job.get("error"),
        "source_type": job["source_type"],
    }
    if str(job.get("status") or "").lower() == "complete":
        payload.update({
            "summary": job["summary"],
            "columns": job["columns"],
            "rows": job["preview_rows"],
            "download_urls": job["download_urls"],
            "map_features": job["map_features"],
        })
    return payload


def _scaled_sampling_progress(job_id: str, phase: str, start_percent: int, end_percent: int):
    span = max(0, end_percent - start_percent)

    def report(_sampling_phase: str, percent: int, detail: Optional[str] = None) -> None:
        bounded = max(0, min(100, int(percent)))
        overall = start_percent + int(round(span * bounded / 100.0))
        update_job(
            job_id,
            status="running",
            phase=phase,
            percent=min(end_percent, overall),
            detail=detail,
        )

    return report
