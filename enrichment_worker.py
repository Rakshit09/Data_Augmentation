import argparse
import json
from pathlib import Path

from building_lookup_app import enrich_exposure_csv, json_safe


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run one CSV enrichment in an isolated process.")
    parser.add_argument("--db-path", required=True)
    parser.add_argument("--csv-path", required=True)
    parser.add_argument("--output-path", required=True)
    parser.add_argument("--lat-col", required=True)
    parser.add_argument("--lon-col", required=True)
    parser.add_argument("--mode", required=True)
    parser.add_argument("--max-distance-m", required=True, type=float)
    parser.add_argument("--appended-fields-json", required=True)
    parser.add_argument("--summary-path", required=True)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    appended_fields = json.loads(args.appended_fields_json)
    if appended_fields is not None and not isinstance(appended_fields, list):
        raise ValueError("Appended fields payload must decode to a list or null.")

    summary = enrich_exposure_csv(
        db_path=args.db_path,
        csv_path=Path(args.csv_path),
        output_path=Path(args.output_path),
        lat_col=args.lat_col,
        lon_col=args.lon_col,
        mode=args.mode,
        max_distance_m=float(args.max_distance_m),
        appended_fields=appended_fields,
        progress_callback=None,
    )

    summary_path = Path(args.summary_path)
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    with summary_path.open("w", encoding="utf-8") as handle:
        json.dump(summary, handle, ensure_ascii=True, default=json_safe)


if __name__ == "__main__":
    main()