import pandas as pd
import geopandas as gpd
import rasterio
import numpy as np
from rasterio.windows import Window
from typing import Optional
import concurrent.futures
import os
import time
from threading import Lock

#Create virtual mosaic
#MOSAIC_DOWNLOAD_PATH = r"\\emea.ajgco.com\GGBRe\Analytics\cms\Internal\Clients\R+V\2026\2523346_R+V_2026_RNWL\17 Flood Projec\Download\RP_MaxDepth_Sri7\
#gdalbuildvrt maxdepth_comb_sri7.vrt *.tif    

#CONFIG 
RASTER_PATH = r"\\emea.ajgco.com\GGBRe\Analytics\cms\Internal\Clients\R+V\2026\2523346_R+V_2026_RNWL\17 Flood Project\Download\RP_MaxDepth_Sri7\maxdepth_comb_sri7.vrt"
EXPOSURE_PATH = r"\\emea.ajgco.com\GGBRe\Analytics\cms\Internal\Clients\R+V\2026\2523346_R+V_2026_RNWL\17 Flood Project\Input Data\RMS_VGB_FL_scaled_loc_v1.csv"
OUT_STATS_CSV = r"J:\cms\Internal\Clients\R+V\2026\2523346_R+V_2026_RNWL\17 Flood Project\Input Data\RMS_VGB_FL_scaled_loc_v1_RP_15m_stats.csv"
EXPOSURE_FORMAT = "csv"
CSV_ENCODING = "latin-1"

X_COL = "USERID2"
Y_COL = "USERID1"
EXPOSURE_CRS = "EPSG:4326"

RADIUS_M = 15.0
DEPTH_COL = "depth_m"
AGG_METHOD = "mean"  # "max" or "mean"

OUT_CSV  = r"J:\cms\Internal\Clients\R+V\2026\2523346_R+V_2026_RNWL\17 Flood Project\Input Data\RMS_VGB_FL_scaled_loc_v1_RP_15m_mean.csv"
OUT_GPKG = r"J:\cms\Internal\Clients\R+V\2026\2523346_R+V_2026_RNWL\17 Flood Project\Input Data\RMS_VGB_FL_scaled_loc_v1_RP_15m.gpkg"

NUM_WORKERS = min(12, os.cpu_count() or 4)
BATCH_SIZE = 1000


# ── PROGRESS BAR ─────────────────────────────────────────────────────────────
class ProgressBar:
    def __init__(self, total: int, desc: str = "Progress", width: int = 50):
        self.total = total
        self.desc = desc
        self.width = width
        self.current = 0
        self.lock = Lock()
        self.start_time = time.time()
        self._display()

    def update(self, n: int = 1):
        with self.lock:
            self.current = min(self.current + n, self.total)
            self._display()

    def _display(self):
        frac = self.current / self.total if self.total > 0 else 1.0
        filled = int(self.width * frac)
        bar = "█" * filled + "░" * (self.width - filled)
        elapsed = time.time() - self.start_time
        rate = self.current / elapsed if elapsed > 0 else 0
        eta = (self.total - self.current) / rate if rate > 0 else 0
        print(
            f"\r  {self.desc}: |{bar}| {self.current:,}/{self.total:,} "
            f"({frac:.1%}) [{elapsed:.0f}s elapsed, ~{eta:.0f}s remaining, "
            f"{rate:.0f} pts/s]",
            end="", flush=True,
        )
        if self.current >= self.total:
            print()

    def close(self):
        if self.current < self.total:
            self.current = self.total
            self._display()


# get exposure -- converts CSV to GeoDataFrame and converts exposure CRS to raster CRS in meters (eg, risk1  Lat Lon
                                                                                                         #   1     11.48 48.13  )
def load_exposure(path, fmt, x_col, y_col, exposure_crs=None, encoding="latin-1"):
    fmt = fmt.lower()
    if fmt == "csv":
        if exposure_crs is None:
            raise ValueError("For CSV input you must provide exposure_crs.")
        for enc in [encoding, "cp1252", "iso-8859-1", "utf-8"]:
            try:
                df = pd.read_csv(path, encoding=enc)
                print(f"  CSV read OK with encoding='{enc}'")
                break
            except UnicodeDecodeError:
                print(f"  Encoding '{enc}' failed, trying next...")
        else:
            raise ValueError("Could not read CSV with any attempted encoding.")
        missing = [c for c in [x_col, y_col] if c not in df.columns]
        if missing:
            raise ValueError(f"CSV missing required columns: {missing}")
        return gpd.GeoDataFrame(
            df, geometry=gpd.points_from_xy(df[x_col], df[y_col]), crs=exposure_crs,
        )
    gdf = gpd.read_file(path)
    if gdf.crs is None:
        if exposure_crs is None:
            raise ValueError("Exposure file has no CRS.")
        gdf = gdf.set_crs(exposure_crs)
    return gdf


# batch sampling - main method
# for each point:
#   find pixel window around point
#   read only that window from TIFF
#   keep pixels within radius
#   remove nodata
#   return mean or max

def sample_batch(raster_path, coords, radius_m, nodata_val,
                 agg_method, progress_bar=None):
    n = len(coords)
    results = np.full(n, np.nan, dtype=np.float64)

    with rasterio.open(raster_path) as src:
        transform = src.transform
        height, width = src.height, src.width
        res_x = transform.a
        res_y = transform.e

        inv = ~transform
        inv_a, inv_b, inv_c = inv.a, inv.b, inv.c
        inv_d, inv_e, inv_f = inv.d, inv.e, inv.f

        xs = coords[:, 0]
        ys = coords[:, 1]
        valid_mask = ~(np.isnan(xs) | np.isnan(ys))

        for i in range(n):  #-- for each cordinate point in exposure
            if not valid_mask[i]:
                continue

            x, y = xs[i], ys[i]

            left   = x - radius_m # create a box with radius_m around the point
            right  = x + radius_m
            bottom = y - radius_m
            top    = y + radius_m

            fc_left  = inv_a * left  + inv_b * top + inv_c #find the pixel coordinates of the box corners in the raster 
            fr_top   = inv_d * left  + inv_e * top + inv_f
            fc_right = inv_a * right + inv_b * bottom + inv_c
            fr_bottom= inv_d * right + inv_e * bottom + inv_f

            col_off = max(0, int(np.floor(min(fc_left, fc_right)))) #convert the pixel coordinates of the box corners to integer pixel offsets and ensure they are within the raster bounds
            row_off = max(0, int(np.floor(min(fr_top, fr_bottom))))
            col_end = min(width,  int(np.ceil(max(fc_left, fc_right))))
            row_end = min(height, int(np.ceil(max(fr_top, fr_bottom))))

            if col_end <= col_off or row_end <= row_off: #ensure the window is valid, if not skip this point
                continue

            win_width  = col_end - col_off #get the width and height of the window
            win_height = row_end - row_off

            win = Window(col_off=col_off, row_off=row_off, #form a rasterio window object to read the pixel values from the raster
                         width=win_width, height=win_height)

            try:
                data = src.read(1, window=win) #read only the pixel values from the raster for the window
            except Exception:
                continue

            # ── Pixel-centre coordinates ─────────────────────────────────
            win_origin_x = transform.c + col_off * transform.a + row_off * transform.b #find the pixel coordinates of the window origin in the raster
            win_origin_y = transform.f + col_off * transform.d + row_off * transform.e

            col_centres = win_origin_x + (np.arange(win_width) + 0.5) * res_x #find the pixel coordinates of the pixel centres in the window
            row_centres = win_origin_y + (np.arange(win_height) + 0.5) * res_y

            px = col_centres[np.newaxis, :]
            py = row_centres[:, np.newaxis]

            # ── Distance filter for circle  ────────────────────────────
            dist_sq = (px - x) ** 2 + (py - y) ** 2 #calculate the distance from the point to each pixel in the window
            radius_sq = radius_m * radius_m

            valid_data = dist_sq <= radius_sq  #threshold the pixels to only those within the radius of the point

            # Exclude nodata
            if nodata_val is not None:
                valid_data &= (data != nodata_val)

            # Exclude NaN
            valid_data &= ~np.isnan(data)

            if not valid_data.any():
                continue

            vals = data[valid_data]

            if len(vals) == 0:
                continue

            if agg_method == "mean": #aggregate the pixel values within the radius using either mean or max
                result = float(np.nanmean(vals))
            else:
                result = float(np.nanmax(vals))

            # safety checks
            if np.isnan(result) or np.isinf(result):
                continue

            if nodata_val is not None and result == nodata_val:
                continue

            results[i] = max(0.0, result)

        if progress_bar:
            progress_bar.update(n)

    return results


# parallel compute
def sample_all_parallel(raster_path, coords, radius_m, nodata_val,
                        agg_method, num_workers, batch_size):
    n = len(coords)
    results = np.full(n, np.nan, dtype=np.float64)

    # Sort points spatially
    with rasterio.open(raster_path) as src:
        inv = ~src.transform
        fc = inv.a * coords[:, 0] + inv.b * coords[:, 1] + inv.c
        fr = inv.d * coords[:, 0] + inv.e * coords[:, 1] + inv.f

    # Tile-based sort key so nearby points are processed together, improving cache locality and reducing I/O overhead
    TILE = 256
    tile_row = (fr / TILE).astype(np.int32)
    tile_col = (fc / TILE).astype(np.int32)
    sort_key = tile_row.astype(np.int64) * 1_000_000 + tile_col

    sort_order = np.argsort(sort_key)
    inv_order = np.argsort(sort_order) 
    coords_sorted = coords[sort_order]

    batches = []
    for start in range(0, n, batch_size):
        end = min(start + batch_size, n)
        batches.append((start, end))

    print(f"  {len(batches)} batches of up to {batch_size} points, "
          f"{num_workers} workers, agg={agg_method}")

    progress = ProgressBar(total=n, desc="Sampling")
    results_sorted = np.full(n, np.nan, dtype=np.float64)

    with concurrent.futures.ThreadPoolExecutor(max_workers=num_workers) as executor:
        future_to_batch = {}
        for start, end in batches:
            fut = executor.submit(
                sample_batch, raster_path, coords_sorted[start:end],
                radius_m, nodata_val, agg_method, progress,
            )
            future_to_batch[fut] = (start, end)

        for fut in concurrent.futures.as_completed(future_to_batch):
            start, end = future_to_batch[fut]
            try:
                batch_results = fut.result()
                results_sorted[start:end] = batch_results
            except Exception as exc:
                print(f"\n  Batch [{start}:{end}] raised: {exc}")

    progress.close()

    # Unsort back to original order
    results = results_sorted[inv_order]
    return results


# ── MAIN ─────
if AGG_METHOD not in ("max", "mean"):
    raise ValueError(f"AGG_METHOD must be 'max' or 'mean', got '{AGG_METHOD}'")

with rasterio.open(RASTER_PATH) as src:
    raster_crs = src.crs
    if raster_crs is None:
        raise ValueError("Raster has no CRS.")

    print(f"Raster CRS   : {raster_crs}")
    print(f"Raster nodata: {src.nodata}")
    print(f"Raster bounds: {src.bounds}")
    print(f"Raster res   : {src.res}")
    print(f"Raster shape : {src.shape}")
    nodata_val = src.nodata

# Load exposure
gdf = load_exposure(
    path=EXPOSURE_PATH, fmt=EXPOSURE_FORMAT,
    x_col=X_COL, y_col=Y_COL,
    exposure_crs=EXPOSURE_CRS, encoding=CSV_ENCODING,
)
print(f"\nExposure loaded : {len(gdf)} records")
print(f"Exposure CRS    : {gdf.crs}")

# Reproject
with rasterio.open(RASTER_PATH) as src:
    raster_crs = src.crs
if gdf.crs.to_epsg() != raster_crs.to_epsg():
    gdf = gdf.to_crs(raster_crs)
    print(f"Reprojected to  : {gdf.crs}")

if not all(gdf.geometry.geom_type == "Point"):
    raise ValueError("All exposure geometries must be Points.")

coords = np.column_stack([gdf.geometry.x.values, gdf.geometry.y.values])

print(f"\nSampling {AGG_METHOD} depth within {RADIUS_M}m circle "
      f"using {NUM_WORKERS} threads...")

t0 = time.time()
results = sample_all_parallel(
    RASTER_PATH, coords, RADIUS_M, nodata_val,
    AGG_METHOD, NUM_WORKERS, BATCH_SIZE,
)
elapsed = time.time() - t0

# Convert NaN to None for pandas
results_series = pd.array(results, dtype=pd.Float64Dtype())
gdf[DEPTH_COL] = results_series


# ── FLOOD DEPTH BAND STATISTICS ──────────────────────────────────────────────

# Columns used for statistics
risk_col = "NUMBLDGS"
tsi_cols = ["FLCV1VAL", "FLCV2VAL", "FLCV3VAL"]

# Ensure numeric columns
gdf[risk_col] = pd.to_numeric(gdf[risk_col], errors="coerce").fillna(0)

for c in tsi_cols:
    gdf[c] = pd.to_numeric(gdf[c], errors="coerce").fillna(0)

# Total TSI per location
gdf["_TSI"] = gdf[tsi_cols].sum(axis=1)

# Convert depth from metres to cm
gdf["_depth_cm"] = gdf[DEPTH_COL] * 1.0

# Define bands
def depth_band(depth_cm):
    if pd.isna(depth_cm) or depth_cm <= 0:
        return "Not affected"
    elif depth_cm < 10:
        return "<10 cm"
    elif depth_cm < 30:
        return "10 to <30 cm"
    elif depth_cm < 50:
        return "30 to <50 cm"
    elif depth_cm < 100:
        return "50 to <100 cm"
    elif depth_cm < 200:
        return "100 to <200 cm"
    elif depth_cm < 400:
        return "200 to <400 cm"
    else:
        return ">=400 cm"

gdf["_depth_band"] = gdf["_depth_cm"].apply(depth_band)

# Keep band order
band_order = [
    "Not affected",
    "<10 cm",
    "10 to <30 cm",
    "30 to <50 cm",
    "50 to <100 cm",
    "100 to <200 cm",
    "200 to <400 cm",
    ">=400 cm",
]

# Total TSI across full portfolio
total_tsi = gdf["_TSI"].sum()

# Aggregate stats
stats_df = (
    gdf.groupby("_depth_band", dropna=False)
       .agg(
           number_of_risks=(risk_col, "sum"),
           TSI=("_TSI", "sum"),
       )
       .reindex(band_order, fill_value=0)
       .reset_index()
       .rename(columns={"_depth_band": "depth_band"})
)

# % of total TSI
if total_tsi > 0:
    stats_df["pct_total_TSI"] = stats_df["TSI"] / total_tsi * 100
else:
    stats_df["pct_total_TSI"] = 0.0

# Optional formatting/rounding
stats_df["number_of_risks"] = stats_df["number_of_risks"].round(0).astype("int64")
stats_df["TSI"] = stats_df["TSI"].round(2)
stats_df["pct_total_TSI"] = stats_df["pct_total_TSI"].round(4)

# Save statistics file
stats_df.to_csv(OUT_STATS_CSV, index=False, encoding="utf-8-sig")

print("\nFlood depth band statistics:")
print(stats_df.to_string(index=False))
print(f"✓ Stats CSV : {OUT_STATS_CSV}")


print(f"\nProcessing complete in {elapsed:.1f}s "
      f"({len(gdf)/elapsed:.0f} points/sec)")
print(gdf[DEPTH_COL].describe())

# ── OUTPUTS ───
gdf.drop(columns="geometry").to_csv(OUT_CSV, index=False, encoding="utf-8-sig")
print(f"✓ CSV  : {OUT_CSV}")

if OUT_GPKG:
    # gdf.to_file
    print(f"✓ GPKG : {OUT_GPKG}")