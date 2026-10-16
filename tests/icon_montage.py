# -*- coding: utf-8 -*-
"""
把程序目录里取到的图标解码出来，拼成一张对照图（用来目视确认清晰度）。

背景：Tk 在窗口里画的图片**用 PrintWindow 抓不到**，所以界面截图里图标列是空的。
这里绕开窗口，直接把 `icon_from_file()` 产出的 PNG 解码、放大、拼图落盘。
纯标准库（zlib + 手写 PNG 解码），不依赖 Pillow。
"""
import base64
import os
import struct
import sys
import zlib

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import winmon_core as core  # noqa: E402


def decode_png(data):
    """极简 PNG 解码：只处理我们自己编码器产出的 8 位 RGBA、无隔行格式。"""
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    pos, idat, w, h = 8, b"", 0, 0
    while pos + 8 <= len(data):
        (length,) = struct.unpack(">I", data[pos:pos + 4])
        tag = data[pos + 4:pos + 8]
        body = data[pos + 8:pos + 8 + length]
        if tag == b"IHDR":
            w, h, depth, ctype = struct.unpack(">IIBB", body[:10])
            assert depth == 8 and ctype == 6, f"只支持 8 位 RGBA，得到 {depth}/{ctype}"
        elif tag == b"IDAT":
            idat += body
        pos += 12 + length
    raw = zlib.decompress(idat)
    rows, stride = [], w * 4
    prev = bytearray(stride)
    p = 0
    for _y in range(h):
        filt = raw[p]
        p += 1
        line = bytearray(raw[p:p + stride])
        p += stride
        # 反滤波（我们自己的编码器每行 filter=0，但这里保险起见支持全类型）
        for i in range(stride):
            a = line[i - 4] if i >= 4 else 0
            b = prev[i]
            c = prev[i - 4] if i >= 4 else 0
            if filt == 1:
                line[i] = (line[i] + a) & 0xFF
            elif filt == 2:
                line[i] = (line[i] + b) & 0xFF
            elif filt == 3:
                line[i] = (line[i] + ((a + b) >> 1)) & 0xFF
            elif filt == 4:
                pa, pb, pc = abs(b - c), abs(a - c), abs(a + b - 2 * c)
                pr = a if (pa <= pb and pa <= pc) else (b if pb <= pc else c)
                line[i] = (line[i] + pr) & 0xFF
        rows.append(bytes(line))
        prev = line
    return w, h, rows


def write_png(rgb, w, h, path):
    raw = bytearray()
    stride = w * 3
    for y in range(h):
        raw.append(0)
        raw += rgb[y * stride:(y + 1) * stride]

    def chunk(tag, data):
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    with open(path, "wb") as fh:
        fh.write(b"\x89PNG\r\n\x1a\n"
                 + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
                 + chunk(b"IDAT", zlib.compress(bytes(raw), 6))
                 + chunk(b"IEND", b""))


def main():
    sizes = [int(x) for x in (sys.argv[1].split(",") if len(sys.argv) > 1
                              else ["32"])]
    scale = int(sys.argv[2]) if len(sys.argv) > 2 else 4
    items = core.build_program_catalog(use_cache=True)
    want = ["哔哩哔哩", "Steam", "Visual Studio Code", "Word", "火绒安全软件",
            "MuMu模拟器", "7-Zip File Manager", "某个 AI 客户端"]
    picked = []
    for w in want:
        hit = next((it for it in items if it.name.lower().startswith(w.lower())
                    or w.lower() in it.name.lower()), None)
        if hit:
            picked.append(hit)
    print(f"选中 {len(picked)} 个程序；要对比的尺寸 {sizes}")

    # 每个尺寸一行，便于横向对比"同尺寸下图标清晰度/体积"
    rows_out = []
    for size in sizes:
        tiles = []
        for it in picked:
            png_b64 = core.icon_from_file(it.target, size)
            if not png_b64:
                continue
            w, h, rows = decode_png(base64.b64decode(png_b64))
            tiles.append((it.name, w, h, rows))
        if not tiles:
            print(f"  {size}: 一个图标都没取到")
            continue
        # 左上角标出尺寸：直接把尺寸数字当成"额外格子"不好画，改为在文件名体现
        rows_out.append((size, tiles))
        print(f"  {size}: {len(tiles)} 个图标解码成功（{tiles[0][1]}×{tiles[0][2]}）")

    if not rows_out:
        print("没有可用图标")
        return 1

    # 纵向拼接：每个尺寸一行
    strip = int(sys.argv[3]) if len(sys.argv) > 3 else 0
    pad = 14
    bg = (240, 243, 246)
    cell_w = max(max(t[1] for t in tiles) for _s, tiles in rows_out) * scale
    cell_h = max(max(t[2] for t in tiles) for _s, tiles in rows_out) * scale
    n_cols = max(len(tiles) for _s, tiles in rows_out)
    W = pad + n_cols * (cell_w + pad)
    H = pad + len(rows_out) * (cell_h + pad)
    buf = bytearray()
    for _y in range(H):
        buf += bytes(bg) * W

    for r, (_size, tiles) in enumerate(rows_out):
        oy = pad + r * (cell_h + pad)
        for idx, (_name, w, h, rows) in enumerate(tiles):
            ox = pad + idx * (cell_w + pad)
            for y in range(h):
                src = rows[y]
                for x in range(w):
                    rr, gg, bb, a = src[x * 4:x * 4 + 4]
                    if a == 0:
                        continue
                    rr = (rr * a + bg[0] * (255 - a)) // 255
                    gg = (gg * a + bg[1] * (255 - a)) // 255
                    bb = (bb * a + bg[2] * (255 - a)) // 255
                    for dy in range(scale):
                        off_row = ((oy + y * scale + dy) * W + ox + x * scale) * 3
                        for dx in range(scale):
                            off = off_row + dx * 3
                            buf[off] = rr
                            buf[off + 1] = gg
                            buf[off + 2] = bb

    out = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       os.pardir,
                       f"screenshot-icons-{'-'.join(str(s) for s, _t in rows_out)}.png")
    out = os.path.abspath(out)
    write_png(bytes(buf), W, H, out)
    print(f"\n已写出对照图: {out} ({W}×{H}，每行一个尺寸，放大 {scale} 倍)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
