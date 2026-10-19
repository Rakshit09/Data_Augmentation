"""Estimate construction period, year built and construction type for every building of a country.

Generic, config-driven (see configs/AUT.json). Intended for countries without an open building-level register:
building-level labels are gathered wherever they exist and the rest is estimated from local statistics.

Evidence
  priors   Eurostat census 2021 dwellings by period of construction per NUTS3 (all EU/EFTA countries), converted
           to building shares with an optional building census (ESRM20 material x period table, per NUTS2).
           Post-census years are extrapolated from the 2016-2020 rate.
  local    DLR World Settlement Footprint Evolution (first built-up year of the 30 m cell, 1985-2015).
  labels   any mix of: register layers (WFS/CSV with period text or exact year), OpenStreetMap start_date
           (Geofabrik PBF), Wikidata heritage items with inception, WSF new-development pseudo-labels.
  typology height / roof / footprint / use / density from the base table, learned from the labels with a
           region (NUTS2) -> national back-off, applied as a tempered likelihood ratio.
Model      P(period | building) ~ prior_NUTS3 x WSF likelihood x typology LR^0.5 ; labelled buildings are restricted
           to their recorded range. Validation: 5-fold spatial-block CV per region and label source, plus a
           WSF transfer test (are post-1985 buildings recognised when the satellite evidence is withheld?).
Type       residential: building census material x period (when configured) + ESRM20 mapping scheme, marginalised
           over the building's own period posterior; otherwise and for non-residential: ESRM20 exposure taxonomy
           shares of the region / occupancy / height class, reweighted per building by its period posterior and
           hall geometry (documented heuristics). Every estimate carries its probability and its source.

Usage:  python estimate_age_type.py configs/AUT.json [--test] [--wait-pid PID]
"""
import argparse
import hashlib
import io
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.parse

for _k in ("CURL_CA_BUNDLE", "SSL_CERT_FILE"):
    # conda's CA bundle makes Schannel verify manually and fail on incomplete server chains.
    os.environ.pop(_k, None)

import duckdb
import numpy as np
import pandas as pd
import pyarrow as pa
from osgeo import gdal

gdal.UseExceptions()

ESRM = "https://gitlab.seismo.ethz.ch/efehr/esrm20_exposure/-/raw/master/"
EUROSTAT = "https://ec.europa.eu/eurostat/api/dissemination/sdmx/2.1/data/cens_21dwop_r3/?format=SDMX-CSV&compressed=false"
NUTS3_GEOJSON = "https://gisco-services.ec.europa.eu/distribution/v2/nuts/geojson/NUTS_RG_01M_2021_4326_LEVL_3.geojson"
WSF = "https://download.geoservice.dlr.de/WSF_EVO/files/WSFevolution_v1_{lon}_{lat}.tif"
WIKIDATA = "https://query.wikidata.org/sparql"
UA = "building-age-estimator/1.0"

# Census 2021 (Eurostat) period classes; the last one runs to the data year.
CLASS_CODES = ["Y_LT1919", "Y1919-1945", "Y1946-1960", "Y1961-1980", "Y1981-2000", "Y2001-2010", "Y2011-2015", "Y_GE2016"]
CENSUS_REF_YEAR = 2020  # census 2021 reference date 1 Jan 2021 -> last complete year 2020

DEFAULTS = {
    "data_year": 2025,
    "first_year": 1600,
    "region_level": "nuts2",
    "density_cell_m": 250,
    "m_estimate": 20.0,
    "lr_power": 0.5,
    "lr_clip": [0.25, 4.0],
    "wsf": {"infill_weight_late": {"Y2001-2010": 0.4, "Y2011-2015": 0.3, "Y_GE2016": 0.25},
            "none_recent_weight": 1.3, "outside_weight": 0.05, "lag_years": 3},
    "point_match_radius_m": 15.0,
    "polygon_min_overlap": 0.3,
    "urm_before_year": 1946,
    "nonres_rules": {"masonry_old": [0.3, 1.7], "concrete_new": [0.3, 1.0], "hall_min_area_m2": 1000,
                     "hall_max_floors": 2, "hall_boost": {"steel": 2.0, "precast": 1.5}},
}


# ---------------------------------------------------------------------------
# Plumbing
# ---------------------------------------------------------------------------

def curl(url, dest=None, extra=(), timeout=1800):
    args = ["curl.exe", "-sfL", "--retry", "4", "--retry-delay", "10", "--max-time", str(timeout), "-A", UA, *extra, url]
    if dest:
        subprocess.run(args + ["-o", dest + ".part"], check=True)
        os.replace(dest + ".part", dest)
        return dest
    return subprocess.run(args, capture_output=True, check=True).stdout


def cached(work, name, url, **kw):
    path = os.path.join(work, name)
    if not os.path.exists(path):
        curl(url, path, **kw)
    return path


def sql_path(p):
    return p.replace("\\", "/").replace("'", "''")


def parse_period(text, first_year, last_year):
    """'1848 -1918', 'vor 1683', 'nach 1945', 'before 1880', '1991 to 2011', '1875' -> (lo, hi) or None."""
    if text is None or (isinstance(text, float) and np.isnan(text)):
        return None
    t = str(text).strip().lower()
    years = [int(y) for y in re.findall(r"(?<!\d)(\d{4})(?!\d)", t)]
    if not years:
        m = re.search(r"(\d{1,2})\s*(?:th|st|nd|rd)?\s*(?:c|century|jh|jahrhundert)", t) or re.match(r"c(\d{1,2})$", t)
        if m:
            c = int(m.group(1))
            return ((c - 1) * 100, (c - 1) * 100 + 99)
        return None
    if re.match(r"^(vor|before|bis|until|pre|<)", t):
        return (first_year, years[0] - 1)
    if re.match(r"^(nach|after|ab|since|post|>)", t):
        return (years[0] + 1, last_year)
    if len(years) >= 2:
        return (min(years[:2]), max(years[:2]))
    return (years[0], years[0])


class Classes:
    def __init__(self, first_year, data_year, spec=None):
        # spec: [[code, from, to], ...] with null for open ends; default = Eurostat census 2021 classes.
        spec = spec or [[c, a, b] for c, (a, b) in zip(CLASS_CODES, [(None, 1918), (1919, 1945), (1946, 1960), (1961, 1980),
                                                                      (1981, 2000), (2001, 2010), (2011, 2015), (2016, None)])]
        bounds = [(first_year if a is None else a, data_year if b is None else b) for _, a, b in spec]
        self.codes = [c for c, _, _ in spec]
        self.bounds = bounds
        self.labels = [f"before {b + 1}" if i == 0 else f"{a}-{b}" for i, (a, b) in enumerate(bounds)]
        self.k = len(bounds)
        self.first, self.last = first_year, data_year
        self.mid = np.array([(a + b) / 2 for a, b in bounds])

    def overlap(self, lo, hi):
        """Fraction of each class covered by [lo, hi] (years outside the span fall into the end classes)."""
        lo = min(max(lo, self.first), self.last)
        hi = min(max(hi, self.first), self.last)
        w = np.zeros(self.k)
        for i, (a, b) in enumerate(self.bounds):
            ov = min(hi, b) - max(lo, a) + 1
            if ov > 0:
                w[i] = ov / (b - a + 1)
        return w

    def index(self, code):
        return self.codes.index(code)


# ---------------------------------------------------------------------------
# Priors: Eurostat dwellings by period (NUTS3) x building/dwelling ratio from a building census
# ---------------------------------------------------------------------------

def eurostat_dwellings(work, iso2, cls, data_year):
    df = pd.read_csv(cached(work, "cens_21dwop_r3.csv", EUROSTAT), dtype=str, low_memory=False)
    df = df[(df["housing"] == "DW") & df["geo"].str.match(rf"^{iso2}\w{{3}}$") & df["y_const"].isin(cls.codes)]
    if df.empty:
        raise RuntimeError(f"No Eurostat census 2021 NUTS3 data for {iso2}")
    df["value"] = pd.to_numeric(df["OBS_VALUE"], errors="coerce").fillna(0.0)
    wide = df.pivot_table(index="geo", columns="y_const", values="value", aggfunc="sum").reindex(columns=cls.codes).fillna(0.0)
    # 2016-2020 (5 years) extrapolated to the data year at the same annual rate.
    wide["Y_GE2016"] *= (data_year - 2015) / (CENSUS_REF_YEAR - 2015)
    return wide


def esrm_building_census(work, cfg, cls):
    """ESRM20 'material/period' census sheet -> {nuts2: (B[class, material], materials, period_rows)}."""
    bc = cfg["building_census"]
    path = cached(work, "building_census.xlsx", ESRM + bc["file"])
    sheets = pd.read_excel(path, sheet_name=None)
    out = {}
    for df in sheets.values():
        for _, row in df.iterrows():
            nuts2 = bc["region_to_nuts2"].get(str(row["id"]))
            if not nuts2:
                continue
            raw = {}
            for col in df.columns[2:]:
                material, _, period = str(col).rpartition("/")
                material, period = material.strip(), period.strip()
                rng = parse_period(period, cls.first, bc.get("census_year", 2011))
                if rng is None or not material:
                    continue
                v = pd.to_numeric(row[col], errors="coerce")
                raw[(rng, material)] = raw.get((rng, material), 0.0) + (0.0 if pd.isna(v) else float(v))
            materials = sorted({m for _, m in raw})
            periods = sorted({p for p, _ in raw})
            out[nuts2] = (raw, materials, periods)
    if not out:
        raise RuntimeError("Building census: no rows matched region_to_nuts2")
    return out


def period_to_class_weights(cls, period):
    """Share of a census period's buildings that fall into each class (uniform within the period)."""
    lo, hi = period
    w = np.zeros(cls.k)
    years = hi - lo + 1
    for i, (a, b) in enumerate(cls.bounds):
        ov = min(hi, b) - max(lo, a) + 1
        if ov > 0:
            w[i] = ov / years
    return w


def build_priors(work, cfg, cls, nuts3_ids):
    dw = eurostat_dwellings(work, cfg["iso2"], cls, cfg["data_year"]).reindex(nuts3_ids).fillna(0.0).astype(float)
    prior = dw.copy()
    info = {"prior_source": "eurostat cens_21dwop_r3 dwellings per NUTS3"}
    census, mat_given = None, {}
    if cfg.get("building_census"):
        census = esrm_building_census(work, cfg, cls)
        covered = [i for i, (a, b) in enumerate(cls.bounds) if b <= cfg["building_census"].get("census_year", 2011) - 1]
        for nuts2, (raw, materials, periods) in census.items():
            B = np.zeros((cls.k, len(materials)))
            for (p, m), v in raw.items():
                B[:, materials.index(m)] += v * period_to_class_weights(cls, p)
            rows = [g for g in dw.index if g.startswith(nuts2)]
            D = dw.loc[rows].sum(axis=0).values
            ratio = np.ones(cls.k)
            for i in covered:
                ratio[i] = B[i].sum() / D[i] if D[i] > 0 else np.nan
            last = covered[-1]
            ratio[last + 1:] = ratio[last]
            ratio = np.where(np.isfinite(ratio) & (ratio > 0), ratio, np.nanmedian(ratio[covered]))
            prior.loc[rows] = dw.loc[rows].values * ratio
            # Material mix per class; classes after the census reuse the last covered class.
            M = B.copy()
            M[last + 1:] = M[last]
            M = (M + 1e-9) / (M + 1e-9).sum(axis=1, keepdims=True)
            mat_given[nuts2] = (M, materials, raw, periods)
        info["prior_source"] += " x building/dwelling ratio from ESRM20 building census (NUTS2)"
    prior = prior.div(prior.sum(axis=1), axis=0).fillna(1.0 / cls.k)
    return prior, mat_given, info


def grid_prior(work, cfg, cls, con, bids):
    """Gridded census building counts per class (e.g. Zensus 2022), coarse -> fine; suppressed cells/classes share
    the remainder in proportion to the coarser level. Returns an (n, k) prior aligned with `bids`."""
    import zipfile
    gp = cfg["grid_prior"]
    zpath = cached(work, os.path.basename(gp["url"]), gp["url"])
    cols = gp.get("columns", cls.codes)
    alpha = float(gp.get("smoothing", 1.0))
    p = np.full((len(bids), cls.k), 1.0 / cls.k)
    for lv in sorted(gp["levels"], key=lambda x: -x["size"]):
        csv = os.path.join(work, os.path.basename(lv["member"]))
        if not os.path.exists(csv):
            with zipfile.ZipFile(zpath) as z, z.open(lv["member"]) as f, open(csv + ".part", "wb") as o:
                shutil.copyfileobj(f, o)
            os.replace(csv + ".part", csv)
        s = int(lv["size"])
        vals = ", ".join(f'TRY_CAST(g."{c}" AS DOUBLE)' for c in cols)
        r = con.execute(f"""
            WITH g AS (SELECT * FROM read_csv('{sql_path(csv)}', delim=';', all_varchar=true, header=true))
            SELECT TRY_CAST(g."{gp['total_column']}" AS DOUBLE), {vals}
            FROM bc c LEFT JOIN g ON TRY_CAST(g."{lv['x']}" AS BIGINT) = CAST(floor(c.cx / {s}) AS BIGINT) * {s} + {s // 2}
                                 AND TRY_CAST(g."{lv['y']}" AS BIGINT) = CAST(floor(c.cy / {s}) AS BIGINT) * {s} + {s // 2}
            ORDER BY c.bid
        """).fetchnumpy()
        arr = [np.ma.filled(np.ma.asarray(a).astype(float), np.nan) for a in r.values()]
        total = arr[0]
        C = np.column_stack(arr[1:])
        scale = float(gp.get("last_class_scale", 1.0))
        total = total + np.nan_to_num(C[:, -1]) * (scale - 1)
        C[:, -1] *= scale
        total, C = total[bids - 1], C[bids - 1]
        has = np.isfinite(total) & (total > 0)
        known = np.nan_to_num(C)
        rem = np.maximum(total - known.sum(axis=1), 0)
        sup = p * np.isnan(C)
        sup_sum = sup.sum(axis=1, keepdims=True)
        fill = np.where(sup_sum > 0, sup / np.where(sup_sum > 0, sup_sum, 1), p) * rem[:, None]
        new = (known + fill + alpha * p) / (np.nan_to_num(total)[:, None] + alpha)
        p[has] = new[has] / new[has].sum(axis=1, keepdims=True)
        logging.info(f"  grid prior {s} m: {has.mean():.1%} of buildings in a published cell")
    return p


def merge_parts(cfg, work):
    parts = list(cfg["parts"])
    outs = [cfg["output_parts"].format(part=p) for p in parts]
    missing = [o for o in outs if not os.path.exists(o + ".duckdb")]
    if missing:
        raise SystemExit(f"missing part outputs: {missing}")
    out = cfg["output"]
    con = duckdb.connect()
    files = ", ".join(f"'{sql_path(o)}.parquet'" for o in outs)
    con.execute(f"COPY (SELECT * FROM read_parquet([{files}], union_by_name=true)) TO '{sql_path(out)}.parquet.part' "
                "(FORMAT parquet, COMPRESSION zstd, ROW_GROUP_SIZE 122880)")
    os.replace(out + ".parquet.part", out + ".parquet")
    tmp_db = os.path.join(work, "merged.duckdb")
    if os.path.exists(tmp_db):
        os.remove(tmp_db)
    con.execute(f"ATTACH '{sql_path(tmp_db)}' AS o")
    for i, (p, o) in enumerate(zip(parts, outs)):
        con.execute(f"ATTACH '{sql_path(o)}.duckdb' AS p{i} (READ_ONLY)")
        for t in ("validation", "build_info"):
            sel = f"SELECT '{p}' AS part, * FROM p{i}.{t}"
            con.execute(f"INSERT INTO o.{t} BY NAME {sel}" if i else f"CREATE TABLE o.{t} AS {sel}")
        con.execute(f"DETACH p{i}")
    con.execute(f"CREATE VIEW o.buildings AS SELECT * FROM read_parquet('{sql_path(out)}.parquet')")
    con.execute("DETACH o")
    con.close()
    shutil.copyfile(tmp_db, out + ".duckdb.part")
    os.replace(out + ".duckdb.part", out + ".duckdb")
    os.remove(tmp_db)


# ---------------------------------------------------------------------------
# ESRM20 mapping schemes and exposure models
# ---------------------------------------------------------------------------

HEIGHT_TOKENS = ("H:1", "H:2", "HBET:3-5", "HBET:6-")


def height_token(floors):
    if floors is None or pd.isna(floors):
        return None
    f = int(floors)
    return "H:1" if f <= 1 else "H:2" if f == 2 else "HBET:3-5" if f <= 5 else "HBET:6-"


def height_ok(tax, token):
    if token is None:
        return True
    present = [t for t in HEIGHT_TOKENS if re.search(rf"(^|/){re.escape(t)}($|/)", tax)]
    return not present or token in present


def family(tax):
    t = tax.upper()
    if t.startswith("MIX(MUR+W)") or t.startswith("MUR") or t.startswith("MIX"):
        return "masonry"
    if t.startswith("CR+PC"):
        return "precast"
    if t.startswith("CR"):
        return "concrete"
    if t.startswith("S"):
        return "steel"
    if t.startswith("W"):
        return "timber"
    return "other"


FAMILY_LABEL = {"masonry": "Masonry", "precast": "Precast concrete", "concrete": "Reinforced concrete",
                "steel": "Steel", "timber": "Timber", "other": "Other"}


def taxonomy_label(tax):
    t = tax.upper()
    if t.startswith("MIX(MUR+W)"):
        base = "Masonry + timber (mixed)"
    elif t.startswith("CR+PC"):
        base = "Precast concrete"
    elif t.startswith("CR/LFINF"):
        base = "RC frame with masonry infill"
    elif t.startswith("CR"):
        base = "RC wall" if "LWAL" in t else "Reinforced concrete"
    elif t.startswith("MUR"):
        base = "Unreinforced masonry" if "CDN" in t else "Masonry"
    elif t.startswith("W"):
        base = "Timber"
    elif t.startswith("S"):
        base = "Steel"
    else:
        base = tax.split("/")[0]
    code = ("high code" if "CDH" in t else "moderate code" if "CDM" in t else "low code" if "CDL" in t
            else "no seismic code" if "CDN" in t else None)
    return f"{base} ({code})" if code else base


def load_mapping(work, cfg, cls):
    """{(kind, material, census_period): [(share, taxonomy)]} with pre-urm_before_year brick forced to URM."""
    if not cfg.get("residential_mapping"):
        return {}
    sheets = pd.read_excel(cached(work, "res_mapping.xlsx", ESRM + cfg["residential_mapping"]), sheet_name=None)
    brick = set(cfg.get("brick_materials", []))
    out = {}
    for name, df in sheets.items():
        kind = "urban" if "urban" in name.lower() else "rural"
        for col in df.columns[1:]:
            material, _, period = str(col).rpartition("/")
            rng = parse_period(period.strip(), cls.first, cfg["building_census"].get("census_year", 2011))
            if rng is None:
                continue
            items = []
            for share, tax in re.findall(r"([\d.]+)%\s*([^\n\r]+)", str(df[col].iloc[0])):
                tax = tax.strip()
                # Pre-war load-bearing brick is unreinforced masonry at any height (ESRM maps 3+ storeys to RC frames).
                if rng[1] < cfg["urm_before_year"] and material.strip() in brick and tax.startswith("CR/LFINF"):
                    h = tax.split("/")[-1]
                    tax = "MUR+CL/LWAL+CDN/" + h if h.startswith("H") else "MUR+CL/LWAL+CDN"
                items.append((float(share) / 100.0, tax))
            out[(kind, material.strip(), rng)] = items
    return out


def load_exposure(work, cfg, con):
    country = cfg["esrm_country"]
    exp = {}
    for occ in ("Res", "Com", "Ind"):
        try:
            e = pd.read_csv(cached(work, f"exposure_{occ}.csv", ESRM + f"_exposure_models/Exposure_Model_{country}_{occ}.csv"))
        except subprocess.CalledProcessError:
            logging.warning(f"no ESRM20 {occ} exposure model for {country}")
            continue
        con.register("ep", pd.DataFrame({"i": np.arange(len(e)), "x": e["LONGITUDE"].astype(float), "y": e["LATITUDE"].astype(float)}))
        reg = dict(con.execute("""
            SELECT p.i, any_value(n.nuts2) FROM ep p LEFT JOIN nuts n ON ST_Contains(n.geom, ST_Point(p.x, p.y)) GROUP BY p.i
        """).fetchall())
        con.unregister("ep")
        e["nuts2"] = [reg.get(i) for i in range(len(e))]
        exp[occ] = e
    return exp


# ---------------------------------------------------------------------------
# Labels
# ---------------------------------------------------------------------------

def labels_wfs_csv(work, src, con, cls):
    paths, start, page = [], 0, int(src.get("page_size", 100_000))
    while True:
        path = os.path.join(work, f"lbl_{src['name']}_{start}.csv")
        if not os.path.exists(path):
            curl(src["url"] + f"&count={page}&startIndex={start}", path)
        with open(path, "rb") as fh:
            head = fh.read(500)
        if b"ExceptionReport" in head:
            raise RuntimeError(f"{src['name']}: WFS error {head[:300]!r}")
        with open(path, "rb") as fh:
            rows = sum(1 for _ in fh) - 1
        if rows <= 0:
            os.remove(path)
            break
        paths.append(sql_path(path))
        if rows < page:
            break
        start += page
    con.execute(f"CREATE OR REPLACE TEMP TABLE raw AS SELECT * FROM read_csv({paths}, header=true, all_varchar=true, union_by_name=true)")
    cols = [c for c, in con.execute("SELECT column_name FROM (DESCRIBE raw)").fetchall()]
    pattern = "POLYGON" if src["geometry"] == "polygon" else "POINT"
    gcol = next(c for c in cols if con.execute(
        f'SELECT count(*) FROM (SELECT "{c}" FROM raw LIMIT 50) WHERE "{c}" ILIKE \'%{pattern}%\'').fetchone()[0] > 0)
    text = src.get("period_sql") or f"CAST({src['year_sql']} AS VARCHAR)"
    btype = src.get("type_sql", "NULL")
    con.execute(f"""
        CREATE OR REPLACE TEMP TABLE lbl_geom AS
        SELECT {text} AS text, {btype} AS btype,
               ST_Transform(TRY(ST_GeomFromText("{gcol}")), '{src['crs']}', 'EPSG:3035', always_xy := true) AS lg
        FROM raw WHERE "{gcol}" NOT ILIKE 'CURVE%' AND "{gcol}" NOT ILIKE '%CURVE%'
    """)
    return match_label_geometries(con, src)


def match_label_geometries(con, src, radius=None):
    if src["geometry"] == "polygon":
        rows = con.execute(f"""
            SELECT b.bid, arg_max(l.text, ST_Area(ST_Intersection(b.g, l.lg))), arg_max(l.btype, ST_Area(ST_Intersection(b.g, l.lg)))
            FROM b JOIN lbl_geom l ON ST_Intersects(b.g, l.lg)
            WHERE l.lg IS NOT NULL AND l.text IS NOT NULL
            GROUP BY b.bid
            HAVING max(ST_Area(ST_Intersection(b.g, l.lg)) / nullif(ST_Area(b.g), 0)) >= {float(src.get('min_overlap', 0.3))}
        """).fetchall()
    else:
        r = float(radius or src.get("radius_m", 15.0))
        rows = con.execute(f"""
            WITH hit AS (
                SELECT b.bid, l.text, l.btype, ST_Distance(b.g, l.lg) AS d
                FROM b JOIN (SELECT * FROM lbl_geom WHERE lg IS NOT NULL AND text IS NOT NULL) l
                  ON ST_Intersects(b.g, ST_Buffer(l.lg, {r}))
            )
            SELECT bid, arg_min(text, d), arg_min(btype, d) FROM hit GROUP BY bid
        """).fetchall()
    return pd.DataFrame(rows, columns=["bid", "text", "btype"])


def labels_wikidata(work, src, con, cfg):
    # Country-wide JSON gets truncated by the endpoint timeout for large countries -> CSV per base-table extent.
    x0, y0, x1, y1 = [round(v, 3) for v in con.execute("SELECT min(lon), min(lat), max(lon), max(lat) FROM b").fetchone()]
    path = os.path.join(work, f"wikidata_{x0}_{y0}_{x1}_{y1}.csv")
    if not os.path.exists(path):
        q = f"""SELECT ?item ?lat ?lon (MIN(YEAR(?d)) AS ?y) WHERE {{
                  SERVICE wikibase:box {{ ?item wdt:P625 ?loc .
                    bd:serviceParam wikibase:cornerSouthWest "Point({x0} {y0})"^^geo:wktLiteral .
                    bd:serviceParam wikibase:cornerNorthEast "Point({x1} {y1})"^^geo:wktLiteral . }}
                  ?item wdt:P1435 ?h; wdt:P571 ?d;
                        p:P625/psv:P625 [wikibase:geoLatitude ?lat; wikibase:geoLongitude ?lon] .
                }} GROUP BY ?item ?lat ?lon"""
        curl(WIKIDATA + "?query=" + urllib.parse.quote(q), path, extra=("-H", "Accept: text/csv"), timeout=300)
    w = pd.read_csv(path, on_bad_lines="skip").dropna(subset=["lat", "lon"])
    df = pd.DataFrame({"lon": pd.to_numeric(w["lon"], errors="coerce"), "lat": pd.to_numeric(w["lat"], errors="coerce"),
                       "text": w["y"].astype("Int64").astype(str).replace("<NA>", None)}).dropna(subset=["lat", "lon"])
    con.register("wd", df)
    con.execute("""CREATE OR REPLACE TEMP TABLE lbl_geom AS
        SELECT text, NULL AS btype, ST_Transform(ST_Point(lon, lat), 'EPSG:4326', 'EPSG:3035', always_xy := true) AS lg FROM wd""")
    con.unregister("wd")
    return match_label_geometries(con, {"geometry": "point", "radius_m": src.get("radius_m", 15.0)})


def labels_osm_pbf(work, src, con):
    out = os.path.join(work, "osm_dated_" + os.path.basename(src["url"]).replace(".osm.pbf", "") + ".gpkg")
    if not os.path.exists(out):
        gdal.SetConfigOption("CPL_TMPDIR", work)
        gdal.SetConfigOption("OSM_USE_CUSTOM_INDEXING", "YES")
        gdal.VectorTranslate(out + ".part.gpkg", "/vsicurl/" + src["url"], format="GPKG",
                             SQLStatement="SELECT other_tags FROM multipolygons WHERE building IS NOT NULL AND "
                                          "(other_tags LIKE '%\"start_date\"%' OR other_tags LIKE '%\"building:year\"%')",
                             geometryType="PROMOTE_TO_MULTI")
        os.replace(out + ".part.gpkg", out)
    con.execute(f"""
        CREATE OR REPLACE TEMP TABLE lbl_geom AS
        SELECT coalesce(nullif(regexp_extract(other_tags, '"start_date"=>"([^"]*)"', 1), ''),
                        nullif(regexp_extract(other_tags, '"building:year"=>"([^"]*)"', 1), '')) AS text, NULL AS btype,
               ST_Transform(ST_Centroid(geom), 'EPSG:4326', 'EPSG:3035', always_xy := true) AS lg
        FROM ST_Read('{sql_path(out)}')
    """)
    return match_label_geometries(con, {"geometry": "point", "radius_m": 2.0})


# ---------------------------------------------------------------------------
# Typology model (labels -> likelihood ratios with region -> national back-off)
# ---------------------------------------------------------------------------

TYPO_LEVELS = [["f_height"], ["f_height", "f_roof", "f_area"], ["f_height", "f_roof", "f_area", "f_use", "f_density"]]


def _key(df, cols):
    key = df[cols[0]].astype(str)
    for c in cols[1:]:
        key = key + "|" + df[c].astype(str)
    return key.values


def fit_typology(df, w):
    model = {"root": w.sum(axis=0) / w.sum(), "national": [], "regional": [], "region_root": None}
    g = pd.DataFrame(w)
    for cols in TYPO_LEVELS:
        g["key"] = _key(df, cols)
        model["national"].append((cols, g.groupby("key").sum()))
        g["key"] = _key(df, ["region"] + cols)
        model["regional"].append((["region"] + cols, g.groupby("key").sum()))
    g["key"] = df["region"].astype(str).values
    model["region_root"] = g.groupby("key").sum()
    return model


def _backoff(p, tables, df, m):
    for cols, sums in tables:
        idx = sums.index.get_indexer(_key(df, cols))
        cnt = np.zeros_like(p)
        found = idx >= 0
        cnt[found] = sums.values[idx[found]]
        p = (cnt + m * p) / (cnt.sum(axis=1, keepdims=True) + m)
    return p


def typology_lr(model, df, cfg):
    m = cfg["m_estimate"]
    root = np.tile(model["root"], (len(df), 1))
    p_nat = _backoff(root, model["national"], df, m)
    rr = _backoff(root, [(["region"], model["region_root"])], df, m)
    p = _backoff(p_nat, model["regional"], df, m)
    lr = np.clip(p / np.maximum(rr, 1e-9), *cfg["lr_clip"])
    return lr ** cfg["lr_power"]


def wsf_likelihood(cls, wsf_years, cfg):
    w = cfg["wsf"]
    lik = np.ones((len(wsf_years), cls.k))
    late = {cls.index(c): f for c, f in w["infill_weight_late"].items()}
    ys = pd.to_numeric(pd.Series(wsf_years), errors="coerce").astype("Float64").to_numpy(dtype=float, na_value=np.nan)
    none = np.isnan(ys)
    lik[none, cls.k - 1] = w["none_recent_weight"]
    old = ~none & (ys <= 1985)
    for c, f in late.items():
        lik[old, c] = f
    for y in np.unique(ys[~none & (ys > 1985)]):
        inside = cls.overlap(int(y) - w["lag_years"], int(y)) > 0
        lik[ys == y] = np.where(inside, 1.0, w["outside_weight"])
    return lik


def posterior(prior, lik, lr=None):
    q = prior * lik * (lr if lr is not None else 1.0)
    return q / q.sum(axis=1, keepdims=True)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("config")
    ap.add_argument("--test", action="store_true", help="run on <output>_test using base_table_test")
    ap.add_argument("--wait-pid", help="wait for this process (the base-table build) before starting")
    ap.add_argument("--part", help="run one entry of cfg['parts'] (base_table/output_parts templated with {part})")
    ap.add_argument("--all", action="store_true", help="run every missing part in sequence, then merge")
    ap.add_argument("--merge", action="store_true", help="merge finished part outputs into cfg['output']")
    args = ap.parse_args()
    cfg = {**DEFAULTS, **json.load(open(args.config, encoding="utf-8"))}
    for key in ("wsf", "nonres_rules"):
        cfg[key] = {**DEFAULTS[key], **cfg.get(key, {})}
    work = cfg.get("work_dir") or os.path.join(os.environ.get("TEMP", "."), f"age_type_{cfg['iso2']}")
    os.makedirs(work, exist_ok=True)
    if args.all:
        for p in cfg["parts"]:
            if not os.path.exists(cfg["output_parts"].format(part=p) + ".duckdb"):
                subprocess.run([sys.executable, "-u", os.path.abspath(__file__), args.config, "--part", p])
        args.merge = True
    if args.merge:
        merge_parts(cfg, work)
        print(f"merged -> {cfg['output']}.parquet")
        return
    if args.part:
        cfg["base_table"] = cfg["base_table"].format(part=args.part)
        cfg["output"] = cfg["output_parts"].format(part=args.part)
        for s in cfg.get("label_sources", []):
            if s["type"] == "osm_pbf":
                s["url"] = cfg["parts"][args.part]["osm"]
    if args.test and "label_sources_test" in cfg:
        cfg["label_sources"] = cfg["label_sources_test"]
    base = cfg["base_table_test"] if args.test else cfg["base_table"]
    output = cfg["output"] + ("_test" if args.test else "")
    os.makedirs(os.path.dirname(output), exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s",
                        handlers=[logging.FileHandler(output + ".log", encoding="utf-8"), logging.StreamHandler(sys.stdout)])
    log = logging.info
    t0 = time.time()
    while args.wait_pid and not os.path.exists(base):
        alive = f" {args.wait_pid} " in subprocess.run(["tasklist", "/FI", f"PID eq {args.wait_pid}", "/NH"],
                                                     capture_output=True, text=True).stdout
        if not alive:
            raise SystemExit(f"{base} missing and process {args.wait_pid} has ended")
        time.sleep(120)
    cls = Classes(cfg["first_year"], cfg["data_year"], cfg.get("classes"))
    col = cfg["columns"]

    con = duckdb.connect()
    con.execute("LOAD spatial; SET preserve_insertion_order = true;")
    log(f"loading {base}")
    src = f"read_parquet('{sql_path(base)}')"
    con.execute(f"""
        CREATE TABLE b AS
        SELECT row_number() OVER () AS bid, *,
               ST_Transform("{col['geometry']}", 'EPSG:4326', 'EPSG:3035', always_xy := true) AS g,
               ST_X(ST_Centroid("{col['geometry']}")) AS lon, ST_Y(ST_Centroid("{col['geometry']}")) AS lat
        FROM {src}
    """)
    con.execute("CREATE TABLE bc AS SELECT bid, ST_X(ST_Centroid(g)) AS cx, ST_Y(ST_Centroid(g)) AS cy FROM b")

    log("regions (NUTS 2021)")
    nuts_path = cached(work, "nuts3.geojson", NUTS3_GEOJSON)
    con.execute(f"""
        CREATE TABLE nuts AS SELECT NUTS_ID AS nuts3, left(NUTS_ID, 4) AS nuts2, geom
        FROM ST_Read('{sql_path(nuts_path)}') WHERE CNTR_CODE = '{cfg['iso2']}'
    """)
    con.execute("""
        CREATE TABLE breg AS SELECT b.bid, n.nuts3, n.nuts2 FROM b JOIN nuts n ON ST_Contains(n.geom, ST_Point(b.lon, b.lat))
        QUALIFY row_number() OVER (PARTITION BY b.bid ORDER BY n.nuts3) = 1
    """)
    con.execute("""
        INSERT INTO breg SELECT b.bid, arg_min(n.nuts3, ST_Distance(n.geom, ST_Point(b.lon, b.lat))),
                               arg_min(n.nuts2, ST_Distance(n.geom, ST_Point(b.lon, b.lat)))
        FROM (SELECT * FROM b ANTI JOIN breg USING (bid)) b CROSS JOIN nuts n GROUP BY b.bid
    """)
    nuts3_ids = [r[0] for r in con.execute("SELECT DISTINCT nuts3 FROM nuts ORDER BY 1").fetchall()]

    log("priors")
    if cfg.get("grid_prior"):
        prior_tbl, mat_given, prior_info = None, {}, {"prior_source": cfg["grid_prior"]["name"]}
    else:
        prior_tbl, mat_given, prior_info = build_priors(work, cfg, cls, nuts3_ids)

    log("WSF + density")
    ext = con.execute("SELECT min(lon), min(lat), max(lon), max(lat) FROM b").fetchone()
    tiles = []
    for lon in range(int(np.floor(ext[0] / 2) * 2), int(ext[2]) + 1, 2):
        for lat in range(int(np.floor(ext[1] / 2) * 2), int(ext[3]) + 1, 2):
            try:
                tiles.append(cached(work, f"wsf_{lon}_{lat}.tif", WSF.format(lon=lon, lat=lat)))
            except subprocess.CalledProcessError:
                pass
    pts = con.execute("SELECT bid, lon, lat FROM b ORDER BY bid").fetchnumpy()
    wsf = np.zeros(len(pts["bid"]), dtype=np.int32)
    for path in tiles:
        ds = gdal.Open(path)
        gt = ds.GetGeoTransform()
        x1, y0 = gt[0] + gt[1] * ds.RasterXSize, gt[3] + gt[5] * ds.RasterYSize
        sel = np.nonzero((pts["lon"] >= gt[0]) & (pts["lon"] < x1) & (pts["lat"] > y0) & (pts["lat"] <= gt[3]))[0]
        if len(sel):
            arr = ds.GetRasterBand(1).ReadAsArray()
            c = np.clip(((pts["lon"][sel] - gt[0]) / gt[1]).astype(np.int64), 0, ds.RasterXSize - 1)
            r = np.clip(((pts["lat"][sel] - gt[3]) / gt[5]).astype(np.int64), 0, ds.RasterYSize - 1)
            wsf[sel] = arr[r, c]
    con.register("wsf_t", pa.table({"bid": pts["bid"], "wsf_year": np.where((wsf >= 1985) & (wsf <= 2015), wsf, 0).astype(np.int32)}))
    con.execute("CREATE TABLE bwsf AS SELECT bid, nullif(wsf_year, 0) AS wsf_year FROM wsf_t")
    con.unregister("wsf_t")
    cell = int(cfg["density_cell_m"])
    con.execute(f"CREATE TABLE bdens AS SELECT bid, count(*) OVER (PARTITION BY floor(cx / {cell}), floor(cy / {cell})) AS density FROM bc")

    def opt(name):
        return f'"{col[name]}"' if col.get(name) else "NULL"

    use_map = cfg.get("use_groups", {})
    use_expr = ("CASE CAST(" + opt("use") + " AS VARCHAR) " + " ".join(f"WHEN '{k_}' THEN '{v}'" for k_, v in use_map.items())
                + " ELSE NULL END") if use_map and col.get("use") else "CAST(NULL AS VARCHAR)"
    df = con.execute(f"""
        SELECT b.bid, r.nuts3, r.nuts2 AS region, w.wsf_year, d.density, c.cx, c.cy,
               {opt('floors')} AS floors, {opt('footprint_area')} AS footprint_area,
               {use_expr} AS use_group,
               CASE WHEN {opt('height')} IS NULL THEN 'na' WHEN {opt('height')} < 3 THEN 'h0' WHEN {opt('height')} < 6 THEN 'h1'
                    WHEN {opt('height')} < 9 THEN 'h2' WHEN {opt('height')} < 13 THEN 'h3' WHEN {opt('height')} < 18 THEN 'h4'
                    WHEN {opt('height')} < 25 THEN 'h5' ELSE 'h6' END AS f_height,
               coalesce(CAST({opt('roof_class')} AS VARCHAR), 'na') AS f_roof,
               CASE WHEN {opt('footprint_area')} IS NULL THEN 'na' WHEN {opt('footprint_area')} < 30 THEN 'a0'
                    WHEN {opt('footprint_area')} < 60 THEN 'a1' WHEN {opt('footprint_area')} < 120 THEN 'a2'
                    WHEN {opt('footprint_area')} < 250 THEN 'a3' WHEN {opt('footprint_area')} < 500 THEN 'a4'
                    WHEN {opt('footprint_area')} < 1500 THEN 'a5' ELSE 'a6' END AS f_area,
               CASE WHEN d.density <= 5 THEN 'd0' WHEN d.density <= 15 THEN 'd1' WHEN d.density <= 40 THEN 'd2'
                    WHEN d.density <= 80 THEN 'd3' ELSE 'd4' END AS f_density
        FROM b JOIN breg r USING (bid) JOIN bwsf w USING (bid) JOIN bdens d USING (bid) JOIN bc c USING (bid)
        ORDER BY b.bid
    """).df()
    df["f_use"] = df["use_group"].fillna("na").str.split(":").str[0]
    n, k = len(df), cls.k
    if prior_tbl is None:
        prior = grid_prior(work, cfg, cls, con, df["bid"].values.astype(np.int64))
    else:
        prior = prior_tbl.reindex(df["nuts3"].values).fillna(1.0 / k).values

    log("labels")
    lab = np.zeros((n, k))
    lab_w = np.zeros(n)
    restrict = np.zeros(n, dtype=bool)
    lab_src = np.full(n, None, dtype=object)
    lab_text = np.full(n, None, dtype=object)
    lab_type = np.full(n, None, dtype=object)
    stats = []
    for s in cfg.get("label_sources", []):
        name = s.get("name", s["type"])
        if s["type"] == "wsf_new_development":
            continue
        try:
            if s["type"] == "wfs_csv":
                got = labels_wfs_csv(work, s, con, cls)
            elif s["type"] == "wikidata_heritage":
                got = labels_wikidata(work, s, con, cfg)
            elif s["type"] == "osm_pbf":
                got = labels_osm_pbf(work, s, con)
            else:
                raise ValueError(f"unknown label source type {s['type']}")
        except Exception as exc:  # optional sources must not stop the build
            logging.warning(f"label source {name} skipped: {exc!r}")
            stats.append((name, 0))
            continue
        used = 0
        for bid, text, btype in got.itertuples(index=False):
            i = int(bid) - 1
            rng = parse_period(text, cls.first, cls.last)
            if rng is None or restrict[i]:
                continue  # first source in the config wins (list registers first)
            w = cls.overlap(*rng) * prior[i]
            if w.sum() <= 0:
                continue
            lab[i] = w / w.sum()
            lab_w[i] = float(s.get("weight", 1.0))
            restrict[i] = True
            lab_src[i], lab_text[i], lab_type[i] = name, str(text), btype
            used += 1
        stats.append((name, used))
        log(f"  {name}: {used:,} buildings labelled")
    pseudo = np.zeros(n, dtype=bool)
    if any(s["type"] == "wsf_new_development" for s in cfg.get("label_sources", [])):
        ws = next(s for s in cfg["label_sources"] if s["type"] == "wsf_new_development")
        ys = df["wsf_year"].values
        for i in np.nonzero(~restrict & ~pd.isna(ys))[0]:
            y = int(ys[i])
            if y > 1985:
                w = cls.overlap(y - cfg["wsf"]["lag_years"], y) * prior[i]
                lab[i] = w / w.sum()
                lab_w[i] = float(ws.get("weight", 0.5))
                pseudo[i] = True
        stats.append(("wsf_new_development", int(pseudo.sum())))
        log(f"  wsf_new_development: {int(pseudo.sum()):,} pseudo-labels")
    # WSF pseudo-labels are all post-1985 and WSF already enters via the likelihood, so they only train the typology as a fallback.
    has = restrict.copy()
    if restrict.sum() < 200:
        logging.warning("very few real labels; typology falls back to WSF pseudo-labels and priors")
        has = lab_w > 0

    lik = wsf_likelihood(cls, df["wsf_year"].values, cfg)

    log("validation: 5-fold spatial-block CV")
    block = (np.floor(df["cx"].values / 5000).astype(np.int64) * 1_000_003 + np.floor(df["cy"].values / 5000).astype(np.int64))
    fold = np.array([int(hashlib.md5(str(b).encode()).hexdigest()[:8], 16) % 5 for b in block])
    val_rows = []
    pred_cv = np.full(n, -1)
    pred_nowsf = np.full(n, -1)
    for f in range(5):
        tr = has & (fold != f)
        te = (fold == f) & (restrict | pseudo)
        if tr.sum() < 50 or te.sum() == 0:
            continue
        model_f = fit_typology(df[tr], lab[tr] * lab_w[tr, None])
        lr_f = typology_lr(model_f, df[te], cfg)
        pred_cv[te] = posterior(prior[te], lik[te], lr_f).argmax(axis=1)
        pred_nowsf[te] = posterior(prior[te], 1.0, lr_f).argmax(axis=1)
    inside = lab > 0
    near = inside.copy()
    near[:, 1:] |= inside[:, :-1]
    near[:, :-1] |= inside[:, 1:]
    base_prior = posterior(prior, 1.0).argmax(axis=1)
    base_wsf = posterior(prior, lik).argmax(axis=1)
    recent = next(i for i, (a, b) in enumerate(cls.bounds) if b >= 1986)
    for region in sorted(df["region"].dropna().unique()):
        for name in [s for s, _ in stats if s != "wsf_new_development"]:
            sel = (df["region"].values == region) & (lab_src == name) & (pred_cv >= 0)
            # Only specific labels (at most 3 classes) are informative for scoring.
            sel &= inside.sum(axis=1) <= 3
            if sel.sum() < 20:
                continue
            idx = np.nonzero(sel)[0]
            val_rows.append({
                "region": region, "label_source": name, "buildings": len(idx),
                "in_recorded_period": float(inside[idx, pred_cv[idx]].mean()),
                "within_one_class": float(near[idx, pred_cv[idx]].mean()),
                "prior_only": float(inside[idx, base_prior[idx]].mean()),
                "prior_plus_wsf": float(inside[idx, base_wsf[idx]].mean()),
            })
        sel = (df["region"].values == region) & pseudo & (pred_nowsf >= 0)
        if sel.sum() >= 20:
            idx = np.nonzero(sel)[0]
            val_rows.append({
                "region": region, "label_source": "wsf_transfer_test", "buildings": len(idx),
                "in_recorded_period": float((pred_nowsf[idx] >= recent).mean()),
                "within_one_class": None,
                "prior_only": float((base_prior[idx] >= recent).mean()),
                "prior_plus_wsf": None,
            })
    val = pd.DataFrame(val_rows)
    for r in val.itertuples(index=False):
        log(f"  {r.region} {r.label_source:24s} n={r.buildings:>7,}  model={r.in_recorded_period:.1%}  "
            f"prior-only={r.prior_only:.1%}" + (f"  prior+WSF={r.prior_plus_wsf:.1%}" if r.prior_plus_wsf is not None else ""))

    log("final model")
    model = fit_typology(df[has], lab[has] * lab_w[has, None])
    post = posterior(prior, lik, typology_lr(model, df, cfg))
    for i in np.nonzero(restrict)[0]:
        p = post[i] * (lab[i] > 0)
        post[i] = p / p.sum() if p.sum() > 0 else lab[i]
    most_likely = post.argmax(axis=1)
    # Plain argmax piles onto the modal class; per-region class weights make assigned shares match mean-posterior shares.
    best = most_likely.copy()
    for region in sorted(df["region"].dropna().unique()):
        r = np.nonzero(df["region"].values == region)[0]
        # Grid censuses are counts, so they are the better target; NUTS-level priors are only proxies.
        target = (prior if cfg.get("grid_prior") else post)[r].sum(axis=0)
        logp = np.log(post[r] + 1e-12)
        mu, best_mu, best_err = np.zeros(k), np.zeros(k), np.inf
        for t in range(400):
            cnt = np.bincount((logp + mu).argmax(axis=1), minlength=k)
            err = np.abs(cnt - target).sum() / len(r)
            if err < best_err:
                best_mu, best_err = mu.copy(), err
            if err < 0.005:
                break
            mu = np.clip(mu + np.clip((target - cnt) / np.maximum(target, 1.0), -1, 1) / (1 + t / 20), -8, 8)
        best[r] = (logp + best_mu).argmax(axis=1)
    p2 = post.copy()
    p2[np.arange(n), best] = -1
    second = p2.argmax(axis=1)
    log("class shares per region (%): census prior | mean posterior | most-likely class | assigned class")
    shares = {"classes": cls.labels}
    for region in sorted(df["region"].dropna().unique()):
        r = df["region"].values == region
        sh = {"census": prior[r].mean(axis=0), "model_mean": post[r].mean(axis=0),
              "most_likely": np.bincount(most_likely[r], minlength=k) / r.sum(),
              "assigned": np.bincount(best[r], minlength=k) / r.sum()}
        shares[region] = {key: np.round(v, 3).tolist() for key, v in sh.items()}
        log(f"  {region} " + " | ".join(" ".join(f"{x * 100:4.0f}" for x in v) for v in sh.values()))
    lo = np.array([cls.bounds[c][0] for c in best])
    hi = np.array([cls.bounds[c][1] for c in best])
    for i in np.nonzero(restrict)[0]:
        rng = parse_period(lab_text[i], cls.first, cls.last)
        lo[i], hi[i] = max(rng[0], cls.first), min(rng[1], cls.last)
    year_est = np.clip(np.rint(post @ cls.mid), lo, hi).astype(int)
    year_src = np.where(restrict, np.array([f"label:{s}" for s in lab_src], dtype=object), "model")

    log("construction type")
    exp = load_exposure(work, cfg, con)
    mapping = load_mapping(work, cfg, cls) if mat_given else {}
    urban = set(cfg.get("building_census", {}).get("urban_nuts2", []))
    floors = df["floors"].values
    htok = np.array([height_token(v) or "na" for v in floors])
    occ = df["use_group"].fillna(cfg.get("unknown_use_group", "res")).values
    occ_kind = np.array([o.split(":")[0] for o in occ])
    occ_sub = np.array([o.split(":")[1] if ":" in o else None for o in occ], dtype=object)
    mat_name = np.full(n, None, dtype=object)
    mat_p = np.zeros(n)
    tax_name = np.full(n, None, dtype=object)
    tax_p = np.zeros(n)
    tax_share = np.zeros(n)
    tax_src = np.full(n, None, dtype=object)
    old_cls = [i for i, (a, b) in enumerate(cls.bounds) if b < cfg["urm_before_year"]]
    p_old = post[:, old_cls].sum(axis=1)
    rules = cfg["nonres_rules"]
    area = pd.to_numeric(df["footprint_area"], errors="coerce").astype("Float64").to_numpy(dtype=float, na_value=0.0)
    nfl = pd.to_numeric(df["floors"], errors="coerce").astype("Float64").to_numpy(dtype=float, na_value=1.0)
    hall = (area >= rules["hall_min_area_m2"]) & (nfl <= rules["hall_max_floors"])

    def family_weights(idx, fams):
        """Per-building weights for structural families: old buildings favour masonry, large low halls steel/precast."""
        w = np.ones((len(idx), len(fams)))
        for j, f in enumerate(fams):
            if f == "masonry":
                w[:, j] = rules["masonry_old"][0] + rules["masonry_old"][1] * p_old[idx]
            elif f in ("concrete", "precast", "steel"):
                w[:, j] = rules["concrete_new"][0] + rules["concrete_new"][1] * (1 - p_old[idx])
                if f in rules["hall_boost"]:
                    w[:, j] *= np.where(hall[idx], rules["hall_boost"][f], 1.0)
        return w

    for region in sorted(df["region"].dropna().unique()):
        in_reg = df["region"].values == region
        kind = "urban" if region in urban else "rural"
        for h in np.unique(htok[in_reg]):
            token = None if h == "na" else h
            sel_h = in_reg & (htok == h)
            # Residential with a building census: material x period mix marginalised over the building's posterior.
            res_idx = np.nonzero(sel_h & (occ_kind == "res"))[0]
            if len(res_idx) and region in mat_given:
                M, materials, raw, periods = mat_given[region]
                pm = post[res_idx] @ M
                j = pm.argmax(axis=1)
                labels_m = cfg.get("material_labels", {})
                mat_name[res_idx] = [labels_m.get(materials[x], materials[x]) for x in j]
                mat_p[res_idx] = pm[np.arange(len(res_idx)), j]
                taxes = {}
                census_year = cfg["building_census"].get("census_year", 2011)
                last_cov = max(i for i, (a, b) in enumerate(cls.bounds) if b <= census_year - 1)
                for c in range(k):
                    cc = min(c, last_cov)  # post-census classes reuse the last census class
                    for mi, m in enumerate(materials):
                        for p in periods:
                            wpc = raw.get((p, m), 0.0) * period_to_class_weights(cls, p)[cc]
                            if wpc <= 0:
                                continue
                            items = mapping.get((kind, m, p), [])
                            ok = [(s_, t_) for s_, t_ in items if height_ok(t_, token)] or items
                            tot = sum(s_ for s_, _ in ok) or 1.0
                            for s_, t_ in ok:
                                taxes[(c, mi, t_)] = taxes.get((c, mi, t_), 0.0) + wpc * s_ / tot
                tax_list = sorted({t_ for _, _, t_ in taxes})
                if tax_list:
                    T = np.zeros((k, len(tax_list)))
                    for c in range(k):
                        rows_c = {key: v for key, v in taxes.items() if key[0] == c}
                        tot_m = {}
                        for (_, mi, _), v in rows_c.items():
                            tot_m[mi] = tot_m.get(mi, 0.0) + v
                        for (_, mi, t_), v in rows_c.items():
                            T[c, tax_list.index(t_)] += M[c, mi] * v / tot_m[mi]
                    pt = post[res_idx] @ T
                    ti = pt.argmax(axis=1)
                    tax_name[res_idx] = [tax_list[x] for x in ti]
                    tax_p[res_idx] = pt[np.arange(len(res_idx)), ti] / pt.sum(axis=1)
                    tax_share[res_idx] = tax_p[res_idx]
                    tax_src[res_idx] = "census_material_x_period+esrm20_mapping"
            # Everything else: ESRM20 exposure shares of this region/occupancy/height, reweighted per building.
            for okind, ekey in (("res", "Res"), ("com", "Com"), ("ind", "Ind")):
                idx_all = np.nonzero(sel_h & (occ_kind == okind) & pd.isna(tax_name))[0]
                if not len(idx_all) or ekey not in exp:
                    continue
                e = exp[ekey]
                e = e[e["nuts2"] == region]
                if e.empty:
                    e = exp[ekey]
                for sub in sorted({x for x in occ_sub[idx_all]}, key=str):
                    idx = idx_all[np.array([x == sub for x in occ_sub[idx_all]])]
                    ee = e[e["OCCUPANCY_TYPE"] == sub] if sub and "OCCUPANCY_TYPE" in e.columns else e
                    if ee.empty:
                        ee = e
                    okh = ee[[height_ok(t_, token) for t_ in ee["TAXONOMY"]]]
                    if okh.empty:
                        okh = ee
                    share = okh.groupby("TAXONOMY")["BUILDINGS"].sum()
                    share = share / share.sum()
                    taxes = share.index.tolist()
                    fams = [family(t_) for t_ in taxes]
                    ufams = sorted(set(fams))
                    fw = family_weights(idx, ufams)
                    W = share.values[None, :] * fw[:, [ufams.index(f_) for f_ in fams]]
                    W /= W.sum(axis=1, keepdims=True)
                    ti = W.argmax(axis=1)
                    tax_name[idx] = [taxes[x] for x in ti]
                    tax_p[idx] = W[np.arange(len(idx)), ti]
                    tax_share[idx] = share.values[ti]
                    tax_src[idx] = f"esrm20_{ekey.lower()}_exposure_reweighted"
                    FP = np.zeros((len(idx), len(ufams)))
                    for j, f_ in enumerate(fams):
                        FP[:, ufams.index(f_)] += W[:, j]
                    fj = FP.argmax(axis=1)
                    mat_name[idx] = [FAMILY_LABEL[ufams[x]] for x in fj]
                    mat_p[idx] = FP[np.arange(len(idx)), fj]

    log("writing output")
    res = pa.Table.from_pandas(pd.DataFrame({
        "bid": df["bid"].values,
        "nuts3": df["nuts3"].values, "nuts2": df["region"].values,
        "construction_period": [cls.labels[c] for c in best],
        "construction_period_prob": np.round(post[np.arange(n), best], 3),
        "construction_period_2nd": [cls.labels[c] for c in second],
        "construction_period_2nd_prob": np.round(post[np.arange(n), second], 3),
        "year_built_min": lo.astype(np.int32), "year_built_max": hi.astype(np.int32), "year_built_est": year_est.astype(np.int32),
        "year_built_source": year_src,
        "label_text": lab_text, "label_building_type": lab_type,
        "wsf_first_built_year": pd.array(df["wsf_year"], dtype="Int64"),
        "construction_material": mat_name, "construction_material_prob": np.round(mat_p, 3),
        "structural_taxonomy": tax_name,
        "structural_type": [taxonomy_label(t) if isinstance(t, str) else None for t in tax_name],
        "structural_type_prob": np.round(tax_p, 3), "structural_type_regional_share": np.round(tax_share, 3),
        "structural_type_source": tax_src,
    }), preserve_index=False)
    con.register("res", res)
    geom = cfg["columns"]["geometry"]
    con.execute(f'CREATE TABLE out AS SELECT b.* EXCLUDE (bid, g, lon, lat, "{geom}"), r.* EXCLUDE (bid), b."{geom}" AS geometry '
                f"FROM b JOIN res r USING (bid) ORDER BY b.bid")
    con.register("val_t", val if len(val) else pd.DataFrame({"region": [], "label_source": []}))
    con.execute("CREATE TABLE validation AS SELECT * FROM val_t")
    info = {
        "built_at": time.strftime("%Y-%m-%d %H:%M:%S"), "base_table": base, "config": json.dumps(cfg),
        **prior_info, "label_counts": json.dumps(dict(stats)),
        "buildings": n, "buildings_with_real_label": int(restrict.sum()), "wsf_pseudo_labels": int(pseudo.sum()),
        "class_shares": json.dumps(shares),
    }
    con.register("info_t", pd.DataFrame([info]))
    con.execute("CREATE TABLE build_info AS SELECT * FROM info_t")
    tmp_pq, tmp_db = os.path.join(work, "out.parquet"), os.path.join(work, "out.duckdb")
    for p in (tmp_pq, tmp_db):
        if os.path.exists(p):
            os.remove(p)
    con.execute(f"COPY out TO '{sql_path(tmp_pq)}' (FORMAT parquet, COMPRESSION zstd, ROW_GROUP_SIZE 122880)")
    con.execute(f"ATTACH '{sql_path(tmp_db)}' AS o")
    for t in ("out", "validation", "build_info"):
        con.execute(f"CREATE TABLE o.{'buildings' if t == 'out' else t} AS SELECT * FROM {t}")
    con.execute("DETACH o")
    con.close()
    for s_, d_ in ((tmp_pq, output + ".parquet"), (tmp_db, output + ".duckdb")):
        shutil.copyfile(s_, d_ + ".part")
        os.replace(d_ + ".part", d_)
        os.remove(s_)
    log(f"ALL DONE in {(time.time() - t0) / 60:.1f} min -> {output}.parquet")


if __name__ == "__main__":
    main()
