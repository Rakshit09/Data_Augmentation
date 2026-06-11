import json
import tempfile
import time
from pathlib import Path
from building_lookup_app import enrich_exposure_csv

csv_path = Path('test_10000.csv').resolve()
dbs = [
    Path(r'J:/cms/Internal/Tools/DE_Data_Augmentation/building_lookup.duckdb'),
    Path(r'C:/Users/rajoshi/Data Augmentation/etl_output/building_lookup.duckdb'),
]

for db_path in dbs:
    print(f'=== {db_path} ===', flush=True)
    out_dir = Path(tempfile.mkdtemp(prefix='enrich_bench_'))
    output_path = out_dir / 'out.csv'
    checkpoints = []
    def progress(phase, percent):
        checkpoints.append((time.perf_counter(), phase, percent))
        print(f'PROGRESS {percent}% {phase}', flush=True)
    started = time.perf_counter()
    try:
        summary = enrich_exposure_csv(
            db_path=str(db_path),
            csv_path=csv_path,
            output_path=output_path,
            lat_col='Latitude',
            lon_col='Longitude',
            mode='inside_nearest',
            max_distance_m=50.0,
            appended_fields=['building_id','height_m','occupancy_code'],
            progress_callback=progress,
        )
        elapsed = time.perf_counter() - started
        print('RESULT ok', json.dumps({
            'elapsed_seconds': round(elapsed, 3),
            'rows': summary.get('total_rows'),
            'avg_nearest': summary.get('average_nearest_distance_m'),
        }, ensure_ascii=True), flush=True)
    except Exception as exc:
        elapsed = time.perf_counter() - started
        print('RESULT error', repr(exc), 'elapsed_seconds', round(elapsed, 3), flush=True)
