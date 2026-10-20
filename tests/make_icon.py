# -*- coding: utf-8 -*-
"""
生成 winmon1.0 的图标文件（.ico，纯标准库，不依赖 Pillow）。

设计（2026-10-20）：**简约线条**。
  · 一块留白很大的圆角浅色底，保证在浅色/深色任务栏上都不糊；
  · 中间是一个用圆头线段画出的 "W"，不填色、不加阴影；
  · 右上角一个小圆点（青绿）表示"正在看"，同时和 W 的右臂留出呼吸感；
  · 所有边缘按覆盖率做抗锯齿，不靠堆尺寸硬撑。

输出 16/20/24/32/40/48/64/256 八档，Windows 会按 DPI 和场景挑合适的一档。
**每一帧都用 32 位 DIB（BITMAPINFOHEADER + BGRA + AND mask），不用 PNG 压缩**：
PNG-in-ICO 虽然 Windows 资源管理器认，但 `System.Drawing.Icon` 和部分
`LoadImage` 调用会解出花屏/直接失败（旧图标就这样）。DIB 是所有 Windows
版本都稳的写法；八档加起来约 300 KB，对一个程序图标可以接受。
"""
import math
import os
import struct
import sys

sys.stdout.reconfigure(encoding="utf-8")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

SIZES = (16, 20, 24, 32, 40, 48, 64, 256)
# 配色：暖白底 + 墨色线条 + 一个青绿点。只保留两种色相，避免小尺寸变花。
TILE = (248, 250, 252)           # 近白（不是纯白，叠在白底上仍有边界）
EDGE = (199, 211, 225)           # 发丝边，白底上也能看出圆角
INK = (22, 33, 43)               # 墨色（W）
ACCENT = (14, 159, 138)          # 青绿（监控点）


def _clamp(value: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return lo if value < lo else hi if value > hi else value


def _coverage(distance: float) -> float:
    """有符号距离 → 像素覆盖率（1px 抗锯齿带）。"""
    return _clamp(0.5 - distance)


def _rounded_box(px: float, py: float, cx: float, cy: float,
                 half: float, radius: float) -> float:
    """圆角正方形的有符号距离（内部为负）。"""
    qx = abs(px - cx) - half + radius
    qy = abs(py - cy) - half + radius
    outside = math.hypot(max(qx, 0.0), max(qy, 0.0))
    inside = min(max(qx, qy), 0.0)
    return outside + inside - radius


def _capsule(px: float, py: float, ax: float, ay: float,
             bx: float, by: float, radius: float) -> float:
    """点到线段的距离减去半径：圆头粗线的有符号距离。"""
    vx, vy = bx - ax, by - ay
    wx, wy = px - ax, py - ay
    denom = vx * vx + vy * vy
    t = 0.0 if denom == 0 else _clamp((wx * vx + wy * vy) / denom)
    return math.hypot(wx - t * vx, wy - t * vy) - radius


def _blend(dst, src, alpha: float):
    """source-over 合成（保留正确的预乘边缘，避免透明角发黑）。"""
    if alpha <= 0:
        return dst
    dr, dg, db, da = dst
    sr, sg, sb = src
    out_a = alpha + da * (1.0 - alpha)
    if out_a <= 0:
        return (0.0, 0.0, 0.0, 0.0)
    out_r = (sr * alpha + dr * da * (1.0 - alpha)) / out_a
    out_g = (sg * alpha + dg * da * (1.0 - alpha)) / out_a
    out_b = (sb * alpha + db * da * (1.0 - alpha)) / out_a
    return (out_r, out_g, out_b, out_a)


def draw(size: int) -> bytes:
    """画一张 size×size 的 RGBA 图，返回裸像素。"""
    s = float(size)
    px = [[(0.0, 0.0, 0.0, 0.0) for _ in range(size)] for _ in range(size)]
    inset = max(0.55, s * 0.055)
    half = s / 2.0 - inset
    radius = max(1.35, s * 0.22)
    # W 的五个折点：起笔高、两个谷、收笔高。中间谷略高，读起来才是 W。
    pts = [(s * 0.275, s * 0.360), (s * 0.385, s * 0.665),
           (s * 0.500, s * 0.465), (s * 0.615, s * 0.665),
           (s * 0.725, s * 0.360)]
    stroke = max(0.68, s * 0.076) / 2.0
    dot_r = max(0.60, s * 0.043)
    dot = (s * 0.760, s * 0.258)

    for y in range(size):
        for x in range(size):
            cx, cy = x + 0.5, y + 0.5
            dst = (0.0, 0.0, 0.0, 0.0)
            d = _rounded_box(cx, cy, s / 2.0, s / 2.0, half, radius)
            dst = _blend(dst, TILE, _coverage(d))
            # 1px 内描边：让近白底在白页面上也有清楚的圆角边界。
            edge = abs(d + 0.5) - 0.5
            dst = _blend(dst, EDGE, _coverage(edge) * 0.78)
            cov = 0.0
            for (ax, ay), (bx, by) in zip(pts, pts[1:]):
                cov = max(cov, _coverage(
                    _capsule(cx, cy, ax, ay, bx, by, stroke)))
            dst = _blend(dst, INK, cov)
            dd = math.hypot(cx - dot[0], cy - dot[1]) - dot_r
            dst = _blend(dst, ACCENT, _coverage(dd))
            r, g, b, a = dst
            px[y][x] = (round(r), round(g), round(b), round(a * 255))

    raw = bytearray()
    for row in px:
        for r, g, b, a in row:
            raw += bytes((r, g, b, a))
    return bytes(raw)


def _dib_frame(size: int) -> bytes:
    """把 RGBA 帧封装成 ICO 里的 32 位 DIB（含 AND mask）。"""
    rgba = draw(size)
    header = struct.pack(
        "<IiiHHIIiiII", 40, size, size * 2, 1, 32, 0,
        size * size * 4, 0, 0, 0, 0)
    xor = bytearray()
    for y in range(size - 1, -1, -1):
        row = rgba[y * size * 4:(y + 1) * size * 4]
        for x in range(size):
            r, g, b, a = row[x * 4:x * 4 + 4]
            xor += bytes((b, g, r, a))
    stride = ((size + 31) // 32) * 4
    mask = bytearray(stride * size)
    for y in range(size):
        src_y = size - 1 - y
        for x in range(size):
            if rgba[(src_y * size + x) * 4 + 3] == 0:
                mask[y * stride + (x // 8)] |= 1 << (7 - (x % 8))
    return header + bytes(xor) + bytes(mask)


def main():
    frames = []
    for size in SIZES:
        data = _dib_frame(size)
        frames.append((size, data))
        print(f"  已绘制 {size}×{size}（DIB {len(data)} 字节）")

    # ICO 容器：ICONDIR + 每个图像的 ICONDIRENTRY（16 字节）+ 图像数据
    # 结构（小端）：
    #   ICONDIR   : reserved(2)=0 type(2)=1 count(2)
    #   ICONDIRENTRY: width(1) height(1) colors(1) reserved(1)
    #                 planes(2) bitCount(2) bytesInRes(4) imageOffset(4)
    count = len(frames)
    header = struct.pack("<HHH", 0, 1, count)
    offset = len(header) + 16 * count
    entries = b""
    blobs = b""
    for size, data in frames:
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
