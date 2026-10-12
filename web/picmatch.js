/* picmatch.js — плитки инвентаря MM2 на скриншоте и распознавание предмета по картинке.
 *
 * Чистые функции над RGBA-буферами ({width, height, data: Uint8ClampedArray}):
 * работают и в браузере (ImageData из canvas), и в Node (тесты).
 *
 *   const tiles = PicMatch.detectTiles(imageData);            // плитки, баннеры, значки xN
 *   const t = PicMatch.tileFromBox(imageData, box, badgeBox);  // плитка по месту из распознавания текста
 *   const f = PicMatch.tileFeature(imageData, tiles[i]);      // признаки картинки на плитке
 *   const r = PicMatch.refFeature(refImageData);              // признаки эталона (прозрачный фон)
 *   const s = PicMatch.similarity(f, r);                      // 0..1
 *   const top = PicMatch.rank(f, refs, 5);                    // [{key, score, parts}]
 *   PicMatch.sureKey(top)                                     // ключ, если картинка уверенно одна
 *
 * Пороги проверены на синтетических сетках из настоящих плиток (верная картинка обычно
 * 0.86..0.99, лучшая неверная не выше ~0.66); ниже порогов решает пользователь.
 */
(function (root, factory) {
  if (typeof module === "object" && module.exports) module.exports = factory();
  else root.PicMatch = factory();
})(typeof self !== "undefined" ? self : this, function () {
  "use strict";

  const FEAT_VERSION = 1;
  const N = 20;               // сетка силуэта и цвета (N x N)
  const HIST_BINS = 16;       // 12 оттенков + 4 уровня серого
  const GAME_BG = 54;         // фон плитки в игре (#363636 — середина градиента #393939..#303030)

  // ---------------------------------------------------------------- пиксели

  const SRGB_LIN = new Float32Array(256);
  for (let i = 0; i < 256; i++) {
    const c = i / 255;
    SRGB_LIN[i] = c <= 0.04045 ? c / 12.92 : Math.pow((c + 0.055) / 1.055, 2.4);
  }

  // sRGB -> OKLab (L 0..1, a/b примерно -0.4..0.4)
  function oklab(r, g, b) {
    const R = SRGB_LIN[r], G = SRGB_LIN[g], B = SRGB_LIN[b];
    const l = Math.cbrt(0.4122214708 * R + 0.5363325363 * G + 0.0514459929 * B);
    const m = Math.cbrt(0.2119034982 * R + 0.6806995451 * G + 0.1073969566 * B);
    const s = Math.cbrt(0.0883024619 * R + 0.2817188376 * G + 0.6299787005 * B);
    return [
      0.2104542553 * l + 0.7936177850 * m - 0.0040720468 * s,
      1.9779984951 * l - 2.4285922050 * m + 0.4505937099 * s,
      0.0259040371 * l + 0.7827717662 * m - 0.8086757660 * s,
    ];
  }

  function hsv(r, g, b) {
    const mx = Math.max(r, g, b), mn = Math.min(r, g, b), d = mx - mn;
    let h = 0;
    if (d) {
      if (mx === r) h = ((g - b) / d + 6) % 6;
      else if (mx === g) h = (b - r) / d + 2;
      else h = (r - g) / d + 4;
    }
    return [h * 60, mx ? d / mx : 0, mx / 255];
  }

  // Видно ли пиксель на сером фоне bg: 0 (фон) .. 1 (точно предмет).
  function visible(r, g, b, bg, dT = 7, cT = 9) {
    const d = Math.max(Math.abs(r - bg), Math.abs(g - bg), Math.abs(b - bg));
    const c = Math.max(r, g, b) - Math.min(r, g, b);
    const v = Math.max((d - dT) / 12, (c - cT) / 12);
    return v <= 0 ? 0 : v >= 1 ? 1 : v;
  }

  // ---------------------------------------------------------------- связные области

  // Связные области маски (8-связность) через отрезки строк + union-find.
  function components(mask, W, H, x0 = 0, y0 = 0, x1 = W - 1, y1 = H - 1) {
    const runs = [];  // плоско: y, xs, xe
    const parent = [];
    const find = (i) => {
      while (parent[i] !== i) { parent[i] = parent[parent[i]]; i = parent[i]; }
      return i;
    };
    let prevStart = 0, prevEnd = 0;
    for (let y = y0; y <= y1; y++) {
      const rowStart = parent.length;
      let x = x0;
      const o = y * W;
      while (x <= x1) {
        while (x <= x1 && !mask[o + x]) x++;
        if (x > x1) break;
        const s = x;
        while (x <= x1 && mask[o + x]) x++;
        const id = parent.length;
        parent.push(id);
        runs.push(y, s, x - 1);
        for (let k = prevStart; k < prevEnd; k++) {
          const ks = runs[k * 3 + 1], ke = runs[k * 3 + 2];
          if (ke < s - 1) continue;
          if (ks > x) break;
          const a = find(id), b = find(k);
          if (a !== b) parent[Math.max(a, b)] = Math.min(a, b);
        }
      }
      prevStart = rowStart;
      prevEnd = parent.length;
    }
    const byRoot = new Map();
    for (let i = 0; i < parent.length; i++) {
      const root = find(i);
      const y = runs[i * 3], s = runs[i * 3 + 1], e = runs[i * 3 + 2];
      let c = byRoot.get(root);
      if (!c) byRoot.set(root, (c = { x0: s, x1: e, y0: y, y1: y, count: 0 }));
      if (s < c.x0) c.x0 = s;
      if (e > c.x1) c.x1 = e;
      if (y < c.y0) c.y0 = y;
      if (y > c.y1) c.y1 = y;
      c.count += e - s + 1;
    }
    return [...byRoot.values()];
  }

  // ---------------------------------------------------------------- 1. плитки

  // Пиксель баннера: цветной (редкость) или светлый (серый баннер Common, белый текст названия).
  function bannerish(r, g, b) {
    const mx = Math.max(r, g, b), mn = Math.min(r, g, b);
    return (mx >= 70 && mx - mn >= 0.35 * mx) || mn >= 0x58;
  }
  const isDark = (r, g, b) => r <= 45 && g <= 45 && b <= 45;
  // Фон плитки: нейтральный тёмно-серый градиент (#393939 сверху .. #303030 снизу).
  const isTileBg = (r, g, b) => Math.max(r, g, b) - Math.min(r, g, b) <= 10 && r >= 0x22 && r <= 0x50;

  function rowFrac(img, y, x0, x1, test) {
    if (y < 0 || y >= img.height) return 0;
    const d = img.data;
    let n = 0;
    for (let x = x0, o = (y * img.width + x0) * 4; x <= x1; x++, o += 4) if (test(d[o], d[o + 1], d[o + 2])) n++;
    return n / (x1 - x0 + 1);
  }

  function colFrac(img, x, y0, y1, test) {
    if (x < 0 || x >= img.width) return 0;
    const d = img.data, W = img.width;
    let n = 0;
    for (let y = y0; y <= y1; y++) {
      const o = (y * W + x) * 4;
      if (test(d[o], d[o + 1], d[o + 2])) n++;
    }
    return n / Math.max(1, y1 - y0 + 1);
  }

  function maskFrac(mask, W, y, x0, x1) {
    let n = 0;
    for (let x = x0; x <= x1; x++) n += mask[y * W + x];
    return n / (x1 - x0 + 1);
  }

  function maskColFrac(mask, W, x, y0, y1) {
    let n = 0;
    for (let y = y0; y <= y1; y++) n += mask[y * W + x];
    return n / (y1 - y0 + 1);
  }

  function median(values) {
    if (!values.length) return NaN;
    const s = Float64Array.from(values).sort();
    return s[s.length >> 1];
  }

  // Цвет баннера: средний цвет верхней полосы (над текстом), оттенок, разброс оттенка (радуга = Chroma).
  function bannerColor(img, b) {
    const d = img.data, W = img.width;
    const yA = b.y0 + 1, yB = b.y0 + Math.max(1, Math.round((b.y1 - b.y0) * 0.2));
    let sr = 0, sg = 0, sb = 0, n = 0;
    const hues = [];
    for (let x = b.x0 + 1; x < b.x1; x++) {
      let cr = 0, cg = 0, cb = 0, cn = 0;
      for (let y = yA; y <= yB; y++) {
        const o = (y * W + x) * 4;
        cr += d[o]; cg += d[o + 1]; cb += d[o + 2]; cn++;
      }
      if (!cn) continue;
      sr += cr; sg += cg; sb += cb; n += cn;
      const [h, s] = hsv(cr / cn, cg / cn, cb / cn);
      if (s >= 0.25) hues.push(h);
    }
    const rgb = n ? [sr / n, sg / n, sb / n].map(Math.round) : [0, 0, 0];
    const [h, s, v] = hsv(...rgb);
    // круговой разброс оттенка по ширине баннера
    let cx = 0, cy = 0;
    for (const hh of hues) { cx += Math.cos(hh * Math.PI / 180); cy += Math.sin(hh * Math.PI / 180); }
    const R = hues.length ? Math.hypot(cx, cy) / hues.length : 1;
    const spread = Math.sqrt(Math.max(0, -2 * Math.log(Math.max(R, 1e-6)))) * 180 / Math.PI;
    return { rgb, hue: Math.round(h), sat: +s.toFixed(2), val: +v.toFixed(2), spread: Math.round(spread) };
  }

  // Значок количества («x40») в правом верхнем углу: белые символы одной высоты на одной линии.
  // Порог яркости — от самого яркого нейтрального пикселя в углу: на мелком скриншоте
  // тонкие белые штрихи смешиваются с серым фоном и уже не белые.
  function findBadge(img, t) {
    const W = img.width, d = img.data;
    const w = t.x1 - t.x0 + 1;
    const rx0 = t.x0 + Math.round(w * 0.4), rx1 = t.x1 - 1;
    const ry0 = t.iconTop + 1, ry1 = Math.min(t.iconBottom, t.iconTop + Math.round(w * 0.36));
    const lum = (o) => (d[o] + d[o + 1] + d[o + 2]) / 3;
    const neutral = (o) => Math.max(d[o], d[o + 1], d[o + 2]) - Math.min(d[o], d[o + 1], d[o + 2]) <= 30;
    let peak = 0;
    for (let y = ry0; y <= ry1; y++) {
      for (let x = rx0; x <= rx1; x++) {
        const o = (y * W + x) * 4;
        if (neutral(o) && lum(o) > peak) peak = lum(o);
      }
    }
    if (peak < 140) return null;
    const bg = 0x39;
    const thr = bg + Math.max(50, 0.55 * (peak - bg));
    // маска только угла плитки (координаты внутри угла -> обратно в координаты скриншота)
    const mw = rx1 - rx0 + 1, mh = ry1 - ry0 + 1;
    const mask = new Uint8Array(mw * mh);
    for (let y = ry0; y <= ry1; y++) {
      for (let x = rx0; x <= rx1; x++) {
        const o = (y * W + x) * 4;
        mask[(y - ry0) * mw + (x - rx0)] = neutral(o) && lum(o) >= thr ? 1 : 0;
      }
    }
    const glyphs = components(mask, mw, mh).map((c) => ({
      x0: c.x0 + rx0, x1: c.x1 + rx0, y0: c.y0 + ry0, y1: c.y1 + ry0, count: c.count,
    })).filter((c) => {
      const gh = c.y1 - c.y0 + 1, gw = c.x1 - c.x0 + 1;
      return gh >= Math.max(2, w * 0.05) && gh <= w * 0.2 && gw <= w * 0.5;
    });
    if (!glyphs.length) return null;
    const bottom = median(glyphs.map((c) => c.y1));
    const line = glyphs.filter((c) => Math.abs(c.y1 - bottom) <= Math.max(1.5, w * 0.03)).sort((a, b) => a.x0 - b.x0);
    // символы идут подряд, без больших промежутков
    for (let i = 1; i < line.length; i++) if (line[i].x0 - line[i - 1].x1 > w * 0.08) return null;
    const box = {
      x0: line[0].x0, x1: line[line.length - 1].x1,
      y0: Math.min(...line.map((c) => c.y0)), y1: Math.max(...line.map((c) => c.y1)),
    };
    const bw = box.x1 - box.x0 + 1, bh = box.y1 - box.y0 + 1;
    // «x» и хотя бы одна цифра: несколько символов или один слившийся широкий
    if (line.length < 2 && bw < bh * 1.5) return null;
    if (bw > w * 0.5 || box.x1 < t.x1 - w * 0.2 || box.y0 > t.iconTop + w * 0.25) return null;
    box.glyphs = line.length;
    return box;
  }

  /**
   * Плитки инвентаря MM2: цветной (или светлый) баннер с названием внизу, над ним
   * чёрная черта и квадрат с картинкой на сером градиенте.
   * Возвращает [{x0, x1, iconTop, iconBottom, banner:{x0,y0,x1,y1}, color, badge, partial}]
   */
  function detectTiles(img) {
    const W = img.width, H = img.height, d = img.data;
    const mask = new Uint8Array(W * H);
    for (let i = 0, o = 0; i < mask.length; i++, o += 4) mask[i] = bannerish(d[o], d[o + 1], d[o + 2]) ? 1 : 0;
    const found = [];
    for (const c of components(mask, W, H)) {
      const w = c.x1 - c.x0 + 1, h = c.y1 - c.y0 + 1;
      if (w < 16 || h < 5) continue;
      const ratio = w / h;
      if (ratio < 2.4 || ratio > 8) continue;
      if (c.count / (w * h) < 0.5) continue;
      // ровные края: верх, низ и бока баннера сплошные (текст их не касается)
      const ys = [c.y0, c.y0 + 1], ye = [c.y1, c.y1 - 1], xs = [c.x0, c.x0 + 1], xe = [c.x1, c.x1 - 1];
      const edge = (rows, f) => Math.max(...rows.map(f));
      if (edge(ys, (y) => maskFrac(mask, W, y, c.x0, c.x1)) < 0.85) continue;
      if (edge(ye, (y) => maskFrac(mask, W, y, c.x0, c.x1)) < 0.85) continue;
      if (edge(xs, (x) => maskColFrac(mask, W, x, c.y0, c.y1)) < 0.8) continue;
      if (edge(xe, (x) => maskColFrac(mask, W, x, c.y0, c.y1)) < 0.8) continue;
      // баннер в чёрной рамке: тёмные черты слева, справа и снизу (или край скриншота)
      const gap = Math.max(2, Math.round(h * 0.2));
      const darkCol = (x) => colFrac(img, x, c.y0, c.y1, isDark) >= 0.8;
      const darkRow = (y) => rowFrac(img, y, c.x0, c.x1, isDark) >= 0.8;
      let framed = true;
      for (const [edgeHit, probe] of [
        [c.x0 <= 1, (k) => darkCol(c.x0 - k)],
        [c.x1 >= W - 2, (k) => darkCol(c.x1 + k)],
        [c.y1 >= H - 2, (k) => darkRow(c.y1 + k)],
      ]) {
        if (edgeHit) continue;
        let ok = false;
        for (let k = 1; k <= gap && !ok; k++) ok = probe(k);
        if (!ok) { framed = false; break; }
      }
      if (!framed) continue;
      // над баннером — тёмная черта
      let yb = -1;
      for (let dy = 1; dy <= gap; dy++) {
        if (rowFrac(img, c.y0 - dy, c.x0, c.x1, isDark) >= 0.8) { yb = c.y0 - dy; break; }
      }
      if (yb < 0) continue;
      while (yb > 0 && rowFrac(img, yb - 1, c.x0, c.x1, isDark) >= 0.8) yb--;
      const iconBottom = yb - 1;
      // верх квадрата с картинкой — следующая тёмная черта (рамка плитки)
      const expect = Math.round(w * 1.05);
      let iconTop = -1;
      for (let y = iconBottom - Math.round(w * 0.6); y >= Math.max(0, iconBottom - Math.round(w * 1.4)); y--) {
        if (rowFrac(img, y, c.x0, c.x1, isDark) >= 0.8) { iconTop = y + 1; break; }
      }
      let partialTop = false;
      if (iconTop < 0) {
        iconTop = iconBottom - expect + 1;
        if (iconTop < 0) { partialTop = true; iconTop = 0; }
      }
      if (iconBottom - iconTop < w * 0.4) continue;
      // по бокам квадрата — серый фон плитки
      const inset = Math.max(1, Math.round(w * 0.01));
      const side = Math.max(
        colFrac(img, c.x0 + inset, iconTop, iconBottom, isTileBg),
        colFrac(img, c.x1 - inset, iconTop, iconBottom, isTileBg),
      );
      if (side < 0.5) continue;
      const tile = {
        x0: c.x0, x1: c.x1, iconTop, iconBottom,
        banner: { x0: c.x0, y0: c.y0, x1: c.x1, y1: c.y1 },
        partial: partialTop || c.x0 <= 1 || c.x1 >= W - 2 || c.y1 >= H - 2,
      };
      tile.color = bannerColor(img, tile.banner);
      tile.badge = findBadge(img, tile);
      found.push(tile);
    }
    // Все плитки одного скриншота одинаковой ширины: случайные «баннеры» другой ширины отбрасываются.
    const full = found.filter((t) => !t.partial);
    const wMed = median(full.map((t) => t.x1 - t.x0 + 1));
    const tiles = found.filter((t) => {
      const w = t.x1 - t.x0 + 1;
      if (Number.isNaN(wMed)) return true;
      return t.partial ? w <= wMed * 1.15 : Math.abs(w - wMed) <= wMed * 0.12;
    });
    tiles.sort((a, b) => (a.banner.y0 - b.banner.y0) || (a.x0 - b.x0));
    // строки сетки: баннеры с близким y — одна строка
    let row = -1, lastY = -1e9;
    for (const t of tiles) {
      if (t.banner.y0 - lastY > (t.banner.y1 - t.banner.y0)) row++;
      lastY = t.banner.y0;
      t.row = row;
    }
    return tiles;
  }

  // ---------------------------------------------------------------- 2. признаки картинки

  // Квадратная область вокруг предмета -> сетка N x N: покрытие и средний цвет (OKLab).
  function gridFeature(src) {
    // src: {w, h, get(x, y) -> [r, g, b, weight], hidden?: {x0, y0, x1, y1}} — hidden: под значком xN
    // 1) рамка предмета по весам (с отсечением 1% массы с каждой стороны — пылинки и шум)
    const colSum = new Float64Array(src.w), rowSum = new Float64Array(src.h);
    const px = new Float32Array(src.w * src.h * 4);
    let total = 0;
    for (let y = 0; y < src.h; y++) {
      for (let x = 0; x < src.w; x++) {
        const p = src.get(x, y);
        const i = (y * src.w + x) * 4;
        px[i] = p[0]; px[i + 1] = p[1]; px[i + 2] = p[2]; px[i + 3] = p[3];
        colSum[x] += p[3]; rowSum[y] += p[3]; total += p[3];
      }
    }
    if (total < 4) return null;
    const trim = (sums) => {
      let a = 0, b = sums.length - 1, acc = 0;
      while (a < b && acc + sums[a] <= total * 0.01) acc += sums[a++];
      acc = 0;
      while (b > a && acc + sums[b] <= total * 0.01) acc += sums[b--];
      return [a, b];
    };
    const [bx0, bx1] = trim(colSum), [by0, by1] = trim(rowSum);
    const side = Math.max(bx1 - bx0 + 1, by1 - by0 + 1);
    const cx = (bx0 + bx1 + 1) / 2, cy = (by0 + by1 + 1) / 2;
    const sx = cx - side / 2, sy = cy - side / 2, cell = side / N;
    // 2) сетка: усреднение по площади (крупный источник) или билинейная выборка (мелкий)
    const cov = new Float32Array(N * N), lab = new Float32Array(N * N * 3);
    const hist = new Float32Array(HIST_BINS);
    const sample = (fx, fy) => {
      const x = Math.floor(fx - 0.5), y = Math.floor(fy - 0.5), tx = fx - 0.5 - x, ty = fy - 0.5 - y;
      const out = [0, 0, 0, 0];
      for (const [dx, dy, k] of [[0, 0, (1 - tx) * (1 - ty)], [1, 0, tx * (1 - ty)], [0, 1, (1 - tx) * ty], [1, 1, tx * ty]]) {
        const xx = x + dx, yy = y + dy;
        if (xx < 0 || yy < 0 || xx >= src.w || yy >= src.h || !k) continue;
        const i = (yy * src.w + xx) * 4, w = px[i + 3] * k;
        out[0] += px[i] * w; out[1] += px[i + 1] * w; out[2] += px[i + 2] * w; out[3] += w;
      }
      return out;
    };
    const sub = cell >= 3 ? Math.min(8, Math.round(cell)) : 3;
    for (let gy = 0; gy < N; gy++) {
      for (let gx = 0; gx < N; gx++) {
        let r = 0, g = 0, b = 0, w = 0;
        for (let j = 0; j < sub; j++) {
          for (let i = 0; i < sub; i++) {
            const s = sample(sx + (gx + (i + 0.5) / sub) * cell, sy + (gy + (j + 0.5) / sub) * cell);
            r += s[0]; g += s[1]; b += s[2]; w += s[3];
          }
        }
        const k = gy * N + gx;
        cov[k] = w / (sub * sub);
        if (w > 1e-6) {
          const L = oklab(Math.round(r / w), Math.round(g / w), Math.round(b / w));
          lab[k * 3] = L[0]; lab[k * 3 + 1] = L[1]; lab[k * 3 + 2] = L[2];
          // гистограмма цветов предмета (не зависит от положения)
          const C = Math.hypot(L[1], L[2]);
          let bin;
          if (C < 0.035) bin = 12 + Math.min(3, Math.floor(L[0] / 0.25));
          else bin = Math.floor(((Math.atan2(L[2], L[1]) + Math.PI) / (2 * Math.PI)) * 12) % 12;
          hist[bin] += cov[k];
        }
      }
    }
    const hs = hist.reduce((a, b) => a + b, 0) || 1;
    for (let i = 0; i < HIST_BINS; i++) hist[i] /= hs;
    // клетки, закрытые значком: при сравнении не учитываются ни у плитки, ни у эталона
    let ignore = null;
    if (src.hidden) {
      ignore = new Uint8Array(N * N);
      const h = src.hidden;
      for (let gy = 0; gy < N; gy++) {
        for (let gx = 0; gx < N; gx++) {
          const cx0 = sx + gx * cell, cy0 = sy + gy * cell;
          if (cx0 + cell > h.x0 && cx0 < h.x1 + 1 && cy0 + cell > h.y0 && cy0 < h.y1 + 1) ignore[gy * N + gx] = 1;
        }
      }
    }
    return { v: FEAT_VERSION, cov, lab, hist, aspect: (bx1 - bx0 + 1) / (by1 - by0 + 1), ignore };
  }

  // Квадрат с картинкой, сглаженный окном 3x3: шум сжатия (JPEG, Discord) не принимается за предмет.
  function smoothCrop(img, x0, y0, x1, y1) {
    const W = img.width, d = img.data, w = x1 - x0 + 1, h = y1 - y0 + 1;
    const out = new Float32Array(w * h * 3);
    for (let y = 0; y < h; y++) {
      for (let x = 0; x < w; x++) {
        let r = 0, g = 0, b = 0, n = 0;
        for (let dy = -1; dy <= 1; dy++) {
          const Y = y0 + y + dy;
          if (Y < y0 || Y > y1) continue;
          for (let dx = -1; dx <= 1; dx++) {
            const X = x0 + x + dx;
            if (X < x0 || X > x1) continue;
            const o = (Y * W + X) * 4;
            r += d[o]; g += d[o + 1]; b += d[o + 2]; n++;
          }
        }
        const i = (y * w + x) * 3;
        out[i] = r / n; out[i + 1] = g / n; out[i + 2] = b / n;
      }
    }
    return out;
  }

  // Фон плитки по строкам (медиана нейтральных пикселей у левого и правого края квадрата)
  // и уровень шума на этом фоне.
  function rowBackground(sm, w, h) {
    const strip = Math.max(2, Math.round(w * 0.06));
    const prof = new Float64Array(h).fill(NaN);
    const at = (x, y) => (y * w + x) * 3;
    for (let y = 0; y < h; y++) {
      const vals = [];
      for (const [a, b] of [[0, strip - 1], [w - strip, w - 1]]) {
        for (let x = a; x <= b; x++) {
          const i = at(x, y), r = sm[i], g = sm[i + 1], bl = sm[i + 2];
          if (Math.max(r, g, bl) - Math.min(r, g, bl) <= 8 && r >= 0x22 && r <= 0x50) vals.push((r + g + bl) / 3);
        }
      }
      if (vals.length >= 2) prof[y] = median(vals);
    }
    // пропуски (предмет касается краёв) — по соседним строкам; затем сглаживание
    let last = NaN;
    for (let i = 0; i < h; i++) if (Number.isNaN(prof[i])) prof[i] = last; else last = prof[i];
    last = NaN;
    for (let i = h - 1; i >= 0; i--) if (Number.isNaN(prof[i])) prof[i] = last; else last = prof[i];
    const bg = new Float64Array(h);
    for (let i = 0; i < h; i++) {
      const win = [];
      for (let k = Math.max(0, i - 3); k <= Math.min(h - 1, i + 3); k++) if (!Number.isNaN(prof[k])) win.push(prof[k]);
      bg[i] = win.length ? median(win) : GAME_BG;
    }
    // шум: типичное отклонение сглаженного фона у краёв от профиля
    const dev = [], chroma = [];
    for (let y = 0; y < h; y += 2) {
      for (const x of [1, w - 2]) {
        const i = at(x, y), r = sm[i], g = sm[i + 1], bl = sm[i + 2];
        const c = Math.max(r, g, bl) - Math.min(r, g, bl);
        const dd = Math.max(Math.abs(r - bg[y]), Math.abs(g - bg[y]), Math.abs(bl - bg[y]));
        if (dd < 20 && c < 20) { dev.push(dd); chroma.push(c); }
      }
    }
    return { bg, noise: dev.length ? median(dev) : 0, noiseC: chroma.length ? median(chroma) : 0 };
  }

  // Цвет баннера по цвету [r, g, b], который нашло распознавание текста (у плиток, найденных
  // не по картинке): без разброса оттенка.
  function colorFromRgb(rgb) {
    if (!Array.isArray(rgb) || rgb.length !== 3) return null;
    const c = rgb.map((v) => Math.max(0, Math.min(255, Math.round(Number(v) || 0))));
    const [h, s, v] = hsv(...c);
    return { rgb: c, hue: Math.round(h), sat: +s.toFixed(2), val: +v.toFixed(2), spread: 0 };
  }

  /**
   * Плитка по месту, которое нашло распознавание текста: box = [x, y, W, H] (верх квадрата
   * с картинкой, ширина плитки), badgeBox = [x, y, w, h] значка xN или null. Место
   * приблизительное, поэтому края квадрата отрезаются шире (inset). null — плитка за краем.
   */
  function tileFromBox(img, box, badgeBox, bg) {
    if (!Array.isArray(box) || box.length < 3) return null;
    const [x, y, w] = box.map(Number);
    if (!(w > 0)) return null;
    const t = {
      x0: Math.round(x), x1: Math.round(x + w) - 1,
      iconTop: Math.round(y), iconBottom: Math.round(y + 1.04 * w) - 1,
      inset: 0.05, approx: true, partial: false,
    };
    if (t.x0 < 0 || t.iconTop < 0 || t.x1 >= img.width || t.iconBottom >= img.height) return null;
    t.banner = { x0: t.x0, x1: t.x1, y0: Math.round(y + 1.08 * w), y1: Math.min(img.height - 1, Math.round(y + 1.31 * w) - 1) };
    // значок, прочитанный вторым проходом, приходит без места — ищется в углу плитки
    t.badge = Array.isArray(badgeBox) && badgeBox.length === 4
      ? { x0: badgeBox[0], y0: badgeBox[1], x1: badgeBox[0] + badgeBox[2] - 1, y1: badgeBox[1] + badgeBox[3] - 1 }
      : findBadge(img, t);
    t.color = colorFromRgb(bg);
    return t;
  }

  // Признаки картинки на плитке: фон (серый градиент) и значок xN убираются.
  function tileFeature(img, t) {
    const W = img.width, d = img.data;
    const w = t.x1 - t.x0 + 1;
    const m = Math.max(1, Math.round(w * (t.inset || 0.012)));
    const x0 = t.x0 + m, x1 = t.x1 - m, y0 = t.iconTop + m, y1 = t.iconBottom - m;
    const cw = x1 - x0 + 1, ch = y1 - y0 + 1;
    const sm = smoothCrop(img, x0, y0, x1, y1);
    const { bg, noise, noiseC } = rowBackground(sm, cw, ch);
    const dT = 7 + 2 * noise, cT = 9 + 2 * noiseC;
    const pad = Math.max(1, Math.round(w * 0.02));
    const badge = t.badge;
    return gridFeature({
      w: cw,
      h: ch,
      hidden: badge ? { x0: badge.x0 - pad - x0, y0: badge.y0 - pad - y0, x1: badge.x1 + pad - x0, y1: badge.y1 + pad - y0 } : null,
      get(x, y) {
        const X = x0 + x, Y = y0 + y, o = (Y * W + X) * 4;
        const r = d[o], g = d[o + 1], b = d[o + 2];
        if (badge && X >= badge.x0 - pad && X <= badge.x1 + pad && Y >= badge.y0 - pad && Y <= badge.y1 + pad) {
          return [r, g, b, 0];
        }
        const i = (y * cw + x) * 3;
        return [r, g, b, visible(sm[i], sm[i + 1], sm[i + 2], bg[y], dT, cT)];
      },
    });
  }

  // Признаки эталона (картинка с сайта/StarPets): прозрачный фон; если фон непрозрачный —
  // он отделяется заливкой от краёв. Картинка «кладётся» на серый фон игры, чтобы тёмные
  // части, невидимые в игре, не учитывались и тут.
  function refFeature(img) {
    const W = img.width, H = img.height, d = img.data;
    let transparent = 0;
    for (let i = 3; i < d.length; i += 4) if (d[i] < 128) transparent++;
    let bgMask = null;
    if (transparent < W * H * 0.02) bgMask = floodBackground(img);
    return gridFeature({
      w: W,
      h: H,
      get(x, y) {
        const o = (y * W + x) * 4;
        const a = bgMask && bgMask[y * W + x] ? 0 : d[o + 3] / 255;
        const r = Math.round(d[o] * a + GAME_BG * (1 - a));
        const g = Math.round(d[o + 1] * a + GAME_BG * (1 - a));
        const b = Math.round(d[o + 2] * a + GAME_BG * (1 - a));
        return [r, g, b, a < 0.05 ? 0 : visible(r, g, b, GAME_BG)];
      },
    });
  }

  function floodBackground(img) {
    const W = img.width, H = img.height, d = img.data;
    const bg = new Uint8Array(W * H);
    const border = [];
    for (let x = 0; x < W; x++) border.push(x, (H - 1) * W + x);
    for (let y = 0; y < H; y++) border.push(y * W, y * W + W - 1);
    const ref = [0, 1, 2].map((c) => median(border.map((i) => d[i * 4 + c])));
    const close = (i) => Math.max(Math.abs(d[i * 4] - ref[0]), Math.abs(d[i * 4 + 1] - ref[1]), Math.abs(d[i * 4 + 2] - ref[2])) <= 24;
    const stack = border.filter(close);
    for (const i of stack) bg[i] = 1;
    while (stack.length) {
      const i = stack.pop(), x = i % W, y = (i / W) | 0;
      for (const j of [x > 0 ? i - 1 : -1, x < W - 1 ? i + 1 : -1, y > 0 ? i - W : -1, y < H - 1 ? i + W : -1]) {
        if (j >= 0 && !bg[j] && close(j)) { bg[j] = 1; stack.push(j); }
      }
    }
    return bg;
  }

  // ---------------------------------------------------------------- 3. сравнение

  const SIGMA = 0.11;  // разница цвета в OKLab, при которой похожесть клетки ~0.6

  function similarity(a, b) {
    if (!a || !b) return { score: 0 };
    let best = { score: -1 };
    for (let dy = -1; dy <= 1; dy++) {
      for (let dx = -1; dx <= 1; dx++) {
        let inter = 0, union = 0, cw = 0, cs = 0;
        for (let y = 0; y < N; y++) {
          const yb = y + dy;
          for (let x = 0; x < N; x++) {
            const xb = x + dx;
            const ka = y * N + x;
            if (a.ignore && a.ignore[ka]) continue;
            const ca = a.cov[ka];
            const cb = xb >= 0 && yb >= 0 && xb < N && yb < N ? b.cov[yb * N + xb] : 0;
            inter += Math.min(ca, cb);
            union += Math.max(ca, cb);
            const w = Math.min(ca, cb);
            if (w > 0.05) {
              const kb = yb * N + xb;
              const dl = a.lab[ka * 3] - b.lab[kb * 3];
              const da = a.lab[ka * 3 + 1] - b.lab[kb * 3 + 1];
              const db = a.lab[ka * 3 + 2] - b.lab[kb * 3 + 2];
              cs += w * Math.exp(-(dl * dl + da * da + db * db) / (2 * SIGMA * SIGMA));
              cw += w;
            }
          }
        }
        const shape = union ? inter / union : 0;
        const color = cw ? cs / cw : 0;
        const s = 0.45 * shape + 0.55 * color;
        if (s > best.score) best = { score: s, shape, color, dx, dy };
      }
    }
    let hist = 0;
    for (let i = 0; i < HIST_BINS; i++) hist += Math.min(a.hist[i], b.hist[i]);
    const aspect = Math.min(a.aspect, b.aspect) / Math.max(a.aspect, b.aspect);
    const score = 0.8 * best.score + 0.2 * hist;
    return {
      score: +score.toFixed(4), shape: +best.shape.toFixed(3), color: +best.color.toFixed(3),
      hist: +hist.toFixed(3), aspect: +aspect.toFixed(3),
    };
  }

  // Грубая похожесть для отбора кандидатов: гистограмма цветов + силуэт 5x5.
  function coarse(f) {
    if (f._coarse) return f._coarse;
    const g = new Float32Array(25), k = N / 5;
    for (let y = 0; y < N; y++) for (let x = 0; x < N; x++) g[Math.floor(y / k) * 5 + Math.floor(x / k)] += f.cov[y * N + x] / (k * k);
    return (f._coarse = g);
  }

  function quickScore(a, b) {
    const ga = coarse(a), gb = coarse(b);
    let inter = 0, union = 0, hist = 0;
    for (let i = 0; i < 25; i++) { inter += Math.min(ga[i], gb[i]); union += Math.max(ga[i], gb[i]); }
    for (let i = 0; i < HIST_BINS; i++) hist += Math.min(a.hist[i], b.hist[i]);
    return 0.5 * (union ? inter / union : 0) + 0.5 * hist;
  }

  // refs: [{key, feat, source?}] — у предмета может быть несколько эталонов (сайт, StarPets,
  // выученный по скриншоту). filter(ref) — например, только предметы подходящей редкости.
  function rank(feat, refs, k = 5, filter = null, shortlist = 60) {
    if (!feat) return [];
    let pool = filter ? refs.filter(filter) : refs;
    if (pool.length > shortlist) {
      pool = pool.map((ref) => ({ ref, q: quickScore(feat, ref.feat) }))
        .sort((x, y) => y.q - x.q).slice(0, shortlist).map((x) => x.ref);
    }
    const bestByKey = new Map();
    for (const ref of pool) {
      const s = similarity(feat, ref.feat);
      const prev = bestByKey.get(ref.key);
      if (!prev || s.score > prev.score) bestByKey.set(ref.key, { key: ref.key, source: ref.source, ...s });
    }
    return [...bestByKey.values()].sort((x, y) => y.score - x.score).slice(0, k);
  }

  // ---------------------------------------------------------------- 4. решение

  // Пороги по опытам на синтетических сетках из настоящих плиток: верная картинка 0.68..0.99
  // (обычно > 0.86), лучшая неверная <= 0.66. Уверенно — только когда картинка похожа
  // сильно и заметно сильнее следующей; иначе предмет выбирает пользователь.
  const PIC_SURE = 0.80, PIC_MARGIN = 0.08, PIC_AGREE = 0.50;

  // ranked: [{key, score}] по убыванию -> ключ, если первый уверенно лучше остальных, иначе null.
  function sureKey(ranked, sure = PIC_SURE, margin = PIC_MARGIN) {
    const top = ranked && ranked[0];
    if (!top || top.score < sure) return null;
    const second = ranked.find((r) => r.key !== top.key);
    return !second || top.score - second.score >= margin ? top.key : null;
  }

  // Цвет баннера -> корзина: оттенок по 10°, «серый» или «радуга» (разноцветный баннер).
  function bannerBucket(color) {
    if (!color) return null;
    if (color.spread >= 60) return "rainbow";   // разброс считается только по цветным столбцам
    if (color.sat < 0.2) return "grey";
    return String(Math.floor(color.hue / 10) * 10);
  }

  // Цвет полосы с названием = редкость (как calc.banner_categories на сервере).
  const BANNER_HUES = [
    [40, 70, ["vintages"]],          // жёлтый (измерено на скриншоте: #e6c800..#857300)
    [275, 335, ["godlies"]],         // розовый, фиолетовый
    [335, 361, ["legendaries"]],     // красный
    [0, 15, ["legendaries"]],
    [185, 255, ["rares"]],           // синий
    [85, 165, ["uncommons"]],        // зелёный
  ];

  function bannerCategories(rgb) {
    if (!Array.isArray(rgb) || rgb.length !== 3) return [];
    const [r, g, b] = rgb.map((c) => Math.max(0, Math.min(255, Math.round(Number(c) || 0))) / 255);
    const hi = Math.max(r, g, b), lo = Math.min(r, g, b);
    if (hi - lo < 0.12) return hi > 0.55 ? ["commons"] : [];
    let hue;
    if (hi === r) hue = (((60 * ((g - b) / (hi - lo))) % 360) + 360) % 360;
    else if (hi === g) hue = 60 * ((b - r) / (hi - lo)) + 120;
    else hue = 60 * ((r - g) / (hi - lo)) + 240;
    const found = BANNER_HUES.find(([start, end]) => start <= hue && hue < end);
    return found ? found[2].slice() : [];
  }

  /**
   * Какие категории подходят к цвету баннера — мягкая подсказка (предметы других категорий
   * при сравнении чуть ниже, но не исключаются). learned: {корзина: {категория: число}} —
   * выученное по уверенно узнанным плиткам важнее встроенной таблицы.
   */
  function rarityPrior(color, learned) {
    if (!color) return null;
    const bucket = bannerBucket(color);
    const counts = (learned && bucket && learned[bucket]) || {};
    const entries = Object.entries(counts).filter(([, n]) => n > 0).sort((a, b) => b[1] - a[1]);
    const total = entries.reduce((sum, [, n]) => sum + n, 0);
    if (total >= 2 && entries[0][1] / total >= 0.8) return [entries[0][0]];
    if (bucket === "rainbow") return null;
    const categories = bannerCategories(color.rgb);
    return categories.length ? categories : null;
  }

  const PRIOR_PENALTY = 0.92;

  // Подсказка по цвету баннера: предметы других категорий — чуть ниже, порядок пересчитан.
  function applyPrior(ranked, categories) {
    if (!categories || !categories.length) return ranked;
    return ranked.map((r) => (categories.includes(r.key.split(":")[0]) ? r
      : { ...r, score: +(r.score * PRIOR_PENALTY).toFixed(4), prior: false }))
      .sort((a, b) => b.score - a.score);
  }

  /**
   * Плитки нескольких скриншотов -> посчитанные плитки каждого предмета (как calc.merge_shots
   * на сервере, но по ключу предмета). Одинаковые предметы в MM2 лежат одной стопкой: один
   * предмет на двух скриншотах (пересекающихся) — одна плитка, берётся наибольшее количество.
   * Две плитки одного предмета на одном скриншоте остаются двумя.
   * tiles: [{shot, key, qty}] -> Map(ключ -> [плитки])
   */
  function mergeByKey(tiles) {
    const order = [], byShot = new Map();
    for (const t of tiles) {
      if (!t.key) continue;
      if (!byShot.has(t.shot)) { byShot.set(t.shot, new Map()); order.push(t.shot); }
      const here = byShot.get(t.shot);
      if (!here.has(t.key)) here.set(t.key, []);
      here.get(t.key).push(t);
    }
    const qty = (t) => Math.max(1, Number(t.qty) || 1);
    const merged = new Map();
    for (const shot of order) {
      for (const [key, group] of byShot.get(shot)) {
        group.sort((a, b) => qty(b) - qty(a));
        const have = merged.get(key) || [];
        group.forEach((t, i) => {
          if (i >= have.length) have.push(t);
          else if (qty(t) > qty(have[i])) have[i] = t;
        });
        merged.set(key, have);
      }
    }
    return merged;
  }

  // ---------------------------------------------------------------- 4. хранение

  // Признаки -> компактная строка base64 (N*N покрытие + N*N*3 цвет + 16 гистограмма + аспект).
  function pack(f) {
    const bytes = new Uint8Array(N * N * 4 + HIST_BINS + 1);
    let o = 0;
    for (let i = 0; i < N * N; i++) bytes[o++] = Math.round(Math.max(0, Math.min(1, f.cov[i])) * 255);
    for (let i = 0; i < N * N; i++) {
      bytes[o++] = Math.round(Math.max(0, Math.min(1, f.lab[i * 3])) * 255);
      bytes[o++] = Math.round(Math.max(-0.5, Math.min(0.5, f.lab[i * 3 + 1])) * 255 + 127.5);
      bytes[o++] = Math.round(Math.max(-0.5, Math.min(0.5, f.lab[i * 3 + 2])) * 255 + 127.5);
    }
    for (let i = 0; i < HIST_BINS; i++) bytes[o++] = Math.round(f.hist[i] * 255);
    bytes[o++] = Math.round(Math.max(0.2, Math.min(5, f.aspect)) * 50);
    let s = "";
    for (let i = 0; i < bytes.length; i++) s += String.fromCharCode(bytes[i]);
    return typeof btoa === "function" ? btoa(s) : Buffer.from(bytes).toString("base64");
  }

  function unpack(text) {
    const bin = typeof atob === "function" ? atob(text) : Buffer.from(text, "base64").toString("binary");
    const bytes = Uint8Array.from(bin, (ch) => ch.charCodeAt(0));
    if (bytes.length !== N * N * 4 + HIST_BINS + 1) return null;
    const cov = new Float32Array(N * N), lab = new Float32Array(N * N * 3), hist = new Float32Array(HIST_BINS);
    let o = 0;
    for (let i = 0; i < N * N; i++) cov[i] = bytes[o++] / 255;
    for (let i = 0; i < N * N; i++) {
      lab[i * 3] = bytes[o++] / 255;
      lab[i * 3 + 1] = (bytes[o++] - 127.5) / 255;
      lab[i * 3 + 2] = (bytes[o++] - 127.5) / 255;
    }
    for (let i = 0; i < HIST_BINS; i++) hist[i] = bytes[o++] / 255;
    return { v: FEAT_VERSION, cov, lab, hist, aspect: bytes[o] / 50 };
  }

  return {
    FEAT_VERSION, N, detectTiles, tileFromBox, tileFeature, refFeature, similarity, rank, pack, unpack,
    sureKey, mergeByKey, rarityPrior, applyPrior, bannerBucket, bannerCategories, colorFromRgb,
    PIC_SURE, PIC_MARGIN, PIC_AGREE, bannerColor, findBadge, oklab, hsv, components,
  };
});
