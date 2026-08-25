#!/usr/bin/env python3
"""Generates the app icons as PNG. Deliberately dependency-free (stdlib only) so that
installing the bundles needs no Python environment.

Usage: make-icon.py <path.png> [run|stop]

Motif "run":  a bar chart with a cashflow line above it — the dashboard itself.
Motif "stop": the same ground, but flat grey bars and a stop square, so that the two
icons stay distinguishable in Spotlight at 32 px. Colors are taken from the dashboard's
own dark theme (app/static/styles.css).
"""

import struct
import sys
import zlib

W = H = 512
BG = (11, 14, 18)  # --ink-0
PANEL = (22, 27, 35)  # --ink-2
GRID = (38, 46, 57)  # --rule
BAR = (91, 201, 143)  # --pos
LINE = (127, 176, 255)  # --focus
OFF = (141, 153, 168)  # --paper-dim

RADIUS = 96


def rounded(x: int, y: int) -> bool:
    """Inside the rounded square?"""
    cx = min(max(x, RADIUS), W - RADIUS)
    cy = min(max(y, RADIUS), H - RADIUS)
    return (x - cx) ** 2 + (y - cy) ** 2 <= RADIUS**2


def blend(dst, src, a):
    return tuple(int(d + (s - d) * a) for d, s in zip(dst, src))


def main(path: str, variant: str = "run") -> None:
    px = [[BG for _ in range(W)] for _ in range(H)]
    for y in range(H):
        for x in range(W):
            px[y][x] = PANEL if rounded(x, y) else BG

    baseline = int(H * 0.74)

    # Two quiet grid lines and the baseline the bars stand on.
    for gy in (int(H * 0.34), int(H * 0.54), baseline):
        for x in range(80, W - 80):
            if rounded(x, gy):
                px[gy][x] = GRID

    bar_color = OFF if variant == "stop" else BAR
    # Flat bars in the stop variant: nothing is being tracked.
    heights = [0.10] * 5 if variant == "stop" else [0.20, 0.34, 0.16, 0.42, 0.28]

    left, width, gap = 92, 52, 20
    for i, h in enumerate(heights):
        x0 = left + i * (width + gap)
        top = baseline - int(H * h)
        for y in range(top, baseline):
            for x in range(x0, x0 + width):
                if rounded(x, y):
                    px[y][x] = bar_color

    if variant == "stop":
        # A stop square centred over the flat bars. The grey bars alone would not read as
        # "stopped" at small sizes; the square is a function symbol, not decoration.
        half, r = 74, 12
        cx, cy = W // 2, int(H * 0.46)
        for y in range(cy - half, cy + half):
            for x in range(cx - half, cx + half):
                ix = min(max(x, cx - half + r), cx + half - r)
                iy = min(max(y, cy - half + r), cy + half - r)
                if (x - ix) ** 2 + (y - iy) ** 2 <= r * r:
                    px[y][x] = OFF
    else:
        # The running-total line from the cashflow chart, drawn over the bars.
        pts = [
            (92, int(H * 0.52)),
            (164, int(H * 0.44)),
            (236, int(H * 0.46)),
            (308, int(H * 0.30)),
            (380, int(H * 0.34)),
            (420, int(H * 0.24)),
        ]
        for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
            dx, dy = x1 - x0, y1 - y0
            length2 = dx * dx + dy * dy
            wid = 12.0
            for y in range(int(min(y0, y1) - wid), int(max(y0, y1) + wid)):
                for x in range(int(min(x0, x1) - wid), int(max(x0, x1) + wid)):
                    if not (0 <= x < W and 0 <= y < H):
                        continue
                    t = 0.0 if length2 == 0 else ((x - x0) * dx + (y - y0) * dy) / length2
                    t = min(1.0, max(0.0, t))
                    px_, py_ = x0 + t * dx, y0 + t * dy
                    d = ((x - px_) ** 2 + (y - py_) ** 2) ** 0.5
                    if d <= wid / 2:
                        px[y][x] = blend(px[y][x], LINE, min(1.0, (wid / 2 - d) / 1.6))

    raw = b"".join(
        b"\x00" + b"".join(struct.pack("BBB", *px[y][x]) for x in range(W))
        for y in range(H)
    )

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    png = (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", W, H, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw, 9))
        + chunk(b"IEND", b"")
    )
    with open(path, "wb") as fh:
        fh.write(png)


if __name__ == "__main__":
    main(
        sys.argv[1] if len(sys.argv) > 1 else "icon.png",
        sys.argv[2] if len(sys.argv) > 2 else "run",
    )
