"use strict";

const LEVELS = [
  { key: "liquid", title: "Ликвидные" },
  { key: "medium", title: "Средние" },
  { key: "illiquid", title: "Неликвидные" },
];
const COLUMN_LIMIT = 300;

const state = {
  data: { items: [], categories: [] },
  category: "weapons",
  search: "",
  sort: "value-desc",
  liqCategory: "weapons",
  liqSearch: "",
};

const $ = (id) => document.getElementById(id);

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = String(text);
  return node;
}

function storageGet(key) {
  try { return localStorage.getItem(key); } catch (e) { return null; }
}

function storageSet(key, value) {
  try { localStorage.setItem(key, value); } catch (e) { /* нет доступа — не страшно */ }
}

async function api(path, options) {
  const response = await fetch(path, options);
  let body = null;
  try { body = await response.json(); } catch (e) { /* пустой ответ */ }
  if (!response.ok) {
    throw new Error((body && body.error) || `Ошибка ${response.status}`);
  }
  return body;
}

function postJson(path, payload) {
  return api(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload || {}),
  });
}

// --- данные -----------------------------------------------------------------

function categoryTitle(slug) {
  const found = state.data.categories.find((c) => c.slug === slug);
  return found ? found.title : slug;
}

function weaponSlugs() {
  return new Set(state.data.categories.filter((c) => c.weapon).map((c) => c.slug));
}

function itemsIn(category) {
  const items = state.data.items;
  if (category === "all") return items;
  if (category === "weapons") {
    const weapons = weaponSlugs();
    return items.filter((item) => weapons.has(item.category));
  }
  return items.filter((item) => item.category === category);
}

function matchesSearch(item, query) {
  if (!query) return true;
  const haystack = `${item.name} ${item.aliases || ""}`.toLowerCase();
  return haystack.includes(query);
}

function compareValueDesc(a, b) {
  const av = a.value === null || a.value === undefined ? -1 : a.value;
  const bv = b.value === null || b.value === undefined ? -1 : b.value;
  return bv - av || a.name.localeCompare(b.name);
}

const SORTS = {
  "value-desc": compareValueDesc,
  "value-asc": (a, b) => {
    const av = a.value === null || a.value === undefined ? Infinity : a.value;
    const bv = b.value === null || b.value === undefined ? Infinity : b.value;
    return av - bv || a.name.localeCompare(b.name);
  },
  name: (a, b) => a.name.localeCompare(b.name),
  demand: (a, b) => (b.demand ?? -1) - (a.demand ?? -1) || compareValueDesc(a, b),
};

function formatDate(iso) {
  if (!iso) return "—";
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso;
  return date.toLocaleString("ru-RU", { dateStyle: "long", timeStyle: "short" });
}

// --- шапка ------------------------------------------------------------------

function renderHeader() {
  $("siteUpdated").textContent = state.data.site_last_updated || "—";
  $("fetchedAt").textContent = formatDate(state.data.fetched_at);
}

// --- вкладка «Цены» ---------------------------------------------------------

function categoryButton(slug, title, count) {
  const li = el("li");
  const button = el("button", slug === state.category ? "active" : "");
  button.type = "button";
  button.append(el("span", "", title), el("span", "count", count));
  button.addEventListener("click", () => {
    state.category = slug;
    storageSet("mm2.category", slug);
    renderValues();
  });
  li.append(button);
  return li;
}

function renderCategories() {
  const list = $("categoryList");
  list.replaceChildren(
    categoryButton("weapons", "Всё оружие", itemsIn("weapons").length),
    categoryButton("all", "Все предметы", state.data.items.length),
    el("li", "divider"),
    ...state.data.categories.map((c) => categoryButton(c.slug, c.title, c.count)),
  );
}

function valueCell(item) {
  const td = el("td", "num value");
  if (item.value === null || item.value === undefined) {
    td.append(el("span", "site-text", item.value_text || "—"));
  } else {
    td.textContent = String(item.value);
  }
  return td;
}

function renderValues() {
  renderCategories();
  const query = state.search.trim().toLowerCase();
  const rows = itemsIn(state.category)
    .filter((item) => matchesSearch(item, query))
    .sort(SORTS[state.sort] || compareValueDesc);

  const body = $("valuesBody");
  if (!state.data.items.length) {
    const tr = el("tr");
    const td = el("td", "empty", "Цен пока нет. Нажмите «Обновить цены» или загрузите их на вкладке «Импорт».");
    td.colSpan = 8;
    tr.append(td);
    body.replaceChildren(tr);
    $("valuesHint").textContent = "";
    return;
  }

  const fragment = document.createDocumentFragment();
  for (const item of rows) {
    const tr = el("tr");
    tr.append(
      el("td", "", item.name),
      el("td", "", categoryTitle(item.category)),
      valueCell(item),
      el("td", "site-text", item.range_text || item.value_text || "—"),
      el("td", "num", item.demand ?? "—"),
      el("td", "num", item.rarity ?? "—"),
      el("td", "", item.stability || "—"),
      el("td", "", item.change || "—"),
    );
    tr.title = item.origin ? `Origin: ${item.origin}` : "";
    fragment.append(tr);
  }
  if (!rows.length) {
    const tr = el("tr");
    const td = el("td", "empty", "Ничего не найдено.");
    td.colSpan = 8;
    tr.append(td);
    fragment.append(tr);
  }
  body.replaceChildren(fragment);
  $("valuesHint").textContent =
    `Показано: ${rows.length}. «Значение» — первое число диапазона с сайта (1320 - 1340 → 1320); ` +
    "если диапазона нет — число из Value.";
}

// --- вкладка «Ликвидность» --------------------------------------------------

function renderLiqCategorySelect() {
  const select = $("liqCategory");
  const options = [["weapons", "Всё оружие"], ["all", "Все предметы"]]
    .concat(state.data.categories.map((c) => [c.slug, c.title]));
  select.replaceChildren(...options.map(([value, title]) => {
    const option = el("option", "", title);
    option.value = value;
    return option;
  }));
  select.value = state.liqCategory;
}

function liqItem(item) {
  const li = el("li", "liq-item");
  const head = el("div", "liq-head");
  head.append(el("span", "liq-name", item.name), el("span", "", `${item.liquidity.score}/100`));
  const bar = el("div", "bar");
  const fill = el("span");
  fill.style.width = `${item.liquidity.score}%`;
  bar.append(fill);
  const value = item.value === null || item.value === undefined ? (item.value_text || "—") : item.value;
  li.append(
    head,
    bar,
    el("div", "liq-meta", `${categoryTitle(item.category)} · значение ${value}`),
    el("div", "liq-meta", item.liquidity.reasons.join("; ")),
  );
  return li;
}

function renderLiquidity() {
  const query = state.liqSearch.trim().toLowerCase();
  const items = itemsIn(state.liqCategory).filter((item) => matchesSearch(item, query));
  const groups = { liquid: [], medium: [], illiquid: [], untradable: [] };
  for (const item of items) {
    (groups[item.liquidity.level] || groups.illiquid).push(item);
  }

  const chips = LEVELS.map(({ key, title }) => el("span", `chip ${key}`, `${title}: ${groups[key].length}`));
  if (groups.untradable.length) {
    chips.push(el("span", "chip untradable", `Не обмениваются: ${groups.untradable.length}`));
  }
  $("liqSummary").replaceChildren(...chips);

  const columns = LEVELS.map(({ key, title }) => {
    const column = el("div", `column ${key}`);
    const sorted = groups[key].sort((a, b) => b.liquidity.score - a.liquidity.score || compareValueDesc(a, b));
    column.append(el("h3", "", `${title} (${sorted.length})`));
    const list = el("ol");
    list.append(...sorted.slice(0, COLUMN_LIMIT).map(liqItem));
    if (sorted.length > COLUMN_LIMIT) {
      list.append(el("li", "more", `…и ещё ${sorted.length - COLUMN_LIMIT}. Уточните поиск или категорию.`));
    }
    if (!sorted.length) list.append(el("li", "more", "Нет предметов."));
    column.append(list);
    return column;
  });
  $("liqColumns").replaceChildren(...columns);
}

// --- вкладка «Импорт» -------------------------------------------------------

function renderImportSelect() {
  const select = $("importCategory");
  const current = select.value;
  select.replaceChildren(...state.data.categories.map((c) => {
    const option = el("option", "", c.title);
    option.value = c.slug;
    return option;
  }));
  if (current) select.value = current;
}

async function submitImport(event) {
  event.preventDefault();
  const result = $("importResult");
  const button = event.submitter || $("importForm").querySelector("button");
  button.disabled = true;
  result.textContent = "Загрузка…";
  try {
    const response = await postJson("/api/import", {
      category: $("importCategory").value,
      text: $("importText").value,
    });
    result.textContent = `Загружено предметов: ${response.imported} (${categoryTitle(response.category)}).`;
    $("importText").value = "";
    await loadData();
  } catch (error) {
    result.textContent = `Ошибка: ${error.message}`;
  } finally {
    button.disabled = false;
  }
}

// --- обновление цен ---------------------------------------------------------

function showStatus(text, isError) {
  const box = $("status");
  box.textContent = text;
  box.classList.toggle("error", Boolean(isError));
  box.classList.toggle("hidden", !text);
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

async function followUpdate() {
  const button = $("updateBtn");
  button.disabled = true;
  button.textContent = "Обновление…";
  try {
    let status = await api("/api/status");
    while (status.running) {
      showStatus(status.log.slice(-6).join("\n") || "Загрузка цен с сайта…");
      await sleep(1000);
      status = await api("/api/status");
    }
    if (status.error) {
      showStatus(`Не удалось обновить цены:\n${status.error}`, true);
    } else if (status.finished_at) {
      showStatus(status.log.slice(-1).join("") || "Цены обновлены.");
    }
    await loadData();
  } catch (error) {
    showStatus(`Нет связи с программой: ${error.message}`, true);
  } finally {
    button.disabled = false;
    button.textContent = "Обновить цены";
  }
}

async function startUpdate() {
  try {
    await postJson("/api/update");
  } catch (error) {
    showStatus(`Не удалось начать обновление: ${error.message}`, true);
    return;
  }
  await followUpdate();
}

// --- общее --------------------------------------------------------------------

function renderAll() {
  renderHeader();
  renderValues();
  renderLiqCategorySelect();
  renderLiquidity();
  renderImportSelect();
}

async function loadData() {
  try {
    state.data = await api("/api/data");
  } catch (error) {
    showStatus(`Не удалось загрузить цены: ${error.message}`, true);
    return;
  }
  const known = new Set(["weapons", "all", ...state.data.categories.map((c) => c.slug)]);
  if (!known.has(state.category)) state.category = "weapons";
  if (!known.has(state.liqCategory)) state.liqCategory = "weapons";
  renderAll();
  if (state.data.errors && state.data.errors.length) {
    $("valuesHint").textContent += ` Не загрузились: ${state.data.errors.length} кат. (подробности — после «Обновить цены»).`;
  }
}

function switchTab(name) {
  for (const tab of document.querySelectorAll(".tab")) {
    const active = tab.dataset.tab === name;
    tab.classList.toggle("active", active);
    tab.setAttribute("aria-selected", String(active));
  }
  for (const panel of document.querySelectorAll(".panel")) {
    panel.classList.toggle("hidden", panel.id !== `tab-${name}`);
  }
  storageSet("mm2.tab", name);
}

function init() {
  state.category = storageGet("mm2.category") || state.category;
  for (const tab of document.querySelectorAll(".tab")) {
    tab.addEventListener("click", () => switchTab(tab.dataset.tab));
  }
  const savedTab = storageGet("mm2.tab");
  if (savedTab && document.getElementById(`tab-${savedTab}`)) switchTab(savedTab);

  $("search").addEventListener("input", (event) => { state.search = event.target.value; renderValues(); });
  $("sort").addEventListener("change", (event) => { state.sort = event.target.value; renderValues(); });
  $("liqSearch").addEventListener("input", (event) => { state.liqSearch = event.target.value; renderLiquidity(); });
  $("liqCategory").addEventListener("change", (event) => { state.liqCategory = event.target.value; renderLiquidity(); });
  $("updateBtn").addEventListener("click", startUpdate);
  $("importForm").addEventListener("submit", submitImport);

  loadData().then(async () => {
    const status = await api("/api/status").catch(() => null);
    if (status && status.running) followUpdate();
  });
}

init();
