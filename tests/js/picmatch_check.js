// Проверка web/picmatch.js в Node (запускает tests/test_picmatch.py).
// node picmatch_check.js job.json -> JSON в stdout.
// job: {"images": [{"name", "file", "boxes"?}], "refs": {"ключ": ["файл", ...]}, "sure"?, "prior"?, "merge"?}
// Файлы — в сыром формате picgen.rgba_file (ширина, высота uint32 LE и RGBA).
"use strict";
const fs = require("fs");
const path = require("path");
const PM = require(path.join(__dirname, "..", "..", "web", "picmatch.js"));

function readRgba(file) {
  const buf = fs.readFileSync(file);
  const width = buf.readUInt32LE(0), height = buf.readUInt32LE(4);
  return { width, height, data: new Uint8ClampedArray(buf.buffer, buf.byteOffset + 8, width * height * 4) };
}

const job = JSON.parse(fs.readFileSync(process.argv[2], "utf8"));
const out = { images: [], roundtrip: [] };
const refs = [];
for (const [key, files] of Object.entries(job.refs || {})) {
  for (const file of files) {
    const feat = PM.refFeature(readRgba(file));
    // как в программе: признаки хранятся упакованными
    const packed = PM.pack(feat);
    const back = PM.unpack(packed);
    out.roundtrip.push([key, packed.length, PM.similarity(feat, back).score]);
    refs.push({ key, feat: back });
  }
}
for (const image of job.images || []) {
  const img = readRgba(image.file);
  const started = Date.now();
  const tiles = PM.detectTiles(img);
  const result = { name: image.name, ms: Date.now() - started, tiles: [] };
  for (const t of tiles) {
    const feat = PM.tileFeature(img, t);
    const ranked = PM.rank(feat, refs, 4);
    result.tiles.push({
      x0: t.x0, x1: t.x1, iconTop: t.iconTop, iconBottom: t.iconBottom, banner: t.banner, partial: t.partial,
      badge: t.badge ? { x0: t.badge.x0, y0: t.badge.y0, x1: t.badge.x1, y1: t.badge.y1 } : null,
      bucket: PM.bannerBucket(t.color), rgb: t.color.rgb, empty: !feat,
      prior: PM.rarityPrior(t.color, job.banners || {}),
      top: ranked.map((r) => [r.key, r.score]), sure: PM.sureKey(ranked),
    });
  }
  // плитки по месту из распознавания текста (приблизительному)
  result.boxes = (image.boxes || []).map((b) => {
    const t = PM.tileFromBox(img, b.box, b.badge_box || null, b.bg || null);
    if (!t) return null;
    const ranked = PM.rank(PM.tileFeature(img, t), refs, 4);
    return { top: ranked.map((r) => [r.key, r.score]), sure: PM.sureKey(ranked) };
  });
  out.images.push(result);
}
if (job.merge) out.merge = [...PM.mergeByKey(job.merge).entries()].map(([key, tiles]) => [key, tiles.map((t) => t.id)]);
if (job.sure) out.sure = job.sure.map((ranked) => PM.sureKey(ranked));
if (job.prior) out.prior = job.prior.map(([rgb, learned]) => PM.rarityPrior(PM.colorFromRgb(rgb), learned));
if (job.apply) out.apply = job.apply.map(([ranked, categories]) => PM.applyPrior(ranked, categories).map((r) => [r.key, r.score]));
process.stdout.write(JSON.stringify(out));
