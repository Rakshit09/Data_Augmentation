(() => {
  const controls = document.getElementById("exposureFieldOptions");
  const selectedFields = new Set();
  let fields = [];

  controls.addEventListener("change", (event) => {
    if (!event.target.matches("input[type=checkbox]")) return;
    if (event.target.checked) {
      selectedFields.add(event.target.value);
    } else {
      selectedFields.delete(event.target.value);
    }
  });

  async function load() {
    controls.innerHTML = "<p>Loading database fields...</p>";

    try {
      const response = await fetch("/api/building-fields");
      const payload = await response.json();
      if (!response.ok) throw new Error(payload.error || "Could not load database fields");

      fields = payload.fields || [];
      selectedFields.clear();
      fields.forEach((field) => selectedFields.add(field));
      controls.innerHTML = fields.length
        ? fields.map((field) => `
            <label class="lookup-field-option">
              <input type="checkbox" value="${escapeHtml(field)}" checked>
              <span>${escapeHtml(field)}</span>
            </label>
          `).join("")
        : "<p>No appendable database fields found.</p>";
    } catch (error) {
      fields = [];
      selectedFields.clear();
      controls.innerHTML = `<p>${escapeHtml(error.message)}</p>`;
    }
  }

  window.exposureEnrichmentFields = {
    load,
    selected() {
      return fields.filter((field) => selectedFields.has(field));
    }
  };

  load();
})();
