"use strict";

/* pictiles.js — калькулятор продажи: предметы на скриншотах инвентаря по их картинке.
 *
 * Названия на плитках и значки «xN» читает сервер (распознавание текста Windows), ответ
 * /api/calc приносит плитки каждого скриншота (shots). Здесь, в браузере, — то, чего
 * распознавание текста сделать не может:
 *   - одно название у нескольких предметов (Cowboy — ружьё и нож, Chroma и обычный):
 *     картинка плитки сравнивается с картинками каждого из них;
 *   - плитки без прочитанного названия и скриншоты, где распознавание не нашло плиток
 *     (PicMatch.detectTiles): картинка сравнивается со всеми предметами, количество
 *     читает сервер по значку «xN» (/api/calc/badges), нет значка — 1 шт.
 * Уверенно узнанное попадает в расчёт с пометкой «по картинке»; неуверенное показывается
 * миниатюрой с выбором предмета — решает пользователь. Картинки предметов идут через свой
 * сервер (/api/itemimg), их признаки хранятся на сервере (/api/picfeat) и считаются один раз.
 * Если картинки не загрузились (нет сети), расчёт по названиям остаётся как был.
 *
 * Пользуется общими функциями app.js: api, postJson, $, el, state, calc, itemKey,
 * categoryTitle, renderCalc. Из app.js вызываются: prefetch, reset, start, rowNote, onVariant.
 */
window.PicTiles = (() => {
  const SKIP_CATEGORIES = new Set(["sets"]);  // набор не лежит в инвентаре плиткой
  const MIN_TILE_WIDTH = 36;                  // мельче картинку уже не сравнить
  const REF_SIDE = 128;                       // картинка предмета уменьшается до 128 px — признакам хватает
  const URGENT_WORKERS = 4;
  const IDLE_WORKERS = 2;
  const BATCH = 120;                          // признаков в одном сохранении на сервер (файл пишется целиком)
  const OFFLINE_AFTER = 6;                    // столько обрывов подряд — картинок сейчас нет
  const BLOCK_PAUSE = 30 * 60 * 1000;         // сайт картинок не пускает программу — его картинки позже
  const PICKED_MAX = 500;                     // записей «picked» в одном /api/calc (как calc.MAX_PICKED)
  const THUMB_H = 64;
  const SUGGEST_MIN = 0.35;                   // похожесть, с которой предмет предлагается в списке
  const SUGGEST_COUNT = 4;

  const refs = {
    loaded: null,         // версия данных (state.dataVersion), для которой загружены признаки
    loading: null,
    sources: {},          // ключ предмета -> {site|market: адрес} — какие картинки есть у предмета
    stored: {},           // ключ -> {site|market: {url, f}} — признаки, сохранённые на сервере
    learned: {},          // ключ -> сколько картинок из игры выучено
    banners: {},          // корзина цвета баннера -> {категория: число}
    feats: new Map(),     // ключ -> [{key, source, feat}]
    all: null,            // все эталоны одним списком (для сравнения со всеми предметами)
    failed: new Set(),    // "ключ|источник" — не загрузились в этом сеансе
    blocked: new Map(),   // источник (site|market) -> до какого времени его сайт не пускает программу
    inflight: new Map(),
    netFails: 0,
    offline: false,
    warming: null,
  };

  const pics = {
    token: 0,             // номер расчёта: старые расчёты прекращаются
    tiles: [],            // плитки последнего расчёта (со всех скриншотов)
    shown: new Set(),     // ключи строк, которые были в таблице: пропавшую убрал пользователь
    templates: new Map(), // ключ -> предмет с ценами (из ответов /api/calc)
    busy: Promise.resolve(),
    badgeErrors: [],
    noPrices: new Set(),  // узнанные предметы, цены которых сервер не отдал (строку не добавить)
    summary: null,
  };

  // --- мелочи ------------------------------------------------------------------------

  const pct = (score) => `${Math.round(score * 100)}%`;
  const category = (key) => String(key).split(":")[0];

  function itemOf(key) {
    return state.data.items.find((item) => itemKey(item) === key) || null;
  }

  function sellableKeys() {
    return new Set(state.data.items.filter((item) => !item.secret && !SKIP_CATEGORIES.has(item.category)).map(itemKey));
  }

  function tileById(id) {
    return pics.tiles.find((t) => t.id === id) || null;
  }

  function statusBox() {
    let box = $("calcPicStatus");
    if (!box) {
      box = el("p", "hint pic-status");
      box.id = "calcPicStatus";
      box.setAttribute("role", "status");
      box.setAttribute("aria-live", "polite");
      const toolbar = $("calcStatus").closest(".toolbar") || $("calcStatus");
      toolbar.after(box);
    }
    return box;
  }

  function setStatus(text) {
    statusBox().textContent = text || "";
  }

  function panel() {
    let box = $("calcPicTiles");
    if (!box) {
      box = el("div", "pic-tiles");
      box.id = "calcPicTiles";
      $("calcUnmatched").after(box);
    }
    return box;
  }

  function loadImage(src) {
    return new Promise((resolve, reject) => {
      const img = new Image();
      img.decoding = "async";
      img.onload = () => resolve(img);
      img.onerror = () => reject(new Error("картинка не открылась"));
      img.src = src;
    });
  }

  function drawCanvas(source, maxSide) {
    const sw = source.naturalWidth || source.width, sh = source.naturalHeight || source.height;
    const scale = maxSide ? Math.min(1, maxSide / Math.max(sw, sh)) : 1;
    const canvas = document.createElement("canvas");
    canvas.width = Math.max(1, Math.round(sw * scale));
    canvas.height = Math.max(1, Math.round(sh * scale));
    const ctx = canvas.getContext("2d", { willReadFrequently: true });
    ctx.imageSmoothingQuality = "high";
    ctx.drawImage(source, 0, 0, canvas.width, canvas.height);
    return { canvas, data: ctx.getImageData(0, 0, canvas.width, canvas.height) };
  }

  // Плитка со скриншота -> маленькая картинка для таблицы и списка «выберите предмет».
  function thumb(canvas, t) {
    const w = t.x1 - t.x0 + 1;
    const bottom = t.banner ? Math.min(canvas.height - 1, t.banner.y1) : t.iconBottom;
    const h = bottom - t.iconTop + 1;
    if (w < 4 || h < 4) return null;
    const out = document.createElement("canvas");
    out.height = THUMB_H;
    out.width = Math.max(1, Math.round(w * THUMB_H / h));
    const ctx = out.getContext("2d");
    ctx.imageSmoothingQuality = "high";
    ctx.drawImage(canvas, t.x0, t.iconTop, w, h, 0, 0, out.width, out.height);
    return out.toDataURL("image/png");
  }

  // --- эталоны: картинки предметов и их признаки ---------------------------------------

  function addRef(key, source, packed) {
    const feat = PicMatch.unpack(packed);
    if (!feat) return;
    const list = refs.feats.get(key) || [];
    if (source !== "learned") {
      const at = list.findIndex((r) => r.source === source);
      if (at >= 0) list.splice(at, 1);
    }
    list.push({ key, source, feat });
    refs.feats.set(key, list);
    refs.all = null;
  }

  // Признаки с сервера; заново — когда изменились цены или площадки (новые предметы и картинки).
  function loadRefs() {
    const version = String(state.dataVersion);
    if (refs.loaded === version) return Promise.resolve();
    if (!refs.loading) {
      refs.loading = api("/api/picfeat").then((snap) => {
        const same = snap.version === PicMatch.FEAT_VERSION;
        refs.sources = snap.sources || {};
        refs.stored = same ? snap.feats || {} : {};
        refs.banners = same ? snap.banners || {} : {};
        refs.feats = new Map();
        refs.learned = {};
        refs.all = null;
        for (const [key, entries] of Object.entries(refs.stored)) {
          for (const [source, entry] of Object.entries(entries)) {
            const now = refs.sources[key] && refs.sources[key][source];
            if (entry && entry.url === now) addRef(key, source, entry.f);  // картинка сменилась — посчитать заново
          }
        }
        for (const [key, list] of Object.entries(same ? snap.learned || {} : {})) {
          refs.learned[key] = list.length;
          for (const f of list) addRef(key, "learned", f);
        }
        refs.loaded = version;
      }).finally(() => { refs.loading = null; });
    }
    return refs.loading;
  }

  function blocked(source) {
    return (refs.blocked.get(source) || 0) > Date.now();
  }

  function hasStored(key, source, url) {
    const entry = refs.stored[key] && refs.stored[key][source];
    return Boolean(entry && entry.url === url);
  }

  // Картинки, у которых ещё нет признаков. firstOnly — одна картинка на предмет (сначала
  // с сайта, если не загрузится — со StarPets), остальные считаются потом, в фоне.
  function missingJobs(keys, firstOnly) {
    const jobs = [];
    for (const key of keys) {
      const sources = Object.entries(refs.sources[key] || {});
      const fresh = sources.filter(([src, url]) => !hasStored(key, src, url) && !refs.failed.has(`${key}|${src}`) && !blocked(src))
        .map(([src, url]) => ({ key, src, url }));
      if (!fresh.length) continue;
      if (!firstOnly) {
        jobs.push(...fresh);
      } else if (!sources.some(([src, url]) => hasStored(key, src, url))) {
        for (let i = 0; i + 1 < fresh.length; i++) fresh[i].fallback = fresh[i + 1];
        jobs.push(fresh[0]);
      }
    }
    return jobs;
  }

  async function refImageData(blob) {
    if (typeof createImageBitmap === "function") {
      const bitmap = await createImageBitmap(blob);
      try { return drawCanvas(bitmap, REF_SIDE).data; } finally { bitmap.close(); }
    }
    const url = URL.createObjectURL(blob);
    try { return drawCanvas(await loadImage(url), REF_SIDE).data; } finally { URL.revokeObjectURL(url); }
  }

  function noteNetFail() {
    refs.netFails += 1;
    if (refs.netFails >= OFFLINE_AFTER) refs.offline = true;
  }

  // Одна картинка -> упакованные признаки (или null). Одинаковые задания не идут дважды:
  // второе ждёт результат первого.
  function refJob(job) {
    const id = `${job.key}|${job.src}`;
    if (refs.inflight.has(id)) return refs.inflight.get(id);
    const promise = (async () => {
      try {
        const response = await fetch(`/api/itemimg?key=${encodeURIComponent(job.key)}&src=${encodeURIComponent(job.src)}`,
          { cache: "no-store" });
        if (!response.ok) {
          if (response.status === 503) noteNetFail();
          else if (response.status === 429) refs.blocked.set(job.src, Date.now() + BLOCK_PAUSE);  // весь сайт — позже
          else refs.failed.add(id);
          return null;
        }
        const blob = await response.blob();
        let feat = null;
        try {
          feat = PicMatch.refFeature(await refImageData(blob));
        } catch (error) { /* картинка не открылась (испорчена) — как картинки нет */ }
        refs.netFails = 0;
        if (!feat) {
          refs.failed.add(id);  // пустая или испорченная картинка: возьмётся другая картинка предмета
          return null;
        }
        const f = PicMatch.pack(feat);
        addRef(job.key, job.src, f);
        refs.stored[job.key] = refs.stored[job.key] || {};
        refs.stored[job.key][job.src] = { url: job.url, f };
        return f;
      } catch (error) {
        noteNetFail();  // программа не ответила или картинка не открылась
        return null;
      } finally {
        refs.inflight.delete(id);
      }
    })();
    refs.inflight.set(id, promise);
    return promise;
  }

  // Посчитать признаки картинок (по workers одновременно) и сохранить их на сервере пачками.
  async function computeRefs(jobs, workers, onProgress, alive) {
    let next = 0, done = 0, batch = {}, size = 0;
    const flush = async () => {
      if (!size) return;
      const feats = batch;
      batch = {};
      size = 0;
      try {
        await postJson("/api/picfeat", { version: PicMatch.FEAT_VERSION, feats });
      } catch (error) { /* не сохранилось — посчитается в следующий раз */ }
    };
    const worker = async () => {
      while (next < jobs.length && !refs.offline && (!alive || alive())) {
        const job = jobs[next++];
        const f = blocked(job.src) ? null : await refJob(job);
        if (f) {
          batch[job.key] = batch[job.key] || {};
          batch[job.key][job.src] = { url: job.url, f };
          size += 1;
        } else if (job.fallback && !refs.offline) {
          jobs.push(job.fallback);
        }
        done += 1;
        if (onProgress) onProgress(done, jobs.length);
        if (size >= BATCH) await flush();
      }
    };
    await Promise.all(Array.from({ length: Math.min(workers, jobs.length) }, worker));
    await flush();
  }

  // Остальные картинки — в фоне, чтобы следующий расчёт не ждал.
  function warmLater() {
    if (refs.warming || refs.offline) return;
    const start = () => {
      const jobs = missingJobs([...sellableKeys()], false);
      if (!jobs.length || refs.offline) return;
      refs.warming = computeRefs(jobs, IDLE_WORKERS).finally(() => { refs.warming = null; });
    };
    if (typeof requestIdleCallback === "function") requestIdleCallback(start, { timeout: 5000 });
    else setTimeout(start, 1500);
  }

  function allRefs() {
    if (!refs.all) {
      const keys = sellableKeys();
      refs.all = [...refs.feats.entries()].filter(([key]) => keys.has(key)).flatMap(([, list]) => list);
    }
    return refs.all;
  }

  // Похожесть плитки на предмет (лучший из его эталонов) или null, если картинок предмета нет.
  function refScore(feat, key) {
    const list = refs.feats.get(key);
    if (!feat || !list || !list.length) return null;
    return Math.max(...list.map((r) => PicMatch.similarity(feat, r.feat).score));
  }

  // --- плитки ------------------------------------------------------------------------

  function fromOcr(shot, t) {
    return {
      id: t.id, shot: shot.index, source: "ocr", key: t.key, sure: Boolean(t.sure),
      via: t.key ? "name" : null, candidates: t.candidates || [], name: t.name, score: Number(t.score) || 0,
      qty: Math.max(1, Number(t.qty) || 1), qtyUncertain: Boolean(t.qty_uncertain), hasBadge: Boolean(t.qty_from_badge),
      clipped: Boolean(t.clipped), box: t.box, badgeBox: t.badge_box, nameBox: t.name_box, bg: t.bg,
      geom: null, feat: null, thumb: null, color: null, top: [], unsure: false, ignored: false,
    };
  }

  function centre(box) {
    return Array.isArray(box) && box.length >= 4 ? [box[0] + box[2] / 2, box[1] + box[3] / 2] : null;
  }

  // Плитка, найденная по картинке, в которой лежит плитка распознавания (её место, название
  // или значок): одна плитка не должна посчитаться дважды.
  function sameTile(found, used, tile) {
    const points = [tile.box, tile.nameBox, tile.badgeBox].map(centre).filter(Boolean);
    return found.find((d) => !used.has(d) && points.some(([x, y]) => x >= d.x0 && x <= d.x1
      && y >= d.iconTop && y <= d.banner.y1 + 2)) || null;
  }

  // Место плитки распознавания в пределах скриншота (для миниатюры), или null.
  function boxGeom(box, canvas) {
    if (!Array.isArray(box) || box.length < 4) return null;
    const x0 = Math.max(0, Math.round(box[0])), iconTop = Math.max(0, Math.round(box[1]));
    const x1 = Math.min(canvas.width - 1, Math.round(box[0] + box[2]) - 1);
    const iconBottom = Math.min(canvas.height - 1, Math.round(box[1] + box[3]) - 1);
    return x1 - x0 >= 4 && iconBottom - iconTop >= 4 ? { x0, x1, iconTop, iconBottom, banner: null } : null;
  }

  // Плитка без названия, которую не сравнить как обычно (обрезана краем скриншота, слишком мелкая):
  // не пропадает молча — похожие предметы показываются подсказкой, а выбирает пользователь.
  function partialTile(t, canvas, data) {
    t.partialView = true;
    const geom = PicMatch.tileFromBox(data, t.box, t.badgeBox, t.bg);
    const wide = Boolean(geom && geom.x1 - geom.x0 + 1 >= MIN_TILE_WIDTH);
    if (wide) {
      t.geom = geom;
      t.color = geom.color;
      t.feat = PicMatch.tileFeature(data, geom);
    }
    const place = wide ? geom : boxGeom(t.box, canvas);
    t.thumb = place ? thumb(canvas, place) : null;
  }

  // Скриншот: плитки по картинке; плитки распознавания получают точное место, признаки и миниатюру.
  // false — скриншот прочитан как текст и плиток инвентаря на нём не видно (список, чат): его
  // строки остаются как есть. Одно случайное слово («Back» -> Black) инвентарь текстом не делает.
  async function scanShot(shot, dataUrl) {
    const { canvas, data } = drawCanvas(await loadImage(dataUrl), 0);
    const found = PicMatch.detectTiles(data).filter((d) => !d.partial && d.x1 - d.x0 + 1 >= MIN_TILE_WIDTH);
    const mine = pics.tiles.filter((t) => t.shot === shot.index);
    if (shot.mode === "text" && shot.text_items > 0 && found.length < 2
        && !mine.some((t) => sameTile(found, new Set(), t))) return false;
    const used = new Set();
    for (const tile of mine) {
      const hit = sameTile(found, used, tile);
      if (hit) used.add(hit);
      // У края скриншота распознавание не верит названию ('Chroma Seer' -> 'Seer'); если
      // картинка плитки видна целиком, предмет узнаётся по ней.
      if (!tile.clipped || hit) {
        tile.clipped = false;
        const geom = hit || PicMatch.tileFromBox(data, tile.box, tile.badgeBox, tile.bg);
        if (geom && geom.x1 - geom.x0 + 1 >= MIN_TILE_WIDTH) {
          tile.geom = geom;
          tile.color = geom.color || PicMatch.colorFromRgb(tile.bg);
          tile.feat = PicMatch.tileFeature(data, geom);
          tile.thumb = thumb(canvas, geom);
        }
      }
      if (!tile.key && !tile.feat) partialTile(tile, canvas, data);
    }
    let number = 0;
    for (const d of found) {
      if (used.has(d)) continue;
      const feat = PicMatch.tileFeature(data, d);
      if (!feat) continue;  // пустой квадрат — не предмет
      pics.tiles.push({
        id: `${shot.index}:p${number++}`, shot: shot.index, source: "pic", key: null, sure: false, via: null,
        candidates: [], name: null, score: 0, qty: 1, qtyUncertain: false, hasBadge: Boolean(d.badge),
        clipped: false, box: [d.x0, d.iconTop, d.x1 - d.x0 + 1, d.banner.y1 - d.iconTop + 1],
        geom: d, feat, thumb: thumb(canvas, d), color: d.color, top: [], unsure: false, ignored: false,
      });
    }
    return true;
  }

  // Количество у плиток, найденных только по картинке: значок «xN» читает сервер (угол каждой
  // плитки — и там, где браузер значка не увидел: мелкое «x2» он может пропустить).
  // Прочитано число — оно; значок есть, но число не прочиталось — «проверьте количество»;
  // значка нет — 1 шт.
  async function readBadges(images) {
    const byShot = new Map();
    for (const t of pics.tiles) {
      if (t.source !== "pic") continue;
      if (!byShot.has(t.shot)) byShot.set(t.shot, []);
      byShot.get(t.shot).push(t);
    }
    if (!byShot.size) return;
    const apply = (t, read) => {
      if (read && read.qty_from_badge) {
        t.qty = Math.max(1, Math.min(9999, Number(read.qty) || 1));
        t.hasBadge = true;
        t.qtyUncertain = Boolean(read.qty_uncertain);
      } else {
        t.qtyUncertain = t.hasBadge || Boolean(read && read.qty_uncertain);
      }
    };
    try {
      const response = await postJson("/api/calc/badges", {
        shots: [...byShot].map(([index, tiles]) => ({ index, image: images[index].data, boxes: tiles.map((t) => t.box) })),
      });
      const answered = new Map((response.shots || []).filter((s) => s.read).map((s) => [s.index, s.tiles || []]));
      for (const [index, tiles] of byShot) tiles.forEach((t, i) => apply(t, (answered.get(index) || [])[i]));
      pics.badgeErrors = response.ocr_errors || [];
    } catch (error) {
      for (const tiles of byShot.values()) tiles.forEach((t) => apply(t, null));
      pics.badgeErrors = [error.message];
    }
  }

  function setKey(t, key, via) {
    t.key = key;
    t.sure = true;
    t.via = via;
    t.unsure = false;
  }

  // Одно название — несколько предметов: выбрать по картинке, если она уверенно одна.
  function decideVariant(t) {
    const scores = t.candidates.map((key) => ({ key, score: refScore(t.feat, key) }));
    if (scores.some((s) => s.score === null)) return false;  // у варианта нет картинки — решит пользователь
    scores.sort((a, b) => b.score - a.score);
    t.top = scores;
    const key = PicMatch.sureKey(scores);
    if (!key) return false;
    setKey(t, key, "pic");
    return true;
  }

  // Плитка без названия: сравнить со всеми предметами (цвет баннера — мягкая подсказка).
  // Плитка, видная не целиком, сама не решается — только подсказки.
  function decideByPicture(t) {
    const ranked = t.feat
      ? PicMatch.applyPrior(PicMatch.rank(t.feat, allRefs(), 8), PicMatch.rarityPrior(t.color, refs.banners)) : [];
    t.top = ranked.map((r) => ({ key: r.key, score: r.score }));
    const key = t.partialView ? null : PicMatch.sureKey(ranked);
    if (key) setKey(t, key, "pic");
    else t.unsure = true;
    return Boolean(key);
  }

  // --- обучение: картинки из игры и цвета баннеров ---------------------------------------

  const taught = new Set();

  // Плитка t — предмет key: запомнить её картинку и цвет баннера. Сервер получает всё одним
  // запросом (файл признаков переписывается целиком); body — общий запрос для нескольких плиток.
  function teach(t, key, body) {
    const own = body || { version: PicMatch.FEAT_VERSION, learn: [], banner: [] };
    if (t.feat && !t.partialView) {  // плитка видна не целиком — её картинку не запоминать
      const f = PicMatch.pack(t.feat);
      addRef(key, "learned", f);
      refs.learned[key] = (refs.learned[key] || 0) + 1;
      own.learn.push({ key, f });
    }
    const bucket = PicMatch.bannerBucket(t.color);
    if (bucket && t.geom && !t.geom.approx) own.banner.push({ bucket, category: category(key) });
    if (!body) sendLearned(own);
  }

  function sendLearned(body) {
    if (body.learn.length || body.banner.length) postJson("/api/picfeat", body).catch(() => {});
  }

  // Название прочитано точно и картинка с ним согласна — запомнить, как предмет выглядит в игре
  // (по одной картинке на предмет; так узнаются и предметы без картинки на сайте).
  function learnFromNames() {
    const body = { version: PicMatch.FEAT_VERSION, learn: [], banner: [] };
    for (const t of pics.tiles) {
      if (t.source !== "ocr" || t.via !== "name" || !t.sure || t.candidates.length !== 1 || t.score < 0.97) continue;
      if (!t.feat || !t.geom || t.geom.approx || refs.learned[t.key] || taught.has(t.key)) continue;
      const score = refScore(t.feat, t.key);
      if (score !== null && score < PicMatch.PIC_AGREE) continue;
      taught.add(t.key);
      teach(t, t.key, body);
    }
    sendLearned(body);
  }

  // --- строки таблицы ----------------------------------------------------------------

  // Поля строки расчёта, которые относятся к найденным плиткам, а не к самому предмету.
  const ROW_FIELDS = ["qty", "seen", "score", "tiles", "tile_qty", "via", "ambiguous", "qty_uncertain", "alternatives"];

  // Предмет с ценами (из ответа /api/calc) — заготовка для строки, которую добавит картинка.
  function rememberTemplates(rows) {
    for (const row of rows || []) {
      for (const item of [row, ...(row.alternatives || [])]) {
        const key = item.key || itemKey(item);
        if (pics.templates.has(key)) continue;
        const template = Object.assign({}, item, { key });
        for (const field of ROW_FIELDS) delete template[field];
        pics.templates.set(key, template);
      }
    }
  }

  // Цены предметов keys (сначала — узнанные, потом — их варианты для выбора в строке):
  // сервер берёт не больше PICKED_MAX записей за раз, поэтому — частями.
  async function fetchTemplates(keys) {
    const need = [...new Set(keys)].filter((key) => key && !pics.templates.has(key));
    for (let at = 0; at < need.length; at += PICKED_MAX) {
      const part = need.slice(at, at + PICKED_MAX);
      try {
        const response = await postJson("/api/calc", { picked: part.map((key) => ({ key, qty: 1, via: "pic" })) });
        rememberTemplates(response.items);
      } catch (error) { /* без цен строки не добавить — о таких предметах скажет строка состояния */ }
    }
  }

  function counted(key) {
    const tiles = PicMatch.mergeByKey(pics.tiles.filter((t) => !t.ignored && t.key === key)).get(key) || [];
    return { tiles, qty: tiles.reduce((sum, t) => sum + Math.max(1, t.qty), 0) };
  }

  // Несколько строк одного предмета (вариант в строке выбран тот, что уже есть строкой) -> одна:
  // количество складывается, а плитки пересчитает updateRow (одна стопка — один раз).
  function mergeRows(key, keep) {
    const rows = calc.items.filter((item) => item.key === key);
    if (rows.length < 2) return;
    const main = rows.includes(keep) ? keep : rows[0];
    const alternatives = new Map((main.alternatives || []).map((alt) => [alt.key || itemKey(alt), alt]));
    for (const row of rows) {
      if (row === main) continue;
      main.qty = (Number(main.qty) || 0) + (Number(row.qty) || 0);
      main.tile_qty = (Number(main.tile_qty) || 0) + (Number(row.tile_qty) || 0);
      main.seen = [...(main.seen || []), ...(row.seen || [])];
      main.score = Math.min(Number(main.score) || 0, Number(row.score) || 0);
      main.qty_uncertain = Boolean(main.qty_uncertain || row.qty_uncertain);
      for (const alt of row.alternatives || []) {
        const altKey = alt.key || itemKey(alt);
        if (altKey !== key && !alternatives.has(altKey)) alternatives.set(altKey, alt);
      }
      calc.items.splice(calc.items.indexOf(row), 1);
    }
    main.alternatives = [...alternatives.values()];
  }

  // Строка предмета key по плиткам: количество с плиток пересчитывается (одна стопка на
  // нескольких скриншотах — один раз). Что сверх плиток — из текста или исправлено
  // пользователем в строке («продаю только 5 из 12») — остаётся: меняется лишь разница плиток.
  function updateRow(key) {
    mergeRows(key);
    const { tiles, qty } = counted(key);
    const refsOf = tiles.map((t) => ({ id: t.id, qty: t.qty }));
    let row = calc.items.find((item) => item.key === key);
    if (row) {
      const next = (Number(row.qty) || 0) + qty - (Number(row.tile_qty) || 0);
      Object.assign(row, {
        qty: Math.max(0, Math.min(9999, next)), tile_qty: qty, tiles: refsOf,
        ambiguous: tiles.some((t) => t.candidates.length > 1 && !t.sure),
        qty_uncertain: tiles.some((t) => t.qtyUncertain),
      });
      if (!row.qty) {
        calc.items.splice(calc.items.indexOf(row), 1);
        pics.shown.delete(key);
      }
      return 0;
    }
    if (!qty || pics.shown.has(key)) return 0;  // строку убрал пользователь
    if (!pics.templates.has(key)) {
      pics.noPrices.add(key);  // не молча: о таких предметах сказано в строке состояния
      return 0;
    }
    pics.noPrices.delete(key);
    const others = new Set();
    for (const t of tiles) {
      for (const other of t.candidates.length > 1 ? t.candidates : t.top.slice(0, 3).map((s) => s.key)) {
        if (other !== key && pics.templates.has(other)) others.add(other);
      }
    }
    row = Object.assign({}, pics.templates.get(key), {
      qty: Math.min(9999, qty), tile_qty: qty, tiles: refsOf, score: 1, seen: [],
      via: tiles.every((t) => t.via === "pic") ? "pic" : "user", ambiguous: false,
      qty_uncertain: tiles.some((t) => t.qtyUncertain),
      alternatives: [...others].map((other) => Object.assign({}, pics.templates.get(other))),
    });
    calc.items.push(row);
    pics.shown.add(key);
    return 1;
  }

  // Плитки сменили предмет: пересчитать затронутые строки (и узнать цены новых предметов).
  // alive() — расчёт ещё тот же (пока шли цены, могли начать новый: его строки не трогать).
  async function applyKeys(keys, alive) {
    const affected = [...new Set(keys)].filter(Boolean);
    const want = [], extra = [];
    for (const key of affected) {
      if (calc.items.some((item) => item.key === key)) continue;
      want.push(key);
      for (const t of counted(key).tiles) {
        extra.push(...(t.candidates.length > 1 ? t.candidates : t.top.slice(0, 3).map((s) => s.key)));
      }
    }
    await fetchTemplates([...want, ...extra]);  // цены узнанных — первыми
    if (alive && !alive()) return 0;
    let added = 0;
    for (const key of affected) added += updateRow(key);
    return added;
  }

  // --- расчёт ------------------------------------------------------------------------

  // Скриншот-инвентарь, который сервер прочитал как текст (названия на плитках не прочитались):
  // предметы, найденные в этом тексте (слово «Back» — похоже на Black), не считаются — плитки
  // решают картинки, а точно прочитанные названия остаются подсказкой у плиток этого скриншота.
  function dropText(shots) {
    const keys = [];
    for (const shot of shots) {
      const hints = (shot.text_rows || []).filter((r) => r.score >= 1).map((r) => r.key);
      for (const t of pics.tiles) if (t.shot === shot.index) t.hints = hints;
      for (const r of shot.text_rows || []) {
        const row = calc.items.find((item) => item.key === r.key);
        if (!row) continue;
        keys.push(r.key);
        row.qty = Math.max(0, (Number(row.qty) || 0) - (Number(r.qty) || 0));
        for (const seen of r.seen || []) {
          const at = (row.seen || []).indexOf(seen);
          if (at >= 0) row.seen.splice(at, 1);
        }
        if (!(row.seen || []).length) row.score = 1;  // «похоже на …» было только отсюда
        if (row.qty <= 0) {
          calc.items.splice(calc.items.indexOf(row), 1);
          pics.shown.delete(r.key);  // строку убрал не пользователь: плитка этого предмета её вернёт
        }
      }
    }
    return keys;
  }

  async function analyse(token, response, images) {
    const alive = () => token === pics.token;
    const shots = (response.shots || []).filter((s) => images[s.index] && images[s.index].data);
    pics.tiles = [];
    for (const shot of shots) for (const t of shot.tiles || []) pics.tiles.push(fromOcr(shot, t));
    pics.templates = new Map();
    rememberTemplates(calc.items);
    pics.shown = new Set(calc.items.map((item) => item.key).filter(Boolean));
    if (!shots.length) return;
    setStatus("Ищу плитки на скриншотах по картинке…");
    const textShots = [];  // прочитаны как текст, но на них плитки инвентаря
    for (const shot of shots) {
      const inventory = await scanShot(shot, images[shot.index].data);
      if (!alive()) return;
      if (inventory && shot.mode === "text" && shot.text_items > 0) textShots.push(shot);
    }
    const variants = pics.tiles.filter((t) => t.feat && t.candidates.length > 1 && !t.sure);
    const nameless = pics.tiles.filter((t) => !t.key && (t.feat || t.partialView));
    const before = new Map(pics.tiles.map((t) => [t.id, [t.key, t.sure]]));
    const badges = readBadges(images);
    try {
      await loadRefs();
    } catch (error) { /* без сохранённых признаков — посчитаем нужные сейчас */ }
    if (!alive()) return;
    const want = new Set();
    variants.forEach((t) => t.candidates.forEach((key) => want.add(key)));
    pics.tiles.filter((t) => t.via === "name" && t.feat && t.candidates.length === 1).forEach((t) => want.add(t.key));
    const urgent = missingJobs([...want], false);
    if (nameless.some((t) => t.feat)) urgent.push(...missingJobs([...sellableKeys()].filter((key) => !want.has(key)), true));
    if (urgent.length) {
      await computeRefs(urgent, URGENT_WORKERS, (done, total) => {
        if (alive()) setStatus(`Готовлю картинки предметов для сравнения (один раз): ${done} из ${total}…`);
      }, alive);
      if (!alive()) return;
    }
    setStatus("Сравниваю картинки…");
    // Сначала — плитки с точно прочитанным названием: их картинка из игры узнаёт тот же предмет
    // на других скриншотах (и без связи с сайтами картинок).
    learnFromNames();
    let resolved = 0, byPicture = 0;
    for (const t of variants) if (decideVariant(t)) resolved += 1;
    for (const [index, t] of nameless.entries()) {
      if (decideByPicture(t)) byPicture += 1;
      if (index % 8 === 7) await new Promise((resolve) => setTimeout(resolve));
    }
    await badges;
    if (!alive()) return;
    const keys = dropText(textShots);
    for (const t of pics.tiles) {
      const [key, sure] = before.get(t.id);
      if (t.key !== key || t.sure !== sure) keys.push(key, t.key);  // и «вариант подтверждён картинкой»
    }
    const added = await applyKeys(keys, alive);
    if (!alive()) return;
    const dropped = textShots.filter((shot) => (shot.text_rows || []).length)
      .map((shot) => ({ shot: shot.index, names: shot.text_rows.map((r) => (itemOf(r.key) || { name: r.key }).name) }));
    pics.summary = { resolved, byPicture, added, offline: refs.offline, dropped,
                     blocked: [...refs.blocked.keys()].some(blocked) };
    renderCalc();
    renderUnsure();
    updateStatus();
    warmLater();
  }

  function updateStatus() {
    const s = pics.summary;
    if (!s) return;
    const unsure = pics.tiles.filter((t) => t.unsure && !t.key && !t.ignored).length;
    const parts = [];
    if (s.byPicture) parts.push(`узнано плиток по картинке: ${s.byPicture}`);
    if (s.resolved) parts.push(`вариант выбран по картинке: ${s.resolved}`);
    if (unsure) parts.push(`не уверен: ${unsure} — выберите ниже`);
    for (const d of s.dropped || []) {
      parts.push(`со скриншота ${d.shot + 1} не учтено прочитанное как текст (${d.names.join(", ")}): на нём плитки инвентаря — их решают картинки`);
    }
    if (s.offline) parts.push("картинки предметов не загрузились (нет связи)");
    else if (s.blocked) parts.push("сайт картинок сейчас не пускает программу — его картинки будут позже");
    if (pics.badgeErrors.length) parts.push(`количество не прочитано: ${pics.badgeErrors.join("; ")}`);
    if (pics.noPrices.size) {
      const names = [...pics.noPrices].map((key) => (itemOf(key) || { name: key }).name);
      parts.push(`не удалось получить цены: ${names.join(", ")} — добавьте их строкой`);
    }
    setStatus(parts.length ? `По картинкам: ${parts.join("; ")}.` : "");
    updateFound();
  }

  // Строки добавлены или убраны — число найденных предметов в строке над таблицей тоже.
  function updateFound() {
    const found = $("calcStatus");
    found.textContent = found.textContent.replace(/^Найдено предметов: \d+/, `Найдено предметов: ${calc.items.length}`);
  }

  // --- плитки, в которых программа не уверена: выбирает пользователь --------------------

  let namesVersion = null;

  function namesList() {
    let list = $("calcPicNames");
    if (!list) {
      list = el("datalist");
      list.id = "calcPicNames";
      document.body.append(list);
    }
    const version = state.data.items.length + (state.data.fetched_at || "");
    if (namesVersion !== version) {
      namesVersion = version;
      list.replaceChildren(...state.data.items.filter((item) => !item.secret && !SKIP_CATEGORIES.has(item.category))
        .map((item) => new Option(`${item.name} — ${categoryTitle(item.category)}`)));
    }
    return list;
  }

  function keyFromText(text) {
    const value = String(text || "").trim().toLowerCase();
    if (!value) return null;
    const items = state.data.items.filter((item) => !item.secret && !SKIP_CATEGORIES.has(item.category));
    const full = items.find((item) => `${item.name} — ${categoryTitle(item.category)}`.toLowerCase() === value);
    if (full) return itemKey(full);
    const named = items.filter((item) => item.name.toLowerCase() === value);
    return named.length === 1 ? itemKey(named[0]) : null;  // одно название у нескольких — нужна категория
  }

  async function choose(t, key) {
    const token = pics.token;
    const old = t.key;
    // Строку этого предмета пользователь раньше убрал, а теперь сам выбрал его для плитки — вернуть.
    if (!calc.items.some((item) => item.key === key)) pics.shown.delete(key);
    setKey(t, key, "user");
    teach(t, key);
    const keys = [old, key];
    // та же картинка на других плитках (тот же предмет на другом скриншоте) — теперь узнаётся
    for (const other of pics.tiles) {
      if (other.unsure && !other.key && !other.ignored && other.feat && decideByPicture(other)) keys.push(other.key);
    }
    await applyKeys(keys, () => token === pics.token);
    if (token !== pics.token) return;
    renderCalc();
    renderUnsure();
    updateStatus();
  }

  function unsureRow(t) {
    const row = el("div", "pic-tile");
    if (t.thumb) {
      const img = el("img", "tile-thumb");
      img.src = t.thumb;
      img.alt = "плитка со скриншота";
      row.append(img);
    }
    const info = el("div", "pic-tile-info");
    const head = el("div", "");
    head.append(el("span", "", `Скриншот ${t.shot + 1}${t.qty > 1 ? ` · ×${t.qty}` : ""}`));
    if (t.qtyUncertain) head.append(el("span", "badge warn-badge", "проверьте количество"));
    info.append(head);
    const top = t.top.filter((s) => s.score >= SUGGEST_MIN && itemOf(s.key)).slice(0, SUGGEST_COUNT);
    // названия, точно прочитанные на этом скриншоте как текст (плитку с ними связать не удалось)
    const hints = (t.hints || []).filter((key) => itemOf(key) && !top.some((s) => s.key === key));
    const guess = top.length ? `похоже на «${itemOf(top[0].key).name}» (${pct(top[0].score)}), но не уверен`
      : (refs.offline || !allRefs().length ? "картинки предметов не загрузились — выберите сами" : "не узнал по картинке");
    const note = t.partialView ? `Плитка видна не целиком: ${top.length ? `похоже на «${itemOf(top[0].key).name}» (${pct(top[0].score)})` : "выберите сами"}`
      : guess[0].toUpperCase() + guess.slice(1);
    info.append(el("span", "muted small", note));
    const controls = el("div", "pic-tile-controls");
    const select = el("select", "pic-choice");
    select.setAttribute("aria-label", "Какой это предмет");
    select.append(new Option("Выберите предмет…", ""));
    for (const s of top) {
      const item = itemOf(s.key);
      select.append(new Option(`${item.name} — ${categoryTitle(item.category)} · ${pct(s.score)}`, s.key));
    }
    for (const key of hints) {
      const item = itemOf(key);
      select.append(new Option(`${item.name} — ${categoryTitle(item.category)} · прочитано на скриншоте`, key));
    }
    select.append(new Option("Другой предмет…", "__other"));
    select.append(new Option("Не считать", "__skip"));
    const other = el("input", "pic-other hidden");
    other.setAttribute("list", namesList().id);
    other.placeholder = "Название предмета";
    other.setAttribute("aria-label", "Название предмета");
    select.addEventListener("change", () => {
      other.classList.toggle("hidden", select.value !== "__other");
      if (select.value === "__other") {
        other.focus();
      } else if (select.value === "__skip") {
        t.ignored = true;
        renderUnsure();
        updateStatus();
      } else if (select.value) {
        choose(t, select.value);
      }
    });
    other.addEventListener("change", () => {
      const key = keyFromText(other.value);
      other.setCustomValidity(key ? "" : "Выберите предмет из списка");
      other.reportValidity();
      if (key) choose(t, key);
    });
    controls.append(select, other);
    info.append(controls);
    row.append(info);
    return row;
  }

  function renderUnsure() {
    const box = panel();
    const list = pics.tiles.filter((t) => t.unsure && !t.key && !t.ignored);
    box.replaceChildren();
    box.classList.toggle("hidden", !list.length);
    if (!list.length) return;
    box.append(el("p", "hint", "Эти плитки не узнаны уверенно — выберите предмет (или «Не считать»):"));
    for (const t of list) box.append(unsureRow(t));
  }

  // --- для app.js --------------------------------------------------------------------

  // Скриншоты добавлены: загрузить сохранённые признаки и готовить картинки предметов в фоне.
  function prefetch() {
    loadRefs().then(warmLater).catch(() => {});
  }

  function reset() {
    pics.token += 1;
    pics.tiles = [];
    pics.summary = null;
    pics.badgeErrors = [];
    pics.noPrices = new Set();
    refs.offline = false;
    refs.netFails = 0;
    setStatus("");
    renderUnsure();
  }

  // После ответа /api/calc со скриншотами (строки уже показаны): уточнить по картинкам.
  function start(response, images) {
    const token = ++pics.token;
    refs.offline = false;
    refs.netFails = 0;
    pics.busy = analyse(token, response, images).catch((error) => {
      if (token !== pics.token) return;
      const nameless = response.unknown_tiles ? `; плиток без названия: ${response.unknown_tiles} — допишите их строкой` : "";
      setStatus(`Сравнение по картинкам не удалось: ${error.message}${nameless}`);
    });
    return pics.busy;
  }

  // Пометка в строке таблицы: миниатюра плитки и «по картинке».
  function rowNote(item) {
    const tiles = (item.tiles || []).map((ref) => tileById(ref.id)).filter((t) => t && (t.via === "pic" || t.via === "user"));
    if (!tiles.length) return null;
    const box = el("span", "pic-note");
    if (tiles[0].thumb) {
      const img = el("img", "tile-thumb small");
      img.src = tiles[0].thumb;
      img.alt = "плитка со скриншота";
      box.append(img);
    }
    const byPicture = tiles.filter((t) => t.via === "pic");
    if (byPicture.length) {
      const score = Math.min(...byPicture.map((t) => (t.top[0] ? t.top[0].score : 0)));
      const badge = el("span", "badge pic-badge", "по картинке");
      badge.title = `Узнано по картинке плитки на скриншоте (похожесть ${pct(score)}). Если неверно — выберите другой вариант или уберите строку.`;
      box.append(badge);
    }
    return box;
  }

  // Пользователь выбрал другой вариант в строке (Cowboy — нож, а не ружьё); строка item уже
  // этот предмет, oldKey — что было. Плитки строки теперь этот предмет. Как он выглядит в игре,
  // запоминается, только если плитка в строке одна: в строке из двух плиток (ружьё и нож с одним
  // названием) одна из картинок — другой предмет. Если у выбранного предмета уже есть строка
  // (узнан по картинке на другом скриншоте), строки сливаются: одна стопка не считается дважды.
  function onVariant(item, oldKey) {
    const newKey = item.key;
    if (!oldKey || !newKey || oldKey === newKey) return;
    const mine = new Set((item.tiles || []).map((ref) => ref.id));
    const names = new Set(pics.tiles.filter((t) => mine.has(t.id)).map((t) => t.name));
    for (const t of pics.tiles) {
      if (t.key !== oldKey) continue;
      if (mine.has(t.id)) {
        setKey(t, newKey, "user");
        if (mine.size === 1) teach(t, newKey);
      } else if (t.name && names.has(t.name) && !t.sure) {
        setKey(t, newKey, "user");  // та же плитка на другом скриншоте
      }
    }
    pics.shown.delete(oldKey);
    pics.shown.add(newKey);
    if (calc.items.filter((row) => row.key === newKey).length > 1) {
      mergeRows(newKey, item);
      updateRow(newKey);
      updateFound();
    }
  }

  return {
    prefetch, reset, start, rowNote, onVariant,
    idle: () => pics.busy,
    _state: { pics, refs, fetchTemplates },  // для проверок
  };
})();
