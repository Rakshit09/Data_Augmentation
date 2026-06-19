(() => {
  const controls = document.getElementById("exposureFieldOptions");
  const selectedFields = new Set();
  let fields = [];

  function ensureActionButtons() {
    const fieldPicker = controls?.closest(".lookup-field-picker");
    if (!fieldPicker) return { markAllBtn: null, clearAllBtn: null };
    fieldPicker.open = true;

    let actions = fieldPicker.querySelector(".lookup-field-actions");
    if (!actions) {
      actions = document.createElement("div");
      actions.className = "lookup-field-actions";
      actions.innerHTML = [
        '<button type="button" id="markAllFields">Mark all</button>',
        '<button type="button" id="clearAllFields">Clear all</button>'
      ].join("");
      controls.before(actions);
    }

    return {
      markAllBtn: actions.querySelector("#markAllFields"),
      clearAllBtn: actions.querySelector("#clearAllFields")
    };
  }

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
      const response = await fetch("api/building-fields");
      const payload = await response.json();
      if (!response.ok) throw new Error(payload.error || "Could not load database fields");

      fields = payload.fields || [];
      const defaultFields = new Set(payload.default_fields || fields);
      selectedFields.clear();
      fields.forEach((field) => {
        if (defaultFields.has(field)) {
          selectedFields.add(field);
        }
      });
      controls.innerHTML = fields.length
        ? fields.map((field) => `
            <label class="lookup-field-option">
              <input type="checkbox" value="${escapeHtml(field)}" ${selectedFields.has(field) ? "checked" : ""}>
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

  const { markAllBtn, clearAllBtn } = ensureActionButtons();

  if (markAllBtn) {
    markAllBtn.addEventListener("click", () => {
      controls.querySelectorAll("input[type=checkbox]").forEach(cb => { cb.checked = true; selectedFields.add(cb.value); });
    });
  }

  if (clearAllBtn) {
    clearAllBtn.addEventListener("click", () => {
      controls.querySelectorAll("input[type=checkbox]").forEach(cb => { cb.checked = false; });
      selectedFields.clear();
    });
  }

  load();
})();
