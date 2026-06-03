(() => {
  const controls = document.getElementById("buildingInfoFieldOptions");
  const selected = new Set();
  let fields = [];
  let labels = new Map();

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

      const preferredFields = Array.isArray(payload.preferred_fields) ? payload.preferred_fields : [];
      const fieldEntries = preferredFields.length
        ? preferredFields
        : (payload.fields || []).map((field) => ({ field, label: formatLabel(field) }));
      fields = fieldEntries.map((entry) => entry.field);
      labels = new Map(fieldEntries.map((entry) => [entry.field, entry.label || formatLabel(entry.field)]));
      selected.clear();
      fields.forEach((field) => selected.add(field));
      controls.innerHTML = fields.length
        ? fieldEntries.map((entry) => `
            <label class="lookup-field-option">
              <input type="checkbox" value="${escapeHtml(entry.field)}" checked>
              <span>${escapeHtml(entry.label || formatLabel(entry.field))}</span>
            </label>
          `).join("")
        : "<p>No displayable database fields found.</p>";
      notifyChange();
    } catch (error) {
      fields = [];
      labels = new Map();
      selected.clear();
      controls.innerHTML = `<p>${escapeHtml(error.message)}</p>`;
      notifyChange();
    }
  }

  function notifyChange() {
    window.dispatchEvent(new CustomEvent("building-info-fields-change"));
  }

  function formatLabel(field) {
    return field
      .replaceAll("_", " ")
      .replace(/\b\w/g, (character) => character.toUpperCase())
      .replace(/\bM2\b/g, "m2")
      .replace(/\bObm\b/g, "OBM")
      .replace(/\bId\b/g, "ID");
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
        .map(([field, value]) => `
          <tr${field.toLowerCase() === "source" ? ' class="building-info-source"' : ""}>
            <th scope="row">${escapeHtml(labels.get(field) || formatLabel(field))}</th>
            <td>${escapeHtml(value)}</td>
          </tr>
        `)
        .join("");
    }
  };

  load();
})();
