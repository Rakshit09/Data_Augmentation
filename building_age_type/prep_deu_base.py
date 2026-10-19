"""Split the Germany OBM lookup into one base parquet per federal state (NUTS1) for estimate_age_type.py --part."""
import os
import subprocess
import sys
import time

import duckdb

LOOKUP = r"J:\cms\Internal\Tools\Data_Augmentation\DEU\building_lookup.duckdb"
OUT = "J:/cms/Internal/Tools/Data_Augmentation/DEU/Supplement/base"
NUTS1 = "https://gisco-services.ec.europa.eu/distribution/v2/nuts/geojson/NUTS_RG_20M_2021_4326_LEVL_1.geojson"
LIMIT = int(sys.argv[1]) if len(sys.argv) > 1 else None  # optional row limit for a dry run
if LIMIT:
    OUT += "_dry"

work = os.path.join(os.environ["TEMP"], "age_type_DE")
os.makedirs(work, exist_ok=True)
os.makedirs(os.path.dirname(OUT), exist_ok=True)
nuts = os.path.join(work, "nuts1_20m.geojson")
if not os.path.exists(nuts):
    subprocess.run(["curl.exe", "-sfL", NUTS1, "-o", nuts], check=True)

t0 = time.time()
con = duckdb.connect()
con.execute("LOAD spatial; SET preserve_insertion_order = false;")
con.execute(f"SET temp_directory = '{work.replace(chr(92), '/')}/duckdb_tmp'")
con.execute(f"ATTACH '{LOOKUP}' AS l (READ_ONLY)")
con.execute(f"CREATE TABLE n AS SELECT NUTS_ID AS part, geom FROM ST_Read('{nuts.replace(chr(92), '/')}') WHERE CNTR_CODE = 'DE'")
lim = f"WHERE rowid < {LIMIT}" if LIMIT else ""
# Small (rid, part) table first so the export below streams instead of materialising all geometries.
con.execute(f"""
    CREATE TABLE b AS SELECT rowid AS rid, centroid_lon AS x, centroid_lat AS y FROM l.buildings {lim};
    CREATE TABLE p AS SELECT b.rid, any_value(n.part) AS part FROM b JOIN n ON ST_Contains(n.geom, ST_Point(b.x, b.y)) GROUP BY ALL;
    INSERT INTO p SELECT b.rid, arg_min(n.part, ST_Distance(n.geom, ST_Point(b.x, b.y)))
                  FROM b ANTI JOIN p USING (rid) CROSS JOIN n GROUP BY ALL;
""")
print(f"parts assigned in {(time.time() - t0) / 60:.1f} min", flush=True)
con.execute(f"""
    COPY (
        SELECT p.part, b.building_id, b.occupancy_code, b.occupancy_group, b.height_m, b.height_source_type,
               b.stories_exact, b.stories_min, b.stories_max,
               coalesce(b.stories_exact, CAST(round((b.stories_min + b.stories_max) / 2) AS INTEGER),
                        CAST(greatest(1, round(b.height_m / 3.0)) AS INTEGER)) AS floors_est,
               b.footprint_area_m2, b.geom AS geometry
        FROM (SELECT rowid AS rid, * FROM l.buildings {lim}) b JOIN p USING (rid)
    ) TO '{OUT}' (FORMAT parquet, PARTITION_BY (part), COMPRESSION zstd, ROW_GROUP_SIZE 122880, OVERWRITE)
""")
print(con.execute(f"SELECT part, count(*) FROM read_parquet('{OUT}/*/*.parquet', hive_partitioning=true) GROUP BY 1 ORDER BY 1").fetchall())
print(f"done in {(time.time() - t0) / 60:.1f} min")

