# Data Augmentation Tool

Data Augmentation Tool is a web application for working with building lookup data. It supports three main tasks:

- Look up a building on the map and inspect its attributes.
- Enrich a CSV exposure file with building attributes from a local DuckDB lookup database.
- Create a new DuckDB lookup database either from OpenBuildingMap data or from an existing Parquet file.

The application runs locally in your browser. Building queries and CSV enrichment use local files. Address search and map tiles depend on online services.


## Starting the Application

The application is placed as a bundled zip file at: "J:\cms\Internal\Tools\Data Augmentation Tool\DataAugmentation.zip"
The version numer changes with each update, so make sure you pick the latest version.

Copy the zip file to your local C:\ drive or "Documents" folder in the analytics desktop. Unzip the package. This will create a folder with the same name. Inside this folder, you will find an EXE. Launch the EXE by double clicking. It starts a local server, opens the browser automatically, and serves the app at `http://127.0.0.1:8100`.

CRITICAL NOTE: Do not run the application from your local desktop if the Parquet or DuckDB files are stored on the J: drive — this will not work reliably.

You must follow one of the options below:

Option 1: Use the Analytics Desktop environment.
Option 2 : Move the Parquet and DuckDB files to a local drive before running the application.

If using the Analytics Desktop, you must first copy the DuckDB and Parquet files from the J: drive into the etl_output folder inside the application directory. 

What ever you use, the parquet/DuckDB files should be placed locally


## Application Layout

The app has three tabs:

1. `Building Lookup`: search for an address or click on the map to inspect a building.
2. `Enrich Exposure`: upload a CSV and append building attributes to each row.
3. `Create OBM Database`: build a new lookup database from OpenBuildingMap or from your own Parquet file.


## Before You Begin

If you already have a lookup database:

1. Open the app.
2. In `Active Data Source`, click `Refresh` to scan for local `.duckdb` files.
3. Select the correct lookup database or type its path.
4. Click `Use selected database`.

The selected file must already contain a `buildings` table. A generic DuckDB file is not enough.

## Tab 1: Building Lookup

Use this tab to inspect individual buildings.

### Search by Address

1. Open `Building Lookup`.
2. Enter at least 3 characters in `Search address`.
3. Choose a result from the list.
4. The map zooms to that location.
5. Click on a building footprint to load its attributes.

### Search by Clicking the Map

1. Open `Building Lookup`.
2. Pan and zoom to the area you need.
3. Click on the building footprint.
4. Review the building details in the right panel.

### What You See

- `Match type`: whether the point matched inside a polygon or by nearest feature logic.
- `Distance`: the lookup distance in meters when a nearest-feature match was used.
- `Choose displayed fields`: controls which building attributes are shown.

Typical fields can include building ID, source, height, occupancy, floorspace, and data quality indicators. The exact list depends on the active lookup database.

## Tab 2: Enrich Exposure

Use this tab to append building information to each row in an exposure CSV.

### Required Input

- A CSV file.
- One latitude column.
- One longitude column.
- An active DuckDB lookup database.

### Steps

1. Open `Enrich Exposure`.
2. Upload a CSV file.
3. Review the preview table.
4. Select the latitude and longitude columns.
5. Choose a `Match mode`.
6. Set `Max nearest distance (m)`. The defaults is set to 50 meters.
7. Choose which database fields to append.
8. Click `Run enrichment`.
9. Wait for the progress indicator to complete.
10. Download the enriched CSV.

### Match Modes

- `Inside polygon + Nearest polygon`: tries to match the point inside a building first, then falls back to the nearest building polygon.
- `Inside polygon only`: only accepts rows whose point falls inside a building polygon.
- `Nearest centroid only`: matches the nearest building centroid within the allowed distance.

### Output Files

The enriched CSV keeps your original columns and adds the fields you select in the "Choose appended columns" container.

The app also shows a statistics panel and offers a separate stats CSV download after the run completes.

### Operational Limits

- Only one enrichment job can run at a time.
- Uploads and results are cleaned up automatically by the app.
- The app keeps only the latest recent upload and result artifacts.

Download your enriched CSV as soon as the run finishes. Do not treat the app's working folders as long-term storage.

## Tab 3: Create OBM Database

Use this tab when you need a new lookup database.

There are two database creation workflows.

### Workflow 1: Create OBM Database

This workflow downloads and prepares building data using OpenBuildingMap.

#### Inputs

- Optional boundary file:
  - `.gpkg`
  - `.zip` containing a shapefile and its sidecar files
- Output folder
- Output Parquet file name
- DuckDB work file name
- DuckDB lookup file name

#### Steps

1. Open `Create OBM Database`.
2. Expand `Workflow 1: Create OBM Database`.
3. Upload a boundary file if you are not using the default Germany extent.
4. Choose the output folder.
5. Confirm the output file names.
6. Click `Create database`.
7. Wait for the ETL progress to finish.

#### Result

The workflow creates:

- A cleaned Parquet file.
- A DuckDB work file.
- A DuckDB lookup database.

When the job completes, the new lookup database is activated automatically in the app.

#### Important Rules

- If no boundary file is supplied, the workflow uses the default Germany boundary.
- The DuckDB work file and DuckDB lookup file must be different paths.
- Output paths are treated as local filesystem paths.
- Existing output files may be replaced by this workflow.

### Workflow 2: Use Custom Parquet

Use this when you already have a local Parquet file with building data.

#### Required Mappings

- Latitude
- Longitude
- Geometry
- Occupancy

#### Optional Mappings

- Height
- Year built
- Construction
- Roof type
- Basement

You can also add up to 10 extra mapped fields.

#### Steps

1. Expand `Workflow 2: Use Custom Parquet`.
2. Browse to a local `.parquet` file.
3. Click `Inspect columns`.
4. Review the suggested mappings.
5. Complete the required mappings.
6. Optionally add extra output fields.
7. Choose the DuckDB lookup output path.
8. Click `Create database from Parquet`.
9. Wait for the progress indicator to complete.

#### Mapping Rules

- The Parquet file must exist locally.
- The output path must end with `.duckdb`.
- Extra field names must use letters, numbers, and underscores, and must start with a letter or underscore.
- Extra field names cannot reuse reserved building column names.

#### Result

The app creates a new DuckDB lookup database and activates it automatically.

## Files and Folders Used by the App

Common locations in this repo:

- `etl_output/building_lookup.duckdb`: default lookup database.
- `etl_output/app_uploads`: temporary uploaded CSV files.
- `etl_output/app_results`: temporary enrichment outputs.
- `etl_output/duckdb_temp`: temporary working files used by database creation.

These folders are runtime working areas. Important outputs should be moved or backed up elsewhere after creation or download.

## Troubleshooting

### "Lookup database has not been selected or prepared"

Select a valid `.duckdb` lookup database from `Active Data Source`, or create one in `Create OBM Database`.

### "The selected DuckDB file is not a lookup database"

The chosen file does not contain the required `buildings` table. Pick a different `.duckdb` file or create a new one.

### CSV upload succeeds but enrichment does not start

Check that:

- You selected both latitude and longitude columns.
- The active lookup database is valid.
- Another enrichment job is not already running.

### Many rows come back with no match

Check that:

- Coordinates are in decimal degrees.
- Latitude and longitude were mapped correctly.
- The lookup database covers the same geographic area as your CSV.
- `Max nearest distance (m)` is large enough for your use case.
- The chosen match mode is appropriate.

### Address search fails

Address search depends on external geocoding services. If it fails:

- Check your internet connection.
- Try a broader or simpler query.
- Click directly on the map instead.

### File or folder browse dialogs do not open

In some runtime environments the native file picker is unavailable. If that happens, type the local path into the field manually.

## Recommended User Workflow

If you are using the app for the first time:

1. Make sure you have or create a DuckDB lookup database.
2. Activate that database in `Active Data Source`.
3. Test a few points in `Building Lookup`.
4. Run a small CSV through `Enrich Exposure` before processing a large file.
5. Download the enriched CSV immediately after completion.

## Command Reference

If you are running from source, the main CLI commands are:

```powershell
python building_lookup_app.py serve --db etl_output/building_lookup.duckdb --host 127.0.0.1 --port 5000
python building_lookup_app.py prepare-index --parquet etl_output/buildings_de_cleaned.parquet --db etl_output/building_lookup.duckdb --force
```

`prepare-index` builds a lookup database from an existing Parquet file. `serve` starts the web app against a lookup database that already exists.