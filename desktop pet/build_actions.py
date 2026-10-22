# -*- coding: utf-8 -*-
"""
把大肥鱼的透明 WebM 动作片段压成 WinMon 运行时用的条带图（构建工具，非运行时依赖）。

运行前提（只在开发机需要）：
  · Python 3.10+、Pillow；
  · ffmpeg 带 libvpx-vp9 解码器（能读 VP9-alpha WebM）。

示例：
  py -3.12 "desktop pet/build_actions.py" ^
      --source "C:\\path\\to\\big-blue-fish\\assets\\fish" ^
      --ffmpeg "C:\\path\\to\\ffmpeg.exe"

输出：
  desktop pet/actions/manifest.json
  desktop pet/actions/<id>/strip_00.png ...
  每 10 帧拼成一条横向条带；96 色调色板 + alpha，10 FPS，高度 300。

运行时只用标准库 + GDI+：`winmon_pet.py` 读 manifest，不调用 ffmpeg/Pillow。
"""
from __future__ import annotations

import argparse
import io
import json
import os
import shutil
import subprocess
import sys

try:
    from PIL import Image
except Exception as exc:  # pragma: no cover - 构建机才会走到
    print(f"需要 Pillow 才能构建动作素材：{exc}", file=sys.stderr)
    raise SystemExit(2)

#: 顺序就是右键菜单的分组顺序；id 是运行时唯一键，source 是素材文件名。
CLIPS = (
    {"id": "idle", "source": "待机呼吸休闲.webm", "label": "呼吸 / 待机",
     "group": "待机", "loop": True},
    {"id": "look", "source": "东张西望.webm", "label": "东张西望",
     "group": "待机", "loop": False},
    {"id": "sleep", "source": "原地小憩沉眠.webm", "label": "小憩",
     "group": "待机", "loop": True},
    {"id": "wake", "source": "打瞌睡被惊醒.webm", "label": "惊醒",
     "group": "待机", "loop": False},

    {"id": "wave", "source": "点击回应-元气挥手.webm", "label": "元气挥手",
     "group": "点击回应", "loop": False},
    {"id": "joy", "source": "点击回应-开心跃动.webm", "label": "开心跃动",
     "group": "点击回应", "loop": False},
    {"id": "shy", "source": "点击回应-害羞惊讶.webm", "label": "害羞惊讶",
     "group": "点击回应", "loop": False},
    {"id": "pout", "source": "点击回应-傲娇生气.webm", "label": "傲娇生气",
     "group": "点击回应", "loop": False},

    {"id": "think", "source": "工作状态-思考冒泡.webm", "label": "思考冒泡",
     "group": "工作状态", "loop": True},
    {"id": "busy", "source": "工作状态-忙碌点按.webm", "label": "忙碌点按",
     "group": "工作状态", "loop": True},
    {"id": "tidy", "source": "工作状态-清点归档.webm", "label": "清点归档",
     "group": "工作状态", "loop": True},
    {"id": "celebrate", "source": "工作状态-雀跃庆祝.webm", "label": "雀跃庆祝",
     "group": "工作状态", "loop": False},
    {"id": "sigh", "source": "工作状态-垂头叹气冒汗.webm", "label": "垂头叹气",
     "group": "工作状态", "loop": False},

    {"id": "talk", "source": "碎碎念-对屏碎碎念.webm", "label": "碎碎念",
     "group": "说话", "loop": True},

    {"id": "stretch", "source": "超大伸懒腰.webm", "label": "伸个懒腰",
     "group": "日常", "loop": False},
    {"id": "cat", "source": "撸猫.webm", "label": "撸猫",
     "group": "日常", "loop": False},
    {"id": "spin", "source": "小幅度原地360度旋转展示.webm", "label": "转个圈",
     "group": "日常", "loop": False},
    {"id": "bubble", "source": "鲸鱼吐泡泡特效.webm", "label": "吐泡泡",
     "group": "日常", "loop": False},

    {"id": "maid_dance", "source": "优雅女仆舞.webm", "label": "优雅女仆舞",
     "group": "舞蹈", "loop": True},
    {"id": "sway_dance", "source": "轻快摇摆舞.webm", "label": "轻快摇摆舞",
     "group": "舞蹈", "loop": True},
    {"id": "curtsy", "source": "女仆屈膝礼仪.webm", "label": "屈膝礼仪",
     "group": "舞蹈", "loop": False},
)


def _ffmpeg(path: str) -> str:
    if path:
        if not os.path.isfile(path):
            raise SystemExit(f"ffmpeg 不存在：{path}")
        return path
    found = shutil.which("ffmpeg")
    if not found:
        raise SystemExit(
            "找不到 ffmpeg。用 --ffmpeg 指向带 libvpx-vp9 的 ffmpeg.exe。")
    return found


def _decode(ffmpeg: str, source: str, fps: int) -> tuple[bytes, int, int]:
    """用 libvpx-vp9 解码 VP9-alpha；不指定它会丢掉 alpha。"""
    cmd = [
        ffmpeg, "-v", "error", "-c:v", "libvpx-vp9", "-i", source,
        "-vf", f"fps={fps},scale=640:-2:flags=lanczos",
        "-c:v", "rawvideo", "-pix_fmt", "rgba", "-f", "rawvideo", "-",
    ]
    proc = subprocess.run(cmd, capture_output=True)
    if proc.returncode != 0:
        raise RuntimeError(
            f"ffmpeg 解码失败：{os.path.basename(source)}\n"
            f"{proc.stderr.decode('utf-8', 'replace')[:400]}")
    raw = proc.stdout
    if len(raw) < 640 * 2 * 4:
        raise RuntimeError(f"解码结果为空：{os.path.basename(source)}")
    # 640 宽、保持比例；绝大多数源是 640×360。
    height = max(2, round(640 * 9 / 16))
    if len(raw) % (640 * height * 4) != 0:
        # 源不是 16:9：从第一帧的尺寸反推。ffmpeg 的 scale=640:-2
        # 只保证宽 640；这里用总字节数找一个合理高度。
        for candidate in range(240, 1201, 2):
            if len(raw) % (640 * candidate * 4) == 0:
                height = candidate
                break
        else:
            raise RuntimeError("无法推断解码画面高度")
    return raw, 640, height


def _frame_bytes(raw: bytes, width: int, height: int):
    stride = width * height * 4
    for offset in range(0, len(raw) - stride + 1, stride):
        yield raw[offset:offset + stride]


def _build_clip(ffmpeg: str, source: str, out_dir: str, clip: dict,
                fps: int, height: int, colors: int, per_strip: int) -> dict:
    raw, width, src_h = _decode(ffmpeg, source, fps)
    frames = []
    bbox = None
    for chunk in _frame_bytes(raw, width, src_h):
        image = Image.frombuffer("RGBA", (width, src_h), chunk,
                                 "raw", "RGBA", 0, 1).copy()
        # VP9 alpha 的背景不是精确 0（常见 0–2），低 alpha 噪声要压掉；
        # 8 以上保留，泡泡/烟雾的软边还在。
        image.putalpha(image.getchannel("A").point(
            lambda value: 0 if value <= 8 else value))
        box = image.getbbox()
        if box:
            bbox = box if bbox is None else (
                min(bbox[0], box[0]), min(bbox[1], box[1]),
                max(bbox[2], box[2]), max(bbox[3], box[3]))
        frames.append(image)
    if not frames or bbox is None:
        raise RuntimeError(f"没有可见像素：{clip['source']}")

    x0, y0, x1, y1 = bbox
    pad = 2
    x0, y0 = max(0, x0 - pad), max(0, y0 - pad)
    x1, y1 = min(width, x1 + pad), min(src_h, y1 + pad)
    scaled = []
    for image in frames:
        cropped = image.crop((x0, y0, x1, y1))
        out_w = max(1, round(cropped.width * height / cropped.height))
        scaled.append(cropped.resize((out_w, height), Image.LANCZOS))

    canvas_w = max(image.width for image in scaled) + 4
    strips = []
    for start in range(0, len(scaled), per_strip):
        group = scaled[start:start + per_strip]
        strip = Image.new("RGBA", (canvas_w * len(group), height),
                          (0, 0, 0, 0))
        for index, image in enumerate(group):
            strip.alpha_composite(image, (index * canvas_w, 0))
        quantized = strip.quantize(colors=colors,
                                   method=Image.Quantize.FASTOCTREE)
        name = f"strip_{len(strips):02d}.png"
        path = os.path.join(out_dir, name)
        quantized.save(path, "PNG", optimize=True)
        strips.append(f"{clip['id']}/{name}")

    return {
        "label": clip["label"],
        "group": clip["group"],
        "source": clip["source"],
        "strips": strips,
        "frames": len(scaled),
        "width": canvas_w,
        "height": height,
        "fps": fps,
        "per_strip": per_strip,
        "loop": bool(clip["loop"]),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", required=True,
                        help="大肥鱼素材目录（含 *.webm）")
    parser.add_argument("--ffmpeg", default="",
                        help="ffmpeg.exe；默认从 PATH 找")
    parser.add_argument("--out", default=os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "actions"))
    parser.add_argument("--fps", type=int, default=10)
    parser.add_argument("--height", type=int, default=300)
    parser.add_argument("--colors", type=int, default=96)
    parser.add_argument("--per-strip", type=int, default=10)
    parser.add_argument("--only", default="",
                        help="只构建逗号分隔的 id（调试用）")
    args = parser.parse_args()

    source_dir = os.path.abspath(args.source)
    if not os.path.isdir(source_dir):
        raise SystemExit(f"素材目录不存在：{source_dir}")
    ffmpeg = _ffmpeg(args.ffmpeg)
    out_dir = os.path.abspath(args.out)
    os.makedirs(out_dir, exist_ok=True)
    only = {part.strip() for part in str(args.only or "").split(",")
            if part.strip()}
    selected = [c for c in CLIPS if not only or c["id"] in only]
    if not selected:
        raise SystemExit("没有选中任何动作")

    manifest = {
        "version": 1,
        "fps": args.fps,
        "height": args.height,
        "colors": args.colors,
        "per_strip": args.per_strip,
        "groups": [],
        "clips": {},
    }
    group_order = []
    for clip in selected:
        if clip["group"] not in group_order:
            group_order.append(clip["group"])
        source = os.path.join(source_dir, clip["source"])
        if not os.path.isfile(source):
            raise SystemExit(f"缺少素材：{source}")
        clip_dir = os.path.join(out_dir, clip["id"])
        os.makedirs(clip_dir, exist_ok=True)
        print(f"[{clip['id']}] {clip['source']} ...", flush=True)
        meta = _build_clip(ffmpeg, source, clip_dir, clip, args.fps,
                           args.height, args.colors, args.per_strip)
        manifest["clips"][clip["id"]] = meta
        print(f"  {meta['frames']} 帧 / {len(meta['strips'])} 条 "
              f"{meta['width']}×{meta['height']}", flush=True)

    for group in group_order:
        manifest["groups"].append({
            "id": group,
            "label": group,
            "clips": [c["id"] for c in selected if c["group"] == group],
        })

    manifest_path = os.path.join(out_dir, "manifest.json")
    with open(manifest_path, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(manifest, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    total = 0
    for root, _dirs, files in os.walk(out_dir):
        total += sum(os.path.getsize(os.path.join(root, name))
                     for name in files)
    print(f"完成：{len(manifest['clips'])} 段，"
          f"{total / 1048576:.1f} MB → {out_dir}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
