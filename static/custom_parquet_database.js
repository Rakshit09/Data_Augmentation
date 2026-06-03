(() => {
  const parquetPath = document.getElementById("customParquetPath");
  const browseButton = document.getElementById("browseCustomParquet");
  const inspectButton = document.getElementById("inspectCustomParquet");
  const mappingsPanel = document.getElementById("customParquetMappings");
  const addFieldButton = document.getElementById("addCustomField");
  const extraFieldsContainer = document.getElementById("customExtraFields");
  const dbPath = document.getElementById("customDbPath");
  const runButton = document.getElementById("runCustomParquet");
  const customStatus = document.getElementById("customParquetStatus");
  const maxExtraFields = 10;
  const selectors = {
    latitude: document.getElementById("customLatitude"),
    longitude: document.getElementById("customLongitude"),
    geometry: document.getElementById("customGeometry"),
    occupancy: document.getElementById("customOccupancy"),
    height: document.getElementById("customHeight"),
    year_built: document.getElementById("customYearBuilt"),
    construction: document.getElementById("customConstruction"),
    roof_type: document.getElementById("customRoofType"),
    basement: document.getElementById("customBasement")
  };
  const optionalMappings = new Set(["height", "year_built", "construction", "roof_type", "basement"]);
  let inspectedColumns = [];

  addFieldButton.addEventListener("click", () => {
    if (extraFieldCount() >= maxExtraFields) return;
    addExtraFieldRow();
  });

  browseButton.addEventListener("click", async () => {
    browseButton.disabled = true;
    showStatus("info", "Opening Parquet file picker...");

    try {
      const response = await fetch("api/browse-file?kind=parquet");
      const payload = await response.json();
      if (!response.ok) throw new Error(payload.error || "Could not open file picker");
      if (payload.cancelled) {
        showStatus("info", "File selection cancelled.");
        return;
      }
      parquetPath.value = payload.path || "";
      showStatus("info", "Parquet selected. Inspect its columns next.");
    } catch (error) {
      showStatus("error", error.message);
    } finally {
      browseButton.disabled = false;
    }
  });

  inspectButton.addEventListener("click", async () => {
    inspectButton.disabled = true;
    mappingsPanel.classList.add("hidden");
    showStatus("info", "Inspecting Parquet columns...");

    try {
      const response = await fetch("api/custom-parquet/inspect", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ parquet_path: parquetPath.value.trim() })
      });
      const payload = await response.json();
      if (!response.ok) throw new Error(payload.error || "Could not inspect Parquet file");

      parquetPath.value = payload.parquet_path || parquetPath.value;
      dbPath.value = payload.default_db_path || "";
      inspectedColumns = payload.columns || [];
      populateMappings(inspectedColumns, payload.suggested_mappings || {});
      renderExtraFieldRows();
      mappingsPanel.classList.remove("hidden");
      showStatus("success", `Found ${formatInteger(inspectedColumns.length)} columns. Review the mappings, then create the database.`);
    } catch (error) {
      inspectedColumns = [];
      renderExtraFieldRows();
      showStatus("error", error.message);
    } finally {
      inspectButton.disabled = false;
    }
  });

  runButton.addEventListener("click", async () => {
    runButton.disabled = true;
    statusEl.textContent = "Creating database";
    showStatus("info", "Submitting Parquet lookup database job...");

    try {
      const response = await fetch("api/custom-parquet/create-database", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          parquet_path: parquetPath.value.trim(),
          db_path: dbPath.value.trim(),
          mappings: Object.fromEntries(
            Object.entries(selectors).map(([key, select]) => [key, select.value])
          ),
          extra_fields: collectExtraFields()
        })
      });
      const payload = await response.json();
      if (!response.ok) throw new Error(payload.error || "Could not create database");
      pollProgress(payload.job_id);
    } catch (error) {
      statusEl.textContent = "Error";
      showStatus("error", error.message);
      runButton.disabled = false;
    }
  });

  function populateMappings(columns, suggestedMappings) {
    const columnOptions = columns
      .map((column) => `<option value="${escapeHtml(column.name)}">${escapeHtml(column.name)} (${escapeHtml(column.type)})</option>`)
      .join("");

    Object.entries(selectors).forEach(([key, select]) => {
      const emptyOption = optionalMappings.has(key)
        ? '<option value="">Not mapped</option>'
        : '<option value="">Select column</option>';
      select.innerHTML = emptyOption + columnOptions;
      if (suggestedMappings[key]) select.value = suggestedMappings[key];
    });

    extraFieldsContainer.querySelectorAll("select").forEach((select) => {
      const currentValue = select.value;
      select.innerHTML = '<option value="">Select column</option>' + columnOptions;
      if (currentValue) select.value = currentValue;
    });
    updateAddFieldButton();
  }

  function addExtraFieldRow(field = {}) {
    const row = document.createElement("div");
    row.className = "custom-field-row";
    row.innerHTML = `
      <label>Field name
        <input type="text" data-role="name" placeholder="custom_field" value="${escapeHtml(field.name || "")}">
      </label>
      <label>Source column
        <select data-role="column"></select>
      </label>
      <button class="etl-tab-button" type="button" data-role="remove">Remove</button>
    `;

    const select = row.querySelector('select[data-role="column"]');
    select.innerHTML = '<option value="">Select column</option>' + inspectedColumns
      .map((column) => `<option value="${escapeHtml(column.name)}">${escapeHtml(column.name)} (${escapeHtml(column.type)})</option>`)
      .join("");
    if (field.column) select.value = field.column;

    row.querySelector('[data-role="remove"]').addEventListener("click", () => {
      row.remove();
      updateAddFieldButton();
    });

    extraFieldsContainer.append(row);
    updateAddFieldButton();
  }

  function renderExtraFieldRows(fields = []) {
    extraFieldsContainer.innerHTML = "";
    fields.forEach((field) => addExtraFieldRow(field));
    updateAddFieldButton();
  }

  function collectExtraFields() {
    return Array.from(extraFieldsContainer.querySelectorAll(".custom-field-row"))
      .map((row) => ({
        name: row.querySelector('[data-role="name"]').value.trim(),
        column: row.querySelector('[data-role="column"]').value.trim()
      }))
      .filter((field) => field.name || field.column);
  }

  function extraFieldCount() {
    return extraFieldsContainer.querySelectorAll(".custom-field-row").length;
  }

  function updateAddFieldButton() {
    addFieldButton.disabled = !inspectedColumns.length || extraFieldCount() >= maxExtraFields;
  }

  async function pollProgress(jobId) {
    try {
      const response = await fetch(`api/custom-parquet/progress/${jobId}`);
      const payload = await response.json();
      if (!response.ok) throw new Error(payload.error || "Could not read database progress");

      const percent = Math.max(0, Math.min(100, Number(payload.percent || 0)));
      showStatus("info", `
        <div class="progress-copy">${escapeHtml(payload.phase || payload.status || "Working")}</div>
        <div class="progress-track"><div class="progress-fill" style="width:${percent}%"></div></div>
        <div class="progress-copy">${percent.toFixed(0)}%</div>
      `);

      if (payload.status === "complete") {
        statusEl.textContent = "Done";
        showStatus("success", `
          <strong>Database created and activated.</strong><br>
          Parquet: <code>${escapeHtml(payload.parquet_path || "")}</code><br>
          DuckDB lookup table: <code>${escapeHtml(payload.db_path || "")}</code><br>
          Buildings: ${escapeHtml(formatInteger(payload.row_count))}
        `);
        runButton.disabled = false;
        await loadDataSources();
        return;
      }

      if (payload.status === "error") throw new Error(payload.error || "Database creation failed");
      window.setTimeout(() => pollProgress(jobId), 1500);
    } catch (error) {
      statusEl.textContent = "Error";
      showStatus("error", error.message);
      runButton.disabled = false;
    }
  }

  function showStatus(type, html) {
    customStatus.classList.remove("hidden", "etl-status--error", "etl-status--success");
    if (type === "error") customStatus.classList.add("etl-status--error");
    if (type === "success") customStatus.classList.add("etl-status--success");
    customStatus.innerHTML = html;
  }

  updateAddFieldButton();
})();
