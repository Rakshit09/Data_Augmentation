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
const activeDbPath = document.getElementById("activeDbPath");
const dbFileOptions = document.getElementById("dbFileOptions");
const browseDb = document.getElementById("browseDb");
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
const filterViewColumn = document.getElementById("filterViewColumn");
const filterViewValue = document.getElementById("filterViewValue");
const filterViewColor = document.getElementById("filterViewColor");
const applyViewFilter = document.getElementById("applyViewFilter");
const filterViewMessage = document.getElementById("filterViewMessage");
const uploadForm = document.getElementById("uploadForm");
const csvFile = document.getElementById("csvFile");
const csvDropzoneTitle = document.getElementById("csvDropzoneTitle");
const csvDropzoneSubtitle = document.getElementById("csvDropzoneSubtitle");
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
const criticalNote = document.getElementById("criticalNote");
const criticalNoteClose = document.getElementById("criticalNoteClose");

let currentUploadId = null;
let currentUploadFilename = null;
let currentStatsDownloadUrl = null;
let availableDbFiles = [];
let selectedBuilding = null;
let activeViewFilter = null;
let viewFilterRequestId = 0;

const statsDownloadLink = ensureStatsDownloadLink();
const emptyFeatureCollection = {
  type: "FeatureCollection",
  features: []
};

function dismissCriticalNote() {
  criticalNote?.classList.add("hidden");
}

criticalNoteClose?.addEventListener("click", dismissCriticalNote);

function ensureStatsDownloadLink() {
  if (!statsPanel || !statsGrid) return null;

  let link = document.getElementById("statsDownloadLink");
  if (!link) {
    link = document.createElement("a");
    link.id = "statsDownloadLink";
    link.className = "download stats-download hidden";
    link.href = "#";
    link.textContent = "Download stats CSV";

    const statsHeading = statsPanel.querySelector("h2");
    if (statsHeading) {
      statsHeading.insertAdjacentElement("afterend", link);
    } else {
      statsGrid.before(link);
    }
  }

  return link;
}

function baseFilename(filename, fallback = "exposure") {
  const raw = String(filename || "").trim();
  if (!raw) return fallback;
  const parts = raw.split(".");
  if (parts.length > 1) parts.pop();
  return parts.join(".") || fallback;
}

function makeEnrichedCsvFilename(filename) {
  return `${baseFilename(filename)}_enriched.csv`;
}

function makeStatsCsvFilename(filename) {
  return `${baseFilename(filename)}_enriched_stats.csv`;
}

function releaseStatsDownload() {
  if (currentStatsDownloadUrl) {
    URL.revokeObjectURL(currentStatsDownloadUrl);
    currentStatsDownloadUrl = null;
  }

  if (statsDownloadLink) {
    statsDownloadLink.classList.add("hidden");
    statsDownloadLink.removeAttribute("href");
    statsDownloadLink.removeAttribute("download");
  }
}

function csvCell(value) {
  const text = value == null ? "" : String(value);
  return /[",\r\n]/.test(text) ? `"${text.replace(/"/g, '""')}"` : text;
}

function buildStatsCsv(summary) {
  const total = Number(summary.total_rows || 0);
  const lines = [["Section", "Name", "Count", "Share"]];
  const overviewRows = [
    ["Total rows", summary.total_rows],
    ["Valid coordinates", summary.valid_coordinate_rows],
    ["Inside polygon", summary.inside_polygon_matches],
    ["Nearest matches", summary.nearest_matches],
    ["No match", summary.no_matches],
    ["Elapsed", summary.enrichment_elapsed_seconds == null
      ? "n/a"
      : `${Number(summary.enrichment_elapsed_seconds).toFixed(1)} s`, null],
    ["DuckDB threads", summary.engine_threads ?? "n/a", null],
    ["Lookup prefix", summary.lookup_quadkey_prefix_column
      ? `${summary.lookup_quadkey_prefix_column} (z${summary.lookup_quadkey_prefix_zoom})`
      : "n/a", null],
    ["Mode", summary.enrichment_mode || "n/a", null],
    ["Avg nearest distance", summary.average_nearest_distance_m == null
      ? "n/a"
      : `${Number(summary.average_nearest_distance_m).toFixed(1)} m`, null]
  ];

  overviewRows.forEach(([label, value, customShare]) => {
    const share = customShare === null ? "" : formatShare(Number(value || 0), total);
    lines.push(["Match Summary", label, formatStatValue(value), share]);
  });

  const appendDistribution = (section, rows) => {
    const sectionTotal = rows.reduce((sum, row) => sum + Number(row.count || 0), 0);
    rows.forEach((row) => {
      lines.push([
        section,
        row.name,
        formatInteger(row.count),
        formatShare(row.count, sectionTotal)
      ]);
    });

    if (!rows.length) {
      lines.push([section, "No data", "", ""]);
    }
  };

  appendDistribution("Detailed Occupancy", summary.detailed_occupancy || summary.occupancy_raw || []);
  appendDistribution("Occupancy Group", summary.occupancy_group || []);

  return lines.map((row) => row.map(csvCell).join(",")).join("\r\n");
}

function updateStatsDownload(summary) {
  if (!statsDownloadLink) return;

  releaseStatsDownload();
  const csv = buildStatsCsv(summary);
  currentStatsDownloadUrl = URL.createObjectURL(new Blob([csv], { type: "text/csv;charset=utf-8;" }));
  statsDownloadLink.href = currentStatsDownloadUrl;
  statsDownloadLink.download = makeStatsCsvFilename(currentUploadFilename);
  statsDownloadLink.classList.remove("hidden");
}

const selectedSource = emptyFeatureCollection;

const map = new maplibregl.Map({
  container: "map",
  style: {
    version: 8,
    sources: {
      osm: {
        type: "raster",
        tiles: [
          "https://tile.openstreetmap.org/{z}/{x}/{y}.png"
        ],
        tileSize: 256,
        maxzoom: 19,
        attribution: "© OpenStreetMap contributors"
      }
    },
    layers: [
      {
        id: "osm",
        type: "raster",
        source: "osm"
      }
    ]
  },
  center: [10.45, 51.16],
  zoom: 5.4,
  maxZoom: 20
});

map.addControl(new maplibregl.NavigationControl({ visualizePitch: true }), "top-left");

lookupTab.addEventListener("click", () => switchMode("lookup"));
exposureTab.addEventListener("click", () => switchMode("exposure"));
etlTab.addEventListener("click", () => switchMode("etl"));
refreshSources.addEventListener("click", () => {
  dismissCriticalNote();
  loadDataSources();
});
applyDataSource.addEventListener("click", () => {
  dismissCriticalNote();
  applySelectedDataSource();
});
browseDb.addEventListener("click", () => {
  dismissCriticalNote();
  browseDbFile();
});

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

  modeEyebrow.textContent = isLookup ? "Germany" : " ";
  modeTitle.textContent = isLookup ? "Building Lookup"
    : isExposure ? "Enrich Exposure"
    : "Create Lookup Database";

  if (isLookup) {
    window.setTimeout(() => map.resize(), 50);
  }
}

async function loadDataSources() {
  setDataSourceMessage("Scanning local files...");
  refreshSources.disabled = true;

  try {
    const response = await fetch("api/data-source");
    const payload = await response.json();

    if (!response.ok) {
      throw new Error(payload.error || "Could not load data source files");
    }

    activeDbPath.value = payload.db_path || "";
    availableDbFiles = payload.db_files || [];
    renderFileOptions(dbFileOptions, availableDbFiles);
    renderFilePicker(dbPicker, availableDbFiles, activeDbPath);
    await window.buildingInfoFields?.load();
    await window.exposureEnrichmentFields?.load();
    await loadViewFilterFields();
    clearViewFilter("Choose a column and value to color polygons in the current view.");
    setDataSourceMessage("Choose a local DuckDB lookup database.", "success");
  } catch (error) {
    setDataSourceMessage(error.message, "error");
  } finally {
    refreshSources.disabled = false;
  }
}

function renderFileOptions(listEl, files) {
  listEl.innerHTML = files
    .map((path) => `<option value="${escapeHtml(path)}"></option>`)
    .join("");
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

function toggleDbFilePicker() {
  if (!availableDbFiles.length) {
    setDataSourceMessage("No matching local files found. Press Refresh after creating files.", "error");
  }
  dbPicker.classList.toggle("hidden");
}

async function browseDbFile() {
  browseDb.disabled = true;
  setDataSourceMessage("Opening DuckDB file picker...");

  try {
    const response = await fetch("api/browse-file?kind=db");
    const payload = await response.json();

    if (!response.ok) {
      throw new Error(payload.error || "Could not open file picker");
    }

    if (payload.cancelled) {
      setDataSourceMessage("File selection cancelled.");
      return;
    }

    activeDbPath.value = payload.path || "";
    dbPicker.classList.add("hidden");
    setDataSourceMessage("Database selected. Press Use selected database.", "success");
  } catch (error) {
    setDataSourceMessage(`${error.message} Showing the local file list instead.`, "error");
    toggleDbFilePicker();
  } finally {
    browseDb.disabled = false;
  }
}

async function applySelectedDataSource() {
  applyDataSource.disabled = true;
  setDataSourceMessage("Applying selected files...");

  try {
    const response = await fetch("api/data-source", {
      method: "POST",
      headers: {
        "Content-Type": "application/json"
      },
      body: JSON.stringify({
        db_path: activeDbPath.value.trim()
      })
    });
    const payload = await response.json();

    if (!response.ok) {
      throw new Error(payload.error || "Could not apply data source");
    }

    activeDbPath.value = payload.db_path || "";
    clearSelection();
    await window.buildingInfoFields?.load();
    await window.exposureEnrichmentFields?.load();
    await loadViewFilterFields();
    clearViewFilter("Choose a column and value to color polygons in the current view.");
    setDataSourceMessage("Active lookup database updated.", "success");
    statusEl.textContent = "Ready";
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

loadDataSources();

map.on("load", () => {
  map.addSource("view-filter-buildings", {
    type: "geojson",
    data: emptyFeatureCollection
  });

  map.addLayer({
    id: "view-filter-buildings-fill",
    type: "fill",
    source: "view-filter-buildings",
    paint: {
      "fill-color": filterViewColor?.value || "#ff6b6b",
      "fill-opacity": 0.36
    }
  });

  map.addLayer({
    id: "view-filter-buildings-outline",
    type: "line",
    source: "view-filter-buildings",
    paint: {
      "line-color": filterViewColor?.value || "#ff6b6b",
      "line-width": 1.2,
      "line-opacity": 0.8
    }
  });

  map.addSource("selected-building", {
    type: "geojson",
    data: selectedSource
  });

  map.addLayer({
    id: "selected-building-fill",
    type: "fill",
    source: "selected-building",
    paint: {
      "fill-color": "#ffb703",
      "fill-opacity": 0.42
    }
  });

  map.addLayer({
    id: "selected-building-outline",
    type: "line",
    source: "selected-building",
    paint: {
      "line-color": "#c1121f",
      "line-width": 3
    }
  });
});

map.on("moveend", () => {
  if (activeViewFilter) {
    refreshViewFilter({ silent: true });
  }
});

map.on("click", async (event) => {
  const { lng, lat } = event.lngLat;
  dismissCriticalNote();
  hideSearchResults();
  statusEl.textContent = "Searching";

  try {
    const response = await fetch(`api/building-at?lon=${lng}&lat=${lat}`);
    const payload = await response.json();

    if (!response.ok) {
      throw new Error(payload.hint || payload.error || "Lookup failed");
    }

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
});

function renderBuilding(payload) {
  const building = payload.building;
  selectedBuilding = building;
  const feature = {
    type: "Feature",
    geometry: building.geometry,
    properties: {
      building_id: building.building_id
    }
  };

  map.getSource("selected-building").setData({
    type: "FeatureCollection",
    features: [feature]
  });

  emptyEl.classList.add("hidden");
  detailsEl.classList.remove("hidden");

  matchTypeEl.textContent = labelForMatch(payload.match_type, payload.confidence);
  distanceEl.textContent = payload.distance_m == null
    ? ""
    : `${Number(payload.distance_m).toFixed(1)} m`;
  buildingIdEl.textContent = building.building_id || "Building";

  attributesEl.innerHTML = buildingInfoFields.render(building);
}

window.addEventListener("building-info-fields-change", () => {
  if (selectedBuilding) attributesEl.innerHTML = buildingInfoFields.render(selectedBuilding);
});

searchForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  dismissCriticalNote();

  const query = searchInput.value.trim();
  if (query.length < 3) {
    renderSearchMessage("Enter at least 3 characters.");
    return;
  }

  statusEl.textContent = "Searching";

  try {
    const response = await fetch(`api/search-address?q=${encodeURIComponent(query)}`);
    const payload = await response.json();

    if (!response.ok) {
      throw new Error(payload.error || "Address search failed");
    }

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
    .map((result, index) => `
      <button type="button" data-index="${index}">
        ${escapeHtml(result.label)}
      </button>
    `)
    .join("");

  searchResults.querySelectorAll("button").forEach((button) => {
    button.addEventListener("click", () => {
      const result = results[Number(button.dataset.index)];
      hideSearchResults();
      map.flyTo({
        center: [result.lon, result.lat],
        zoom: 18,
        speed: 1.4
      });
    });
  });
}

function renderSearchMessage(message) {
  searchResults.classList.remove("hidden");
  searchResults.innerHTML = `<p>${escapeHtml(message)}</p>`;
}

function hideSearchResults() {
  searchResults.classList.add("hidden");
  searchResults.innerHTML = "";
}

filterViewColumn?.addEventListener("change", async () => {
  dismissCriticalNote();
  await loadViewFilterValues(filterViewColumn.value);
});

applyViewFilter?.addEventListener("click", async () => {
  dismissCriticalNote();

  const column = filterViewColumn?.value.trim() || "";
  const value = filterViewValue?.value || "";
  const color = filterViewColor?.value || "#ff6b6b";

  if (!column || !value) {
    clearViewFilter("Choose a column and value before applying.", "error");
    return;
  }

  activeViewFilter = { column, value, color };
  await refreshViewFilter();
});

async function loadViewFilterFields() {
  if (!filterViewColumn || !filterViewValue) return;

  filterViewColumn.innerHTML = '<option value="">Loading columns...</option>';
  filterViewValue.innerHTML = '<option value="">Select value</option>';
  filterViewValue.disabled = true;

  try {
    const response = await fetch("api/building-fields");
    const payload = await response.json();

    if (!response.ok) {
      throw new Error(payload.error || "Could not load filter fields");
    }

    const fields = Array.isArray(payload.filter_fields)
      ? payload.filter_fields
      : (payload.fields || []);
    renderViewFilterFieldOptions(fields);
  } catch (error) {
    renderViewFilterFieldOptions([]);
    setFilterViewMessage(error.message, "error");
  }
}

function renderViewFilterFieldOptions(fields) {
  if (!filterViewColumn) return;

  filterViewColumn.innerHTML = [
    '<option value="">Select column</option>',
    ...fields.map((field) => `<option value="${escapeHtml(field)}">${escapeHtml(formatFieldLabel(field))}</option>`)
  ].join("");

  renderViewFilterValueOptions([]);
}

async function loadViewFilterValues(column) {
  if (!filterViewValue) return;

  if (!column) {
    renderViewFilterValueOptions([]);
    setFilterViewMessage("Choose a column and value to color polygons in the current view.");
    return;
  }

  filterViewValue.disabled = true;
  filterViewValue.innerHTML = '<option value="">Loading values...</option>';

  try {
    const response = await fetch(`api/building-filter-values?column=${encodeURIComponent(column)}`);
    const payload = await response.json();

    if (!response.ok) {
      throw new Error(payload.error || "Could not load filter values");
    }

    renderViewFilterValueOptions(payload.values || []);
    setFilterViewMessage("Choose a value and color, then apply the filter.");
  } catch (error) {
    renderViewFilterValueOptions([]);
    setFilterViewMessage(error.message, "error");
  }
}

function renderViewFilterValueOptions(values) {
  if (!filterViewValue) return;

  filterViewValue.innerHTML = [
    '<option value="">Select value</option>',
    ...values.map((value) => `<option value="${escapeHtml(value)}">${escapeHtml(value)}</option>`)
  ].join("");
  filterViewValue.disabled = values.length === 0;
}

async function refreshViewFilter({ silent = false } = {}) {
  if (!activeViewFilter) return;
  if (!map.isStyleLoaded()) return;

  const requestId = ++viewFilterRequestId;
  const bounds = map.getBounds();
  const params = new URLSearchParams({
    min_lon: String(bounds.getWest()),
    min_lat: String(bounds.getSouth()),
    max_lon: String(bounds.getEast()),
    max_lat: String(bounds.getNorth()),
    column: activeViewFilter.column,
    value: activeViewFilter.value
  });

  if (!silent) {
    statusEl.textContent = "Filtering";
    setFilterViewMessage("Loading matching footprints in the current view...");
  }

  try {
    const response = await fetch(`api/building-footprints?${params.toString()}`);
    const payload = await response.json();

    if (requestId !== viewFilterRequestId) return;
    if (!response.ok) {
      throw new Error(payload.error || "Could not load building footprints");
    }

    setViewFilterLayerColor(activeViewFilter.color);
    map.getSource("view-filter-buildings")?.setData(payload);

    const matched = Number(payload.count || (payload.features || []).length || 0);
    setFilterViewMessage(`${formatInteger(matched)} polygons matched in the current view.`, matched ? "success" : "");
    if (!silent) {
      statusEl.textContent = "Ready";
    }
  } catch (error) {
    if (requestId !== viewFilterRequestId) return;

    clearViewFilterLayer();
    setFilterViewMessage(error.message, "error");
    if (!silent) {
      statusEl.textContent = "Error";
    }
  }
}

function clearViewFilter(message = "", type = "") {
  activeViewFilter = null;
  viewFilterRequestId += 1;
  clearViewFilterLayer();
  if (filterViewColumn) filterViewColumn.value = "";
  renderViewFilterValueOptions([]);
  setFilterViewMessage(message, type);
}

function clearViewFilterLayer() {
  map.getSource("view-filter-buildings")?.setData(emptyFeatureCollection);
}

function setViewFilterLayerColor(color) {
  map.setPaintProperty("view-filter-buildings-fill", "fill-color", color);
  map.setPaintProperty("view-filter-buildings-outline", "line-color", color);
}

function setFilterViewMessage(message, type = "") {
  if (!filterViewMessage) return;

  filterViewMessage.textContent = message;
  filterViewMessage.classList.toggle("error", type === "error");
  filterViewMessage.classList.toggle("success", type === "success");
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
    setUploadedCsvName("");
    setUploadSummary("Choose a CSV or Excel (.xlsx) file first.");
    return;
  }

  dismissCriticalNote();

  const formData = new FormData();
  formData.append("file", csvFile.files[0]);
  setUploadedCsvName(csvFile.files[0].name);

  statusEl.textContent = "Uploading";
  setUploadSummary("Reading file preview...");
  downloadLink.classList.add("hidden");

  try {
    const response = await fetch("api/exposure/preview", {
      method: "POST",
      body: formData
    });
    const payload = await response.json();

    if (!response.ok) {
      throw new Error(payload.error || "Upload failed");
    }

    currentUploadId = payload.upload_id;
    currentUploadFilename = payload.filename;
  setUploadedCsvName(payload.filename);
    populateColumnSelectors(payload.columns);
    renderPreview(payload.columns, payload.rows);
    mappingControls.classList.remove("hidden");
    statsPanel.classList.add("hidden");
    releaseStatsDownload();
    renderFileSummary(payload.filename, payload.rows.length);
    statusEl.textContent = "Ready";
  } catch (error) {
    statusEl.textContent = "Error";
    setUploadSummary(error.message);
    previewTable.classList.add("hidden");
  }
}

runEnrichment.addEventListener("click", async () => {
  dismissCriticalNote();

  if (!currentUploadId) {
    setUploadSummary("Upload a file first.");
    return;
  }

  runEnrichment.disabled = true;
  statusEl.textContent = "Enriching";
  setUploadSummary("Running batch spatial enrichment...");
  downloadLink.classList.add("hidden");
  statsPanel.classList.add("hidden");
  releaseStatsDownload();

  try {
    const response = await fetch("api/exposure/enrich", {
      method: "POST",
      headers: {
        "Content-Type": "application/json"
      },
      body: JSON.stringify({
        upload_id: currentUploadId,
        lat_col: latColumn.value,
        lon_col: lonColumn.value,
        mode: matchMode.value,
        max_distance_m: Number(maxDistance.value || 50),
        appended_fields: window.exposureEnrichmentFields?.selected() || []
      })
    });
    const payload = await response.json();

    if (!response.ok) {
      throw new Error(payload.error || "Enrichment failed");
    }

    pollEnrichmentProgress(payload.job_id);
  } catch (error) {
    statusEl.textContent = "Error";
    setUploadSummary(error.message);
    runEnrichment.disabled = false;
  }
});

async function pollEnrichmentProgress(jobId) {
  try {
    const response = await fetch(`api/exposure/progress/${jobId}`);
    const payload = await response.json();

    if (!response.ok) {
      throw new Error(payload.error || "Could not read progress");
    }

    renderProgress(payload);

    if (payload.status === "complete") {
      downloadLink.href = payload.download_url;
      downloadLink.download = payload.download_name || makeEnrichedCsvFilename(currentUploadFilename);
      downloadLink.classList.remove("hidden");
      renderSummary(payload.summary);
      renderStats(payload.summary);
      updateStatsDownload(payload.summary);
      statusEl.textContent = "Done";
      runEnrichment.disabled = false;
      return;
    }

    if (payload.status === "error") {
      throw new Error(payload.error || "Enrichment failed");
    }

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
      <span>${formatInteger(rowCount)} preview rows loaded</span>
    </div>
  `;
}

function populateColumnSelectors(columns) {
  const options = columns
    .map((column) => `<option value="${escapeHtml(column)}">${escapeHtml(column)}</option>`)
    .join("");

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

  const header = columns
    .map((column) => `<th>${escapeHtml(column)}</th>`)
    .join("");
  const body = rows
    .map((row) => `
      <tr>
        ${columns.map((column) => `<td>${escapeHtml(row[column] ?? "")}</td>`).join("")}
      </tr>
    `)
    .join("");

  previewTable.innerHTML = `
    <table>
      <thead><tr>${header}</tr></thead>
      <tbody>${body}</tbody>
    </table>
  `;
}

function renderSummary(summary) {
  const elapsed = summary.enrichment_elapsed_seconds == null
    ? ""
    : ` · Elapsed: ${Number(summary.enrichment_elapsed_seconds).toFixed(1)} s`;
  setUploadSummary(`
    Total: ${formatInteger(summary.total_rows)}
    · Valid coords: ${formatInteger(summary.valid_coordinate_rows)}
    · Inside: ${formatInteger(summary.inside_polygon_matches)}
    · Nearest: ${formatInteger(summary.nearest_matches)}
    · No match: ${formatInteger(summary.no_matches)}
    ${elapsed}
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
    ["Elapsed", summary.enrichment_elapsed_seconds == null
      ? "n/a"
      : `${Number(summary.enrichment_elapsed_seconds).toFixed(1)} s`, null],
    ["DuckDB threads", summary.engine_threads ?? "n/a", null],
    ["Lookup prefix", summary.lookup_quadkey_prefix_column
      ? `${summary.lookup_quadkey_prefix_column} (z${summary.lookup_quadkey_prefix_zoom})`
      : "n/a", null],
    ["Mode", summary.enrichment_mode || "n/a", null],
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
        <thead>
          <tr><th>Metric</th><th>Count</th><th>Share</th></tr>
        </thead>
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
        <thead>
          <tr><th>Name</th><th>Count</th><th>Share</th></tr>
        </thead>
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
  if (typeof value === "number") return formatInteger(value);
  return value;
}

function setUploadSummary(message) {
  uploadSummary.textContent = message;
}

function setUploadedCsvName(filename) {
  if (csvDropzoneTitle) {
    csvDropzoneTitle.textContent = filename || "Choose Exposure File";
  }

  if (csvDropzoneSubtitle) {
    csvDropzoneSubtitle.textContent = filename
      ? "Click to choose a different file"
      : "CSV or Excel (.xlsx) with latitude, longitude, and other columns";
  }
}

function formatInteger(value) {
  return Number(value || 0).toLocaleString();
}

function clearSelection() {
  selectedBuilding = null;
  map.getSource("selected-building")?.setData(selectedSource);
  detailsEl.classList.add("hidden");
  emptyEl.classList.remove("hidden");
}

function labelForMatch(matchType, confidence) {
  if (matchType === "inside_polygon") return "Inside";
  if (matchType === "nearest") return `Nearest · ${confidence}`;
  return "None";
}

function formatNumber(value, suffix) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return null;
  return `${Number(value).toLocaleString(undefined, { maximumFractionDigits: 1 })}${suffix}`;
}

function formatPercent(value) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return null;
  return `${Math.round(Number(value) * 100)}%`;
}

function formatFieldLabel(field) {
  return String(field)
    .replaceAll("_", " ")
    .replace(/\b\w/g, (character) => character.toUpperCase())
    .replace(/\bM2\b/g, "m2")
    .replace(/\bObm\b/g, "OBM")
    .replace(/\bId\b/g, "ID");
}

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

// -----------------------------------------------------------------------
// ETL: Create OBM Database
// -----------------------------------------------------------------------
const boundaryFile = document.getElementById("boundaryFile");
const boundaryFileName = document.getElementById("boundaryFileName");
const etlOutputDir = document.getElementById("etlOutputDir");
const browseOutputDir = document.getElementById("browseOutputDir");
const etlOutputParquet = document.getElementById("etlOutputParquet");
const etlLookupDbFile = document.getElementById("etlLookupDbFile");
const runEtlBtn = document.getElementById("runEtl");
const etlStatusEl = document.getElementById("etlStatus");
const etlWorkflowToggle = document.getElementById("etlWorkflowToggle");
const etlWorkflowBody = document.getElementById("etlWorkflowBody");
const customParquetToggle = document.getElementById("customParquetToggle");
const customParquetBody = document.getElementById("customParquetBody");

function setExpandedEtlWorkflow(workflow) {
  const showCreate = workflow === "create";
  const showCustom = workflow === "custom";

  etlWorkflowToggle.setAttribute("aria-expanded", String(showCreate));
  customParquetToggle.setAttribute("aria-expanded", String(showCustom));
  etlWorkflowBody.classList.toggle("hidden", !showCreate);
  customParquetBody.classList.toggle("hidden", !showCustom);
}

etlWorkflowToggle.addEventListener("click", () => {
  dismissCriticalNote();
  const isExpanded = etlWorkflowToggle.getAttribute("aria-expanded") === "true";
  setExpandedEtlWorkflow(isExpanded ? null : "create");
});

customParquetToggle.addEventListener("click", () => {
  dismissCriticalNote();
  const isExpanded = customParquetToggle.getAttribute("aria-expanded") === "true";
  setExpandedEtlWorkflow(isExpanded ? null : "custom");
});

setExpandedEtlWorkflow(null);

boundaryFile.addEventListener("change", () => {
  if (boundaryFile.files.length) {
    dismissCriticalNote();
  }

  if (boundaryFile.files.length) {
    boundaryFileName.textContent = boundaryFile.files[0].name;
    boundaryFileName.classList.remove("hidden");
  } else {
    boundaryFileName.classList.add("hidden");
  }
});

function updateEtlOutputPlaceholders() {
  const dir = etlOutputDir.value.trim() || "./etl_output";
  if (!etlOutputParquet.dataset.userEdited) {
    etlOutputParquet.placeholder = `${dir}/buildings_cleaned.parquet`;
  }
  if (!etlLookupDbFile.dataset.userEdited) {
    etlLookupDbFile.placeholder = `${dir}/building_lookup.duckdb`;
  }
}

// Auto-fill Parquet / DuckDB paths when output dir changes
etlOutputDir.addEventListener("input", updateEtlOutputPlaceholders);

etlOutputParquet.addEventListener("input", () => { etlOutputParquet.dataset.userEdited = "1"; });
etlLookupDbFile.addEventListener("input", () => { etlLookupDbFile.dataset.userEdited = "1"; });
updateEtlOutputPlaceholders();

browseOutputDir.addEventListener("click", async () => {
  dismissCriticalNote();
  browseOutputDir.disabled = true;
  showEtlStatus("info", "Opening output folder picker...");

  try {
    const response = await fetch("api/browse-folder");
    const payload = await response.json();

    if (!response.ok) {
      throw new Error(payload.error || "Could not open folder picker");
    }

    if (payload.cancelled) {
      showEtlStatus("info", "Folder selection cancelled.");
      return;
    }

    etlOutputDir.value = payload.path || "";
    updateEtlOutputPlaceholders();
    showEtlStatus("info", "Output folder selected.");
  } catch (error) {
    showEtlStatus("error", error.message);
  } finally {
    browseOutputDir.disabled = false;
  }
});

runEtlBtn.addEventListener("click", async () => {
  dismissCriticalNote();
  runEtlBtn.disabled = true;
  statusEl.textContent = "ETL running";
  showEtlStatus("info", "Submitting ETL job...");

  const formData = new FormData();
  if (boundaryFile.files.length) {
    formData.append("boundary_file", boundaryFile.files[0]);
  }

  const dir = etlOutputDir.value.trim() || "./etl_output";
  formData.append("output_dir", dir);
  formData.append("output_parquet", etlOutputParquet.value.trim() || `${dir}/buildings_cleaned.parquet`);
  formData.append("lookup_db_file", etlLookupDbFile.value.trim() || `${dir}/building_lookup.duckdb`);
  try {
    const response = await fetch("api/etl/create-database", {
      method: "POST",
      body: formData
    });
    const payload = await response.json();

    if (!response.ok) {
      throw new Error(payload.error || "ETL submission failed");
    }

    pollEtlProgress(payload.job_id);
  } catch (error) {
    statusEl.textContent = "Error";
    showEtlStatus("error", error.message);
    runEtlBtn.disabled = false;
  }
});

async function pollEtlProgress(jobId) {
  try {
    const response = await fetch(`api/etl/progress/${jobId}`);
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
        <strong>Database created successfully.</strong><br>
        ${formatBoundaryExtent(payload.boundary_extent)}
        Parquet: <code>${escapeHtml(payload.output_parquet || "")}</code><br>
        DuckDB lookup table: <code>${escapeHtml(payload.lookup_db_file || "")}</code>
      `);
      runEtlBtn.disabled = false;
      await loadDataSources();
      return;
    }

    if (payload.status === "error") {
      throw new Error(payload.error || "ETL failed");
    }

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

function formatBoundaryExtent(extent) {
  if (!extent) return "";
  return `Boundary extent: <code>${Number(extent.lon_min).toFixed(4)}, ${Number(extent.lat_min).toFixed(4)} to ${Number(extent.lon_max).toFixed(4)}, ${Number(extent.lat_max).toFixed(4)}</code><br>`;
}
