# -*- coding: utf-8 -*-
"""
WinMon 桌面宠物（可选）
======================

一只会在桌面上活动的「鲸鱼娘 · 大肥鱼」：点一下弹出信息气泡，
拖到哪儿就待在哪，右键出菜单；不抢焦点、不写注册表、不联网。

为什么单独一个模块
-----------------
`winmon.py` 已经很大；桌宠的窗口/动画/命中测试和主界面是两块独立的心智负担。
这里只依赖标准库 `tkinter` + `ctypes`，不 import `winmon.py`，便于单测和以后复用。

透明怎么做（**Windows 专有**）
---------------------------
Tk 本身没有逐像素透明窗口；`-transparentcolor` 只能把一种颜色键掉，窗口
仍是矩形，DWM 在移动/合成时可能补一圈阴影或黑边。这里参考 dsh-pet 的
「透明置顶小窗」做法，改用 **GDI+ 32 位预乘 alpha + UpdateLayeredWindow**：

  · Tk 只负责窗口和事件，不再用 Canvas 画精灵；
  · 每帧把精灵和粒子画进 BGRA DIB，再由 `UpdateLayeredWindow` 提交；
  · 没有颜色键、没有 `SetWindowRgn`、没有 DWM 阴影；
  · Windows 按 alpha 做命中测试，透明像素点击穿透，只有身体能点。

窗口本身还会去掉边框、阴影和圆角（DWM），并把画布留白压到最小；
如果系统仍给弹出窗加装饰，`_strip_window_frame()` 会再清一遍。
Tk 的客户区子窗口会在移动时重画底色，`ChildPaintSuppressor` 会吞掉
它的 `WM_PAINT`/`WM_ERASEBKGND`，否则精灵上会盖一层品红矩形。

坐标约定
--------
所有 `x/y` 都是**屏幕物理像素**；窗口高度由 `SIZE_PRESETS` 给出。
默认落在工作区右下角（任务栏上方），并夹在屏幕内。

动作
----
本地行为不调用模型：呼吸、眨眼、左右看、跳一下、散步、发呆、睡着，
以及在音乐/视频/点击时吐小泡泡。`perform()` 给以后的 Agent 留了
`idle / walk / watch / sleep / bubble / hop` 词表；Agent 只影响表现，
不获得关窗、结束进程等系统能力。
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
import json
import math
import os
import random
import time
import tkinter as tk

from pet_layered import (
    ChildPaintSuppressor,
    LayerImage,
    LayeredSurface,
    LayeredSurfaceError,
    rgb,
)

#: 尺寸档位：标签 → 精灵高度（物理像素）
SIZE_PRESETS: tuple[tuple[str, int], ...] = (("小", 187), ("中", 238), ("大", 306))
DEFAULT_SIZE = "中"
#: 默认窗口相对屏幕右下角的留白
EDGE_MARGIN = 28
#: 动画节拍（毫秒）。16ms ≈ 60 FPS；动作在下一个 vsync 就能看见。
TICK_MS = 16
#: 画布左右 / 上方留白：给摇摆和跳跃用。透明，不会画出来。
PAD_X = 4
PAD_TOP = 18
#: 活泼度：标签 → 速度倍率（影响走路、眨眼、发呆的频率，不影响安全边界）
MOTION_PRESETS: tuple[tuple[str, float], ...] = (
    ("安静", 0.55), ("标准", 1.0), ("活泼", 1.45))
DEFAULT_MOTION = "标准"
#: 系统键鼠闲置多久后允许睡着（秒）
SLEEP_AFTER = 150.0
#: 闭眼持续时间（秒）
BLINK_SECONDS = 0.14


def project_dir() -> str:
    return os.path.dirname(os.path.abspath(__file__))


def asset_dir() -> str:
    # 桌宠的代码、素材、说明、截图都在 `desktop pet/` 下；
    # `project_dir()` 就是这一层，所以素材是 `desktop pet/assets/`。
    return os.path.join(project_dir(), "assets")


def sprite_path(height: int, name: str) -> str:
    return os.path.join(asset_dir(), str(int(height)), f"{name}.png")


def action_dir() -> str:
    """21 段动作条带图与 manifest 的目录（构建产物，运行时只读）。"""
    return os.path.join(project_dir(), "actions")


def available_heights() -> tuple[int, ...]:
    return tuple(h for _label, h in SIZE_PRESETS
                 if os.path.isfile(sprite_path(h, "front")))


def height_for_choice(choice: str) -> int:
    """「小 / 中 / 大」→ 精灵高度；不认识的值退回默认档。"""
    text = str(choice or "").strip()
    for label, height in SIZE_PRESETS:
        if text == label:
            return height
    return dict(SIZE_PRESETS)[DEFAULT_SIZE]


def choice_for_height(height: int) -> str:
    """高度 → 最接近的档位标签（用于把旧设置里的数字归一）。"""
    try:
        value = int(height)
    except (TypeError, ValueError):
        return DEFAULT_SIZE
    return min(SIZE_PRESETS, key=lambda item: abs(item[1] - value))[0]


def normalize_motion(value: str) -> str:
    """把旧设置/英文/空值归一成三档活泼度之一。"""
    text = str(value or "").strip()
    return text if text in dict(MOTION_PRESETS) else DEFAULT_MOTION


def motion_factor(value: str) -> float:
    return float(dict(MOTION_PRESETS)[normalize_motion(value)])


def clamp_position(x: int, y: int, width: int, height: int,
                   left: int = 0, top: int = 0,
                   right: int = 1920, bottom: int = 1080) -> tuple[int, int]:
    """
    把窗口夹在可见区域内（纯函数，可单测）。

    不要求整个窗口都在屏幕内 —— 只保证左上角不越界、右下角不出去。
    屏幕比窗口还小时优先保住左上角。
    """
    width = max(1, int(width))
    height = max(1, int(height))
    max_x = max(int(left), int(right) - width)
    max_y = max(int(top), int(bottom) - height)
    return (min(max(int(x), int(left)), max_x),
            min(max(int(y), int(top)), max_y))


def work_area() -> tuple[int, int, int, int]:
    """
    桌面工作区（不含任务栏），`(left, top, right, bottom)`。

    读不到就退回整块屏幕；全部失败给 0/0/1920/1080，保证调用方永远拿到四个数。
    """
    try:
        rect = wintypes.RECT()
        SPI_GETWORKAREA = 0x0030
        if ctypes.windll.user32.SystemParametersInfoW(
                SPI_GETWORKAREA, 0, ctypes.byref(rect), 0):
            return (int(rect.left), int(rect.top),
                    int(rect.right), int(rect.bottom))
    except Exception:
        pass
    try:
        root = tk._default_root
        if root is not None:
            return (0, 0, int(root.winfo_screenwidth()),
                    int(root.winfo_screenheight()))
    except Exception:
        pass
    return (0, 0, 1920, 1080)


def seconds_since_last_input() -> float:
    """
    系统键鼠闲置秒数；任何失败都返回 0（宁可多动，不要误睡）。

    用 `GetLastInputInfo` 而不是自己记鼠标事件：用户在其他程序里打字时，
    桌宠也应该知道“有人在用电脑”，而不是因为没点它就去睡觉。
    """
    class LASTINPUTINFO(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]

    try:
        info = LASTINPUTINFO()
        info.cbSize = ctypes.sizeof(info)
        user32 = ctypes.windll.user32
        if not user32.GetLastInputInfo(ctypes.byref(info)):
            return 0.0
        tick = int(user32.GetTickCount())
        elapsed = (tick - int(info.dwTime)) & 0xFFFFFFFF
        return max(0.0, elapsed / 1000.0)
    except Exception:
        return 0.0


def default_position(width: int, height: int,
                     area: tuple[int, int, int, int] | None = None) -> tuple[int, int]:
    """默认位置：工作区右下角，留 `EDGE_MARGIN` 给任务栏和屏幕边。"""
    left, top, right, bottom = area or work_area()
    return clamp_position(right - width - EDGE_MARGIN,
                          bottom - height - EDGE_MARGIN,
                          width, height, left, top, right, bottom)


def avatar_image(master: tk.Misc, size: int = 52) -> tk.PhotoImage:
    """气泡左上角的小头像；调用方必须自己保留返回的引用。"""
    img = tk.PhotoImage(master=master, file=os.path.join(asset_dir(), "avatar.png"))
    factor = max(1, round(img.width() / max(16, int(size))))
    if factor > 1:
        img = img.subsample(factor, factor)
    return img


class ActionLibrary:
    """
    动作条带图库：读 `desktop pet/actions/manifest.json`，按需加载一段动作。

    每段动作是若干张横向条带 PNG（默认 10 帧/条），运行时只保留**当前一段**
    的条带；切到下一段时释放上一段，避免 21 段 × 100 帧全部驻留内存。
    只读本地文件，不联网、不依赖 ffmpeg/Pillow。
    """

    def __init__(self, root: str = "") -> None:
        self.root = os.path.abspath(root or action_dir())
        self.surface = None
        self._manifest: dict = {}
        self._clips: dict[str, dict] = {}
        self._groups: list[dict] = []
        self._loaded = ""
        self._meta: dict = {}
        self._images: dict[int, LayerImage] = {}
        self._strip_paths: list[str] = []
        self._read_manifest()

    def _read_manifest(self) -> None:
        path = os.path.join(self.root, "manifest.json")
        try:
            with open(path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, ValueError, TypeError):
            return
        if not isinstance(data, dict):
            return
        clips = data.get("clips")
        if not isinstance(clips, dict):
            return
        for clip_id, meta in clips.items():
            if not isinstance(meta, dict):
                continue
            strips = meta.get("strips")
            if not isinstance(strips, list) or not strips:
                continue
            try:
                frames = int(meta.get("frames", 0))
                width = int(meta.get("width", 0))
                height = int(meta.get("height", 0))
                per_strip = int(meta.get("per_strip", 10))
                fps = float(meta.get("fps", 0))
            except (TypeError, ValueError):
                continue
            if frames <= 0 or width <= 0 or height <= 0 or per_strip <= 0:
                continue
            item = dict(meta)
            item.update({"frames": frames, "width": width, "height": height,
                         "per_strip": per_strip, "fps": fps or 10.0,
                         "label": str(meta.get("label") or clip_id)})
            self._clips[str(clip_id)] = item
        groups = data.get("groups")
        if isinstance(groups, list):
            for group in groups:
                if not isinstance(group, dict):
                    continue
                ids = [str(cid) for cid in group.get("clips", [])
                       if str(cid) in self._clips]
                if ids:
                    self._groups.append({
                        "id": str(group.get("id") or group.get("label") or ""),
                        "label": str(group.get("label") or group.get("id") or ""),
                        "clips": ids,
                    })
        self._manifest = data

    # ---- 查询 -------------------------------------------------------

    def available(self) -> bool:
        return bool(self._clips)

    def has(self, clip_id: str) -> bool:
        return str(clip_id or "") in self._clips

    def meta(self, clip_id: str) -> dict:
        return self._clips.get(str(clip_id or ""), {})

    def label(self, clip_id: str) -> str:
        return str(self.meta(clip_id).get("label") or clip_id)

    def groups(self) -> list[dict]:
        if self._groups:
            return [dict(group) for group in self._groups]
        return [{"id": "all", "label": "动作", "clips": list(self._clips)}]

    def duration(self, clip_id: str) -> float:
        meta = self.meta(clip_id)
        if not meta:
            return 0.0
        return float(meta["frames"]) / max(0.1, float(meta["fps"]))

    def max_width(self, target_height: int) -> int:
        """所有动作按目标高度缩放后的最大宽度（窗口要装得下，不能裁边）。"""
        if not self._clips:
            return 0
        target = max(1, int(target_height))
        return max(
            int(round(int(meta["width"]) * target / max(1, int(meta["height"]))))
            for meta in self._clips.values()
        )

    # ---- 加载 / 释放 ------------------------------------------------

    def load(self, clip_id: str) -> bool:
        clip_id = str(clip_id or "")
        meta = self._clips.get(clip_id)
        if meta is None:
            return False
        if self._loaded == clip_id and self._strip_paths:
            return True
        self.release()
        if self.surface is None:
            return False
        paths = []
        for rel in meta["strips"]:
            path = os.path.join(self.root, str(rel).replace("/", os.sep))
            if not os.path.isfile(path):
                return False
            paths.append(path)
        self._loaded = clip_id
        self._meta = meta
        self._strip_paths = paths
        #: 条带按需解码：刚创建桌宠时只解码第 0 条，测试/启动更快；
        #: 播到第 10 帧再读下一条，10 秒后才占满一段动作的内存。
        return True

    def release(self) -> None:
        clip_id = self._loaded
        self._loaded = ""
        self._meta = {}
        self._images = {}
        self._strip_paths = []
        if clip_id and self.surface is not None:
            try:
                self.surface.drop_images(os.path.join(self.root, clip_id))
            except Exception:
                pass

    # ---- 取帧 -------------------------------------------------------

    def frame(self, index: int):
        """返回 `(image, src_x, src_y, src_w, src_h)`；没加载时返回 None。"""
        meta = self._meta
        if not meta or not self._strip_paths or self.surface is None:
            return None
        frames = int(meta["frames"])
        per = max(1, int(meta["per_strip"]))
        index = max(0, int(index)) % max(1, frames)
        strip_index, frame_index = divmod(index, per)
        if strip_index >= len(self._strip_paths):
            return None
        image = self._images.get(strip_index)
        if image is None:
            try:
                image = self.surface.image(self._strip_paths[strip_index])
            except Exception:
                return None
            self._images[strip_index] = image
        return (image, frame_index * int(meta["width"]),
                0, int(meta["width"]), int(meta["height"]))


class DesktopPet:
    """
    无边框、置顶、透明底的桌宠窗口。

    参数
    ----
    parent : Tk 根窗口/Toplevel
    height : 精灵高度（187/238/306）；不认识时用默认档
    x, y   : 初始左上角（屏幕物理像素）；None 表示右下角
    motion : 安静 / 标准 / 活泼
    on_click / on_menu / on_double / on_moved : 回调，全部在主线程调用
    """

    def __init__(self, parent: tk.Misc, *, height: int = 238,
                 x: int | None = None, y: int | None = None,
                 motion: str = DEFAULT_MOTION,
                 on_click=None, on_menu=None, on_double=None,
                 on_moved=None) -> None:
        self._parent = parent
        self._on_click = on_click if callable(on_click) else None
        self._on_menu = on_menu if callable(on_menu) else None
        self._on_double = on_double if callable(on_double) else None
        self._on_moved = on_moved if callable(on_moved) else None
        self._height = int(height)
        self._motion = normalize_motion(motion)
        self._speed = motion_factor(self._motion)
        self._alive = True
        self._paused = False
        self._after_id = None
        self._click_after = None
        self._dragging = False
        self._moved = False
        self._drag_offset = (0, 0)
        self._drag_sparked = False
        self._hop_until = 0.0
        self._watch_until = 0.0
        self._spin_until = 0.0
        self._spin_next = 0.0
        self._spin_side = "side"
        self._blink_until = 0.0
        self._look_until = 0.0
        self._look_sprite = "side"
        self._mode = "idle"
        self._manual_sleep = False
        self._ambient = ""
        self._ambient_since = 0.0
        self._music_next = 0.0
        self._next_zzz = 0.0
        now = time.monotonic()
        self._now = now
        self._last_tick = now
        self._last_interaction = now
        self._next_blink = now + random.uniform(1.2, 3.0)
        self._next_action = now + random.uniform(1.5, 3.5)
        self._next_walk = now + random.uniform(3.0, 6.0)
        self._walk_remaining = 0
        self._walk_target = (0, 0)
        self._walk_step = 4
        # 60 FPS 下把“每 50ms 的步长”换成小数累积，避免速度变成原来的 3 倍。
        self._walk_accum = 0.0
        self._last_save = 0.0
        self._rng = random.Random()
        self._particles: list[dict] = []
        self._sprite = "front"
        self._hwnd = 0
        self._surface: LayeredSurface | None = None
        self._paint_guard: ChildPaintSuppressor | None = None
        self._frame_count = 0
        self._render_error = ""
        self._last_render_args = ("front", 0, 0)

        self.top = tk.Toplevel(parent)
        self.top.withdraw()
        self.top.overrideredirect(True)
        # 这个底色永远不该被看见：画面由 UpdateLayeredWindow 提供，
        # Tk 客户区的 WM_PAINT 由 ChildPaintSuppressor 吞掉。
        self.top.configure(bg="#101010")
        try:
            self.top.attributes("-topmost", True)
        except tk.TclError:
            pass
        self.top.protocol("WM_DELETE_WINDOW", self.destroy)

        self._images: dict[str, LayerImage] = {}
        self._actions = ActionLibrary()
        self._clip_id = ""
        self._clip_elapsed = 0.0
        self._clip_loop = False
        self._w, self._h = 1, 1
        self._x = int(x or 0)
        self._y = int(y or 0)
        try:
            self._make_tool_window()
            self._load_height(self._height, keep_bottom=False, x=x, y=y)

            self.top.configure(cursor="hand2")
            self.top.bind("<ButtonPress-1>", self._on_press, add="+")
            self.top.bind("<B1-Motion>", self._on_drag, add="+")
            self.top.bind("<ButtonRelease-1>", self._on_release, add="+")
            self.top.bind("<Double-Button-1>", self._on_double_click, add="+")
            self.top.bind("<Button-3>", self._on_right_click, add="+")
            self.top.bind("<Escape>", lambda _e: self.destroy(), add="+")
            self.show()
        except Exception:
            self._alive = False
            if self._surface is not None:
                try:
                    self._surface.close()
                except Exception:
                    pass
                self._surface = None
            if self._paint_guard is not None:
                try:
                    self._paint_guard.close()
                except Exception:
                    pass
                self._paint_guard = None
            try:
                self.top.destroy()
            except Exception:
                pass
            raise

    # ---- 生命周期 ----

    def show(self) -> None:
        if not self._alive:
            return
        try:
            self.top.deiconify()
            self.top.update_idletasks()
            self.top.lift()
        except tk.TclError:
            return
        # 窗口真正显示后再设一次：Tk 的 deiconify/lift 会让 Win11 把
        # 分层窗口重新套成圆角，早设的 DONOTROUND 会被覆盖。
        self._apply_dwm_frame(self._hwnd)
        self._last_tick = time.monotonic()
        self._render(self._bob(self._last_tick), self._sway(self._last_tick))
        self._schedule_tick()

    def hide(self) -> None:
        try:
            self.top.withdraw()
        except tk.TclError:
            pass

    def destroy(self) -> None:
        """停掉动画并销毁窗口；可重复调用。"""
        if not self._alive:
            return
        self._alive = False
        for attr in ("_after_id", "_click_after"):
            after_id = getattr(self, attr, None)
            if after_id:
                try:
                    self.top.after_cancel(after_id)
                except tk.TclError:
                    pass
                setattr(self, attr, None)
        self._clear_particles()
        try:
            self._actions.release()
        except Exception:
            pass
        if self._surface is not None:
            try:
                self._surface.close()
            except Exception:
                pass
            self._surface = None
        if self._paint_guard is not None:
            try:
                self._paint_guard.close()
            except Exception:
                pass
            self._paint_guard = None
        self._images = {}
        try:
            self.top.destroy()
        except tk.TclError:
            pass

    def set_paused(self, paused: bool) -> None:
        """暂停自动走动（气泡/菜单打开时用），呼吸和眨眼保留。"""
        self._paused = bool(paused)
        if self._paused:
            self._walk_remaining = 0
            self._walk_accum = 0.0
            if self._mode == "walk":
                self._mode = "idle"
        self._last_interaction = time.monotonic()

    def set_motion(self, motion: str) -> None:
        """切换活泼度：即时生效，不重建窗口。"""
        self._motion = normalize_motion(motion)
        self._speed = motion_factor(self._motion)
        now = time.monotonic()
        self._next_walk = now + self._rng.uniform(2.5, 5.5)
        self._next_action = now + self._rng.uniform(1.2, 2.8)
        self._next_blink = now + self._rng.uniform(1.0, 2.4)

    def set_ambient(self, kind: str, *, title: str = "") -> None:
        """
        告诉桌宠现在在放什么：`music` / `video` / 空字符串。

        主程序只传一个很粗的类别，不把曲名/窗口标题塞进动画层；
        桌宠据此决定“跟着音乐晃/认真看/不睡觉”。
        """
        kind = kind if kind in ("music", "video") else ""
        if kind == self._ambient:
            return
        self._ambient = kind
        self._ambient_since = time.monotonic()
        self._last_interaction = self._ambient_since
        if kind == "music":
            self._music_next = self._ambient_since + 0.4
            self._wake(self._ambient_since)
        elif kind == "video":
            self._sprite = "front"
            self._wake(self._ambient_since)
        else:
            self._next_walk = self._ambient_since + self._rng.uniform(4.0, 8.0)
        self._render(0)

    def move_to(self, x: int, y: int) -> None:
        """把窗口移到指定坐标（自动夹在屏幕内），并通知保存。"""
        self._walk_remaining = 0
        self._walk_accum = 0.0
        self._mode = "idle"
        self._move_window(int(x), int(y))
        self._notify_moved(force=True)

    def set_height(self, height: int) -> None:
        """换一档尺寸：保持窗口底边中心不动，不跑到屏幕外。"""
        height = int(height)
        if height == self._height and self._images:
            return
        cx = self._x + self._w // 2
        bottom = self._y + self._h
        self._walk_remaining = 0
        self._walk_accum = 0.0
        self._mode = "idle"
        self._load_height(height, keep_bottom=True, x=cx, y=bottom)
        self._notify_moved(force=True)

    def bounds(self) -> tuple[int, int, int, int]:
        """`(x, y, width, height)`，给气泡定位用。"""
        return self._x, self._y, self._w, self._h

    def perform(self, action: str, duration: float = 0.0) -> str:
        """
        执行一个桌宠动作（给本地规则/以后的 Agent 用）。

        返回实际执行的动作名；不认识的动作退回 `idle`，不抛异常。
        `say` 不在这里处理：说话是主程序气泡层的事，动画层不伪造台词。
        """
        name = str(action or "").strip().lower()
        now = time.monotonic()
        self._last_interaction = now
        if name == "hop":
            self._hop_until = now + max(0.35, float(duration or 0.5))
            self._spawn_particle("heart")
        elif name == "greet":
            self._hop_until = now + 0.6
            self._look_until = now + 1.0
            self._look_sprite = "side"
            for _ in range(2):
                self._spawn_particle("heart")
        elif name == "spin":
            self._spin_until = now + 0.9
            self._spin_next = 0.0
            self._spin_side = "side"
            self._hop_until = now + 0.45
        elif name == "stretch":
            self._hop_until = now + 0.8
            self._spawn_particle("sparkle")
        elif name == "look":
            self._look_until = now + 1.6
            self._look_sprite = self._rng.choice(("side", "side_left"))
        elif name == "wake":
            self._wake(now)
        elif name == "walk":
            self._start_walk(now, force=True)
        elif name == "watch":
            self._watch_until = now + max(1.0, float(duration or 6.0))
            self._wake(now)
        elif name == "sleep":
            self._enter_sleep(now, manual=True)
        elif name == "bubble":
            self._spawn_particle("bubble")
        elif name == "idle":
            self._wake(now)
        else:
            name = "idle"
            self._wake(now)
        self._last_interaction = now
        # 不等下一个 16ms tick：点完动作立刻提交一帧。
        self._render(self._bob(now), self._sway(now))
        return name

    # ---- 动作条带（21 段 WebM 的离线转码产物） --------------------

    def _play_clip(self, clip_id: str, *, loop: bool | None = None) -> bool:
        clip_id = str(clip_id or "")
        if not self._actions.available() or not self._actions.has(clip_id):
            return False
        if not self._actions.load(clip_id):
            return False
        if self._mode == "walk":
            self._walk_remaining = 0
            self._walk_accum = 0.0
            self._mode = "idle"
        meta = self._actions.meta(clip_id)
        self._clip_id = clip_id
        self._clip_elapsed = 0.0
        self._clip_loop = (bool(meta.get("loop"))
                           if loop is None else bool(loop))
        self._render(self._bob(time.monotonic()), self._sway(time.monotonic()))
        return True

    def _clear_clip(self) -> None:
        self._clip_id = ""
        self._clip_elapsed = 0.0
        self._clip_loop = False
        self._actions.release()

    def _advance_clip(self, dt: float) -> None:
        if not self._clip_id:
            return
        # 气泡/菜单打开时也继续播动作帧：只停“自动走动”，不冻住表情；
        # 否则点一下桌宠，点击回应会卡在第一帧。
        self._clip_elapsed += max(0.0, float(dt))
        meta = self._actions.meta(self._clip_id)
        if not meta:
            self._clear_clip()
            return
        duration = float(meta["frames"]) / max(0.1, float(meta["fps"]))
        if not self._clip_loop and self._clip_elapsed >= duration:
            self._clear_clip()
            if self._mode != "sleep":
                self._play_clip("idle", loop=True)

    def _clip_frame(self):
        if not self._clip_id:
            return None
        meta = self._actions.meta(self._clip_id)
        if not meta:
            return None
        frames = max(1, int(meta["frames"]))
        fps = max(0.1, float(meta["fps"]))
        index = int(self._clip_elapsed * fps)
        index = index % frames if self._clip_loop else min(frames - 1, index)
        return self._actions.frame(index)

    def perform_action(self, action: str) -> str:
        """
        执行一段 21 动作里的动作；没有动作包时回退到旧的本地点动作。

        与 `perform()` 分开：旧词表（hop/walk/watch…）仍被测试和旧调用使用，
        不能因为新素材接入就改变它们的语义。
        """
        name = str(action or "").strip().lower()
        now = time.monotonic()
        self._last_interaction = now
        if name == "sleep":
            self._enter_sleep(now, manual=True)
            if self._play_clip("sleep", loop=True):
                return "sleep"
        elif name == "wake":
            self._wake(now)
            if self._play_clip("wake", loop=False):
                return "wake"
        elif name == "idle":
            self._wake(now)
            if self._play_clip("idle", loop=True):
                return "idle"
        else:
            if self._mode == "sleep":
                self._wake(now)
            if self._play_clip(name, loop=None):
                return name
        return self.perform(action)

    # ---- 尺寸 / 精灵 ----

    def _load_height(self, height: int, *, keep_bottom: bool,
                     x: int | None = None, y: int | None = None) -> None:
        if height not in available_heights():
            height = dict(SIZE_PRESETS)[DEFAULT_SIZE]
        if self._surface is None:
            raise LayeredSurfaceError("桌宠窗口尚未初始化分层表面")
        images: dict[str, LayerImage] = {}
        for name in ("front", "side", "side_left", "back",
                     "front_blink", "side_blink", "side_left_blink"):
            path = sprite_path(height, name)
            if os.path.isfile(path):
                images[name] = self._surface.image(path)
        if "front" not in images:
            raise FileNotFoundError(f"缺少桌宠精灵：{sprite_path(height, 'front')}")
        self._height = height
        self._images = images
        action_w = (self._actions.max_width(height)
                    if self._actions.available() else 0)
        self._w = max(max(img.width() for img in images.values()),
                      action_w) + PAD_X * 2
        self._h = max(img.height() for img in images.values()) + PAD_TOP
        self._surface.resize(self._w, self._h)
        self._actions.surface = self._surface
        self._clear_particles()
        if x is None or y is None:
            self._x, self._y = default_position(self._w, self._h)
        else:
            if keep_bottom:
                self._x = int(x) - self._w // 2
                self._y = int(y) - self._h
            else:
                self._x, self._y = int(x), int(y)
            self._x, self._y = clamp_position(self._x, self._y,
                                              self._w, self._h, *work_area())
        self._geometry()
        self._sprite = "front"
        self._render(0)
        # 有动作包时用真实的呼吸循环替换“静态图 + 合成眨眼”；
        # 没有动作包则保持原来的精灵动画，完全不影响旧安装。
        if not self._clip_id:
            self._play_clip("idle", loop=True)

    def _geometry(self) -> None:
        try:
            self.top.geometry(f"{self._w}x{self._h}+{self._x}+{self._y}")
        except tk.TclError:
            pass

    def _sprite_name(self, now: float | None = None) -> str:
        now = time.monotonic() if now is None else now
        base = self._sprite if self._sprite in self._images else "front"
        if (self._mode == "sleep" or now < self._blink_until
                or self._mode == "watch"):
            blink = f"{base}_blink"
            if blink in self._images:
                return blink
        return base

    def _render(self, bob: float, sway: float = 0) -> None:
        """
        把当前帧画进分层表面并提交。

        `bob`/`sway` 是浮点（呼吸、走路），这里只取整到物理像素；
        每帧一次 `UpdateLayeredWindow`，没有 HRGN 重建。
        """
        if self._surface is None:
            return
        try:
            clip = self._clip_frame() if self._mode != "walk" else None
            if clip is not None:
                image, src_x, src_y, src_w, src_h = clip
                target_h = max(1, int(self._height))
                target_w = max(1, round(src_w * target_h / max(1, src_h)))
                clip_x = self._w // 2 - target_w // 2
                clip_y = self._h - target_h
                self._surface.begin()
                self._surface.draw_image_rect(
                    image, src_x, src_y, src_w, src_h,
                    clip_x, clip_y, target_w, target_h)
                self._draw_particles()
                if not self._surface.present():
                    self._render_error = (self._surface.last_error
                                          or "UpdateLayeredWindow 失败")
                    return
                self._last_render_args = (f"clip:{self._clip_id}", 0, 0)
                self._frame_count += 1
                self._render_error = ""
                return
            name = self._sprite_name()
            image = self._images.get(name) or self._images["front"]
            sway = max(-PAD_X + 1, min(PAD_X - 1, int(round(sway))))
            bob = max(0, int(round(bob)))
            x = self._w // 2 - image.width() // 2 + sway
            y = self._h - image.height() - bob
            self._surface.begin()
            self._surface.draw_image(image, x, y)
            self._draw_particles()
            if not self._surface.present():
                self._render_error = (self._surface.last_error
                                      or "UpdateLayeredWindow 失败")
                return
            self._last_render_args = (name, sway, bob)
            self._frame_count += 1
            self._render_error = ""
        except (LayeredSurfaceError, OSError, ValueError) as exc:
            self._render_error = f"{type(exc).__name__}: {exc}"

    def _draw_particles(self) -> None:
        """把粒子按剩余寿命淡出；坐标和 Tk 时代保持一致。"""
        if self._surface is None:
            return
        for particle in self._particles:
            left = max(0.0, 1.0 - particle["age"] / max(0.01, particle["life"]))
            alpha = int(255 * left)
            kind = particle["kind"]
            if kind == "text":
                self._surface.draw_text(
                    particle["text"], particle["x"], particle["y"],
                    particle["size"], rgb(particle["color"]), alpha)
            elif kind == "sparkle":
                r = particle["r"]
                x, y = particle["x"], particle["y"]
                points = []
                for px, py in ((0, -r), (r * 0.35, -r * 0.35),
                               (r, 0), (r * 0.35, r * 0.35),
                               (0, r), (-r * 0.35, r * 0.35),
                               (-r, 0), (-r * 0.35, -r * 0.35)):
                    points.append((x + px, y + py))
                self._surface.draw_polygon(
                    points, rgb(particle["color"]), alpha)
            elif kind == "oval":
                r = particle["r"]
                x, y = particle["x"], particle["y"]
                if particle.get("fill"):
                    self._surface.fill_ellipse(
                        x - r, y - r, r * 2, r * 2,
                        rgb(particle["color"]), alpha)
                else:
                    self._surface.draw_ellipse(
                        x - r, y - r, r * 2, r * 2,
                        rgb(particle["color"]),
                        particle.get("width", 1), alpha)

    def _move_window(self, x: int, y: int) -> None:
        left, top, right, bottom = work_area()
        nx, ny = clamp_position(x, y, self._w, self._h,
                                left, top, right, bottom)
        if nx == self._x and ny == self._y:
            return
        self._x, self._y = nx, ny
        try:
            self.top.geometry(f"+{nx}+{ny}")
        except tk.TclError:
            return

    def _make_tool_window(self) -> None:
        """
        给窗口加 `WS_EX_LAYERED | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE`：

        · WS_EX_LAYERED：`UpdateLayeredWindow` 的逐像素 alpha 画布；
        · TOOLWINDOW：不出现在 Alt+Tab 里；
        · NOACTIVATE：点它/拖它不抢当前程序焦点。

        同时清掉 DWM 边框/阴影/圆角，并阻止 Tk 客户区重画底色。
        这里不吞异常：分层表面建不起来时宁可让主程序报错，也不要
        静默退回一个带矩形底色的窗口。
        """
        self.top.update_idletasks()
        user32 = ctypes.windll.user32
        user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
        user32.GetAncestor.restype = wintypes.HWND
        user32.GetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int]
        user32.GetWindowLongPtrW.restype = ctypes.c_longlong
        user32.SetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int,
                                             ctypes.c_longlong]
        user32.SetWindowLongPtrW.restype = ctypes.c_longlong
        hwnd = user32.GetAncestor(
            wintypes.HWND(self.top.winfo_id()), 2)  # GA_ROOT
        if not hwnd:
            raise LayeredSurfaceError("找不到桌宠顶层窗口")
        self._hwnd = int(hwnd)
        style = int(user32.GetWindowLongPtrW(hwnd, -20) or 0)
        want = style | 0x00080000 | 0x00000080 | 0x08000000
        if want != style:
            user32.SetWindowLongPtrW(hwnd, -20, want)
        self._strip_window_frame(hwnd)
        self._paint_guard = ChildPaintSuppressor(self.top.winfo_id())
        self._surface = LayeredSurface(self._hwnd, 1, 1)

    @staticmethod
    def _apply_dwm_frame(hwnd) -> None:
        """
        关掉 Win11 的 DWM 圆角。

        分层窗口本身没有底色，但 Win11 会给顶层窗口套一个圆角/边框；
        在深色桌面上看，就是精灵背后一层半透明的圆角白卡。这个属性必须
        在窗口已经显示、并且 `SetWindowPos(FRAMECHANGED)` 之后再设，
        否则会被 DWM 重新套回默认圆角。
        """
        try:
            dwm = ctypes.windll.dwmapi
            set_attr = dwm.DwmSetWindowAttribute
            set_attr.argtypes = [wintypes.HWND, wintypes.DWORD,
                                 ctypes.POINTER(wintypes.DWORD),
                                 wintypes.DWORD]
            set_attr.restype = ctypes.c_long
            value = wintypes.DWORD()
            # DWMWA_NCRENDERING_POLICY = 2, DWMNCRP_DISABLED = 1
            value.value = 1
            set_attr(wintypes.HWND(hwnd), 2, ctypes.byref(value),
                     ctypes.sizeof(value))
            # DWMWA_WINDOW_CORNER_PREFERENCE = 33, DONOTROUND = 1
            value.value = 1
            set_attr(wintypes.HWND(hwnd), 33, ctypes.byref(value),
                     ctypes.sizeof(value))
            # DWMWA_BORDER_COLOR = 34, DWMWA_COLOR_NONE = 0xFFFFFFFE
            value.value = 0xFFFFFFFE
            set_attr(wintypes.HWND(hwnd), 34, ctypes.byref(value),
                     ctypes.sizeof(value))
        except Exception:
            pass

    @staticmethod
    def _strip_window_frame(hwnd) -> None:
        """去掉系统边框、DWM 阴影/圆角；全部失败也不影响桌宠。"""
        try:
            user32 = ctypes.windll.user32
            style = int(user32.GetWindowLongPtrW(hwnd, -20) or 0)
            # WS_EX_WINDOWEDGE | CLIENTEDGE | STATICEDGE | DLGMODALFRAME
            style &= ~(0x00000100 | 0x00000200 | 0x00020000 | 0x00040000)
            user32.SetWindowLongPtrW(hwnd, -20, style)
        except Exception:
            pass
        try:
            # CS_DROPSHADOW：某些 Tk 版本的窗口类会带这个位
            user32 = ctypes.windll.user32
            get_cls = user32.GetClassLongPtrW
            get_cls.argtypes = [wintypes.HWND, ctypes.c_int]
            get_cls.restype = ctypes.c_longlong
            set_cls = user32.SetClassLongPtrW
            set_cls.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_longlong]
            set_cls.restype = ctypes.c_longlong
            GCL_STYLE, CS_DROPSHADOW = -26, 0x00020000
            cls = int(get_cls(hwnd, GCL_STYLE) or 0)
            if cls & CS_DROPSHADOW:
                set_cls(hwnd, GCL_STYLE, cls & ~CS_DROPSHADOW)
        except Exception:
            pass
        try:
            user32 = ctypes.windll.user32
            SWP_NOSIZE, SWP_NOMOVE, SWP_NOZORDER = 0x0001, 0x0002, 0x0004
            SWP_NOACTIVATE, SWP_FRAMECHANGED = 0x0010, 0x0020
            user32.SetWindowPos(wintypes.HWND(hwnd), 0, 0, 0, 0, 0,
                                SWP_NOSIZE | SWP_NOMOVE | SWP_NOZORDER
                                | SWP_NOACTIVATE | SWP_FRAMECHANGED)
        except Exception:
            pass
        # 必须在 FRAMECHANGED 之后设，否则圆角会被 DWM 重新套回来。
        DesktopPet._apply_dwm_frame(hwnd)

    # ---- 动画 ----

    def _schedule_tick(self) -> None:
        if not self._alive or self._after_id:
            return
        try:
            self._after_id = self.top.after(TICK_MS, self._tick)
        except tk.TclError:
            self._after_id = None

    def _tick(self) -> None:
        self._after_id = None
        if not self._alive:
            return
        now = time.monotonic()
        dt = max(0.01, min(0.25, now - self._last_tick))
        self._last_tick = now
        self._now = now
        try:
            self._step_particles(now, dt)
            if self._dragging:
                self._render(0)
                return
            if self._mode == "sleep":
                self._sleep_tick(now)
                self._advance_clip(dt)
                self._render(self._bob(now), 0)
                return
            if self._mode == "walk" and not self._paused:
                bob = self._walk_tick(now)
                self._render(bob, self._sway(now))
                return
            if self._mode == "walk" and self._paused:
                self._mode = "idle"
            self._idle_tick(now)
            self._advance_clip(dt)
            self._render(self._bob(now), self._sway(now))
        finally:
            self._schedule_tick()

    def _idle_tick(self, now: float) -> None:
        self._sprite = "front"
        if now < self._spin_until:
            if now >= self._spin_next:
                self._spin_next = now + 0.08
                self._spin_side = ("side_left" if self._spin_side == "side"
                                   else "side")
            self._sprite = self._spin_side
            return
        if now < self._look_until:
            self._sprite = self._look_sprite
        if now >= self._next_blink:
            self._blink_until = now + BLINK_SECONDS
            self._next_blink = now + self._rng.uniform(2.6, 6.5) / self._speed
        if now < self._watch_until:
            self._watch_tick(now)
            return
        if self._ambient == "music":
            self._music_tick(now)
            return
        if self._ambient == "video":
            self._watch_tick(now)
            return
        if self._maybe_sleep(now):
            return
        if (not self._paused and self._walk_remaining > 0
                and self._walk_target != (0, 0)):
            self._mode = "walk"
            return
        if now >= self._next_action:
            self._next_action = now + self._rng.uniform(4.0, 8.5) / self._speed
            if not self._clip_id:
                self._play_once(now)
        if (not self._paused and now >= self._next_walk
                and self._walk_remaining <= 0):
            self._next_walk = now + self._rng.uniform(7.0, 14.0) / self._speed
            if self._rng.random() < 0.62 * self._speed:
                self._start_walk(now)

    def _play_once(self, now: float) -> None:
        """发呆时的小动作：看看旁边、跳一下、伸个懒腰、转个圈、眨眨眼。"""
        choice = self._rng.choice(("look", "hop", "look", "blink",
                                   "stretch", "spin", "greet"))
        if choice == "look":
            self._look_sprite = self._rng.choice(("side", "side_left"))
            self._look_until = now + self._rng.uniform(0.8, 1.8)
        elif choice == "hop":
            self._hop_until = now + 0.45
        elif choice == "stretch":
            self._hop_until = now + 0.75
            self._spawn_particle("sparkle")
        elif choice == "spin":
            self._spin_until = now + 0.8
            self._spin_next = 0.0
        elif choice == "greet":
            self._hop_until = now + 0.5
            self._spawn_particle("heart")
        else:
            self._blink_until = now + BLINK_SECONDS

    def _watch_tick(self, now: float) -> None:
        """视频/认真看：正面、少走动，偶尔闪一下“看到了”。"""
        self._sprite = "front"
        if now >= self._next_action:
            self._next_action = now + self._rng.uniform(3.5, 7.0) / self._speed
            if self._rng.random() < 0.65:
                self._spawn_particle("sparkle")
        if (now - self._last_interaction > 240
                and seconds_since_last_input() > 300):
            self._enter_sleep(now)

    def _music_tick(self, now: float) -> None:
        """音乐：正面跟着节拍晃，隔一会儿吐一个泡泡/音符。"""
        self._sprite = "front"
        if now >= self._music_next:
            self._music_next = now + self._rng.uniform(1.8, 3.6) / self._speed
            self._spawn_particle(self._rng.choice(("bubble", "note", "heart")))
        if now >= self._next_action:
            self._next_action = now + self._rng.uniform(2.2, 4.2) / self._speed
            self._hop_until = now + 0.32

    def _maybe_sleep(self, now: float) -> bool:
        if self._paused or self._ambient:
            return False
        if (now - self._last_interaction > 60
                and seconds_since_last_input() > SLEEP_AFTER):
            self._enter_sleep(now)
            return True
        return False

    def _enter_sleep(self, now: float, *, manual: bool = False) -> None:
        self._mode = "sleep"
        self._manual_sleep = bool(manual)
        self._sprite = "front"
        self._walk_remaining = 0
        self._next_zzz = now
        self._last_interaction = now
        self._play_clip("sleep", loop=True)

    def _sleep_tick(self, now: float) -> None:
        if self._paused or self._ambient:
            self._wake(now)
            return
        if not self._manual_sleep and seconds_since_last_input() < 3.0:
            self._wake(now)
            return
        if now >= self._next_zzz:
            self._next_zzz = now + self._rng.uniform(2.2, 3.8)
            self._spawn_particle("zzz")

    def _wake(self, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        if self._mode == "sleep":
            self._mode = "idle"
        self._manual_sleep = False
        self._last_interaction = now
        self._sprite = "front"
        self._next_walk = now + self._rng.uniform(4.0, 8.0)
        self._next_action = now + self._rng.uniform(1.5, 3.0)
        if self._clip_id in ("", "sleep"):
            self._play_clip("idle", loop=True)

    def _bob(self, now: float) -> float:
        if self._mode == "sleep":
            bob = 2.0 + 1.2 * math.sin(now * 1.2)
        elif self._mode == "walk":
            bob = 3.0 + abs(math.sin(now * 13.0)) * 6.5
        elif self._ambient == "music":
            bob = 2.0 + 2.0 * math.sin(now * 6.5)
        elif self._ambient == "video":
            bob = 1.5 + 1.2 * math.sin(now * 1.6)
        else:
            bob = 2.0 + 2.4 * math.sin(now * 2.0 * self._speed)
        if now < self._hop_until:
            left = max(0.0, (self._hop_until - now) / 0.5)
            bob += 12.0 * math.sin(math.pi * min(1.0, left))
        return bob

    def _sway(self, now: float) -> float:
        if self._mode == "walk":
            return math.sin(now * 13.0) * 2.0
        if self._ambient == "music":
            return math.sin(now * 6.5) * 3.0
        return math.sin(now * 0.9) * 1.4

    def _walk_tick(self, now: float) -> float:
        tx, ty = self._walk_target
        dx, dy = tx - self._x, ty - self._y
        if abs(dx) <= 2 and abs(dy) <= 2:
            self._finish_walk(now)
            return 3.0
        # `_walk_step` 是“每 50ms 的步长”；按实际节拍累积成整数像素，
        # 60 FPS 下不会把散步变成 3 倍速。
        self._walk_accum += self._walk_step * (TICK_MS / 50.0)
        step = max(1, int(round(self._walk_accum)))
        self._walk_accum = max(0.0, self._walk_accum - step)
        sx = step if dx > 2 else (-step if dx < -2 else dx)
        sy = step if dy > 2 else (-step if dy < -2 else dy)
        before = (self._x, self._y)
        self._move_window(self._x + sx, self._y + sy)
        if (self._x, self._y) == before:
            self._finish_walk(now)
            return 3.0
        self._walk_remaining -= 1
        if abs(dx) >= abs(dy):
            # side 面向左；side_left 是镜像，面向右。
            self._sprite = "side" if dx < 0 else "side_left"
        elif dy < 0:
            self._sprite = "back"
        else:
            self._sprite = "front"
        self._notify_moved()
        if self._walk_remaining <= 0:
            self._finish_walk(now)
        return 3.0 + abs(math.sin(now * 13.0)) * 6.5

    def _finish_walk(self, now: float) -> None:
        self._walk_remaining = 0
        self._walk_target = (0, 0)
        self._walk_accum = 0.0
        self._mode = "idle"
        self._sprite = "front"
        self._next_walk = now + self._rng.uniform(6.0, 12.0) / self._speed
        self._spawn_particle("dust")
        if not self._clip_id:
            self._play_clip("idle", loop=True)

    def _start_walk(self, now: float, *, force: bool = False) -> None:
        if self._paused and not force:
            return
        if self._ambient and not force:
            return
        left, top, right, bottom = work_area()
        room_x = max(0, right - left - self._w)
        room_y = max(0, bottom - top - self._h)
        reach_x = min(190, room_x)
        reach_y = min(120, room_y)
        dx = self._rng.randint(-reach_x, reach_x)
        dy = self._rng.randint(-reach_y, reach_y)
        if abs(dx) < 36 and abs(dy) < 24:
            dx = 60 if self._rng.random() < 0.5 else -60
            dy = self._rng.randint(-40, 40)
        tx, ty = clamp_position(self._x + dx, self._y + dy,
                                self._w, self._h, left, top, right, bottom)
        if abs(tx - self._x) < 8 and abs(ty - self._y) < 8:
            return
        self._walk_target = (tx, ty)
        self._walk_step = max(2, min(7, round(4 * self._speed)))
        self._walk_accum = 0.0
        distance = max(abs(tx - self._x), abs(ty - self._y))
        per_tick = max(0.5, self._walk_step * (TICK_MS / 50.0))
        self._walk_remaining = max(4, int(distance / per_tick) + 3)
        self._mode = "walk"
        self._sprite = "side" if dx < 0 else "side_left"
        self._hop_until = 0.0

    # ---- 粒子 ----

    def _particle_px(self, scale: float = 1.0) -> int:
        return max(10, int(round(self._height * 0.055 * scale)))

    def _spawn_particle(self, kind: str) -> None:
        if not self._alive or not self._images:
            return
        sprite_h = max(img.height() for img in self._images.values())
        head_y = self._h - sprite_h * 0.80
        cx = self._w // 2
        if kind == "zzz":
            particle = {
                "kind": "text", "text": self._rng.choice(("z", "Z")),
                "x": cx + self._rng.randint(10, 30),
                "y": head_y + self._rng.randint(2, 12),
                "vx": 0.2, "vy": -0.22, "life": 2.2,
                "size": self._particle_px(0.9), "color": "#9fc5ff",
            }
        elif kind == "note":
            particle = {
                "kind": "text", "text": "♪",
                "x": cx + self._rng.randint(-22, 22), "y": head_y + 48,
                "vx": self._rng.uniform(-0.2, 0.2), "vy": -0.55,
                "life": 1.6, "size": self._particle_px(),
                "color": "#4f86ff",
            }
        elif kind == "heart":
            particle = {
                "kind": "text", "text": "♥",
                "x": cx + self._rng.randint(-18, 18), "y": head_y + 34,
                "vx": self._rng.uniform(-0.3, 0.3), "vy": -0.55,
                "life": 1.5, "size": self._particle_px(0.9),
                "color": "#ff6b9d",
            }
        elif kind == "!":
            particle = {
                "kind": "text", "text": "!",
                "x": cx + self._rng.randint(-14, 14), "y": head_y + 18,
                "vx": 0.0, "vy": -0.35, "life": 0.8,
                "size": self._particle_px(1.0), "color": "#ff7a59",
            }
        elif kind == "sparkle":
            particle = {
                "kind": "sparkle",
                "x": cx + self._rng.randint(-30, 30),
                "y": head_y + self._rng.randint(0, 20),
                "r": max(4, self._height // 34),
                "vx": 0.0, "vy": -0.25, "life": 0.9,
                "color": "#ffd166",
            }
        elif kind == "dust":
            particle = {
                "kind": "oval", "fill": True,
                "x": cx + self._rng.randint(-26, 26), "y": self._h - 4,
                "r": max(2, self._height // 55),
                "vx": self._rng.uniform(-0.3, 0.3), "vy": -0.25,
                "life": 0.8, "color": "#cfd6e6",
            }
        else:  # bubble
            particle = {
                "kind": "oval", "fill": False,
                "x": cx + self._rng.randint(-20, 20), "y": head_y + 45,
                "r": max(4, self._height // 30),
                "vx": self._rng.uniform(-0.15, 0.15), "vy": -0.4,
                "life": 1.6, "color": "#8ec5ff", "width": 2,
            }
        particle["age"] = 0.0
        self._particles.append(particle)

    def _step_particles(self, now: float, dt: float) -> None:
        if not self._particles:
            return
        alive: list[dict] = []
        for particle in self._particles:
            particle["age"] += dt
            if particle["age"] >= particle["life"]:
                continue
            particle["x"] += particle["vx"] * dt * 60.0
            particle["y"] += particle["vy"] * dt * 60.0
            alive.append(particle)
        self._particles = alive

    def _clear_particles(self) -> None:
        self._particles = []

    # ---- 鼠标 ----

    def _on_press(self, event) -> None:
        now = time.monotonic()
        self._wake(now)
        self._dragging = True
        self._moved = False
        self._drag_sparked = False
        self._drag_offset = (int(event.x_root) - self._x,
                             int(event.y_root) - self._y)
        self._walk_remaining = 0
        self._mode = "idle"
        self.set_paused(True)
        # 按下就给反馈：参考 dsh-pet 的 Q 弹，不等 240ms 双击判定。
        self._hop_until = now + 0.35
        self._spawn_particle("heart")
        self._render(self._bob(now), self._sway(now))
        if self._click_after:
            try:
                self.top.after_cancel(self._click_after)
            except tk.TclError:
                pass
            self._click_after = None

    def _on_drag(self, event) -> None:
        if not self._dragging:
            return
        x = int(event.x_root) - self._drag_offset[0]
        y = int(event.y_root) - self._drag_offset[1]
        if (abs(x - self._x) > 2 or abs(y - self._y) > 2):
            self._moved = True
            if not self._drag_sparked:
                self._drag_sparked = True
                self._spawn_particle("!")
        self._move_window(x, y)

    def _on_release(self, _event) -> None:
        if not self._dragging:
            return
        self._dragging = False
        self.set_paused(False)
        if self._moved:
            self._notify_moved(force=True)
            return
        # 单击延后 240ms，给双击让路；双击会撤掉这条定时器。
        if self._on_click is not None:
            try:
                self._click_after = self.top.after(240, self._fire_click)
            except tk.TclError:
                self._click_after = None

    def _fire_click(self) -> None:
        self._click_after = None
        if not self._alive:
            return
        self._wake()
        # 点击的即时回应：四段“点击回应”随机一段；动作包缺失时
        # perform_action 会回退到本地点动作，不会报错。
        try:
            self.perform_action(self._rng.choice(
                ("wave", "joy", "shy", "pout")))
        except Exception:
            pass
        if self._on_click is not None:
            try:
                self._on_click(self)
            except Exception:
                pass

    def _on_double_click(self, _event) -> None:
        if self._click_after:
            try:
                self.top.after_cancel(self._click_after)
            except tk.TclError:
                pass
            self._click_after = None
        now = time.monotonic()
        self._wake(now)
        self._hop_until = now + 0.7
        for _ in range(3):
            self._spawn_particle("heart")
        self._render(self._bob(now), self._sway(now))
        if self._on_double is not None:
            try:
                self._on_double(self)
            except Exception:
                pass

    def _on_right_click(self, event) -> None:
        self._wake()
        if self._on_menu is not None:
            try:
                self._on_menu(self, int(event.x_root), int(event.y_root))
            except Exception:
                pass

    def _notify_moved(self, force: bool = False) -> None:
        if self._on_moved is None:
            return
        now = time.monotonic()
        if not force and now - self._last_save < 1.5:
            return
        self._last_save = now
        try:
            self._on_moved(self._x, self._y)
        except Exception:
            pass
