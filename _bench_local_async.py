import json
import tempfile
import time
from pathlib import Path
from building_lookup_app import enrich_exposure_csv

csv_path = Path('test_10000.csv').resolve()
db_path = Path(r'C:/Users/rajoshi/Data Augmentation/etl_output/building_lookup.duckdb')
out_dir = Path(tempfile.mkdtemp(prefix='enrich_local_async_'))
output_path = out_dir / 'out.csv'

def progress(phase, percent):
    print(f'PROGRESS {percent}% {phase}', flush=True)

started = time.perf_counter()
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
print('RESULT', json.dumps({'elapsed_seconds': round(time.perf_counter()-started, 3), 'rows': summary.get('total_rows')}, ensure_ascii=True), flush=True)
