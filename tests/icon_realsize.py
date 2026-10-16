# -*- coding: utf-8 -*-
"""
按**真实屏幕像素**对比图标尺寸：本机是 150% 缩放，
所以 24px 的图标在屏幕上实际占 36 个物理像素、32px 占 48 个。

这张图直接按 1.5 倍（四舍五入）放大图标，模拟"你在屏幕上真实看到的大小与清晰度"，
一行一个候选尺寸。比 5 倍放大更能反映实际观感。
"""
import base64
import os
import sys

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import winmon_core as core                      # noqa: E402
from icon_montage import decode_png, write_png  # noqa: E402

SCALE = 1.5          # 本机显示缩放（150%）


def scale_rows(rows, w, h, factor):
    """最近邻放大到 factor 倍（取整）。"""
    f = int(round(factor))
    if f <= 1:
        return w, h, rows
    out = []
    for y in range(h):
        src = rows[y]
        line = bytearray()
        for x in range(w):
            line += src[x * 4:x * 4 + 4] * f
        for _ in range(f):
            out.append(bytes(line))
    return w * f, h * f, out


def main():
    sizes = [int(x) for x in (sys.argv[1].split(",") if len(sys.argv) > 1
                              else ["16", "24", "28", "32"])]
    bg = (243, 243, 243)                       # 贴近 Tk 列表的浅灰底
    items = core.build_program_catalog(use_cache=True)
    want = ["哔哩哔哩", "Steam", "Visual Studio Code", "Word", "火绒安全软件",
            "MuMu模拟器", "7-Zip File Manager", "某个 AI 客户端"]
    picked = [next((it for it in items
                    if it.name.lower().startswith(w.lower()) or w.lower() in it.name.lower()),
                   None) for w in want]
    picked = [p for p in picked if p]

    rows_out = []
    print(f"模拟显示缩放 {SCALE:.2f}×（本机 150%）；候选尺寸 {sizes}")
    for size in sizes:
        tiles = []
        for it in picked:
            b64 = core.icon_from_file(it.target, size)
            if not b64:
                continue
            w, h, rows = decode_png(base64.b64decode(b64))
            w2, h2, rows2 = scale_rows(rows, w, h, SCALE)
            tiles.append((it.name, w2, h2, rows2))
        if tiles:
            rows_out.append((size, tiles))
            print(f"  {size}px → 屏幕上占 {tiles[0][1]}px，{len(tiles)} 个图标")

    pad, gap = 12, 8
    cell_w = max(max(t[1] for t in tiles) for _s, tiles in rows_out)
    cell_h = max(max(t[2] for t in tiles) for _s, tiles in rows_out)
    n_cols = max(len(tiles) for _s, tiles in rows_out)
    row_h = cell_h + gap
    W = pad + n_cols * (cell_w + gap)
    H = pad + len(rows_out) * row_h
    buf = bytearray()
    for _y in range(H):
        buf += bytes(bg) * W

    for r, (_size, tiles) in enumerate(rows_out):
        oy = pad + r * row_h
        for idx, (_name, w, h, rows) in enumerate(tiles):
            ox = pad + idx * (cell_w + gap)
            for y in range(h):
                src = rows[y]
                for x in range(w):
                    rr, gg, bb, a = src[x * 4:x * 4 + 4]
                    if a == 0:
                        continue
                    rr = (rr * a + bg[0] * (255 - a)) // 255
                    gg = (gg * a + bg[1] * (255 - a)) // 255
                    bb = (bb * a + bg[2] * (255 - a)) // 255
                    off = ((oy + y) * W + ox + x) * 3
                    buf[off], buf[off + 1], buf[off + 2] = rr, gg, bb

    out = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       f"screenshot-icons-realsize-{'-'.join(str(s) for s, _t in rows_out)}.png")
    write_png(bytes(buf), W, H, out)
    print(f"\n已写出（按真实显示像素）: {out} ({W}×{H})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
