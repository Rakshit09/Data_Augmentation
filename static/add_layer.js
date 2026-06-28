(function () {
  const layerFile = document.getElementById("addLayerFile");
  const layerFileTitle = document.getElementById("addLayerFileTitle");
  const layerFileSubtitle = document.getElementById("addLayerFileSubtitle");
  const uploadButton = document.getElementById("uploadMapLayer");
  const controls = document.getElementById("addLayerControls");
  const fieldSelect = document.getElementById("addLayerField");
  const colormapSelect = document.getElementById("addLayerColormap");
  const transparencyInput = document.getElementById("addLayerOpacity");
  const clearButton = document.getElementById("clearMapLayer");
  const messageEl = document.getElementById("addLayerMessage");

  const vectorSourceId = "user-added-vector-layer";
  const rasterSourceId = "user-added-raster-layer";
  const vectorLayerIds = {
    fill: "user-added-vector-fill",
    outline: "user-added-vector-outline",
    line: "user-added-vector-line",
    point: "user-added-vector-point"
  };
  const rasterLayerId = "user-added-raster-layer";
  const emptyCollection = { type: "FeatureCollection", features: [] };

  const state = {
    layer: null,
    refreshTimer: null,
    refreshRunning: false,
    refreshQueued: false,
    requestId: 0,
    popup: null
  };

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
    const file = layerFile.files && layerFile.files[0];
    const name = file ? file.name : "";
    layerFileTitle.textContent = name || "Choose Layer File";
    layerFileSubtitle.textContent = name
      ? "Click Add layer to render it on the map"
      : "GeoPackage, zipped shapefile, shapefile, GeoJSON, or GeoTIFF";
  });

  uploadButton.addEventListener("click", uploadLayer);
  clearButton?.addEventListener("click", clearLayer);
  fieldSelect.addEventListener("change", () => {
    if (!state.layer) return;
    state.layer.field = fieldSelect.value;
    scheduleVectorRefresh({ immediate: true });
  });
  colormapSelect.addEventListener("change", () => {
    if (!state.layer) return;
    state.layer.colormap = colormapSelect.value;
    scheduleVectorRefresh({ immediate: true });
  });
  transparencyInput.addEventListener("input", updateLayerOpacity);

  async function uploadLayer() {
    const file = layerFile.files && layerFile.files[0];
    if (!file) {
      setMessage("Choose a layer file first.", "error");
      return;
    }

    if (typeof dismissCriticalNote === "function") dismissCriticalNote();
    uploadButton.disabled = true;
    if (typeof statusEl !== "undefined") statusEl.textContent = "Adding layer";
    setMessage("Preparing layer for smooth map rendering...");

    const formData = new FormData();
    formData.append("file", file);

    try {
      const response = await fetch("api/layers/upload", {
        method: "POST",
        body: formData
      });
      const payload = await response.json();

      if (!response.ok) {
        throw new Error(payload.error || "Could not add layer");
      }

      clearLayer({ silent: true });
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
      fitToExtent(payload.extent);
      setMessage(`Layer ready: ${payload.name || "uploaded layer"}.`, "success");
      if (typeof statusEl !== "undefined") statusEl.textContent = "Ready";
    } catch (error) {
      clearLayer({ silent: true });
      setMessage(error.message, "error");
      if (typeof statusEl !== "undefined") statusEl.textContent = "Error";
    } finally {
      uploadButton.disabled = false;
    }
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
      });
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
      });
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
      });
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
      });
    }

    for (const layerId of Object.values(vectorLayerIds)) {
      map.on("click", layerId, showFeaturePopup);
      map.on("mouseenter", layerId, () => { map.getCanvas().style.cursor = "pointer"; });
      map.on("mouseleave", layerId, () => { map.getCanvas().style.cursor = ""; });
    }
  }

  function activateVectorLayer(layer) {
    removeRasterLayer();
    populateFieldOptions(layer.fields || [], layer.default_field || "");
    fieldSelect.disabled = !((layer.fields || []).length);
    colormapSelect.disabled = false;
    scheduleVectorRefresh({ immediate: true });
  }

  function activateRasterLayer(layer) {
    clearVectorSource();
    populateFieldOptions([], "");
    fieldSelect.disabled = true;
    colormapSelect.disabled = true;
    removeRasterLayer();

    map.addSource(rasterSourceId, {
      type: "raster",
      tiles: [layer.tile_url],
      tileSize: 256
    });
    map.addLayer({
      id: rasterLayerId,
      type: "raster",
      source: rasterSourceId,
      paint: {
        "raster-opacity": activeOpacity(1)
      }
    });
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
    const feature = event.features && event.features[0];
    if (!feature || !state.layer) return;

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

  function clearLayer({ silent = false } = {}) {
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
    if (!silent) setMessage("Layer cleared.");
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
})();
