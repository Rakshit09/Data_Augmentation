import argparse
from pathlib import Path

import duckdb


def prepare_index(parquet_path: str, db_path: str, force: bool = False, threads: int = 8) -> None:
    parquet = Path(parquet_path)
    if not parquet.exists():
        raise FileNotFoundError(f"Parquet file not found: {parquet}")

    db = Path(db_path)
    db.parent.mkdir(parents=True, exist_ok=True)

    if db.exists() and force:
        db.unlink()

    
    con = duckdb.connect(db_path)
    con.execute("LOAD spatial;")
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

    parquet_sql = f"'{parquet.as_posix()}'"

    print("Creating lookup table from Parquet. This is a one-time step. Generating 3035 projections...")
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
            SUBSTR(CAST(quadkey AS VARCHAR), 1, 14) AS quadkey_prefix_14,
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
        ORDER BY quadkey_prefix_14, bbox_xmin, bbox_ymin;
    """)

    print("Creating spatial index.")
    con.execute("CREATE INDEX buildings_geom_rtree ON buildings USING RTREE (geom);")
    con.execute("CREATE INDEX buildings_geom_3035_rtree ON buildings USING RTREE (geom_3035);")
    con.execute("CREATE INDEX buildings_quadkey_prefix_14_idx ON buildings(quadkey_prefix_14);")

    row_count = con.execute("SELECT COUNT(*) FROM buildings;").fetchone()[0]
    con.close()
    print(f"Ready: {db_path} ({row_count:,} buildings)")

if __name__ == "__main__":
    parser = argparse.ArgumentParser()

    parser.add_argument("--parquet_path", required=True)
    parser.add_argument("--db_path", required=True)
    parser.add_argument("--threads", type=int, default=8)
    parser.add_argument("--force", action="store_true")

    args = parser.parse_args()

    prepare_index(
        parquet_path=args.parquet_path,
        db_path=args.db_path,
        force=args.force,
        threads=args.threads)