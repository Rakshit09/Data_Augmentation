(() => {
  const controls = document.getElementById("buildingInfoFieldOptions");
  const selected = new Set();
  let fields = [];

  const formatters = {
    height_m: (value) => formatNumber(value, " m"),
    footprint_area_m2: (value) => formatNumber(value, " m2"),
    floorspace_obm_m2: (value) => formatNumber(value, " m2"),
    floorspace_est_m2: (value) => formatNumber(value, " m2"),
    attribute_completeness_score: formatPercent
  };

  controls.addEventListener("change", (event) => {
    if (!event.target.matches("input[type=checkbox]")) return;
    if (event.target.checked) {
      selected.add(event.target.value);
    } else {
      selected.delete(event.target.value);
    }
    notifyChange();
  });

  async function load() {
    controls.innerHTML = "<p>Loading database fields...</p>";

    try {
      const response = await fetch("api/building-fields");
      const payload = await response.json();
      if (!response.ok) throw new Error(payload.error || "Could not load database fields");

      fields = payload.fields || [];
      selected.clear();
      fields.forEach((field) => selected.add(field));
      controls.innerHTML = fields.length
        ? fields.map((field) => `
            <label class="lookup-field-option">
              <input type="checkbox" value="${escapeHtml(field)}" checked>
              <span>${escapeHtml(field)}</span>
            </label>
          `).join("")
        : "<p>No displayable database fields found.</p>";
      notifyChange();
    } catch (error) {
      fields = [];
      selected.clear();
      controls.innerHTML = `<p>${escapeHtml(error.message)}</p>`;
      notifyChange();
    }
  }

  function notifyChange() {
    window.dispatchEvent(new CustomEvent("building-info-fields-change"));
  }

  window.buildingInfoFields = {
    load,
    render(building) {
      return fields
        .filter((field) => selected.has(field))
        .map((field) => {
          const format = formatters[field];
          const value = format ? format(building[field]) : building[field];
          return [field, value];
        })
        .filter(([, value]) => value !== null && value !== undefined && value !== "")
        .map(([label, value]) => `<dt>${escapeHtml(label)}</dt><dd>${escapeHtml(value)}</dd>`)
        .join("");
    }
  };

  load();
})();
