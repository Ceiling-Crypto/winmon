# -*- coding: utf-8 -*-
"""
WinMon 桌面宠物（可选）
======================

一只会待在桌面角落的「鲸鱼娘 · 大肥鱼」：点一下弹出信息气泡，
拖到哪儿就待在哪，右键出菜单；不抢焦点、不写注册表、不联网。

为什么单独一个模块
------------------
`winmon.py` 已经很大；桌宠的窗口/动画/命中测试和主界面是两块独立的心智负担。
这里只依赖标准库 `tkinter` + `ctypes`，不 import `winmon.py`，便于单测和以后复用。

透明怎么做（**Windows 专有**）
----------------------------
Tk 没有逐像素透明窗口，但有 `-transparentcolor`：把某一种颜色键掉。
精灵图是**二值 alpha**（透明/不透明）的，窗口底色用 `KEY_COLOR`，
因此不会出现“半透明边缘和底色混成绿边”的问题。半透明原图不直接使用，
由构建时处理成二值 alpha，见 `assets/pet/NOTICE.md`。

坐标约定
--------
所有 `x/y` 都是**屏幕物理像素**；窗口高度由 `SIZE_PRESETS` 给出。
默认落在工作区右下角（任务栏上方），并夹在屏幕内。
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
#: 动画节拍（毫秒）。70ms ≈ 14 FPS；桌宠只是轻轻呼吸，不需要 60 FPS。
TICK_MS = 70


def project_dir() -> str:
    return os.path.dirname(os.path.abspath(__file__))


def asset_dir() -> str:
    return os.path.join(project_dir(), "assets", "pet")


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
    on_click / on_menu / on_double / on_moved : 回调，全部在主线程调用
    """

    def __init__(self, parent: tk.Misc, *, height: int = 238,
                 x: int | None = None, y: int | None = None,
                 on_click=None, on_menu=None, on_double=None,
                 on_moved=None) -> None:
        self._parent = parent
        self._on_click = on_click if callable(on_click) else None
        self._on_menu = on_menu if callable(on_menu) else None
        self._on_double = on_double if callable(on_double) else None
        self._on_moved = on_moved if callable(on_moved) else None
        self._height = int(height)
        self._alive = True
        self._paused = False
        self._after_id = None
        self._click_after = None
        self._dragging = False
        self._moved = False
        self._drag_offset = (0, 0)
        self._hop_until = 0.0
        self._next_walk = time.monotonic() + random.uniform(8.0, 15.0)
        self._walk_remaining = 0
        self._walk_dir = 1
        self._last_save = 0.0
        self._rng = random.Random()

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
        self._sprite = "front"
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

    def _make_tool_window(self) -> None:
        """
        给窗口加 `WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE`：

        · 不出现在 Alt+Tab 里（它只是桌宠，不是第二个主窗口）；
        · 点它/拖它**不抢当前程序焦点**，气泡看完还能接着打字。
        失败只影响这两个“礼貌性”行为，不影响桌宠本身。
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
        except Exception:
            pass

    # ---- 生命周期 ----

    def show(self) -> None:
        if not self._alive:
            return
        try:
            self.top.deiconify()
            self.top.lift()
        except tk.TclError:
            return
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
        try:
            self.top.destroy()
        except tk.TclError:
            pass

    def set_paused(self, paused: bool) -> None:
        """暂停自动走动（气泡/菜单打开时用），呼吸动画保留。"""
        self._paused = bool(paused)
        if self._paused:
            self._walk_remaining = 0

    def move_to(self, x: int, y: int) -> None:
        """把窗口移到指定坐标（自动夹在屏幕内），并通知保存。"""
        self._move_window(int(x), int(y))
        self._notify_moved(force=True)

    def set_height(self, height: int) -> None:
        """换一档尺寸：保持窗口底边中心不动，不跑到屏幕外。"""
        height = int(height)
        if height == self._height and self._images:
            return
        cx = self._x + self._w // 2
        bottom = self._y + self._h
        self._load_height(height, keep_bottom=True, x=cx, y=bottom)
        self._notify_moved(force=True)

    def bounds(self) -> tuple[int, int, int, int]:
        """`(x, y, width, height)`，给气泡定位用。"""
        return self._x, self._y, self._w, self._h

    # ---- 尺寸 / 精灵 ----

    def _load_height(self, height: int, *, keep_bottom: bool,
                     x: int | None = None, y: int | None = None) -> None:
        if height not in available_heights():
            height = dict(SIZE_PRESETS)[DEFAULT_SIZE]
        images = {
            name: tk.PhotoImage(master=self.top, file=sprite_path(height, name))
            for name in ("front", "side", "side_left", "back")
        }
        self._height = height
        self._images = images
        self._w = max(img.width() for img in images.values()) + 12
        self._h = max(img.height() for img in images.values()) + 16
        self.canvas.configure(width=self._w, height=self._h)
        self.canvas.coords(self._image_item, self._w // 2, self._h - 6)
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
        self._render(0)

    def _geometry(self) -> None:
        try:
            self.top.geometry(f"{self._w}x{self._h}+{self._x}+{self._y}")
        except tk.TclError:
            pass

    def _render(self, bob: int) -> None:
        try:
            self.canvas.itemconfigure(self._image_item,
                                      image=self._images[self._sprite])
            self.canvas.coords(self._image_item, self._w // 2,
                               self._h - 6 - max(0, int(bob)))
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
        try:
            if self._walk_remaining > 0 and not self._paused and not self._dragging:
                step = 2 * self._walk_dir
                before = self._x
                self._move_window(self._x + step, self._y)
                if self._x == before:
                    self._walk_remaining = 0
                else:
                    self._walk_remaining -= 1
                    self._sprite = "side_left" if self._walk_dir < 0 else "side"
                    self._notify_moved()
            else:
                self._sprite = "front"
                if (not self._paused and not self._dragging
                        and now >= self._next_walk):
                    self._maybe_walk(now)

            if self._walk_remaining > 0 and not self._dragging:
                bob = abs(math.sin(now * 10.0)) * 3.0
            else:
                bob = 2.0 + 2.0 * math.sin(now * 2.2)
            if now < self._hop_until:
                left = max(0.0, (self._hop_until - now) / 0.5)
                bob += 16.0 * math.sin(math.pi * min(1.0, left))
            self._render(round(bob))
        finally:
            self._schedule_tick()

    def _maybe_walk(self, now: float) -> None:
        self._next_walk = now + self._rng.uniform(9.0, 18.0)
        if self._rng.random() > 0.45:
            return
        distance = self._rng.randint(12, 44)
        self._walk_dir = self._rng.choice((-1, 1))
        self._walk_remaining = distance // 2

    # ---- 鼠标 ----

    def _on_press(self, event) -> None:
        self._dragging = True
        self._moved = False
        self._drag_offset = (int(event.x_root) - self._x,
                             int(event.y_root) - self._y)
        self._walk_remaining = 0
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
        self._hop_until = time.monotonic() + 0.5
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
        if self._on_double is not None:
            try:
                self._on_double(self)
            except Exception:
                pass

    def _on_right_click(self, event) -> None:
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
