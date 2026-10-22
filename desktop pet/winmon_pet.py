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
Tk 没有逐像素透明窗口，但有 `-transparentcolor`：把某一种颜色键掉。
精灵图是**二值 alpha**（透明/不透明）的，窗口底色用 `KEY_COLOR`，
因此不会出现“半透明边缘和底色混成绿边”的问题。半透明原图不直接使用，
由构建时处理成二值 alpha，见同目录 `NOTICE.md`。

窗口本身还会去掉边框、阴影和圆角（DWM），并把画布留白压到最小；
如果系统仍给弹出窗加装饰，`_strip_window_frame()` 会再清一遍。

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
import math
import os
import random
import time
import tkinter as tk

#: 透明键色：精灵图里没有这个颜色（构建时二值化后更不会出现）
KEY_COLOR = "#ff00fe"
#: 尺寸档位：标签 → 精灵高度（物理像素）
SIZE_PRESETS: tuple[tuple[str, int], ...] = (("小", 187), ("中", 238), ("大", 306))
DEFAULT_SIZE = "中"
#: 默认窗口相对屏幕右下角的留白
EDGE_MARGIN = 28
#: 动画节拍（毫秒）。50ms = 20 FPS；足够让呼吸/走路看起来连续。
TICK_MS = 50
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
        self._last_save = 0.0
        self._rng = random.Random()
        self._particles: list[dict] = []
        self._sprite = "front"

        self.top = tk.Toplevel(parent)
        self.top.withdraw()
        self.top.overrideredirect(True)
        self.top.configure(bg=KEY_COLOR)
        try:
            self.top.attributes("-topmost", True)
        except tk.TclError:
            pass
        try:
            self.top.wm_attributes("-transparentcolor", KEY_COLOR)
        except tk.TclError:
            # 非 Windows/极简 Tk：保持窗口可用，只是底色调成中性色
            self.top.configure(bg="#20242e")
        self.top.protocol("WM_DELETE_WINDOW", self.destroy)

        self._images: dict[str, tk.PhotoImage] = {}
        self.canvas = tk.Canvas(self.top, bg=KEY_COLOR, bd=0,
                                highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)
        self._image_item = self.canvas.create_image(0, 0, anchor="s")
        self._w, self._h = 1, 1
        self._x = int(x or 0)
        self._y = int(y or 0)
        self._load_height(self._height, keep_bottom=False, x=x, y=y)

        self.canvas.configure(cursor="hand2")
        self.canvas.bind("<ButtonPress-1>", self._on_press, add="+")
        self.canvas.bind("<B1-Motion>", self._on_drag, add="+")
        self.canvas.bind("<ButtonRelease-1>", self._on_release, add="+")
        self.canvas.bind("<Double-Button-1>", self._on_double_click, add="+")
        self.canvas.bind("<Button-3>", self._on_right_click, add="+")
        self.top.bind("<Escape>", lambda _e: self.destroy(), add="+")
        self.show()
        self._make_tool_window()

    # ---- 生命周期 ----

    def show(self) -> None:
        if not self._alive:
            return
        try:
            self.top.deiconify()
            self.top.lift()
        except tk.TclError:
            return
        self._last_tick = time.monotonic()
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
            self.top.destroy()
        except tk.TclError:
            pass

    def set_paused(self, paused: bool) -> None:
        """暂停自动走动（气泡/菜单打开时用），呼吸和眨眼保留。"""
        self._paused = bool(paused)
        if self._paused:
            self._walk_remaining = 0
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
        return name

    # ---- 尺寸 / 精灵 ----

    def _load_height(self, height: int, *, keep_bottom: bool,
                     x: int | None = None, y: int | None = None) -> None:
        if height not in available_heights():
            height = dict(SIZE_PRESETS)[DEFAULT_SIZE]
        images: dict[str, tk.PhotoImage] = {}
        for name in ("front", "side", "side_left", "back",
                     "front_blink", "side_blink", "side_left_blink"):
            path = sprite_path(height, name)
            if os.path.isfile(path):
                images[name] = tk.PhotoImage(master=self.top, file=path)
        if "front" not in images:
            raise FileNotFoundError(f"缺少桌宠精灵：{sprite_path(height, 'front')}")
        self._height = height
        self._images = images
        self._w = max(img.width() for img in images.values()) + PAD_X * 2
        self._h = max(img.height() for img in images.values()) + PAD_TOP
        self.canvas.configure(width=self._w, height=self._h)
        self.canvas.coords(self._image_item, self._w // 2, self._h)
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

    def _render(self, bob: int, sway: int = 0) -> None:
        try:
            name = self._sprite_name()
            image = self._images.get(name) or self._images["front"]
            sway = max(-PAD_X + 1, min(PAD_X - 1, int(sway)))
            self.canvas.itemconfigure(self._image_item, image=image)
            self.canvas.coords(self._image_item, self._w // 2 + sway,
                               self._h - max(0, int(bob)))
        except (tk.TclError, KeyError):
            pass

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
        给窗口加 `WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE`：

        · 不出现在 Alt+Tab 里（它只是桌宠，不是第二个主窗口）；
        · 点它/拖它**不抢当前程序焦点**，气泡看完还能接着打字。
        另外清掉 DWM 给弹出窗的边框/阴影/圆角，避免精灵背后出现“透明框架”。
        失败只影响这些“礼貌性”行为，不影响桌宠本身。
        """
        try:
            self.top.update_idletasks()
            user32 = ctypes.windll.user32
            user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
            user32.GetAncestor.restype = wintypes.HWND
            user32.GetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int]
            user32.SetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int,
                                                 ctypes.c_longlong]
            hwnd = user32.GetAncestor(
                wintypes.HWND(self.top.winfo_id()), 2)  # GA_ROOT
            if not hwnd:
                return
            user32.GetWindowLongPtrW.restype = ctypes.c_longlong
            user32.SetWindowLongPtrW.restype = ctypes.c_longlong
            style = int(user32.GetWindowLongPtrW(hwnd, -20) or 0)
            want = style | 0x00000080 | 0x08000000
            if want != style:
                user32.SetWindowLongPtrW(hwnd, -20, want)
            self._strip_window_frame(hwnd)
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
            dwm = ctypes.windll.dwmapi
            value = wintypes.DWORD()
            # DWMWA_NCRENDERING_POLICY = 2, DWMNCRP_DISABLED = 1
            value.value = 1
            dwm.DwmSetWindowAttribute(wintypes.HWND(hwnd), 2,
                                      ctypes.byref(value), ctypes.sizeof(value))
            # DWMWA_WINDOW_CORNER_PREFERENCE = 33, DONOTROUND = 1
            value.value = 1
            dwm.DwmSetWindowAttribute(wintypes.HWND(hwnd), 33,
                                      ctypes.byref(value), ctypes.sizeof(value))
            # DWMWA_BORDER_COLOR = 34, DWMWA_COLOR_NONE = 0xFFFFFFFE
            value.value = 0xFFFFFFFE
            dwm.DwmSetWindowAttribute(wintypes.HWND(hwnd), 34,
                                      ctypes.byref(value), ctypes.sizeof(value))
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
                self._render(self._bob(now), 0)
                return
            if self._mode == "walk" and not self._paused:
                bob = self._walk_tick(now)
                self._render(bob, self._sway(now))
                return
            if self._mode == "walk" and self._paused:
                self._mode = "idle"
            self._idle_tick(now)
            self._render(self._bob(now), self._sway(now))
        finally:
            self._schedule_tick()

    def _idle_tick(self, now: float) -> None:
        self._sprite = "front"
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
            self._play_once(now)
        if (not self._paused and now >= self._next_walk
                and self._walk_remaining <= 0):
            self._next_walk = now + self._rng.uniform(7.0, 14.0) / self._speed
            if self._rng.random() < 0.62 * self._speed:
                self._start_walk(now)

    def _play_once(self, now: float) -> None:
        """发呆时的小动作：看看旁边、跳一下、伸个懒腰、眨眨眼。"""
        choice = self._rng.choice(("look", "hop", "look", "blink", "stretch"))
        if choice == "look":
            self._look_sprite = self._rng.choice(("side", "side_left"))
            self._look_until = now + self._rng.uniform(0.8, 1.8)
        elif choice == "hop":
            self._hop_until = now + 0.45
        elif choice == "stretch":
            self._hop_until = now + 0.75
            self._spawn_particle("sparkle")
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
        step = self._walk_step
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
        self._mode = "idle"
        self._sprite = "front"
        self._next_walk = now + self._rng.uniform(6.0, 12.0) / self._speed
        self._spawn_particle("dust")

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
        distance = max(abs(tx - self._x), abs(ty - self._y))
        self._walk_remaining = max(4, distance // self._walk_step + 3)
        self._mode = "walk"
        self._sprite = "side" if dx < 0 else "side_left"
        self._hop_until = 0.0

    # ---- 粒子 ----

    def _particle_font(self, scale: float = 1.0) -> tuple:
        size = max(10, int(round(self._height * 0.055 * scale)))
        return ("Segoe UI Symbol", size, "bold")

    def _spawn_particle(self, kind: str) -> None:
        if not self._alive:
            return
        try:
            sprite_h = max(img.height() for img in self._images.values())
            head_y = self._h - sprite_h * 0.80
            cx = self._w // 2
            if kind == "zzz":
                x = cx + self._rng.randint(10, 34)
                y = head_y - self._rng.randint(0, 12)
                item = self.canvas.create_text(
                    x, y, text=self._rng.choice(("z", "Z")),
                    font=self._particle_font(0.9), fill="#9fc5ff",
                    tags=("pet-particle",))
                particle = {"item": item, "kind": "text", "x": x, "y": y,
                            "vx": 0.35, "vy": -0.55, "life": 2.6}
            elif kind == "note":
                x = cx + self._rng.randint(-22, 22)
                y = head_y + 28
                item = self.canvas.create_text(
                    x, y, text="♪", font=self._particle_font(),
                    fill="#4f86ff", tags=("pet-particle",))
                particle = {"item": item, "kind": "text", "x": x, "y": y,
                            "vx": self._rng.uniform(-0.25, 0.25),
                            "vy": -0.85, "life": 1.9}
            elif kind == "heart":
                x = cx + self._rng.randint(-18, 18)
                y = head_y + 18
                item = self.canvas.create_text(
                    x, y, text="♥", font=self._particle_font(0.9),
                    fill="#ff6b9d", tags=("pet-particle",))
                particle = {"item": item, "kind": "text", "x": x, "y": y,
                            "vx": self._rng.uniform(-0.35, 0.35),
                            "vy": -0.95, "life": 1.8}
            elif kind == "!":
                x = cx + self._rng.randint(-14, 14)
                y = head_y - 4
                item = self.canvas.create_text(
                    x, y, text="!", font=self._particle_font(1.0),
                    fill="#ff7a59", tags=("pet-particle",))
                particle = {"item": item, "kind": "text", "x": x, "y": y,
                            "vx": 0.0, "vy": -0.45, "life": 0.8}
            elif kind == "sparkle":
                x = cx + self._rng.randint(-30, 30)
                y = head_y + self._rng.randint(0, 20)
                r = max(4, self._height // 34)
                points = []
                for px, py in ((0, -r), (r * 0.35, -r * 0.35),
                               (r, 0), (r * 0.35, r * 0.35),
                               (0, r), (-r * 0.35, r * 0.35),
                               (-r, 0), (-r * 0.35, -r * 0.35)):
                    points.extend((x + px, y + py))
                item = self.canvas.create_polygon(
                    *points, fill="#ffd166", outline="",
                    tags=("pet-particle",))
                particle = {"item": item, "kind": "sparkle", "x": x, "y": y,
                            "r": r, "vx": 0.0, "vy": -0.25, "life": 0.9}
            elif kind == "dust":
                x = cx + self._rng.randint(-26, 26)
                y = self._h - 4
                r = max(2, self._height // 55)
                item = self.canvas.create_oval(
                    x - r, y - r, x + r, y + r, fill="#cfd6e6",
                    outline="", stipple="gray50", tags=("pet-particle",))
                particle = {"item": item, "kind": "oval", "x": x, "y": y,
                            "r": r, "vx": self._rng.uniform(-0.3, 0.3),
                            "vy": -0.25, "life": 0.8}
            else:  # bubble
                x = cx + self._rng.randint(-20, 20)
                y = head_y + 30
                r = max(4, self._height // 30)
                item = self.canvas.create_oval(
                    x - r, y - r, x + r, y + r, outline="#8ec5ff",
                    width=2, tags=("pet-particle",))
                particle = {"item": item, "kind": "oval", "x": x, "y": y,
                            "r": r, "vx": self._rng.uniform(-0.2, 0.2),
                            "vy": -0.7, "life": 2.2}
            particle["age"] = 0.0
            self._particles.append(particle)
        except tk.TclError:
            pass

    def _step_particles(self, now: float, dt: float) -> None:
        if not self._particles:
            return
        alive: list[dict] = []
        for particle in self._particles:
            particle["age"] += dt
            if particle["age"] >= particle["life"]:
                try:
                    self.canvas.delete(particle["item"])
                except tk.TclError:
                    pass
                continue
            particle["x"] += particle["vx"] * dt * 60.0
            particle["y"] += particle["vy"] * dt * 60.0
            try:
                if particle["kind"] == "sparkle":
                    r = particle["r"]
                    x, y = particle["x"], particle["y"]
                    points = []
                    for px, py in ((0, -r), (r * 0.35, -r * 0.35),
                                   (r, 0), (r * 0.35, r * 0.35),
                                   (0, r), (-r * 0.35, r * 0.35),
                                   (-r, 0), (-r * 0.35, -r * 0.35)):
                        points.extend((x + px, y + py))
                    self.canvas.coords(particle["item"], *points)
                elif particle["kind"] == "oval":
                    r = particle["r"]
                    x, y = particle["x"], particle["y"]
                    self.canvas.coords(particle["item"], x - r, y - r,
                                       x + r, y + r)
                else:
                    self.canvas.coords(particle["item"], particle["x"],
                                       particle["y"])
            except tk.TclError:
                continue
            alive.append(particle)
        self._particles = alive

    def _clear_particles(self) -> None:
        for particle in self._particles:
            try:
                self.canvas.delete(particle["item"])
            except tk.TclError:
                pass
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
        self._hop_until = time.monotonic() + 0.5
        for _ in range(2):
            self._spawn_particle("heart")
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
        self._wake()
        self._hop_until = time.monotonic() + 0.7
        for _ in range(3):
            self._spawn_particle("heart")
        if self._on_double is not None:
            try:
                self._on_double(self)
            except Exception:
                pass

    def _on_right_click(self, event) -> None:
        self._wake()
        self.set_paused(True)
        if self._on_menu is not None:
            try:
                self._on_menu(self, int(event.x_root), int(event.y_root))
            except Exception:
                pass
        if self._alive:
            self.set_paused(False)

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
