import {Deck, FlyToInterpolator} from "https://esm.sh/@deck.gl/core@^9.0.0";
import {BitmapLayer, GeoJsonLayer} from "https://esm.sh/@deck.gl/layers@^9.0.0";
import {TileLayer} from "https://esm.sh/@deck.gl/geo-layers@^9.0.0";
import {VectorTileLayer} from "https://esm.sh/@deck.gl/carto@^9.0.0";
import {vectorQuerySource} from "https://esm.sh/@carto/api-client";

const statusEl = document.getElementById("status");
const lookupTab = document.getElementById("lookupTab");
const exposureTab = document.getElementById("exposureTab");
const etlTab = document.getElementById("etlTab");
const lookupMain = document.getElementById("lookupMain");
const exposureMain = document.getElementById("exposureMain");
const etlMain = document.getElementById("etlMain");
const lookupTools = document.getElementById("lookupTools");
const exposureTools = document.getElementById("exposureTools");
const etlTools = document.getElementById("etlTools");
const modeEyebrow = document.getElementById("modeEyebrow");
const modeTitle = document.getElementById("modeTitle");
const dataSourcePanel = document.getElementById("dataSourcePanel");
const activeParquetPath = document.getElementById("activeParquetPath");
const activeDbPath = document.getElementById("activeDbPath");
const parquetFileOptions = document.getElementById("parquetFileOptions");
const dbFileOptions = document.getElementById("dbFileOptions");
const browseParquet = document.getElementById("browseParquet");
const browseDb = document.getElementById("browseDb");
const parquetPicker = document.getElementById("parquetPicker");
const dbPicker = document.getElementById("dbPicker");
const refreshSources = document.getElementById("refreshSources");
const applyDataSource = document.getElementById("applyDataSource");
const dataSourceMessage = document.getElementById("dataSourceMessage");
const emptyEl = document.getElementById("empty");
const detailsEl = document.getElementById("details");
const matchTypeEl = document.getElementById("matchType");
const distanceEl = document.getElementById("distance");
const buildingIdEl = document.getElementById("buildingId");
const attributesEl = document.getElementById("attributes");
const searchForm = document.getElementById("searchForm");
const searchInput = document.getElementById("searchInput");
const searchResults = document.getElementById("searchResults");
const uploadForm = document.getElementById("uploadForm");
const csvFile = document.getElementById("csvFile");
const mappingControls = document.getElementById("mappingControls");
const latColumn = document.getElementById("latColumn");
const lonColumn = document.getElementById("lonColumn");
const matchMode = document.getElementById("matchMode");
const maxDistance = document.getElementById("maxDistance");
const runEnrichment = document.getElementById("runEnrichment");
const uploadSummary = document.getElementById("uploadSummary");
const previewTable = document.getElementById("previewTable");
const downloadLink = document.getElementById("downloadLink");
const statsPanel = document.getElementById("statsPanel");
const statsGrid = document.getElementById("statsGrid");

let currentUploadId = null;
let availableParquetFiles = [];
let availableDbFiles = [];
let selectedFeature = null;
let buildingsData = null;
let viewState = {
  longitude: 10.45,
  latitude: 51.16,
  zoom: 5.4,
  pitch: 0,
  bearing: 0
};

const osmLayer = new TileLayer({
  id: "osm-raster",
  data: "https://tile.openstreetmap.org/{z}/{x}/{y}.png",
  minZoom: 0,
  maxZoom: 19,
  tileSize: 256,
  renderSubLayers: (props) => {
    const {west, south, east, north} = props.tile.bbox;
    return new BitmapLayer(props, {
      id: `${props.id}-bitmap`,
      image: props.data,
      bounds: [west, south, east, north]
    });
  }
});

let deckgl;
deckgl = new Deck({
  parent: document.getElementById("map"),
  initialViewState: viewState,
  controller: true,
  layers: [osmLayer],
  getCursor: ({isDragging}) => (isDragging ? "grabbing" : "crosshair"),
  onViewStateChange: ({viewState: nextViewState}) => {
    viewState = nextViewState;
    deckgl.setProps({viewState});
  },
  onClick: ({coordinate}) => {
    if (!coordinate) return;
    lookupAtCoordinate(coordinate[0], coordinate[1]);
  }
});

lookupTab.addEventListener("click", () => switchMode("lookup"));
exposureTab.addEventListener("click", () => switchMode("exposure"));
etlTab.addEventListener("click", () => switchMode("etl"));
refreshSources.addEventListener("click", () => loadDataSources());
applyDataSource.addEventListener("click", () => applySelectedDataSource());
browseParquet.addEventListener("click", () => toggleFilePicker("parquet"));
browseDb.classList.add("hidden");

loadDataSources();
loadCartoLayer();

async function loadCartoLayer() {
  try {
    const response = await fetch("/api/carto-config");
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || "Could not load CARTO configuration");

    viewState = {...viewState, ...payload.initialViewState};
    deckgl.setProps({viewState});

    const sourceOptions = {
      accessToken: payload.accessToken,
      connectionName: payload.connectionName,
      sqlQuery: payload.sqlQuery,
      spatialDataColumn: payload.spatialDataColumn || "geom",
      columns: ["building_id", "occupancy_group", "height_m", "footprint_area_m2"]
    };
    if (payload.apiBaseUrl) sourceOptions.apiBaseUrl = payload.apiBaseUrl;

    buildingsData = vectorQuerySource(sourceOptions);
    updateDeckLayers();
    statusEl.textContent = "Ready";
  } catch (error) {
    statusEl.textContent = "Map config error";
    emptyEl.innerHTML = `<p>${escapeHtml(error.message)}</p>`;
  }
}

function updateDeckLayers() {
  const layers = [osmLayer];

  if (buildingsData) {
    layers.push(new VectorTileLayer({
      id: "carto-buildings",
      data: buildingsData,
      pickable: false,
      getFillColor: (feature) => colorForOccupancy(feature.properties?.occupancy_group),
      getLineColor: [20, 30, 45, 140],
      lineWidthMinPixels: 0.4,
      opacity: 0.74,
      updateTriggers: {
        getFillColor: []
      }
    }));
  }

  if (selectedFeature) {
    layers.push(new GeoJsonLayer({
      id: "selected-building",
      data: {
        type: "FeatureCollection",
        features: [selectedFeature]
      },
      pickable: false,
      filled: true,
      stroked: true,
      getFillColor: [255, 183, 3, 110],
      getLineColor: [193, 18, 31, 245],
      getLineWidth: 3,
      lineWidthUnits: "pixels"
    }));
  }

  deckgl.setProps({layers});
}

function colorForOccupancy(value) {
  const key = String(value || "").toLowerCase();
  if (key.includes("residential")) return [102, 166, 30, 175];
  if (key.includes("commercial")) return [231, 111, 81, 180];
  if (key.includes("industrial")) return [117, 112, 179, 175];
  if (key.includes("public") || key.includes("civic")) return [27, 158, 119, 175];
  return [111, 172, 222, 165];
}

function switchMode(mode) {
  const isLookup = mode === "lookup";
  const isExposure = mode === "exposure";
  const isEtl = mode === "etl";

  lookupTab.classList.toggle("active", isLookup);
  exposureTab.classList.toggle("active", isExposure);
  etlTab.classList.toggle("active", isEtl);

  lookupMain.classList.toggle("hidden", !isLookup);
  exposureMain.classList.toggle("hidden", !isExposure);
  etlMain.classList.toggle("hidden", !isEtl);

  lookupTools.classList.toggle("hidden", !isLookup);
  exposureTools.classList.toggle("hidden", !isExposure);
  etlTools.classList.toggle("hidden", !isEtl);
  dataSourcePanel.classList.toggle("hidden", isEtl);

  modeEyebrow.textContent = isLookup ? "Snowflake + CARTO" : " ";
  modeTitle.textContent = isLookup ? "Building Lookup"
    : isExposure ? "Enrich Exposure"
    : "Create OBM Table";

  if (isLookup) window.setTimeout(() => deckgl.redraw(true), 50);
}

async function loadDataSources() {
  setDataSourceMessage("Reading active Snowflake source...");
  refreshSources.disabled = true;

  try {
    const response = await fetch("/api/data-source");
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || "Could not load data source");

    activeParquetPath.value = payload.parquet_path || "";
    activeDbPath.value = payload.snowflake_buildings_table || payload.db_path || "";
    availableParquetFiles = payload.parquet_files || [];
    availableDbFiles = payload.db_files || [];
    renderFileOptions(parquetFileOptions, availableParquetFiles);
    renderFileOptions(dbFileOptions, availableDbFiles);
    renderFilePicker(parquetPicker, availableParquetFiles, activeParquetPath);
    renderFilePicker(dbPicker, availableDbFiles, activeDbPath);
    setDataSourceMessage("Using Snowflake for lookup and enrichment.", "success");
  } catch (error) {
    setDataSourceMessage(error.message, "error");
  } finally {
    refreshSources.disabled = false;
  }
}

function renderFileOptions(listEl, files) {
  listEl.innerHTML = files.map((path) => `<option value="${escapeHtml(path)}"></option>`).join("");
}

function renderFilePicker(pickerEl, files, inputEl) {
  if (!files.length) {
    pickerEl.innerHTML = "<p>No matching files found.</p>";
    return;
  }

  pickerEl.innerHTML = files
    .map((path) => `<button type="button" data-path="${escapeHtml(path)}">${escapeHtml(path)}</button>`)
    .join("");

  pickerEl.querySelectorAll("button").forEach((button) => {
    button.addEventListener("click", () => {
      inputEl.value = button.dataset.path || "";
      pickerEl.classList.add("hidden");
    });
  });
}

function toggleFilePicker(kind) {
  const picker = kind === "parquet" ? parquetPicker : dbPicker;
  const otherPicker = kind === "parquet" ? dbPicker : parquetPicker;
  otherPicker.classList.add("hidden");

  const files = kind === "parquet" ? availableParquetFiles : availableDbFiles;
  if (!files.length) {
    setDataSourceMessage("No matching local files found. Press Refresh after creating files.", "error");
  }
  picker.classList.toggle("hidden");
}

async function applySelectedDataSource() {
  applyDataSource.disabled = true;
  setDataSourceMessage("Applying Snowflake source...");

  try {
    const response = await fetch("/api/data-source", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({
        parquet_path: activeParquetPath.value.trim(),
        snowflake_buildings_table: activeDbPath.value.trim()
      })
    });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || "Could not apply data source");

    activeParquetPath.value = payload.parquet_path || "";
    activeDbPath.value = payload.snowflake_buildings_table || "";
    clearSelection();
    setDataSourceMessage("Active Snowflake table updated.", "success");
    await loadCartoLayer();
  } catch (error) {
    setDataSourceMessage(error.message, "error");
  } finally {
    applyDataSource.disabled = false;
  }
}

function setDataSourceMessage(message, type = "") {
  dataSourceMessage.textContent = message;
  dataSourceMessage.classList.toggle("error", type === "error");
  dataSourceMessage.classList.toggle("success", type === "success");
}

async function lookupAtCoordinate(lng, lat) {
  hideSearchResults();
  statusEl.textContent = "Searching";

  try {
    const response = await fetch(`/api/building-at?lon=${lng}&lat=${lat}`);
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.hint || payload.error || "Lookup failed");

    if (!payload.building) {
      clearSelection();
      statusEl.textContent = "No match";
      emptyEl.innerHTML = "<p>No building found near this point.</p>";
      return;
    }

    renderBuilding(payload);
    statusEl.textContent = "Matched";
  } catch (error) {
    clearSelection();
    statusEl.textContent = "Error";
    emptyEl.innerHTML = `<p>${escapeHtml(error.message)}</p>`;
  }
}

function renderBuilding(payload) {
  const building = payload.building;
  selectedFeature = {
    type: "Feature",
    geometry: building.geometry,
    properties: {building_id: building.building_id}
  };
  updateDeckLayers();

  emptyEl.classList.add("hidden");
  detailsEl.classList.remove("hidden");

  matchTypeEl.textContent = labelForMatch(payload.match_type, payload.confidence);
  distanceEl.textContent = payload.distance_m == null ? "" : `${Number(payload.distance_m).toFixed(1)} m`;
  buildingIdEl.textContent = building.building_id || "Building";

  const rows = [
    ["Use", building.occupancy_group],
    ["Raw occupancy", building.occupancy_raw],
    ["Occupancy code", building.occupancy_code],
    ["Raw height", building.height_raw],
    ["Height (m)", formatNumber(building.height_m, " m")],
    ["Height quality", building.height_quality],
    ["Footprint", formatNumber(building.footprint_area_m2, " m2")],
    ["Floorspace", formatNumber(building.floorspace_est_m2, " m2")],
    ["Completeness", formatPercent(building.attribute_completeness_score)],
    ["Source", building.source],
    ["Updated", building.last_update]
  ];

  attributesEl.innerHTML = rows
    .filter(([, value]) => value !== null && value !== undefined && value !== "")
    .map(([label, value]) => `<dt>${escapeHtml(label)}</dt><dd>${escapeHtml(value)}</dd>`)
    .join("");
}

searchForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const query = searchInput.value.trim();
  if (query.length < 3) {
    renderSearchMessage("Enter at least 3 characters.");
    return;
  }

  statusEl.textContent = "Searching";
  try {
    const response = await fetch(`/api/search-address?q=${encodeURIComponent(query)}`);
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || "Address search failed");

    renderSearchResults(payload.results || []);
    statusEl.textContent = "Ready";
  } catch (error) {
    renderSearchMessage(error.message);
    statusEl.textContent = "Error";
  }
});

function renderSearchResults(results) {
  if (!results.length) {
    renderSearchMessage("No address found.");
    return;
  }

  searchResults.classList.remove("hidden");
  searchResults.innerHTML = results
    .map((result, index) => `<button type="button" data-index="${index}">${escapeHtml(result.label)}</button>`)
    .join("");

  searchResults.querySelectorAll("button").forEach((button) => {
    button.addEventListener("click", () => {
      const result = results[Number(button.dataset.index)];
      hideSearchResults();
      flyTo(result.lon, result.lat, 18);
    });
  });
}

function flyTo(longitude, latitude, zoom) {
  viewState = {
    ...viewState,
    longitude,
    latitude,
    zoom,
    transitionDuration: 1000,
    transitionInterpolator: new FlyToInterpolator()
  };
  deckgl.setProps({viewState});
}

function renderSearchMessage(message) {
  searchResults.classList.remove("hidden");
  searchResults.innerHTML = `<p>${escapeHtml(message)}</p>`;
}

function hideSearchResults() {
  searchResults.classList.add("hidden");
  searchResults.innerHTML = "";
}

uploadForm.addEventListener("submit", (event) => {
  event.preventDefault();
  uploadSelectedCsv();
});

csvFile.addEventListener("change", () => {
  uploadSelectedCsv();
});

async function uploadSelectedCsv() {
  if (!csvFile.files.length) {
    setUploadSummary("Choose a CSV file first.");
    return;
  }

  const formData = new FormData();
  formData.append("file", csvFile.files[0]);

  statusEl.textContent = "Uploading";
  setUploadSummary("Reading CSV preview...");
  downloadLink.classList.add("hidden");

  try {
    const response = await fetch("/api/exposure/preview", {
      method: "POST",
      body: formData
    });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || "Upload failed");

    currentUploadId = payload.upload_id;
    populateColumnSelectors(payload.columns);
    renderPreview(payload.columns, payload.rows);
    mappingControls.classList.remove("hidden");
    statsPanel.classList.add("hidden");
    renderFileSummary(payload.filename, payload.rows.length);
    statusEl.textContent = "Ready";
  } catch (error) {
    statusEl.textContent = "Error";
    setUploadSummary(error.message);
    previewTable.classList.add("hidden");
  }
}

runEnrichment.addEventListener("click", async () => {
  if (!currentUploadId) {
    setUploadSummary("Upload a CSV first.");
    return;
  }

  runEnrichment.disabled = true;
  statusEl.textContent = "Enriching";
  setUploadSummary("Running Snowflake spatial enrichment...");
  downloadLink.classList.add("hidden");
  statsPanel.classList.add("hidden");

  try {
    const response = await fetch("/api/exposure/enrich", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({
        upload_id: currentUploadId,
        lat_col: latColumn.value,
        lon_col: lonColumn.value,
        mode: matchMode.value,
        max_distance_m: Number(maxDistance.value || 50)
      })
    });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || "Enrichment failed");

    pollEnrichmentProgress(payload.job_id);
  } catch (error) {
    statusEl.textContent = "Error";
    setUploadSummary(error.message);
    runEnrichment.disabled = false;
  }
});

async function pollEnrichmentProgress(jobId) {
  try {
    const response = await fetch(`/api/exposure/progress/${jobId}`);
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || "Could not read progress");

    renderProgress(payload);
    if (payload.status === "complete") {
      downloadLink.href = payload.download_url;
      downloadLink.classList.remove("hidden");
      renderSummary(payload.summary);
      renderStats(payload.summary);
      statusEl.textContent = "Done";
      runEnrichment.disabled = false;
      return;
    }

    if (payload.status === "error") throw new Error(payload.error || "Enrichment failed");
    window.setTimeout(() => pollEnrichmentProgress(jobId), 1500);
  } catch (error) {
    statusEl.textContent = "Error";
    setUploadSummary(error.message);
    runEnrichment.disabled = false;
  }
}

function renderProgress(payload) {
  const percent = Math.max(0, Math.min(100, Number(payload.percent || 0)));
  uploadSummary.innerHTML = `
    <div class="progress-copy">${escapeHtml(payload.phase || payload.status || "Working")}</div>
    <div class="progress-track">
      <div class="progress-fill" style="width: ${percent}%"></div>
    </div>
    <div class="progress-copy">${percent.toFixed(0)}%</div>
  `;
}

function renderFileSummary(filename, rowCount) {
  uploadSummary.innerHTML = `
    <div class="file-summary">
      <span class="file-label">Selected CSV</span>
      <strong>${escapeHtml(filename)}</strong>
      <span>${formatInteger(rowCount)} preview rows loaded</span>
    </div>
  `;
}

function populateColumnSelectors(columns) {
  const options = columns.map((column) => `<option value="${escapeHtml(column)}">${escapeHtml(column)}</option>`).join("");
  latColumn.innerHTML = options;
  lonColumn.innerHTML = options;

  const latGuess = guessColumn(columns, ["lat", "latitude", "y"]);
  const lonGuess = guessColumn(columns, ["lon", "lng", "longitude", "x"]);
  if (latGuess) latColumn.value = latGuess;
  if (lonGuess) lonColumn.value = lonGuess;
}

function guessColumn(columns, candidates) {
  const normalized = columns.map((column) => [
    column,
    column.toLowerCase().replaceAll(/[^a-z0-9]/g, "")
  ]);

  for (const candidate of candidates) {
    const exact = normalized.find(([, cleaned]) => cleaned === candidate);
    if (exact) return exact[0];
  }
  for (const candidate of candidates) {
    const partial = normalized.find(([, cleaned]) => cleaned.includes(candidate));
    if (partial) return partial[0];
  }
  return null;
}

function renderPreview(columns, rows) {
  previewTable.classList.remove("hidden");
  const header = columns.map((column) => `<th>${escapeHtml(column)}</th>`).join("");
  const body = rows.map((row) => `
    <tr>${columns.map((column) => `<td>${escapeHtml(row[column] ?? "")}</td>`).join("")}</tr>
  `).join("");

  previewTable.innerHTML = `
    <table>
      <thead><tr>${header}</tr></thead>
      <tbody>${body}</tbody>
    </table>
  `;
}

function renderSummary(summary) {
  setUploadSummary(`
    Total: ${formatInteger(summary.total_rows)}
    · Valid coords: ${formatInteger(summary.valid_coordinate_rows)}
    · Inside: ${formatInteger(summary.inside_polygon_matches)}
    · Nearest: ${formatInteger(summary.nearest_matches)}
    · No match: ${formatInteger(summary.no_matches)}
  `);
}

function renderStats(summary) {
  statsPanel.classList.remove("hidden");
  const total = Number(summary.total_rows || 0);
  const overviewRows = [
    ["Total rows", summary.total_rows],
    ["Valid coordinates", summary.valid_coordinate_rows],
    ["Inside polygon", summary.inside_polygon_matches],
    ["Nearest matches", summary.nearest_matches],
    ["No match", summary.no_matches],
    ["Avg nearest distance", summary.average_nearest_distance_m == null
      ? "n/a"
      : `${Number(summary.average_nearest_distance_m).toFixed(1)} m`, null]
  ];

  statsGrid.innerHTML = `
    ${renderStatsTable("Match Summary", overviewRows, total)}
    ${renderDistributionTable("Detailed Occupancy", summary.detailed_occupancy || summary.occupancy_raw || [])}
    ${renderDistributionTable("Occupancy Group", summary.occupancy_group || [])}
  `;
}

function renderStatsTable(title, rows, total) {
  return `
    <section class="stats-table">
      <h3>${escapeHtml(title)}</h3>
      <table>
        <thead><tr><th>Metric</th><th>Count</th><th>Share</th></tr></thead>
        <tbody>
          ${rows.map(([label, value, customShare]) => {
            const share = customShare === null ? "" : formatShare(Number(value || 0), total);
            return `
              <tr>
                <td>${escapeHtml(label)}</td>
                <td>${escapeHtml(formatStatValue(value))}</td>
                <td>${escapeHtml(share)}</td>
              </tr>
            `;
          }).join("")}
        </tbody>
      </table>
    </section>
  `;
}

function renderDistributionTable(title, rows) {
  const total = rows.reduce((sum, row) => sum + Number(row.count || 0), 0);
  return `
    <section class="stats-table">
      <h3>${escapeHtml(title)}</h3>
      <table>
        <thead><tr><th>Name</th><th>Count</th><th>Share</th></tr></thead>
        <tbody>
          ${rows.length ? rows.map((row) => `
            <tr>
              <td>${escapeHtml(row.name)}</td>
              <td>${escapeHtml(formatInteger(row.count))}</td>
              <td>${escapeHtml(formatShare(row.count, total))}</td>
            </tr>
          `).join("") : `<tr><td colspan="3">No data</td></tr>`}
        </tbody>
      </table>
    </section>
  `;
}

function formatShare(value, total) {
  if (!total) return "0.0%";
  return `${((Number(value || 0) / total) * 100).toFixed(1)}%`;
}

function formatStatValue(value) {
  return typeof value === "number" ? formatInteger(value) : value;
}

function setUploadSummary(message) {
  uploadSummary.textContent = message;
}

function formatInteger(value) {
  return Number(value || 0).toLocaleString();
}

function clearSelection() {
  selectedFeature = null;
  updateDeckLayers();
  detailsEl.classList.add("hidden");
  emptyEl.classList.remove("hidden");
}

function labelForMatch(matchType, confidence) {
  if (matchType === "inside_polygon") return "Inside";
  if (matchType === "nearest_polygon" || matchType === "nearest") return `Nearest · ${confidence}`;
  return "None";
}

function formatNumber(value, suffix) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return null;
  return `${Number(value).toLocaleString(undefined, {maximumFractionDigits: 1})}${suffix}`;
}

function formatPercent(value) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return null;
  return `${Math.round(Number(value) * 100)}%`;
}

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

const boundaryFile = document.getElementById("boundaryFile");
const boundaryFileName = document.getElementById("boundaryFileName");
const etlOutputDir = document.getElementById("etlOutputDir");
const browseOutputDir = document.getElementById("browseOutputDir");
const etlOutputParquet = document.getElementById("etlOutputParquet");
const etlDuckdbFile = document.getElementById("etlDuckdbFile");
const etlLookupDbFile = document.getElementById("etlLookupDbFile");
const runEtlBtn = document.getElementById("runEtl");
const etlStatusEl = document.getElementById("etlStatus");

boundaryFile.addEventListener("change", () => {
  if (boundaryFile.files.length) {
    boundaryFileName.textContent = boundaryFile.files[0].name;
    boundaryFileName.classList.remove("hidden");
  } else {
    boundaryFileName.classList.add("hidden");
  }
});

function updateEtlOutputPlaceholders() {
  const dir = etlOutputDir.value.trim() || "./etl_output";
  if (!etlOutputParquet.dataset.userEdited) etlOutputParquet.placeholder = `${dir}/buildings_cleaned.parquet`;
  if (!etlDuckdbFile.dataset.userEdited) etlDuckdbFile.placeholder = `${dir}/work_obm.duckdb`;
  if (!etlLookupDbFile.dataset.userEdited) etlLookupDbFile.placeholder = "OBM_BUILDINGS";
}

etlOutputDir.addEventListener("input", updateEtlOutputPlaceholders);
etlOutputParquet.addEventListener("input", () => { etlOutputParquet.dataset.userEdited = "1"; });
etlDuckdbFile.addEventListener("input", () => { etlDuckdbFile.dataset.userEdited = "1"; });
etlLookupDbFile.addEventListener("input", () => { etlLookupDbFile.dataset.userEdited = "1"; });
updateEtlOutputPlaceholders();
if (browseOutputDir) browseOutputDir.classList.add("hidden");

runEtlBtn.addEventListener("click", async () => {
  runEtlBtn.disabled = true;
  statusEl.textContent = "ETL running";
  showEtlStatus("info", "Submitting ETL and Snowflake load job...");

  const formData = new FormData();
  if (boundaryFile.files.length) formData.append("boundary_file", boundaryFile.files[0]);

  const dir = etlOutputDir.value.trim() || "./etl_output";
  formData.append("output_dir", dir);
  formData.append("output_parquet", etlOutputParquet.value.trim() || `${dir}/buildings_cleaned.parquet`);
  formData.append("duckdb_file", etlDuckdbFile.value.trim() || `${dir}/work_obm.duckdb`);
  formData.append("snowflake_table", etlLookupDbFile.value.trim() || "OBM_BUILDINGS");
  formData.append("lon_min", document.getElementById("etlLonMin").value);
  formData.append("lon_max", document.getElementById("etlLonMax").value);
  formData.append("lat_min", document.getElementById("etlLatMin").value);
  formData.append("lat_max", document.getElementById("etlLatMax").value);

  try {
    const response = await fetch("/api/etl/create-database", {
      method: "POST",
      body: formData
    });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || "ETL submission failed");
    pollEtlProgress(payload.job_id);
  } catch (error) {
    statusEl.textContent = "Error";
    showEtlStatus("error", error.message);
    runEtlBtn.disabled = false;
  }
});

async function pollEtlProgress(jobId) {
  try {
    const response = await fetch(`/api/etl/progress/${jobId}`);
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || "Could not read ETL progress");

    const percent = Math.max(0, Math.min(100, Number(payload.percent || 0)));
    showEtlStatus("info", `
      <div class="progress-copy">${escapeHtml(payload.phase || payload.status || "Working")}</div>
      <div class="progress-track"><div class="progress-fill" style="width:${percent}%"></div></div>
      <div class="progress-copy">${percent.toFixed(0)}%</div>
    `);

    if (payload.status === "complete") {
      statusEl.textContent = "Done";
      showEtlStatus("success", `
        <strong>Snowflake table created successfully.</strong><br>
        Parquet: <code>${escapeHtml(payload.output_parquet || "")}</code><br>
        ETL work file: <code>${escapeHtml(payload.duckdb_file || "")}</code><br>
        Snowflake table: <code>${escapeHtml(payload.snowflake_table || "")}</code>
      `);
      await loadDataSources();
      await loadCartoLayer();
      runEtlBtn.disabled = false;
      return;
    }

    if (payload.status === "error") throw new Error(payload.error || "ETL failed");
    window.setTimeout(() => pollEtlProgress(jobId), 3000);
  } catch (error) {
    statusEl.textContent = "Error";
    showEtlStatus("error", error.message);
    runEtlBtn.disabled = false;
  }
}

function showEtlStatus(type, html) {
  etlStatusEl.classList.remove("hidden", "etl-status--error", "etl-status--success");
  if (type === "error") etlStatusEl.classList.add("etl-status--error");
  if (type === "success") etlStatusEl.classList.add("etl-status--success");
  etlStatusEl.innerHTML = html;
}
