import json
from pathlib import Path
from typing import Any, Dict, List, Optional

import duckdb

from .utils import safe_json_value, sql_string, write_csv


def write_exports(job_dir: Path, columns: List[str], rows: List[Dict[str, Any]]) -> Dict[str, Optional[Path]]:
    job_dir.mkdir(parents=True, exist_ok=True)
    csv_path = job_dir / "results.csv"
    parquet_path = job_dir / "results.parquet"
    geojson_path = job_dir / "results.geojson"

    write_csv(csv_path, columns, rows)
    parquet_ok = _write_parquet_from_csv(csv_path, parquet_path)
    _write_geojson(geojson_path, rows)

    return {
        "csv": csv_path,
        "parquet": parquet_path if parquet_ok else None,
        "geojson": geojson_path,
    }


def _write_parquet_from_csv(csv_path: Path, parquet_path: Path) -> bool:
    con = duckdb.connect()
    try:
        con.execute(f"""
            COPY (
                SELECT *
                FROM read_csv_auto({sql_string(str(csv_path.resolve()))}, header = true, ignore_errors = true)
            )
            TO {sql_string(str(parquet_path.resolve()))}
            (FORMAT PARQUET);
        """)
        return parquet_path.is_file()
    except Exception:
        parquet_path.unlink(missing_ok=True)
        return False
    finally:
        con.close()


def _write_geojson(path: Path, rows: List[Dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        handle.write('{"type":"FeatureCollection","features":[')
        first = True
        for row in rows:
            lon = _coordinate(row, "raster_sample_lon")
            lat = _coordinate(row, "raster_sample_lat")
            if lon is None or lat is None:
                continue
            feature = {
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [lon, lat]},
                "properties": {
                    key: safe_json_value(value)
                    for key, value in row.items()
                    if key not in {"raster_sample_lon", "raster_sample_lat"}
                },
            }
            if not first:
                handle.write(",")
            json.dump(feature, handle, ensure_ascii=False)
            first = False
        handle.write("]}")


def _coordinate(row: Dict[str, Any], key: str) -> Optional[float]:
    try:
        value = float(row.get(key))
    except (TypeError, ValueError):
        return None
    if not (-180 <= value <= 180) and key.endswith("_lon"):
        return None
    if not (-90 <= value <= 90) and key.endswith("_lat"):
        return None
    return value
