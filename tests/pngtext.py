"""Диагностика распознавания: PNG -> текстовая картинка для журнала сборки (без библиотек)."""

import struct, zlib

def decode_png(data):
    """Minimal PNG reader (8-bit RGB/RGBA/gray, non-interlaced) -> (w, h, rows of gray values)."""
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    pos, idat, w = 8, b"", None
    while pos < len(data):
        length, kind = struct.unpack(">I4s", data[pos:pos + 8])
        chunk = data[pos + 8:pos + 8 + length]
        pos += 12 + length
        if kind == b"IHDR":
            w, h, depth, ctype, _, _, interlace = struct.unpack(">IIBBBBB", chunk)
            assert depth == 8 and interlace == 0, (depth, interlace)
            bpp = {0: 1, 2: 3, 4: 2, 6: 4}[ctype]
        elif kind == b"IDAT":
            idat += chunk
        elif kind == b"IEND":
            break
    raw = zlib.decompress(idat)
    stride = w * bpp
    prev = bytearray(stride)
    rows = []
    for y in range(h):
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
        if bpp >= 3:
            rows.append([(line[x * bpp] * 299 + line[x * bpp + 1] * 587 + line[x * bpp + 2] * 114) // 1000 for x in range(w)])
        else:
            rows.append([line[x * bpp] for x in range(w)])
    return w, h, rows

def ascii_art(data, sx=3, sy=6, light_ink=False):
    """Text picture of a PNG: '#' ink, '+' half-tone, ' ' paper (ink = dark, or light with
    light_ink). Blank lines are folded."""
    w, h, rows = decode_png(data)
    out, blank = [], 0
    for y0 in range(0, h, sy):
        line = ""
        for x0 in range(0, w, sx):
            cell = [rows[y][x] for y in range(y0, min(h, y0 + sy)) for x in range(x0, min(w, x0 + sx))]
            v = 255 - max(cell) if light_ink else min(cell)
            line += "#" if v < 90 else "+" if v < 170 else " "
        if line.strip():
            if blank:
                out.append("~ %d blank" % blank)
                blank = 0
            out.append("%4d|%s" % (y0, line.rstrip()))
        else:
            blank += 1
    return "%dx%d\n" % (w, h) + "\n".join(out)
