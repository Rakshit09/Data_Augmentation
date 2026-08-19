(function () {
  const layerFile = document.getElementById("addLayerFile");
  const layerFileTitle = document.getElementById("addLayerFileTitle");
  const layerFileSubtitle = document.getElementById("addLayerFileSubtitle");
  const dropzone = document.querySelector('label.add-layer-dropzone[for="addLayerFile"]');
  const uploadButton = document.getElementById("uploadMapLayer");
  const controls = document.getElementById("addLayerControls");
  const fieldSelect = document.getElementById("addLayerField");
  const colormapSelect = document.getElementById("addLayerColormap");
  const transparencyInput = document.getElementById("addLayerOpacity");
  const clearButton = document.getElementById("clearMapLayer");
  const messageEl = document.getElementById("addLayerMessage");
  const importStatusEl = document.getElementById("layerImportStatus");
  const maxUploadBytes = Number(layerFile?.dataset.maxBytes || 0);

  const vectorSourceId = "user-added-vector-layer";
  const rasterSourceId = "user-added-raster-layer";
  const vectorLayerIds = {
    fill: "user-added-vector-fill",
    outline: "user-added-vector-outline",
    line: "user-added-vector-line",
    point: "user-added-vector-point"
  };
  const rasterLayerId = "user-added-raster-layer";
  const buildingOverlayLayerId = "view-filter-buildings-fill";
  const emptyCollection = { type: "FeatureCollection", features: [] };

  const state = {
    layer: null,
    refreshTimer: null,
    refreshRunning: false,
    refreshQueued: false,
    requestId: 0,
    popup: null,
    importCycle: 0
  };
  const defaultLayerTitle = "Choose Layer File";
  const defaultLayerSubtitle = "GeoPackage, zipped shapefile, shapefile (.shp), GeoJSON, or GeoTIFF";
  let selectedLocalPath = "";
  let activeImportJobId = "";
  let uploadAfterPick = false;
  publishLayerChange();

  if (!layerFile || !uploadButton || !fieldSelect || !colormapSelect || !transparencyInput) {
    return;
  }

  if (typeof map !== "undefined") {
    if (map.loaded()) {
      initLayerSources();
    } else {
      map.on("load", initLayerSources);
    }

    map.on("move", () => scheduleVectorRefresh());
    map.on("moveend", () => scheduleVectorRefresh({ immediate: true }));
  }

  layerFile.addEventListener("change", () => {
    selectedLocalPath = "";
    const files = Array.from(layerFile.files || []);
    const primaryName = files[0] ? files[0].name : "";
    layerFileTitle.textContent = files.length > 1 ? `${files.length} files selected` : (primaryName || defaultLayerTitle);
    layerFileSubtitle.textContent = files.length
      ? "Click Add layer to render it on the map"
      : defaultLayerSubtitle;
    if (!files.length) {
      uploadAfterPick = false;
      return;
    }
    if (uploadAfterPick) {
      uploadAfterPick = false;
      uploadLayerFromBrowser();
    }
  });

  dropzone?.addEventListener("click", (event) => {
    event.preventDefault();
    chooseLocalLayer(false);
  });
  uploadButton.addEventListener("click", handleAddLayerClick);
  clearButton?.addEventListener("click", () => clearLayer());
  fieldSelect.addEventListener("change", () => {
    if (!state.layer) return;
    state.layer.field = fieldSelect.value;
    publishLayerChange();
    scheduleVectorRefresh({ immediate: true });
  });
  colormapSelect.addEventListener("change", () => {
    if (!state.layer) return;
    state.layer.colormap = colormapSelect.value;
    if (state.layer.kind === "raster") {
      reloadRasterTiles();
    } else {
      scheduleVectorRefresh({ immediate: true });
    }
  });
  transparencyInput.addEventListener("input", updateLayerOpacity);

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
    if (activeImportJobId) return;
    uploadButton.disabled = true;
    const originalLabel = uploadButton.textContent;
    uploadButton.innerHTML = '<span class="spinner"></span> Selecting...';
    setMessage("Opening layer file picker...");

    try {
      const response = await fetch("api/browse-file?kind=layer");
      const payload = await response.json();
      if (!response.ok) {
        if (response.status === 501) {
          hideImportStatus();
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
        await importLocalLayer();
      } else {
        setMessage("Layer selected. Click Import layer to load it.", "success");
      }
    } catch (error) {
      setMessage(error.message, "error");
    } finally {
      if (importCycle === state.importCycle && !activeImportJobId) {
        uploadButton.disabled = false;
        uploadButton.textContent = originalLabel;
      }
    }
  }

  async function importLocalLayer() {
    if (!selectedLocalPath) {
      setMessage("Choose a layer file to import.", "error");
      return;
    }

    const importCycle = ++state.importCycle;
    if (typeof dismissCriticalNote === "function") dismissCriticalNote();
    uploadButton.disabled = true;
    const originalLabel = uploadButton.textContent;
    uploadButton.innerHTML = '<span class="spinner"></span> Importing...';
    if (typeof statusEl !== "undefined") statusEl.textContent = "Adding layer";
    setMessage("Submitting local layer import...");

    const previousLayerId = state.layer?.id || "";
    try {
      const response = await fetch("api/layers/import-local", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          path: selectedLocalPath,
          replace_layer_id: previousLayerId
        })
      });
      const payload = await response.json();
      if (!response.ok) {
        throw new Error(payload.error || "Could not import layer");
      }
      if (importCycle !== state.importCycle) return;

      activeImportJobId = payload.job_id || "";
      showImportProgress(payload);
      setMessage(payload.phase || "Importing layer...");
      pollLayerImport(activeImportJobId, originalLabel, importCycle);
    } catch (error) {
      if (importCycle !== state.importCycle) return;
      activeImportJobId = "";
      uploadButton.disabled = false;
      uploadButton.textContent = originalLabel;
      showImportError(error.message);
      setMessage(error.message, "error");
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
    uploadButton.disabled = true;
    const originalLabel = uploadButton.textContent;
    uploadButton.innerHTML = '<span class="spinner"></span> Uploading\u2026';
    if (typeof statusEl !== "undefined") statusEl.textContent = "Adding layer";
    setMessage("Preparing layer for smooth map rendering...");

    const formData = new FormData();
    for (const file of files) {
      formData.append("file", file);
    }
    const previousLayerId = state.layer?.id || "";
    if (previousLayerId) {
      formData.append("replace_layer_id", previousLayerId);
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

      clearLayer({ silent: true, cleanupServer: false, invalidatePending: false });
      state.layer = {
        ...payload,
        field: payload.default_field || "",
        colormap: colormapSelect.value || "hazard"
      };

      if (payload.kind === "raster") {
        activateRasterLayer(payload);
      } else {
        activateVectorLayer(payload);
      }

      controls.classList.remove("hidden");
      publishLayerChange();
      fitToExtent(payload.extent);
      if (payload.kind === "raster") {
        setMessage(`Raster ready: ${payload.name || "uploaded layer"}. Click buildings normally; Alt/Option-click the layer for its popup.`, "success");
      } else {
        setMessage(`Vector layer ready: ${payload.name || "uploaded layer"}. Click buildings normally; Alt/Option-click the layer for its popup.`, "success");
      }
      if (typeof statusEl !== "undefined") statusEl.textContent = "Ready";
    } catch (error) {
      if (importCycle !== state.importCycle) return;
      if (state.layer && state.layer.id !== previousLayerId) {
        clearLayer({ silent: true });
      }
      setMessage(error.message, "error");
      if (typeof statusEl !== "undefined") statusEl.textContent = "Error";
    } finally {
      if (importCycle === state.importCycle) {
        uploadButton.disabled = false;
        uploadButton.textContent = originalLabel;
      }
    }
  }

  async function pollLayerImport(jobId, originalLabel, importCycle) {
    try {
      const response = await fetch(`api/layers/import-jobs/${encodeURIComponent(jobId)}`);
      const payload = await response.json();
      if (importCycle !== state.importCycle) return;
      if (!response.ok) {
        throw new Error(payload.error || "Could not check layer import status");
      }

      showImportProgress(payload);
      setMessage(payload.phase || "Importing layer...");
      if (payload.status === "complete") {
        activeImportJobId = "";
        uploadButton.disabled = false;
        uploadButton.textContent = originalLabel;
        showImportSuccess(payload);
        applyLoadedLayer(payload.layer || {});
        return;
      }

      if (payload.status === "error") {
        throw new Error(payload.error || "Layer import failed");
      }

      window.setTimeout(() => pollLayerImport(jobId, originalLabel, importCycle), 1500);
    } catch (error) {
      if (importCycle !== state.importCycle) return;
      activeImportJobId = "";
      uploadButton.disabled = false;
      uploadButton.textContent = originalLabel;
      showImportError(error.message);
      setMessage(error.message, "error");
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
    const filename = localPathName(selectedLocalPath);
    layerFileTitle.textContent = filename || defaultLayerTitle;
    layerFileSubtitle.textContent = selectedLocalPath
      ? `Ready to import from ${selectedLocalPath}`
      : defaultLayerSubtitle;
  }

  function applyLoadedLayer(payload) {
    clearLayer({ silent: true, cleanupServer: false, invalidatePending: false });
    state.layer = {
      ...payload,
      field: payload.default_field || "",
      colormap: colormapSelect.value || "hazard"
    };

    if (payload.kind === "raster") {
      activateRasterLayer(payload);
    } else {
      activateVectorLayer(payload);
    }

    controls.classList.remove("hidden");
    publishLayerChange();
    fitToExtent(payload.extent);
    if (payload.kind === "raster") {
      setMessage(`Raster ready: ${payload.name || "uploaded layer"}. Click buildings normally; Alt/Option-click the layer for its popup.`, "success");
    } else {
      setMessage(`Vector layer ready: ${payload.name || "uploaded layer"}. Click buildings normally; Alt/Option-click the layer for its popup.`, "success");
    }
    if (typeof statusEl !== "undefined") statusEl.textContent = "Ready";
  }

  function localPathName(path) {
    const value = String(path || "").trim();
    if (!value) return "";
    const parts = value.split(/[\\/]/);
    return parts[parts.length - 1] || value;
  }

  function showImportProgress(payload) {
    if (!importStatusEl) return;
    const percent = Math.max(0, Math.min(100, Number(payload?.percent || 0)));
    const phase = payload?.phase || payload?.status || "Importing layer...";
    const displayName = payload?.display_name || localPathName(payload?.path || selectedLocalPath) || "Selected layer";
    const path = payload?.path || selectedLocalPath;

    importStatusEl.classList.remove("hidden", "etl-status--error", "etl-status--success");
    importStatusEl.innerHTML = `
      <strong>${htmlEscape(displayName)}</strong><br>
      <div class="progress-copy">${htmlEscape(phase)}</div>
      <div class="progress-track"><div class="progress-fill" style="width:${percent}%"></div></div>
      <div class="progress-copy">${percent.toFixed(0)}%</div>
      ${path ? `<div class="progress-copy">${htmlEscape(path)}</div>` : ""}
    `;
  }

  function showImportSuccess(payload) {
    if (!importStatusEl) return;
    const layer = payload?.layer || {};
    const layerName = layer.name || payload?.display_name || "Layer";
    importStatusEl.classList.remove("hidden", "etl-status--error");
    importStatusEl.classList.add("etl-status--success");
    importStatusEl.innerHTML = `
      <strong>${htmlEscape(layerName)} ready.</strong><br>
      <div class="progress-copy">${htmlEscape(payload?.phase || "Layer ready.")}</div>
      ${payload?.path ? `<div class="progress-copy">${htmlEscape(payload.path)}</div>` : ""}
    `;
  }

  function showImportError(message) {
    if (!importStatusEl) return;
    importStatusEl.classList.remove("hidden", "etl-status--success");
    importStatusEl.classList.add("etl-status--error");
    importStatusEl.innerHTML = `<div class="progress-copy">${htmlEscape(message || "Layer import failed.")}</div>`;
  }

  function hideImportStatus() {
    if (!importStatusEl) return;
    importStatusEl.classList.add("hidden");
    importStatusEl.classList.remove("etl-status--error", "etl-status--success");
    importStatusEl.innerHTML = "";
  }

  function initLayerSources() {
    if (!map.getSource(vectorSourceId)) {
      map.addSource(vectorSourceId, {
        type: "geojson",
        data: emptyCollection
      });
    }

    if (!map.getLayer(vectorLayerIds.fill)) {
      map.addLayer({
        id: vectorLayerIds.fill,
        type: "fill",
        source: vectorSourceId,
        filter: ["==", "$type", "Polygon"],
        paint: {
          "fill-color": ["coalesce", ["get", "__color"], "#2563eb"],
          "fill-opacity": activeOpacity(0.78)
        }
      }, layerBeforeId());
    }

    if (!map.getLayer(vectorLayerIds.outline)) {
      map.addLayer({
        id: vectorLayerIds.outline,
        type: "line",
        source: vectorSourceId,
        filter: ["==", "$type", "Polygon"],
        paint: {
          "line-color": ["coalesce", ["get", "__color"], "#1d4ed8"],
          "line-width": 1.2,
          "line-opacity": activeOpacity(0.92)
        }
      }, layerBeforeId());
    }

    if (!map.getLayer(vectorLayerIds.line)) {
      map.addLayer({
        id: vectorLayerIds.line,
        type: "line",
        source: vectorSourceId,
        filter: ["==", "$type", "LineString"],
        paint: {
          "line-color": ["coalesce", ["get", "__color"], "#2563eb"],
          "line-width": 2.2,
          "line-opacity": activeOpacity(0.95)
        }
      }, layerBeforeId());
    }

    if (!map.getLayer(vectorLayerIds.point)) {
      map.addLayer({
        id: vectorLayerIds.point,
        type: "circle",
        source: vectorSourceId,
        filter: ["==", "$type", "Point"],
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
          "circle-opacity": activeOpacity(0.94),
          "circle-stroke-color": "#ffffff",
          "circle-stroke-width": 1
        }
      }, layerBeforeId());
    }

    for (const layerId of Object.values(vectorLayerIds)) {
      map.on("click", layerId, showFeaturePopup);
      map.on("mouseenter", layerId, () => { map.getCanvas().style.cursor = "pointer"; });
      map.on("mouseleave", layerId, () => { map.getCanvas().style.cursor = ""; });
    }

    window.applyOverlayLayerOrder?.();
  }

  function activateVectorLayer(layer) {
    removeRasterLayer();
    populateFieldOptions(layer.fields || [], layer.default_field || "");
    fieldSelect.disabled = !((layer.fields || []).length);
    colormapSelect.disabled = false;
    window.applyOverlayLayerOrder?.();
    scheduleVectorRefresh({ immediate: true });
  }

  function activateRasterLayer(layer) {
    clearVectorSource();
    fieldSelect.innerHTML = '<option value="">Raster values</option>';
    fieldSelect.disabled = true;
    colormapSelect.disabled = false;
    removeRasterLayer();

    if (layer.render_mode === "image" && layer.image_url && layer.image_coordinates) {
      map.addSource(rasterSourceId, {
        type: "image",
        url: layer.image_url,
        coordinates: layer.image_coordinates
      });
      map.addLayer({
        id: rasterLayerId,
        type: "raster",
        source: rasterSourceId,
        paint: {
          "raster-opacity": activeOpacity(1)
        }
      }, layerBeforeId());
      window.applyOverlayLayerOrder?.();
      return;
    }

    const bounds = layer.extent
      ? [layer.extent.min_lon, layer.extent.min_lat, layer.extent.max_lon, layer.extent.max_lat]
      : undefined;
    const tileUrl = rasterTileUrl(layer);
    map.addSource(rasterSourceId, {
      type: "raster",
      tiles: [tileUrl],
      tileSize: 256,
      minzoom: layer.min_zoom || 0,
      maxzoom: layer.max_zoom || 18,
      bounds: bounds
    });
    map.addLayer({
      id: rasterLayerId,
      type: "raster",
      source: rasterSourceId,
      paint: {
        "raster-opacity": activeOpacity(1)
      }
    }, layerBeforeId());
    window.applyOverlayLayerOrder?.();
  }

  function rasterTileUrl(layer) {
    const cm = (layer || state.layer || {}).colormap || colormapSelect.value || "hazard";
    const base = (layer || state.layer || {}).tile_url || "";
    return base + (base.includes("?") ? "&" : "?") + "colormap=" + encodeURIComponent(cm);
  }

  function reloadRasterTiles() {
    if (!state.layer || state.layer.kind !== "raster") return;
    removeRasterLayer();
    activateRasterLayer(state.layer);
  }

  function populateFieldOptions(fields, selectedField) {
    const options = ['<option value="">No field</option>'];
    for (const field of fields) {
      const label = `${fieldLabel(field.name)}${field.numeric ? " (numeric)" : ""}`;
      options.push(`<option value="${htmlEscape(field.name)}">${htmlEscape(label)}</option>`);
    }
    fieldSelect.innerHTML = options.join("");
    fieldSelect.value = selectedField || "";
  }

  function scheduleVectorRefresh({ immediate = false } = {}) {
    if (!state.layer || state.layer.kind !== "vector" || typeof map === "undefined" || !map.isStyleLoaded()) return;

    if (immediate) {
      if (state.refreshTimer) {
        window.clearTimeout(state.refreshTimer);
        state.refreshTimer = null;
      }
      refreshVectorLayer();
      return;
    }

    if (state.refreshTimer) return;
    state.refreshTimer = window.setTimeout(() => {
      state.refreshTimer = null;
      refreshVectorLayer();
    }, 180);
  }

  async function refreshVectorLayer() {
    if (!state.layer || state.layer.kind !== "vector") return;
    if (state.refreshRunning) {
      state.refreshQueued = true;
      return;
    }

    const requestId = ++state.requestId;
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
      field: fieldSelect.value || "",
      colormap: colormapSelect.value || "hazard"
    });

    state.refreshRunning = true;
    try {
      const response = await fetch(`api/layers/${state.layer.id}/features?${params.toString()}`);
      const payload = await response.json();
      if (requestId !== state.requestId || !state.layer) return;
      if (!response.ok) {
        throw new Error(payload.error || "Could not load layer features");
      }

      map.getSource(vectorSourceId)?.setData({
        type: "FeatureCollection",
        features: payload.features || []
      });
      updateLayerOpacity();

      const visible = Number(payload.visible_count || 0);
      const returned = Number(payload.returned_count || 0);
      const clipped = payload.truncated ? " · zoom in for detail" : "";
      setMessage(`${integerFormat(visible)} visible · ${integerFormat(returned)} drawn${clipped}`, "success");
    } catch (error) {
      if (requestId !== state.requestId) return;
      clearVectorSource();
      setMessage(error.message, "error");
    } finally {
      state.refreshRunning = false;
      if (state.refreshQueued && state.layer) {
        state.refreshQueued = false;
        scheduleVectorRefresh({ immediate: true });
      }
    }
  }

  function updateLayerOpacity() {
    const fillOpacity = activeOpacity(0.78);
    const strokeOpacity = activeOpacity(0.95);
    if (map.getLayer(vectorLayerIds.fill)) map.setPaintProperty(vectorLayerIds.fill, "fill-opacity", fillOpacity);
    if (map.getLayer(vectorLayerIds.outline)) map.setPaintProperty(vectorLayerIds.outline, "line-opacity", strokeOpacity);
    if (map.getLayer(vectorLayerIds.line)) map.setPaintProperty(vectorLayerIds.line, "line-opacity", strokeOpacity);
    if (map.getLayer(vectorLayerIds.point)) map.setPaintProperty(vectorLayerIds.point, "circle-opacity", strokeOpacity);
    if (map.getLayer(rasterLayerId)) map.setPaintProperty(rasterLayerId, "raster-opacity", activeOpacity(1));
  }

  function activeOpacity(base) {
    const transparency = Number(transparencyInput.value || 55);
    const opacity = Math.max(0.05, Math.min(0.95, (100 - transparency) / 100));
    return Math.max(0.03, Math.min(0.95, opacity * base));
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

  function showFeaturePopup(event) {
    const original = event.originalEvent || {};
    if (!original.altKey) return;
    const feature = event.features && event.features[0];
    if (!feature || !state.layer) return;
    event.preventDefault();

    const field = feature.properties.display_field || "Value";
    const value = feature.properties.display_value || "n/a";
    const html = `
      <strong>${htmlEscape(state.layer.name || "Layer")}</strong>
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

  function clearLayer({ silent = false, cleanupServer = true, invalidatePending = true } = {}) {
    const layerId = state.layer?.id || "";
    if (invalidatePending) {
      activeImportJobId = "";
      state.importCycle += 1;
    }
    state.layer = null;
    state.requestId += 1;
    state.refreshQueued = false;
    if (state.refreshTimer) {
      window.clearTimeout(state.refreshTimer);
      state.refreshTimer = null;
    }
    if (state.popup) {
      state.popup.remove();
      state.popup = null;
    }
    clearVectorSource();
    removeRasterLayer();
    controls.classList.add("hidden");
    publishLayerChange();
    if (cleanupServer && layerId) {
      deleteLayerOnServer(layerId);
    }
    if (!silent) setMessage("Layer cleared.");
  }

  function resetLayerUi() {
    clearLayer({ silent: true });
    selectedLocalPath = "";
    uploadAfterPick = false;
    try {
      layerFile.value = "";
    } catch (_error) {
      // Ignore browsers that do not allow clearing a file input here.
    }
    layerFileTitle.textContent = defaultLayerTitle;
    layerFileSubtitle.textContent = defaultLayerSubtitle;
    fieldSelect.innerHTML = "";
    colormapSelect.value = "hazard";
    transparencyInput.value = "55";
    hideImportStatus();
    setMessage("Upload a layer to display it over buildings and exposure points.");
    uploadButton.disabled = false;
    uploadButton.textContent = "Import layer";
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
    window.currentAddedMapLayer = state.layer ? { ...state.layer } : null;
    window.dispatchEvent(new CustomEvent("added-map-layer-change", {
      detail: window.currentAddedMapLayer
    }));
  }

  function clearVectorSource() {
    if (typeof map !== "undefined" && map.getSource(vectorSourceId)) {
      map.getSource(vectorSourceId).setData(emptyCollection);
    }
  }

  function removeRasterLayer() {
    if (typeof map === "undefined") return;
    if (map.getLayer(rasterLayerId)) map.removeLayer(rasterLayerId);
    if (map.getSource(rasterSourceId)) map.removeSource(rasterSourceId);
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

  window.addedMapLayerController = {
    clear: clearLayer,
    reset: resetLayerUi
  };
})();
