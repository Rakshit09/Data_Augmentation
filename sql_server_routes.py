"""Flask routes: load an exposure table from SQL Server and write enrichment/raster results back to it."""
import re
import uuid
from pathlib import Path
from threading import Event, Lock, Thread
from typing import Any, Callable, Dict

import pandas as pd
from flask import Flask, jsonify, request
from werkzeug.utils import secure_filename

import sql_server_source as sql
from raster_intersections.results import get_job as get_intersection_job

HEX_ID = re.compile(r"[0-9a-f]{32}")
MAX_REMEMBERED_SOURCES = 20
INTERSECTION_HELPER_COLUMNS = {
    "exposure_row_id",
    "raster_sample_lon",
    "raster_sample_lat",
    "vector_feature_id",
    "vector_field",
}

# Background table copies keyed by upload id; find_upload() waits on these before reading the CSV.
_exports: Dict[str, Dict[str, Any]] = {}
_exports_lock = Lock()


def wait_for_sql_export(upload_id: str) -> None:
    with _exports_lock:
        export = _exports.get(upload_id)
    if export is not None:
        export["done"].wait()


def _export_status(export: Dict[str, Any]) -> Dict[str, Any]:
    return {key: export[key] for key in ("status", "rows", "total", "error")}


def _text(payload: Dict[str, Any], key: str, label: str) -> str:
    value = str(payload.get(key) or "").strip()
    if not value:
        raise ValueError(f"{label} is required.")
    return value


def _respond(action: Callable[[], Any]):
    try:
        return jsonify(action())
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:
        return jsonify({"error": sql.friendly_error(exc)}), 502


def register_sql_server_routes(
    app: Flask,
    *,
    on_upload_ready: Callable[[str], None],
) -> None:
    sources: Dict[str, Dict[str, Any]] = {}
    sources_lock = Lock()

    def source_for(upload_id: str) -> Dict[str, Any]:
        with sources_lock:
            source = sources.get(upload_id)
        if source is None:
            raise ValueError("The current exposure was not loaded from SQL Server.")
        return source

    def result_csv(kind: str, job_id: str) -> Path:
        if not HEX_ID.fullmatch(job_id):
            raise ValueError("A valid result job id is required.")
        if kind == "enrichment":
            path = Path(app.config["RESULT_DIR"]) / f"enriched_{job_id}.csv"
        elif kind == "intersection":
            job = get_intersection_job(job_id) or {}
            if job.get("source_type") not in {"exposure", "vector_exposure"}:
                raise ValueError("Only exposure intersection results can be written back.")
            path = Path(str((job.get("paths") or {}).get("csv") or ""))
        else:
            raise ValueError(f"Unknown result type: {kind}")
        if not path.is_file():
            raise ValueError("The result file is no longer available. Run the analysis again.")
        return path

    @app.route("/api/sql/servers")
    def sql_servers():
        return jsonify({"servers": list(sql.SQL_SERVERS), "driver": sql.odbc_driver()})

    @app.route("/api/sql/databases")
    def sql_databases():
        return _respond(lambda: {
            "databases": sql.list_databases(_text(request.args, "server", "Server")),
        })

    @app.route("/api/sql/tables")
    def sql_tables():
        return _respond(lambda: {
            "tables": sql.list_tables(
                _text(request.args, "server", "Server"),
                _text(request.args, "database", "Database"),
            ),
        })

    def copy_table(target: Dict[str, str], upload_path: Path, export: Dict[str, Any]) -> None:
        # Written under a .part name so find_upload() never sees a half-written CSV.
        part_path = upload_path.with_name(upload_path.name + ".part")
        try:
            info = sql.export_table(
                target["server"], target["database"], target["schema"], target["table"], part_path,
                progress=lambda rows: export.update(rows=rows),
                cancelled=export["cancel"].is_set,
            )
            part_path.replace(upload_path)
            export.update(status="complete", rows=info["row_count"], total=info["row_count"])
        except Exception as exc:
            part_path.unlink(missing_ok=True)
            message = str(exc) if isinstance(exc, ValueError) else sql.friendly_error(exc)
            export.update(status="cancelled" if export["cancel"].is_set() else "error", error=message)
        finally:
            export["done"].set()

    @app.route("/api/sql/load", methods=["POST"])
    def sql_load():
        def load() -> Dict[str, Any]:
            payload = request.get_json(silent=True) or {}
            target = {
                "server": _text(payload, "server", "Server"),
                "database": _text(payload, "database", "Database"),
                "schema": _text(payload, "schema", "Schema"),
                "table": _text(payload, "table", "Table"),
            }
            preview = sql.preview_table(target["server"], target["database"], target["schema"], target["table"])
            if not preview["rows"]:
                raise ValueError(f"Table {target['schema']}.{target['table']} is empty.")

            upload_id = uuid.uuid4().hex
            upload_path = Path(app.config["UPLOAD_DIR"]) / f"{upload_id}_{secure_filename(target['table']) or 'sql_table'}.csv"
            export = {"status": "running", "rows": 0, "total": preview["row_count"], "error": None, "done": Event(), "cancel": Event()}
            with _exports_lock:
                for previous in _exports.values():
                    previous["cancel"].set()
                _exports[upload_id] = export

            source = {
                **target,
                "export_columns": preview["columns"],
                "columns": list(preview["columns"]),
                "key_columns": preview["key_columns"],
                "row_count": preview["row_count"],
            }
            with sources_lock:
                sources[upload_id] = source
                while len(sources) > MAX_REMEMBERED_SOURCES:
                    stale_id = next(iter(sources))
                    sources.pop(stale_id)
                    with _exports_lock:
                        _exports.pop(stale_id, None)
            Thread(target=copy_table, args=(target, upload_path, export), daemon=True).start()
            on_upload_ready(upload_id)

            return {
                "upload_id": upload_id,
                "filename": upload_path.name.partition("_")[2],
                "columns": preview["columns"],
                "rows": preview["rows"],
                "sheets": [],
                "sheet": None,
                "sql_source": {
                    **{key: source[key] for key in ("server", "database", "schema", "table", "row_count", "key_columns")},
                    "export": _export_status(export),
                },
            }

        return _respond(load)

    def export_for(upload_id: str) -> Dict[str, Any]:
        with _exports_lock:
            export = _exports.get(upload_id)
        if export is None:
            raise ValueError("No table copy was found for this upload.")
        return export

    @app.route("/api/sql/load/<upload_id>/progress")
    def sql_load_progress(upload_id: str):
        return _respond(lambda: _export_status(export_for(upload_id)))

    @app.route("/api/sql/load/<upload_id>/cancel", methods=["POST"])
    def sql_load_cancel(upload_id: str):
        def cancel() -> Dict[str, Any]:
            export = export_for(upload_id)
            export["cancel"].set()
            return _export_status(export)

        return _respond(cancel)

    @app.route("/api/sql/write-back/plan", methods=["POST"])
    def sql_write_back_plan():
        def plan() -> Dict[str, Any]:
            payload = request.get_json(silent=True) or {}
            kind = _text(payload, "kind", "Result type")
            source = source_for(_text(payload, "upload_id", "Upload id"))
            header = [str(name) for name in pd.read_csv(result_csv(kind, _text(payload, "job_id", "Job id")), nrows=0, encoding="utf-8-sig").columns]
            exported = {name.casefold() for name in source["export_columns"]}
            return {
                "target": {key: source[key] for key in ("server", "database", "schema", "table")},
                "table_columns": source["columns"],
                "key_columns": source["key_columns"],
                "columns": [
                    {
                        "name": name,
                        "selected": kind != "intersection" or name not in INTERSECTION_HELPER_COLUMNS,
                    }
                    for name in header
                    if name.casefold() not in exported
                ],
            }

        return _respond(plan)

    @app.route("/api/sql/write-back", methods=["POST"])
    def sql_write_back():
        def write() -> Dict[str, Any]:
            payload = request.get_json(silent=True) or {}
            upload_id = _text(payload, "upload_id", "Upload id")
            source = source_for(upload_id)
            path = result_csv(_text(payload, "kind", "Result type"), _text(payload, "job_id", "Job id"))
            key_columns = [str(name) for name in payload.get("key_columns") or [] if str(name).strip()]
            raw_columns = payload.get("columns") or {}
            if not isinstance(raw_columns, dict):
                raise ValueError("Columns must map result columns to target column names.")
            column_map = {str(name): str(target or name).strip() for name, target in raw_columns.items()}
            if any(not target or len(target) > 128 for target in column_map.values()):
                raise ValueError("Target column names must be 1-128 characters.")

            summary = sql.write_back(
                source["server"],
                source["database"],
                source["schema"],
                source["table"],
                path,
                key_columns,
                column_map,
            )
            with sources_lock:
                source["columns"].extend(summary["columns_added"])
            return summary

        return _respond(write)
