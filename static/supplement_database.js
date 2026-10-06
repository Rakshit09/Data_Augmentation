(() => {
  const $ = (id) => document.getElementById(id);
  const els = {
    body: $("supplementBody"),
    form: $("supplementForm"),
    stepper: document.querySelector(".supp-stepper"),
    targetDb: $("suppTargetDb"),
    browseTarget: $("suppBrowseTarget"),
    sourcePath: $("suppSourcePath"),
    browseSource: $("suppBrowseSource"),
    inspect: $("suppInspect"),
    summary: $("suppSourceSummary"),
    layerPanel: $("suppLayerPanel"),
    geometryPanel: $("suppGeometryPanel"),
    geometryColumn: $("suppGeometryColumn"),
    lonColumn: $("suppLonColumn"),
    latColumn: $("suppLatColumn"),
    crsRow: $("suppCrsRow"),
    crs: $("suppSourceCrs"),
    methodCard: $("suppMethodCard"),
    radius: $("suppRadius"),
    minIou: $("suppMinIou"),
    minIouValue: $("suppMinIouValue"),
    iouRow: $("suppIouRow"),
    aggregateRow: $("suppAggregateRow"),
    aggregate: $("suppAggregate"),
    filterColumn: $("suppFilterColumn"),
    filterOp: $("suppFilterOp"),
    filterValues: $("suppFilterValues"),
    filterChips: $("suppFilterChips"),
    filterEntry: $("suppFilterEntry"),
    filterSuggestions: $("suppFilterSuggestions"),
    filterTopValues: $("suppFilterTopValues"),
    filterRange: $("suppFilterRange"),
    filterMin: $("suppFilterMin"),
    filterMax: $("suppFilterMax"),
    newBuildingsRow: $("suppNewBuildingsRow"),
    addNew: $("suppAddNew"),
    prefix: $("suppPrefix"),
    columnSearch: $("suppColumnSearch"),
    columnCount: $("suppColumnCount"),
    selectAll: $("suppSelectAll"),
    selectNone: $("suppSelectNone"),
    columnList: $("suppColumnList"),
    matchColumnsNote: $("suppMatchColumnsNote"),
    conflictNote: $("suppConflictNote"),
    replaceRow: $("suppReplaceRow"),
    replaceExisting: $("suppReplaceExisting"),
    outputPathRow: $("suppOutputPathRow"),
    outputPath: $("suppOutputPath"),
    activateRow: $("suppActivateRow"),
    activate: $("suppActivate"),
    backupRow: $("suppBackupRow"),
    keepBackup: $("suppKeepBackup"),
    workDir: $("suppWorkDir"),
    browseWork: $("suppBrowseWork"),
    copyLocalRow: $("suppCopyLocalRow"),
    copyLocal: $("suppCopyLocal"),
    disk: $("suppDiskMeter"),
    presetSelect: $("suppPresetSelect"),
    presetApply: $("suppPresetApply"),
    presetDelete: $("suppPresetDelete"),
    presetName: $("suppPresetName"),
    presetSave: $("suppPresetSave"),
    actions: $("suppActions"),
    preview: $("suppPreview"),
    run: $("suppRun"),
    cancel: $("suppCancel"),
    status: $("supplementStatus"),
    splash: $("etlSplash"),
    mapWrap: $("supplementMapWrap"),
    mapContainer: $("supplementMap")
  };
  if (!els.body) return;

  const PRESET_KEY = "dataAugmentation.supplementPresets.v1";
  const NAME_PATTERN = /^[A-Za-z_][A-Za-z0-9_]{0,62}$/;
  const HIDDEN_PREFIXES = ["geom", "bbox", "quadkey"];
  const COLORS = {
    high: "#16a34a",
    medium: "#f59e0b",
    low: "#f97316",
    none: "#94a3b8",
    new: "#7c3aed",
    matched: "#2563eb",
    unmatched: "#64748b"
  };
  const RUN_STEPS = [
    { label: "Read source", match: /copying source|reading/i },
    { label: "Reproject", match: /reproject/i },
    { label: "Stage targets", match: /staging target/i },
    { label: "Match", match: /^matching (footprints|points)|finding new/i },
    { label: "Assemble", match: /assembl|matching complete/i },
    { label: "Write database", match: /writing database|database written|rebuilding/i },
    { label: "Swap in", match: /^swapping|^complete$/i }
  ];
  const PREVIEW_LAYERS = [
    "supp-preview-target-fill",
    "supp-preview-target-line",
    "supp-preview-source-line",
    "supp-preview-source-point"
  ];

  let inspected = null;
  let columnRows = [];
  let filterValues = [];
  let jobToken = 0;
  let currentJobId = null;
  let spaceTimer = null;
  let previewPopup = null;
  let previewMap = null;
  let previewShownFields = [];

  // ---------------------------------------------------------------------
  // Helpers
  // ---------------------------------------------------------------------

  const fmtInt = (value) => Number(value || 0).toLocaleString();
  const esc = (value) => escapeHtml(value ?? "");

  function fmtBytes(bytes) {
    if (bytes === null || bytes === undefined) return "n/a";
    const units = ["B", "KB", "MB", "GB", "TB"];
    let value = Number(bytes);
    let unit = 0;
    while (value >= 1024 && unit < units.length - 1) {
      value /= 1024;
      unit += 1;
    }
    return `${value.toFixed(value >= 100 || unit === 0 ? 0 : 1)} ${units[unit]}`;
  }

  function fmtDuration(seconds) {
    const total = Math.max(0, Math.round(seconds));
    const minutes = Math.floor(total / 60);
    const rest = total % 60;
    return minutes ? `${minutes}m ${String(rest).padStart(2, "0")}s` : `${rest}s`;
  }

  function sanitizeName(name) {
    let cleaned = String(name).replace(/[^A-Za-z0-9_]+/g, "_").replace(/^_+|_+$/g, "").toLowerCase();
    if (!cleaned || !/^[a-z_]/.test(cleaned)) cleaned = `f_${cleaned}`;
    return cleaned.slice(0, 48);
  }

  function normalizedPrefix() {
    let prefix = els.prefix.value.trim();
    if (prefix && !prefix.endsWith("_")) prefix += "_";
    return prefix;
  }

  function currentGroup() {
    if (!inspected) return null;
    const groups = inspected.source.groups || [];
    const selectedGroup = els.layerPanel.querySelector("select[data-role='group']")?.value;
    return groups.find((group) => group.id === selectedGroup) || groups[0] || null;
  }

  function geometryKind() {
    const source = inspected?.source;
    if (!source) return "unknown";
    if (source.kind === "parquet") {
      if (els.geometryColumn.value) return currentGroup()?.kind || "unknown";
      return els.lonColumn.value && els.latColumn.value ? "point" : "unknown";
    }
    return currentGroup()?.kind || "unknown";
  }

  function outputMode() {
    return els.form.querySelector("input[name='suppOutputMode']:checked")?.value || "new";
  }

  function matchColumnNames() {
    const prefix = normalizedPrefix();
    const names = [`${prefix}match_type`, `${prefix}match_confidence`, `${prefix}match_distance_m`];
    if (geometryKind() === "polygon") names.push(`${prefix}match_iou`, `${prefix}match_shared`);
    else names.push(`${prefix}match_count`);
    return names;
  }

  async function postJson(url, body) {
    const response = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body || {})
    });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(payload.error || `Request failed (${response.status})`);
    return payload;
  }

  function showStatus(type, html) {
    els.status.classList.remove("hidden", "etl-status--error", "etl-status--success");
    if (type === "error") els.status.classList.add("etl-status--error");
    if (type === "success") els.status.classList.add("etl-status--success");
    els.status.innerHTML = html;
  }

  function hideStatus() {
    els.status.classList.add("hidden");
    els.status.innerHTML = "";
  }

  // ---------------------------------------------------------------------
  // Stepper
  // ---------------------------------------------------------------------

  function panel(step) {
    return els.form.querySelector(`[data-step-panel='${step}']`);
  }

  function setActiveStep(step) {
    els.stepper.querySelectorAll("li").forEach((item) => {
      item.classList.toggle("is-active", item.dataset.step === step);
    });
  }

  function refreshStepState() {
    const ready = Boolean(inspected);
    const selected = columnRows.filter((row) => row.selected).length;
    const done = {
      source: ready && selectedLayers().length > 0 && geometryKind() !== "unknown",
      match: ready,
      columns: ready && selected > 0,
      output: ready && (outputMode() === "inplace" || /\.duckdb$/i.test(els.outputPath.value.trim()))
    };
    els.stepper.querySelectorAll("li").forEach((item) => {
      const step = item.dataset.step;
      item.classList.toggle("is-done", Boolean(done[step]));
      item.classList.toggle("is-locked", step !== "source" && !ready);
    });
  }

  els.stepper.addEventListener("click", (event) => {
    const item = event.target.closest("li");
    if (!item || item.classList.contains("is-locked")) return;
    const target = panel(item.dataset.step);
    if (!target || target.classList.contains("hidden")) return;
    setActiveStep(item.dataset.step);
    target.scrollIntoView({ behavior: "smooth", block: "start" });
  });

  els.form.addEventListener("focusin", (event) => {
    const fieldset = event.target.closest("[data-step-panel]");
    if (fieldset) setActiveStep(fieldset.dataset.stepPanel);
  });

  // ---------------------------------------------------------------------
  // Pickers and inspect
  // ---------------------------------------------------------------------

  async function pickFile(kind, input, button) {
    button.disabled = true;
    try {
      const response = await fetch(`api/browse-file?kind=${kind}`);
      const payload = await response.json();
      if (!response.ok) throw new Error(payload.error || "Could not open file picker");
      if (!payload.cancelled && payload.path) {
        input.value = payload.path;
        input.dispatchEvent(new Event("change"));
      }
    } catch (error) {
      showStatus("error", esc(error.message));
    } finally {
      button.disabled = false;
    }
  }

  els.browseTarget.addEventListener("click", () => pickFile("db", els.targetDb, els.browseTarget));
  els.browseSource.addEventListener("click", () => pickFile("supplement", els.sourcePath, els.browseSource));
  els.browseWork.addEventListener("click", async () => {
    els.browseWork.disabled = true;
    try {
      const response = await fetch("api/browse-folder");
      const payload = await response.json();
      if (!response.ok) throw new Error(payload.error || "Could not open folder picker");
      if (!payload.cancelled && payload.path) {
        els.workDir.value = payload.path;
        scheduleSpaceCheck();
      }
    } catch (error) {
      showStatus("error", esc(error.message));
    } finally {
      els.browseWork.disabled = false;
    }
  });

  async function prefillTarget() {
    if (els.targetDb.value.trim()) return;
    try {
      const response = await fetch("api/data-source");
      const payload = await response.json();
      if (payload.db_path && !els.targetDb.value.trim()) els.targetDb.value = payload.db_path;
    } catch {
      // The user can still type or browse a target path.
    }
  }

  els.inspect.addEventListener("click", inspect);
  els.sourcePath.addEventListener("keydown", (event) => {
    if (event.key === "Enter") {
      event.preventDefault();
      inspect();
    }
  });

  async function inspect() {
    const sourcePath = els.sourcePath.value.trim();
    if (!sourcePath) {
      showStatus("error", "Choose a GeoPackage or Parquet source first.");
      return;
    }
    els.inspect.disabled = true;
    showStatus("info", `<div class="progress-copy">Inspecting source and target…</div>`);
    try {
      inspected = await postJson("api/supplement/inspect", {
        source_path: sourcePath,
        target_db_path: els.targetDb.value.trim()
      });
      els.sourcePath.value = inspected.source.path || sourcePath;
      if (inspected.target?.db_path) els.targetDb.value = inspected.target.db_path;
      renderAfterInspect();
      hideStatus();
    } catch (error) {
      inspected = null;
      showStatus("error", esc(error.message));
    } finally {
      els.inspect.disabled = false;
      refreshStepState();
    }
  }

  function renderAfterInspect() {
    const { source, target, defaults } = inspected;
    renderLayers();
    renderGeometryPanel();
    els.crsRow.classList.remove("hidden");
    els.prefix.value = defaults.prefix || source.default_prefix || "supp_";
    if (defaults.output_path) els.outputPath.value = defaults.output_path;
    if (defaults.work_dir && !els.workDir.value.trim()) els.workDir.value = defaults.work_dir;
    els.copyLocalRow.classList.toggle("hidden", !source.is_remote);
    els.copyLocal.checked = false;
    ["match", "columns", "output"].forEach((step) => panel(step).classList.remove("hidden"));
    els.actions.classList.remove("hidden");
    if (!target) {
      showStatus("error", "No target database selected. Choose the lookup database that should receive the columns.");
    }
    onGroupChanged();
    refreshPresetList();
    scheduleSpaceCheck();
    setActiveStep("match");
  }

  function renderSummary() {
    const { source, target } = inspected;
    const group = currentGroup();
    const layers = selectedLayers();
    const features = source.kind === "gpkg"
      ? (source.layers || []).filter((layer) => layers.includes(layer.name)).reduce((sum, layer) => sum + (layer.feature_estimate || 0), 0)
      : group?.feature_estimate || 0;
    const tiles = [
      ["Format", source.kind === "gpkg" ? "GeoPackage" : "Parquet"],
      ["Size", fmtBytes(source.size_bytes)],
      ["Features", `~${fmtInt(features)}`],
      ["Geometry", geometryKind()],
      ["CRS", els.crs.value.trim() || group?.crs || "unknown", !(els.crs.value.trim() || group?.crs)],
      ["Location", source.is_remote ? "Network drive" : "Local disk"],
      ["Target rows", target ? fmtInt(target.rows) : "—", !target],
      ["Target size", target ? fmtBytes(target.size_bytes) : "—"]
    ];
    els.summary.innerHTML = tiles.map(([label, value, warn]) => `
      <div class="supp-stat${warn ? " supp-stat--warn" : ""}">
        <span class="supp-stat-label">${esc(label)}</span>
        <span class="supp-stat-value" title="${esc(value)}">${esc(value)}</span>
      </div>
    `).join("");
    if (target?.history?.length) {
      const last = target.history[0];
      els.summary.insertAdjacentHTML("beforeend", `
        <div class="supp-stat" style="grid-column: 1 / -1">
          <span class="supp-stat-label">Previous supplements in target</span>
          <span class="supp-stat-value" title="${esc(last.source_path)}">${esc(target.history.length)} · last: ${esc(last.prefix)} from ${esc(String(last.source_path).split(/[\\/]/).pop())}</span>
        </div>
      `);
    }
    els.summary.classList.remove("hidden");
  }

  function renderLayers() {
    const { source } = inspected;
    if (source.kind !== "gpkg") {
      els.layerPanel.classList.add("hidden");
      els.layerPanel.innerHTML = "";
      return;
    }
    const groups = source.groups || [];
    const groupSelect = groups.length > 1
      ? `<select data-role="group" aria-label="Layer schema group">${groups.map((group) => `
          <option value="${esc(group.id)}">${esc(group.layers.length)} layer(s) · ${esc(group.kind)} · ${esc(group.fields.length)} columns</option>
        `).join("")}</select>`
      : "";
    els.layerPanel.innerHTML = `
      <div class="supp-layer-head">
        <span class="supp-subhead">Layers</span>
        <span class="supp-toolbar-actions">
          ${groupSelect}
          <button type="button" class="etl-tab-button" data-role="all">All</button>
          <button type="button" class="etl-tab-button" data-role="none">None</button>
        </span>
      </div>
      <div class="supp-layer-grid"></div>
      <p class="etl-hint">Layers with the same columns are processed together; pick one schema group at a time.</p>
    `;
    els.layerPanel.classList.remove("hidden");
    renderLayerChips();
    els.layerPanel.querySelector("select[data-role='group']")?.addEventListener("change", () => {
      renderLayerChips();
      onGroupChanged();
    });
    els.layerPanel.querySelector("[data-role='all']").addEventListener("click", () => setAllLayers(true));
    els.layerPanel.querySelector("[data-role='none']").addEventListener("click", () => setAllLayers(false));
  }

  function renderLayerChips() {
    const group = currentGroup();
    const grid = els.layerPanel.querySelector(".supp-layer-grid");
    const layers = (inspected.source.layers || []).filter((layer) => layer.group === group.id);
    const suffix = layers.length > 1 ? commonSuffix(layers.map((layer) => layer.name)) : "";
    grid.innerHTML = layers.map((layer) => `
      <label class="supp-layer-chip" title="${esc(layer.name)}${layer.has_spatial_index ? "" : " (no spatial index: preview reads the whole layer)"}">
        <input type="checkbox" value="${esc(layer.name)}" checked>
        <span class="supp-layer-name">${esc(suffix ? layer.name.slice(0, -suffix.length) || layer.name : layer.name)}</span>
        <span class="supp-layer-count">${esc(compactNumber(layer.feature_estimate))}</span>
      </label>
    `).join("");
    grid.querySelectorAll("input").forEach((input) => input.addEventListener("change", () => {
      renderSummary();
      refreshStepState();
    }));
  }

  function commonSuffix(names) {
    let suffix = names[0] || "";
    names.forEach((name) => {
      while (suffix && !name.endsWith(suffix)) suffix = suffix.slice(1);
    });
    return suffix.length >= 4 ? suffix : "";
  }

  function compactNumber(value) {
    const number = Number(value || 0);
    if (number >= 1e6) return `${(number / 1e6).toFixed(1)}M`;
    if (number >= 1e3) return `${Math.round(number / 1e3)}k`;
    return String(number);
  }

  function setAllLayers(checked) {
    els.layerPanel.querySelectorAll(".supp-layer-grid input").forEach((input) => {
      input.checked = checked;
    });
    renderSummary();
    refreshStepState();
  }

  function selectedLayers() {
    if (!inspected) return [];
    if (inspected.source.kind !== "gpkg") return (inspected.source.layers || []).map((layer) => layer.name);
    return Array.from(els.layerPanel.querySelectorAll(".supp-layer-grid input:checked")).map((input) => input.value);
  }

  function renderGeometryPanel() {
    const { source } = inspected;
    if (source.kind !== "parquet") {
      els.geometryPanel.classList.add("hidden");
      return;
    }
    const geometryOptions = (source.geometry_columns || [])
      .map((column) => `<option value="${esc(column.name)}">${esc(column.name)} (${esc(column.type)})</option>`)
      .join("");
    els.geometryColumn.innerHTML = `<option value="">Use longitude / latitude</option>${geometryOptions}`;
    if ((source.geometry_columns || []).length) els.geometryColumn.value = source.geometry_columns[0].name;
    const numeric = source.numeric_columns || [];
    const numericOptions = numeric.map((name) => `<option value="${esc(name)}">${esc(name)}</option>`).join("");
    els.lonColumn.innerHTML = `<option value="">—</option>${numericOptions}`;
    els.latColumn.innerHTML = `<option value="">—</option>${numericOptions}`;
    if (source.lon_guess) els.lonColumn.value = source.lon_guess;
    if (source.lat_guess) els.latColumn.value = source.lat_guess;
    els.geometryPanel.classList.remove("hidden");
    syncGeometryInputs();
  }

  function syncGeometryInputs() {
    const usesColumn = Boolean(els.geometryColumn.value);
    els.lonColumn.disabled = usesColumn;
    els.latColumn.disabled = usesColumn;
  }

  [els.geometryColumn, els.lonColumn, els.latColumn].forEach((select) => select.addEventListener("change", () => {
    syncGeometryInputs();
    onGeometryKindChanged();
  }));

  function onGroupChanged() {
    const group = currentGroup();
    els.crs.value = group?.crs || "";
    els.crs.placeholder = group?.crs ? group.crs : "Unknown: enter e.g. EPSG:25832";
    renderFilterColumns();
    renderColumns();
    onGeometryKindChanged();
  }

  function onGeometryKindChanged() {
    const kind = geometryKind();
    els.iouRow.classList.toggle("hidden", kind !== "polygon");
    els.aggregateRow.classList.toggle("hidden", kind !== "point");
    els.newBuildingsRow.classList.toggle("hidden", kind !== "polygon");
    if (kind === "point" && Number(els.radius.value) === 5) els.radius.value = "25";
    if (kind === "polygon" && Number(els.radius.value) === 25) els.radius.value = "5";
    renderMethodCard();
    renderSummary();
    updateMatchNote();
    refreshStepState();
  }

  els.crs.addEventListener("input", () => renderSummary());

  function renderMethodCard() {
    const kind = geometryKind();
    if (kind === "polygon") {
      els.methodCard.innerHTML = `
        <strong>Footprint matching · inside + nearest + overlap check</strong>
        <ol>
          <li>A point guaranteed to lie on each target building is tested against the source footprints (spatial join).</li>
          <li>Buildings without a hit take the closest source footprint within the search radius.</li>
          <li>Overlap (IoU) and area ratio rate each match: <b>high</b> = inside and overlap ≥ threshold, <b>medium</b> = inside or touching, <b>low</b> = nearest only.</li>
        </ol>`;
    } else if (kind === "point") {
      els.methodCard.innerHTML = `
        <strong>Point matching · inside + nearest</strong>
        <ol>
          <li>Each source point is assigned to the building it falls inside (smallest building wins).</li>
          <li>Points outside every building take the closest building within the search radius.</li>
          <li>Several points on one building are combined with the rule below; the closest point supplies text values.</li>
        </ol>`;
    } else {
      els.methodCard.innerHTML = `<strong>Choose a geometry</strong> Only point and polygon sources can be matched to buildings.`;
    }
  }

  // ---------------------------------------------------------------------
  // Filter builder
  // ---------------------------------------------------------------------

  function renderFilterColumns() {
    const group = currentGroup();
    const fields = group?.fields || [];
    els.filterColumn.innerHTML = `<option value="">No filter: use every row</option>${fields.map((field) => `
      <option value="${esc(field.name)}">${esc(field.name)}</option>
    `).join("")}`;
    filterValues = [];
    const preferred = fields.find((field) => /function|class|type|category|use/i.test(field.name) && field.distinct_in_sample > 1);
    els.filterColumn.value = "";
    renderFilterValues();
    if (preferred) els.filterColumn.dataset.suggested = preferred.name;
  }

  function currentFilterField() {
    return (currentGroup()?.fields || []).find((field) => field.name === els.filterColumn.value) || null;
  }

  function commonPrefixes(topValues) {
    const counts = new Map();
    topValues.forEach(({ value, count }) => {
      const match = /^([^_\-.:/ ]+[_\-.:/])/.exec(value);
      if (match) counts.set(match[1], (counts.get(match[1]) || 0) + count);
    });
    return Array.from(counts.entries())
      .filter(([, count]) => count > 1)
      .sort((a, b) => b[1] - a[1])
      .slice(0, 10)
      .map(([value, count]) => ({ value, count }));
  }

  function renderFilterValues() {
    const field = currentFilterField();
    const op = els.filterOp.value;
    const hasColumn = Boolean(field);
    els.filterOp.disabled = !hasColumn;
    els.filterValues.classList.toggle("hidden", !hasColumn || op === "between");
    els.filterRange.classList.toggle("hidden", !hasColumn || op !== "between");
    if (!hasColumn) return;

    els.filterChips.innerHTML = filterValues.map((value, index) => `
      <span class="supp-chip">${esc(value)}<button type="button" data-index="${index}" aria-label="Remove ${esc(value)}">×</button></span>
    `).join("");
    const top = field.top_values || [];
    const suggestions = op.includes("prefix") ? commonPrefixes(top) : top.slice(0, 16);
    els.filterSuggestions.innerHTML = top.map((item) => `<option value="${esc(item.value)}"></option>`).join("");
    els.filterTopValues.innerHTML = suggestions
      .filter((item) => !filterValues.includes(item.value))
      .map((item) => `<button type="button" data-value="${esc(item.value)}">${esc(item.value)} <small>${esc(fmtInt(item.count))}</small></button>`)
      .join("");
    if (op === "between") {
      if (!els.filterMin.value && field.min !== null && field.min !== undefined) els.filterMin.placeholder = String(field.min);
      if (!els.filterMax.value && field.max !== null && field.max !== undefined) els.filterMax.placeholder = String(field.max);
    }
  }

  els.filterColumn.addEventListener("change", () => {
    filterValues = [];
    const field = currentFilterField();
    if (field) {
      const prefixes = commonPrefixes(field.top_values || []);
      els.filterOp.value = field.numeric ? "between" : (prefixes.length ? "prefix" : "in");
    }
    renderFilterValues();
  });
  els.filterOp.addEventListener("change", renderFilterValues);
  els.filterChips.addEventListener("click", (event) => {
    const button = event.target.closest("button[data-index]");
    if (!button) return;
    filterValues.splice(Number(button.dataset.index), 1);
    renderFilterValues();
  });
  els.filterTopValues.addEventListener("click", (event) => {
    const button = event.target.closest("button[data-value]");
    if (!button) return;
    addFilterValue(button.dataset.value);
  });
  els.filterEntry.addEventListener("keydown", (event) => {
    if (event.key !== "Enter") return;
    event.preventDefault();
    addFilterValue(els.filterEntry.value);
    els.filterEntry.value = "";
  });

  function addFilterValue(value) {
    const text = String(value || "").trim();
    if (!text || filterValues.includes(text)) return;
    filterValues.push(text);
    renderFilterValues();
  }

  function collectFilter() {
    const field = currentFilterField();
    if (!field) return null;
    const op = els.filterOp.value;
    if (op === "between") {
      const low = els.filterMin.value === "" ? Number(els.filterMin.placeholder) : Number(els.filterMin.value);
      const high = els.filterMax.value === "" ? Number(els.filterMax.placeholder) : Number(els.filterMax.value);
      if (!Number.isFinite(low) || !Number.isFinite(high)) throw new Error("Enter a minimum and maximum for the range filter.");
      return { column: field.name, op, values: [Math.min(low, high), Math.max(low, high)] };
    }
    const pending = els.filterEntry.value.trim();
    if (pending) addFilterValue(pending);
    els.filterEntry.value = "";
    if (!filterValues.length) throw new Error(`Add at least one value for the ${field.name} filter, or choose "No filter".`);
    return { column: field.name, op, values: [...filterValues] };
  }

  // ---------------------------------------------------------------------
  // Column picker
  // ---------------------------------------------------------------------

  function renderColumns() {
    const group = currentGroup();
    const fields = group?.fields || [];
    const prefix = normalizedPrefix();
    const previous = new Map(columnRows.map((row) => [row.source, row]));
    columnRows = fields.map((field) => {
      const prior = previous.get(field.name);
      return {
        source: field.name,
        field,
        selected: prior ? prior.selected : fields.length <= 24,
        output: prior?.edited ? prior.output : `${prefix}${sanitizeName(field.name)}`,
        edited: Boolean(prior?.edited),
        type: prior ? prior.type : (field.suggested_type || "auto")
      };
    });
    els.columnList.innerHTML = columnRows.map((row, index) => {
      const field = row.field;
      const filled = field.null_pct === null || field.null_pct === undefined ? 100 : Math.max(0, 100 - field.null_pct);
      const range = field.numeric && field.min !== null && field.min !== undefined
        ? ` · ${formatNumberShort(field.min)} – ${formatNumberShort(field.max)}`
        : "";
      const samples = (field.samples || []).slice(0, 3).join(", ");
      return `
        <div class="supp-col-row" role="listitem" data-index="${index}">
          <input type="checkbox" data-role="select" aria-label="Add ${esc(field.name)}">
          <div class="supp-col-info">
            <div class="supp-col-name"><span title="${esc(field.name)}">${esc(field.name)}</span><span class="supp-badge">${esc(shortType(field.type))}</span></div>
            <div class="supp-col-meta" title="${esc(samples)}">${samples ? `e.g. ${esc(samples)}` : "no sample values"}${esc(range)} · ${esc(filled.toFixed(0))}% filled</div>
            <span class="supp-null-bar" style="--fill:${filled}%"></span>
          </div>
          <input type="text" data-role="output" aria-label="Output name for ${esc(field.name)}" spellcheck="false">
          <select data-role="type" aria-label="Output type for ${esc(field.name)}">
            <option value="auto">Keep (${esc(shortType(field.type))})</option>
            <option value="text">Text</option>
            <option value="integer">Integer</option>
            <option value="double">Decimal</option>
            <option value="boolean">Boolean</option>
          </select>
        </div>
      `;
    }).join("") || `<p class="etl-hint">The source has no attribute columns.</p>`;
    els.columnList.querySelectorAll(".supp-col-row").forEach((element) => {
      const row = columnRows[Number(element.dataset.index)];
      element.querySelector("[data-role='select']").checked = row.selected;
      element.querySelector("[data-role='output']").value = row.output;
      element.querySelector("[data-role='type']").value = row.type;
    });
    applyColumnSearch();
    updateColumnState();
  }

  function shortType(type) {
    const upper = String(type || "").toUpperCase();
    if (/INT/.test(upper)) return "INT";
    if (/DOUBLE|FLOAT|REAL|DECIMAL|NUMERIC/.test(upper)) return "NUM";
    if (/BOOL/.test(upper)) return "BOOL";
    if (/DATE|TIME/.test(upper)) return "DATE";
    return "TEXT";
  }

  function formatNumberShort(value) {
    const number = Number(value);
    if (!Number.isFinite(number)) return String(value);
    return Math.abs(number) >= 1000 ? number.toLocaleString(undefined, { maximumFractionDigits: 0 }) : String(Math.round(number * 100) / 100);
  }

  els.columnList.addEventListener("change", (event) => {
    const element = event.target.closest(".supp-col-row");
    if (!element) return;
    const row = columnRows[Number(element.dataset.index)];
    const role = event.target.dataset.role;
    if (role === "select") row.selected = event.target.checked;
    if (role === "type") row.type = event.target.value;
    updateColumnState();
  });

  els.columnList.addEventListener("input", (event) => {
    if (event.target.dataset.role !== "output") return;
    const element = event.target.closest(".supp-col-row");
    const row = columnRows[Number(element.dataset.index)];
    row.output = event.target.value.trim();
    row.edited = true;
    if (!row.selected) {
      row.selected = true;
      element.querySelector("[data-role='select']").checked = true;
    }
    updateColumnState();
  });

  els.prefix.addEventListener("input", () => {
    const prefix = normalizedPrefix();
    columnRows.forEach((row, index) => {
      if (row.edited) return;
      row.output = `${prefix}${sanitizeName(row.source)}`;
      const input = els.columnList.querySelector(`.supp-col-row[data-index='${index}'] [data-role='output']`);
      if (input) input.value = row.output;
    });
    updateColumnState();
  });

  els.columnSearch.addEventListener("input", applyColumnSearch);

  function applyColumnSearch() {
    const term = els.columnSearch.value.trim().toLowerCase();
    els.columnList.querySelectorAll(".supp-col-row").forEach((element) => {
      const row = columnRows[Number(element.dataset.index)];
      element.classList.toggle("hidden", Boolean(term) && !row.source.toLowerCase().includes(term));
    });
  }

  function setAllColumns(selected) {
    els.columnList.querySelectorAll(".supp-col-row:not(.hidden)").forEach((element) => {
      const row = columnRows[Number(element.dataset.index)];
      row.selected = selected;
      element.querySelector("[data-role='select']").checked = selected;
    });
    updateColumnState();
  }

  els.selectAll.addEventListener("click", () => setAllColumns(true));
  els.selectNone.addEventListener("click", () => setAllColumns(false));

  function columnProblems() {
    const targetNames = new Map((inspected?.target?.columns || []).map((column) => [column.name.toLowerCase(), column.name]));
    const seen = new Map();
    const conflicts = [];
    const invalid = [];
    const outputs = columnRows.filter((row) => row.selected).map((row) => row.output).concat(matchColumnNames());
    outputs.forEach((name) => {
      const folded = name.toLowerCase();
      if (!NAME_PATTERN.test(name) || HIDDEN_PREFIXES.some((prefix) => folded.startsWith(prefix))) invalid.push(name || "(empty)");
      if (seen.has(folded)) invalid.push(`${name} (duplicate)`);
      seen.set(folded, true);
      if (targetNames.has(folded)) conflicts.push(targetNames.get(folded));
    });
    return { conflicts, invalid };
  }

  function updateColumnState() {
    const { conflicts, invalid } = columnProblems();
    const conflictSet = new Set(conflicts.map((name) => name.toLowerCase()));
    els.columnList.querySelectorAll(".supp-col-row").forEach((element) => {
      const row = columnRows[Number(element.dataset.index)];
      element.classList.toggle("is-selected", row.selected);
      element.classList.toggle("has-conflict", row.selected && conflictSet.has(row.output.toLowerCase()));
    });
    const selected = columnRows.filter((row) => row.selected).length;
    els.columnCount.textContent = `${selected} of ${columnRows.length} selected`;
    const messages = [];
    if (conflicts.length) messages.push(`<b>${esc(conflicts.length)}</b> name(s) already exist in the target: ${esc(conflicts.slice(0, 6).join(", "))}${conflicts.length > 6 ? "…" : ""}. Change the prefix or replace them.`);
    if (invalid.length) messages.push(`Fix these names (letters, digits, underscores; not starting with geom, bbox or quadkey): ${esc(invalid.slice(0, 6).join(", "))}`);
    els.conflictNote.innerHTML = messages.join("<br>");
    els.conflictNote.classList.toggle("hidden", !messages.length);
    els.replaceRow.classList.toggle("hidden", !conflicts.length);
    updateMatchNote();
    refreshStepState();
  }

  function updateMatchNote() {
    if (!inspected) return;
    els.matchColumnsNote.innerHTML = `Always added: ${matchColumnNames().map((name) => `<code>${esc(name)}</code>`).join(" ")}`;
  }

  // ---------------------------------------------------------------------
  // Output, disk space
  // ---------------------------------------------------------------------

  function onOutputModeChanged() {
    const mode = outputMode();
    els.outputPathRow.classList.toggle("hidden", mode !== "new");
    els.activateRow.classList.toggle("hidden", mode !== "new");
    els.backupRow.classList.toggle("hidden", mode !== "inplace");
    scheduleSpaceCheck();
    refreshStepState();
  }

  els.form.querySelectorAll("input[name='suppOutputMode']").forEach((input) => input.addEventListener("change", onOutputModeChanged));
  [els.outputPath, els.workDir].forEach((input) => input.addEventListener("input", () => {
    scheduleSpaceCheck();
    refreshStepState();
  }));
  els.copyLocal.addEventListener("change", scheduleSpaceCheck);
  els.keepBackup.addEventListener("change", scheduleSpaceCheck);
  els.minIou.addEventListener("input", () => {
    els.minIouValue.textContent = Number(els.minIou.value).toFixed(2);
  });

  function scheduleSpaceCheck() {
    window.clearTimeout(spaceTimer);
    spaceTimer = window.setTimeout(checkSpace, 350);
  }

  async function checkSpace() {
    if (!inspected?.target) {
      els.disk.classList.add("hidden");
      return;
    }
    const workDir = els.workDir.value.trim();
    const outputTarget = outputMode() === "new" ? els.outputPath.value.trim() : els.targetDb.value.trim();
    if (!workDir || !outputTarget) return;
    try {
      const space = await postJson("api/supplement/space", { paths: [workDir, outputTarget] });
      const sourceSize = Number(inspected.source.size_bytes || 0);
      const targetSize = Number(inspected.target.size_bytes || 0);
      const workNeed = sourceSize * 0.6 + targetSize * 0.5 + (els.copyLocal.checked ? sourceSize : 0);
      const outputNeed = targetSize * 1.1;
      const rows = [
        ["Working folder (temporary)", space[workDir], workNeed],
        [outputMode() === "new" ? "New database folder" : "Target folder (staged copy)", space[outputTarget], outputNeed]
      ];
      els.disk.innerHTML = rows.map(([label, info, need]) => {
        const free = info?.free_bytes;
        const ratio = free ? Math.min(1, need / free) : 1;
        const state = free === null || free === undefined ? "" : (need > free ? "is-short" : (need > free * 0.7 ? "is-tight" : ""));
        return `
          <div class="supp-disk-row ${state}">
            <div><span>${esc(label)}${info?.is_remote ? " · network drive" : ""}</span><span>needs ~${esc(fmtBytes(need))} · ${esc(fmtBytes(free))} free</span></div>
            <div class="supp-disk-track"><div class="supp-disk-fill" style="width:${(ratio * 100).toFixed(0)}%"></div></div>
          </div>`;
      }).join("") + `<span class="etl-hint">Estimates. Both locations on the same drive add up.</span>`;
      els.disk.classList.remove("hidden");
    } catch {
      els.disk.classList.add("hidden");
    }
  }

  // ---------------------------------------------------------------------
  // Presets (browser storage)
  // ---------------------------------------------------------------------

  function loadPresets() {
    try {
      return JSON.parse(window.localStorage.getItem(PRESET_KEY) || "{}");
    } catch {
      return {};
    }
  }

  function refreshPresetList() {
    const presets = loadPresets();
    els.presetSelect.innerHTML = `<option value="">Saved presets…</option>${Object.keys(presets).sort().map((name) => `
      <option value="${esc(name)}">${esc(name)}</option>
    `).join("")}`;
  }

  els.presetSave.addEventListener("click", () => {
    const name = els.presetName.value.trim();
    if (!name || !inspected) {
      showStatus("error", "Inspect a source and enter a preset name first.");
      return;
    }
    let filter = null;
    try {
      filter = collectFilter();
    } catch {
      filter = null;
    }
    const presets = loadPresets();
    presets[name] = {
      layers: selectedLayers(),
      prefix: els.prefix.value.trim(),
      columns: columnRows.map((row) => ({ source: row.source, selected: row.selected, output: row.output, edited: row.edited, type: row.type })),
      filter,
      radius: els.radius.value,
      minIou: els.minIou.value,
      aggregate: els.aggregate.value,
      addNew: els.addNew.checked,
      mode: outputMode(),
      saved_at: new Date().toISOString()
    };
    window.localStorage.setItem(PRESET_KEY, JSON.stringify(presets));
    refreshPresetList();
    els.presetSelect.value = name;
    showStatus("success", `Preset <b>${esc(name)}</b> saved.`);
  });

  els.presetDelete.addEventListener("click", () => {
    const name = els.presetSelect.value;
    if (!name) return;
    const presets = loadPresets();
    delete presets[name];
    window.localStorage.setItem(PRESET_KEY, JSON.stringify(presets));
    refreshPresetList();
  });

  els.presetApply.addEventListener("click", () => {
    const preset = loadPresets()[els.presetSelect.value];
    if (!preset || !inspected) return;
    if (inspected.source.kind === "gpkg" && preset.layers?.length) {
      const wanted = new Set(preset.layers);
      const inputs = els.layerPanel.querySelectorAll(".supp-layer-grid input");
      const anyMatch = Array.from(inputs).some((input) => wanted.has(input.value));
      if (anyMatch) inputs.forEach((input) => { input.checked = wanted.has(input.value); });
    }
    els.prefix.value = preset.prefix || els.prefix.value;
    const saved = new Map((preset.columns || []).map((column) => [column.source, column]));
    columnRows.forEach((row) => {
      const column = saved.get(row.source);
      if (!column) {
        row.selected = false;
        return;
      }
      row.selected = column.selected;
      row.type = column.type;
      row.edited = column.edited;
      row.output = column.edited ? column.output : `${normalizedPrefix()}${sanitizeName(row.source)}`;
    });
    renderColumnsFromState();
    if (preset.filter && (currentGroup()?.fields || []).some((field) => field.name === preset.filter.column)) {
      els.filterColumn.value = preset.filter.column;
      els.filterOp.value = preset.filter.op;
      if (preset.filter.op === "between") {
        els.filterMin.value = preset.filter.values[0];
        els.filterMax.value = preset.filter.values[1];
        filterValues = [];
      } else {
        filterValues = [...preset.filter.values];
      }
    } else {
      els.filterColumn.value = "";
      filterValues = [];
    }
    renderFilterValues();
    els.radius.value = preset.radius || els.radius.value;
    els.minIou.value = preset.minIou || els.minIou.value;
    els.minIouValue.textContent = Number(els.minIou.value).toFixed(2);
    els.aggregate.value = preset.aggregate || "closest";
    els.addNew.checked = preset.addNew !== false;
    const modeInput = els.form.querySelector(`input[name='suppOutputMode'][value='${preset.mode === "inplace" ? "inplace" : "new"}']`);
    if (modeInput) modeInput.checked = true;
    onOutputModeChanged();
    renderSummary();
    showStatus("success", `Preset <b>${esc(els.presetSelect.value)}</b> applied.`);
  });

  function renderColumnsFromState() {
    els.columnList.querySelectorAll(".supp-col-row").forEach((element) => {
      const row = columnRows[Number(element.dataset.index)];
      element.querySelector("[data-role='select']").checked = row.selected;
      element.querySelector("[data-role='output']").value = row.output;
      element.querySelector("[data-role='type']").value = row.type;
    });
    updateColumnState();
  }

  // ---------------------------------------------------------------------
  // Payload, preview, run
  // ---------------------------------------------------------------------

  function collectPayload() {
    if (!inspected) throw new Error("Inspect a source first.");
    if (!inspected.target) throw new Error("Choose a target lookup database and inspect again.");
    const layers = selectedLayers();
    if (!layers.length) throw new Error("Select at least one layer.");
    if (geometryKind() === "unknown") throw new Error("Choose a geometry column or a longitude/latitude pair.");
    const columns = columnRows.filter((row) => row.selected).map((row) => ({ source: row.source, output: row.output, type: row.type }));
    if (!columns.length) throw new Error("Select at least one column to add.");
    const { conflicts, invalid } = columnProblems();
    if (invalid.length) throw new Error(`Fix the output column names: ${invalid.slice(0, 5).join(", ")}`);
    if (conflicts.length && !els.replaceExisting.checked) throw new Error("Some output columns already exist. Change the prefix or tick 'Replace existing columns'.");
    const detectedCrs = currentGroup()?.crs || "";
    const crs = els.crs.value.trim();
    return {
      target_db_path: els.targetDb.value.trim(),
      source_path: els.sourcePath.value.trim(),
      layers,
      geometry: inspected.source.kind === "parquet"
        ? { column: els.geometryColumn.value, lon: els.lonColumn.value, lat: els.latColumn.value }
        : undefined,
      geometry_kind: geometryKind(),
      source_crs: crs && crs.toUpperCase() !== String(detectedCrs).toUpperCase() ? crs : "",
      prefix: normalizedPrefix(),
      columns,
      filter: collectFilter(),
      match: {
        radius_m: Number(els.radius.value || 0),
        min_iou: Number(els.minIou.value || 0),
        point_aggregate: els.aggregate.value
      },
      add_new_buildings: geometryKind() === "polygon" && els.addNew.checked,
      replace_existing: els.replaceExisting.checked,
      output: {
        mode: outputMode(),
        path: els.outputPath.value.trim(),
        activate: els.activate.checked,
        keep_backup: els.keepBackup.checked
      },
      work_dir: els.workDir.value.trim(),
      copy_source_local: els.copyLocal.checked
    };
  }

  els.preview.addEventListener("click", async () => {
    let payload;
    try {
      payload = collectPayload();
    } catch (error) {
      showStatus("error", esc(error.message));
      return;
    }
    const previewTarget = ensurePreviewMap();
    if (!previewTarget) {
      showStatus("error", "The preview map is not available.");
      return;
    }
    const bounds = previewTarget.getBounds();
    payload.bbox = [bounds.getWest(), bounds.getSouth(), bounds.getEast(), bounds.getNorth()];
    startJob("api/supplement/preview", payload, "preview");
  });

  els.run.addEventListener("click", async () => {
    let payload;
    try {
      payload = collectPayload();
    } catch (error) {
      showStatus("error", esc(error.message));
      return;
    }
    const target = inspected.target;
    const mode = payload.output.mode;
    const question = mode === "inplace"
      ? `Update ${target.db_path} in place?\n\nAn updated copy is built next to it and swapped in when complete${payload.output.keep_backup ? " (the original is kept as a backup)" : ""}.`
      : `Write a new database to ${payload.output.path}?`;
    if (!window.confirm(question)) return;
    startJob("api/supplement/run", payload, "run");
  });

  els.cancel.addEventListener("click", async () => {
    if (!currentJobId) return;
    els.cancel.disabled = true;
    try {
      await postJson(`api/supplement/cancel/${currentJobId}`);
    } catch (error) {
      showStatus("error", esc(error.message));
    } finally {
      els.cancel.disabled = false;
    }
  });

  function setBusy(busy) {
    [els.preview, els.run, els.inspect, els.browseSource, els.browseTarget].forEach((button) => {
      button.disabled = busy;
    });
    els.cancel.classList.toggle("hidden", !busy);
  }

  async function startJob(url, payload, kind) {
    const token = ++jobToken;
    setBusy(true);
    if (typeof statusEl !== "undefined") statusEl.textContent = kind === "preview" ? "Previewing" : "Supplementing";
    showStatus("info", renderProgress({ phase: "Starting worker", percent: 0, kind }));
    try {
      const response = await postJson(url, payload);
      if (token !== jobToken) return;
      currentJobId = response.job_id;
      poll(response.job_id, token, kind);
    } catch (error) {
      if (token !== jobToken) return;
      setBusy(false);
      if (typeof statusEl !== "undefined") statusEl.textContent = "Error";
      showStatus("error", esc(error.message));
    }
  }

  async function poll(jobId, token, kind) {
    try {
      const response = await fetch(`api/supplement/progress/${jobId}`);
      const job = await response.json();
      if (token !== jobToken) return;
      if (!response.ok) throw new Error(job.error || "Could not read progress");

      if (job.status === "complete") {
        currentJobId = null;
        setBusy(false);
        if (typeof statusEl !== "undefined") statusEl.textContent = "Done";
        if (kind === "preview") {
          renderPreviewResult(job);
        } else {
          renderRunResult(job);
          if (job.result?.activated && typeof loadDataSources === "function") await loadDataSources();
        }
        return;
      }
      if (job.status === "error") throw new Error(job.error || "Supplement failed");
      if (job.status === "cancelled") {
        currentJobId = null;
        setBusy(false);
        if (typeof statusEl !== "undefined") statusEl.textContent = "Ready";
        showStatus("info", "Cancelled. Temporary files were removed; the target database was not changed.");
        return;
      }
      els.cancel.disabled = job.cancellable === false;
      showStatus("info", renderProgress({ ...job, kind }));
      window.setTimeout(() => poll(jobId, token, kind), 1000);
    } catch (error) {
      if (token !== jobToken) return;
      currentJobId = null;
      setBusy(false);
      if (typeof statusEl !== "undefined") statusEl.textContent = "Error";
      showStatus("error", `<strong>Supplement failed.</strong><br>${esc(error.message)}`);
    }
  }

  function renderProgress(job) {
    const percent = Math.max(0, Math.min(100, Number(job.percent || 0)));
    const steps = job.kind === "preview" ? RUN_STEPS.slice(0, 5) : RUN_STEPS;
    const phase = String(job.phase || "");
    let active = -1;
    steps.forEach((step, index) => {
      if (step.match.test(phase)) active = index;
    });
    const elapsed = job.created_at ? fmtDuration(Date.now() / 1000 - Number(job.created_at)) : "";
    return `
      <div class="supp-progress">
        <div class="supp-progress-head"><span>${esc(phase || "Working")}</span><span>${percent.toFixed(0)}%</span></div>
        <div class="progress-track"><div class="progress-fill" style="width:${percent}%"></div></div>
        <div class="supp-progress-detail">${esc(job.detail || "")}${elapsed ? ` · elapsed ${esc(elapsed)}` : ""}</div>
        <ul class="supp-phase-list">
          ${steps.map((step, index) => `<li class="${index < active ? "is-done" : (index === active ? "is-active" : "")}">${esc(step.label)}</li>`).join("")}
        </ul>
      </div>
    `;
  }

  function statTile(label, value) {
    return `<div class="supp-stat"><span class="supp-stat-label">${esc(label)}</span><span class="supp-stat-value">${esc(value)}</span></div>`;
  }

  function confidenceBar(stats) {
    const conf = stats.by_confidence || {};
    const unmatched = Number(stats.unmatched_targets || 0);
    const parts = [
      ["high", Number(conf.high || 0)],
      ["medium", Number(conf.medium || 0)],
      ["low", Number(conf.low || 0)],
      ["none", unmatched]
    ];
    const total = parts.reduce((sum, [, count]) => sum + count, 0) || 1;
    return `
      <div class="supp-conf-bar" aria-hidden="true">
        ${parts.map(([key, count]) => `<span style="width:${(count / total) * 100}%;background:${COLORS[key]}" title="${esc(key)}: ${esc(fmtInt(count))}"></span>`).join("")}
      </div>
      <div class="supp-legend">
        ${parts.map(([key, count]) => `<span><i style="background:${COLORS[key]}"></i>${esc(key === "none" ? "unmatched" : key)} ${esc(fmtInt(count))} (${((count / total) * 100).toFixed(1)}%)</span>`).join("")}
      </div>
    `;
  }

  function statsTiles(stats) {
    const tiles = [
      statTile("Target buildings", fmtInt(stats.targets)),
      statTile("Matched", `${fmtInt(stats.matched_targets)} (${stats.targets ? ((stats.matched_targets / stats.targets) * 100).toFixed(1) : "0"}%)`),
      statTile("Source features", fmtInt(stats.sources)),
      statTile("Sources used", fmtInt(stats.sources_matched))
    ];
    if (stats.geometry_kind === "polygon") {
      tiles.push(statTile("New buildings", fmtInt(stats.new_buildings)));
      if (stats.mean_iou_inside !== null && stats.mean_iou_inside !== undefined) tiles.push(statTile("Mean overlap", Number(stats.mean_iou_inside).toFixed(2)));
    }
    tiles.push(statTile("Matching time", fmtDuration(stats.elapsed_seconds || 0)));
    return `<div class="supp-result-grid">${tiles.join("")}</div>`;
  }

  function renderRunResult(job) {
    const result = job.result || {};
    const stats = job.stats || {};
    const warnings = (result.warnings || []).map((warning) => `<li>${esc(warning)}</li>`).join("");
    showStatus("success", `
      <div class="supp-result">
        <strong>${result.mode === "inplace" ? "Database updated in place." : "New database written."}</strong>
        ${statsTiles(stats)}
        ${confidenceBar(stats)}
        <div>
          Output: <code>${esc(result.output_path)}</code>${result.activated ? " · loaded in the app" : ""}<br>
          Rows: ${esc(fmtInt(result.rows_total))} total · ${esc(fmtInt(result.rows_updated))} supplemented · ${esc(fmtInt(result.rows_inserted))} new<br>
          Columns added: ${esc((result.columns_added || []).length)}
          ${result.backup_path ? `<br>Backup: <code>${esc(result.backup_path)}</code>` : ""}
        </div>
        ${warnings ? `<ul class="supp-warning">${warnings}</ul>` : ""}
      </div>
    `);
  }

  // ---------------------------------------------------------------------
  // Map preview (own MapLibre instance, independent of the Spatial Explorer map)
  // ---------------------------------------------------------------------

  function ensurePreviewMap() {
    if (previewMap || typeof window.createBasemapMap !== "function") return previewMap;
    previewMap = window.createBasemapMap("supplementMap");
    // The container starts hidden or resizes with tab switches; keep the canvas in sync.
    new ResizeObserver(() => previewMap.resize()).observe(els.mapContainer);
    previewMap.on("click", "supp-preview-target-fill", (event) => {
      const feature = event.features?.[0];
      if (!feature) return;
      const props = feature.properties || {};
      const fields = previewShownFields.map((name) => `<div><b>${esc(name)}</b>: ${esc(props[name] ?? "—")}</div>`).join("");
      previewPopup?.remove();
      previewPopup = new maplibregl.Popup({ closeButton: true, maxWidth: "280px" })
        .setLngLat(event.lngLat)
        .setHTML(`
          <div class="supp-popup">
            <strong>${esc(props.match_type)} · ${esc(props.confidence)}</strong>
            <div>Overlap (IoU): ${esc(props.iou === null || props.iou === undefined ? "—" : Number(props.iou).toFixed(2))} · distance ${esc(props.distance_m ?? "—")} m</div>
            ${fields}
          </div>`)
        .addTo(previewMap);
    });
    previewMap.on("mouseenter", "supp-preview-target-fill", () => { previewMap.getCanvas().style.cursor = "pointer"; });
    previewMap.on("mouseleave", "supp-preview-target-fill", () => { previewMap.getCanvas().style.cursor = ""; });
    return previewMap;
  }

  function setMapVisible(visible) {
    els.mapWrap?.classList.toggle("hidden", !visible);
    els.splash?.classList.toggle("hidden", visible);
    if (visible) ensurePreviewMap()?.resize();
  }

  function clearPreview() {
    if (!previewMap) return;
    PREVIEW_LAYERS.forEach((id) => {
      if (previewMap.getLayer(id)) previewMap.removeLayer(id);
    });
    if (previewMap.getSource("supp-preview")) previewMap.removeSource("supp-preview");
    previewPopup?.remove();
    previewPopup = null;
  }

  function renderPreviewResult(job) {
    const stats = job.stats || {};
    const geojson = job.geojson || { type: "FeatureCollection", features: [] };
    const map = ensurePreviewMap();
    clearPreview();
    previewShownFields = geojson.shown_fields || [];
    map.addSource("supp-preview", { type: "geojson", data: geojson });
    const confidenceColor = [
      "match", ["get", "confidence"],
      "high", COLORS.high,
      "medium", COLORS.medium,
      "low", COLORS.low,
      COLORS.none
    ];
    map.addLayer({
      id: "supp-preview-target-fill",
      type: "fill",
      source: "supp-preview",
      filter: ["==", ["get", "layer"], "target"],
      paint: { "fill-color": confidenceColor, "fill-opacity": 0.55 }
    });
    map.addLayer({
      id: "supp-preview-target-line",
      type: "line",
      source: "supp-preview",
      filter: ["==", ["get", "layer"], "target"],
      paint: { "line-color": confidenceColor, "line-width": 1 }
    });
    const stateColor = ["match", ["get", "state"], "new", COLORS.new, "matched", COLORS.matched, COLORS.unmatched];
    map.addLayer({
      id: "supp-preview-source-line",
      type: "line",
      source: "supp-preview",
      filter: ["all", ["==", ["get", "layer"], "source"], ["!=", ["geometry-type"], "Point"]],
      paint: { "line-color": stateColor, "line-width": ["match", ["get", "state"], "new", 2.2, 1.2], "line-dasharray": [2, 1.5] }
    });
    map.addLayer({
      id: "supp-preview-source-point",
      type: "circle",
      source: "supp-preview",
      filter: ["all", ["==", ["get", "layer"], "source"], ["==", ["geometry-type"], "Point"]],
      paint: { "circle-radius": 3.5, "circle-color": stateColor, "circle-stroke-color": "#fff", "circle-stroke-width": 1 }
    });
    const capped = geojson.features.length >= 6000 ? "<br><span class='etl-hint'>Showing the first 6,000 buildings in view.</span>" : "";
    showStatus("success", `
      <div class="supp-result">
        <strong>Preview of the current map view</strong>
        ${statsTiles(stats)}
        ${confidenceBar(stats)}
        <div class="supp-legend">
          <span><i class="is-outline" style="color:${COLORS.matched}"></i>source matched</span>
          <span><i class="is-outline" style="color:${COLORS.new}"></i>source → new building</span>
          <span><i class="is-outline" style="color:${COLORS.unmatched}"></i>source unused</span>
        </div>
        <span class="etl-hint">Click a building on the map to see its match and values.${capped}</span>
        <div class="supp-result-actions">
          <button type="button" class="etl-tab-button" data-role="clear-preview">Clear preview</button>
        </div>
      </div>
    `);
    els.status.querySelector("[data-role='clear-preview']")?.addEventListener("click", () => {
      clearPreview();
      hideStatus();
    });
  }

  // ---------------------------------------------------------------------
  // Reset / open
  // ---------------------------------------------------------------------

  function reset() {
    jobToken += 1;
    currentJobId = null;
    inspected = null;
    columnRows = [];
    filterValues = [];
    els.form.reset();
    els.minIouValue.textContent = "0.50";
    els.summary.classList.add("hidden");
    els.layerPanel.classList.add("hidden");
    els.layerPanel.innerHTML = "";
    els.geometryPanel.classList.add("hidden");
    els.crsRow.classList.add("hidden");
    els.columnList.innerHTML = "";
    ["match", "columns", "output"].forEach((step) => panel(step).classList.add("hidden"));
    els.actions.classList.add("hidden");
    els.disk.classList.add("hidden");
    setBusy(false);
    hideStatus();
    clearPreview();
    setActiveStep("source");
    refreshStepState();
    onOutputModeChanged();
  }

  window.supplementUi = {
    reset,
    onOpen: prefillTarget,
    setMapVisible,
    get map() {
      return previewMap;
    }
  };

  onOutputModeChanged();
  refreshStepState();
})();
