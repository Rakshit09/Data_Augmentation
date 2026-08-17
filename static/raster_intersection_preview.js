(function () {
  function render(payload) {
    if (typeof switchMode === "function") switchMode("exposure");
    if (typeof renderPreview === "function") {
      renderPreview(payload.columns || [], payload.rows || []);
    }
    const panel = ensurePanel();
    panel.classList.remove("hidden");
    panel.innerHTML = resultHtml(payload);
  }

  function ensurePanel() {
    let panel = document.getElementById("rasterIntersectionResultPanel");
    if (panel) return panel;
    panel = document.createElement("section");
    panel.id = "rasterIntersectionResultPanel";
    panel.className = "raster-result-panel hidden";
    const previewTable = document.getElementById("previewTable");
    if (previewTable && previewTable.parentElement) {
      previewTable.parentElement.insertBefore(panel, previewTable.nextSibling);
    }
    return panel;
  }

  function resultHtml(payload) {
    const summary = payload.summary || {};
    const downloads = payload.download_urls || {};
    const warning = summary.warning ? `<p>${escapeHtml(summary.warning)}</p>` : "";
    return `
      <div class="raster-result-header">
        <div>
          <h3>${escapeHtml(title(summary.source_type))}</h3>
          <p>${escapeHtml(formatInteger(summary.matched_count))} matched from ${escapeHtml(formatInteger(summary.candidate_count))} candidates${escapeHtml(detailText(summary))}</p>
          ${warning}
        </div>
      </div>
      <div class="raster-summary-grid">
        ${metric("Matched", formatInteger(summary.matched_count))}
        ${metric(valueLabel(summary, "min"), formatNumber(summary.raster_value_min))}
        ${metric(valueLabel(summary, "mean"), formatNumber(summary.raster_value_mean))}
        ${metric(valueLabel(summary, "max"), formatNumber(summary.raster_value_max))}
        ${metric("Sampling", samplingLabel(summary))}
        ${metric("Threshold matches", summary.count_matching_threshold == null ? "n/a" : formatInteger(summary.count_matching_threshold))}
        ${metric("Total SI", summary.total_si == null ? "n/a" : formatNumber(summary.total_si))}
        ${metric("SI field", summary.si_field || "n/a")}
        ${metric("Elapsed", `${formatNumber(summary.elapsed_seconds)} s`)}
      </div>
      <div class="raster-downloads">
        ${downloadLink(downloads.csv, "Download CSV")}
        ${downloadLink(downloads.parquet, "Download Parquet")}
        ${downloadLink(downloads.geojson, "Download GeoJSON")}
      </div>
    `;
  }

  function metric(label, value) {
    return `
      <div class="raster-summary-metric">
        <span>${escapeHtml(label)}</span>
        <strong>${escapeHtml(value)}</strong>
      </div>
    `;
  }

  function downloadLink(url, label) {
    if (!url) return "";
    return `<a href="${escapeHtml(url)}">${escapeHtml(label)}</a>`;
  }

  function title(sourceType) {
    if (sourceType === "database") return "Raster intersected with building database";
    if (sourceType === "vector_database") return "Vector intersected with building database";
    if (sourceType === "vector_exposure") return "Vector intersected with exposure";
    return "Raster intersected with exposure";
  }

  function detailText(summary) {
    if (summary.source_type === "vector_database" || summary.source_type === "vector_exposure") {
      return ` · Field ${summary.vector_field || "feature_id"}`;
    }
    const radiusText = summary.sample_radius_m == null ? "Point" : `Radius ${formatNumber(summary.sample_radius_m)} m`;
    const aggregation = summary.sample_radius_m == null ? "" : ` · ${samplingAggregationLabel(summary)}`;
    return ` · Band ${summary.raster_band || 1} · ${radiusText}${aggregation}`;
  }

  function samplingLabel(summary) {
    if (summary.source_type === "vector_database" || summary.source_type === "vector_exposure") {
      return "n/a";
    }
    if (summary.sample_radius_m == null) {
      return "Point";
    }
    return `${formatNumber(summary.sample_radius_m)} m ${samplingAggregationLabel(summary)}`;
  }

  function samplingAggregationLabel(summary) {
    const value = String(summary.sample_radius_aggregation || "mean").toLowerCase();
    if (value === "max") return "Max";
    if (value === "min") return "Min";
    return "Mean";
  }

  function valueLabel(summary, statistic) {
    const prefix = summary.source_type === "vector_database" || summary.source_type === "vector_exposure"
      ? "Value"
      : "Raster";
    return `${prefix} ${statistic}`;
  }

  function formatInteger(value) {
    return Number(value || 0).toLocaleString();
  }

  function formatNumber(value) {
    const number = Number(value);
    if (!Number.isFinite(number)) return "n/a";
    return number.toLocaleString(undefined, { maximumFractionDigits: 4 });
  }

  function escapeHtml(value) {
    return String(value ?? "")
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;")
      .replaceAll("'", "&#039;");
  }

  window.rasterIntersectionPreview = { render };
})();
