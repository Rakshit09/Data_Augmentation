"""Workflow 3 regression tests and an optional, isolated matching benchmark."""

import argparse
import json
import os
import subprocess
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest.mock import patch

import duckdb

import supplement_database as supplement


def connection():
    con = duckdb.connect()
    con.execute("LOAD spatial")
    con.execute(f"SET threads = {min(8, os.cpu_count() or 1)}")
    con.execute("SET preserve_insertion_order = false")
    return con


def seed(con, count, kind="polygon"):
    con.execute(f"""
        CREATE TABLE grid AS
        SELECT feature_id, (feature_id % 1000) * 25.0 AS x,
               floor(feature_id / 1000) * 25.0 AS y
        FROM range({count}) features(feature_id)
    """)
    con.execute("""
        CREATE TABLE tgt AS
        WITH shapes AS (
            SELECT feature_id AS rid, ST_MakeEnvelope(x, y, x + 10, y + 10) AS g
            FROM grid
        ), pointed AS (SELECT *, ST_PointOnSurface(g) AS pt FROM shapes)
        SELECT *, ST_Area(g) AS area,
               ST_XMin(g) AS bx0, ST_YMin(g) AS by0,
               ST_XMax(g) AS bx1, ST_YMax(g) AS by1,
               CAST(floor(ST_X(pt) / 5000) AS INTEGER) AS cx,
               CAST(floor(ST_Y(pt) / 5000) AS INTEGER) AS cy
        FROM pointed ORDER BY cy, cx
    """)
    geometry = (
        "ST_MakeEnvelope(x + shift, y + shift, x + shift + 10, y + shift + 10)"
        if kind == "polygon" else "ST_Point(x + shift + 5, y + shift + 5)"
    )
    con.execute(f"""
        CREATE TABLE src AS
        WITH shifted AS (
            SELECT *, CASE feature_id % 5 WHEN 0 THEN 12 WHEN 1 THEN 8 ELSE 1 END AS shift
            FROM grid
        ), shapes AS (SELECT feature_id AS src_id, {geometry} AS g FROM shifted),
        pointed AS (SELECT *, ST_Centroid(g) AS pt FROM shapes)
        SELECT *, ST_Area(g) AS area,
               ST_XMin(g) AS bx0, ST_YMin(g) AS by0,
               ST_XMax(g) AS bx1, ST_YMax(g) AS by1,
               CAST(floor(ST_X(pt) / 5000) AS INTEGER) AS cx,
               CAST(floor(ST_Y(pt) / 5000) AS INTEGER) AS cy
        FROM pointed ORDER BY cy, cx
    """)
    con.execute("DROP TABLE grid")


def polygon_reference(con, radius):
    return con.execute(f"""
        WITH candidates AS (
            SELECT t.rid, s.src_id, ST_Intersects(s.g, t.pt) AS inside,
                   ST_Distance(t.g, s.g) AS distance0, t.area AS ta, s.area AS sa,
                   COALESCE(TRY(ST_Area(ST_Intersection(t.g, s.g))), 0.0) AS inter
            FROM tgt t CROSS JOIN src s
        ), ranked AS (
            SELECT *, bool_or(inside) OVER (PARTITION BY rid) AS has_inside
            FROM candidates
        ), kept AS (
            SELECT rid, src_id, inside, CASE WHEN inside THEN 0.0 ELSE distance0 END AS distance_m,
                   inter / NULLIF(ta + sa - inter, 0) AS iou, sa / NULLIF(ta, 0) AS area_ratio
            FROM ranked WHERE inside OR (NOT has_inside AND distance0 <= {radius})
        )
        SELECT * FROM kept
        QUALIFY row_number() OVER (
            PARTITION BY rid ORDER BY inside DESC, iou DESC NULLS LAST, distance_m, src_id
        ) = 1 ORDER BY rid
    """).fetchall()


def point_reference(con, radius):
    return con.execute(f"""
        WITH candidates AS (
            SELECT s.src_id, t.rid, t.area, ST_Intersects(t.g, s.pt) AS inside,
                   ST_Distance(t.g, s.pt) AS distance_m
            FROM src s CROSS JOIN tgt t
        )
        SELECT src_id, rid, inside, distance_m FROM candidates
        WHERE inside OR distance_m <= {radius}
        QUALIFY row_number() OVER (
            PARTITION BY src_id ORDER BY inside DESC, distance_m, area, rid
        ) = 1 ORDER BY src_id
    """).fetchall()


class MatchingTests(unittest.TestCase):
    def test_output_alignment_with_deleted_rows(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            target = root / "target.duckdb"
            work = root / "work.duckdb"
            with duckdb.connect(str(target)) as con:
                con.execute("LOAD spatial")
                con.execute("""
                    CREATE TABLE buildings AS
                    SELECT feature_id::VARCHAR AS building_id, feature_id * 2 AS original,
                           ST_Point(feature_id, feature_id) AS geom
                    FROM range(1000) features(feature_id)
                """)
                con.execute("DELETE FROM buildings WHERE original % 14 = 0")
                con.execute("CREATE INDEX geometry_idx ON buildings USING RTREE(geom)")
                con.execute("CREATE TABLE metadata AS SELECT 'unchanged' AS value")
                expected = con.execute("""
                    SELECT building_id, original, ST_AsWKB(geom),
                           CASE WHEN rowid % 3 = 0 THEN rowid * 10 ELSE NULL END
                    FROM buildings ORDER BY rowid
                """).fetchall()
            with duckdb.connect(str(work)) as con:
                con.execute("""
                    CREATE TABLE supp AS SELECT feature_id AS rid, feature_id * 10 AS test_value,
                        'high' AS test_match_confidence
                    FROM range(1000) features(feature_id) WHERE feature_id % 3 = 0
                    ORDER BY feature_id DESC
                """)
            cfg = {
                "job_id": "alignment", "work_dir": folder, "target_db": str(target), "work_db": str(work),
                "threads": 4, "output": {"mode": "new", "keep_columns": None},
                "columns": [{"output": "test_value"}], "prefix": "test_", "add_new_buildings": False,
                "display_seed_fields": ["building_id"], "filter": None, "match": {"radius_m": 5},
                "source": {"path": "fixture.parquet", "geometry_kind": "polygon", "layers": [{"name": "fixture"}]},
            }
            original_signature = supplement._file_signature(target)
            for add_new in (False, True):
                cfg["add_new_buildings"] = add_new
                if add_new:
                    with duckdb.connect(str(work)) as con:
                        con.execute("LOAD spatial")
                        con.execute("""
                            CREATE TABLE new_rows AS SELECT 'new' AS building_id, 2000::BIGINT AS original,
                                   ST_Point(1000, 1000) AS geom, 123::BIGINT AS test_value
                        """)
                result = supplement._build_output_db(root / "output.duckdb", cfg, {}, lambda *_: None)
                with duckdb.connect(str(root / "output.duckdb")) as con:
                    con.execute("LOAD spatial")
                    actual = con.execute("SELECT building_id, original, ST_AsWKB(geom), test_value FROM buildings WHERE building_id <> 'new' ORDER BY rowid").fetchall()
                    self.assertEqual(expected, actual)
                    self.assertEqual([("unchanged",)], con.execute("SELECT * FROM metadata").fetchall())
                    self.assertEqual([("geometry_idx",)], con.execute("SELECT index_name FROM duckdb_indexes() WHERE table_name='buildings'").fetchall())
                    if add_new:
                        self.assertEqual([("new", 123)], con.execute("SELECT building_id, test_value FROM buildings WHERE ST_Intersects(geom, ST_Point(1000,1000))").fetchall())
                    self.assertEqual(len(expected) + int(add_new), result["rows_total"])
                    self.assertEqual([], result["warnings"])
                    self.assertEqual("rebuild" if add_new else "index_reuse", result["write_strategy"])
                    self.assertEqual([("3", 30)], con.execute("SELECT building_id, test_value FROM buildings WHERE ST_Intersects(geom, ST_Point(3,3))").fetchall())
                self.assertEqual(original_signature, supplement._file_signature(target))

    def test_replacement_and_pruned_output(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            target, work = root / "target.duckdb", root / "work.duckdb"
            with duckdb.connect(str(target)) as con:
                con.execute("LOAD spatial")
                con.execute("""
                    CREATE TABLE buildings AS SELECT feature_id::VARCHAR AS building_id,
                        'discard' AS unwanted, 99::INTEGER AS test_value, ST_Point(feature_id, feature_id) AS geom
                    FROM range(3) features(feature_id)
                """)
                con.execute("CREATE INDEX geometry_idx ON buildings USING RTREE(geom)")
                con.execute("CREATE INDEX unwanted_idx ON buildings(unwanted)")
            with duckdb.connect(str(work)) as con:
                con.execute("CREATE TABLE supp AS SELECT 1::BIGINT AS rid, 12.5::DOUBLE AS test_value, 'high' AS test_match_confidence")
            cfg = {
                "job_id": "replace", "work_dir": folder, "target_db": str(target), "work_db": str(work),
                "threads": 4, "output": {"mode": "new", "keep_columns": ["building_id", "geom"]},
                "columns": [{"output": "test_value"}], "prefix": "test_", "add_new_buildings": False,
                "display_seed_fields": ["building_id"], "filter": None, "match": {"radius_m": 5},
                "source": {"path": "fixture.parquet", "geometry_kind": "polygon", "layers": [{"name": "fixture"}]},
            }
            result = supplement._build_output_db(root / "output.duckdb", cfg, {}, lambda *_: None)
            self.assertEqual("rebuild", result["write_strategy"])
            self.assertEqual([], result["warnings"])
            with duckdb.connect(str(root / "output.duckdb")) as con:
                con.execute("LOAD spatial")
                self.assertEqual([("0", None), ("1", 12.5), ("2", None)], con.execute("SELECT building_id, test_value FROM buildings ORDER BY rowid").fetchall())
                self.assertEqual(["building_id", "geom", "test_value", "test_match_confidence"], [row[0] for row in con.execute("DESCRIBE buildings").fetchall()])
                self.assertEqual([("geometry_idx",)], con.execute("SELECT index_name FROM duckdb_indexes() WHERE table_name='buildings'").fetchall())

    def test_commit_signature_and_failed_copy_cleanup(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            target = root / "target.duckdb"
            target.write_bytes(b"fixture")
            cfg = {"job_id": "failure", "target_db": str(target), "target_signature": [0, 0],
                   "output": {"mode": "new", "path": str(root / "output.duckdb")}}
            with self.assertRaisesRegex(RuntimeError, "changed after matching"):
                supplement.commit_supplement(cfg, supplement.ProgressWriter(None))
            cfg["target_signature"] = supplement._file_signature(target)
            staged = supplement.staged_output_path(cfg)
            partial = staged.with_name(staged.name + ".part")

            def fail_copy(*_args):
                partial.write_bytes(b"partial copy")
                raise OSError("simulated disk failure")

            with patch.object(supplement, "_build_output_db", side_effect=fail_copy):
                with self.assertRaisesRegex(OSError, "simulated disk failure"):
                    supplement.commit_supplement(cfg, supplement.ProgressWriter(None))
            self.assertFalse(partial.exists())
            self.assertFalse(staged.exists())
            self.assertEqual(cfg["target_signature"], supplement._file_signature(target))

    def test_polygon_matches_and_new_buildings(self):
        for radius in (0.0, 5.0, 250.0):
            with self.subTest(radius=radius), connection() as con:
                seed(con, 240)
                con.execute("INSERT INTO src SELECT * REPLACE (src_id + 1000 AS src_id) FROM src WHERE src_id < 5")
                expected = polygon_reference(con, radius)
                with patch.object(supplement, "BATCH_TARGET_ROWS", 50):
                    supplement._match_polygons(con, {"match": {"radius_m": radius}}, lambda *_: None)
                    self.assertEqual(expected, con.execute("SELECT * FROM matches ORDER BY rid").fetchall())
                    supplement._find_new_buildings(con, lambda *_: None)
                expected_new = con.execute("""
                    SELECT s.src_id FROM src s
                    WHERE NOT EXISTS (SELECT 1 FROM matches m WHERE m.src_id = s.src_id)
                      AND NOT EXISTS (SELECT 1 FROM tgt t WHERE ST_Intersects(t.g, ST_PointOnSurface(s.g)))
                    ORDER BY s.src_id
                """).fetchall()
                self.assertEqual(expected_new, con.execute("SELECT * FROM new_src ORDER BY src_id").fetchall())

    def test_point_matches(self):
        for radius in (0.0, 5.0, 250.0):
            with self.subTest(radius=radius), connection() as con:
                seed(con, 240, "point")
                con.execute("INSERT INTO tgt SELECT * REPLACE (rid + 1000 AS rid) FROM tgt WHERE rid < 5")
                expected = point_reference(con, radius)
                with patch.object(supplement, "BATCH_TARGET_ROWS", 50):
                    supplement._match_points(con, {"match": {"radius_m": radius}}, lambda *_: None)
                self.assertEqual(expected, con.execute("SELECT * FROM pmatches ORDER BY src_id").fetchall())

    def test_empty_candidates(self):
        for kind in ("polygon", "point"):
            with self.subTest(kind=kind), connection() as con:
                seed(con, 20, kind)
                con.execute("DELETE FROM src" if kind == "polygon" else "DELETE FROM tgt")
                matcher = supplement._match_polygons if kind == "polygon" else supplement._match_points
                matcher(con, {"match": {"radius_m": 5}}, lambda *_: None)
                table = "matches" if kind == "polygon" else "pmatches"
                self.assertEqual(0, con.execute(f"SELECT count(*) FROM {table}").fetchone()[0])


class TimedConnection:
    def __init__(self, con):
        self.con = con
        self.queries = []

    def execute(self, sql, params=None):
        started = time.perf_counter()
        result = self.con.execute(sql, params or [])
        elapsed = time.perf_counter() - started
        if elapsed >= 0.1:
            self.queries.append({"seconds": round(elapsed, 3), "sql": " ".join(sql.split())[:180]})
        return result


def benchmark(count, compare_head):
    implementations = [("current", supplement)]
    if compare_head:
        source = subprocess.check_output(["git", "show", "HEAD:supplement_database.py"], text=True)
        original = types.ModuleType("supplement_reference")
        exec(compile(source, "supplement_reference", "exec"), original.__dict__)
        implementations.insert(0, ("HEAD", original))
    print(json.dumps({"duckdb": duckdb.__version__, "features_per_side": count}), flush=True)
    for kind in ("polygon", "point"):
        expected = None
        for name, implementation in implementations:
            with connection() as con:
                seed(con, count, kind)
                timed = TimedConnection(con)
                matcher = implementation._match_polygons if kind == "polygon" else implementation._match_points
                started = time.perf_counter()
                matcher(timed, {"match": {"radius_m": 5}}, lambda *_: None)
                elapsed = time.perf_counter() - started
                table, key = ("matches", "rid") if kind == "polygon" else ("pmatches", "src_id")
                actual = con.execute(f"SELECT * FROM {table} ORDER BY {key}").fetchall()
                if expected is not None and actual != expected:
                    raise AssertionError(f"{kind}: benchmark results changed")
                expected = actual
                print(json.dumps({"implementation": name, "kind": kind, "seconds": round(elapsed, 3),
                                  "matched": len(actual), "queries": timed.queries}), flush=True)


def output_benchmark(count, target_db):
    original = types.ModuleType("supplement_reference")
    source = subprocess.check_output(["git", "show", "HEAD:supplement_database.py"], text=True)
    exec(compile(source, "supplement_reference", "exec"), original.__dict__)
    with tempfile.TemporaryDirectory(prefix="supplement_bench_") as folder:
        root = Path(folder)
        target, work = root / "target.duckdb", root / "work.duckdb"
        with duckdb.connect(str(target)) as con:
            con.execute("LOAD spatial")
            con.execute(f"ATTACH {supplement._sql_string(str(Path(target_db).resolve()))} AS real (READ_ONLY)")
            con.execute(f"CREATE TABLE buildings AS SELECT * FROM real.buildings WHERE rowid < {count} ORDER BY rowid")
            indexes = [row[0] for row in con.execute("SELECT sql FROM duckdb_indexes() WHERE database_name='real' AND table_name='buildings'").fetchall()]
            for sql in indexes:
                con.execute(sql)
        with duckdb.connect(str(work)) as con:
            con.execute(f"""
                CREATE TABLE supp AS SELECT feature_id AS rid, feature_id * 1.5 AS test_value,
                    'high' AS test_match_confidence FROM range({count}) features(feature_id)
                WHERE feature_id % 3 = 0
            """)
        cfg = {
            "job_id": "bench", "work_dir": folder, "target_db": str(target), "work_db": str(work),
            "threads": 8, "output": {"mode": "new", "keep_columns": None},
            "columns": [{"output": "test_value"}], "prefix": "test_", "add_new_buildings": False,
            "display_seed_fields": ["building_id"], "filter": None, "match": {"radius_m": 5},
            "source": {"path": "fixture.parquet", "geometry_kind": "polygon", "layers": [{"name": "fixture"}]},
        }
        for name, implementation in (("old", original), ("new", supplement)):
            started = time.perf_counter()
            result = implementation._build_output_db(root / f"{name}.duckdb", cfg, {"matched_targets": (count + 2) // 3}, lambda *_: None)
            print(json.dumps({"implementation": name, "seconds": round(time.perf_counter() - started, 3),
                              "size_mb": round((root / f"{name}.duckdb").stat().st_size / 1e6, 1), "result": result}), flush=True)
        with connection() as con:
            con.execute(f"ATTACH {supplement._sql_string(str(root / 'old.duckdb'))} AS old (READ_ONLY)")
            con.execute(f"ATTACH {supplement._sql_string(str(root / 'new.duckdb'))} AS new (READ_ONLY)")
            differences = con.execute("""
                SELECT count(*) FROM (
                    (SELECT rowid,* FROM old.buildings EXCEPT ALL SELECT rowid,* FROM new.buildings)
                    UNION ALL
                    (SELECT rowid,* FROM new.buildings EXCEPT ALL SELECT rowid,* FROM old.buildings)
                )
            """).fetchone()[0]
            print("EXACT_ROW_AND_ORDER_DIFFERENCES", differences, flush=True)
            assert differences == 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--benchmark", type=int)
    parser.add_argument("--compare-head", action="store_true")
    parser.add_argument("--output-benchmark", type=int)
    parser.add_argument("--target-db", default="etl_output/building_lookup.duckdb")
    options, remaining = parser.parse_known_args()
    if options.benchmark:
        benchmark(options.benchmark, options.compare_head)
    elif options.output_benchmark:
        output_benchmark(options.output_benchmark, options.target_db)
    else:
        unittest.main(argv=[__file__] + remaining)