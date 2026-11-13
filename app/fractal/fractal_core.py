"""fractal_core.py — render one Mandelbrot tile. No Ray, no I/O.

This is the unit of work of the bag-of-tasks app: one call renders one tile of
an N x N canvas, independently of every other tile. The tile is computed at its
full resolution (tile_px x tile_px escape-time iterations, the real cost), and
only a small preview is colored and PNG-encoded for the dashboard.

Determinism: the escape counts are integers produced by the same IEEE float64
operations in the same order on every machine (NumPy and the pure-Python
fallback do identical arithmetic), so a tile's `checksum` is reproducible and a
complete canvas has a single reference checksum for a given spec. Preview
colors use log2 and may differ in the last bit between paths; they are not part
of the checksum.

Imported by render.py on the driver, inside the per-node Ray actors, and by the
actors' spawned worker processes (it must stay importable on its own).
"""

import base64
import hashlib
import math
import struct
import time
import zlib

try:
    import numpy as np
except ImportError:  # the pure-Python path is slow but exact
    np = None

# The full Mandelbrot set: (xmin, ymin, xmax, ymax).
DEFAULT_REGION = (-2.5, -1.25, 1.0, 1.25)

# Color stops per palette; the gradient repeats every COLOR_CYCLE iterations, so
# the look does not wash out when max_iter is raised to add work per tile.
PALETTES = {
    "electric": [(0, 0, 0), (0, 7, 100), (32, 107, 203), (237, 255, 255), (255, 170, 0), (0, 2, 0)],
    "fire": [(0, 0, 0), (25, 7, 26), (109, 1, 31), (189, 21, 2), (238, 113, 0), (255, 255, 0)],
    "ocean": [(0, 0, 0), (0, 20, 50), (0, 72, 120), (0, 150, 180), (72, 210, 210), (200, 255, 255)],
    "cosmic": [(0, 0, 0), (20, 0, 40), (80, 0, 120), (160, 30, 200), (220, 100, 255), (255, 220, 255)],
}
COLOR_CYCLE = 64.0


def preview_size(tiles, tile_px):
    """Preview edge in px: the dashboard canvas is tiles * preview px wide, so this
    keeps it near 2048 px whatever the grid (min 4 px, never above tile_px)."""
    return max(1, min(tile_px, max(4, 2048 // tiles)))


def tile_origin(spec, tx, ty):
    xmin, ymin, xmax, ymax = spec["region"]
    n = spec["tiles"]
    tw = (xmax - xmin) / n
    th = (ymax - ymin) / n
    return xmin + tx * tw, ymin + ty * th, tw, th


def _counts_numpy(spec, tx, ty):
    p, m = spec["tile_px"], spec["max_iter"]
    x0, y0, tw, th = tile_origin(spec, tx, ty)
    frac = np.arange(p, dtype=np.float64) / p
    xs = x0 + frac * tw
    ys = y0 + frac * th
    cx = np.tile(xs, p)            # row-major: pixel (py, px) -> index py*p + px
    cy = np.repeat(ys, p)
    npx = p * p
    counts = np.full(npx, m, dtype=np.int32)
    mag = np.zeros(npx, dtype=np.float64)
    idx = np.arange(npx)
    zx = np.zeros(npx)
    zy = np.zeros(npx)
    acx, acy = cx, cy
    for n in range(m):
        zx2 = zx * zx
        zy2 = zy * zy
        r2 = zx2 + zy2
        keep = r2 <= 4.0
        if not keep.all():
            out = ~keep
            counts[idx[out]] = n
            mag[idx[out]] = r2[out]
            idx, zx, zy, acx, acy = idx[keep], zx[keep], zy[keep], acx[keep], acy[keep]
            zx2, zy2 = zx2[keep], zy2[keep]
            if idx.size == 0:
                break
        zy = 2.0 * zx * zy + acy   # uses the old zx, as in the scalar loop
        zx = zx2 - zy2 + acx
    if idx.size:
        mag[idx] = zx * zx + zy * zy
    return counts, mag


def _counts_python(spec, tx, ty):
    p, m = spec["tile_px"], spec["max_iter"]
    x0, y0, tw, th = tile_origin(spec, tx, ty)
    counts, mag = [], []
    for py in range(p):
        cy = y0 + (py / p) * th
        for px in range(p):
            cx = x0 + (px / p) * tw
            zx = zy = 0.0
            n = 0
            while True:
                zx2 = zx * zx
                zy2 = zy * zy
                if zx2 + zy2 > 4.0 or n >= m:
                    break
                zy = 2.0 * zx * zy + cy
                zx = zx2 - zy2 + cx
                n += 1
            counts.append(n)
            mag.append(zx * zx + zy * zy)
    return counts, mag


def _color(palette, count, r2, max_iter):
    if count >= max_iter:
        return (0, 0, 0)
    smooth = count + 1 - math.log2(math.log2(max(r2, 1.001)))
    t = (smooth % COLOR_CYCLE) / COLOR_CYCLE
    k = len(palette) - 1
    pos = t * k
    i = min(int(pos), k - 1)
    f = pos - i
    c0, c1 = palette[i], palette[i + 1]
    return tuple(int(a + (b - a) * f) for a, b in zip(c0, c1))


def _png(width, height, rgb_rows):
    def chunk(kind, data):
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)
    raw = b"".join(b"\x00" + bytes(row) for row in rgb_rows)
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 6))
            + chunk(b"IEND", b""))


def render_tile(spec, tx, ty, force_python=False):
    """Render tile (tx, ty) of `spec` (keys: tiles, tile_px, max_iter, palette,
    region, preview_px). Returns a JSON-safe dict."""
    start = time.time()
    p, m = spec["tile_px"], spec["max_iter"]
    if np is not None and not force_python:
        counts, mag = _counts_numpy(spec, tx, ty)
        digest = hashlib.sha256(counts.astype("<i4").tobytes()).hexdigest()
        iter_sum = int(counts.sum(dtype=np.int64))
        count_at = lambda i: int(counts[i])
        mag_at = lambda i: float(mag[i])
    else:
        counts, mag = _counts_python(spec, tx, ty)
        digest = hashlib.sha256(struct.pack("<%di" % len(counts), *counts)).hexdigest()
        iter_sum = sum(counts)
        count_at = counts.__getitem__
        mag_at = mag.__getitem__
    q = spec.get("preview_px") or p
    palette = PALETTES.get(spec.get("palette"), PALETTES["electric"])
    rows = []
    for j in range(q):
        py = (j * p) // q
        row = []
        for i in range(q):
            k = py * p + (i * p) // q
            row.extend(_color(palette, count_at(k), mag_at(k), m))
        rows.append(row)
    png = _png(q, q, rows)
    return {
        "x": tx,
        "y": ty,
        "checksum": digest,
        "iter_sum": iter_sum,
        "ms": round((time.time() - start) * 1000.0, 1),
        "preview_px": q,
        "png_b64": base64.b64encode(png).decode("ascii"),
    }


def render_tile_args(args):
    """Single-argument wrapper for executor.map / pool workers."""
    spec, tx, ty = args
    return render_tile(spec, tx, ty)
