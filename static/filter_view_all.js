(function () {
  const ALL_VALUE = "__ALL__";
  const legendId = "filterViewLegend";

  function allValue() {
    return ALL_VALUE;
  }

  function ensureLegend(container) {
    let legend = document.getElementById(legendId);
    if (legend) return legend;
    legend = document.createElement("div");
    legend.id = legendId;
    legend.className = "filter-view-legend hidden";
    container?.insertAdjacentElement("afterend", legend);
    return legend;
  }

  function renderLegend(payload, columnLabel) {
    const message = document.getElementById("filterViewMessage");
    const legend = ensureLegend(message);
    const items = Array.isArray(payload?.legend) ? payload.legend : [];
    if (!items.length) {
      clearLegend();
      return;
    }

    legend.classList.remove("hidden");
    legend.innerHTML = `
      <div class="filter-view-legend-header">
        <strong>${escapeHtml(columnLabel || "All values")}</strong>
        <span>${escapeHtml(items.length)} shown</span>
      </div>
      <div class="filter-view-legend-list">
        ${items.map((item) => `
          <div class="filter-view-legend-item">
            <span class="filter-view-legend-swatch" style="background:${escapeHtml(item.color || "#64748b")}"></span>
            <span class="filter-view-legend-label">${escapeHtml(item.value)}</span>
            <span class="filter-view-legend-count">${escapeHtml(formatInteger(item.count))}</span>
          </div>
        `).join("")}
      </div>
    `;
  }

  function clearLegend() {
    const legend = document.getElementById(legendId);
    if (!legend) return;
    legend.classList.add("hidden");
    legend.innerHTML = "";
  }

  function isAll(value) {
    return value === ALL_VALUE;
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

  window.filterViewAll = {
    allValue,
    clearLegend,
    isAll,
    renderLegend
  };
})();
