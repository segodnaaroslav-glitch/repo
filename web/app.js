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
  view: "cards",         // "cards" или "table"
  mkCategory: "weapons",
  mkSearch: "",
  mkSort: "dreampets:desc",
  mkOnlyListed: true,
  offline: null,         // текст ошибки, если программа не отвечает
  userUpdate: false,     // обновление запущено кнопкой (о результате сообщить всплывающим окном)
  lastError: null,       // последняя показанная ошибка автообновления
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

const MARKET_ORDER = ["starpets", "dreampets"];

function usdRub() {
  const rate = (state.status && state.status.rate) || state.data.rate;
  return rate && rate.usd_rub ? rate.usd_rub : null;
}

function fmtRub(value) {
  return `${Number(value).toLocaleString("ru-RU", { minimumFractionDigits: 2, maximumFractionDigits: 2 })} ₽`;
}

function fmtUsd(value) {
  return `$${Number(value).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
}

// Цена в обеих валютах: [в валюте площадки, в другой по курсу Rapira].
function bothPrices(value, currency) {
  if (value === null || value === undefined) return null;
  const rate = usdRub();
  if (currency === "RUB") return [fmtRub(value), rate ? fmtUsd(value / rate) : null];
  return [fmtUsd(value), rate ? fmtRub(value * rate) : null];
}

function priceNode(value, currency, className) {
  const pair = bothPrices(value, currency);
  if (!pair) return el("span", "muted", "нет в продаже");
  const box = el("span", `price ${className || ""}`);
  box.append(el("b", "", pair[0]));
  if (pair[1]) box.append(el("span", "muted", ` · ${pair[1]}`));
  return box;
}

function finite(value) {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

// Цена площадки в обеих валютах. Точные — как на самой площадке (StarPets сам
// задаёт и доллары, и рубли); пересчитанные по курсу — с пометкой «≈».
// factor — множитель (комиссия, количество).
function infoMoney(info, factor = 1) {
  if (!info) return null;
  const rate = usdRub();
  let usd = info.currency === "RUB" ? null : finite(info.price);
  let rub = info.currency === "RUB" ? finite(info.price) : finite(info.price_rub);
  let usdApprox = false;
  let rubApprox = info.currency !== "RUB" && Boolean(info.price_rub_approx) && rub !== null;
  if (usd === null && rub === null) return null;
  if (rub === null && rate) {
    rub = usd * rate;
    rubApprox = true;
  }
  if (usd === null && rate) {
    usd = rub / rate;
    usdApprox = true;
  }
  return {
    usd: usd === null ? null : usd * factor,
    rub: rub === null ? null : rub * factor,
    usdApprox,
    rubApprox,
    primary: info.currency === "RUB" ? "rub" : "usd",
  };
}

function moneyText(money, currency) {
  const value = money[currency];
  if (value === null || value === undefined) return null;
  const approx = currency === "usd" ? money.usdApprox : money.rubApprox;
  return `${approx ? "≈" : ""}${currency === "usd" ? fmtUsd(value) : fmtRub(value)}`;
}

function moneyNode(money, className) {
  if (!money) return el("span", "muted", "нет в продаже");
  const box = el("span", `price ${className || ""}`);
  const order = money.primary === "rub" ? ["rub", "usd"] : ["usd", "rub"];
  const first = moneyText(money, order[0]) || moneyText(money, order[1]);
  const second = moneyText(money, order[0]) ? moneyText(money, order[1]) : null;
  box.append(el("b", "", first));
  if (second) box.append(el("span", "muted", ` · ${second}`));
  if (money.usdApprox || money.rubApprox) {
    box.title = "≈ — пересчитано по курсу; без знака — цена самой площадки";
  }
  return box;
}

function addMoney(total, money) {
  for (const currency of ["usd", "rub"]) {
    if (money[currency] === null) total.incomplete = true;
    else total[currency] += money[currency];
  }
  total.usdApprox = total.usdApprox || money.usdApprox;
  total.rubApprox = total.rubApprox || money.rubApprox;
}

function marketInfo(item, id) {
  return item.market && item.market[id] ? item.market[id] : null;
}

const MARKET_DEFAULTS = { starpets: { title: "StarPets", fee: 0.2 }, dreampets: { title: "DreamPets", fee: 0.1 } };

function marketTitle(id) {
  const market = (state.data.markets || []).find((m) => m.id === id);
  return market ? market.title : (MARKET_DEFAULTS[id] ? MARKET_DEFAULTS[id].title : id);
}

function feeOf(id) {
  const fees = state.data.fees || {};
  if (typeof fees[id] === "number") return fees[id];
  return MARKET_DEFAULTS[id] ? MARKET_DEFAULTS[id].fee : 0;
}

function feeText(id) {
  return `${Math.round(feeOf(id) * 100)}%`;
}

function marketsOff() {
  return !(state.data.markets || []).length;
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
    case "liquidity": return (item.combined || {}).score;
    case "starpets":
    case "dreampets": {
      const money = infoMoney(marketInfo(item, key));
      if (!money) return null;
      return money.usd !== null ? money.usd : money.rub / 100;
    }
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
  renderRate();
}

function renderRate() {
  const rate = (state.status && state.status.rate) || state.data.rate;
  const box = $("rateBox");
  if (!rate) {
    box.classList.add("hidden");
    return;
  }
  box.classList.remove("hidden");
  $("rateValue").textContent = rate.usd_rub ? `1 $ = ${rate.usd_rub.toLocaleString("ru-RU", { maximumFractionDigits: 2 })} ₽` : "нет курса";
  $("rateSource").textContent = rate.error && !rate.usd_rub
    ? `Rapira недоступна: ${rate.error}`
    : `Rapira, USDT/RUB${rate.updated_at ? ` · ${fmtDate(rate.updated_at)}` : ""}${rate.error ? " · старый курс" : ""}`;
}

function renderSync() {
  const status = state.status;
  const sync = status && status.sync;
  const dot = $("syncDot");
  dot.className = "dot";
  if (state.offline) {
    dot.classList.add("err");
    $("syncNext").textContent = "Программа не отвечает";
    $("syncResult").textContent = `Нет связи с программой: ${state.offline}. Запустите её снова.`;
    $("syncProgress").style.width = "0";
    return;
  }
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
    if (again) again.focus({ preventScroll: true });  // не прокручивать страницу к списку
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
  renderMarketsTab();
  renderLiquidity();
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

const CARD_CHUNK = 90;
const STABILITY_ARROW = { good: "↗", mid: "↕", bad: "↘", none: "→" };

function visibleRows() {
  const query = state.search.trim().toLowerCase();
  let rows = itemsIn(state.category).filter((item) => matchesSearch(item, query));
  if (state.onlyFav) rows = rows.filter((item) => state.favorites.has(itemKey(item)));
  const hiddenSecret = state.hideSecret ? rows.filter((item) => item.secret).length : 0;
  if (state.hideSecret) rows = rows.filter((item) => !item.secret);
  rows.sort(compareItems);
  return { rows, hiddenSecret };
}

function renderValues() {
  renderCategories();
  const { rows, hiddenSecret } = visibleRows();
  const cards = state.view === "cards";
  $("cardsGrid").classList.toggle("hidden", !cards);
  $("tableWrap").classList.toggle("hidden", cards);
  $("sortLabel").classList.toggle("hidden", !cards);
  syncSortSelect();
  if (!cards) $("moreRow").classList.add("hidden");
  for (const button of document.querySelectorAll("#viewToggle button")) {
    button.classList.toggle("active", button.dataset.view === state.view);
    button.setAttribute("aria-pressed", String(button.dataset.view === state.view));
  }
  if (cards) renderCards(rows); else renderTable(rows);
  if (!state.data.items.length) {
    $("valuesHint").textContent = "";
    return;
  }
  let hint = `Показано: ${rows.length}. «Значение» — первое число диапазона с сайта (1,320 - 1,340 → 1,320); ` +
    `если диапазона нет — число из Value. Нажмите на ${cards ? "карточку" : "строку"}, чтобы открыть подробности.`;
  if (hiddenSecret) hint += ` Скрыто секретных предметов с условным значением 1,000,000: ${hiddenSecret}.`;
  $("valuesHint").textContent = hint;
}

// Сортировка из заголовка таблицы, которой нет в списке, — временным пунктом списка.
function syncSortSelect() {
  const select = $("sortSelect");
  const wanted = `${state.sort.key}:${state.sort.dir}`;
  for (const option of select.querySelectorAll("option[data-temp]")) {
    if (option.value !== wanted) option.remove();
  }
  if (![...select.options].some((option) => option.value === wanted)) {
    const header = document.querySelector(`th .sort[data-sort="${CSS.escape(state.sort.key)}"]`);
    const title = header ? header.textContent.trim() : state.sort.key;
    const option = el("option", "", `${title} ${state.sort.dir === "asc" ? "↑" : "↓"}`);
    option.value = wanted;
    option.dataset.temp = "1";
    select.append(option);
  }
  select.value = wanted;
}

// --- карточки -----------------------------------------------------------------

const failedImages = new Map();  // адрес -> когда не загрузился: не запрашивать снова какое-то время
const IMAGE_RETRY_MS = 10 * 60 * 1000;
window.addEventListener("online", () => failedImages.clear());

function imageFailed(url) {
  const at = failedImages.get(url);
  if (at === undefined) return false;
  if (Date.now() - at > IMAGE_RETRY_MS) {
    failedImages.delete(url);
    return false;
  }
  return true;
}

function monogram(item) {
  const words = item.name.replace(/^Chroma\s+/i, "").split(/\s+/).filter(Boolean);
  const letters = (words.length > 1 ? words[0][0] + words[1][0] : (words[0] || "?").slice(0, 2)).toUpperCase();
  return el("span", "mono", letters);
}

function itemArt(item, size) {
  const box = el("div", `art ${size || ""}`);
  const sources = [item.image, item.image_market].filter((url) => url && !imageFailed(url));
  if (!sources.length) {
    box.append(monogram(item));
    return box;
  }
  const img = el("img");
  img.alt = item.name;
  img.loading = "lazy";
  img.decoding = "async";
  img.referrerPolicy = "no-referrer";
  let next = 0;
  img.addEventListener("error", () => {
    failedImages.set(sources[next], Date.now());
    next += 1;
    if (next < sources.length) img.src = sources[next];
    else img.replaceWith(monogram(item));  // картинка не загрузилась — значок с буквами
  });
  img.src = sources[0];
  box.append(img);
  return box;
}

function statLine(label, value) {
  const span = el("span", "stat");
  span.append(`${label} `, el("b", "", value === null || value === undefined ? "—" : value));
  return span;
}

function itemCard(item) {
  const key = itemKey(item);
  const card = el("article", `card cat-${item.category}${key === state.selected ? " selected" : ""}`);
  card.tabIndex = 0;
  card.dataset.key = key;
  card.setAttribute("role", "button");
  card.setAttribute("aria-label", `${item.name}: подробнее`);
  card.append(itemArt(item));

  const info = el("div", "info");
  const head = el("div", "title-row");
  head.append(el("h3", "", item.name), starButton(item));
  info.append(head);

  const tags = el("div", "tags");
  tags.append(el("span", "tag", categoryTitle(item.category)));
  if (item.secret) tags.append(el("span", "badge secret", "секретный"));
  info.append(tags);

  if (item.contains) info.append(el("p", "contains", `Состав: ${item.contains}`));

  const valueRow = el("div", "value-row");
  if (item.value === null || item.value === undefined) {
    valueRow.append(el("span", "value text", item.value_text || "—"));
  } else {
    valueRow.append(el("span", `value${item.secret ? " muted" : ""}`, fmtNum(item.value)));
    if (item.secret) valueRow.append(el("span", "badge secret", "условно"));
  }
  if (item.range_text) valueRow.append(el("span", "range", item.range_text));
  info.append(valueRow);

  const stats = el("div", "stats");
  if (item.stability) {
    const kind = STABILITY_CLASS[item.stability.toLowerCase()] || "none";
    stats.append(el("span", `chip ${kind}`, `${STABILITY_ARROW[kind]} ${item.stability}`));
  }
  stats.append(statLine("Спрос", item.demand), statLine("Редкость", item.rarity));
  info.append(stats);

  const prices = el("div", "card-prices");
  for (const id of MARKET_ORDER) {
    if (!(state.data.markets || []).some((m) => m.id === id)) continue;
    const info = marketInfo(item, id);
    const row = el("div", `mp ${id}`);
    row.append(el("span", "mp-name", marketTitle(id)));
    row.append(info ? moneyNode(infoMoney(info)) : el("span", "muted", "нет в продаже"));
    if (info && id === "dreampets" && info.stock !== null && info.stock !== undefined) {
      row.append(el("span", "muted", ` · ${info.stock} лот.`));
    }
    prices.append(row);
  }
  if (prices.childElementCount) info.append(prices);

  const foot = el("div", "foot");
  foot.append(el("span", `change ${changeClass(item.change)}`, item.change || ""));
  foot.append(liquidityChip(item.combined));
  info.append(foot);
  card.append(info);

  card.addEventListener("click", () => openDrawer(key));
  card.addEventListener("keydown", (event) => {
    if (event.target !== card) return;  // Enter на звёздочке — её собственное действие
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      openDrawer(key);
    }
  });
  return card;
}

let cardRows = [];
let cardShown = 0;

function renderCards(rows) {
  const grid = $("cardsGrid");
  cardRows = rows;
  // При обновлении данных не сворачивать уже открытые карточки.
  cardShown = Math.min(rows.length, Math.max(CARD_CHUNK, cardShown));
  if (!state.data.items.length) {
    grid.replaceChildren(el("p", "empty-cards", "Цен пока нет. Нажмите «Обновить цены» или загрузите их на вкладке «Импорт»."));
    $("moreRow").classList.add("hidden");
    return;
  }
  if (!rows.length) {
    grid.replaceChildren(el("p", "empty-cards", "Ничего не найдено."));
    $("moreRow").classList.add("hidden");
    return;
  }
  // Карточки создаются заново: запомнить, на какой был фокус клавиатуры, и вернуть его.
  const active = document.activeElement;
  const focusedCard = active && active.closest ? active.closest("#cardsGrid .card") : null;
  const focus = focusedCard ? { key: focusedCard.dataset.key, star: active.classList.contains("star") } : null;
  const fragment = document.createDocumentFragment();
  for (const item of rows.slice(0, cardShown)) fragment.append(itemCard(item));
  grid.replaceChildren(fragment);
  updateMoreButton();
  if (focus) focusCard(focus.key, focus.star);
}

function focusCard(key, star) {
  const card = $("cardsGrid").querySelector(`.card[data-key="${CSS.escape(key)}"]`);
  if (!card) return;
  const target = star ? card.querySelector(".star") || card : card;
  target.focus({ preventScroll: true });
}

function showMoreCards() {
  if (state.view !== "cards" || cardShown >= cardRows.length) return;
  const grid = $("cardsGrid");
  const fragment = document.createDocumentFragment();
  const next = cardRows.slice(cardShown, cardShown + CARD_CHUNK);
  for (const item of next) fragment.append(itemCard(item));
  grid.append(fragment);
  cardShown += next.length;
  updateMoreButton();
}

function updateMoreButton() {
  const left = cardRows.length - cardShown;
  $("moreRow").classList.toggle("hidden", left <= 0);
  $("moreBtn").textContent = `Показать ещё (${left})`;
}

// --- таблица ------------------------------------------------------------------

function renderTable(rows) {
  for (const button of document.querySelectorAll("th .sort")) {
    if (button.dataset.sort === state.sort.key) button.dataset.dir = state.sort.dir;
    else delete button.dataset.dir;
  }
  const body = $("valuesBody");
  if (!state.data.items.length) {
    const tr = el("tr");
    const td = el("td", "empty", "Цен пока нет. Нажмите «Обновить цены» или загрузите их на вкладке «Импорт».");
    td.colSpan = 12;
    tr.append(td);
    body.replaceChildren(tr);
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
    liq.append(liquidityChip(item.combined));
    const prices = MARKET_ORDER.map((id) => {
      const info = marketInfo(item, id);
      const td = el("td", "num price-cell");
      td.append(info ? moneyNode(infoMoney(info)) : el("span", "muted", "—"));
      return td;
    });
    tr.append(
      favCell,
      name,
      el("td", "", categoryTitle(item.category)),
      valueCell(item),
      ...prices,
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
    td.colSpan = 12;
    tr.append(td);
    fragment.append(tr);
  }
  body.replaceChildren(fragment);
}

// --- карточка предмета --------------------------------------------------------

function openDrawer(key) {
  state.selected = key;
  renderDrawer();
  renderValues();
  $("drawerClose").focus({ preventScroll: true });  // Tab — дальше по окну предмета
}

function closeDrawer() {
  const key = state.selected;
  state.selected = null;
  $("drawer").classList.add("hidden");
  renderValues();
  if (key && state.view === "cards") focusCard(key, false);  // вернуться к карточке
}

function fact(list, title, value) {
  list.append(el("dt", "", title));
  const dd = el("dd");
  if (value instanceof Node) dd.append(value); else dd.textContent = value || "—";
  list.append(dd);
}

function tileStat(box, label, value, hint) {
  const stat = el("div", "mstat");
  stat.append(el("span", "mstat-label", label));
  const node = el("strong", "mstat-value");
  node.append(value);
  stat.append(node);
  if (hint) stat.title = hint;
  box.append(stat);
}

function marketTile(item, id) {
  const info = marketInfo(item, id);
  const tile = el("section", `tile ${id}`);
  const head = el("div", "tile-head");
  head.append(el("strong", "tile-name", marketTitle(id)));
  if (info && info.url) {
    const link = el("a", "small", "Открыть ↗");
    link.href = info.url;
    link.target = "_blank";
    link.rel = "noopener";
    head.append(link);
  }
  tile.append(head);
  if (!info) {
    tile.append(el("p", "muted small", "Не найден на площадке."));
    return tile;
  }
  const priceRow = el("div", "tile-price");
  priceRow.append(moneyNode(infoMoney(info)));
  tile.append(priceRow);
  const fee = typeof info.fee === "number" ? info.fee : feeOf(id);
  if (info.price !== null && info.price !== undefined) {
    const payout = el("div", "tile-payout");
    payout.append(el("span", "muted", `Вы получите (−${Math.round(fee * 100)}%)`));
    payout.append(moneyNode(infoMoney(info, 1 - fee), "payout"));
    tile.append(payout);
  }
  const stats = el("div", "mstats");
  if (info.stock !== null && info.stock !== undefined) tileStat(stats, "Лотов", String(info.stock), "Лотов в продаже сейчас");
  if (info.sales_week !== null && info.sales_week !== undefined) {
    tileStat(stats, "Продаж/нед.", String(info.sales_week), "Продаж за неделю по данным площадки");
  }
  if (typeof info.sold_48h === "number") {
    tileStat(stats, "Куплено, 2 дня", String(info.sold_48h), "Сколько лотов исчезло с площадки (купили) за последние 48 часов");
    tileStat(stats, "Новых, 2 дня", String(info.listed_48h || 0), "Сколько новых лотов выставили за последние 48 часов");
  } else {
    tileStat(stats, "Куплено, 2 дня", "нет данных", "Площадка не показывает число лотов, поэтому покупки не посчитать — смотрите продажи за неделю");
  }
  tile.append(stats);
  const liq = el("div", "tile-liq");
  liq.append(el("span", "muted", "Ликвидность"), liquidityChip(info));
  tile.append(liq);
  return tile;
}

function renderDrawer() {
  const item = state.selected && findItem(state.selected);
  if (!item) {
    $("drawer").classList.add("hidden");
    return;
  }
  const drawer = $("drawer");
  drawer.className = `drawer cat-${item.category}`;
  $("drawerTitle").textContent = item.name;
  $("drawerSub").textContent = categoryTitle(item.category);
  const body = $("drawerBody");
  body.replaceChildren();

  const hero = el("div", "hero");
  hero.append(itemArt(item, "large"));
  const heroInfo = el("div", "hero-info");
  heroInfo.append(el("span", "muted small", "Supreme Values"));
  heroInfo.append(el("div", "big-value", item.value === null || item.value === undefined ? (item.value_text || "—") : fmtNum(item.value)));
  if (item.range_text) heroInfo.append(el("div", "muted small", `диапазон ${item.range_text}`));
  if (item.change) heroInfo.append(el("div", `change ${changeClass(item.change)}`, item.change));
  const fav = starButton(item);
  fav.append(state.favorites.has(itemKey(item)) ? " В избранном" : " В избранное");
  heroInfo.append(fav);
  hero.append(heroInfo);
  body.append(hero);
  if (item.secret) body.append(el("p", "warn", "Секретный предмет: на сайте стоит условное значение 1,000,000, по нему не торгуют."));
  if (item.contains) body.append(el("p", "contains", `Состав: ${item.contains}`));

  const markets = (state.data.markets || []).map((m) => m.id);
  if (markets.length) {
    body.append(el("h3", "", "На площадках"));
    const tiles = el("div", "tiles");
    for (const id of markets) tiles.append(marketTile(item, id));
    body.append(tiles);
    const combined = el("div", "tile-liq total-liq");
    combined.append(el("span", "", "Общая ликвидность (StarPets + DreamPets)"), liquidityChip(item.combined));
    body.append(combined);
  }

  body.append(el("h3", "", "Supreme Values"));
  const facts = el("dl", "facts");
  fact(facts, "Спрос", item.demand === null ? "" : `${item.demand}/10`);
  fact(facts, "Редкость", item.rarity === null ? "" : `${item.rarity}/10`);
  fact(facts, "Стабильность", stabilityChip(item.stability));
  fact(facts, "Value на сайте", item.value_text);
  fact(facts, "Origin", item.origin);
  fact(facts, "Aliases", item.aliases);
  body.append(facts);
  drawer.classList.remove("hidden");
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
  $("marketsBtn").classList.toggle("hidden", !live.length);
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
  const views = [{ id: "combined", title: "Общая" }];
  for (const market of state.data.markets || []) views.push({ id: market.id, title: market.title });
  return views;
}

function liquidityFor(item, view) {
  if (view === "combined") return item.combined || null;
  return marketInfo(item, view);
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
    combined: "Среднее по StarPets и DreamPets — где у предмета есть данные.",
  };
  const market = (state.data.markets || []).find((m) => m.id === state.liqView);
  $("liqViewHint").textContent = hints[state.liqView]
    || (market ? (market.id === "starpets"
      ? "По данным StarPets: продажи за неделю (их показывает сама площадка) и место в списке популярных."
      : `По данным ${market.title}: лоты в продаже и сколько лотов купили и выставили за последние 2 дня.`) : "");
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
  li.append(head, bar, el("div", "liq-meta", `${categoryTitle(item.category)} · Supreme ${value}`));
  const prices = el("div", "liq-meta");
  for (const id of MARKET_ORDER) {
    const market = marketInfo(item, id);
    const money = infoMoney(market);
    if (money) {
      const order = money.primary === "rub" ? ["rub", "usd"] : ["usd", "rub"];
      const texts = order.map((currency) => moneyText(money, currency)).filter(Boolean);
      prices.append(`${marketTitle(id)}: ${texts[0]}${texts[1] ? ` (${texts[1]})` : ""}  `);
    }
  }
  if (prices.textContent) li.append(prices);
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
  $("methodLiquid").textContent = info.liquid_from;
  $("methodMedium").textContent = `${info.medium_from}–${info.liquid_from - 1}`;
  $("methodIlliquid").textContent = info.medium_from;
}

// --- вкладка «Площадки» -------------------------------------------------------

function mkSortValue(item, key) {
  const dp = marketInfo(item, "dreampets");
  const sp = marketInfo(item, "starpets");
  switch (key) {
    case "starpets": return sortValue(item, "starpets");
    case "dreampets": return sortValue(item, "dreampets");
    case "lots": return dp ? dp.stock : null;
    case "sold": return dp ? dp.sold_48h : null;
    case "sales": return sp ? sp.sales_week : null;
    default: return item.name.toLowerCase();
  }
}

function payoutCell(info, id) {
  const td = el("td", "num");
  if (!info || info.price === null || info.price === undefined) {
    td.append(el("span", "muted", "—"));
    return td;
  }
  const fee = typeof info.fee === "number" ? info.fee : feeOf(id);
  td.append(moneyNode(infoMoney(info, 1 - fee), "payout"));
  return td;
}

function numCell(value) {
  return el("td", "num", value === null || value === undefined ? "—" : value);
}

function renderMarketsTab() {
  const select = $("mkCategory");
  if (!select.childElementCount || select.dataset.version !== String(state.data.categories.length)) {
    const options = [["weapons", "Всё оружие"], ["all", "Все предметы"], ["favorites", "★ Избранное"]]
      .concat(state.data.categories.map((c) => [c.slug, c.title]));
    select.replaceChildren(...options.map(([value, title]) => {
      const option = el("option", "", title);
      option.value = value;
      return option;
    }));
    select.dataset.version = String(state.data.categories.length);
    select.value = state.mkCategory;
  }
  const query = state.mkSearch.trim().toLowerCase();
  let rows = itemsIn(state.mkCategory).filter((item) => !item.secret && matchesSearch(item, query));
  if (state.mkOnlyListed) {
    rows = rows.filter((item) => MARKET_ORDER.some((id) => {
      const info = marketInfo(item, id);
      return info && info.price !== null && info.price !== undefined;
    }));
  }
  const [key, dir] = state.mkSort.split(":");
  rows.sort((a, b) => {
    const av = mkSortValue(a, key);
    const bv = mkSortValue(b, key);
    const aMissing = av === null || av === undefined;
    const bMissing = bv === null || bv === undefined;
    if (aMissing !== bMissing) return aMissing ? 1 : -1;
    let result = aMissing ? 0 : (typeof av === "string" ? av.localeCompare(bv) : av - bv);
    if (dir === "desc") result = -result;
    return result || a.name.localeCompare(b.name);
  });

  const fragment = document.createDocumentFragment();
  for (const item of rows.slice(0, 600)) {
    const sp = marketInfo(item, "starpets");
    const dp = marketInfo(item, "dreampets");
    const tr = el("tr");
    const nameCell = el("td", "item-cell");
    const art = itemArt(item, "tiny");
    nameCell.append(art, el("span", "", item.name));
    const supreme = item.value === null || item.value === undefined ? (item.value_text || "—") : fmtNum(item.value);
    const spPrice = el("td", "num");
    spPrice.append(sp ? moneyNode(infoMoney(sp)) : el("span", "muted", "—"));
    const dpPrice = el("td", "num");
    dpPrice.append(dp ? moneyNode(infoMoney(dp)) : el("span", "muted", "—"));
    tr.append(
      nameCell,
      el("td", "num muted", supreme),
      spPrice,
      payoutCell(sp, "starpets"),
      numCell(sp ? sp.sales_week : null),
      dpPrice,
      payoutCell(dp, "dreampets"),
      numCell(dp ? dp.stock : null),
      numCell(dp ? dp.sold_48h : null),
      numCell(dp ? dp.listed_48h : null),
    );
    tr.addEventListener("click", () => openDrawer(itemKey(item)));
    fragment.append(tr);
  }
  if (!rows.length) {
    const tr = el("tr");
    let message = state.data.items.length ? "Ничего не найдено." : "Цен пока нет.";
    if (state.data.items.length && marketsOff()) message = "Площадки выключены (программа запущена с --no-markets).";
    else if (state.data.items.length && state.mkOnlyListed && !state.data.items.some((i) => i.market && Object.keys(i.market).length)) {
      message = "Цены площадок ещё загружаются — первые появятся через минуту-две.";
    }
    const td = el("td", "empty", message);
    td.colSpan = 10;
    tr.append(td);
    fragment.append(tr);
  }
  $("mkBody").replaceChildren(fragment);
  const rate = usdRub();
  $("mkHint").textContent = `Показано: ${Math.min(rows.length, 600)}${rows.length > 600 ? ` из ${rows.length}` : ""}. ` +
    `Цены пересчитаны ${rate ? `по курсу 1 $ = ${rate.toLocaleString("ru-RU", { maximumFractionDigits: 2 })} ₽ (Rapira)` : "— курс ещё не получен"}. ` +
    `«Вы получите» — цена минус комиссия площадки: ${marketTitle("starpets")} ${feeText("starpets")}, ` +
    `${marketTitle("dreampets")} ${feeText("dreampets")}.`;
  $("mkSpPayout").textContent = `Вы получите (−${feeText("starpets")})`;
  $("mkDpPayout").textContent = `Вы получите (−${feeText("dreampets")})`;
  $("calcFeeSp").textContent = feeText("starpets");
  $("calcFeeDp").textContent = feeText("dreampets");
}

// --- вкладка «Калькулятор продажи» --------------------------------------------

const calc = { images: [], items: [] };

const MAX_SHOTS = 8;
const MAX_SHOT_SIDE = 2400;  // больше для распознавания не нужно, а запрос становится огромным
const MAX_SHOT_BYTES = 6 * 1024 * 1024;
const MAX_CALC_BYTES = 60 * 1024 * 1024;

function readDataUrl(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(reader.result);
    reader.onerror = () => reject(reader.error);
    reader.readAsDataURL(file);
  });
}

// Большой скриншот (4K, несжатый PNG) уменьшается до MAX_SHOT_SIDE по длинной стороне.
async function prepareShot(file) {
  if (typeof createImageBitmap !== "function") return readDataUrl(file);
  let bitmap;
  try {
    bitmap = await createImageBitmap(file);
  } catch (error) {
    return readDataUrl(file);
  }
  const scale = Math.min(1, MAX_SHOT_SIDE / Math.max(bitmap.width, bitmap.height));
  if (scale === 1 && file.size <= MAX_SHOT_BYTES) {
    bitmap.close();
    return readDataUrl(file);
  }
  const canvas = document.createElement("canvas");
  canvas.width = Math.max(1, Math.round(bitmap.width * scale));
  canvas.height = Math.max(1, Math.round(bitmap.height * scale));
  canvas.getContext("2d").drawImage(bitmap, 0, 0, canvas.width, canvas.height);
  bitmap.close();
  return canvas.toDataURL("image/png");
}

function addCalcFiles(files) {
  for (const file of files) {
    if (!file.type.startsWith("image/")) continue;
    if (calc.images.length >= MAX_SHOTS) {
      toast(`Не больше ${MAX_SHOTS} скриншотов за раз`, "error");
      break;
    }
    // Место занимается сразу: лимит работает и когда выбрано много файлов одновременно.
    const image = { name: file.name || "скриншот", data: null };
    calc.images.push(image);
    prepareShot(file).then((data) => {
      image.data = data;
      renderCalcThumbs();
    }).catch(() => {
      const index = calc.images.indexOf(image);
      if (index >= 0) calc.images.splice(index, 1);
      renderCalcThumbs();
      toast(`Не удалось открыть картинку «${image.name}»`, "error");
    });
  }
  renderCalcThumbs();
}

function renderCalcThumbs() {
  $("calcThumbs").replaceChildren(...calc.images.map((image) => {
    const box = el("div", "thumb");
    const img = el("img");
    if (image.data) img.src = image.data;
    else {
      img.hidden = true;  // ещё открывается
      box.classList.add("loading");
    }
    img.alt = image.name;
    const remove = el("button", "ghost icon", "✕");
    remove.type = "button";
    remove.title = "Убрать скриншот";
    remove.addEventListener("click", (event) => {
      event.stopPropagation();
      const index = calc.images.indexOf(image);
      if (index >= 0) calc.images.splice(index, 1);
      renderCalcThumbs();
    });
    box.append(img, remove);
    return box;
  }));
}

async function runCalc() {
  const button = $("calcBtn");
  if (calc.images.some((image) => !image.data)) {
    $("calcStatus").textContent = "Скриншоты ещё открываются — нажмите «Посчитать» через секунду.";
    return;
  }
  const size = calc.images.reduce((sum, image) => sum + image.data.length, 0);
  if (size > MAX_CALC_BYTES) {
    $("calcStatus").textContent = "Скриншоты слишком большие вместе — уберите часть и посчитайте в два захода.";
    return;
  }
  button.disabled = true;
  $("calcStatus").textContent = calc.images.length ? "Распознаю скриншоты…" : "Считаю…";
  try {
    const response = await postJson("/api/calc", {
      text: $("calcText").value,
      images: calc.images.map((image) => image.data),
    });
    calc.items = response.items.map((item) => ({ ...item }));
    calc.fees = response.fees || {};
    const notes = [];
    if (response.ocr_lines) notes.push(`распознано строк на скриншотах: ${response.ocr_lines}`);
    if (response.ocr_errors && response.ocr_errors.length) notes.push(response.ocr_errors.join("; "));
    $("calcStatus").textContent = `Найдено предметов: ${calc.items.length}${notes.length ? ` (${notes.join("; ")})` : ""}`;
    $("calcUnmatched").textContent = response.unmatched && response.unmatched.length
      ? `Не узнал: ${response.unmatched.join(", ")}. Проверьте написание или добавьте строкой.` : "";
    renderCalc();
  } catch (error) {
    $("calcStatus").textContent = `Ошибка: ${error.message}`;
  } finally {
    button.disabled = false;
  }
}

function calcFee(id) {
  return calc.fees && typeof calc.fees[id] === "number" ? calc.fees[id] : feeOf(id);
}

function calcMoney(item, id) {
  const info = item.prices[id];
  if (!info || (finite(info.price) === null && finite(info.price_rub) === null)) return null;
  return infoMoney(info, (1 - calcFee(id)) * item.qty);
}

function fillCalcSums(item, sums) {
  for (const id of MARKET_ORDER) {
    const money = calcMoney(item, id);
    sums[id].replaceChildren(money === null ? el("span", "muted", "—") : moneyNode(money, "payout"));
  }
}

function renderCalc() {
  const body = $("calcBody");
  body.replaceChildren(...calc.items.map((item, index) => {
    const tr = el("tr");
    const nameCell = el("td", "item-cell");
    nameCell.append(itemArt(item, "tiny"), el("span", "", item.name));
    if (item.score < 1) nameCell.append(el("span", "badge", `похоже на «${item.seen[0]}»`));
    const qtyCell = el("td", "num");
    const qty = el("input");
    qty.type = "number";
    qty.min = "1";
    qty.max = "9999";
    qty.value = item.qty;
    qty.className = "qty";
    qty.setAttribute("aria-label", `Количество: ${item.name}`);
    const cells = [];
    const sums = {};
    for (const id of MARKET_ORDER) {
      const info = item.prices[id];
      const unit = el("td", "num");
      const unitMoney = infoMoney(info);
      if (unitMoney) {
        unit.append(moneyNode(unitMoney));
      } else {
        unit.append(el("span", "muted", "нет в продаже"));
      }
      sums[id] = el("td", "num");
      cells.push(unit, sums[id]);
    }
    fillCalcSums(item, sums);
    // Строки не перерисовываются при вводе: меняются только суммы и итог,
    // иначе поле ввода пропадало бы из-под курсора.
    qty.addEventListener("input", () => {
      const value = parseInt(qty.value, 10);
      if (!(value >= 1)) return;
      item.qty = Math.min(9999, value);
      fillCalcSums(item, sums);
      renderCalcTotals();
    });
    qty.addEventListener("change", () => {
      item.qty = Math.max(1, Math.min(9999, parseInt(qty.value, 10) || 1));
      qty.value = item.qty;
      fillCalcSums(item, sums);
      renderCalcTotals();
    });
    qtyCell.append(qty);
    const removeCell = el("td");
    const remove = el("button", "ghost icon", "✕");
    remove.type = "button";
    remove.title = "Убрать из расчёта";
    remove.setAttribute("aria-label", `Убрать: ${item.name}`);
    remove.addEventListener("click", () => {
      calc.items.splice(index, 1);
      renderCalc();
    });
    removeCell.append(remove);
    tr.append(nameCell, qtyCell, ...cells, removeCell);
    return tr;
  }));
  $("calcWrap").classList.toggle("hidden", !calc.items.length);
  renderCalcTotals();
}

function renderCalcTotals() {
  const box = $("calcTotals");
  box.replaceChildren();
  if (!calc.items.length) return;
  if (marketsOff()) {
    box.append(el("p", "hint", "Площадки выключены (программа запущена с --no-markets) — цен StarPets и DreamPets нет."));
    return;
  }
  const totals = {};
  const missing = {};
  for (const id of MARKET_ORDER) {
    totals[id] = { usd: 0, rub: 0, usdApprox: false, rubApprox: false, incomplete: false,
      primary: id === "dreampets" ? "rub" : "usd" };
    missing[id] = 0;
    for (const item of calc.items) {
      const money = calcMoney(item, id);
      if (money === null) missing[id] += item.qty;
      else addMoney(totals[id], money);
    }
  }
  for (const id of MARKET_ORDER) {
    const card = el("div", `total ${id}`);
    card.append(el("span", "muted", `${marketTitle(id)} — вы получите (−${Math.round(calcFee(id) * 100)}%)`));
    const sum = el("div", "total-sum");
    sum.append(moneyNode(totals[id]));
    card.append(sum);
    if (missing[id]) card.append(el("span", "muted small", `не продаётся там: ${missing[id]} шт.`));
    box.append(card);
  }
  if (MARKET_ORDER.some((id) => totals[id].incomplete)) {
    box.append(el("p", "hint", "Нет курса доллара — часть сумм не пересчитана в другую валюту."));
  }
  const star = totals.starpets;
  const dream = totals.dreampets;
  if ((star.rub || dream.rub) && !star.incomplete && !dream.incomplete) {
    // Сравнение в рублях: у StarPets — его собственные рублёвые цены, у DreamPets — рубли.
    const best = star.rub > dream.rub ? "starpets" : "dreampets";
    const diff = Math.abs(star.rub - dream.rub);
    const approx = star.rubApprox || dream.rubApprox ? "≈" : "";
    box.append(el("p", "hint", `Выгоднее продать на ${marketTitle(best)} — больше на ${approx}${fmtRub(diff)}.`));
  }
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
    state.userUpdate = true;
    toast("Загружаю цены с сайта…");
  } catch (error) {
    toast(`Не удалось начать обновление: ${error.message}`, "error");
  }
  pollSoon();
}

async function pollMarkets() {
  try {
    await postJson("/api/markets/poll");
    toast("Опрашиваю площадки…");
  } catch (error) {
    toast(error.message, "error");
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
    state.offline = null;
    if (status.now) state.clockOffset = new Date(status.now).getTime() - Date.now();
    state.status = status;
    if (wasRunning && !status.running) {
      // О результате своего обновления — всегда; об автообновлении — только о новой ошибке.
      if (status.error && (state.userUpdate || status.error !== state.lastError)) {
        toast(`Не удалось обновить цены:\n${status.error}`, "error");
      } else if (!status.error) {
        toast((status.log || []).filter((line) => line.startsWith("Готово")).join("\n") || "Цены обновлены.", "ok");
      }
      state.lastError = status.error || null;
      state.userUpdate = false;
    }
    wasRunning = status.running;
    const version = `${status.data_version}:${status.market_version || 0}`;
    if (version !== state.dataVersion) {
      state.dataVersion = version;
      await loadData();
    }
    renderSync();
    renderRate();
    renderMarketCards();
  } catch (error) {
    state.offline = error.message;
    renderSync();
  }
  pollTimer = setTimeout(poll, state.status && state.status.running ? 1500 : 5000);
}

// --- общее --------------------------------------------------------------------

function renderAll() {
  renderHeader();
  renderMarketsTab();
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
  state.view = storageGet("mm2.view") === "table" ? "table" : "cards";
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
    cardShown = 0;
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
  $("search").addEventListener("input", (event) => { state.search = event.target.value; cardShown = 0; renderValues(); });
  for (const button of document.querySelectorAll("#viewToggle button")) {
    button.addEventListener("click", () => {
      state.view = button.dataset.view;
      storageSet("mm2.view", state.view);
      renderValues();
    });
  }
  $("sortSelect").addEventListener("change", (event) => {
    const [key, dir] = event.target.value.split(":");
    state.sort = { key, dir };
    storageSet("mm2.sort", JSON.stringify(state.sort));
    cardShown = 0;
    renderValues();
  });
  $("moreBtn").addEventListener("click", showMoreCards);
  $("mkSearch").addEventListener("input", (event) => { state.mkSearch = event.target.value; renderMarketsTab(); });
  $("mkCategory").addEventListener("change", (event) => { state.mkCategory = event.target.value; renderMarketsTab(); });
  $("mkSort").addEventListener("change", (event) => { state.mkSort = event.target.value; renderMarketsTab(); });
  $("mkOnlyListed").addEventListener("change", (event) => { state.mkOnlyListed = event.target.checked; renderMarketsTab(); });
  $("calcBtn").addEventListener("click", runCalc);
  $("calcClear").addEventListener("click", () => {
    calc.images = [];
    calc.items = [];
    $("calcText").value = "";
    $("calcStatus").textContent = "";
    $("calcUnmatched").textContent = "";
    renderCalcThumbs();
    renderCalc();
  });
  const drop = $("calcDrop");
  drop.addEventListener("click", () => $("calcFiles").click());
  drop.addEventListener("keydown", (event) => {
    if (event.target !== drop) return;  // Enter на ✕ у скриншота — убрать его
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      $("calcFiles").click();
    }
  });
  $("calcFiles").addEventListener("change", (event) => {
    addCalcFiles(event.target.files);
    event.target.value = "";
  });
  drop.addEventListener("dragover", (event) => { event.preventDefault(); drop.classList.add("over"); });
  drop.addEventListener("dragleave", () => drop.classList.remove("over"));
  drop.addEventListener("drop", (event) => {
    event.preventDefault();
    drop.classList.remove("over");
    addCalcFiles(event.dataTransfer.files);
  });
  document.addEventListener("paste", (event) => {
    if (!$("tab-calc") || $("tab-calc").classList.contains("hidden")) return;
    const files = [...(event.clipboardData ? event.clipboardData.files : [])];
    if (files.length) {
      event.preventDefault();
      addCalcFiles(files);
    }
  });
  if ("IntersectionObserver" in window) {
    // Карточки подгружаются сами, когда пользователь докручивает до конца списка.
    new IntersectionObserver((entries) => {
      if (entries.some((entry) => entry.isIntersecting)) showMoreCards();
    }, { rootMargin: "600px" }).observe($("moreRow"));
  }
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
  $("marketsBtn").addEventListener("click", pollMarkets);
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
