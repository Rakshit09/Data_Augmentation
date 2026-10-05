// Turns a <select> into a type-to-filter combobox. The hidden <select> stays the source of truth,
// so existing code can keep reading .value, refilling options, toggling .disabled and listening for "change".
window.makeSearchableSelect = (select, { maxResults = 300 } = {}) => {
  const wrapper = document.createElement("div");
  wrapper.className = "search-select";
  const input = document.createElement("input");
  input.type = "text";
  input.className = "search-select-input";
  input.autocomplete = "off";
  input.spellcheck = false;
  input.setAttribute("role", "combobox");
  input.setAttribute("aria-autocomplete", "list");
  input.setAttribute("aria-expanded", "false");
  const list = document.createElement("ul");
  list.className = "search-select-list hidden";
  list.id = `${select.id || "searchSelect"}Options`;
  list.setAttribute("role", "listbox");
  input.setAttribute("aria-controls", list.id);

  select.before(wrapper);
  // Input first so a wrapping <label> focuses it rather than the hidden select.
  wrapper.append(input, list, select);
  select.classList.add("search-select-native");
  select.tabIndex = -1;
  select.setAttribute("aria-hidden", "true");

  let open = false;
  let typed = false;
  let options = [];
  let active = -1;

  const escapeHtml = (value) => String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");

  function highlight(text, needle) {
    if (!needle) return escapeHtml(text);
    const index = text.toLowerCase().indexOf(needle);
    if (index < 0) return escapeHtml(text);
    return escapeHtml(text.slice(0, index))
      + `<mark>${escapeHtml(text.slice(index, index + needle.length))}</mark>`
      + escapeHtml(text.slice(index + needle.length));
  }

  function selectedText() {
    return select.value ? select.selectedOptions[0]?.text || "" : "";
  }

  function sync() {
    input.disabled = select.disabled;
    input.placeholder = select.options[0]?.value === "" ? select.options[0].text : "";
    if (select.disabled && open) close();
    if (open) {
      render();
    } else {
      input.value = selectedText();
    }
  }

  function render() {
    const needle = typed ? input.value.trim().toLowerCase() : "";
    const all = [...select.options].filter((option) => option.value !== "");
    const matches = needle ? all.filter((option) => option.text.toLowerCase().includes(needle)) : all;
    options = matches.slice(0, maxResults);
    const selectedIndex = options.findIndex((option) => option.value === select.value);
    active = typed ? (options.length ? 0 : -1) : selectedIndex;

    const items = options.map((option, index) => `
      <li role="option" data-index="${index}" aria-selected="${option.value === select.value}"
          class="${index === active ? "active" : ""} ${option.value === select.value ? "selected" : ""}">
        <span class="search-select-label">${highlight(option.text, needle)}</span>
        ${option.dataset.meta ? `<span class="search-select-meta">${escapeHtml(option.dataset.meta)}</span>` : ""}
      </li>
    `);
    if (!options.length) {
      items.push(`<li class="search-select-note">${all.length ? "No matches" : "Nothing to choose from"}</li>`);
    } else if (matches.length > options.length) {
      items.push(`<li class="search-select-note">Showing ${maxResults} of ${matches.length.toLocaleString()}. Type to narrow.</li>`);
    }
    list.innerHTML = items.join("");
    scrollActiveIntoView();
  }

  function scrollActiveIntoView() {
    list.querySelector("li.active")?.scrollIntoView({ block: "nearest" });
  }

  function moveActive(step) {
    if (!options.length) return;
    active = (active + step + options.length) % options.length;
    list.querySelectorAll("li[data-index]").forEach((item) => {
      item.classList.toggle("active", Number(item.dataset.index) === active);
    });
    scrollActiveIntoView();
  }

  function openList() {
    if (open || select.disabled) return;
    open = true;
    typed = false;
    wrapper.classList.add("open");
    list.classList.remove("hidden");
    input.setAttribute("aria-expanded", "true");
    render();
    input.select();
  }

  function close() {
    open = false;
    typed = false;
    wrapper.classList.remove("open");
    list.classList.add("hidden");
    input.setAttribute("aria-expanded", "false");
    input.value = selectedText();
  }

  function choose(index) {
    const option = options[index];
    if (!option) return;
    const changed = select.value !== option.value;
    select.value = option.value;
    close();
    if (changed) select.dispatchEvent(new Event("change", { bubbles: true }));
  }

  input.addEventListener("focus", openList);
  input.addEventListener("click", openList);
  input.addEventListener("input", () => {
    if (!open) openList();
    typed = true;
    render();
  });
  input.addEventListener("keydown", (event) => {
    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      event.preventDefault();
      if (!open) openList();
      else moveActive(event.key === "ArrowDown" ? 1 : -1);
    } else if (event.key === "Enter" && open) {
      event.preventDefault();
      choose(active);
    } else if (event.key === "Escape" && open) {
      event.preventDefault();
      close();
    } else if (event.key === "Tab" && open) {
      close();
    }
  });
  // Keep focus in the input while clicking an option.
  list.addEventListener("mousedown", (event) => event.preventDefault());
  list.addEventListener("click", (event) => {
    const item = event.target.closest("li[data-index]");
    if (item) choose(Number(item.dataset.index));
  });
  wrapper.addEventListener("focusout", (event) => {
    if (open && !wrapper.contains(event.relatedTarget)) close();
  });
  select.addEventListener("change", sync);
  new MutationObserver(sync).observe(select, { childList: true, attributes: true, attributeFilter: ["disabled"] });

  sync();
  return { sync, input };
};
