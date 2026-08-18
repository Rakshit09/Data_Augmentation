(function () {
  const controls = document.getElementById("rasterIntersectionControls");
  const layerName = document.getElementById("rasterIntersectionLayerName");
  const bandSelect = document.getElementById("rasterIntersectionBand");
  const bandLabel = bandSelect?.closest("label");
  const areaSelect = document.getElementById("rasterIntersectionArea");
  const siFieldSelect = document.getElementById("rasterIntersectionSiField");
  const thresholdOperator = document.getElementById("rasterThresholdOperator");
  const thresholdValue = document.getElementById("rasterThresholdValue");
  const radiusInput = document.getElementById("rasterSampleRadius");
  const aggregationSelect = document.getElementById("rasterSampleAggregation");
  const thresholdRow = document.querySelector(".raster-threshold-row");
  const radiusLabel = radiusInput?.closest("label");
  const aggregationLabel = aggregationSelect?.closest("label");
  const exposureButton = document.getElementById("intersectExposureRaster");
  const databaseButton = document.getElementById("intersectDatabaseRaster");
  const message = document.getElementById("rasterIntersectionMessage");

  let activeLayer = window.currentAddedMapLayer || null;
  let running = false;

  if (!controls || !bandSelect || !siFieldSelect || !exposureButton || !databaseButton || !radiusInput || !aggregationSelect) return;

  exposureButton.addEventListener("click", () => runIntersection("exposure"));
  databaseButton.addEventListener("click", () => runIntersection("database"));
  window.addEventListener("added-map-layer-change", (event) => {
    activeLayer = event.detail || null;
    syncLayerState();
  });
  window.addEventListener("exposure-upload-state-change", syncLayerState);
  syncLayerState();

  function syncLayerState({ updateMessage = true } = {}) {
    const isRaster = activeLayer && activeLayer.kind === "raster";
    const isVector = activeLayer && activeLayer.kind === "vector";
    const isIntersectable = isRaster || isVector;
    const exposureState = window.getExposureUploadState ? window.getExposureUploadState() : {};
    controls.classList.toggle("hidden", !isIntersectable);
    exposureButton.disabled = running || !isIntersectable || !hasExposureUpload();
    databaseButton.disabled = running || !isIntersectable;
    bandLabel?.classList.toggle("hidden", !isRaster);
    thresholdRow?.classList.toggle("hidden", !isRaster);
    radiusLabel?.classList.toggle("hidden", !isRaster);
    aggregationLabel?.classList.toggle("hidden", !isRaster);
    syncSiFieldOptions(Array.isArray(exposureState.columns) ? exposureState.columns : []);

    if (!isIntersectable) {
      layerName.textContent = "No layer selected";
      bandSelect.innerHTML = "";
      if (updateMessage) setMessage("Upload a GeoTIFF or vector layer to intersect exposure or building locations.");
      return;
    }

    layerName.textContent = activeLayer.name || "Layer";
    if (isVector) {
      const field = activeLayer.field || activeLayer.default_field || "";
      const fieldCopy = field ? `Using field: ${field}.` : "No field selected; feature id will be appended.";
      if (updateMessage) setMessage(hasExposureUpload()
        ? `${fieldCopy} Exposure and database intersections are ready.`
        : `${fieldCopy} Upload an exposure CSV to enable exposure intersection.`, hasExposureUpload() ? "success" : "");
      return;
    }

    const bands = activeLayer.bands && activeLayer.bands.length
      ? activeLayer.bands
      : [{ index: 1, name: "Band 1" }];
    const previous = bandSelect.value || String(activeLayer.default_band || 1);
    bandSelect.innerHTML = bands
      .map((band) => `<option value="${escapeHtml(band.index)}">${escapeHtml(band.name || `Band ${band.index}`)}</option>`)
      .join("");
    bandSelect.value = bands.some((band) => String(band.index) === previous) ? previous : String(bands[0].index);
    const exposureCopy = hasExposureUpload() ? "Exposure and database intersections are ready." : "Upload an exposure CSV to enable exposure intersection.";
    if (updateMessage) setMessage(exposureCopy, hasExposureUpload() ? "success" : "");
  }

  async function runIntersection(sourceType) {
    if (!activeLayer || !["raster", "vector"].includes(activeLayer.kind)) {
      setMessage("Upload a GeoTIFF, GeoPackage, shapefile, or GeoJSON layer first.", "error");
      return;
    }

    const exposureState = window.getExposureUploadState ? window.getExposureUploadState() : {};
    if (sourceType === "exposure" && (!exposureState.upload_id || !exposureState.lat_col || !exposureState.lon_col)) {
      setMessage("Upload an exposure CSV and choose latitude/longitude columns first.", "error");
      return;
    }

    const isRaster = activeLayer.kind === "raster";
    const threshold = thresholdValue.value.trim();
    const radiusText = radiusInput.value.trim();
    running = true;
    syncLayerState({ updateMessage: false });
    setMessage(sourceType === "exposure" ? "Intersecting exposure points..." : "Intersecting building centroids...");
    if (typeof statusEl !== "undefined") statusEl.textContent = isRaster ? "Intersecting raster" : "Intersecting vector";

    try {
      const payload = {
        layer_id: activeLayer.id,
        area_mode: areaSelect.value || "visible",
        bounds: currentBounds()
      };
      if (isRaster) {
        payload.band_index = Number(bandSelect.value || 1);
        payload.threshold = threshold === "" ? null : Number(threshold);
        payload.threshold_operator = thresholdOperator.value || ">";
        if (radiusText !== "") {
          const radius = Number(radiusText);
          if (!Number.isFinite(radius) || radius <= 0) {
            throw new Error("Radius must be greater than 0 metres.");
          }
          payload.sample_radius_m = radius;
          payload.sample_radius_aggregation = aggregationSelect.value || "mean";
        }
      } else {
        payload.field = activeLayer.field || activeLayer.default_field || "";
      }
      if (sourceType === "exposure") {
        payload.upload_id = exposureState.upload_id;
        payload.lat_col = exposureState.lat_col;
        payload.lon_col = exposureState.lon_col;
        payload.si_field = siFieldSelect.value || "";
      }

      let result;
      if (isRaster) {
        result = sourceType === "exposure"
          ? await window.rasterIntersectionApi.intersectExposure(payload)
          : await window.rasterIntersectionApi.intersectDatabase(payload);
        await pollRasterIntersectionProgress(result.job_id);
      } else {
        result = sourceType === "exposure"
          ? await window.rasterIntersectionApi.intersectVectorExposure(payload)
          : await window.rasterIntersectionApi.intersectVectorDatabase(payload);
        window.rasterIntersectionPreview.render(result);
        window.rasterIntersectionLayers.render(result.map_features);
        setMessage(`Done: ${formatInteger(result.summary?.matched_count)} matched locations.`, "success");
        if (typeof statusEl !== "undefined") statusEl.textContent = "Done";
      }
    } catch (error) {
      setMessage(error.message, "error");
      if (typeof statusEl !== "undefined") statusEl.textContent = "Error";
    } finally {
      running = false;
      syncLayerState({ updateMessage: false });
    }
  }

  function currentBounds() {
    if (typeof map === "undefined") return activeLayer.extent || {};
    const bounds = map.getBounds();
    return {
      min_lon: bounds.getWest(),
      min_lat: bounds.getSouth(),
      max_lon: bounds.getEast(),
      max_lat: bounds.getNorth()
    };
  }

  function hasExposureUpload() {
    const state = window.getExposureUploadState ? window.getExposureUploadState() : window.rasterIntersectionExposureState;
    return Boolean(state && state.upload_id && state.lat_col && state.lon_col);
  }

  function syncSiFieldOptions(columns) {
    const previous = siFieldSelect.value || "";
    const options = [
      '<option value="">Auto-detect</option>',
      ...columns.map((column) => `<option value="${escapeHtml(column)}">${escapeHtml(column)}</option>`)
    ];
    siFieldSelect.innerHTML = options.join("");
    siFieldSelect.value = columns.includes(previous) ? previous : "";
    siFieldSelect.disabled = !columns.length;
  }

  function setMessage(text, type = "") {
    if (!message) return;
    message.textContent = text || "";
    message.classList.toggle("error", type === "error");
    message.classList.toggle("success", type === "success");
  }

  function setMessageHtml(html, type = "") {
    if (!message) return;
    message.innerHTML = html || "";
    message.classList.toggle("error", type === "error");
    message.classList.toggle("success", type === "success");
  }

  async function pollRasterIntersectionProgress(jobId) {
    while (true) {
      const payload = await window.rasterIntersectionApi.progress(jobId);
      if (payload.status === "complete") {
        window.rasterIntersectionPreview.render(payload);
        window.rasterIntersectionLayers.render(payload.map_features);
        setMessage(`Done: ${formatInteger(payload.summary?.matched_count)} matched locations.`, "success");
        if (typeof statusEl !== "undefined") statusEl.textContent = "Done";
        return;
      }

      if (payload.status === "error") {
        throw new Error(payload.error || "Raster intersection failed");
      }

      renderProgressMessage(payload);
      await wait(1500);
    }
  }

  function renderProgressMessage(payload) {
    const percent = Math.max(0, Math.min(100, Number(payload.percent || 0)));
    const phase = escapeHtml(payload.phase || payload.status || "Working");
    const detail = payload.detail
      ? `<div class="progress-copy">${escapeHtml(payload.detail)}</div>`
      : "";
    setMessageHtml(`
      <div class="progress-copy">${phase}</div>
      ${detail}
      <div class="progress-track"><div class="progress-fill" style="width:${percent}%"></div></div>
      <div class="progress-copy">${percent.toFixed(0)}%</div>
    `);
  }

  function wait(ms) {
    return new Promise((resolve) => window.setTimeout(resolve, ms));
  }

  function formatInteger(value) {
    return Number(value || 0).toLocaleString();
  }

  function escapeHtml(value) {
    return String(value ?? "")
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;")
      .replaceAll("'", "&#039;");
  }
})();
