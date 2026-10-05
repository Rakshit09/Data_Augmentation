// Exposure from SQL Server (Windows auth) and write-back of enrichment / raster results.
(() => {
  const STORAGE_KEY = "sqlServerSource.last";
  const DEFAULT_MESSAGE = "Connects with your Windows login.";

  const choice = document.getElementById("exposureSourceChoice");
  const openButton = document.getElementById("sqlSourceOpen");
  const panel = document.getElementById("sqlSourcePanel");
  const backButton = document.getElementById("sqlSourceBack");
  const serverSelect = document.getElementById("sqlServer");
  const databaseSelect = document.getElementById("sqlDatabase");
  const tableSelect = document.getElementById("sqlTable");
  const loadButton = document.getElementById("sqlLoadTable");
  const message = document.getElementById("sqlSourceMessage");
  const writeBackPanel = document.getElementById("sqlWriteBackPanel");
  if (!choice || !panel || !writeBackPanel) return;

  window.makeSearchableSelect?.(databaseSelect);
  window.makeSearchableSelect?.(tableSelect);

  let serversLoaded = false;
  let listRequest = 0;
  let writeBackRequest = 0;
  let loading = false;
  let tables = [];
  let activeSource = null;
  let writeBack = null;

  // ---------------------------------------------------------------- helpers

  function escapeHtml(value) {
    return String(value ?? "")
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;")
      .replaceAll("'", "&#039;");
  }

  function formatInteger(value) {
    return Number(value || 0).toLocaleString();
  }

  function remembered() {
    try {
      return JSON.parse(localStorage.getItem(STORAGE_KEY)) || {};
    } catch (_error) {
      return {};
    }
  }

  function remember(values) {
    try {
      localStorage.setItem(STORAGE_KEY, JSON.stringify({ ...remembered(), ...values }));
    } catch (_error) {
      // Private mode or storage disabled: remembering selections is optional.
    }
  }

  function setMessage(element, text, type = "") {
    if (!element) return;
    element.textContent = text || "";
    element.classList.toggle("error", type === "error");
    element.classList.toggle("success", type === "success");
  }

  async function requestJson(url, body) {
    const options = body === undefined
      ? {}
      : { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) };
    const response = await fetch(url, options);
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(payload.error || `Request failed (${response.status})`);
    return payload;
  }

  function fillSelect(select, items, placeholder, selected) {
    select.innerHTML = [
      `<option value="">${escapeHtml(placeholder)}</option>`,
      ...items.map((item) => `<option value="${escapeHtml(item.value)}" data-meta="${escapeHtml(item.meta || "")}">${escapeHtml(item.label)}</option>`)
    ].join("");
    select.value = items.some((item) => item.value === selected) ? selected : "";
  }

  function disableSelect(select, placeholder) {
    fillSelect(select, [], placeholder, "");
    select.disabled = true;
  }

  function tableKey(table) {
    return `${table.schema}.${table.table}`;
  }

  function selectedTable() {
    return tableSelect.value === "" ? null : tables[Number(tableSelect.value)] || null;
  }

  function syncLoadButton() {
    loadButton.disabled = loading || !selectedTable();
  }

  // ---------------------------------------------------------------- source picker

  function showPanel(show) {
    choice.classList.toggle("hidden", show);
    panel.classList.toggle("hidden", !show);
    if (show) void loadServers();
  }

  async function loadServers() {
    if (serversLoaded) return;
    try {
      const payload = await requestJson("api/sql/servers");
      const servers = payload.servers || [];
      fillSelect(serverSelect, servers.map((name) => ({ value: name, label: name })), "Choose a server", remembered().server || servers[0]);
      serversLoaded = true;
      if (!payload.driver) {
        setMessage(message, "No SQL Server ODBC driver found. Install 'ODBC Driver 17 for SQL Server' or newer.", "error");
        return;
      }
      if (serverSelect.value) await loadDatabases();
    } catch (error) {
      setMessage(message, error.message, "error");
    }
  }

  async function loadDatabases() {
    const requestId = ++listRequest;
    resetTables("");
    if (!serverSelect.value) {
      disableSelect(databaseSelect, "");
      return;
    }
    disableSelect(databaseSelect, "Loading databases...");
    setMessage(message, `Connecting to ${serverSelect.value}...`);
    try {
      const payload = await requestJson(`api/sql/databases?server=${encodeURIComponent(serverSelect.value)}`);
      if (requestId !== listRequest) return;
      const databases = payload.databases || [];
      fillSelect(databaseSelect, databases.map((name) => ({ value: name, label: name })), "Type to search databases", remembered().database);
      databaseSelect.disabled = false;
      setMessage(message, `${formatInteger(databases.length)} databases available on ${serverSelect.value}.`);
      if (databaseSelect.value) await loadTables();
    } catch (error) {
      if (requestId !== listRequest) return;
      disableSelect(databaseSelect, "");
      setMessage(message, error.message, "error");
    }
  }

  function resetTables(placeholder) {
    tables = [];
    disableSelect(tableSelect, placeholder);
    syncLoadButton();
  }

  async function loadTables() {
    const requestId = ++listRequest;
    const database = databaseSelect.value;
    if (!database) {
      resetTables("");
      return;
    }
    resetTables("Loading tables...");
    try {
      const query = `server=${encodeURIComponent(serverSelect.value)}&database=${encodeURIComponent(database)}`;
      const payload = await requestJson(`api/sql/tables?${query}`);
      if (requestId !== listRequest) return;
      tables = payload.tables || [];
      tableSelect.disabled = !tables.length;
      renderTables(remembered().table);
      setMessage(message, tables.length ? `${formatInteger(tables.length)} tables in ${database}.` : `No tables found in ${database}.`);
    } catch (error) {
      if (requestId !== listRequest) return;
      resetTables("");
      setMessage(message, error.message, "error");
    }
  }

  function renderTables(selectedKey) {
    const items = tables.map((table, index) => ({
      value: String(index),
      key: tableKey(table),
      label: tableKey(table),
      meta: `${formatInteger(table.rows)} rows`
    }));
    const match = items.find((item) => item.key === selectedKey);
    fillSelect(tableSelect, items, items.length ? "Type to search tables" : "No tables", match ? match.value : "");
    syncLoadButton();
  }

  async function loadTable() {
    const table = selectedTable();
    if (!table || loading || typeof window.loadExposureSource !== "function") return;

    const server = serverSelect.value;
    const database = databaseSelect.value;
    remember({ server, database, table: tableKey(table) });
    loading = true;
    syncLoadButton();
    loadButton.textContent = "Loading...";
    setMessage(message, `Reading preview of ${tableKey(table)}...`);

    try {
      const payload = await window.loadExposureSource({
        displayName: table.table,
        loadingText: `Reading preview of ${tableKey(table)} from ${server}...`,
        request: () => fetch("api/sql/load", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ server, database, schema: table.schema, table: table.table })
        })
      });
      if (!payload) {
        const status = document.getElementById("status")?.textContent;
        const summary = document.getElementById("uploadSummary")?.textContent?.trim();
        setMessage(message, status === "Error" ? summary || "Could not load the table." : "", status === "Error" ? "error" : "");
        return;
      }

      activeSource = { upload_id: payload.upload_id, ...payload.sql_source };
      // The CSV tile must not keep showing the table name when the user switches back.
      if (typeof setUploadedCsvName === "function") setUploadedCsvName("");
      const csvInput = document.getElementById("csvFile");
      if (csvInput) csvInput.value = "";
      void trackCopy(activeSource, `${database}.${tableKey(table)}`);
    } finally {
      loading = false;
      loadButton.textContent = "Load table";
      syncLoadButton();
    }
  }

  // Preview is shown immediately; the full table keeps copying in the background.
  async function trackCopy(source, label) {
    setCopyGate(true);
    let status = source.export || { status: "running", rows: 0, total: source.row_count };
    while (activeSource === source && status.status === "running") {
      renderCopyProgress(status, label);
      await new Promise((resolve) => window.setTimeout(resolve, 1000));
      if (activeSource !== source) break;
      try {
        status = await requestJson(`api/sql/load/${source.upload_id}/progress`);
      } catch (error) {
        status = { status: "error", error: error.message };
      }
    }
    if (activeSource !== source) return;
    source.export = status;
    setCopyGate(false);

    if (status.status === "complete") {
      source.row_count = status.rows;
      const keys = source.key_columns?.length ? ` Rows will match on ${source.key_columns.join(", ")}.` : "";
      setMessage(
        message,
        `Loaded ${formatInteger(status.rows)} rows from ${label}. After enrichment or raster sampling you can write results back to this table.${keys}`,
        "success"
      );
    } else {
      setMessage(message, `Table copy failed: ${status.error || status.status}`, "error");
    }
  }

  function renderCopyProgress(status, label) {
    const total = Number(status.total) || 0;
    const percent = total ? Math.min(99, (Number(status.rows) / total) * 100) : 0;
    message.classList.remove("error", "success");
    message.innerHTML = `
      <span>Preview ready. Copying ${escapeHtml(label)}: ${formatInteger(status.rows)}${total ? ` / ${formatInteger(total)}` : ""} rows</span>
      <span class="sql-progress"><span style="width:${percent.toFixed(1)}%"></span></span>
    `;
  }

  // Map and enrichment need the full table; keep them disabled until the copy finishes.
  function setCopyGate(copying) {
    ["runEnrichment", "showExposureOnMap"].forEach((id) => {
      const button = document.getElementById(id);
      if (!button) return;
      button.disabled = copying;
      button.title = copying ? "Waiting for the SQL Server table to finish copying" : "";
    });
  }

  function cancelCopy(source) {
    if (source?.export?.status !== "running") return;
    void fetch(`api/sql/load/${source.upload_id}/cancel`, { method: "POST", keepalive: true }).catch(() => {});
  }

  // ---------------------------------------------------------------- write-back

  function writeBackKeysStorageKey(target) {
    return `sqlServerSource.keys.${target.server}|${target.database}|${target.schema}.${target.table}`;
  }

  function hideWriteBack() {
    writeBackRequest += 1;
    writeBack = null;
    writeBackPanel.classList.add("hidden");
    writeBackPanel.innerHTML = "";
  }

  async function offerWriteBack({ kind, jobId, sourceType } = {}) {
    const requestId = ++writeBackRequest;
    const isExposureResult = kind === "enrichment" || ["exposure", "vector_exposure"].includes(sourceType);
    const currentUploadId = window.getExposureUploadState?.()?.upload_id;
    if (!activeSource || !jobId || !isExposureResult || currentUploadId !== activeSource.upload_id) {
      hideWriteBack();
      return;
    }

    try {
      const plan = await requestJson("api/sql/write-back/plan", {
        upload_id: activeSource.upload_id,
        kind,
        job_id: jobId
      });
      if (requestId !== writeBackRequest) return;
      writeBack = { kind, jobId, uploadId: activeSource.upload_id, plan };
      renderWriteBack();
    } catch (error) {
      if (requestId !== writeBackRequest) return;
      writeBackPanel.innerHTML = `<p class="sql-message error">${escapeHtml(error.message)}</p>`;
      writeBackPanel.classList.remove("hidden");
    }
  }

  function defaultKeys(plan) {
    try {
      const saved = JSON.parse(localStorage.getItem(writeBackKeysStorageKey(plan.target)) || "null");
      if (Array.isArray(saved) && saved.length && saved.every((key) => plan.table_columns.includes(key))) return saved;
    } catch (_error) {
      // Fall back to the server-suggested keys.
    }
    return plan.key_columns || [];
  }

  function renderWriteBack() {
    const { plan } = writeBack;
    const target = plan.target;
    const keys = new Set(defaultKeys(plan));
    const columns = plan.columns || [];

    const keyOptions = plan.table_columns.map((name) => `
      <label class="lookup-field-option">
        <input type="checkbox" name="sqlKey" value="${escapeHtml(name)}" ${keys.has(name) ? "checked" : ""}>
        <span>${escapeHtml(name)}</span>
      </label>
    `).join("");

    const columnRows = columns.map((column) => `
      <div class="sql-column-row">
        <label class="sql-column-source" title="${escapeHtml(column.name)}">
          <input type="checkbox" name="sqlWrite" value="${escapeHtml(column.name)}" ${column.selected ? "checked" : ""}>
          <span>${escapeHtml(column.name)}</span>
        </label>
        <span class="sql-column-arrow" aria-hidden="true">&rarr;</span>
        <input type="text" class="sql-column-target" data-source="${escapeHtml(column.name)}" value="${escapeHtml(column.name)}" maxlength="128" aria-label="Table column for ${escapeHtml(column.name)}">
        <span class="sql-badge" data-badge-for="${escapeHtml(column.name)}"></span>
      </div>
    `).join("");

    writeBackPanel.innerHTML = `
      <div class="sql-panel-header">
        <span class="dropzone-title">Write back to SQL Server</span>
        <span class="field-tip" tabindex="0" data-tooltip="Updates the source table in place. Rows are matched on the key columns; new columns are created automatically and existing ones are overwritten. Rows without a result are left unchanged. Everything runs in one transaction.">i</span>
      </div>
      <p class="sql-target">
        <span>${escapeHtml(target.server)}</span>
        <span>${escapeHtml(target.database)}</span>
        <strong>${escapeHtml(`${target.schema}.${target.table}`)}</strong>
      </p>
      <details class="lookup-field-picker sql-keys" ${keys.size ? "" : "open"}>
        <summary>
          <span class="field-label-row"><span>Match rows on</span></span>
          <span class="sql-key-summary"></span>
        </summary>
        <div class="lookup-field-options">${keyOptions}</div>
      </details>
      ${columns.length ? `
        <div class="sql-column-map">
          <div class="sql-column-head"><span>Result column</span><span></span><span>Table column</span><span></span></div>
          ${columnRows}
        </div>
        <button id="sqlWriteBackRun" class="sql-primary-button" type="button">Write to table</button>
      ` : ""}
      <p id="sqlWriteBackMessage" class="sql-message">${columns.length
        ? "Rename a target to keep several runs side by side, e.g. RP100_depth."
        : "This result has no new columns to write."}</p>
    `;
    writeBackPanel.classList.remove("hidden");
    syncWriteBackState();
  }

  function checkedValues(name) {
    return [...writeBackPanel.querySelectorAll(`input[name="${name}"]:checked`)].map((input) => input.value);
  }

  function syncWriteBackState() {
    if (!writeBack) return;
    const tableColumns = new Set(writeBack.plan.table_columns.map((name) => name.toLowerCase()));
    const keys = checkedValues("sqlKey");
    const writes = new Set(checkedValues("sqlWrite"));

    const summary = writeBackPanel.querySelector(".sql-key-summary");
    if (summary) {
      summary.textContent = keys.length ? keys.join(", ") : "Choose key columns";
      summary.classList.toggle("missing", !keys.length);
    }

    writeBackPanel.querySelectorAll(".sql-column-target").forEach((input) => {
      const source = input.dataset.source;
      const enabled = writes.has(source);
      const exists = tableColumns.has(input.value.trim().toLowerCase());
      input.disabled = !enabled;
      const badge = [...writeBackPanel.querySelectorAll("[data-badge-for]")].find((el) => el.dataset.badgeFor === source);
      if (badge) {
        badge.textContent = enabled ? (exists ? "overwrite" : "new") : "";
        badge.classList.toggle("overwrite", enabled && exists);
      }
    });

    const runButton = writeBackPanel.querySelector("#sqlWriteBackRun");
    if (runButton && !runButton.dataset.busy) {
      runButton.disabled = !keys.length || !writes.size;
      runButton.textContent = writes.size > 1 ? `Write ${writes.size} columns to table` : "Write to table";
    }
  }

  async function runWriteBack() {
    if (!writeBack) return;
    const { plan, kind, jobId, uploadId } = writeBack;
    const target = plan.target;
    const keyColumns = checkedValues("sqlKey");
    const columns = {};
    writeBackPanel.querySelectorAll(".sql-column-target:not(:disabled)").forEach((input) => {
      columns[input.dataset.source] = input.value.trim() || input.dataset.source;
    });
    const resultMessage = writeBackPanel.querySelector("#sqlWriteBackMessage");
    const runButton = writeBackPanel.querySelector("#sqlWriteBackRun");

    const summary = Object.entries(columns).map(([source, name]) => (source === name ? name : `${source} → ${name}`)).join(", ");
    const confirmed = window.confirm(
      `Update ${target.schema}.${target.table} in ${target.database} on ${target.server}?\n\n`
      + `Columns: ${summary}\nMatched on: ${keyColumns.join(", ")}`
    );
    if (!confirmed) return;

    runButton.dataset.busy = "1";
    runButton.disabled = true;
    runButton.textContent = "Writing...";
    setMessage(resultMessage, "Uploading results and updating the table...");

    try {
      const result = await requestJson("api/sql/write-back", {
        upload_id: uploadId,
        kind,
        job_id: jobId,
        key_columns: keyColumns,
        columns
      });
      if (writeBack?.jobId !== jobId) return;
      try {
        localStorage.setItem(writeBackKeysStorageKey(target), JSON.stringify(keyColumns));
      } catch (_error) {
        // Remembering key columns is optional.
      }
      plan.table_columns.push(...result.columns_added);
      const added = result.columns_added.length ? ` Added ${result.columns_added.join(", ")}.` : "";
      const unmatched = result.rows_updated < result.rows_in_results
        ? ` ${formatInteger(result.rows_in_results - result.rows_updated)} result rows had no matching key.`
        : "";
      setMessage(
        resultMessage,
        `Updated ${formatInteger(result.rows_updated)} table rows from ${formatInteger(result.rows_in_results)} result rows.${added}${unmatched}`,
        "success"
      );
    } catch (error) {
      setMessage(resultMessage, error.message, "error");
    } finally {
      delete runButton.dataset.busy;
      syncWriteBackState();
    }
  }

  // ---------------------------------------------------------------- wiring

  function reset() {
    if (activeSource) {
      cancelCopy(activeSource);
      setCopyGate(false);
    }
    activeSource = null;
    hideWriteBack();
    showPanel(false);
    if (tables.length) renderTables(remembered().table);
    setMessage(message, DEFAULT_MESSAGE);
  }

  openButton.addEventListener("click", () => showPanel(true));
  backButton?.addEventListener("click", () => showPanel(false));
  serverSelect.addEventListener("change", () => {
    remember({ server: serverSelect.value });
    void loadDatabases();
  });
  databaseSelect.addEventListener("change", () => {
    remember({ database: databaseSelect.value });
    void loadTables();
  });
  tableSelect.addEventListener("change", syncLoadButton);
  loadButton.addEventListener("click", () => void loadTable());

  writeBackPanel.addEventListener("change", syncWriteBackState);
  writeBackPanel.addEventListener("input", syncWriteBackState);
  writeBackPanel.addEventListener("click", (event) => {
    if (event.target.closest("#sqlWriteBackRun")) void runWriteBack();
  });

  window.addEventListener("exposure-upload-state-change", (event) => {
    if (activeSource && event.detail?.upload_id !== activeSource.upload_id) {
      cancelCopy(activeSource);
      setCopyGate(false);
      activeSource = null;
      hideWriteBack();
    }
  });

  window.sqlServerSource = { reset, offerWriteBack, hideWriteBack };
})();
