import argparse
import os

from obm_country_to_parquet import ETLConfig, OpenBuildingMapCountryETL
from snowflake_loader import DEFAULT_BUILDINGS_TABLE, DEFAULT_RAW_TABLE, load_parquet_to_snowflake


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run OBM ETL, then load the Parquet into Snowflake.")
    parser.add_argument("--output-dir", default="./etl_output")
    parser.add_argument("--output-parquet", default="")
    parser.add_argument("--duckdb-file", default="")
    parser.add_argument("--boundary-file", default=None)
    parser.add_argument("--lon-min", type=float, default=5.5)
    parser.add_argument("--lon-max", type=float, default=15.5)
    parser.add_argument("--lat-min", type=float, default=47.0)
    parser.add_argument("--lat-max", type=float, default=55.3)
    parser.add_argument("--skip-etl", action="store_true", help="Load an existing Parquet without running ETL.")
    parser.add_argument("--raw-table", default=os.getenv("SNOWFLAKE_RAW_TABLE", DEFAULT_RAW_TABLE))
    parser.add_argument("--buildings-table", default=os.getenv("SNOWFLAKE_BUILDINGS_TABLE", DEFAULT_BUILDINGS_TABLE))
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    output_parquet = args.output_parquet or f"{args.output_dir}/buildings_cleaned.parquet"
    duckdb_file = args.duckdb_file or f"{args.output_dir}/work_obm.duckdb"

    if not args.skip_etl:
        cfg = ETLConfig(
            output_dir=args.output_dir,
            output_parquet=output_parquet,
            duckdb_file=duckdb_file,
            temp_directory=f"{args.output_dir}/duckdb_temp",
            lon_min=args.lon_min,
            lon_max=args.lon_max,
            lat_min=args.lat_min,
            lat_max=args.lat_max,
            boundary_file=args.boundary_file,
            force=True,
        )
        etl = OpenBuildingMapCountryETL(cfg)
        etl.run()

    result = load_parquet_to_snowflake(
        parquet_path=output_parquet,
        raw_table=args.raw_table,
        buildings_table=args.buildings_table,
        force=True,
    )
    print(f"Loaded {result['row_count']:,} buildings into {result['buildings_table']}")


if __name__ == "__main__":
    main()
