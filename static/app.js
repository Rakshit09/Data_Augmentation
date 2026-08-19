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
const exposureUploadActions = document.getElementById("exposureUploadActions");
const mappingControls = document.getElementById("mappingControls");
const latColumn = document.getElementById("latColumn");
const lonColumn = document.getElementById("lonColumn");
const matchMode = document.getElementById("matchMode");
const maxDistance = document.getElementById("maxDistance");
const runEnrichment = document.getElementById("runEnrichment");
const uploadSummary = document.getElementById("uploadSummary");
const previewTable = document.getElementById("previewTable");
const exposureMapControls = document.getElementById("exposureMapControls");
const showExposureOnMap = document.getElementById("showExposureOnMap");
const exposureMapMessage = document.getElementById("exposureMapMessage");
const exposureMapPanel = document.getElementById("exposureMapPanel");
const exposureMapTitle = document.getElementById("exposureMapTitle");
const exposureMapStats = document.getElementById("exposureMapStats");
const clearExposureMap = document.getElementById("clearExposureMap");
const downloadLink = document.getElementById("downloadLink");
const statsPanel = document.getElementById("statsPanel");
const statsGrid = document.getElementById("statsGrid");
const criticalNote = document.getElementById("criticalNote");
const criticalNoteClose = document.getElementById("criticalNoteClose");
const clearAllStateButton = document.getElementById("clearAllState");
const clearExposureUpload = document.getElementById("clearExposureUpload");
const etlForm = document.getElementById("etlForm");

let currentUploadId = null;
let currentUploadFilename = null;
let currentUploadColumns = [];
let currentStatsDownloadUrl = null;
let availableDbFiles = [];
let selectedBuilding = null;
let activeViewFilter = null;
let viewFilterRequestId = 0;
let viewFilterTileUrlActive = "";
let viewFilterFetchController = null;
let activeExposureMap = null;
let exposureMapFetchController = null;
let exposureMapRequestId = 0;
let exposureUploadRequestId = 0;
let exposureActivationRequestId = 0;
let enrichmentProgressRequestId = 0;
let etlProgressRequestId = 0;

const defaultEmptyStateHtml = emptyEl?.innerHTML || "<p>Select a building footprint on the map.</p>";
const defaultUploadSummaryText = uploadSummary?.textContent?.trim() || "Upload an exposure to preview";
const defaultFilterViewMessage = "Choose a column and value to color polygons in the current view.";
const defaultMatchModeValue = matchMode?.value || "inside_nearest";
const defaultMaxDistanceValue = maxDistance?.value || "50";

function exposureUploadState() {
  return {
    upload_id: currentUploadId,
    filename: currentUploadFilename,
    columns: [...currentUploadColumns],
    lat_col: latColumn?.value || "",
    lon_col: lonColumn?.value || ""
  };
}

function publishExposureUploadState() {
  window.rasterIntersectionExposureState = exposureUploadState();
  window.dispatchEvent(new CustomEvent("exposure-upload-state-change", {
    detail: window.rasterIntersectionExposureState
  }));
}

window.getExposureUploadState = exposureUploadState;
publishExposureUploadState();
latColumn?.addEventListener("change", publishExposureUploadState);
lonColumn?.addEventListener("change", publishExposureUploadState);

const statsDownloadLink = ensureStatsDownloadLink();
const emptyFeatureCollection = {
  type: "FeatureCollection",
  features: []
};
const viewFilterSourceId = "view-filter-buildings";
const viewFilterFillLayerId = "view-filter-buildings-fill";
const viewFilterOutlineLayerId = "view-filter-buildings-outline";
const viewFilterSourceLayer = "buildings";
const mapElement = document.getElementById("map");
const viewFilterMinZoom = Number(mapElement?.dataset.filterViewMinZoom || 15);
const viewFilterMaxTileZoom = Number(mapElement?.dataset.filterViewMaxTileZoom || 17);
const viewFilterZoomMessage = "Zoom to see building level filter view";
const exposureRawPointZoom = Number(mapElement?.dataset.exposureRawPointZoom || 14.5);

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
const BASEMAPS = {
  osm: {
    label: "OpenStreetMap",
    tiles: ["https://tile.openstreetmap.org/{z}/{x}/{y}.png"],
    attribution: "© OpenStreetMap contributors",
    maxzoom: 19
  },
  cartoLight: {
    label: "Light",
    tiles: ["https://a.basemaps.cartocdn.com/light_all/{z}/{x}/{y}.png"],
    attribution: "© OpenStreetMap contributors © CARTO",
    maxzoom: 20
  },
  cartoDark: {
    label: "Dark",
    tiles: ["https://a.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}.png"],
    attribution: "© OpenStreetMap contributors © CARTO",
    maxzoom: 20
  },
  voyager: {
    label: "Voyager",
    tiles: ["https://a.basemaps.cartocdn.com/rastertiles/voyager/{z}/{x}/{y}.png"],
    attribution: "© OpenStreetMap contributors © CARTO",
    maxzoom: 20
  },
  satellite: {
    label: "Satellite",
    tiles: ["https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}"],
    attribution: "Source: Esri, Maxar, Earthstar Geographics, and the GIS User Community",
    maxzoom: 19
  }
};
const BASEMAP_STORAGE_KEY = "dataAugmentationBasemap";
const OVERLAY_ORDER_STORAGE_KEY = "dataAugmentationOverlayOrder";
const OVERLAY_ORDER_IMPORTED_TOP = "imported-top";
const OVERLAY_ORDER_EXPOSURE_TOP = "exposure-top";
const EXPOSURE_POINT_LAYER_IDS = [
  "exposure-points-halo",
  "exposure-points-circle",
  "exposure-points-count"
];
const IMPORTED_LAYER_IDS = [
  "user-added-vector-fill",
  "user-added-vector-outline",
  "user-added-vector-line",
  "user-added-vector-point",
  "user-added-raster-layer"
];
const defaultBasemapId = getStoredBasemapId();
let overlayLayerOrder = getStoredOverlayLayerOrder();
let overlayOrderButton = null;
let overlayOrderSelect = null;
let exposureRefreshButton = null;
let exposureRefreshBusy = false;

function basemapSourceId(id) {
  return `basemap-source-${id}`;
}

function basemapLayerId(id) {
  return `basemap-layer-${id}`;
}

function getStoredBasemapId() {
  try {
    const stored = window.localStorage?.getItem(BASEMAP_STORAGE_KEY);
    return stored && BASEMAPS[stored] ? stored : "osm";
  } catch (_error) {
    return "osm";
  }
}

function getStoredOverlayLayerOrder() {
  try {
    const stored = window.localStorage?.getItem(OVERLAY_ORDER_STORAGE_KEY);
    return stored === OVERLAY_ORDER_EXPOSURE_TOP ? OVERLAY_ORDER_EXPOSURE_TOP : OVERLAY_ORDER_IMPORTED_TOP;
  } catch (_error) {
    return OVERLAY_ORDER_IMPORTED_TOP;
  }
}

function setBasemap(id) {
  if (!BASEMAPS[id]) return;

  for (const basemapId of Object.keys(BASEMAPS)) {
    const layerId = basemapLayerId(basemapId);
    if (map.getLayer(layerId)) {
      map.setLayoutProperty(layerId, "visibility", basemapId === id ? "visible" : "none");
    }
  }

  try {
    window.localStorage?.setItem(BASEMAP_STORAGE_KEY, id);
  } catch (_error) {
    // Ignore private browsing/storage restrictions.
  }
}

class BasemapControl {
  onAdd(mapInstance) {
    this.map = mapInstance;
    this.container = document.createElement("div");
    this.container.className = "maplibregl-ctrl maplibregl-ctrl-group basemap-control";

    const button = document.createElement("button");
    button.type = "button";
    button.className = "basemap-control-button";
    button.setAttribute("aria-label", "Choose basemap");
    button.setAttribute("title", "Choose basemap");
    button.innerHTML = '<span aria-hidden="true"></span>';

    const select = document.createElement("select");
    select.className = "basemap-control-select";
    select.setAttribute("aria-label", "Basemap type");

    for (const [id, basemap] of Object.entries(BASEMAPS)) {
      const option = document.createElement("option");
      option.value = id;
      option.textContent = basemap.label;
      select.appendChild(option);
    }

    select.value = defaultBasemapId;
    select.addEventListener("change", () => setBasemap(select.value));

    button.addEventListener("click", () => {
      this.container.classList.toggle("open");
      select.focus();
    });
    select.addEventListener("blur", () => {
      window.setTimeout(() => this.container.classList.remove("open"), 120);
    });

    this.container.addEventListener("mousedown", (event) => event.stopPropagation());
    this.container.addEventListener("dblclick", (event) => event.stopPropagation());
    this.container.append(button, select);
    return this.container;
  }

  onRemove() {
    this.container?.parentNode?.removeChild(this.container);
    this.map = undefined;
  }
}

class OverlayOrderControl {
  onAdd(mapInstance) {
    this.map = mapInstance;
    this.container = document.createElement("div");
    this.container.className = "maplibregl-ctrl maplibregl-ctrl-group layer-order-control";

    const button = document.createElement("button");
    button.type = "button";
    button.className = "layer-order-control-button";
    button.innerHTML = '<span aria-hidden="true"></span>';

    const select = document.createElement("select");
    select.className = "layer-order-control-select";
    select.setAttribute("aria-label", "Map layer order");

    const importedOption = document.createElement("option");
    importedOption.value = OVERLAY_ORDER_IMPORTED_TOP;
    importedOption.textContent = "Imports above exposure";
    select.appendChild(importedOption);

    const exposureOption = document.createElement("option");
    exposureOption.value = OVERLAY_ORDER_EXPOSURE_TOP;
    exposureOption.textContent = "Exposure above imports";
    select.appendChild(exposureOption);

    select.addEventListener("change", () => setOverlayLayerOrder(select.value));

    button.addEventListener("click", () => {
      this.container.classList.toggle("open");
      select.focus();
    });
    select.addEventListener("blur", () => {
      window.setTimeout(() => this.container.classList.remove("open"), 120);
    });

    this.container.addEventListener("mousedown", (event) => event.stopPropagation());
    this.container.addEventListener("dblclick", (event) => event.stopPropagation());
    this.container.append(button, select);
    this.button = button;
    this.select = select;
    overlayOrderButton = button;
    overlayOrderSelect = select;
    syncOverlayOrderButton();
    return this.container;
  }

  onRemove() {
    if (overlayOrderButton === this.button) {
      overlayOrderButton = null;
    }
    if (overlayOrderSelect === this.select) {
      overlayOrderSelect = null;
    }
    this.container?.parentNode?.removeChild(this.container);
    this.map = undefined;
  }
}

class ExposureRefreshControl {
  onAdd(mapInstance) {
    this.map = mapInstance;
    this.container = document.createElement("div");
    this.container.className = "maplibregl-ctrl maplibregl-ctrl-group exposure-refresh-control";

    const button = document.createElement("button");
    button.type = "button";
    button.className = "exposure-refresh-control-button";
    button.innerHTML = '<span aria-hidden="true"></span>';
    button.addEventListener("click", () => {
      if (!activeExposureMap || exposureRefreshBusy) return;
      refreshExposurePoints({ manual: true });
    });

    this.container.addEventListener("mousedown", (event) => event.stopPropagation());
    this.container.addEventListener("dblclick", (event) => event.stopPropagation());
    this.container.append(button);
    this.button = button;
    exposureRefreshButton = button;
    syncExposureRefreshControl();
    return this.container;
  }

  onRemove() {
    if (exposureRefreshButton === this.button) {
      exposureRefreshButton = null;
    }
    this.container?.parentNode?.removeChild(this.container);
    this.map = undefined;
  }
}

const map = new maplibregl.Map({
  container: "map",
  style: {
    version: 8,
    glyphs: "https://demotiles.maplibre.org/font/{fontstack}/{range}.pbf",
    sources: Object.fromEntries(Object.entries(BASEMAPS).map(([id, basemap]) => [
      basemapSourceId(id),
      {
        type: "raster",
        tiles: basemap.tiles,
        tileSize: 256,
        maxzoom: basemap.maxzoom || 19,
        attribution: basemap.attribution
      }
    ])),
    layers: Object.keys(BASEMAPS).map((id) => ({
        id: basemapLayerId(id),
        type: "raster",
        source: basemapSourceId(id),
        layout: {
          visibility: id === defaultBasemapId ? "visible" : "none"
        }
      }
    ))
  },
  center: [10.45, 51.16],
  zoom: 5.4,
  maxZoom: 22
});

map.addControl(new maplibregl.NavigationControl({ visualizePitch: true }), "top-left");
map.addControl(new BasemapControl(), "top-left");
map.addControl(new OverlayOrderControl(), "top-left");
map.addControl(new ExposureRefreshControl(), "top-left");

window.getOverlayLayerOrder = () => overlayLayerOrder;
window.applyOverlayLayerOrder = applyOverlayLayerOrder;

lookupTab.addEventListener("click", () => switchMode("lookup"));
exposureTab.addEventListener("click", () => switchMode("exposure"));
etlTab.addEventListener("click", () => switchMode("etl"));
refreshSources.addEventListener("click", () => {
  dismissCriticalNote();
  loadDataSources();
});
clearAllStateButton?.addEventListener("click", () => {
  void clearAllState();
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
  modeTitle.textContent = isLookup ? "Spatial Explorer"
    : isExposure ? "Exposure Analytics"
    : "Create Database";

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
    setDataSourceMessage("Choose a building lookup database.", "success");
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
  map.addSource("exposure-points", {
    type: "geojson",
    data: emptyFeatureCollection,
    buffer: 64
  });

  map.addLayer({
    id: "exposure-points-halo",
    type: "circle",
    source: "exposure-points",
    paint: {
      "circle-color": "#ffffff",
      "circle-opacity": 0.84,
      "circle-radius": [
        "interpolate",
        ["linear"],
        ["coalesce", ["get", "csv_count"], 1],
        1, 5,
        20, 8,
        200, 12
      ],
      "circle-stroke-color": "rgba(0, 31, 63, 0.16)",
      "circle-stroke-width": 1
    }
  });

  map.addLayer({
    id: "exposure-points-circle",
    type: "circle",
    source: "exposure-points",
    paint: {
      "circle-color": "#0f766e",
      "circle-opacity": 0.88,
      "circle-radius": [
        "interpolate",
        ["linear"],
        ["coalesce", ["get", "csv_count"], 1],
        1, 3,
        20, 5,
        200, 8
      ],
      "circle-stroke-color": "#064e3b",
      "circle-stroke-width": 0.5
    }
  });

  map.addLayer({
    id: "exposure-points-count",
    type: "symbol",
    source: "exposure-points",
    minzoom: 11,
    filter: [">", ["coalesce", ["get", "csv_count"], 1], 1],
    layout: {
      "text-field": ["coalesce", ["get", "csv_label"], ["to-string", ["get", "csv_count"]]],
      "text-font": ["Open Sans Bold", "Arial Unicode MS Bold"],
      "text-size": [
        "interpolate",
        ["linear"],
        ["zoom"],
        9, 10,
        14, 12,
        18, 13
      ],
      "text-allow-overlap": false,
      "text-ignore-placement": false
    },
    paint: {
      "text-color": "#063f35",
      "text-halo-color": "rgba(255, 255, 255, 0.92)",
      "text-halo-width": 1.2
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

  applyOverlayLayerOrder();
});

function setOverlayLayerOrder(order) {
  const nextOrder = order === OVERLAY_ORDER_EXPOSURE_TOP ? OVERLAY_ORDER_EXPOSURE_TOP : OVERLAY_ORDER_IMPORTED_TOP;
  overlayLayerOrder = nextOrder;
  syncOverlayOrderButton();
  applyOverlayLayerOrder();
  try {
    window.localStorage?.setItem(OVERLAY_ORDER_STORAGE_KEY, nextOrder);
  } catch (_error) {
    // Ignore private browsing/storage restrictions.
  }
}

function syncOverlayOrderButton() {
  if (!overlayOrderButton) return;
  const exposureTop = overlayLayerOrder === OVERLAY_ORDER_EXPOSURE_TOP;
  if (overlayOrderSelect) {
    overlayOrderSelect.value = overlayLayerOrder;
  }
  overlayOrderButton.classList.toggle("is-exposure-top", exposureTop);
  overlayOrderButton.setAttribute("aria-pressed", exposureTop ? "true" : "false");
  overlayOrderButton.setAttribute(
    "aria-label",
    exposureTop
      ? "Exposure dots are above imported layers. Click to choose a different layer order."
      : "Imported layers are above exposure dots. Click to choose a different layer order."
  );
  overlayOrderButton.setAttribute(
    "title",
    exposureTop
      ? "Layer order: exposure above imports"
      : "Layer order: imports above exposure"
  );
}

function applyOverlayLayerOrder() {
  if (typeof map === "undefined" || !map.isStyleLoaded()) return;

  const exposureLayers = EXPOSURE_POINT_LAYER_IDS.filter((layerId) => map.getLayer(layerId));
  const importedLayers = IMPORTED_LAYER_IDS.filter((layerId) => map.getLayer(layerId));
  if (!exposureLayers.length || !importedLayers.length) return;

  if (overlayLayerOrder === OVERLAY_ORDER_EXPOSURE_TOP) {
    const anchorLayerId = exposureLayers[0];
    for (const layerId of importedLayers) {
      if (layerId !== anchorLayerId && map.getLayer(layerId) && map.getLayer(anchorLayerId)) {
        map.moveLayer(layerId, anchorLayerId);
      }
    }
    return;
  }

  const anchorLayerId = importedLayers[0];
  for (const layerId of exposureLayers) {
    if (layerId !== anchorLayerId && map.getLayer(layerId) && map.getLayer(anchorLayerId)) {
      map.moveLayer(layerId, anchorLayerId);
    }
  }
}

map.on("moveend", () => {
  if (activeViewFilter) {
    refreshViewFilter({ silent: true });
  }
  requestExposurePointRefresh();
});

map.on("click", async (event) => {
  if (event.defaultPrevented) return;

  if (activeExposureMap && map.isStyleLoaded()) {
    const pointFeatures = map.queryRenderedFeatures(event.point, {
      layers: ["exposure-points-circle"]
    });
    if (pointFeatures.length) {
      await renderExposurePointDetails(pointFeatures[0]);
      return;
    }
  }

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

async function renderExposurePointDetails(feature) {
  if (!activeExposureMap) return;

  const rowId = Number(feature?.properties?.row_id);
  if (!Number.isFinite(rowId) || rowId < 1) return;

  dismissCriticalNote();
  hideSearchResults();
  statusEl.textContent = "Loading CSV row";

  const csvCount = Number(feature?.properties?.csv_count || 1);
  const params = new URLSearchParams({
    upload_id: activeExposureMap.upload_id,
    lat_col: activeExposureMap.lat_col,
    lon_col: activeExposureMap.lon_col,
    row_id: String(rowId)
  });

  try {
    const response = await fetch(`api/exposure/row?${params.toString()}`);
    const payload = await response.json();
    if (!response.ok) {
      throw new Error(payload.error || "Could not load CSV row");
    }

    renderExposureRow(payload, { csvCount });
    statusEl.textContent = "CSV row";
  } catch (error) {
    selectedBuilding = null;
    map.getSource("selected-building")?.setData(selectedSource);
    emptyEl.classList.add("hidden");
    detailsEl.classList.remove("hidden");
    matchTypeEl.textContent = "Exposure point";
    distanceEl.textContent = "";
    buildingIdEl.textContent = `CSV row ${formatInteger(rowId)}`;
    attributesEl.innerHTML = `<tr><td colspan="2">${escapeHtml(error.message)}</td></tr>`;
    statusEl.textContent = "Error";
  }
}

function renderExposureRow(payload, { csvCount = 1 } = {}) {
  const row = payload.row || {};
  const values = row.values || {};
  selectedBuilding = null;
  map.getSource("selected-building")?.setData(selectedSource);

  emptyEl.classList.add("hidden");
  detailsEl.classList.remove("hidden");

  matchTypeEl.textContent = csvCount > 1
    ? `Exposure cluster (${formatInteger(csvCount)})`
    : "Exposure point";
  const lat = Number(row.lat);
  const lon = Number(row.lon);
  distanceEl.textContent = Number.isFinite(lat) && Number.isFinite(lon)
    ? formatCoordinateLabel(lat, lon)
    : "";
  buildingIdEl.textContent = `CSV row ${formatInteger(Number(row.row_id || 0))}`;

  const rows = Object.entries(values).map(([field, value]) => `
    <tr>
      <th scope="row">${escapeHtml(field)}</th>
      <td>${escapeHtml(value == null ? "" : String(value))}</td>
    </tr>
  `);
  attributesEl.innerHTML = rows.length
    ? rows.join("")
    : '<tr><td colspan="2">No CSV fields found for this row.</td></tr>';
}

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
  const coordinateResult = parseCoordinateSearch(query);
  if (coordinateResult) {
    hideSearchResults();
    await goToSearchResult(coordinateResult, { lookupBuilding: true });
    return;
  }

  if (query.length < 3) {
    renderSearchMessage("Enter at least 3 characters, or coordinates like 52.5200, 13.4050.");
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
      goToSearchResult(result);
    });
  });
}

async function goToSearchResult(result, { lookupBuilding = false } = {}) {
  const lon = Number(result.lon);
  const lat = Number(result.lat);
  if (!Number.isFinite(lon) || !Number.isFinite(lat)) {
    renderSearchMessage("Search result does not contain valid coordinates.");
    return;
  }

  map.flyTo({
    center: [lon, lat],
    zoom: Math.max(map.getZoom(), 18),
    speed: 1.4
  });

  if (!lookupBuilding) return;
  statusEl.textContent = "Searching";
  try {
    const response = await fetch(`api/building-at?lon=${encodeURIComponent(lon)}&lat=${encodeURIComponent(lat)}`);
    const payload = await response.json();
    if (!response.ok) {
      throw new Error(payload.hint || payload.error || "Lookup failed");
    }
    if (!payload.building) {
      clearSelection();
      emptyEl.innerHTML = `<p>No building found at ${escapeHtml(formatCoordinateLabel(lat, lon))}.</p>`;
      statusEl.textContent = "No match";
      return;
    }
    renderBuilding(payload);
    statusEl.textContent = "Match found";
  } catch (error) {
    renderSearchMessage(error.message);
    statusEl.textContent = "Error";
  }
}

function parseCoordinateSearch(query) {
  if (!query) return null;
  const lower = query.toLowerCase();
  const numbers = [...query.matchAll(/[+-]?\d+(?:\.\d+)?/g)].map((match) => Number(match[0]));
  if (numbers.length < 2 || !numbers.slice(0, 2).every(Number.isFinite)) return null;

  let lat;
  let lon;
  const hasLatLabel = /\b(lat|latitude|y)\b/.test(lower);
  const hasLonLabel = /\b(lon|lng|long|longitude|x)\b/.test(lower);
  if (hasLatLabel && hasLonLabel) {
    lat = labelledCoordinate(lower, /\b(?:lat|latitude|y)\b\s*[:=]?\s*([+-]?\d+(?:\.\d+)?)/);
    lon = labelledCoordinate(lower, /\b(?:lon|lng|long|longitude|x)\b\s*[:=]?\s*([+-]?\d+(?:\.\d+)?)/);
  }

  if (!Number.isFinite(lat) || !Number.isFinite(lon)) {
    const first = numbers[0];
    const second = numbers[1];
    if (Math.abs(first) <= 90 && Math.abs(second) <= 180) {
      lat = first;
      lon = second;
    }
    if ((Math.abs(first) > 90 || /\b(lon|lng|long|longitude|x)\b/.test(lower.split(/[,\s]+/)[0] || "")) && Math.abs(first) <= 180 && Math.abs(second) <= 90) {
      lon = first;
      lat = second;
    }
  }

  if (!Number.isFinite(lat) || !Number.isFinite(lon)) return null;
  if (lat < -90 || lat > 90 || lon < -180 || lon > 180) return null;
  return {
    label: formatCoordinateLabel(lat, lon),
    lat,
    lon
  };
}

function labelledCoordinate(text, pattern) {
  const match = text.match(pattern);
  return match ? Number(match[1]) : NaN;
}

function formatCoordinateLabel(lat, lon) {
  return `${Number(lat).toFixed(6)}, ${Number(lon).toFixed(6)}`;
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

filterViewValue?.addEventListener("change", () => {
  updateFilterViewColorAvailability();
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

  activeViewFilter = {
    column,
    value,
    color,
    all: window.filterViewAll?.isAll(value) || false
  };
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
    setFilterViewMessage("Choose one value, or ALL for a discrete colour legend.");
  } catch (error) {
    renderViewFilterValueOptions([]);
    setFilterViewMessage(error.message, "error");
  }
}

function renderViewFilterValueOptions(values) {
  if (!filterViewValue) return;

  filterViewValue.innerHTML = [
    '<option value="">Select value</option>',
    values.length ? `<option value="${window.filterViewAll?.allValue?.() || "__ALL__"}">ALL</option>` : "",
    ...values.map((value) => `<option value="${escapeHtml(value)}">${escapeHtml(value)}</option>`)
  ].join("");
  filterViewValue.disabled = values.length === 0;
  updateFilterViewColorAvailability();
}

function removeViewFilterLayer() {
  if (map.getLayer(viewFilterFillLayerId)) {
    map.removeLayer(viewFilterFillLayerId);
  }
  if (map.getLayer(viewFilterOutlineLayerId)) {
    map.removeLayer(viewFilterOutlineLayerId);
  }
  if (map.getSource(viewFilterSourceId)) {
    map.removeSource(viewFilterSourceId);
  }
}

function viewFilterTileUrl() {
  if (!activeViewFilter) return "";

  const params = new URLSearchParams({
    column: activeViewFilter.column,
    value: activeViewFilter.value
  });

  if (!activeViewFilter.all && activeViewFilter.color) {
    params.set("color", activeViewFilter.color);
  }

  return `${window.location.origin}/api/tiles/{z}/{x}/{y}.mvt?${params.toString()}`;
}

function renderViewFilterLayer(tileUrl) {
  if (!tileUrl) return;
  if (map.getSource(viewFilterSourceId) && viewFilterTileUrlActive === tileUrl) {
    return;
  }

  removeViewFilterLayer();
  viewFilterTileUrlActive = tileUrl;

  map.addSource(viewFilterSourceId, {
    type: "vector",
    tiles: [tileUrl],
    minzoom: viewFilterMinZoom,
    maxzoom: viewFilterMaxTileZoom
  });

  map.addLayer({
    id: viewFilterFillLayerId,
    type: "fill",
    source: viewFilterSourceId,
    "source-layer": viewFilterSourceLayer,
    minzoom: viewFilterMinZoom,
    paint: {
      "fill-color": ["coalesce", ["get", "__color"], "#64748b"],
      "fill-opacity": [
        "interpolate",
        ["linear"],
        ["zoom"],
        viewFilterMinZoom, 0.34,
        viewFilterMinZoom + 1, 0.52
      ]
    }
  });

  map.addLayer({
    id: viewFilterOutlineLayerId,
    type: "line",
    source: viewFilterSourceId,
    "source-layer": viewFilterSourceLayer,
    minzoom: viewFilterMinZoom,
    paint: {
      "line-color": ["coalesce", ["get", "__color"], "#64748b"],
      "line-width": [
        "interpolate",
        ["linear"],
        ["zoom"],
        viewFilterMinZoom, 0.7,
        viewFilterMinZoom + 2, 1.2
      ],
      "line-opacity": [
        "interpolate",
        ["linear"],
        ["zoom"],
        viewFilterMinZoom, 0.7,
        viewFilterMinZoom + 1, 0.95
      ]
    }
  });
}

function applyViewFilterLegendColors(legend) {
  if (!activeViewFilter?.all || !map.getLayer(viewFilterFillLayerId)) return;

  const items = Array.isArray(legend) ? legend : [];
  if (!items.length) return;
  const colorExpression = ["match", ["get", "filter_value"]];
  for (const item of items) {
    colorExpression.push(String(item.value ?? ""), item.color || "#64748b");
  }
  colorExpression.push(["coalesce", ["get", "__color"], "#64748b"]);

  map.setPaintProperty(viewFilterFillLayerId, "fill-color", colorExpression);
  if (map.getLayer(viewFilterOutlineLayerId)) {
    map.setPaintProperty(viewFilterOutlineLayerId, "line-color", colorExpression);
  }
}

async function refreshViewFilter({ silent = false } = {}) {
  if (!activeViewFilter) return;
  if (!map.isStyleLoaded()) return;

  const requestId = ++viewFilterRequestId;
  const bounds = map.getBounds();
  const tileUrl = viewFilterTileUrl();
  renderViewFilterLayer(tileUrl);

  if (viewFilterFetchController) {
    viewFilterFetchController.abort();
    viewFilterFetchController = null;
  }

  if (map.getZoom() < viewFilterMinZoom) {
    window.filterViewAll?.clearLegend();
    setFilterViewMessage(viewFilterZoomMessage, "zoom");
    if (!silent || statusEl.textContent === "Filtering") {
      statusEl.textContent = "Ready";
    }
    return;
  }
  viewFilterFetchController = new AbortController();

  const params = new URLSearchParams({
    min_lon: String(bounds.getWest()),
    min_lat: String(bounds.getSouth()),
    max_lon: String(bounds.getEast()),
    max_lat: String(bounds.getNorth()),
    zoom: String(map.getZoom()),
    column: activeViewFilter.column,
    value: activeViewFilter.value
  });

  if (!silent) {
    statusEl.textContent = "Filtering";
    setFilterViewMessage("Loading matching footprints in the current view...");
  }

  try {
    const response = await fetch(`api/building-filter-summary?${params.toString()}`, {
      signal: viewFilterFetchController.signal
    });
    const payload = await response.json();

    if (requestId !== viewFilterRequestId) return;
    if (!response.ok) {
      throw new Error(payload.error || "Could not load building footprints");
    }
    viewFilterFetchController = null;

    const colored = Number(payload.colored_count ?? payload.count ?? 0);
    const shown = Number(payload.shown_count ?? colored);
    const tileLimit = Number(payload.tile_feature_limit || colored);
    const maxColoredPerTile = Math.min(colored, tileLimit);
    const filterCountMessage = colored > maxColoredPerTile
      ? `${formatInteger(shown)} polygons shown · ${formatInteger(colored)} matching · up to ${formatInteger(maxColoredPerTile)} coloured per tile.`
      : `${formatInteger(shown)} polygons shown · ${formatInteger(colored)} coloured.`;
    if (activeViewFilter.all) {
      applyViewFilterLegendColors(payload.legend);
      window.filterViewAll?.renderLegend(payload, formatFieldLabel(activeViewFilter.column));
      setFilterViewMessage(filterCountMessage, colored ? "success" : "");
    } else {
      window.filterViewAll?.clearLegend();
      setFilterViewMessage(filterCountMessage, colored ? "success" : "");
    }
    if (!silent || statusEl.textContent === "Filtering") {
      statusEl.textContent = "Ready";
    }
  } catch (error) {
    if (error.name === "AbortError") {
      return;
    }
    if (requestId !== viewFilterRequestId) return;

    viewFilterFetchController = null;

    window.filterViewAll?.clearLegend();
    setFilterViewMessage(error.message, "error");
    if (!silent || statusEl.textContent === "Filtering") {
      statusEl.textContent = "Error";
    }
  }
}

function clearViewFilter(message = "", type = "") {
  activeViewFilter = null;
  viewFilterRequestId += 1;
  clearViewFilterLayer();
  window.filterViewAll?.clearLegend();
  if (filterViewColumn) filterViewColumn.value = "";
  renderViewFilterValueOptions([]);
  updateFilterViewColorAvailability();
  setFilterViewMessage(message, type);
}

function clearViewFilterLayer() {
  if (viewFilterFetchController) {
    viewFilterFetchController.abort();
    viewFilterFetchController = null;
  }
  viewFilterTileUrlActive = "";
  removeViewFilterLayer();
  window.filterViewAll?.clearLegend();
}

function updateFilterViewColorAvailability() {
  if (!filterViewColor) return;
  const disabled = window.filterViewAll?.isAll(filterViewValue?.value || "") || false;
  filterViewColor.disabled = disabled;
  filterViewColor.title = disabled ? "ALL mode uses automatic discrete colours." : "";
  filterViewColor.closest("label")?.classList.toggle("disabled", disabled);
}

function setFilterViewMessage(message, type = "") {
  if (!filterViewMessage) return;

  filterViewMessage.textContent = message;
  filterViewMessage.classList.toggle("error", type === "error");
  filterViewMessage.classList.toggle("success", type === "success");
  filterViewMessage.classList.toggle("zoom-notice", type === "zoom");
}

uploadForm.addEventListener("submit", (event) => {
  event.preventDefault();
  uploadSelectedCsv();
});

csvFile.addEventListener("change", () => {
  uploadSelectedCsv();
});

showExposureOnMap?.addEventListener("click", () => {
  activateExposureMap();
});

clearExposureUpload?.addEventListener("click", () => {
  void clearExposureWorkflow();
});

clearExposureMap?.addEventListener("click", () => {
  clearActiveExposureMap();
});

async function uploadSelectedCsv() {
  const requestId = ++exposureUploadRequestId;
  if (!csvFile.files.length) {
    setUploadedCsvName("");
    setUploadSummary("Choose a CSV or Excel (.xlsx) file first.");
    exposureUploadActions?.classList.add("hidden");
    exposureMapControls?.classList.add("hidden");
    return;
  }

  dismissCriticalNote();

  const formData = new FormData();
  formData.append("file", csvFile.files[0]);
  setUploadedCsvName(csvFile.files[0].name);
  exposureUploadActions?.classList.remove("hidden");
  clearActiveExposureMap({ keepControls: true });
  setExposureMapMessage("");
  exposureMapControls?.classList.add("hidden");

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
    if (requestId !== exposureUploadRequestId) return;

    currentUploadId = payload.upload_id;
    currentUploadFilename = payload.filename;
    currentUploadColumns = Array.isArray(payload.columns) ? [...payload.columns] : [];
    setUploadedCsvName(payload.filename);
    populateColumnSelectors(payload.columns);
    publishExposureUploadState();
    renderPreview(payload.columns, payload.rows);
    mappingControls.classList.remove("hidden");
    exposureMapControls?.classList.remove("hidden");
    statsPanel.classList.add("hidden");
    releaseStatsDownload();
    renderFileSummary(payload.filename, payload.rows.length);
    statusEl.textContent = "Ready";
  } catch (error) {
    if (requestId !== exposureUploadRequestId) return;
    statusEl.textContent = "Error";
    currentUploadColumns = [];
    setUploadSummary(error.message);
    previewTable.classList.add("hidden");
    exposureMapControls?.classList.add("hidden");
  }
}

async function activateExposureMap() {
  const requestId = ++exposureActivationRequestId;
  if (!currentUploadId) {
    setExposureMapMessage("Upload a file first.", "error");
    return;
  }

  const latCol = latColumn.value;
  const lonCol = lonColumn.value;
  if (!latCol || !lonCol) {
    setExposureMapMessage("Choose latitude and longitude columns first.", "error");
    return;
  }

  clearActiveExposureMap({ keepControls: true });
  showExposureOnMap.disabled = true;
  statusEl.textContent = "Preparing map";
  setExposureMapMessage("Preparing map points...");

  try {
    const params = new URLSearchParams({
      upload_id: currentUploadId,
      lat_col: latCol,
      lon_col: lonCol
    });
    const response = await fetch(`api/exposure/map-points?${params.toString()}`);
    const payload = await response.json();

    if (!response.ok) {
      throw new Error(payload.error || "Could not prepare map points");
    }
    if (requestId !== exposureActivationRequestId) return;

    const validRows = Number(payload.valid_rows || 0);
    if (!validRows || !payload.extent) {
      throw new Error("No valid latitude/longitude rows were found.");
    }

    activeExposureMap = {
      upload_id: currentUploadId,
      filename: payload.filename || currentUploadFilename || "Exposure",
      lat_col: latCol,
      lon_col: lonCol,
      total_rows: Number(payload.total_rows || 0),
      valid_rows: validRows,
      extent: payload.extent
    };
    exposureMapRequestId += 1;
    syncExposureRefreshControl();

    updateExposureMapPanel({
      visible_count: 0,
      returned_count: 0,
      cell_count: 0,
      mode: "loading"
    });
    setExposureMapMessage(`Ready to map ${formatInteger(validRows)} locations.`, "success");
    switchMode("lookup");
    clearSelection();

    window.setTimeout(() => {
      map.resize();
      fitMapToExposureExtent(activeExposureMap.extent);
    }, 80);
  } catch (error) {
    if (requestId !== exposureActivationRequestId) return;
    clearActiveExposureMap({ keepControls: true });
    statusEl.textContent = "Error";
    setExposureMapMessage(error.message, "error");
  } finally {
    showExposureOnMap.disabled = false;
  }
}

function fitMapToExposureExtent(extent) {
  if (!extent) {
    requestExposurePointRefresh();
    return;
  }

  const minLon = Number(extent.min_lon);
  const minLat = Number(extent.min_lat);
  const maxLon = Number(extent.max_lon);
  const maxLat = Number(extent.max_lat);
  if (![minLon, minLat, maxLon, maxLat].every(Number.isFinite)) {
    requestExposurePointRefresh();
    return;
  }

  const center = [(minLon + maxLon) / 2, (minLat + maxLat) / 2];
  const lonSpan = Math.abs(maxLon - minLon);
  const latSpan = Math.abs(maxLat - minLat);

  if (lonSpan < 0.00005 && latSpan < 0.00005) {
    map.flyTo({
      center,
      zoom: Math.max(map.getZoom(), 16),
      speed: 1.5
    });
  } else {
    map.fitBounds([[minLon, minLat], [maxLon, maxLat]], {
      padding: 72,
      maxZoom: 15,
      duration: 650
    });
  }
}

function requestExposurePointRefresh() {
  if (!activeExposureMap || !map.isStyleLoaded()) return;
  refreshExposurePoints({ silent: true });
}

async function refreshExposurePoints({ silent = false, manual = false } = {}) {
  if (!activeExposureMap || !map.isStyleLoaded()) return;

  const requestId = ++exposureMapRequestId;
  const bounds = map.getBounds();
  const canvas = map.getCanvas();
  const lonPadding = Math.max(0.00001, Math.abs(bounds.getEast() - bounds.getWest()) * 0.12);
  const latPadding = Math.max(0.00001, Math.abs(bounds.getNorth() - bounds.getSouth()) * 0.12);
  const params = new URLSearchParams({
    upload_id: activeExposureMap.upload_id,
    lat_col: activeExposureMap.lat_col,
    lon_col: activeExposureMap.lon_col,
    min_lon: String(bounds.getWest() - lonPadding),
    min_lat: String(bounds.getSouth() - latPadding),
    max_lon: String(bounds.getEast() + lonPadding),
    max_lat: String(bounds.getNorth() + latPadding),
    width: String(canvas.clientWidth || 1200),
    height: String(canvas.clientHeight || 800),
    zoom: String(map.getZoom()),
    format: "compact"
  });

  if (exposureMapFetchController) {
    exposureMapFetchController.abort();
  }
  const controller = new AbortController();
  exposureMapFetchController = controller;
  if (manual) {
    setExposureMapRefreshState(true);
    updateExposureMapPanel({
      ...activeExposureMap,
      mode: "loading"
    });
  }
  if (!silent) {
    statusEl.textContent = "Loading points";
  }

  try {
    const response = await fetch(`api/exposure/map-points?${params.toString()}`, {
      signal: controller.signal
    });
    const payload = await response.json();

    if (requestId !== exposureMapRequestId || !activeExposureMap) return;
    if (!response.ok) {
      throw new Error(payload.error || "Could not load map points");
    }

    const points = Array.isArray(payload.points) ? payload.points : [];
    const features = new Array(points.length);
    for (let index = 0; index < points.length; index += 1) {
      const point = points[index];
      features[index] = {
        type: "Feature",
        geometry: { type: "Point", coordinates: [point[0], point[1]] },
        properties: {
          row_id: point[2],
          csv_count: point[3],
          csv_label: point[4],
          duplicate_count: point[5]
        }
      };
    }

    // One setData call swaps the whole padded viewport at once. Keeping the
    // previous collection until here avoids blank flashes during navigation.
    map.getSource("exposure-points")?.setData({ type: "FeatureCollection", features });

    activeExposureMap = {
      ...activeExposureMap,
      total_rows: Number(payload.total_rows || activeExposureMap.total_rows || 0),
      valid_rows: Number(payload.valid_rows || activeExposureMap.valid_rows || 0),
      visible_count: Number(payload.visible_count || 0),
      returned_count: Number(payload.returned_count || features.length || 0),
      cell_count: Number(payload.cell_count || 0),
      mode: payload.mode || (map.getZoom() >= exposureRawPointZoom ? "raw" : "grid")
    };
    updateExposureMapPanel(activeExposureMap);
    if (!silent || statusEl.textContent === "Preparing map" || statusEl.textContent === "Loading points") {
      statusEl.textContent = "Ready";
    }
  } catch (error) {
    if (error.name === "AbortError") return;
    if (requestId !== exposureMapRequestId) return;
    setExposureMapPanelError(error.message);
    if (!silent || statusEl.textContent === "Preparing map" || statusEl.textContent === "Loading points") {
      statusEl.textContent = "Error";
    }
  } finally {
    if (exposureMapFetchController === controller) {
      exposureMapFetchController = null;
    }
    if (manual) {
      setExposureMapRefreshState(false);
    }
  }
}

function updateExposureMapPanel(payload) {
  if (!activeExposureMap || !exposureMapPanel) return;

  exposureMapPanel.classList.remove("hidden", "error");
  if (exposureMapTitle) {
    exposureMapTitle.textContent = activeExposureMap.filename || "Exposure locations";
  }

  const visible = Number(payload.visible_count || 0);
  const drawn = Number(payload.returned_count || 0);
  const valid = Number(activeExposureMap.valid_rows || 0);
  if (exposureMapStats) {
    exposureMapStats.textContent = payload.mode === "loading"
      ? `${formatInteger(valid)} valid locations · loading view…`
      : visible
      ? `${formatInteger(visible)} in view · ${formatInteger(drawn)} drawn · ${formatInteger(valid)} valid`
      : `${formatInteger(valid)} valid locations`;
  }
}

function setExposureMapPanelError(message) {
  if (!exposureMapPanel || !exposureMapStats) return;
  exposureMapPanel.classList.remove("hidden");
  exposureMapPanel.classList.add("error");
  exposureMapStats.textContent = message;
}

function clearActiveExposureMap({ keepControls = false } = {}) {
  activeExposureMap = null;
  exposureMapRequestId += 1;
  if (exposureMapFetchController) {
    exposureMapFetchController.abort();
    exposureMapFetchController = null;
  }

  map.getSource("exposure-points")?.setData(emptyFeatureCollection);
  exposureMapPanel?.classList.add("hidden");
  exposureMapPanel?.classList.remove("error");
  if (exposureMapStats) exposureMapStats.textContent = "";
  setExposureMapRefreshState(false);
  if (!keepControls) setExposureMapMessage("");
  if (statusEl.textContent === "Loading points" || statusEl.textContent === "Preparing map") {
    statusEl.textContent = "Ready";
  }
}

function setExposureMapRefreshState(isRefreshing) {
  exposureRefreshBusy = Boolean(isRefreshing);
  syncExposureRefreshControl();
}

function syncExposureRefreshControl() {
  if (!exposureRefreshButton) return;

  const hasExposureMap = Boolean(activeExposureMap);
  const isDisabled = !hasExposureMap || exposureRefreshBusy;
  exposureRefreshButton.disabled = isDisabled;
  exposureRefreshButton.classList.toggle("is-active", hasExposureMap);
  exposureRefreshButton.classList.toggle("is-busy", exposureRefreshBusy);
  exposureRefreshButton.setAttribute("aria-disabled", isDisabled ? "true" : "false");
  exposureRefreshButton.setAttribute(
    "aria-label",
    exposureRefreshBusy
      ? "Refreshing exposure map points."
      : hasExposureMap
      ? "Refresh exposure map points."
      : "Refresh exposure map points. Load an exposure map first."
  );
  exposureRefreshButton.setAttribute(
    "title",
    exposureRefreshBusy
      ? "Refreshing exposure map points"
      : hasExposureMap
      ? "Refresh exposure map points"
      : "Load an exposure map first"
  );
}

function setExposureMapMessage(message, type = "") {
  if (!exposureMapMessage) return;
  exposureMapMessage.textContent = message;
  exposureMapMessage.classList.toggle("error", type === "error");
  exposureMapMessage.classList.toggle("success", type === "success");
}

async function deleteExposureUpload(uploadId) {
  if (!uploadId) return;

  try {
    const response = await fetch(`api/exposure/upload/${encodeURIComponent(uploadId)}`, {
      method: "DELETE",
      keepalive: true
    });
    if (response.ok || response.status === 404 || response.status === 409) {
      return;
    }
  } catch (_error) {
    // Client-side reset is already complete.
  }
}

async function clearExposureWorkflow({ preserveStatus = false } = {}) {
  const uploadId = currentUploadId;

  exposureUploadRequestId += 1;
  exposureActivationRequestId += 1;
  enrichmentProgressRequestId += 1;
  currentUploadId = null;
  currentUploadFilename = null;
  currentUploadColumns = [];
  publishExposureUploadState();

  clearActiveExposureMap({ keepControls: false });
  window.rasterIntersectionController?.reset?.();
  window.rasterIntersectionPreview?.clear?.();
  window.rasterIntersectionLayers?.clear?.();

  try {
    uploadForm?.reset();
  } catch (_error) {
    try {
      csvFile.value = "";
    } catch (_innerError) {
      // Ignore browsers that do not allow clearing a file input here.
    }
  }

  setUploadedCsvName("");
  setUploadSummary(defaultUploadSummaryText);
  previewTable.classList.add("hidden");
  previewTable.innerHTML = "";
  exposureUploadActions?.classList.add("hidden");
  mappingControls.classList.add("hidden");
  exposureMapControls?.classList.add("hidden");
  statsPanel.classList.add("hidden");
  statsGrid.innerHTML = "";
  releaseStatsDownload();
  downloadLink.classList.add("hidden");
  downloadLink.removeAttribute("href");
  downloadLink.removeAttribute("download");
  latColumn.innerHTML = "";
  lonColumn.innerHTML = "";
  matchMode.value = defaultMatchModeValue;
  maxDistance.value = defaultMaxDistanceValue;
  runEnrichment.disabled = false;
  setExposureMapMessage("");

  if (!preserveStatus) {
    statusEl.textContent = "Ready";
  }

  await deleteExposureUpload(uploadId);
}

function resetLookupUi() {
  clearSelection();
  emptyEl.innerHTML = defaultEmptyStateHtml;
  hideSearchResults();
  searchForm?.reset();
  clearViewFilter(defaultFilterViewMessage);
  filterViewColor.value = "#ff6b6b";
}

function resetEtlUi() {
  etlProgressRequestId += 1;
  etlForm?.reset();
  delete etlOutputParquet.dataset.userEdited;
  delete etlLookupDbFile.dataset.userEdited;
  resetBoundaryFileSelection();
  updateEtlOutputDefaults();
  setExpandedEtlWorkflow(null);
  runEtlBtn.disabled = false;
  etlStatusEl.classList.add("hidden");
  etlStatusEl.classList.remove("etl-status--error", "etl-status--success");
  etlStatusEl.innerHTML = "";
  window.customParquetUi?.reset?.();
}

async function clearAllState() {
  clearAllStateButton.disabled = true;
  statusEl.textContent = "Clearing";

  try {
    dismissCriticalNote();
    dbPicker.classList.add("hidden");
    setDataSourceMessage("");
    resetLookupUi();
    await clearExposureWorkflow({ preserveStatus: true });
    window.addedMapLayerController?.reset?.();
    window.rasterIntersectionController?.reset?.();
    resetEtlUi();
    statusEl.textContent = "Ready";
  } finally {
    clearAllStateButton.disabled = false;
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
  const requestId = ++enrichmentProgressRequestId;

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
    if (requestId !== enrichmentProgressRequestId) return;

    pollEnrichmentProgress(payload.job_id, requestId);
  } catch (error) {
    if (requestId !== enrichmentProgressRequestId) return;
    statusEl.textContent = "Error";
    setUploadSummary(error.message);
    runEnrichment.disabled = false;
  }
});

async function pollEnrichmentProgress(jobId, requestId) {
  try {
    const response = await fetch(`api/exposure/progress/${jobId}`);
    const payload = await response.json();
    if (requestId !== enrichmentProgressRequestId) return;

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

    window.setTimeout(() => pollEnrichmentProgress(jobId, requestId), 1500);
  } catch (error) {
    if (requestId !== enrichmentProgressRequestId) return;
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
const etlCountrySelect = document.getElementById("etlCountrySelect");
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

function resetBoundaryFileSelection() {
  boundaryFile.value = "";
  boundaryFileName.textContent = "";
  boundaryFileName.classList.add("hidden");
}

async function loadEtlCountries() {
  if (!etlCountrySelect) return;

  etlCountrySelect.disabled = true;
  etlCountrySelect.innerHTML = '<option value="">Loading countries...</option>';

  try {
    const response = await fetch("api/etl/countries");
    const payload = await response.json();

    if (!response.ok) {
      throw new Error(payload.error || "Could not load country catalog");
    }

    const options = ['<option value="" data-code="">Use uploaded boundary or default Germany boundary</option>'];
    for (const country of payload.countries || []) {
      options.push(
        `<option value="${escapeHtml(country.key || "")}" data-code="${escapeHtml(country.code || "")}">${escapeHtml(country.label || country.name || "")}</option>`
      );
    }

    etlCountrySelect.innerHTML = options.join("");
    etlCountrySelect.disabled = false;
  } catch (error) {
    console.error(error);
    etlCountrySelect.innerHTML = '<option value="">Country catalog unavailable</option>';
    etlCountrySelect.disabled = true;
  }
}

etlCountrySelect?.addEventListener("change", () => {
  if (etlCountrySelect.value) {
    dismissCriticalNote();
    resetBoundaryFileSelection();
  }
  updateEtlOutputDefaults();
});

loadEtlCountries();

boundaryFile.addEventListener("change", () => {
  if (boundaryFile.files.length) {
    dismissCriticalNote();
    if (etlCountrySelect) {
      etlCountrySelect.value = "";
    }
  }

  if (boundaryFile.files.length) {
    boundaryFileName.textContent = boundaryFile.files[0].name;
    boundaryFileName.classList.remove("hidden");
  } else {
    boundaryFileName.classList.add("hidden");
  }
  updateEtlOutputDefaults();
});

function getEtlSuffix() {
  const selectedOption = etlCountrySelect?.selectedOptions?.[0];
  const countryCode = selectedOption?.dataset?.code;
  if (countryCode) return countryCode;
  if (boundaryFile.files.length) {
    const stem = boundaryFile.files[0].name.replace(/\.[^.]+$/, "").replace(/[^A-Za-z0-9]/g, "");
    return (stem.slice(0, 3) || "CUS").toUpperCase();
  }
  return "";
}

function updateEtlOutputDefaults() {
  const dir = etlOutputDir.value.trim() || "./etl_output";
  const suffix = getEtlSuffix();
  const tag = suffix ? `_${suffix}` : "";
  if (!etlOutputParquet.dataset.userEdited) {
    etlOutputParquet.value = `${dir}/buildings_cleaned${tag}.parquet`;
  }
  if (!etlLookupDbFile.dataset.userEdited) {
    etlLookupDbFile.value = `${dir}/building_lookup${tag}.duckdb`;
  }
}

etlOutputDir.addEventListener("input", () => { updateEtlOutputDefaults(); });

etlOutputParquet.addEventListener("input", () => { etlOutputParquet.dataset.userEdited = "1"; });
etlLookupDbFile.addEventListener("input", () => { etlLookupDbFile.dataset.userEdited = "1"; });
updateEtlOutputDefaults();

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
    updateEtlOutputDefaults();
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
  } else if (etlCountrySelect?.value) {
    formData.append("country_key", etlCountrySelect.value);
  }

  const dir = etlOutputDir.value.trim() || "./etl_output";
  formData.append("output_dir", dir);
  formData.append("output_parquet", etlOutputParquet.value.trim());
  formData.append("lookup_db_file", etlLookupDbFile.value.trim());
  const requestId = ++etlProgressRequestId;
  try {
    const response = await fetch("api/etl/create-database", {
      method: "POST",
      body: formData
    });
    const payload = await response.json();

    if (!response.ok) {
      throw new Error(payload.error || "ETL submission failed");
    }
    if (requestId !== etlProgressRequestId) return;

    pollEtlProgress(payload.job_id, requestId);
  } catch (error) {
    if (requestId !== etlProgressRequestId) return;
    statusEl.textContent = "Error";
    showEtlStatus("error", error.message);
    runEtlBtn.disabled = false;
  }
});

async function pollEtlProgress(jobId, requestId) {
  try {
    const response = await fetch(`api/etl/progress/${jobId}`);
    const payload = await response.json();
    if (requestId !== etlProgressRequestId) return;

    if (!response.ok) throw new Error(payload.error || "Could not read ETL progress");

    const percent = Math.max(0, Math.min(100, Number(payload.percent || 0)));
    const phaseText = escapeHtml(payload.phase || payload.status || "Working");
    const detailHtml = payload.detail
      ? `<div class="progress-copy">${escapeHtml(payload.detail)}</div>`
      : "";
    showEtlStatus("info", `
      <div class="progress-copy">${phaseText}</div>
      ${detailHtml}
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

    window.setTimeout(() => pollEtlProgress(jobId, requestId), 3000);
  } catch (error) {
    if (requestId !== etlProgressRequestId) return;
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
