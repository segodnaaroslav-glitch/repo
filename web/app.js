"use strict";

const LEVELS = [
  { key: "liquid", title: "Ликвидные" },
  { key: "medium", title: "Средние" },
  { key: "illiquid", title: "Неликвидные" },
];
const LEVEL_TITLES = {
  liquid: "ликвидный", medium: "средний", illiquid: "неликвидный",
  untradable: "не обменивается", secret: "секретный", none: "нет данных",
};
const COLUMN_LIMIT = 300;
const STABILITY_CLASS = {
  "overpaid for": "good", "doing well": "good", "improving": "good", "rising": "good",
  "stable": "none", "peaking": "mid", "fluctuating": "mid",
  "underpaid for": "bad", "declining": "bad", "dropping": "bad", "receding": "bad",
};

const state = {
  data: { items: [], categories: [], markets: [] },
  status: null,
  clockOffset: 0,        // серверное время минус время браузера, мс
  dataVersion: null,
  category: "weapons",
  search: "",
  sort: { key: "value", dir: "desc" },
  onlyFav: false,
  hideSecret: true,
  favorites: new Set(),
  liqView: "combined",
  liqCategory: "weapons",
  liqSearch: "",
  selected: null,        // ключ предмета в карточке
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

function toast(text, kind) {
  const box = el("div", `toast ${kind || ""}`, text);
  $("toasts").append(box);
  setTimeout(() => box.remove(), kind === "error" ? 9000 : 5000);
}

// --- форматирование ----------------------------------------------------------

function fmtNum(value) {
  if (value === null || value === undefined) return "—";
  return Number(value).toLocaleString("en-US", { maximumFractionDigits: 2 });
}

function fmtPrice(value, currency) {
  if (value === null || value === undefined) return "—";
  const number = Number(value).toLocaleString("ru-RU", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  if (currency === "RUB") return `${number} ₽`;
  return `$${Number(value).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
}

function fmtDate(iso) {
  if (!iso) return "—";
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso;
  return date.toLocaleString("ru-RU", { day: "numeric", month: "long", hour: "2-digit", minute: "2-digit" });
}

function serverNow() {
  return Date.now() + state.clockOffset;
}

function fmtCountdown(iso) {
  if (!iso) return "—";
  const ms = new Date(iso).getTime() - serverNow();
  if (Number.isNaN(ms)) return "—";
  if (ms <= 0) return "сейчас";
  const total = Math.ceil(ms / 1000);
  const minutes = Math.floor(total / 60);
  const seconds = String(total % 60).padStart(2, "0");
  return `${minutes}:${seconds}`;
}

function changeClass(text) {
  const match = /[+\-−]/.exec(text || "");
  if (!match || /\(\s*\+?0\s*\)/.test(text)) return "";
  return match[0] === "+" ? "up" : "down";
}

// --- данные ------------------------------------------------------------------

const itemKey = (item) => `${item.category}:${item.name}`;

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
  if (category === "favorites") return items.filter((item) => state.favorites.has(itemKey(item)));
  if (category === "weapons") {
    const weapons = weaponSlugs();
    return items.filter((item) => weapons.has(item.category));
  }
  return items.filter((item) => item.category === category);
}

function matchesSearch(item, query) {
  if (!query) return true;
  return `${item.name} ${item.aliases || ""}`.toLowerCase().includes(query);
}

function sortValue(item, key) {
  switch (key) {
    case "name": return item.name.toLowerCase();
    case "category": return categoryTitle(item.category);
    case "demand": return item.demand;
    case "rarity": return item.rarity;
    case "liquidity": return (item.combined || item.liquidity || {}).score;
    default: return item.secret ? null : item.value;  // секретные — в конец
  }
}

function compareItems(a, b) {
  const { key, dir } = state.sort;
  const av = sortValue(a, key);
  const bv = sortValue(b, key);
  const aMissing = av === null || av === undefined;
  const bMissing = bv === null || bv === undefined;
  if (aMissing !== bMissing) return aMissing ? 1 : -1;  // пустые всегда внизу
  let result = 0;
  if (!aMissing) result = typeof av === "string" ? av.localeCompare(bv) : av - bv;
  if (dir === "desc") result = -result;
  return result || a.name.localeCompare(b.name);
}

function findItem(key) {
  return state.data.items.find((item) => itemKey(item) === key) || null;
}

// --- шапка и автообновление ----------------------------------------------------

function renderHeader() {
  $("siteUpdated").textContent = state.data.site_last_updated || "—";
  $("fetchedAt").textContent = fmtDate(state.data.fetched_at);
}

function renderSync() {
  const status = state.status;
  const sync = status && status.sync;
  const dot = $("syncDot");
  dot.className = "dot";
  if (status && status.running) {
    dot.classList.add("busy");
    $("syncNext").textContent = "Идёт загрузка цен с сайта…";
    $("syncResult").textContent = (status.log || []).slice(-1).join("");
    $("syncProgress").style.width = "100%";
  } else if (sync) {
    dot.classList.add(status.error ? "err" : "ok");
    $("syncNext").textContent = `Проверка сайта через ${fmtCountdown(sync.next_check_at)}`;
    const left = new Date(sync.next_check_at).getTime() - serverNow();
    const share = 1 - Math.min(1, Math.max(0, left / (sync.interval * 1000)));
    $("syncProgress").style.width = `${Math.round(share * 100)}%`;
    const result = status.error ? `Ошибка обновления: ${status.error.split("\n")[0]}` : sync.last_result;
    $("syncResult").textContent = sync.last_check_at ? `${fmtDate(sync.last_check_at)}: ${result}` : result;
  } else {
    $("syncNext").textContent = "Автопроверка сайта выключена";
    $("syncProgress").style.width = "0";
  }
  $("updateBtn").disabled = Boolean(status && status.running);
  $("updateBtn").textContent = status && status.running ? "Обновление…" : "Обновить цены";
  $("checkBtn").classList.toggle("hidden", !sync);
}

function renderErrors() {
  const box = $("errorsBox");
  box.replaceChildren();
  const failed = state.data.errors || [];
  if (!state.data.load_error && !failed.length) {
    box.classList.add("hidden");
    return;
  }
  if (state.data.load_error) box.append(el("p", "", state.data.load_error));
  if (failed.length) {
    box.append(el("strong", "", "Эти категории не обновились при последней загрузке:"));
    const list = el("ul");
    list.append(...failed.map((error) => {
      const info = state.data.categories.find((c) => c.slug === error.category);
      const kept = info && info.count ? "остались прежние цены" : "цен пока нет";
      return el("li", "", `${error.title}: ${error.message} (${kept})`);
    }));
    box.append(list);
  }
  box.classList.remove("hidden");
}

// --- вкладка «Цены» -----------------------------------------------------------

function categoryButton(slug, title, count, info) {
  const li = el("li");
  const button = el("button", slug === state.category ? "active" : "");
  button.type = "button";
  button.dataset.slug = slug;
  button.setAttribute("aria-pressed", String(slug === state.category));
  const label = el("span", "", title);
  if (info && info.error) label.append(el("span", "warn", " ⚠"));
  button.append(label, el("span", "count", count));
  if (info) {
    const parts = [info.updated_at
      ? `Обновлено: ${fmtDate(info.updated_at)} (${info.source === "import" ? "импорт" : "с сайта"})`
      : "Ещё не загружалось"];
    if (info.error) parts.push(`Последнее обновление не удалось: ${info.error}`);
    button.title = parts.join("\n");
  }
  li.append(button);
  return li;
}

function renderCategories() {
  const list = $("categoryList");
  const focused = list.contains(document.activeElement) ? document.activeElement.dataset.slug : null;
  list.replaceChildren(
    categoryButton("weapons", "Всё оружие", itemsIn("weapons").length),
    categoryButton("all", "Все предметы", state.data.items.length),
    categoryButton("favorites", "★ Избранное", itemsIn("favorites").length),
    el("li", "divider"),
    ...state.data.categories.map((c) => categoryButton(c.slug, c.title, c.count, c)),
  );
  if (focused) {
    const again = list.querySelector(`button[data-slug="${CSS.escape(focused)}"]`);
    if (again) again.focus();
  }
}

function meter(value) {
  if (value === null || value === undefined) return el("span", "muted", "—");
  const box = el("span", "meter");
  const track = el("span", "track");
  const fill = el("span");
  fill.style.width = `${Math.max(0, Math.min(10, value)) * 10}%`;
  track.append(fill);
  box.append(el("span", "", value), track);
  return box;
}

function stabilityChip(text) {
  if (!text) return el("span", "muted", "—");
  return el("span", `chip ${STABILITY_CLASS[text.toLowerCase()] || "none"}`, text);
}

function liquidityChip(info) {
  if (!info) return el("span", "muted", "—");
  const label = info.level === "secret" || info.level === "untradable" || info.level === "none"
    ? LEVEL_TITLES[info.level]
    : `${info.score} · ${LEVEL_TITLES[info.level] || info.level}`;
  return el("span", `chip ${info.level}`, label);
}

function starButton(item) {
  const key = itemKey(item);
  const on = state.favorites.has(key);
  const button = el("button", `star${on ? " on" : ""}`, on ? "★" : "☆");
  button.type = "button";
  button.title = on ? "Убрать из избранного" : "В избранное";
  button.addEventListener("click", (event) => {
    event.stopPropagation();
    toggleFavorite(key);
  });
  return button;
}

function toggleFavorite(key) {
  if (state.favorites.has(key)) state.favorites.delete(key); else state.favorites.add(key);
  storageSet("mm2.favorites", JSON.stringify([...state.favorites]));
  renderValues();
  if (state.selected === key) renderDrawer();
}

function valueCell(item) {
  const td = el("td", "num value");
  if (item.value === null || item.value === undefined) {
    td.append(el("span", "site-text", item.value_text || "—"));
  } else {
    td.textContent = fmtNum(item.value);
    if (item.secret) {
      td.classList.add("site-text");
      td.append(el("span", "badge secret", "условно"));
    }
  }
  return td;
}

function renderValues() {
  renderCategories();
  const query = state.search.trim().toLowerCase();
  let rows = itemsIn(state.category).filter((item) => matchesSearch(item, query));
  if (state.onlyFav) rows = rows.filter((item) => state.favorites.has(itemKey(item)));
  const hiddenSecret = state.hideSecret ? rows.filter((item) => item.secret).length : 0;
  if (state.hideSecret) rows = rows.filter((item) => !item.secret);
  rows.sort(compareItems);

  for (const button of document.querySelectorAll("th .sort")) {
    if (button.dataset.sort === state.sort.key) button.dataset.dir = state.sort.dir;
    else delete button.dataset.dir;
  }

  const body = $("valuesBody");
  if (!state.data.items.length) {
    const tr = el("tr");
    const td = el("td", "empty", "Цен пока нет. Нажмите «Обновить цены» или загрузите их на вкладке «Импорт».");
    td.colSpan = 10;
    tr.append(td);
    body.replaceChildren(tr);
    $("valuesHint").textContent = "";
    return;
  }

  const fragment = document.createDocumentFragment();
  for (const item of rows) {
    const tr = el("tr");
    const key = itemKey(item);
    if (key === state.selected) tr.classList.add("selected");
    const name = el("td", "", item.name);
    if (item.secret) name.append(el("span", "badge secret", "секретный"));
    const favCell = el("td");
    favCell.append(starButton(item));
    const demand = el("td");
    demand.append(meter(item.demand));
    const rarity = el("td");
    rarity.append(meter(item.rarity));
    const stability = el("td");
    stability.append(stabilityChip(item.stability));
    const liq = el("td");
    liq.append(liquidityChip(item.combined || item.liquidity));
    tr.append(
      favCell,
      name,
      el("td", "", categoryTitle(item.category)),
      valueCell(item),
      el("td", "site-text", item.range_text || item.value_text || "—"),
      demand,
      rarity,
      stability,
      el("td", changeClass(item.change), item.change || "—"),
      liq,
    );
    tr.addEventListener("click", () => openDrawer(key));
    fragment.append(tr);
  }
  if (!rows.length) {
    const tr = el("tr");
    const td = el("td", "empty", "Ничего не найдено.");
    td.colSpan = 10;
    tr.append(td);
    fragment.append(tr);
  }
  body.replaceChildren(fragment);
  let hint = `Показано: ${rows.length}. «Значение» — первое число диапазона с сайта (1,320 - 1,340 → 1,320); ` +
    "если диапазона нет — число из Value. Нажмите на строку, чтобы открыть подробности.";
  if (hiddenSecret) hint += ` Скрыто секретных предметов с условным значением 1,000,000: ${hiddenSecret}.`;
  $("valuesHint").textContent = hint;
}

// --- карточка предмета --------------------------------------------------------

function openDrawer(key) {
  state.selected = key;
  renderDrawer();
  renderValues();
}

function closeDrawer() {
  state.selected = null;
  $("drawer").classList.add("hidden");
  renderValues();
}

function fact(list, title, value) {
  list.append(el("dt", "", title));
  const dd = el("dd");
  if (value instanceof Node) dd.append(value); else dd.textContent = value || "—";
  list.append(dd);
}

function renderDrawer() {
  const item = state.selected && findItem(state.selected);
  if (!item) {
    $("drawer").classList.add("hidden");
    return;
  }
  $("drawerTitle").textContent = item.name;
  $("drawerSub").textContent = categoryTitle(item.category);
  const body = $("drawerBody");
  body.replaceChildren();

  const head = el("div");
  head.append(el("div", "big-value", item.value === null || item.value === undefined ? (item.value_text || "—") : fmtNum(item.value)));
  if (item.secret) head.append(el("p", "warn", "Секретный предмет: на сайте стоит условное значение 1,000,000, по нему не торгуют."));
  const fav = starButton(item);
  fav.append(state.favorites.has(itemKey(item)) ? " В избранном" : " В избранное");
  head.append(fav);
  body.append(head);

  const facts = el("dl", "facts");
  fact(facts, "Value на сайте", item.value_text);
  fact(facts, "Диапазон", item.range_text);
  fact(facts, "Спрос", item.demand === null ? "" : `${item.demand}/10`);
  fact(facts, "Редкость", item.rarity === null ? "" : `${item.rarity}/10`);
  fact(facts, "Стабильность", stabilityChip(item.stability));
  fact(facts, "Изменение", el("span", changeClass(item.change), item.change || "—"));
  fact(facts, "Origin", item.origin);
  fact(facts, "Aliases", item.aliases);
  body.append(facts);

  body.append(el("h3", "", "Ликвидность"));
  const liq = el("dl", "facts");
  if (item.combined) fact(liq, "Общая", liquidityChip(item.combined));
  fact(liq, "Supreme Values", liquidityChip(item.liquidity));
  body.append(liq);
  if (item.liquidity && item.liquidity.reasons.length) {
    const reasons = el("ul", "reasons");
    reasons.append(...item.liquidity.reasons.map((r) => el("li", "", r)));
    body.append(reasons);
  }

  const markets = state.data.markets || [];
  if (markets.length) {
    body.append(el("h3", "", "Торговые площадки"));
    const table = el("table", "mini");
    const head2 = el("tr");
    for (const title of ["Площадка", "Цена от", "Лотов", "Продаж/нед.", "Куплено/сутки", "Оценка"]) head2.append(el("th", "", title));
    table.append(head2);
    for (const market of markets) {
      const info = item.market && item.market[market.id];
      const tr = el("tr");
      const nameCell = el("td");
      if (info && info.url) {
        const link = el("a", "", market.title);
        link.href = info.url;
        link.target = "_blank";
        link.rel = "noopener";
        nameCell.append(link);
      } else {
        nameCell.textContent = market.title;
      }
      const scoreCell = el("td");
      scoreCell.append(info ? liquidityChip(info) : el("span", "muted", "не найден"));
      tr.append(
        nameCell,
        el("td", "", info ? fmtPrice(info.price, info.currency) : "—"),
        el("td", "", info && info.stock !== null && info.stock !== undefined ? info.stock : "—"),
        el("td", "", info && info.sales_week !== null && info.sales_week !== undefined ? info.sales_week : "—"),
        el("td", "", info && info.sold_24h ? info.sold_24h : "—"),
        scoreCell,
      );
      table.append(tr);
    }
    body.append(table);
  }
  $("drawer").classList.remove("hidden");
}

// --- вкладка «Ликвидность» ----------------------------------------------------

function marketStatusClass(market) {
  if (market.status === "ok") return "ok";
  if (market.status === "error") return "err";
  return "busy";
}

function renderMarketCards() {
  const markets = state.data.markets || [];
  const box = $("marketCards");
  const live = (state.status && state.status.markets) || markets;
  box.replaceChildren(...live.map((market) => {
    const card = el("div", "market");
    const head = el("div", "market-head");
    const name = el("span");
    name.append(el("span", `dot ${marketStatusClass(market)}`), " ");
    name.append(el("strong", "", market.title));
    head.append(name, el("span", "timer", market.status === "off" ? "выкл." : fmtCountdown(market.next_poll_at)));
    card.append(head);
    const parts = [];
    if (market.last_ok_at) parts.push(`данные от ${fmtDate(market.last_ok_at)}`);
    if (market.offers) parts.push(`предложений: ${market.offers}, найдено в списке: ${market.matched}`);
    card.append(el("div", "msg", parts.join(" · ") || "ещё не загружалось"));
    if (market.message) card.append(el("div", "msg", market.message));
    return card;
  }));
  box.classList.toggle("hidden", !live.length);
}

function liqViews() {
  const views = [{ id: "combined", title: "Общая" }, { id: "supreme", title: "Supreme Values" }];
  for (const market of state.data.markets || []) views.push({ id: market.id, title: market.title });
  return views;
}

function liquidityFor(item, view) {
  if (view === "supreme") return item.liquidity;
  if (view === "combined") return item.combined || item.liquidity;
  return item.market && item.market[view] ? item.market[view] : null;
}

function renderLiqViews() {
  const views = liqViews();
  if (!views.some((v) => v.id === state.liqView)) state.liqView = "combined";
  $("liqViews").replaceChildren(...views.map((view) => {
    const button = el("button", view.id === state.liqView ? "active" : "", view.title);
    button.type = "button";
    button.setAttribute("aria-pressed", String(view.id === state.liqView));
    button.addEventListener("click", () => {
      state.liqView = view.id;
      storageSet("mm2.liqView", view.id);
      renderLiqViews();
      renderLiquidity();
    });
    return button;
  }));
  const hints = {
    combined: "Среднее по Supreme Values и всем площадкам, где есть данные по предмету.",
    supreme: "По данным Supreme Values: спрос, стабильность и диапазон цены.",
  };
  const market = (state.data.markets || []).find((m) => m.id === state.liqView);
  $("liqViewHint").textContent = hints[state.liqView]
    || (market ? `По данным ${market.title}: продажи за неделю, число предложений и покупки между проверками.` : "");
}

function renderLiqCategorySelect() {
  const select = $("liqCategory");
  const options = [["weapons", "Всё оружие"], ["all", "Все предметы"], ["favorites", "★ Избранное"]]
    .concat(state.data.categories.map((c) => [c.slug, c.title]));
  select.replaceChildren(...options.map(([value, title]) => {
    const option = el("option", "", title);
    option.value = value;
    return option;
  }));
  select.value = state.liqCategory;
}

function liqItem(item, info) {
  const li = el("li", "liq-item");
  const head = el("div", "liq-head");
  head.append(el("span", "liq-name", item.name), el("span", "", `${info.score}/100`));
  const bar = el("div", "bar");
  const fill = el("span");
  fill.style.width = `${info.score}%`;
  bar.append(fill);
  const value = item.value === null || item.value === undefined ? (item.value_text || "—") : fmtNum(item.value);
  li.append(head, bar, el("div", "liq-meta", `${categoryTitle(item.category)} · значение ${value}`));
  if (info.reasons && info.reasons.length) li.append(el("div", "liq-meta", info.reasons.join("; ")));
  li.addEventListener("click", () => openDrawer(itemKey(item)));
  return li;
}

function renderLiquidity() {
  renderMarketCards();
  const query = state.liqSearch.trim().toLowerCase();
  const items = itemsIn(state.liqCategory).filter((item) => matchesSearch(item, query));
  const groups = { liquid: [], medium: [], illiquid: [], untradable: [], secret: [], none: [] };
  for (const item of items) {
    const info = liquidityFor(item, state.liqView);
    const level = info ? info.level : "none";
    (groups[level] || groups.none).push([item, info]);
  }

  const chips = LEVELS.map(({ key, title }) => el("span", `chip ${key}`, `${title}: ${groups[key].length}`));
  if (groups.secret.length) chips.push(el("span", "chip secret", `Секретные: ${groups.secret.length}`));
  if (groups.untradable.length) chips.push(el("span", "chip untradable", `Не обмениваются: ${groups.untradable.length}`));
  if (groups.none.length) chips.push(el("span", "chip none", `Нет данных: ${groups.none.length}`));
  $("liqSummary").replaceChildren(...chips);

  const columns = LEVELS.map(({ key, title }) => {
    const column = el("div", `column ${key}`);
    const sorted = groups[key].sort((a, b) => b[1].score - a[1].score || compareItems(a[0], b[0]));
    column.append(el("h3", "", `${title} (${sorted.length})`));
    const list = el("ol");
    list.append(...sorted.slice(0, COLUMN_LIMIT).map(([item, info]) => liqItem(item, info)));
    if (sorted.length > COLUMN_LIMIT) {
      list.append(el("li", "more", `…и ещё ${sorted.length - COLUMN_LIMIT}. Уточните поиск или категорию.`));
    }
    if (!sorted.length) list.append(el("li", "more", "Нет предметов."));
    column.append(list);
    return column;
  });
  $("liqColumns").replaceChildren(...columns);
}

function renderMethod() {
  const info = state.data.liquidity;
  if (!info) return;
  const weights = Object.entries(info.stability)
    .sort((a, b) => b[1] - a[1])
    .map(([name, bonus]) => `${name.replace(/\b\w/g, (c) => c.toUpperCase())} ${bonus > 0 ? "+" : bonus < 0 ? "−" : "±"}${Math.abs(bonus)}`);
  $("methodStability").textContent = weights.join(", ");
  $("methodLiquid").textContent = info.liquid_from;
  $("methodMedium").textContent = `${info.medium_from}–${info.liquid_from - 1}`;
  $("methodIlliquid").textContent = info.medium_from;
}

// --- вкладка «Импорт» ---------------------------------------------------------

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
    const parts = [];
    if (response.imported) {
      parts.push(`Загружено предметов: ${response.imported} (${categoryTitle(response.category)}).`);
    } else {
      parts.push("Предметы в тексте не найдены — цены не изменены.");
    }
    if (response.site_last_updated) parts.push(`Дата обновления цен на сайте сохранена: ${response.site_last_updated}.`);
    result.textContent = parts.join(" ");
    if (response.imported) {
      $("importText").value = "";
      toast(parts[0], "ok");
    }
    await loadData();
  } catch (error) {
    result.textContent = `Ошибка: ${error.message}`;
  } finally {
    button.disabled = false;
  }
}

// --- обновление и опрос сервера ----------------------------------------------

async function startUpdate() {
  try {
    await postJson("/api/update");
    toast("Загружаю цены с сайта…");
  } catch (error) {
    toast(`Не удалось начать обновление: ${error.message}`, "error");
  }
  pollSoon();
}

async function checkSite() {
  try {
    await postJson("/api/check");
    toast("Проверяю, не обновил ли сайт цены…");
  } catch (error) {
    toast(error.message, "error");
  }
  pollSoon();
}

let pollTimer = null;
let wasRunning = false;

function pollSoon() {
  clearTimeout(pollTimer);
  pollTimer = setTimeout(poll, 300);
}

async function poll() {
  clearTimeout(pollTimer);
  try {
    const status = await api("/api/status");
    if (status.now) state.clockOffset = new Date(status.now).getTime() - Date.now();
    state.status = status;
    if (wasRunning && !status.running) {
      if (status.error) toast(`Не удалось обновить цены:\n${status.error}`, "error");
      else toast((status.log || []).filter((line) => line.startsWith("Готово")).join("\n") || "Цены обновлены.", "ok");
    }
    wasRunning = status.running;
    const version = `${status.data_version}:${status.market_version || 0}`;
    if (version !== state.dataVersion) {
      state.dataVersion = version;
      await loadData();
    }
    renderSync();
    renderMarketCards();
  } catch (error) {
    $("syncResult").textContent = `Нет связи с программой: ${error.message}`;
    $("syncDot").className = "dot err";
  }
  pollTimer = setTimeout(poll, state.status && state.status.running ? 1500 : 5000);
}

// --- общее --------------------------------------------------------------------

function renderAll() {
  renderHeader();
  renderErrors();
  renderMethod();
  renderValues();
  renderLiqViews();
  renderLiqCategorySelect();
  renderLiquidity();
  renderImportSelect();
  renderDrawer();
}

async function loadData() {
  try {
    state.data = await api("/api/data");
  } catch (error) {
    toast(`Не удалось загрузить цены: ${error.message}`, "error");
    return;
  }
  state.data.markets = state.data.markets || [];
  const known = new Set(["weapons", "all", "favorites", ...state.data.categories.map((c) => c.slug)]);
  if (!known.has(state.category)) state.category = "weapons";
  if (!known.has(state.liqCategory)) state.liqCategory = "weapons";
  renderAll();
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
  state.liqView = storageGet("mm2.liqView") || state.liqView;
  try {
    state.favorites = new Set(JSON.parse(storageGet("mm2.favorites") || "[]"));
  } catch (e) {
    state.favorites = new Set();
  }
  try {
    const sort = JSON.parse(storageGet("mm2.sort") || "null");
    if (sort && sort.key && sort.dir) state.sort = sort;
  } catch (e) { /* по умолчанию */ }
  state.hideSecret = storageGet("mm2.hideSecret") !== "0";
  $("hideSecret").checked = state.hideSecret;

  for (const tab of document.querySelectorAll(".tab")) {
    tab.addEventListener("click", () => switchTab(tab.dataset.tab));
  }
  const savedTab = storageGet("mm2.tab");
  if (savedTab && document.getElementById(`tab-${savedTab}`)) switchTab(savedTab);

  $("categoryList").addEventListener("click", (event) => {
    const button = event.target.closest("button[data-slug]");
    if (!button) return;
    state.category = button.dataset.slug;
    storageSet("mm2.category", state.category);
    renderValues();
  });
  for (const button of document.querySelectorAll("th .sort")) {
    button.addEventListener("click", () => {
      const key = button.dataset.sort;
      const textual = key === "name" || key === "category";
      if (state.sort.key === key) state.sort.dir = state.sort.dir === "asc" ? "desc" : "asc";
      else state.sort = { key, dir: textual ? "asc" : "desc" };
      storageSet("mm2.sort", JSON.stringify(state.sort));
      renderValues();
    });
  }
  state.search = $("search").value;
  state.liqSearch = $("liqSearch").value;
  $("search").addEventListener("input", (event) => { state.search = event.target.value; renderValues(); });
  $("onlyFav").addEventListener("change", (event) => { state.onlyFav = event.target.checked; renderValues(); });
  $("hideSecret").addEventListener("change", (event) => {
    state.hideSecret = event.target.checked;
    storageSet("mm2.hideSecret", state.hideSecret ? "1" : "0");
    renderValues();
  });
  $("liqSearch").addEventListener("input", (event) => { state.liqSearch = event.target.value; renderLiquidity(); });
  $("liqCategory").addEventListener("change", (event) => { state.liqCategory = event.target.value; renderLiquidity(); });
  $("updateBtn").addEventListener("click", startUpdate);
  $("checkBtn").addEventListener("click", checkSite);
  $("importForm").addEventListener("submit", submitImport);
  $("drawerClose").addEventListener("click", closeDrawer);
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && state.selected) closeDrawer();
    const typing = /^(INPUT|TEXTAREA|SELECT)$/.test(document.activeElement.tagName);
    if (event.key === "/" && !typing) {
      event.preventDefault();
      switchTab("values");
      $("search").focus();
    }
  });

  // Таймеры в шапке и на карточках площадок тикают каждую секунду.
  setInterval(() => { renderSync(); renderMarketCards(); }, 1000);
  poll();
}

init();
