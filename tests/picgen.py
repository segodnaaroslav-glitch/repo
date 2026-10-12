"""Синтетические скриншоты инвентаря MM2 и картинки предметов для проверки сравнения по картинке.

Без библиотек: свой PNG (запись/чтение), заливка многоугольников со сглаживанием.
Плитка — как в игре (измерено на настоящей плитке при ширине W = 130 px): чёрная рамка,
квадрат 130x136 на сером градиенте #393939..#303030, чёрная черта, полоса с названием
высотой 31 px (цвет — редкость), значок «xN» белыми знаками в правом верхнем углу.
Предметы нарисованы простыми фигурами: у каждого свой силуэт и свои цвета; «трудные»
пары — одинаковый силуэт с другими цветами (Chroma) и одинаковое название (Cowboy).
"""

import math
import random
import struct
import zlib

# --- PNG ----------------------------------------------------------------------------------


def png_bytes(width, height, rgba, alpha=True):
    """RGBA bytearray -> PNG (RGBA или RGB)."""
    channels = 4 if alpha else 3
    rows = bytearray()
    for y in range(height):
        rows.append(0)
        line = rgba[y * width * 4:(y + 1) * width * 4]
        if alpha:
            rows += line
        else:
            for x in range(width):
                rows += line[x * 4:x * 4 + 3]

    def chunk(kind, body):
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body) & 0xFFFFFFFF)

    header = struct.pack(">IIBBBBB", width, height, 8, 6 if alpha else 2, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(bytes(rows), 6))
            + chunk(b"IEND", b""))


def png_decode(data):
    """PNG (8 бит, RGB/RGBA/серый, без чересстрочности) -> (ширина, высота, RGBA bytearray)."""
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    pos, idat = 8, b""
    while pos < len(data):
        length, kind = struct.unpack(">I4s", data[pos:pos + 8])
        body = data[pos + 8:pos + 8 + length]
        pos += 12 + length
        if kind == b"IHDR":
            width, height, depth, ctype, _, _, interlace = struct.unpack(">IIBBBBB", body)
            assert depth == 8 and interlace == 0
            bpp = {0: 1, 2: 3, 4: 2, 6: 4}[ctype]
        elif kind == b"IDAT":
            idat += body
        elif kind == b"IEND":
            break
    raw = zlib.decompress(idat)
    stride = width * bpp
    prev = bytearray(stride)
    out = bytearray(width * height * 4)
    for y in range(height):
        f = raw[y * (stride + 1)]
        line = bytearray(raw[y * (stride + 1) + 1:(y + 1) * (stride + 1)])
        for i in range(stride):
            a = line[i - bpp] if i >= bpp else 0
            b = prev[i]
            c = prev[i - bpp] if i >= bpp else 0
            if f == 1:
                line[i] = (line[i] + a) & 255
            elif f == 2:
                line[i] = (line[i] + b) & 255
            elif f == 3:
                line[i] = (line[i] + ((a + b) >> 1)) & 255
            elif f == 4:
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                line[i] = (line[i] + (a if pa <= pb and pa <= pc else b if pb <= pc else c)) & 255
        prev = line
        for x in range(width):
            o = (y * width + x) * 4
            if bpp >= 3:
                out[o:o + 3] = line[x * bpp:x * bpp + 3]
                out[o + 3] = line[x * bpp + 3] if bpp == 4 else 255
            else:
                out[o] = out[o + 1] = out[o + 2] = line[x * bpp]
                out[o + 3] = line[x * bpp + 1] if bpp == 2 else 255
    return width, height, out


def rgba_file(width, height, rgba):
    """Сырой формат для проверки в Node: ширина, высота (uint32 LE) и RGBA."""
    return struct.pack("<II", width, height) + bytes(rgba)


# --- рисование ----------------------------------------------------------------------------


class Canvas:
    def __init__(self, width, height, color=(0, 0, 0, 0)):
        self.width, self.height = width, height
        self.px = bytearray(bytes(color) * (width * height))

    def rect(self, x0, y0, x1, y1, color):
        """Прямоугольник [x0, x1) x [y0, y1) непрозрачным цветом."""
        x0, x1 = max(0, int(x0)), min(self.width, int(x1))
        row = bytes(tuple(color[:3]) + (255,)) * max(0, x1 - x0)
        for y in range(max(0, int(y0)), min(self.height, int(y1))):
            self.px[(y * self.width + x0) * 4:(y * self.width + x1) * 4] = row

    def blend(self, x, y, color, cover):
        if cover <= 0 or not (0 <= x < self.width and 0 <= y < self.height):
            return
        o = (y * self.width + x) * 4
        a = cover * color[3] / 255.0 if len(color) > 3 else cover
        da = self.px[o + 3] / 255.0
        out_a = a + da * (1 - a)
        for c in range(3):
            src, dst = color[c], self.px[o + c]
            value = (src * a + dst * da * (1 - a)) / out_a if out_a > 0 else 0
            self.px[o + c] = max(0, min(255, int(round(value))))
        self.px[o + 3] = max(0, min(255, int(round(out_a * 255))))

    def polygon(self, points, color, ss=4):
        """Многоугольник со сглаживанием (ss строк выборки на пиксель, точное покрытие по x)."""
        ys = [p[1] for p in points]
        xs = [p[0] for p in points]
        y0, y1 = max(0, int(min(ys))), min(self.height - 1, int(max(ys)) + 1)
        x0, x1 = max(0, int(min(xs))), min(self.width - 1, int(max(xs)) + 1)
        if x1 < x0 or y1 < y0:
            return
        edges = list(zip(points, points[1:] + points[:1]))
        for y in range(y0, y1 + 1):
            cov = [0.0] * (x1 - x0 + 2)
            for s in range(ss):
                sy = y + (s + 0.5) / ss
                hits = sorted(ax + (sy - ay) * (bx - ax) / (by - ay)
                              for (ax, ay), (bx, by) in edges if (ay <= sy < by) or (by <= sy < ay))
                for i in range(0, len(hits) - 1, 2):
                    left, right = max(hits[i], x0), min(hits[i + 1], x1 + 1)
                    if right <= left:
                        continue
                    a, b = int(left), int(right)
                    if a == b:
                        cov[a - x0] += (right - left) / ss
                        continue
                    cov[a - x0] += (a + 1 - left) / ss
                    for k in range(a + 1, b):
                        cov[k - x0] += 1.0 / ss
                    if b <= x1:
                        cov[b - x0] += (right - b) / ss
            for k, value in enumerate(cov):
                if value > 0:
                    self.blend(x0 + k, y, color, min(1.0, value))

    def paste(self, other, ox, oy):
        for y in range(other.height):
            for x in range(other.width):
                o = (y * other.width + x) * 4
                a = other.px[o + 3]
                if a:
                    self.blend(ox + x, oy + y, tuple(other.px[o:o + 3]) + (255,), a / 255.0)

    def png(self, alpha=True):
        return png_bytes(self.width, self.height, self.px, alpha)

    def rgba(self):
        return rgba_file(self.width, self.height, self.px)


# --- предметы -----------------------------------------------------------------------------
# Фигуры в квадрате 0..1 (x вправо, y вниз): [(точки, цвет)].

def _poly(*pts):
    return [tuple(p) for p in pts]


SCYTHE = [  # коса: длинная рукоять по диагонали и изогнутое лезвие сверху
    (_poly((0.16, 0.90), (0.26, 0.97), (0.72, 0.24), (0.62, 0.17)), "handle"),
    (_poly((0.58, 0.12), (0.74, 0.26), (0.56, 0.06), (0.30, 0.04), (0.08, 0.16), (0.02, 0.38), (0.20, 0.28),
           (0.36, 0.22), (0.52, 0.24)), "blade"),
]
KNIFE = [  # нож: длинное лезвие, гарда и рукоять
    (_poly((0.24, 0.66), (0.80, 0.04), (0.94, 0.14), (0.40, 0.78)), "blade"),
    (_poly((0.18, 0.62), (0.26, 0.56), (0.52, 0.82), (0.44, 0.88)), "guard"),
    (_poly((0.28, 0.76), (0.38, 0.82), (0.18, 0.98), (0.08, 0.92)), "handle"),
]
REVOLVER = [  # револьвер: ствол, барабан, рукоять
    (_poly((0.12, 0.30), (0.88, 0.30), (0.88, 0.42), (0.12, 0.42)), "blade"),
    (_poly((0.52, 0.26), (0.74, 0.26), (0.74, 0.54), (0.52, 0.54)), "guard"),
    (_poly((0.66, 0.50), (0.82, 0.48), (0.92, 0.86), (0.74, 0.90)), "handle"),
]
BOWIE = [  # широкий нож с изогнутым концом
    (_poly((0.08, 0.40), (0.62, 0.30), (0.70, 0.34), (0.70, 0.56), (0.30, 0.60), (0.10, 0.52)), "blade"),
    (_poly((0.68, 0.24), (0.74, 0.24), (0.74, 0.66), (0.68, 0.66)), "guard"),
    (_poly((0.74, 0.36), (0.96, 0.36), (0.96, 0.54), (0.74, 0.54)), "handle"),
]
FANG = [  # два клыка на зелёной рукояти
    (_poly((0.20, 0.10), (0.36, 0.12), (0.40, 0.56), (0.30, 0.60)), "blade"),
    (_poly((0.56, 0.14), (0.70, 0.10), (0.66, 0.58), (0.56, 0.56)), "blade"),
    (_poly((0.22, 0.56), (0.74, 0.52), (0.74, 0.70), (0.22, 0.74)), "guard"),
    (_poly((0.42, 0.70), (0.56, 0.70), (0.56, 0.96), (0.42, 0.96)), "handle"),
]
PISTOL = [  # пистолет с длинной рукоятью
    (_poly((0.10, 0.26), (0.80, 0.26), (0.80, 0.40), (0.10, 0.40)), "blade"),
    (_poly((0.56, 0.38), (0.80, 0.38), (0.86, 0.88), (0.62, 0.88)), "handle"),
    (_poly((0.40, 0.40), (0.52, 0.40), (0.52, 0.56), (0.40, 0.56)), "guard"),
]
JAGGED = [  # зубчатый клинок (Icebreaker)
    (_poly((0.18, 0.86), (0.30, 0.66), (0.26, 0.56), (0.44, 0.48), (0.40, 0.38), (0.60, 0.30), (0.56, 0.20),
           (0.86, 0.06), (0.74, 0.34), (0.66, 0.40), (0.62, 0.52), (0.46, 0.58), (0.44, 0.70), (0.26, 0.92)), "blade"),
    (_poly((0.10, 0.80), (0.30, 0.94), (0.24, 0.98), (0.06, 0.86)), "handle"),
]

# ключ предмета -> (название, категория, фигура, цвета частей, цвет полосы)
ITEMS = {
    "ancients:Harvester": ("Harvester", "ancients", SCYTHE, {"handle": (90, 60, 40), "blade": (200, 40, 40)}, (230, 120, 20)),
    "godlies:Elderwood Scythe": ("Elderwood Scythe", "godlies", SCYTHE,
                                 {"handle": (110, 80, 40), "blade": (60, 190, 90)}, (220, 60, 180)),
    "godlies:Seer": ("Seer", "godlies", KNIFE, {"blade": (150, 110, 255), "guard": (220, 180, 60), "handle": (70, 50, 110)},
                     (220, 60, 180)),
    "chromas:Chroma Seer": ("Chroma Seer", "chromas", KNIFE,
                            {"blade": (60, 230, 120), "guard": (240, 90, 60), "handle": (240, 220, 60)}, (220, 60, 180)),
    "godlies:Icebreaker": ("Icebreaker", "godlies", JAGGED, {"blade": (150, 230, 255), "handle": (40, 80, 200)}, (220, 60, 180)),
    "vintages:Cowboy": ("Cowboy", "vintages", REVOLVER, {"blade": (170, 170, 175), "guard": (120, 120, 125), "handle": (140, 90, 50)},
                        (230, 200, 0)),
    "rares:Cowboy": ("Cowboy", "rares", BOWIE, {"blade": (225, 225, 230), "guard": (200, 150, 40), "handle": (170, 100, 30)},
                     (40, 120, 230)),
    "godlies:Fang": ("Fang", "godlies", FANG, {"blade": (245, 245, 225), "guard": (60, 160, 80), "handle": (40, 100, 50)},
                     (220, 60, 180)),
    "commons:Luger": ("Luger", "commons", PISTOL, {"blade": (120, 120, 120), "handle": (25, 25, 25), "guard": (90, 90, 90)},
                      (200, 200, 200)),
}


def draw_item(canvas, key, x, y, size, rng=None, bright=1.0):
    """Предмет в квадрате size x size с левым верхним углом (x, y)."""
    shape, colors = ITEMS[key][2], ITEMS[key][3]
    for points, part in shape:
        color = tuple(max(0, min(255, int(c * bright))) for c in colors[part]) + (255,)
        canvas.polygon([(x + px * size, y + py * size) for px, py in points], color)


def reference(key, size=110, fill=0.94, shift=(0.0, 0.0)):
    """Картинка предмета как на сайте / StarPets: прозрачный фон."""
    canvas = Canvas(size, size)
    inner = size * fill
    draw_item(canvas, key, (size - inner) / 2 + shift[0] * size, (size - inner) / 2 + shift[1] * size, inner)
    return canvas


def decoy(seed, size=110):
    """Картинка «другого предмета»: случайные многоугольники случайных цветов (для отбора
    кандидатов среди многих эталонов и проверки, что чужое не узнаётся уверенно)."""
    rng = random.Random(seed)
    canvas = Canvas(size, size)
    for _ in range(rng.randint(2, 4)):
        cx, cy, r = rng.uniform(0.25, 0.75), rng.uniform(0.25, 0.75), rng.uniform(0.12, 0.4)
        points = []
        for k in range(rng.randint(3, 7)):
            angle = 6.2832 * k / 7 + rng.uniform(-0.3, 0.3)
            dist = r * rng.uniform(0.5, 1.0)
            points.append((size * (cx + dist * math.cos(angle)), size * (cy + dist * math.sin(angle))))
        canvas.polygon(points, tuple(rng.randint(30, 250) for _ in range(3)) + (255,))
    return canvas


def add_noise(canvas, amount, seed=3):
    """Шум сжатия скриншота: каждый канал ±amount."""
    rng = random.Random(seed)
    px = canvas.px
    for i in range(0, len(px), 4):
        for c in range(3):
            px[i + c] = max(0, min(255, px[i + c] + rng.randint(-amount, amount)))


# --- значок «xN»: шрифт 3x5 ---------------------------------------------------------------
FONT = {
    "x": ("...", "...", "#.#", ".#.", "#.#"),
    "0": ("###", "#.#", "#.#", "#.#", "###"), "1": (".#.", "##.", ".#.", ".#.", "###"),
    "2": ("###", "..#", "###", "#..", "###"), "3": ("###", "..#", "###", "..#", "###"),
    "4": ("#.#", "#.#", "###", "..#", "..#"), "5": ("###", "#..", "###", "..#", "###"),
    "6": ("###", "#..", "###", "#.#", "###"), "7": ("###", "..#", "..#", "..#", "..#"),
    "8": ("###", "#.#", "###", "#.#", "###"), "9": ("###", "#.#", "###", "..#", "###"),
}


def draw_text_px(canvas, text, right, top, height, color=(255, 255, 255)):
    """Значок пиксельным шрифтом: правый край right, высота знаков height."""
    cell = max(1, int(round(height / 5.0)))
    gap = cell
    width = len(text) * 3 * cell + (len(text) - 1) * gap
    x = int(round(right - width))
    for ch in text:
        for row, bits in enumerate(FONT[ch]):
            for col, bit in enumerate(bits):
                if bit == "#":
                    canvas.rect(x + col * cell, top + row * cell, x + (col + 1) * cell, top + (row + 1) * cell, color)
        x += 3 * cell + gap


# --- плитка и сетка -----------------------------------------------------------------------


def tile_size(width):
    s = width / 130.0
    frame, sep = max(1, int(round(2 * s))), max(2, int(round(3 * s)))
    icon_h, banner_h = int(round(136 * s)), max(6, int(round(31 * s)))
    return frame, sep, icon_h, banner_h, frame + icon_h + sep + banner_h + sep


def draw_tile(canvas, key, left, top, width, badge=None, rng=None, banner=None, empty=False):
    """Плитка с левым верхним углом рамки (left, top) и шириной содержимого width.
    -> место плитки: {"x0", "x1", "icon_top", "icon_bottom", "banner": [y0, y1]}."""
    rng = rng or random.Random(1)
    frame, sep, icon_h, banner_h, outer_h = tile_size(width)
    canvas.rect(left, top, left + width + 2 * frame, top + outer_h, (0, 0, 0))
    x0, y0 = left + frame, top + frame
    for y in range(icon_h):  # серый градиент сверху вниз
        v = 0x39 if y < 0.73 * icon_h else int(round(0x39 - 9 * (y - 0.73 * icon_h) / (0.27 * icon_h)))
        canvas.rect(x0, y0 + y, x0 + width, y0 + y + 1, (v, v, v))
    if not empty:
        size = width * rng.uniform(0.80, 0.90)
        ox = x0 + (width - size) / 2 + rng.uniform(-0.03, 0.03) * width
        oy = y0 + (icon_h - size) / 2 + rng.uniform(-0.03, 0.03) * width
        draw_item(canvas, key, ox, oy, size, bright=rng.uniform(0.92, 1.06))
    by0 = y0 + icon_h + sep
    color = banner or ITEMS[key][4]
    for x in range(width):  # полоса: цвет слева направо темнеет, как в игре
        k = 1.0 - 0.42 * x / max(1, width - 1)
        canvas.rect(x0 + x, by0, x0 + x + 1, by0 + banner_h, tuple(int(c * k) for c in color))
    # «название» — белые штрихи с тёмной обводкой посередине полосы
    text_h = max(3, int(round(banner_h * 0.45)))
    ty = by0 + (banner_h - text_h) // 2
    tx0, tx1 = x0 + int(0.22 * width), x0 + int(0.78 * width)
    canvas.rect(tx0 - 1, ty - 1, tx1 + 1, ty + text_h + 1, (60, 50, 10))
    step = max(3, int(round(width * 0.06)))
    for x in range(tx0, tx1, step):
        canvas.rect(x, ty, min(tx1, x + step - 1), ty + text_h, (255, 255, 255))
    if badge:
        draw_text_px(canvas, badge, x0 + width - 0.05 * width, y0 + int(round(0.09 * width)), 0.12 * width)
    return {"x0": x0, "x1": x0 + width - 1, "icon_top": y0, "icon_bottom": y0 + icon_h - 1,
            "banner": [by0, by0 + banner_h - 1]}


def grid(cells, width=130, cols=4, seed=7, header=40, cut_left=True):
    """Скриншот инвентаря: cells — [(ключ, значок или None)] по строкам; None — пустое место.
    -> (Canvas, [{"key", "badge", ...место плитки}])."""
    rng = random.Random(seed)
    frame, sep, icon_h, banner_h, outer_h = tile_size(width)
    gap = max(2, int(round(4 * width / 130.0)))
    pitch_x, pitch_y = width + 2 * frame + gap, outer_h + gap
    rows = (len(cells) + cols - 1) // cols
    left = int(0.4 * width) if cut_left else 10
    canvas = Canvas(left + cols * pitch_x + 12, header + rows * pitch_y + 14, (0x21, 0x21, 0x21, 255))
    canvas.rect(0, 0, canvas.width, header, (0x1b, 0x1b, 0x1d))
    for k in range(3):  # надписи в шапке окна
        canvas.rect(14 + k * 90, header // 3, 14 + k * 90 + 60, header // 3 + 9, (230, 230, 230))
    truth = []
    keys = [cell[0] for cell in cells if cell]
    if cut_left:  # обрезанный столбец у левого края скриншота
        for row in range(rows):
            draw_tile(canvas, keys[row % len(keys)], left - pitch_x + gap, header + row * pitch_y, width, rng=rng)
    for index, cell in enumerate(cells):
        if cell is None:
            continue
        key, badge = cell[0], cell[1]
        row, col = divmod(index, cols)
        place = draw_tile(canvas, key, left + col * pitch_x, header + row * pitch_y, width, badge, rng)
        truth.append(dict(place, key=key, badge=badge))
    return canvas, truth
