(function () {
  const layerFile = document.getElementById("addLayerFile");
  const layerFileTitle = document.getElementById("addLayerFileTitle");
  const layerFileSubtitle = document.getElementById("addLayerFileSubtitle");
  const layerFileProgress = document.getElementById("addLayerFileProgress");
  const dropzone = document.querySelector('label.add-layer-dropzone[for="addLayerFile"]');
  const uploadButton = document.getElementById("uploadMapLayer");
  const addAnotherButton = document.getElementById("addAnotherLayer");
  const removeSelectedButton = document.getElementById("removeSelectedLayers");
  const importedListEl = document.getElementById("addLayerImported");
  const controls = document.getElementById("addLayerControls");
  const clearButton = document.getElementById("clearMapLayer");
  const messageEl = document.getElementById("addLayerMessage");
  const layerListEl = document.getElementById("addLayerList");
  const layerListPanel = document.getElementById("addLayerListPanel");
  const maxUploadBytes = Number(layerFile?.dataset.maxBytes || 0);

  const MAX_LAYERS = 5;
  const buildingOverlayLayerId = "view-filter-buildings-fill";
  const emptyCollection = { type: "FeatureCollection", features: [] };
  // Keep in sync with COLOR_MAPS in layer_upload_routes.py.
  const legendPalettes = {
    viridis: ["#440154", "#3b528b", "#21918c", "#5ec962", "#fde725"],
    plasma: ["#0d0887", "#7e03a8", "#cc4778", "#f89540", "#f0f921"],
    magma: ["#000004", "#3b0f70", "#8c2981", "#de4968", "#fcfdbf"],
    cividis: ["#00204c", "#414d6b", "#7c7b78", "#b8ad6f", "#ffea46"],
    hazard: ["#2c7bb6", "#abd9e9", "#ffffbf", "#fdae61", "#d7191c"],
    reds: ["#fff5f0", "#fcbba1", "#fb6a4a", "#cb181d", "#67000d"],
    blues: ["#f7fbff", "#c6dbef", "#6baed6", "#2171b5", "#08306b"],
    greens: ["#f7fcf5", "#c7e9c0", "#74c476", "#238b45", "#00441b"],
    purples: ["#fcfbfd", "#dadaeb", "#9e9ac8", "#6a51a3", "#3f007d"],
    ylorrd: ["#ffffcc", "#fed976", "#fd8d3c", "#e31a1c", "#800026"],
    turbo: ["#30123b", "#4686fb", "#1ae4b6", "#a2fc3c", "#faba39", "#e4460a", "#7a0403"],
    spectral: ["#9e0142", "#f46d43", "#fee08b", "#e6f598", "#66c2a5", "#5e4fa2"],
    terrain: ["#333399", "#0294fa", "#24d36d", "#fefe98", "#835f53", "#ffffff"],
    brbg: ["#543005", "#bf812d", "#f5f5f5", "#35978f", "#003c30"],
    categorical: ["#2563eb", "#16a34a", "#dc2626", "#9333ea", "#ea580c", "#0891b2", "#be123c", "#4d7c0f"]
  };
  const colormapOptions = [
    ["hazard", "Hazard"],
    ["viridis", "Viridis"],
    ["plasma", "Plasma"],
    ["magma", "Magma"],
    ["cividis", "Cividis"],
    ["reds", "Reds"],
    ["blues", "Blues"],
    ["greens", "Greens"],
    ["purples", "Purples"],
    ["ylorrd", "Yellow-Orange-Red"],
    ["turbo", "Turbo (rainbow)"],
    ["spectral", "Spectral"],
    ["terrain", "Terrain"],
    ["brbg", "Brown-Teal (diverging)"],
    ["categorical", "Categorical"]
  ];

  const state = {
    // Index 0 is drawn on top of the map.
    layers: [],
    activeId: "",
    popup: null,
    importCycle: 0,
    importCounter: 0
  };
  const runtime = new Map();
  const selectedForRemoval = new Set();
  const defaultLayerTitle = "Choose Layer File";
  const defaultLayerSubtitle = "GeoPackage, zipped shapefile, shapefile (.shp), GeoJSON, or GeoTIFF";
  let selectedLocalPath = "";
  let activeImportJobId = "";
  let uploadAfterPick = false;
  let importing = false;
  let dropzoneOpen = true;
  publishLayerChange();

  if (!layerFile || !uploadButton) {
    return;
  }

  if (typeof map !== "undefined") {
    map.on("move", () => scheduleAllVectorRefresh());
    map.on("moveend", () => scheduleAllVectorRefresh({ immediate: true }));
  }

  layerFile.addEventListener("change", () => {
    selectedLocalPath = "";
    const files = Array.from(layerFile.files || []);
    if (!files.length) {
      uploadAfterPick = false;
      return;
    }
    setDropzoneContent(
      files.length > 1 ? `${files.length} files selected` : files[0].name,
      "Ready to import – click Import layer to add it to the map"
    );
    if (uploadAfterPick) {
      uploadAfterPick = false;
      uploadLayerFromBrowser();
    }
  });

  dropzone?.addEventListener("click", (event) => {
    event.preventDefault();
    if (importing) return;
    chooseLocalLayer(false);
  });
  uploadButton.addEventListener("click", handleAddLayerClick);
  addAnotherButton?.addEventListener("click", () => {
    if (!canAddLayer()) return;
    dropzoneOpen = true;
    resetDropzone();
    syncActions();
    chooseLocalLayer(true);
  });
  removeSelectedButton?.addEventListener("click", removeSelectedLayers);
  importedListEl?.addEventListener("change", (event) => {
    const input = event.target.closest('input[data-action="select-remove"]');
    const card = input?.closest("[data-layer-id]");
    if (!card) return;
    if (input.checked) selectedForRemoval.add(card.dataset.layerId);
    else selectedForRemoval.delete(card.dataset.layerId);
    card.classList.toggle("is-selected", input.checked);
    syncActions();
  });
  clearButton?.addEventListener("click", () => resetLayerUi());
  layerListEl?.addEventListener("click", (event) => {
    const button = event.target.closest("button[data-action]");
    const id = button?.closest("[data-layer-id]")?.dataset.layerId;
    if (!id) return;
    const action = button.dataset.action;
    if (action === "up") moveLayerBy(id, -1);
    else if (action === "down") moveLayerBy(id, 1);
    else if (action === "remove") removeLayer(id);
  });
  layerListEl?.addEventListener("change", (event) => {
    const target = event.target;
    const entry = findLayer(target.closest?.("[data-layer-id]")?.dataset.layerId);
    if (!entry) return;
    if (target.dataset.action === "toggle") setLayerVisible(entry.id, target.checked);
    else if (target.dataset.setting === "field") setLayerField(entry, target.value);
    else if (target.dataset.setting === "colormap") setLayerColormap(entry, target.value);
  });
  layerListEl?.addEventListener("input", (event) => {
    const target = event.target;
    const setting = target.dataset?.setting;
    if (setting !== "transparency" && setting !== "boundary") return;
    const entry = findLayer(target.closest("[data-layer-id]")?.dataset.layerId);
    if (!entry) return;
    if (setting === "transparency") {
      entry.transparency = Number(target.value);
      updateLayerOpacity(entry);
    } else {
      entry.boundaryWidth = Number(target.value);
      updateBoundaryWidth(entry);
    }
    const output = target.closest("label")?.querySelector("output");
    if (output) output.textContent = settingValueLabel(setting, Number(target.value));
  });

  async function handleAddLayerClick() {
    if (selectedLocalPath) {
      await importLocalLayer();
      return;
    }

    const files = Array.from(layerFile.files || []);
    if (files.length) {
      await uploadLayerFromBrowser();
      return;
    }

    await chooseLocalLayer(true);
  }

  async function chooseLocalLayer(autoImport) {
    if (activeImportJobId || importing) return;
    const selectionCycle = state.importCycle;
    uploadButton.disabled = true;
    uploadButton.innerHTML = '<span class="spinner"></span> Selecting...';
    setMessage("Opening layer file picker...");

    try {
      const response = await fetch("api/browse-file?kind=layer");
      const payload = await response.json();
      if (!response.ok) {
        if (response.status === 501) {
          setMessage("Native file picker is unavailable in this session. Choose a layer file in the browser instead.");
          openLayerPicker(autoImport);
          return;
        }
        throw new Error(payload.error || "Could not open layer file picker");
      }
      if (payload.cancelled) {
        setMessage("Layer selection cancelled.");
        return;
      }

      applyLocalSelection(payload.path || "");
      if (!selectedLocalPath) {
        setMessage("Choose a layer file to import.");
        return;
      }
      if (autoImport) {
        syncActions();
        await importLocalLayer();
      } else {
        setMessage("Layer selected. Click Import layer to load it.", "success");
      }
    } catch (error) {
      setMessage(error.message, "error");
    } finally {
      if (selectionCycle === state.importCycle && !activeImportJobId) {
        syncActions();
      }
    }
  }

  async function importLocalLayer() {
    if (!selectedLocalPath) {
      setMessage("Choose a layer file to import.", "error");
      return;
    }
    if (!canAddLayer()) return;

    const importCycle = ++state.importCycle;
    if (typeof dismissCriticalNote === "function") dismissCriticalNote();
    importing = true;
    uploadButton.disabled = true;
    uploadButton.innerHTML = '<span class="spinner"></span> Importing...';
    syncActions();
    if (typeof statusEl !== "undefined") statusEl.textContent = "Adding layer";
    setDropzoneProgress(localPathName(selectedLocalPath), "Submitting import…", 0);
    setMessage("");

    try {
      const response = await fetch("api/layers/import-local", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ path: selectedLocalPath })
      });
      const payload = await response.json();
      if (!response.ok) {
        throw new Error(payload.error || "Could not import layer");
      }
      if (importCycle !== state.importCycle) return;

      activeImportJobId = payload.job_id || "";
      showImportProgress(payload);
      pollLayerImport(activeImportJobId, importCycle);
    } catch (error) {
      if (importCycle !== state.importCycle) return;
      activeImportJobId = "";
      importing = false;
      setDropzoneError(error.message);
      setMessage(error.message, "error");
      syncActions();
      if (typeof statusEl !== "undefined") statusEl.textContent = "Error";
    }
  }

  async function uploadLayerFromBrowser() {
    const files = Array.from(layerFile.files || []);
    if (!files.length) {
      setMessage("Choose a layer file to upload.");
      openLayerPicker(true);
      return;
    }

    if (!canAddLayer()) return;

    const oversizeFile = files.find((file) => maxUploadBytes > 0 && Number(file.size || 0) > maxUploadBytes);
    if (oversizeFile) {
      setMessage(
        `Layer is too large for this local session. ${oversizeFile.name} is ${formatBytes(oversizeFile.size)}, limit ${formatBytes(maxUploadBytes)}.`,
        "error"
      );
      return;
    }

    if (typeof dismissCriticalNote === "function") dismissCriticalNote();
    const importCycle = ++state.importCycle;
    importing = true;
    uploadButton.disabled = true;
    uploadButton.innerHTML = '<span class="spinner"></span> Uploading\u2026';
    syncActions();
    if (typeof statusEl !== "undefined") statusEl.textContent = "Adding layer";
    setDropzoneProgress(files[0].name, "Uploading and preparing layer…", null);
    setMessage("");

    const formData = new FormData();
    for (const file of files) {
      formData.append("file", file);
    }
    try {
      const response = await fetch("api/layers/upload", {
        method: "POST",
        body: formData
      });
      const payload = await response.json();

      if (!response.ok) {
        throw new Error(payload.error || "Could not add layer");
      }

      if (importCycle !== state.importCycle) return;
      addLoadedLayer(payload);
    } catch (error) {
      if (importCycle !== state.importCycle) return;
      setDropzoneError(error.message);
      setMessage(error.message, "error");
      if (typeof statusEl !== "undefined") statusEl.textContent = "Error";
    } finally {
      if (importCycle === state.importCycle) {
        importing = false;
        syncActions();
      }
    }
  }

  async function pollLayerImport(jobId, importCycle) {
    try {
      const response = await fetch(`api/layers/import-jobs/${encodeURIComponent(jobId)}`);
      const payload = await response.json();
      if (importCycle !== state.importCycle) return;
      if (!response.ok) {
        throw new Error(payload.error || "Could not check layer import status");
      }

      showImportProgress(payload);
      if (payload.status === "complete") {
        activeImportJobId = "";
        addLoadedLayer(payload.layer || {});
        return;
      }

      if (payload.status === "error") {
        throw new Error(payload.error || "Layer import failed");
      }

      window.setTimeout(() => pollLayerImport(jobId, importCycle), 1500);
    } catch (error) {
      if (importCycle !== state.importCycle) return;
      activeImportJobId = "";
      importing = false;
      setDropzoneError(error.message);
      setMessage(error.message, "error");
      syncActions();
      if (typeof statusEl !== "undefined") statusEl.textContent = "Error";
    }
  }

  function openLayerPicker(autoUpload) {
    uploadAfterPick = autoUpload;
    try {
      if (typeof layerFile.showPicker === "function") {
        layerFile.showPicker();
        return;
      }
    } catch (_error) {
      // Fall back to the standard file input click below.
    }
    layerFile.click();
  }

  function applyLocalSelection(path) {
    selectedLocalPath = String(path || "").trim();
    try {
      layerFile.value = "";
    } catch (_error) {
      // Ignore browsers that do not allow clearing a file input here.
    }
    if (selectedLocalPath) {
      setDropzoneContent(localPathName(selectedLocalPath), `Ready to import from ${selectedLocalPath}`);
    } else {
      setDropzoneContent(defaultLayerTitle, defaultLayerSubtitle);
    }
  }

  function addLoadedLayer(payload) {
    importing = false;
    if (!payload?.id) {
      setDropzoneError("The layer import did not return a usable layer.");
      syncActions();
      return;
    }
    const entry = {
      ...payload,
      field: payload.default_field || "",
      colormap: "hazard",
      transparency: 55,
      boundaryWidth: payload.kind === "raster" ? 0 : 0.8,
      visible: true,
      importIndex: ++state.importCounter
    };
    state.layers.unshift(entry);
    state.activeId = entry.id;
    addMapLayers(entry);
    applyLayerOrder();
    syncControlsVisibility();
    renderLayerList();
    publishLayerChange();
    fitToExtent(entry.extent);
    dropzoneOpen = false;
    resetDropzone();
    renderImportedCards();
    syncActions();
    const kindCopy = entry.kind === "raster" ? "Raster ready." : "Vector layer ready.";
    setMessage(`${kindCopy} Alt/Option-click it on the map for details.`, "success");
    if (typeof statusEl !== "undefined") statusEl.textContent = "Ready";
    scheduleVectorRefresh(entry, { immediate: true });
  }

  function canAddLayer() {
    if (state.layers.length < MAX_LAYERS) return true;
    setMessage(`Up to ${MAX_LAYERS} layers can be added. Remove one before adding another.`, "error");
    return false;
  }

  function resetDropzone() {
    selectedLocalPath = "";
    uploadAfterPick = false;
    try {
      layerFile.value = "";
    } catch (_error) {
      // Ignore browsers that do not allow clearing a file input here.
    }
    setDropzoneContent(defaultLayerTitle, defaultLayerSubtitle);
  }

  function setDropzoneContent(title, subtitle, stateClass = "") {
    dropzone?.classList.remove("is-importing", "is-error");
    if (stateClass) dropzone?.classList.add(stateClass);
    layerFileTitle.textContent = title;
    layerFileSubtitle.textContent = subtitle;
    layerFileProgress?.classList.toggle("hidden", stateClass !== "is-importing");
  }

  function setDropzoneProgress(title, phase, percent) {
    const known = percent !== null && percent !== undefined && Number.isFinite(Number(percent));
    const value = known ? Math.max(0, Math.min(100, Number(percent))) : 0;
    setDropzoneContent(title || "Importing layer", known ? `${phase} · ${value.toFixed(0)}%` : phase, "is-importing");
    const fill = layerFileProgress?.firstElementChild;
    if (fill) {
      fill.style.width = known ? `${value}%` : "";
      fill.classList.toggle("is-indeterminate", !known);
    }
  }

  function setDropzoneError(message) {
    setDropzoneContent(layerFileTitle.textContent || defaultLayerTitle, message || "Layer import failed.", "is-error");
  }

  function syncActions() {
    const hasLayers = state.layers.length > 0;
    const showDropzone = dropzoneOpen || !hasLayers;
    dropzone?.classList.toggle("hidden", !showDropzone);
    uploadButton.classList.toggle("hidden", !showDropzone);
    addAnotherButton?.classList.toggle("hidden", showDropzone);
    removeSelectedButton?.classList.toggle("hidden", !hasLayers);
    for (const id of [...selectedForRemoval]) {
      if (!findLayer(id)) selectedForRemoval.delete(id);
    }
    const count = selectedForRemoval.size;
    if (removeSelectedButton) {
      removeSelectedButton.disabled = importing || count === 0;
      removeSelectedButton.textContent = count > 1 ? `Remove ${count} layers` : "Remove layer";
      removeSelectedButton.title = count ? "" : "Tick an imported layer to remove it";
    }
    if (addAnotherButton) addAnotherButton.disabled = importing;
    if (!importing && !activeImportJobId) {
      uploadButton.disabled = false;
      uploadButton.textContent = "Import layer";
    }
  }

  function renderImportedCards() {
    if (!importedListEl) return;
    const entries = [...state.layers].sort((a, b) => (a.importIndex || 0) - (b.importIndex || 0));
    importedListEl.classList.toggle("hidden", !entries.length);
    importedListEl.innerHTML = entries.map((entry) => {
      const name = htmlEscape(entry.name || "Layer");
      const selected = selectedForRemoval.has(entry.id);
      return `
        <label class="add-layer-imported-card${selected ? " is-selected" : ""}" data-layer-id="${htmlEscape(entry.id)}">
          <span class="add-layer-imported-text">
            <span class="add-layer-imported-name">${name}</span>
            <span class="add-layer-imported-status">\u2713 Successfully imported</span>
          </span>
          <input type="checkbox" data-action="select-remove" ${selected ? "checked" : ""} aria-label="Select ${name} for removal" title="Select to remove">
        </label>`;
    }).join("");
  }

  function removeSelectedLayers() {
    const ids = [...selectedForRemoval].filter((id) => findLayer(id));
    if (!ids.length) return;
    for (const id of ids) removeLayer(id, { silent: true });
    setMessage(`Removed ${ids.length} layer${ids.length > 1 ? "s" : ""}.`);
  }

  function localPathName(path) {
    const value = String(path || "").trim();
    if (!value) return "";
    const parts = value.split(/[\\/]/);
    return parts[parts.length - 1] || value;
  }

  function showImportProgress(payload) {
    const displayName = payload?.display_name || localPathName(payload?.path || selectedLocalPath) || "Selected layer";
    const phase = payload?.phase || payload?.status || "Importing layer…";
    setDropzoneProgress(displayName, phase, Number(payload?.percent || 0));
  }

  function legendMarkup(entry) {
    const bands = entry.bands || [];
    // Colourmaps are only applied to single-band tiled rasters.
    if (entry.kind !== "raster" || entry.render_mode === "image" || bands.length > 1) return "";
    const palette = legendPalettes[entry.colormap] || legendPalettes.hazard;
    const min = Number(bands[0]?.min);
    const max = Number(bands[0]?.max);
    const hasRange = bands[0]?.min != null && bands[0]?.max != null && Number.isFinite(min) && Number.isFinite(max);
    const labels = hasRange
      ? [min, (min + max) / 2, max].map(formatLegendValue)
      : ["Low", "", "High"];
    const bandName = String(bands[0]?.name || "");
    const title = bandName && !/^Band \d+$/.test(bandName) ? bandName : "Raster values";
    return `
      <div class="add-layer-legend" aria-label="Raster legend">
        <div class="add-layer-legend-title">${htmlEscape(title)}</div>
        <div class="add-layer-legend-bar" style="background: linear-gradient(to right, ${palette.join(", ")})"></div>
        <div class="add-layer-legend-labels">${labels.map((label) => `<span>${htmlEscape(label)}</span>`).join("")}</div>
      </div>
    `;
  }

  function formatLegendValue(value) {
    const abs = Math.abs(value);
    if (abs !== 0 && (abs >= 1e6 || abs < 1e-3)) return value.toExponential(2);
    return value.toLocaleString(undefined, { maximumFractionDigits: abs >= 100 ? 0 : abs >= 1 ? 2 : 3 });
  }

  function mapIdsFor(entry) {
    const prefix = `user-layer-${entry.id}`;
    if (entry.kind === "raster") {
      return {
        source: `${prefix}-raster-src`,
        boundarySource: `${prefix}-boundary-src`,
        layers: [`${prefix}-raster`, `${prefix}-boundary`]
      };
    }
    return {
      source: `${prefix}-vector-src`,
      layers: [`${prefix}-fill`, `${prefix}-outline`, `${prefix}-line`, `${prefix}-point`]
    };
  }

  function runtimeFor(entry) {
    if (!runtime.has(entry.id)) {
      runtime.set(entry.id, { refreshTimer: null, refreshRunning: false, refreshQueued: false, requestId: 0, popupHandler: null });
    }
    return runtime.get(entry.id);
  }

  function layoutFor(entry) {
    return { visibility: entry.visible ? "visible" : "none" };
  }

  function addMapLayers(entry) {
    if (typeof map === "undefined") return;
    if (entry.kind === "raster") {
      addRasterMapLayers(entry);
    } else {
      addVectorMapLayers(entry);
    }
  }

  function addVectorMapLayers(entry) {
    removeMapLayers(entry);
    const ids = mapIdsFor(entry);
    const [fillId, outlineId, lineId, pointId] = ids.layers;
    const beforeId = layerBeforeId();
    map.addSource(ids.source, { type: "geojson", data: emptyCollection });
    map.addLayer({
      id: fillId,
      type: "fill",
      source: ids.source,
      filter: ["==", "$type", "Polygon"],
      layout: layoutFor(entry),
      paint: {
        "fill-color": ["coalesce", ["get", "__color"], "#2563eb"],
        "fill-opacity": layerOpacity(entry, 0.78)
      }
    }, beforeId);
    map.addLayer({
      id: outlineId,
      type: "line",
      source: ids.source,
      filter: ["==", "$type", "Polygon"],
      layout: layoutFor(entry),
      paint: {
        "line-color": ["coalesce", ["get", "__color"], "#1d4ed8"],
        "line-width": Number(entry.boundaryWidth ?? 0.8),
        "line-opacity": 0.95
      }
    }, beforeId);
    map.addLayer({
      id: lineId,
      type: "line",
      source: ids.source,
      filter: ["==", "$type", "LineString"],
      layout: layoutFor(entry),
      paint: {
        "line-color": ["coalesce", ["get", "__color"], "#2563eb"],
        "line-width": 2.2,
        "line-opacity": layerOpacity(entry, 0.95)
      }
    }, beforeId);
    map.addLayer({
      id: pointId,
      type: "circle",
      source: ids.source,
      filter: ["==", "$type", "Point"],
      layout: layoutFor(entry),
      paint: {
        "circle-color": ["coalesce", ["get", "__color"], "#2563eb"],
        "circle-radius": [
          "interpolate",
          ["linear"],
          ["zoom"],
          4, 3,
          12, 5,
          18, 7
        ],
        "circle-opacity": layerOpacity(entry, 0.94),
        "circle-stroke-color": "#ffffff",
        "circle-stroke-width": 1
      }
    }, beforeId);

    const rt = runtimeFor(entry);
    rt.popupHandler = (event) => showFeaturePopup(event, entry.id);
    for (const layerId of ids.layers) {
      map.on("click", layerId, rt.popupHandler);
      map.on("mouseenter", layerId, setPointerCursor);
      map.on("mouseleave", layerId, resetPointerCursor);
    }
  }

  function addRasterMapLayers(entry) {
    removeMapLayers(entry);
    const ids = mapIdsFor(entry);
    if (entry.render_mode === "image" && entry.image_url && entry.image_coordinates) {
      map.addSource(ids.source, {
        type: "image",
        url: entry.image_url,
        coordinates: entry.image_coordinates
      });
    } else {
      const bounds = entry.extent
        ? [entry.extent.min_lon, entry.extent.min_lat, entry.extent.max_lon, entry.extent.max_lat]
        : undefined;
      map.addSource(ids.source, {
        type: "raster",
        tiles: [rasterTileUrl(entry)],
        tileSize: 256,
        minzoom: entry.min_zoom || 0,
        maxzoom: entry.max_zoom || 18,
        bounds: bounds
      });
    }
    map.addLayer({
      id: ids.layers[0],
      type: "raster",
      source: ids.source,
      layout: layoutFor(entry),
      paint: {
        "raster-opacity": layerOpacity(entry, 1)
      }
    }, layerBeforeId());

    addRasterBoundary(entry);
  }

  function addRasterBoundary(entry) {
    const ids = mapIdsFor(entry);
    const strength = Number(entry.boundaryWidth || 0);
    if (strength <= 0 || entry.render_mode === "image" || !entry.tile_url) return;
    const bounds = entry.extent
      ? [entry.extent.min_lon, entry.extent.min_lat, entry.extent.max_lon, entry.extent.max_lat]
      : undefined;
    const base = entry.tile_url;
    map.addSource(ids.boundarySource, {
      type: "raster",
      tiles: [base + (base.includes("?") ? "&" : "?") + "boundary=1"],
      tileSize: 256,
      minzoom: entry.min_zoom || 0,
      maxzoom: entry.max_zoom || 18,
      bounds: bounds
    });
    map.addLayer({
      id: ids.layers[1],
      type: "raster",
      source: ids.boundarySource,
      layout: layoutFor(entry),
      paint: {
        "raster-opacity": strength,
        "raster-resampling": "nearest"
      }
    }, layerBeforeId());
  }

  function updateBoundaryWidth(entry) {
    if (typeof map === "undefined") return;
    const ids = mapIdsFor(entry);
    const value = Number(entry.boundaryWidth || 0);
    if (entry.kind === "raster") {
      // Raster edges are a 1 px line; the 0-1 value controls how strongly it shows.
      if (value > 0 && map.getLayer(ids.layers[1])) {
        map.setPaintProperty(ids.layers[1], "raster-opacity", value);
        return;
      }
      if (map.getLayer(ids.layers[1])) map.removeLayer(ids.layers[1]);
      if (map.getSource(ids.boundarySource)) map.removeSource(ids.boundarySource);
      if (value > 0) {
        addRasterBoundary(entry);
        applyLayerOrder();
      }
      return;
    }
    if (map.getLayer(ids.layers[1])) map.setPaintProperty(ids.layers[1], "line-width", value);
  }

  function settingValueLabel(setting, value) {
    if (setting === "transparency") return `${value}%`;
    return value > 0 ? value.toFixed(1) : "Off";
  }

  function removeMapLayers(entry) {
    if (typeof map === "undefined") return;
    const ids = mapIdsFor(entry);
    const rt = runtime.get(entry.id);
    for (const layerId of ids.layers) {
      if (rt?.popupHandler) {
        map.off("click", layerId, rt.popupHandler);
        map.off("mouseenter", layerId, setPointerCursor);
        map.off("mouseleave", layerId, resetPointerCursor);
      }
      if (map.getLayer(layerId)) map.removeLayer(layerId);
    }
    if (rt) rt.popupHandler = null;
    if (map.getSource(ids.source)) map.removeSource(ids.source);
    if (ids.boundarySource && map.getSource(ids.boundarySource)) map.removeSource(ids.boundarySource);
  }

  function setPointerCursor() {
    map.getCanvas().style.cursor = "pointer";
  }

  function resetPointerCursor() {
    map.getCanvas().style.cursor = "";
  }

  function applyLayerOrder() {
    if (typeof map === "undefined") return;
    const beforeId = layerBeforeId();
    // Move bottom-most first so each later move lands above the previous one.
    for (let index = state.layers.length - 1; index >= 0; index -= 1) {
      for (const layerId of mapIdsFor(state.layers[index]).layers) {
        if (map.getLayer(layerId)) map.moveLayer(layerId, beforeId);
      }
    }
    window.applyOverlayLayerOrder?.();
  }

  function rasterTileUrl(entry) {
    const base = entry.tile_url || "";
    return base + (base.includes("?") ? "&" : "?") + "colormap=" + encodeURIComponent(entry.colormap || "hazard");
  }

  function findLayer(id) {
    return state.layers.find((entry) => entry.id === id) || null;
  }

  function syncControlsVisibility() {
    controls.classList.toggle("hidden", !state.layers.length);
  }

  function setLayerField(entry, field) {
    if (entry.kind !== "vector") return;
    entry.field = field;
    publishLayerChange();
    scheduleVectorRefresh(entry, { immediate: true });
  }

  function setLayerColormap(entry, colormap) {
    entry.colormap = colormap;
    if (entry.kind === "raster") {
      addRasterMapLayers(entry);
      applyLayerOrder();
      renderLayerList();
    } else {
      scheduleVectorRefresh(entry, { immediate: true });
    }
  }

  function setLayerVisible(id, visible) {
    const entry = findLayer(id);
    if (!entry) return;
    entry.visible = Boolean(visible);
    if (typeof map !== "undefined") {
      for (const layerId of mapIdsFor(entry).layers) {
        if (map.getLayer(layerId)) map.setLayoutProperty(layerId, "visibility", entry.visible ? "visible" : "none");
      }
    }
    scheduleVectorRefresh(entry, { immediate: true });
    renderLayerList();
    publishLayerChange();
  }

  function moveLayerBy(id, delta) {
    const index = state.layers.findIndex((entry) => entry.id === id);
    const target = index + delta;
    if (index < 0 || target < 0 || target >= state.layers.length) return;
    const [entry] = state.layers.splice(index, 1);
    state.layers.splice(target, 0, entry);
    applyLayerOrder();
    renderLayerList();
    publishLayerChange();
  }

  function renderLayerList() {
    if (!layerListEl) return;
    if (!state.layers.length) {
      layerListPanel?.classList.add("hidden");
      layerListEl.innerHTML = "";
      return;
    }
    const lastIndex = state.layers.length - 1;
    const rows = state.layers.map((entry, index) => {
      const name = htmlEscape(entry.name || "Layer");
      const isRaster = entry.kind === "raster";
      const colormapHtml = colormapOptions
        .map(([value, label]) => `<option value="${value}" ${value === (entry.colormap || "hazard") ? "selected" : ""}>${label}</option>`)
        .join("");
      return `
        <li class="add-layer-item${entry.visible ? "" : " is-hidden"}" data-layer-id="${htmlEscape(entry.id)}">
          <div class="add-layer-item-head">
            <input type="checkbox" data-action="toggle" ${entry.visible ? "checked" : ""} aria-label="Show ${name}" title="Show or hide">
            <div class="add-layer-item-name" title="${name}">
              <span>${name}</span>
              <small>${isRaster ? "Raster" : "Vector"}</small>
            </div>
            <button type="button" class="add-layer-item-icon" data-action="up" ${index === 0 ? "disabled" : ""} aria-label="Move ${name} up" title="Move up">&#9650;</button>
            <button type="button" class="add-layer-item-icon" data-action="down" ${index === lastIndex ? "disabled" : ""} aria-label="Move ${name} down" title="Move down">&#9660;</button>
            <button type="button" class="add-layer-item-icon is-remove" data-action="remove" aria-label="Remove ${name}" title="Remove layer">&times;</button>
          </div>
          <div class="add-layer-item-settings">
            <label>
              <span>Displayed field</span>
              <select data-setting="field" ${isRaster || !(entry.fields || []).length ? "disabled" : ""}>${fieldOptionsMarkup(entry)}</select>
            </label>
            <label>
              <span>Colourmap</span>
              <select data-setting="colormap">${colormapHtml}</select>
            </label>
            <label>
              <span class="add-layer-setting-head"><span>Transparency</span><output>${settingValueLabel("transparency", Number(entry.transparency ?? 55))}</output></span>
              <input type="range" data-setting="transparency" min="0" max="100" step="1" value="${Number(entry.transparency ?? 55)}">
            </label>
            <label title="${isRaster ? "Outline along the edges of the raster data" : "Outline of each polygon"}; stays visible at 100% transparency">
              <span class="add-layer-setting-head"><span>Boundary</span><output>${settingValueLabel("boundary", Number(entry.boundaryWidth ?? 0))}</output></span>
              <input type="range" data-setting="boundary" min="0" max="1" step="0.1" value="${Number(entry.boundaryWidth ?? 0)}" ${isRaster && entry.render_mode === "image" ? "disabled" : ""}>
            </label>
            ${legendMarkup(entry)}
          </div>
        </li>`;
    }).join("");
    layerListEl.innerHTML = rows;
    layerListPanel?.classList.remove("hidden");
  }

  function fieldOptionsMarkup(entry) {
    if (entry.kind === "raster") return '<option value="">Raster values</option>';
    const options = ['<option value="">No field</option>'];
    for (const field of entry.fields || []) {
      const label = `${fieldLabel(field.name)}${field.numeric ? " (numeric)" : ""}`;
      const selected = field.name === entry.field ? "selected" : "";
      options.push(`<option value="${htmlEscape(field.name)}" ${selected}>${htmlEscape(label)}</option>`);
    }
    return options.join("");
  }

  function scheduleAllVectorRefresh(options) {
    for (const entry of state.layers) scheduleVectorRefresh(entry, options);
  }

  function scheduleVectorRefresh(entry, { immediate = false } = {}) {
    if (!entry || entry.kind !== "vector" || !entry.visible || typeof map === "undefined" || !map.isStyleLoaded()) return;
    const rt = runtimeFor(entry);

    if (immediate) {
      if (rt.refreshTimer) {
        window.clearTimeout(rt.refreshTimer);
        rt.refreshTimer = null;
      }
      refreshVectorLayer(entry);
      return;
    }

    if (rt.refreshTimer) return;
    rt.refreshTimer = window.setTimeout(() => {
      rt.refreshTimer = null;
      refreshVectorLayer(entry);
    }, 180);
  }

  async function refreshVectorLayer(entry) {
    if (!findLayer(entry.id) || !entry.visible) return;
    const rt = runtimeFor(entry);
    if (rt.refreshRunning) {
      rt.refreshQueued = true;
      return;
    }

    const requestId = ++rt.requestId;
    const bounds = map.getBounds();
    const canvas = map.getCanvas();
    const params = new URLSearchParams({
      min_lon: String(bounds.getWest()),
      min_lat: String(bounds.getSouth()),
      max_lon: String(bounds.getEast()),
      max_lat: String(bounds.getNorth()),
      width: String(canvas.clientWidth || 1200),
      height: String(canvas.clientHeight || 800),
      zoom: String(map.getZoom()),
      field: entry.field || "",
      colormap: entry.colormap || "hazard"
    });

    rt.refreshRunning = true;
    try {
      const response = await fetch(`api/layers/${encodeURIComponent(entry.id)}/features?${params.toString()}`);
      const payload = await response.json();
      if (requestId !== rt.requestId || !findLayer(entry.id)) return;
      if (!response.ok) {
        throw new Error(payload.error || "Could not load layer features");
      }

      map.getSource(mapIdsFor(entry).source)?.setData({
        type: "FeatureCollection",
        features: payload.features || []
      });

      if (entry.id === state.activeId) {
        const visible = Number(payload.visible_count || 0);
        const returned = Number(payload.returned_count || 0);
        const clipped = payload.truncated ? " · zoom in for detail" : "";
        setMessage(`${integerFormat(visible)} visible · ${integerFormat(returned)} drawn${clipped}`, "success");
      }
    } catch (error) {
      if (requestId !== rt.requestId || !findLayer(entry.id)) return;
      map.getSource(mapIdsFor(entry).source)?.setData(emptyCollection);
      if (entry.id === state.activeId) setMessage(error.message, "error");
    } finally {
      rt.refreshRunning = false;
      if (rt.refreshQueued && findLayer(entry.id)) {
        rt.refreshQueued = false;
        scheduleVectorRefresh(entry, { immediate: true });
      }
    }
  }

  function updateLayerOpacity(entry) {
    if (typeof map === "undefined") return;
    const ids = mapIdsFor(entry);
    const setOpacity = (layerId, property, base) => {
      if (map.getLayer(layerId)) map.setPaintProperty(layerId, property, layerOpacity(entry, base));
    };
    if (entry.kind === "raster") {
      setOpacity(ids.layers[0], "raster-opacity", 1);
      return;
    }
    const [fillId, , lineId, pointId] = ids.layers;
    setOpacity(fillId, "fill-opacity", 0.78);
    setOpacity(lineId, "line-opacity", 0.95);
    setOpacity(pointId, "circle-opacity", 0.95);
  }

  function layerOpacity(entry, base) {
    const transparency = Number(entry.transparency ?? 55);
    const opacity = Math.max(0, Math.min(0.95, (100 - transparency) / 100));
    return opacity * base;
  }

  function fitToExtent(extent) {
    if (!extent || typeof map === "undefined") return;
    const minLon = Number(extent.min_lon);
    const minLat = Number(extent.min_lat);
    const maxLon = Number(extent.max_lon);
    const maxLat = Number(extent.max_lat);
    if (![minLon, minLat, maxLon, maxLat].every(Number.isFinite)) return;

    if (Math.abs(maxLon - minLon) < 0.00005 && Math.abs(maxLat - minLat) < 0.00005) {
      map.flyTo({
        center: [(minLon + maxLon) / 2, (minLat + maxLat) / 2],
        zoom: Math.max(map.getZoom(), 15),
        speed: 1.4
      });
      return;
    }

    map.fitBounds([[minLon, minLat], [maxLon, maxLat]], {
      padding: 72,
      maxZoom: 15,
      duration: 650
    });
  }

  function showFeaturePopup(event, layerId) {
    const original = event.originalEvent || {};
    if (!original.altKey) return;
    const feature = event.features && event.features[0];
    const entry = findLayer(layerId);
    if (!feature || !entry) return;
    event.preventDefault();

    const field = feature.properties.display_field || "Value";
    const value = feature.properties.display_value || "n/a";
    const html = `
      <strong>${htmlEscape(entry.name || "Layer")}</strong>
      <span>${htmlEscape(fieldLabel(field))}: ${htmlEscape(value)}</span>
    `;

    if (state.popup) state.popup.remove();
    state.popup = new maplibregl.Popup({ closeButton: true, closeOnClick: true })
      .setLngLat(event.lngLat)
      .setHTML(`<div class="add-layer-popup">${html}</div>`)
      .addTo(map);
  }

  function layerBeforeId() {
    return typeof map !== "undefined" && map.getLayer(buildingOverlayLayerId)
      ? buildingOverlayLayerId
      : undefined;
  }

  function removeLayer(id, { silent = false } = {}) {
    const index = state.layers.findIndex((entry) => entry.id === id);
    if (index < 0) return;
    const [entry] = state.layers.splice(index, 1);
    const rt = runtime.get(id);
    if (rt?.refreshTimer) window.clearTimeout(rt.refreshTimer);
    removeMapLayers(entry);
    runtime.delete(id);
    deleteLayerOnServer(id);
    if (state.popup) {
      state.popup.remove();
      state.popup = null;
    }
    if (state.activeId === id) state.activeId = state.layers[0]?.id || "";
    selectedForRemoval.delete(id);
    if (!state.layers.length) dropzoneOpen = true;
    syncControlsVisibility();
    renderLayerList();
    renderImportedCards();
    syncActions();
    publishLayerChange();
    if (!silent) {
      setMessage(state.layers.length
        ? `Removed ${entry.name || "layer"}.`
        : "Upload a layer to display it over buildings and exposure points.");
    }
  }

  function removeAllLayers() {
    for (const entry of [...state.layers]) removeLayer(entry.id, { silent: true });
  }

  function resetLayerUi() {
    activeImportJobId = "";
    importing = false;
    state.importCycle += 1;
    removeAllLayers();
    dropzoneOpen = true;
    resetDropzone();
    setMessage("Upload a layer to display it over buildings and exposure points.");
    syncActions();
  }

  async function deleteLayerOnServer(layerId) {
    try {
      await fetch(`/api/layers/${encodeURIComponent(layerId)}`, {
        method: "DELETE",
        keepalive: true
      });
    } catch (_error) {
      // The UI is already cleared; server-side session cleanup can be retried by replacement.
    }
  }

  function publishLayerChange() {
    const layers = state.layers.map((entry) => ({ ...entry }));
    window.currentAddedMapLayers = layers;
    window.currentAddedMapLayer = layers.find((entry) => entry.id === state.activeId) || null;
    window.dispatchEvent(new CustomEvent("added-map-layer-change", {
      detail: window.currentAddedMapLayer
    }));
  }

  function setMessage(message, type = "") {
    messageEl.textContent = message;
    messageEl.classList.toggle("error", type === "error");
    messageEl.classList.toggle("success", type === "success");
  }

  function fieldLabel(field) {
    return typeof formatFieldLabel === "function"
      ? formatFieldLabel(field)
      : String(field || "").replaceAll("_", " ").replace(/\b\w/g, (character) => character.toUpperCase());
  }

  function htmlEscape(value) {
    if (typeof escapeHtml === "function") return escapeHtml(value);
    return String(value)
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;")
      .replaceAll("'", "&#039;");
  }

  function integerFormat(value) {
    return typeof formatInteger === "function"
      ? formatInteger(value)
      : Number(value || 0).toLocaleString();
  }

  function formatBytes(value) {
    const bytes = Number(value || 0);
    if (!Number.isFinite(bytes) || bytes < 0) return "n/a";
    if (bytes >= 1024 ** 3) return `${(bytes / (1024 ** 3)).toFixed(2)} GB`;
    if (bytes >= 1024 ** 2) return `${(bytes / (1024 ** 2)).toFixed(0)} MB`;
    if (bytes >= 1024) return `${(bytes / 1024).toFixed(0)} KB`;
    return `${bytes} bytes`;
  }

  window.getImportedMapLayerIds = () => state.layers
    .slice()
    .reverse()
    .flatMap((entry) => mapIdsFor(entry).layers);
  window.addedMapLayerController = {
    clear: removeAllLayers,
    reset: resetLayerUi
  };
})();
