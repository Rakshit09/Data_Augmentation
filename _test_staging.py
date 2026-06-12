import json
import os
import sys
import time
from pathlib import Path

from building_lookup_app import enrich_exposure_csv

DB = sys.argv[1]
CSV = Path(sys.argv[2])
OUT = Path(sys.argv[3])

started = time.perf_counter()
summary = enrich_exposure_csv(
    db_path=DB,
    csv_path=CSV,
    output_path=OUT,
    lat_col="Latitude",
    lon_col="Longitude",
    mode="inside_nearest",
    max_distance_m=50.0,
    appended_fields=None,
    progress_callback=lambda phase, pct: print(f"[PROGRESS {pct:>3}%] {phase}", flush=True),
)
elapsed = time.perf_counter() - started
print(json.dumps({
    "wall_seconds": round(elapsed, 2),
    "total_rows": summary["total_rows"],
    "inside_polygon_matches": summary["inside_polygon_matches"],
    "nearest_matches": summary["nearest_matches"],
    "no_matches": summary["no_matches"],
    "remote_staging": summary.get("remote_staging"),
    "engine_threads": summary["engine_threads"],
}, indent=2))
