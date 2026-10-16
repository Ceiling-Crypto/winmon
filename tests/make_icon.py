# -*- coding: utf-8 -*-
"""
生成 WinMon 的图标文件（.ico，纯标准库，不依赖 Pillow）。

画什么：一个"监视器 + 波形"的图标 —— 外框是显示器（对应"看画面"），
里面是一条心跳/波形（对应"实时监控"）。
输出 4 个尺寸（16/24/32/48）的 ICO，Windows 会按需要挑合适的尺寸。

为什么自己画：项目约定零第三方依赖，而为了一个快捷方式图标去装 Pillow
不划算。PNG 编码本项目已有现成实现（winmon_core.encode_png_rgba），
.ico 只是把这些 PNG 打包进一个容器，格式很简单。
"""
import os
import struct
import sys

sys.stdout.reconfigure(encoding="utf-8")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import winmon_core as core  # noqa: E402

SIZES = (16, 24, 32, 48)
# 配色：深蓝底 + 青色波形（终端/监控风格）
BG = (0x14, 0x1E, 0x2B)          # 深蓝灰
FRAME = (0x5A, 0xC8, 0xE8)       # 亮青
WAVE = (0x7C, 0xFC, 0x9A)        # 亮绿
ACCENT = (0xFF, 0x6B, 0x6B)      # 红点（"录制/监控中"）


def blend(dst, src, a):
    """把 src 按 alpha(0~255) 叠到 dst 上。"""
    inv = 255 - a
    return tuple((src[i] * a + dst[i] * inv) // 255 for i in range(3))


def draw(size: int) -> bytes:
    """画一张 size×size 的 RGBA 图，返回裸像素。"""
    W = H = size
    px = [[(255, 255, 255, 0) for _ in range(W)] for _ in range(H)]

    def put(x, y, color, a=255):
        if 0 <= x < W and 0 <= y < H and a > 0:
            r, g, b, old_a = px[y][x]
            base = (r, g, b) if old_a else (255, 255, 255)
            nr, ng, nb = blend(base, color, a)
            na = min(255, old_a + a)
            px[y][x] = (nr, ng, nb, na)

    def rect(x0, y0, x1, y1, color, a=255):
        for y in range(max(0, y0), min(H, y1)):
            for x in range(max(0, x0), min(W, x1)):
                put(x, y, color, a)

    def line(x0, y0, x1, y1, color, thick=1, a=255):
        """Bresenham，带简单加粗。"""
        dx, dy = abs(x1 - x0), abs(y1 - y0)
        sx = 1 if x0 < x1 else -1
        sy = 1 if y0 < y1 else -1
        err = dx - dy
        while True:
            for oy in range(-(thick // 2), thick // 2 + 1):
                for ox in range(-(thick // 2), thick // 2 + 1):
                    put(x0 + ox, y0 + oy, color, a)
            if x0 == x1 and y0 == y1:
                break
            e2 = 2 * err
            if e2 > -dy:
                err -= dy
                x0 += sx
            if e2 < dx:
                err += dx
                y0 += sy

    # --- 屏幕外框（留 1px 边距，保证小尺寸也看得清）---
    m = max(1, size // 16)
    rect(m, m, W - m, H - m, FRAME, 255)                 # 外框实心
    inset = m + max(1, size // 12)
    rect(inset, inset, W - inset, H - inset, BG, 255)    # 内部深色

    # --- 底座 ---
    base_h = max(1, size // 12)
    rect(max(1, W // 4), H - m - base_h, W - max(1, W // 4), H - m, FRAME, 255)

    # --- 波形 ---
    # 线宽与幅度都要"随尺寸温和增长"：按 size//16 直线增长会让 48×48 变成一坨，
    # 所以线宽用 size//22（48→2px、16→1px），幅度只占内部高度的一半左右。
    iy0, iy1 = inset, H - inset
    mid = (iy0 + iy1) // 2
    left, right = inset + 1, W - inset - 1
    span = max(1, right - left)
    thick = max(1, size // 22)
    amp = max(1, int((iy1 - iy0) * 0.30))
    pts = []
    steps = max(2, span)
    for i in range(steps + 1):
        x = left + i
        t = i / steps
        # 一段"心跳"：平 → 下探 → 上冲 → 平
        if 0.22 < t < 0.40:
            v = -(t - 0.31) / 0.09
        elif 0.40 <= t < 0.60:
            v = (t - 0.50) / 0.10
        else:
            v = 0.0
        y = int(mid + v * amp)
        pts.append((x, max(iy0 + 1, min(iy1 - 2, y))))
    for a_, b_ in zip(pts, pts[1:]):
        line(a_[0], a_[1], b_[0], b_[1], WAVE, thick)

    # --- 右上角红点（监控中）---
    dot_r = max(1, size // 12)
    cx, cy = W - inset - dot_r - max(0, size // 16), inset + dot_r + max(0, size // 16)
    for y in range(cy - dot_r, cy + dot_r + 1):
        for x in range(cx - dot_r, cx + dot_r + 1):
            if (x - cx) ** 2 + (y - cy) ** 2 <= dot_r * dot_r:
                put(x, y, ACCENT, 255)

    out = bytearray()
    for y in range(H):
        for x in range(W):
            r, g, b, a = px[y][x]
            out += bytes((r, g, b, a))
    return bytes(out)


def main():
    pngs = []
    for size in SIZES:
        rgba = draw(size)
        pngs.append((size, core.encode_png_rgba(rgba, size, size)))
        print(f"  已绘制 {size}×{size}（PNG {len(pngs[-1][1])} 字节）")

    # ICO 容器：ICONDIR + 每个图像的 ICONDIRENTRY（16 字节）+ PNG 数据
    # 结构（小端）：
    #   ICONDIR   : reserved(2)=0 type(2)=1 count(2)
    #   ICONDIRENTRY: width(1) height(1) colors(1) reserved(1)
    #                 planes(2) bitCount(2) bytesInRes(4) imageOffset(4)
    count = len(pngs)
    header = struct.pack("<HHH", 0, 1, count)
    offset = len(header) + 16 * count
    entries = b""
    blobs = b""
    for size, data in pngs:
        w_byte = 0 if size >= 256 else size      # 0 表示 256
        entries += struct.pack("<BBBBHHII", w_byte, w_byte, 0, 0, 1, 32,
                               len(data), offset)
        offset += len(data)
        blobs += data
    ico = header + entries + blobs

    path = os.path.join(ROOT, "winmon.ico")
    with open(path, "wb") as fh:
        fh.write(ico)
    print(f"\n已写出图标: {path}（{len(ico)} 字节，含 {count} 个尺寸）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
