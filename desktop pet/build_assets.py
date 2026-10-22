# -*- coding: utf-8 -*-
"""
开发者工具：从三张基础视图生成桌宠的镜像/眨眼帧。

**运行时不依赖 Pillow**；这个脚本只在重新生成素材时手动跑一次。
生成结果直接写回 `assets/<高度>/`，因此打包/运行仍然只需要标准库。

用法（需要 Pillow）：
    python "desktop pet/build_assets.py"

生成内容：
  · `side_left.png`      —— `side.png` 的水平镜像（向右走时用）；
  · `front_blink.png`    —— 正面闭眼；
  · `side_blink.png`     —— 侧面闭眼；
  · `side_left_blink.png`—— 侧面闭眼镜像。

眼睛位置按 306px 原图标注，再按各档位等比缩放。闭眼帧是**在眼睛上覆盖
一块与眼周肤色一致的椭圆，再画一条下垂的睫毛线**；不是重新绘制角色。
"""
from __future__ import annotations

import os
import sys

try:
    from PIL import Image, ImageDraw
except ImportError:                                             # pragma: no cover
    sys.exit("需要 Pillow 才能重新生成素材：pip install pillow")

HERE = os.path.dirname(os.path.abspath(__file__))
ASSETS = os.path.join(HERE, "assets")
HEIGHTS = (187, 238, 306)
#: 306px 原图上的眼睛外框：(x0, y0, x1, y1)
FRONT_EYES_306 = ((60, 112, 108, 166), (120, 112, 164, 166))
SIDE_EYE_306 = (22, 110, 65, 152)
#: 眼周肤色与睫毛颜色（从原图采样）
EYELID = (246, 226, 220, 255)
LASH = (74, 54, 66, 255)


def _load(path: str) -> Image.Image:
    return Image.open(path).convert("RGBA")


def _binary_alpha(image: Image.Image) -> Image.Image:
    """Tk 的透明色是颜色键，不是逐像素 alpha；边缘必须二值化。"""
    alpha = image.getchannel("A").point(lambda value: 255 if value >= 128 else 0)
    image.putalpha(alpha)
    return image


def _blink(source: Image.Image, scaled_boxes) -> Image.Image:
    """在眼睛位置画闭眼：肤色椭圆 + 下垂睫毛线。"""
    out = source.copy()
    draw = ImageDraw.Draw(out)
    for x0, y0, x1, y1 in scaled_boxes:
        cx, cy = (x0 + x1) / 2.0, (y0 + y1) / 2.0
        rx = max(4.0, (x1 - x0) / 2.0 * 0.78)
        ry = max(4.0, (y1 - y0) / 2.0 * 0.68)
        draw.ellipse((cx - rx, cy - ry, cx + rx, cy + ry), fill=EYELID)
        # 向下弯的闭眼线：中间低、两端高
        points = []
        for step in range(15):
            t = step / 14.0
            x = cx - rx + 2 + (2 * rx - 4) * t
            y = cy - 2 + (ry * 0.45) * (1 - (2 * t - 1) ** 2)
            points.append((x, y))
        draw.line(points, fill=LASH, width=max(2, int(round(rx / 7))))
        # 眼尾的小睫毛，保留原画风格
        draw.line((cx - rx + 2, cy - 2, cx - rx - 4, cy - ry * 0.55),
                  fill=LASH, width=max(1, int(round(rx / 9))))
        draw.line((cx + rx - 2, cy - 2, cx + rx + 4, cy - ry * 0.55),
                  fill=LASH, width=max(1, int(round(rx / 9))))
    return _binary_alpha(out)


def _scale_box(box, sx: float, sy: float):
    x0, y0, x1, y1 = box
    return (round(x0 * sx), round(y0 * sy), round(x1 * sx), round(y1 * sy))


def build_height(height: int) -> None:
    folder = os.path.join(ASSETS, str(height))
    front_path = os.path.join(folder, "front.png")
    side_path = os.path.join(folder, "side.png")
    if not (os.path.isfile(front_path) and os.path.isfile(side_path)):
        print(f"跳过 {height}：缺少 front/side")
        return
    front = _load(front_path)
    side = _load(side_path)

    # 侧面镜像：原仓库的 side_left 与 side 完全相同，向左/向右走会“倒着走”。
    side_left = side.transpose(Image.Transpose.FLIP_LEFT_RIGHT)
    side_left.save(os.path.join(folder, "side_left.png"), optimize=True)

    front_sx, front_sy = front.width / 229.0, front.height / 306.0
    side_sx, side_sy = side.width / 200.0, side.height / 306.0
    front_blink = _blink(front, [_scale_box(box, front_sx, front_sy)
                                 for box in FRONT_EYES_306])
    side_blink = _blink(side, [_scale_box(SIDE_EYE_306, side_sx, side_sy)])
    side_left_blink = side_blink.transpose(Image.Transpose.FLIP_LEFT_RIGHT)

    front_blink.save(os.path.join(folder, "front_blink.png"), optimize=True)
    side_blink.save(os.path.join(folder, "side_blink.png"), optimize=True)
    side_left_blink.save(os.path.join(folder, "side_left_blink.png"),
                         optimize=True)
    print(f"{height}: front_blink / side_blink / side_left_blink / side_left")


def main() -> int:
    for height in HEIGHTS:
        build_height(height)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
