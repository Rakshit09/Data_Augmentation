(() => {
  const fields = [
    { key: "occupancy_group", label: "Use" },
    { key: "occupancy_raw", label: "Raw occupancy" },
    { key: "occupancy_code", label: "Occupancy code" },
    { key: "height_raw", label: "Raw height" },
    { key: "height_m", label: "Height (m)", format: (value) => formatNumber(value, " m") },
    { key: "height_quality", label: "Height quality" },
    { key: "footprint_area_m2", label: "Footprint", format: (value) => formatNumber(value, " m2") },
    { key: "floorspace_est_m2", label: "Floorspace", format: (value) => formatNumber(value, " m2") },
    { key: "attribute_completeness_score", label: "Completeness", format: formatPercent },
    { key: "year_built", label: "Year built" },
    { key: "construction", label: "Construction" },
    { key: "roof_type", label: "Roof type" },
    { key: "basement", label: "Basement" },
    { key: "source", label: "Source" },
    { key: "last_update", label: "Updated" }
  ];
  const selected = new Set(fields.map((field) => field.key));
  const controls = document.getElementById("buildingInfoFieldOptions");

  controls.innerHTML = fields
    .map((field) => `
      <label class="lookup-field-option">
        <input type="checkbox" value="${escapeHtml(field.key)}" checked>
        <span>${escapeHtml(field.label)}</span>
      </label>
    `)
    .join("");

  controls.addEventListener("change", (event) => {
    if (!event.target.matches("input[type=checkbox]")) return;
    if (event.target.checked) {
      selected.add(event.target.value);
    } else {
      selected.delete(event.target.value);
    }
    window.dispatchEvent(new CustomEvent("building-info-fields-change"));
  });

  window.buildingInfoFields = {
    render(building) {
      return fields
        .filter((field) => selected.has(field.key))
        .map((field) => {
          const rawValue = building[field.key];
          const value = field.format ? field.format(rawValue) : rawValue;
          return [field.label, value];
        })
        .filter(([, value]) => value !== null && value !== undefined && value !== "")
        .map(([label, value]) => `<dt>${escapeHtml(label)}</dt><dd>${escapeHtml(value)}</dd>`)
        .join("");
    }
  };
})();
