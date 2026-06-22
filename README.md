# Data Augmentation Platform User Manual

The Data Augmentation Platform is a local web application for working with building lookup data. Use it when you need to:

- inspect individual building footprints and attributes on a map
- enrich an exposure CSV with local building attributes
- create a new local DuckDB lookup database from OpenBuildingMap or from an existing Parquet file

> [!IMPORTANT]
> **Run the application, the Parquet files, and the DuckDB files from a local drive.** Do not work directly against `J:` or other network locations.

## 1. Install and Start the Application

### Where to get the package

- The packaged zip file is stored at `J:\cms\Internal\Tools\Data Augmentation Tool\DataAugmentation.zip`.
- The version number changes over time, so always use the latest available package.

### Start-up steps

1. Copy the zip file to a **local** folder such as `C:\` or your local `Documents` folder in Analytics Desktop.
2. Unzip the package.
3. Open the extracted folder.
4. Double-click the `.exe` file.
5. Wait for the browser to open automatically.
6. If needed, open `http://127.0.0.1:8100` manually in your browser.

> [!WARNING]
> **Do not launch the app from your local Desktop if the Parquet or DuckDB files still live on `J:`.** That setup is not reliable.

### Local file rule

- If you use Analytics Desktop, copy the Parquet and DuckDB files from `J:` into a local folder inside the application area before you start.
- If you are not using Analytics Desktop, move the working Parquet and DuckDB files to a local drive first.
- **The app should read and write local files only.**

## 2. Understand the Main Areas of the App

The application has three main tabs:

1. `Building Lookup` lets you search for a location or click the map and inspect building attributes.
2. `Enrich Exposure` lets you upload a CSV and append building information to each row.
3. `Create OBM Database` lets you create a new local DuckDB lookup database.

You will also see an `Active Data Source` section. This controls which `.duckdb` lookup database is currently in use by both `Building Lookup` and `Enrich Exposure`.

## 3. Select an Existing Lookup Database

If you already have a lookup database:

1. Open the app.
2. In `Active Data Source`, click `Refresh`.
3. Select the correct `.duckdb` file from the list, or type the local path manually.
4. Click `Use selected database`.

> [!IMPORTANT]
> **The selected DuckDB file must already contain a `buildings` table.** A generic DuckDB file is not enough.

## 4. Use the Building Lookup Tab

Use `Building Lookup` when you want to inspect one building at a time.

### Search by address

1. Open `Building Lookup`.
2. Type at least 3 characters into `Search address`.
3. Choose a result from the list.
4. The map will zoom to the selected location.
5. Click a building footprint to load the building details.

### Search by clicking the map

1. Open `Building Lookup`.
2. Pan and zoom to the area you need.
3. Click directly on a building footprint.
4. Review the building details in the right-hand panel.

### What the result fields mean

- `Match type` tells you whether the point matched inside a polygon or by nearest-feature logic.
- `Distance` shows the lookup distance in meters when a nearest-feature match was used.
- `Choose displayed fields` lets you control which building attributes appear in the result panel.

Typical fields include building ID, source, height, occupancy, floorspace, and data quality indicators. The exact list depends on the active lookup database.

## 5. Use the Enrich Exposure Tab

Use `Enrich Exposure` when you need to append building information to every row in an exposure CSV.

### You need all of the following

- a CSV file
- one latitude column
- one longitude column
- an active DuckDB lookup database

### Enrichment steps

1. Open `Enrich Exposure`.
2. Upload the CSV file.
3. Review the preview table.
4. Select the latitude column.
5. Select the longitude column.
6. Choose the `Match mode`.
7. Set `Max nearest distance (m)` if needed. The default is `50`.
8. Select the building fields you want to append.
9. Click `Run enrichment`.
10. Wait for the progress indicator to finish.
11. Download the enriched CSV.

### Match modes

- `Inside polygon + Nearest polygon` first tries to match a point inside a building, then falls back to the nearest building polygon.
- `Inside polygon only` only accepts rows whose point falls inside a building polygon.
- `Nearest centroid only` matches the nearest building centroid within the allowed distance.

### What you get back

- The enriched CSV keeps your original columns.
- The selected building fields are added as new columns.
- A statistics panel is shown in the app when the run completes.
- A separate statistics CSV is also available for download.

> [!NOTE]
> Only one enrichment job can run at a time. Uploads and results are temporary. **Download your output as soon as the job finishes.**

## 6. Use the Create OBM Database Tab

Use `Create OBM Database` when you need a new lookup database.

There are two workflows.

### Workflow 1: Create OBM Database

This workflow downloads and prepares building data from OpenBuildingMap.

#### Inputs

- built-in country selection from the catalog
- or a custom boundary file
- output folder
- output Parquet file name
- DuckDB work file name
- DuckDB lookup file name

> [!IMPORTANT]
> **Use the built-in country dropdown OR upload a custom boundary file, not both.** If both are provided, the custom boundary takes priority.

#### Supported custom boundary formats

- `.gpkg`
- `.zip` containing a shapefile and its required sidecar files

#### Workflow 1 steps

1. Open `Create OBM Database`.
2. Expand `Workflow 1: Create OBM Database`.
3. Choose a country from the built-in catalog **or** upload a custom boundary.
4. Choose the output folder.
5. Confirm the output file names.
6. Click `Create database`.
7. Watch the ETL progress until it completes.

#### Workflow 1 result

The workflow creates:

- a cleaned Parquet file
- a DuckDB work file
- a DuckDB lookup database

When the job completes, the new lookup database is activated automatically in the app.

#### Workflow 1 rules

- If you leave both boundary options empty, the app uses the legacy default Germany boundary.
- The DuckDB work file and the DuckDB lookup file must be different paths.
- Output paths are treated as local filesystem paths.
- Existing files at the same output path may be replaced.

### Workflow 2: Use Custom Parquet

Use this workflow when you already have a local Parquet file with building data.

#### Required field mappings

- Latitude
- Longitude
- Geometry
- Occupancy

#### Optional field mappings

- Height
- Year built
- Construction
- Roof type
- Basement

You can also add up to 10 extra mapped fields.

#### Workflow 2 steps

1. Expand `Workflow 2: Use Custom Parquet`.
2. Browse to a local `.parquet` file.
3. Click `Inspect columns`.
4. Review the suggested mappings.
5. Complete the required mappings.
6. Optionally add extra output fields.
7. Choose the DuckDB lookup output path.
8. Click `Create database from Parquet`.
9. Wait for the progress indicator to complete.

#### Workflow 2 rules

- The Parquet file must exist locally.
- The output path must end with `.duckdb`.
- Extra field names must use letters, numbers, and underscores, and must start with a letter or underscore.
- Extra field names cannot reuse reserved building column names.

#### Workflow 2 result

The app creates a new DuckDB lookup database and activates it automatically.

## 7. Files and Working Folders

Common runtime locations include:

- `etl_output/building_lookup.duckdb` for the default lookup database
- `etl_output/app_uploads` for temporary uploaded CSV files
- `etl_output/app_results` for temporary enrichment results
- `etl_output/duckdb_temp` for temporary database-creation working files

> [!IMPORTANT]
> These are **working folders**, not long-term storage. Move or back up important output files after creation or download.

## 8. Troubleshooting

### "Lookup database has not been selected or prepared"

Select a valid `.duckdb` lookup database in `Active Data Source`, or create a new one in `Create OBM Database`.

### "The selected DuckDB file is not a lookup database"

The chosen file does not contain the required `buildings` table. Select a different `.duckdb` file or create a new lookup database.

### CSV upload succeeds but enrichment does not start

Check the following:

- both latitude and longitude columns were selected
- the active lookup database is valid
- another enrichment job is not already running

### Many rows come back with no match

Check the following:

- coordinates are in decimal degrees
- latitude and longitude were mapped correctly
- the lookup database covers the same geographic area as the CSV
- `Max nearest distance (m)` is large enough for your use case
- the selected match mode fits your use case

### Address search fails

Address search depends on online geocoding services. If it fails:

- check your internet connection
- try a broader or simpler query
- click directly on the map instead

### File or folder browse dialogs do not open

In some runtime environments the native file picker is unavailable. If that happens, type the local path directly into the field.

## 9. Recommended First-Time Workflow

If you are using the application for the first time:

1. Make sure you already have a lookup database, or create one.
2. Activate that database in `Active Data Source`.
3. Test a few points in `Building Lookup`.
4. Run a small CSV through `Enrich Exposure` before processing a large file.
5. Download the enriched CSV immediately after completion.

## 10. Run From Source

If you are running the application from source, these are the main commands:

```powershell
python building_lookup_app.py serve --db etl_output/building_lookup.duckdb --host 127.0.0.1 --port 5000
python building_lookup_app.py prepare-index --parquet etl_output/buildings_de_cleaned.parquet --db etl_output/building_lookup.duckdb --force
```

`prepare-index` builds a lookup database from an existing Parquet file. `serve` starts the web app using a lookup database that already exists.