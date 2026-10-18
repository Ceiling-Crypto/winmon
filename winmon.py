# -*- coding: utf-8 -*-
"""
WinMon —— Windows 桌面程序实时监控与终止工具（图形界面 + 命令行）
==================================================================

功能
----
1. 列出屏幕上正在运行的程序：程序图标 / 窗口标题 / PID / 内存 / CPU / 状态
2. 选中即可看到该程序的**实时画面**（分辨率与刷新率可调）
3. 「实时总览」把多个程序画面排成一屏当监控墙
4. 「进程树」按父子层级展示，结束进程树前能看清到底会波及哪些进程
5. 一键终止：优雅关闭（发关闭消息）/ 强制结束 / 结束整棵进程树
6. 操作历史：谁在什么时候被关闭/结束了，可复制、可导出
7. 前台窗口实时高亮、置顶窗口标记、程序图标、隐藏窗口过滤

命令行
------
    python winmon.py               启动图形界面
    python winmon.py --scan        打印当前窗口清单（不开界面）
    python winmon.py --scan --all  连同隐藏窗口一起列出
    python winmon.py --json        以 JSON 输出
    python winmon.py --selftest    环境自检（枚举/捕获/渲染/图标/终止）
    python winmon.py --smoke       界面自检并截图
    python winmon.py --catalog     列出程序目录（按类别分组）
    python winmon.py --catalog --category 游戏娱乐   只看某一类
    python winmon.py --catalog --favorites           只看收藏
    python winmon.py --catalog --recent              只看最近使用
    python winmon.py --find 哔哩    按关键词查找
    python winmon.py --open 哔哩哔哩 按关键词精准打开

核心实现见 winmon_core.py（可单独当库用）。
"""

from __future__ import annotations

import ctypes
import json
import os
import queue
import re
import shutil
import sys
import threading
import time
import tkinter as tk
from tkinter import messagebox, ttk

import winmon_ai as ai
import winmon_ai_provider as provider
import winmon_core as core
import winmon_mcp as mcp

APP_NAME = "WinMon"
#: 正式版本号。2026-10-11 这次「逐文件 SHA256 核对 C/D 两份副本 + 统一测试基线」
#: 之后，项目正式定名为 **WinMon 1.0**（`APP_RELEASE`），版本号从这里重新起算
#: （所以它比上一版的 "1.1" 看起来更小 —— 这是一次重新命名，不是回退）。
APP_VER = "1.0"
#: 发布名（用户看到的产品名）：winmon1.0
APP_RELEASE = "winmon1.0"
APP_TITLE = f"WinMon {APP_VER} — 桌面程序实时监控与终止"

#: 设计稿窗口尺寸（**逻辑像素**，也就是"100% 缩放"下的版式基准）。
#: 真正开窗时会按 DPI 放大（见 `scaled_window_size()`）：本进程是 DPI 感知的，
#: 150% 下字体与控件都比设计稿宽 1.5 倍 —— 窗口不跟着放大，就真的有控件被挤没。
APP_W, APP_H = 1280, 800
APP_MIN_W, APP_MIN_H = 1080, 660


def dpi_scale_from(fpixels_per_inch) -> float:
    """
    把"每英寸像素数"换成缩放比例（96 DPI = 1.0），并夹在 [1.0, 3.0]。

    为什么要有这道夹取：读不到/读到离谱的值时按 1.0 处理 ——
    版式宁可小一点，也不能把窗口撑到屏幕外面去。纯逻辑，可单测。
    """
    try:
        value = float(fpixels_per_inch)
    except (TypeError, ValueError):
        return 1.0
    if not value > 0:            # <=0 与 NaN 都走这里（NaN 的比较恒为假）
        return 1.0
    return max(1.0, min(3.0, value / 96.0))


def scaled_window_size(scale: float, screen=(0, 0),
                       design=(APP_W, APP_H)) -> tuple[int, int]:
    """
    按 DPI 缩放算开窗尺寸，并**夹到屏幕内**（纯逻辑，可单测）。

    为什么必须按 DPI 放大窗口（2026-10-13 修的真 bug）：`main()` 会先
    `enable_dpi_awareness()`，于是 150% 缩放下字体与控件都是设计稿的 1.5 倍，
    而窗口一直是 1280×800 **物理像素** —— 实测顶部「加入总览」被压到 17px
    （109px 需要的按钮只剩一条缝）、媒体那行的说明被压到 14px，
    等于**看不见**。把窗口按同一比例放大，版式比例就与设计稿一致。
    （`test_a5` 抓不到这类问题：测试进程没有开 DPI 感知，字体没被放大。）

    屏幕装不下时退回"屏幕的 98% / 92%"（留出任务栏与标题栏），
    但**不小于设计稿** —— 比设计稿还小才是真的挤坏，那属于小屏硬限制。
    """
    scale = max(1.0, min(3.0, float(scale or 1.0)))
    w = int(round(design[0] * scale))
    h = int(round(design[1] * scale))
    sw, sh = (list(screen) + [0, 0])[:2]
    if sw and sh:
        w = max(design[0], min(w, int(sw * 0.98)))
        h = max(design[1], min(h, int(sh * 0.92)))
    return w, h

# 界面排版常量
UI_FONT = ("Microsoft YaHei UI", 9)
BOLD_FONT = ("Microsoft YaHei UI", 10, "bold")
MONO_FONT = ("Consolas", 9)
TILE_W, TILE_H = 216, 150
# 列表里程序图标的边长。注意：Treeview 行高必须大于图标高度，否则图标会被行裁剪
# （开发中曾因此表现为"图标只剩一条竖条"），故行高取 ICON_SIZE + 6。
#
# 尺寸怎么定的：这是个"像任务管理器那样的列表"，要**小巧同时清楚**，所以三个候选都实测过
#   · 16×16（对应 SHGFI_SMALLICON）：最省空间，但图标细节丢得多
#   · 24×24：SHGetFileInfoW 在 24 会走 LARGEICON 再缩，线条偏软
#   · 32×32（SHGFI_LARGEICON 原生）：最清楚，但行高 38、一屏少看约 20% 条目
# 取 **28**：由原生 32 缩放而来（比 24 保留了更多细节），行高 34，
# 比 32 省一点空间、比 24 清楚 —— 也就是"小巧同时清楚"的折中。
# 设 WINMON_ICON_SIZE 环境变量可覆盖（调试用，不必改代码）。
ICON_SIZE = int(os.environ.get("WINMON_ICON_SIZE") or 28)

PREVIEW_SIZES = [
    ("小 (340×220)", 340, 220),
    ("中 (480×300)", 480, 300),
    ("大 (640×400)", 640, 400),
    ("特大 (800×500)", 800, 500),
]
FPS_CHOICES = [("极省电 1 FPS", 1000), ("省电 2 FPS", 500), ("流畅 5 FPS", 200),
               ("很流畅 10 FPS", 100), ("极流畅 15 FPS", 66), ("最高 30 FPS", 33)]
# ---- 左栏列表的点击手势（`plan_window_click` 的返回值）----
CLICK_NONE = "none"          # 什么都不做（单击只选中）
CLICK_ACTIVATE = "activate"  # 还原并切到最前
CLICK_MINIMIZE = "minimize"  # 最小化

# ---- 点窗口 × 时的行为（设置里可改，**默认结束进程**）----
CLOSE_MODE_TRAY = "tray"     # 隐藏到托盘（音乐继续放、监控继续跑）
CLOSE_MODE_EXIT = "exit"     # 真的退出
CLOSE_MODE_DEFAULT = CLOSE_MODE_EXIT
CLOSE_MODE_CHOICES: tuple[tuple[str, str], ...] = (
    (CLOSE_MODE_EXIT, "结束进程"),
    (CLOSE_MODE_TRAY, "隐藏到托盘"),
)

# ---- 媒体会话轮询 ----
#: 每 4 秒读一次系统媒体会话。这个数不是随手定的：一次读取要走
#: PowerShell + WinRT 投影，实测约 290ms，所以放在后台线程里、间隔取 4 秒
#: —— 既够"看得见在放什么"，又不会把 CPU 耗在一个辅助功能上。
MEDIA_POLL_SECONDS = 4.0

#: 「指定媒体应用」下拉框里代表"不指定（自动挑正在放的那个）"的那一项
MEDIA_APP_ANY = "（自动：正在放的那个）"


def build_media_app_entries(running, available, found, sessions=(),
                            any_label: str = MEDIA_APP_ANY):
    """
    组装「媒体应用」下拉框的清单 —— **纯逻辑**（不碰控件、不碰系统），便于单测。

    参数：
      · `running`   ：正在运行的进程名（小写）集合
      · `available` ：`callable(exe) -> bool`，这个 exe 在这台机器上找不找得到
      · `found`     ：`core.installed_media_apps()` 的结果（开始菜单里发现的那些）
      · `sessions`  ：**此刻活着的媒体会话的 app id**（`core.session_app_ids()`）——
                      第一真源，见下面规矩 ⓪。

    返回 `(labels, {label: MediaApp})`。

    三条规矩（都是实测踩出来的）：

      ⓪ **有会话的程序一定在清单里**（2026-10-16 新增）：谁能发布系统媒体会话，
         谁就自动出现在下拉框里并可被**精确控制** —— 与写死的候选表、与
         "开始菜单里有没有它"都无关。这样"用户装了个表里没有的播放器"
         "换一台机器"都不会漏（这就是用户要的"基于所有可识别的媒体"）。
         会话条目**排在最前面**（此刻真在放东西的最该被看到），并且**先占住主干**，
         后面两轮就不会再为同一个程序列出第二条。

      ① **同一个程序只列一条**：写死的候选表与开始菜单会撞车 —— 实测 Edge 出现过
         两次（`Microsoft Edge（浏览器）（运行中）` 与 `…（菜单）`）。去重按
         `core.media_app_stem()` 比**主干**，并且**优先保留带 exe 名的那条**：
         它带着 exe 名，"按应用发命令"才真的发得出去。
         （原来拿 `MediaApp.key`（exe 名）与"菜单项的显示名"混在一个 `taken` 集合里比，
         两种东西根本不同源，于是永远去不掉重复。）

      ② **开始菜单条目要补 exe**：它本身只有 `.lnk`（`MediaApp.exe` 是空串），
         按名字能在候选表里对上就补上 `core.media_app_exe_for_label()`，
         否则命令会发给"第一个会话"（用户选了 A、控的却是 B）。
    """
    labels: list[str] = [any_label]
    mapping: dict = {any_label: None}
    taken: set[str] = set()

    # ⓪ 第一真源：此刻活着的媒体会话（与候选表、开始菜单都无关）
    for app_id in sessions or ():
        app = core.media_app_from_session(app_id, running)
        if app is None:
            continue
        stem = core.media_app_stem(app.label)
        if not stem or stem in taken:
            continue
        # 认得出候选表的（`kind` 为空）才叠「（运行中）」—— 与以前给 Edge/网易云
        # 的标签完全一致；从会话发现的**陌生**应用，`app.title` 里那个「（媒体会话）」
        # 已经把"它是活的、而且是从系统媒体会话里认出来的"说清楚了，不再叠第二个词。
        mark = "（运行中）" if not app.kind else ""
        text = f"{app.title}{mark}"
        if text in mapping:
            continue
        labels.append(text)
        mapping[text] = app
        taken.add(stem)

    # ① 候选表：装了或正在跑的那些（会话那一轮已经列过的会被 `taken` 挡掉）
    for label, exe, browser in core.MEDIA_APP_CANDIDATES:
        if not available(exe) and exe.lower() not in running:
            continue                      # 没装也没在跑：不列出来
        stem = core.media_app_stem(label)
        if stem in taken:
            continue
        app = core.MediaApp(label, exe, browser)
        mark = "（运行中）" if exe.lower() in running else ""
        text = f"{app.title}{mark}"
        if text in mapping:
            continue
        labels.append(text)
        mapping[text] = app
        taken.add(stem)

    # ② 开始菜单里发现的（媒体 / 浏览器两类）
    for app in found or []:
        stem = core.media_app_stem(app.label)
        if not stem or stem in taken:
            continue                      # 前面已经列过同一个程序
        text = f"{app.title}（菜单）"
        if text in mapping:
            continue
        exe = app.exe or core.media_app_exe_for_label(app.label)
        labels.append(text)
        mapping[text] = core.MediaApp(app.label, exe, app.browser, app.path,
                                      getattr(app, "kind", ""))
        taken.add(stem)
    mapping[any_label] = None
    return labels, mapping


#: 操作历史日志**保留天数**（约定：默认 30 天自动清理，另设 7 天 / 90 天）
HISTORY_DAYS_CHOICES: tuple[tuple[int, str], ...] = (
    (7, "7 天"), (30, "30 天"), (90, "90 天"),
)
HISTORY_DAYS_DEFAULT = 30


def fit_media_text(text: str, budget: int = 78) -> str:
    """
    把"正在播放"那一行裁到标签装得下的长度，超出部分用 `…` 收尾（纯逻辑，可单测）。

    为什么要在代码里裁（2026-10-13 修的真问题）：那个标签是**定宽**的
    （不定宽的话长曲名会把右边的按钮挤坏 —— `test_a5` 的挤压检查守着这条），
    而定宽控件会**默默把文字切掉**、连省略号都没有：实测
    `网易云音乐：us feat. ゆきな - 澤田空海理（从窗口标题读的）` 需要 335px
    而标签只有 300px，于是"（从窗口标题读的）"这个**来源标注**被吞掉，
    多会话那句"还有 N 个在放（…）"更是整句看不见 —— 用户会以为后面没有内容。

    按"中文算 2 个宽度单位、西文算 1 个"来裁（与定宽标签的算法一致）。
    """
    budget = max(8, int(budget))
    limit = budget - 1                  # 给收尾的 `…` 留一个位置
    used = 0
    kept: list[str] = []
    for ch in str(text or ""):
        unit = 2 if ord(ch) > 0x2E80 else 1
        if used + unit > limit:
            return "".join(kept) + "…"
        used += unit
        kept.append(ch)
    return "".join(kept)


def plan_window_click(gesture: str, iconic: bool = False) -> str:
    """
    左栏列表的手势决策 —— **纯函数**，独立于窗口操作本身。

    为什么要拆开：真去抢前台、真去最小化，在测试里反复触发会把测试机的桌面
    搅得一团乱（而且结果依赖当时谁在前台）。所以这里只回答"该做什么"，
    由 `WinMonApp` 去执行，测试则逐条核对这张决策表。

    ## 手势表（用户 2026-10-11 定的）

    | 手势 | 动作 |
    | --- | --- |
    | **单击** | **只选中**（右侧出实时预览）—— 刻意**不碰任何窗口** |
    | **双击** | 该窗口**已最小化 → 还原并切到最前**；**否则 → 最小化** |
    | Enter / 中键 / 右键菜单 | 切到最前（最小化的先还原） |

    ## 两条都是踩过坑才定下来的

    **双击为什么看"窗口自己是否最小化"**：第一版规则是"按下之前它已在最前 → 最小化"，
    实测双击**只会唤醒、从来不最小化**。两个原因，第二个致命：
      1. "已在最前"靠 700ms 轮询维护，正常节奏（瞟一眼 → 立刻回 WinMon 双击）
         根本达不到 700ms，信号常年过期；
      2. 人已经在 WinMon 里了，**此刻双击哪一行，那一行按定义就不是前台窗口** ——
         那条分支几乎永远不可达。
    改成看窗口自己的状态之后，两个方向都随时可达，也不用再维护任何历史状态。

    **单击为什么不切前台**：这工具的本职是"看着、不打扰"。单击就抢焦点的话，
    你只是想点一行看看它的画面，也会把正在干活的窗口赶下去。
    所以"切到最前"改由 Enter / 中键 / 右键菜单承担。
    """
    if gesture == "double":
        return CLICK_ACTIVATE if iconic else CLICK_MINIMIZE
    if gesture == "foreground":
        return CLICK_ACTIVATE
    return CLICK_NONE


#: 「关闭视频」下拉框：slug（存设置）/ 中文（给用户看）
#: **第一项就是默认项** —— `_close_mode_label()` 与下拉框初始值都取它。
#: 用户已放弃"自动打开视频"，但**关闭方式仍然要选**，所以这份表保留。
VIDEO_CLOSE_MODES: tuple[tuple[str, str], ...] = (
    ("window", "关闭当前窗口"),
    ("close", "关闭当前标签页"),
    ("pause", "暂停视频"),
    ("back", "返回上一页"),
    ("smart", "暂停并返回"),
)
DEFAULT_VIDEO_CLOSE_MODE = "window"


def _close_mode_label(slug: str) -> str:
    """slug → 下拉框显示的中文。认不出来就退回第一项（默认项）。"""
    for s, label in VIDEO_CLOSE_MODES:
        if s == slug:
            return label
    return VIDEO_CLOSE_MODES[0][1]


def _icon_path() -> str:
    """托盘/窗口用的图标文件（`winmon.ico`，与主程序同目录；没有就返回空串）。"""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "winmon.ico")
    return path if os.path.exists(path) else ""


def _idle_seconds_of(label: str) -> int:
    """闲置时长下拉框里的中文 → 秒数。认不出来就退回默认（30 分钟）。"""
    for text, value in core.IDLE_CHECK_CHOICES:
        if text == label:
            return value
    return core.IDLE_DEFAULT_SECONDS


def act_text(wi, is_fg: bool) -> str:
    """
    「活动」列里的状态标记。

        ●  当前活动窗口
        ▁  已最小化
        顶 置顶

    为什么要几个小符号而不是只靠行颜色：行的 tag 是**互斥**的（见 `_insert_window_row`
    里的 if/elif —— 最小化和置顶可以同时成立，但只能打一个 tag），
    而"最小化 + 置顶"这种组合是真会出现的。加一列太占地方（右栏只剩约 560px），
    所以把它们压进这一列。
    """
    return (("●" if is_fg else "") + ("▁" if wi.iconic else "")
            + ("顶" if wi.always_on_top else ""))


class _RowTip:
    """
    鼠标在某一行上停一会儿时，弹出一行说明（程序名 / 状态 / 能做什么 / 已闲置多久）。

    为什么要自己写：Tk 没有内置 tooltip，而项目约定不引入第三方库。几个必须注意的点：

      · `overrideredirect(True)` + 不 `grab`、不 `focus` —— 提示条**绝不能**把焦点
        从列表上抢走，否则"双击最小化"这类手势会被它打断；
      · **延迟出现**（默认 550ms）：鼠标扫过列表时不至于一路闪；
      · **复用同一个 Toplevel**，只改文字与位置 —— 每次新建窗口会明显卡顿；
      · 位置要夹在屏幕内，否则贴右边缘时提示会跑到屏幕外。
    """

    #: 鼠标停在同一行多久才弹（毫秒）。太短会闪，太长等于没有。
    DELAY_MS = 550

    def __init__(self, widget: tk.Misc, text_for) -> None:
        self.widget = widget
        self.text_for = text_for
        self.top: tk.Toplevel | None = None
        self.label: tk.Label | None = None
        self._after_id = None
        self._row = ""
        self._xy = (0, 0)
        widget.bind("<Motion>", self._on_motion, add="+")
        widget.bind("<Leave>", lambda _e: self.hide(), add="+")
        # 一按鼠标就收起来：不然提示条会挡着刚点开的东西
        widget.bind("<ButtonPress>", lambda _e: self.hide(), add="+")

    # ---- 内部 ----

    def _ensure(self) -> tk.Toplevel:
        if self.top is None:
            self.top = tk.Toplevel(self.widget)
            self.top.overrideredirect(True)
            self.top.attributes("-topmost", True)
            self.label = tk.Label(self.top, font=("Microsoft YaHei UI", 9),
                                  bg="#243040", fg="#eaf2f8", justify="left",
                                  bd=1, relief="solid", padx=8, pady=5)
            self.label.pack()
            self.top.withdraw()
        return self.top

    def _on_motion(self, event) -> None:
        try:
            row = self.widget.identify_row(event.y)
        except tk.TclError:
            return
        self._xy = (event.x, event.y)
        if row == self._row:
            return
        self._row = row
        self.hide()
        if row:
            self._after_id = self.widget.after(
                self.DELAY_MS, lambda r=row: self._show(r))

    def _show(self, row: str) -> None:
        self._after_id = None
        if not row or row != self._row:
            return
        try:
            text = self.text_for(row)
        except Exception:
            text = ""
        if not text:
            return
        top = self._ensure()
        assert self.label is not None
        self.label.configure(text=text)
        top.update_idletasks()
        x, y = self._xy
        rx = self.widget.winfo_rootx() + x + 16
        ry = self.widget.winfo_rooty() + y + 22
        sw, sh = top.winfo_screenwidth(), top.winfo_screenheight()
        w, h = top.winfo_reqwidth(), top.winfo_reqheight()
        top.geometry(f"+{max(0, min(rx, sw - w - 6))}+{max(0, min(ry, sh - h - 6))}")
        top.deiconify()
        top.lift()

    def hide(self) -> None:
        """收起提示条（并取消还没到点的延迟弹出）。"""
        if self._after_id is not None:
            try:
                self.widget.after_cancel(self._after_id)
            except Exception:
                pass
            self._after_id = None
        if self.top is not None:
            try:
                self.top.withdraw()
            except tk.TclError:
                pass

    def destroy(self) -> None:
        """彻底销毁（关程序时调，避免留下孤儿 Toplevel）。"""
        self.hide()
        if self.top is not None:
            try:
                self.top.destroy()
            except tk.TclError:
                pass
            self.top = None


def _close_mode_slug(value: str) -> str:
    """
    下拉框里的中文（或旧设置里存的 slug）→ 核心层认识的 slug。

    容忍两种形式的原因：设置文件里以前存的是 "smart"/"pause"/"back"/"close"，
    老版本升级上来不能因为认不出就把用户的选择丢掉。
    """
    v = (value or "").strip()
    for slug, label in VIDEO_CLOSE_MODES:
        if v == slug or v == label:
            return slug
    return DEFAULT_VIDEO_CLOSE_MODE


def _video_dialog_text(mode: str) -> tuple[str, str]:
    """
    弹窗标题问什么、确认按钮写什么 —— 纯函数，可单测。

    为什么要写这么细：按钮上必须写清楚**点了会发生什么**。
    原来的按钮只写"是，关闭 (Space)"，而实际动作可能只是暂停一下 ——
    用户点完发现视频还在，就会以为程序坏了。默认动作现在是"关闭当前窗口"，
    按钮上就照着写"关闭窗口"。
    """
    mapping = {
        "window": ("是否关闭当前窗口？", "是，关闭窗口 (Space)"),
        "close": ("是否关闭当前标签页？", "是，关闭标签页 (Space)"),
        "pause": ("是否暂停当前视频？", "是，暂停 (Space)"),
        "back": ("是否返回上一页？", "是，返回上一页 (Space)"),
        "smart": ("是否停止当前视频？", "是，暂停并返回 (Space)"),
    }
    return mapping.get(mode, mapping[DEFAULT_VIDEO_CLOSE_MODE])


def _relative_time(ts) -> str:
    """
    把时间戳变成"多久以前"（用于「最近使用」列表）。

    为什么不用绝对时间：用户关心的是"这个我多久没用了"，
    "3 分钟前 / 2 天前"比 "2026-10-10 18:22:31" 直观得多。
    """
    if ts is None or ts == "":
        return "时间未知"
    try:
        ts = float(ts)
    except (TypeError, ValueError):
        return "时间未知"
    if ts <= 0:
        return "时间未知"          # 0 = 1970，说明记录里没写时间，不要显示"56 年前"
    delta = time.time() - ts
    if delta < 0:
        return "刚刚"
    if delta < 60:
        return "刚刚"
    if delta < 3600:
        return f"{int(delta // 60)} 分钟前"
    if delta < 86400:
        return f"{int(delta // 3600)} 小时前"
    if delta < 86400 * 30:
        return f"{int(delta // 86400)} 天前"
    if delta < 86400 * 365:
        return f"{int(delta // (86400 * 30))} 个月前"
    return f"{int(delta // (86400 * 365))} 年前"


def _local_day(ts: float = 0.0) -> str:
    """
    本地日历日 `YYYY-MM-DD` —— 「闲置提醒：每天只提醒一次」的判据。

    为什么放在界面层而不是核心层：`IdleWatcher` 刻意做成**纯逻辑**
    （不碰 Win32、也不读墙上时钟），日期由这里算好传进去，
    这样"跨过零点才允许再提醒"这条规则能确定性单测，不用等真的过夜。

    `ts` 可以在测试里直接给，生产调用一律不传（取当前时刻）。
    """
    return time.strftime("%Y-%m-%d", time.localtime(ts or time.time()))


# ============================================================================
# 一、图标 -> tkinter PhotoImage
# ============================================================================

class IconProvider:
    """把核心层产出的 base64 PNG 变成 tkinter 可用的 PhotoImage（带缓存）。"""

    def __init__(self, size: int = ICON_SIZE) -> None:
        self.cache = core.IconCache(size)
        self._photos: dict[str, tk.PhotoImage] = {}
        self.failed = 0

    def photo(self, wi) -> tk.PhotoImage | None:
        png = self.cache.get(wi)
        if not png:
            return None
        key = png[:80]                      # 同一个图标只建一次 PhotoImage
        img = self._photos.get(key)
        if img is None:
            try:
                img = tk.PhotoImage(data=png)
            except tk.TclError:
                self.failed += 1
                return None
            self._photos[key] = img
        return img

    def forget_hwnd(self, hwnd: int) -> None:
        self.cache.forget_hwnd(hwnd)


# ============================================================================
# 二、实时预览面板
# ============================================================================

class PreviewPane:
    """右侧详情预览面板：显示单个程序的实时画面。"""

    def __init__(self, parent: tk.Widget, app: "WinMonApp") -> None:
        self.app = app
        self.frame = ttk.Frame(parent)
        self.frame.rowconfigure(1, weight=1)
        self.frame.columnconfigure(0, weight=1)

        head = ttk.Frame(self.frame)
        head.grid(row=0, column=0, sticky="ew", padx=6, pady=(6, 2))
        head.columnconfigure(1, weight=1)
        self.lbl_title = ttk.Label(head, text="（未选择程序）", font=BOLD_FONT, anchor="w")
        self.lbl_title.grid(row=0, column=0, columnspan=2, sticky="ew")
        self.lbl_meta = ttk.Label(head, text="", font=UI_FONT, foreground="#666", anchor="w")
        self.lbl_meta.grid(row=1, column=0, sticky="ew")
        self.lbl_stats = ttk.Label(head, text="", font=MONO_FONT, foreground="#0a7", anchor="e")
        self.lbl_stats.grid(row=1, column=1, sticky="e")

        holder = tk.Frame(self.frame, bg="#101418", bd=1, relief="solid")
        holder.grid(row=1, column=0, sticky="nsew", padx=6, pady=4)
        holder.rowconfigure(0, weight=1)
        holder.columnconfigure(0, weight=1)
        self.canvas = tk.Canvas(holder, bg="#101418", highlightthickness=0)
        self.canvas.grid(row=0, column=0, sticky="nsew")
        self.img_item = self.canvas.create_image(0, 0, anchor="nw")
        self.canvas.bind("<Configure>", self._on_canvas_resize)
        # 双击画面 = 最小化/还原**正在看的这个窗口**，与左栏列表的手势保持一致
        # （以前这里是"切到前台"，同一个手势在两处含义不同最容易形成错误直觉）。
        self.canvas.bind("<Double-1>", lambda e: self.app.action_minimize_window())

        self.photo: tk.PhotoImage | None = None
        self.base_photo: tk.PhotoImage | None = None
        self.photo_w = self.photo_h = 0
        self.base_w = self.base_h = 0
        self.scale = 1
        self._display_mode: tuple[int, int] = (0, 0)
        self.placeholder = self.canvas.create_text(
            12, 12, anchor="nw", fill="#5a6a78", font=UI_FONT, justify="left",
            text=("请选择左侧列表中的程序\n\n"
                  "· 选中后此处显示该程序的实时画面\n"
                  "· 双击画面 = 最小化/还原该程序（与列表一致）\n"
                  "· 列表里单击只选中；Enter / 中键切到前台\n"
                  "· 底部按钮可关闭或结束该程序"))

        btns = ttk.Frame(self.frame)
        btns.grid(row=2, column=0, sticky="ew", padx=6, pady=(2, 0))
        ttk.Button(btns, text="关闭窗口", width=10,
                   command=self.app.action_close_window).pack(side="left", padx=1)
        ttk.Button(btns, text="结束进程", width=10,
                   command=self.app.action_terminate).pack(side="left", padx=1)
        ttk.Button(btns, text="结束进程树", width=12,
                   command=self.app.action_terminate_tree).pack(side="left", padx=1)
        ttk.Button(btns, text="切到前台", width=10,
                   command=self.app.action_bring_front).pack(side="left", padx=1)
        self.btn_pause = ttk.Button(btns, text="暂停刷新", width=10, command=self.app.toggle_pause)
        self.btn_pause.pack(side="right", padx=1)

        self.hint = ttk.Label(self.frame, text="", font=UI_FONT, foreground="#888", anchor="w")
        self.hint.grid(row=3, column=0, sticky="ew", padx=8, pady=(2, 6))

    # -- 画面显示 --
    # 显示链路：抓取 → 抽样成小图 → PhotoImage 缩放 → canvas 居中
    #   · 抽样把 Python 侧像素生成量降到 1/scale²（这是主要耗时点）
    #   · zoom / subsample 由 Tk 在 C 层完成，比在 Python 里重建像素快得多
    def _on_canvas_resize(self, _event=None) -> None:
        self._place_image()

    def _ensure_display(self) -> None:
        """按面板大小决定显示用的缩放（zoom 或 subsample），必要时重建显示图。"""
        if self.base_photo is None or self.photo_w <= 0:
            return
        cw = max(2, self.canvas.winfo_width())
        ch = max(2, self.canvas.winfo_height())
        nz, ns = 1, 1
        if self.photo_w > cw or self.photo_h > ch:
            ns = max(1, -(-self.photo_w // cw), -(-self.photo_h // ch))
        else:
            fit = min(cw / self.photo_w, ch / self.photo_h)
            if fit >= 1.2:
                nz = max(1, min(6, int(fit)))
        if (nz, ns) == self._display_mode and self.photo is not None:
            return
        self._display_mode = (nz, ns)
        img = self.base_photo
        if self.scale > 1:
            img = img.zoom(self.scale)
        if nz > 1:
            img = img.zoom(nz)
        if ns > 1:
            img = img.subsample(ns)
        self.photo = img
        self.photo_w, self.photo_h = img.width(), img.height()
        self.canvas.itemconfigure(self.img_item, image=img)

    def _place_image(self) -> None:
        if self.base_photo is None or self.photo_w <= 0 or self.photo_h <= 0:
            self.canvas.coords(self.placeholder, 12, 12)
            self.canvas.itemconfigure(self.placeholder, state="normal")
            return
        self.canvas.itemconfigure(self.placeholder, state="hidden")
        self._ensure_display()
        cw = self.canvas.winfo_width()
        ch = self.canvas.winfo_height()
        x = max(0, (cw - self.photo_w) // 2) if cw > 1 else 0
        y = max(0, (ch - self.photo_h) // 2) if ch > 1 else 0
        self.canvas.coords(self.img_item, x, y)

    def set_frame(self, data: str, base_w: int, base_h: int, scale: int = 1) -> None:
        """data 是按 (base_w × base_h) 抽样得到的像素串，scale 为抽样间隔。"""
        scale = max(1, int(scale))
        if self.base_photo is None or self.base_w != base_w or self.base_h != base_h:
            self.base_photo = tk.PhotoImage(width=base_w, height=base_h)
            self.base_w, self.base_h = base_w, base_h
            self._display_mode = (0, 0)
        try:
            self.base_photo.put(data)
        except tk.TclError:
            return
        self.scale = scale
        img = self.base_photo.zoom(scale) if scale > 1 else self.base_photo
        self.photo = img
        self.photo_w, self.photo_h = img.width(), img.height()
        self._display_mode = (1 if scale > 1 else 0, 1)
        self.canvas.itemconfigure(self.img_item, image=img)
        self._place_image()

    def clear(self, text: str = "") -> None:
        self.photo = None
        self.base_photo = None
        self.base_w = self.base_h = 0
        self.photo_w = self.photo_h = 0
        self._display_mode = (0, 0)
        self.canvas.itemconfigure(self.img_item, image="")
        if text:
            self.canvas.itemconfigure(self.placeholder, text=text, state="normal")
            self.canvas.coords(self.placeholder, 12, 12)

    def set_title(self, text: str) -> None:
        self.lbl_title.configure(text=text)

    def set_meta(self, text: str) -> None:
        self.lbl_meta.configure(text=text)

    def set_stats(self, text: str) -> None:
        self.lbl_stats.configure(text=text)

    def set_hint(self, text: str) -> None:
        self.hint.configure(text=text)


# ============================================================================
# 三、实时总览（监控墙）
# ============================================================================

class GalleryPane:
    """多个程序画面同时显示的监控墙。"""

    def __init__(self, parent: tk.Widget, app: "WinMonApp") -> None:
        self.app = app
        self.frame = ttk.Frame(parent)
        self.frame.rowconfigure(0, weight=1)
        self.frame.columnconfigure(0, weight=1)

        self.canvas = tk.Canvas(self.frame, bg="#0d1116", highlightthickness=0)
        self.canvas.grid(row=0, column=0, sticky="nsew")
        bar = ttk.Scrollbar(self.frame, orient="vertical", command=self.canvas.yview)
        bar.grid(row=0, column=1, sticky="ns")
        self.canvas.configure(yscrollcommand=bar.set)

        self.inner = ttk.Frame(self.canvas)
        self.canvas.create_window((0, 0), window=self.inner, anchor="nw", tags="inner")
        self.inner.bind("<Configure>", self._on_inner_configure)
        self.canvas.bind("<Configure>", self._on_canvas_configure)
        self.canvas.bind_all("<MouseWheel>", self._on_wheel)

        self.tiles: dict = {}
        #: 拖拽排序（B.3）用：正在拖的那一格 / 悬停在哪一格
        self._drag_key = None
        self._drag_hover = None
        self.empty_label = ttk.Label(
            self.inner, font=UI_FONT, foreground="#7a8a99", justify="left",
            text=("实时总览为空\n\n"
                  "在左侧列表中按住 Ctrl 或 Shift 多选程序，\n"
                  "然后点击上方「加入总览」即可把它们同时显示在这里。"))
        self.empty_label.pack(padx=16, pady=16, anchor="w")
        self._cols = 0

    def _on_wheel(self, event) -> None:
        if self.app.notebook.index("current") != 1:
            return
        self.canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

    def _on_inner_configure(self, _e=None) -> None:
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))

    def _on_canvas_configure(self, event) -> None:
        self.canvas.itemconfigure("inner", width=event.width)
        cols = max(1, event.width // (TILE_W + 12))
        if cols != self._cols:
            self._cols = cols
            self.relayout()

    def add(self, key, title: str, meta: str = "") -> None:
        if key in self.tiles:
            return
        cell = ttk.Frame(self.inner, relief="solid", borderwidth=1)
        head = ttk.Label(cell, text=title, font=UI_FONT, anchor="w", width=26)
        head.pack(fill="x", padx=2)
        holder = tk.Frame(cell, bg="#000", width=TILE_W, height=TILE_H - 40)
        holder.pack(padx=2)
        holder.pack_propagate(False)
        lbl = tk.Label(holder, bg="#000", text="等待画面…", fg="#556", font=UI_FONT)
        lbl.pack(fill="both", expand=True)
        foot = ttk.Label(cell, text=meta, font=("Microsoft YaHei UI", 8),
                         foreground="#777", anchor="w")
        foot.pack(fill="x", padx=2)
        self.tiles[key] = {"cell": cell, "photo": None, "label": lbl,
                           "foot": foot, "w": 0, "h": 0, "title": title}
        # **拖拽排序**（B.3）：任何一处（标题/画面/说明行）按下都能拖，
        # 松手落在哪一格就排到那一格前面。
        for widget in (cell, head, holder, lbl, foot):
            widget.bind("<Button-1>", lambda e, k=key: self._drag_start(k))
            # **移动/松手必须跟"按下"绑在同一批控件上**（2026-10-13 修的真 bug）：
            # Tk 会给收到 Button-1 的那个控件一个**隐式 grab**，后续的 B1-Motion /
            # ButtonRelease 只发给它 —— 只绑在 cell 上的话，从标题/画面/说明行按下
            # 时 `_drag_motion`/`_drag_drop` 根本不会被调用（那三块几乎盖满整个格子，
            # 只剩 1~2px 的边框能拖，等于这个功能没法用）。
            widget.bind("<B1-Motion>", self._drag_motion)
            widget.bind("<ButtonRelease-1>", self._drag_drop)
        cell.configure(cursor="hand2")
        self.empty_label.pack_forget()
        self.relayout()
        self._on_inner_configure()

    # ---- 拖拽排序（B.3）----

    def _drag_start(self, key) -> None:
        self._drag_key = key
        self._drag_hover = None

    def _tile_at(self, x_root: int, y_root: int):
        """鼠标现在压在哪一格上（返回它的 key；不在任何格子上就 None）。"""
        for k, tile in self.tiles.items():
            cell = tile["cell"]
            try:
                if not cell.winfo_ismapped():
                    continue
                if (cell.winfo_rootx() <= x_root <= cell.winfo_rootx() + cell.winfo_width()
                        and cell.winfo_rooty() <= y_root
                        <= cell.winfo_rooty() + cell.winfo_height()):
                    return k
            except Exception:
                continue
        return None

    def _drag_motion(self, event) -> None:
        if getattr(self, "_drag_key", None) is None:
            return
        target = self._tile_at(event.x_root, event.y_root)
        if target != getattr(self, "_drag_hover", None):
            # 把上一次的高亮清掉，给新的目标描个蓝边（拖拽反馈）
            old = getattr(self, "_drag_hover", None)
            if old is not None and old in self.tiles:
                self.tiles[old]["cell"].configure(relief="solid", borderwidth=1)
            self._drag_hover = target
            if target is not None and target in self.tiles:
                self.tiles[target]["cell"].configure(relief="solid", borderwidth=2)

    def _drag_drop(self, event) -> None:
        src = getattr(self, "_drag_key", None)
        target = self._tile_at(event.x_root, event.y_root)
        self._drag_key = None
        for k in list(self.tiles):
            self.tiles[k]["cell"].configure(relief="solid", borderwidth=1)
        self._drag_hover = None
        if src is None or target is None or src == target:
            return
        if src not in self.tiles or target not in self.tiles:
            return
        keys = list(self.tiles)
        keys.remove(src)
        keys.insert(keys.index(target), src)
        self.tiles = {k: self.tiles[k] for k in keys}      # 重排顺序 = 重排字典
        self.relayout()
        self._on_inner_configure()
        # 告诉主程序：顺序变了，请记住（B.3 的"布局保存"）
        try:
            self.app.on_gallery_reordered(list(self.tiles))
        except Exception:
            pass

    def remove(self, key) -> None:
        tile = self.tiles.pop(key, None)
        if tile:
            tile["cell"].destroy()
        if not self.tiles:
            self.empty_label.pack(padx=16, pady=16, anchor="w")
        self.relayout()
        self._on_inner_configure()

    def clear(self) -> None:
        for key in list(self.tiles):
            self.remove(key)

    def relayout(self) -> None:
        cols = max(1, self._cols or 1)
        for idx, tile in enumerate(self.tiles.values()):
            r, c = divmod(idx, cols)
            tile["cell"].grid(row=r, column=c, padx=6, pady=6, sticky="nw")

    def set_frame(self, key, data: str, ow: int, oh: int) -> None:
        tile = self.tiles.get(key)
        if not tile:
            return
        if tile["photo"] is None or tile["w"] != ow or tile["h"] != oh:
            tile["photo"] = tk.PhotoImage(width=ow, height=oh)
            tile["w"], tile["h"] = ow, oh
            tile["label"].configure(image=tile["photo"], text="")
            tile["label"].image = tile["photo"]
        try:
            tile["photo"].put(data)
        except tk.TclError:
            pass

    def set_error(self, key, text: str) -> None:
        tile = self.tiles.get(key)
        if tile and tile["photo"] is None:
            tile["label"].configure(text=text[:40])


# ============================================================================
# 四、进程树面板
# ============================================================================

class ProcessTreePane:
    """
    进程树视图：按「父进程 → 子进程」层级展示。

    为什么需要：原先「结束进程树」是盲操作，用户看不到会波及哪些进程。
    有了这棵树，选中节点即可看到整棵子树，执行前还会把影响清单列清楚。
    """

    def __init__(self, parent: tk.Widget, app: "WinMonApp") -> None:
        self.app = app
        self.frame = ttk.Frame(parent)
        self.frame.rowconfigure(1, weight=1)
        self.frame.columnconfigure(0, weight=1)

        top = ttk.Frame(self.frame)
        top.grid(row=0, column=0, sticky="ew", padx=6, pady=(6, 2))
        self.info = ttk.Label(top, text="", font=UI_FONT, foreground="#555")
        self.info.pack(side="left")
        ttk.Button(top, text="刷新", width=7,
                   command=lambda: self.app.refresh_process_tree(force=True)).pack(side="right", padx=2)
        ttk.Button(top, text="结束选中进程及子树", width=18,
                   command=self.app.action_kill_tree_selection).pack(side="right", padx=2)

        cols = ("pid", "win", "mem", "mempriv", "cpu")
        tv = ttk.Treeview(self.frame, columns=cols, show="tree headings", selectmode="extended")
        self.tree = tv
        tv.heading("#0", text="进程（父 → 子）")
        tv.heading("pid", text="PID")
        tv.heading("win", text="窗口")
        tv.heading("mem", text="内存(工作集)")
        tv.heading("mempriv", text="私有(任务管理器)")
        tv.heading("cpu", text="CPU")
        tv.column("#0", width=300, anchor="w")
        tv.column("pid", width=66, anchor="e", stretch=False)
        tv.column("win", width=150, anchor="w")
        tv.column("mem", width=92, anchor="e", stretch=False)
        tv.column("mempriv", width=104, anchor="e", stretch=False)
        tv.column("cpu", width=58, anchor="e", stretch=False)
        vs = ttk.Scrollbar(self.frame, orient="vertical", command=tv.yview)
        tv.configure(yscrollcommand=vs.set)
        tv.grid(row=1, column=0, sticky="nsew", padx=(6, 0), pady=(0, 6))
        vs.grid(row=1, column=1, sticky="ns", pady=(0, 6))
        tv.tag_configure("haswin", foreground="#1b5e20")
        tv.tag_configure("self", background="#d6ecff")
        tv.tag_configure("critical", foreground="#8e44ad")
        tv.bind("<<TreeviewSelect>>", self._on_select)

    def _on_select(self, _event=None) -> None:
        sel = self.tree.selection()
        if not sel:
            return
        pid = self.app._tree_pid_of.get(sel[0])
        if not pid:
            return
        subtree = core.collect_descendants(pid, core.build_process_tree(self.app.proc_map))
        mem = sum(self.app.proc_map.get(p, {}).get("mem_kb", 0) for p in [pid] + subtree)
        name = self.app.proc_map.get(pid, {}).get("name", "?")
        self.app._set_status(
            f"已选中 {name} [{pid}]：连同子树共 {len(subtree) + 1} 个进程，"
            f"合计内存 {core._fmt_mem(mem)}；点「结束选中进程及子树」可一并结束。")

    def set_info(self, text: str) -> None:
        self.info.configure(text=text)


class WorkspacePane:
    """
    「工作区」页：把当前一组窗口的位置与最小化状态按名字存下来，之后一键切回去。

    用途是"写代码 / 看视频 / 打游戏"各存一套布局。设计取舍（都在 `winmon_core` 里）：

      · 匹配靠**可执行文件名 + 标题**，不存 hwnd（句柄下次运行必然不同）；
      · 恢复时**只处理能对上的窗口**，对不上的列出来告诉你 —— 不替你启动程序；
      · 矩形会**夹回屏幕内**：快照可能是插着外接显示器时存的，
        拔掉之后不夹就会把窗口挪到看不见的地方，用户会以为"东西丢了"。
    """

    def __init__(self, parent: tk.Widget, app: "WinMonApp") -> None:
        self.app = app
        self.frame = ttk.Frame(parent)
        self.frame.rowconfigure(3, weight=1)
        self.frame.columnconfigure(0, weight=1)

        head = ttk.Frame(self.frame)
        head.grid(row=0, column=0, sticky="ew", padx=8, pady=(8, 2))
        tk.Label(head, text="工作区快照", font=BOLD_FONT, anchor="w").pack(side="left")
        tk.Label(head, text="　把当前窗口的位置与最小化状态存下来，之后一键切回去",
                 font=UI_FONT, foreground="#777").pack(side="left")

        bar = ttk.Frame(self.frame)
        bar.grid(row=1, column=0, sticky="ew", padx=8, pady=(2, 4))
        ttk.Button(bar, text="保存当前布局…", width=13,
                   command=self.action_save).pack(side="left")
        ttk.Button(bar, text="应用", width=6,
                   command=self.action_apply).pack(side="left", padx=(6, 2))
        ttk.Button(bar, text="用当前布局更新", width=13,
                   command=self.action_update).pack(side="left", padx=2)
        ttk.Button(bar, text="删除", width=6,
                   command=self.action_delete).pack(side="left", padx=2)
        ttk.Button(bar, text="刷新列表", width=8,
                   command=self.refresh).pack(side="left", padx=(12, 0))

        #: 「顺带最小化其它窗口」**单独一行**：和那排按钮挤在一行时，
        #: 在 1280 宽的窗口下这个勾选框会被压窄（`test_a5` 的布局安全检查抓到的）。
        opts = ttk.Frame(self.frame)
        opts.grid(row=2, column=0, sticky="ew", padx=8, pady=(0, 4))
        ttk.Checkbutton(opts, text="应用时顺带最小化其它窗口（保护进程与 WinMon 自己不动）",
                        variable=app.ws_minimize_others).pack(side="left")

        cols = ("count", "saved")
        tv = ttk.Treeview(self.frame, columns=cols, show="tree headings",
                          selectmode="browse")
        self.tree = tv
        tv.heading("#0", text="工作区")
        tv.heading("count", text="窗口数")
        tv.heading("saved", text="保存时间")
        tv.column("#0", width=320, anchor="w")
        tv.column("count", width=70, anchor="e", stretch=False)
        tv.column("saved", width=170, anchor="w")
        tv.tag_configure("group", background="#eef2f6")
        vs = ttk.Scrollbar(self.frame, orient="vertical", command=tv.yview)
        tv.configure(yscrollcommand=vs.set)
        tv.grid(row=3, column=0, sticky="nsew", padx=(8, 0), pady=(0, 4))
        vs.grid(row=3, column=1, sticky="ns", pady=(0, 4))
        tv.bind("<<TreeviewSelect>>", lambda _e: self._show_detail())
        tv.bind("<Double-1>", lambda _e: self.action_apply())

        self.info = ttk.Label(self.frame, text="", font=UI_FONT, foreground="#555",
                              anchor="w", justify="left", wraplength=900)
        self.info.grid(row=4, column=0, columnspan=2, sticky="ew", padx=8, pady=(0, 8))
        self._detail_ids: list[str] = []

        empty = ("还没有保存过工作区。\n\n"
                 "把窗口摆成你想要的样子（程序可以只开一部分），点「保存当前布局…」起个名字。\n"
                 "以后无论窗口乱成什么样，选中它点「应用」就能回到那个样子。\n"
                 "注意：恢复只对**当前开着的**窗口生效，不会替你启动没开的程序。")
        self._set_info(empty)

    # ---- 小工具 ----

    def _set_info(self, text: str) -> None:
        self.info.configure(text=text)

    def _selected(self) -> dict | None:
        sel = self.tree.selection()
        if not sel:
            return None
        name = self.tree.item(sel[0], "text")
        for ws in self.app.workspaces:
            if ws.get("name") == name:
                return ws
        return None

    # ---- 列表 ----

    def refresh(self, keep: str = "") -> None:
        """重建工作区列表（保持选中项）。"""
        tv = self.tree
        if not keep:
            sel = tv.selection()
            keep = tv.item(sel[0], "text") if sel else ""
        for item in tv.get_children(""):
            tv.delete(item)
        for ws in self.app.workspaces:
            saved = ws.get("saved_at") or 0.0
            when = time.strftime("%Y-%m-%d %H:%M", time.localtime(saved)) if saved else "—"
            tv.insert("", "end", text=ws.get("name", ""),
                      values=(len(ws.get("entries") or []), when))
        if keep:
            for item in tv.get_children(""):
                if tv.item(item, "text") == keep:
                    tv.selection_set(item)
                    break
        self._show_detail()

    def _show_detail(self) -> None:
        """选中某个工作区时，在下面列出它包含哪些窗口。"""
        ws = self._selected()
        if ws is None:
            if self.app.workspaces:
                self._set_info("选中一个工作区查看内容，或点「应用」切过去。")
            return
        entries = ws.get("entries") or []
        parts = [f"{e.get('name') or '（未知）'}"
                 f"{'（最小化）' if e.get('iconic') else ''}"
                 f"  {e.get('title') or ''}".rstrip() for e in entries[:12]]
        more = f"\n…另有 {len(entries) - 12} 个窗口" if len(entries) > 12 else ""
        self._set_info(f"「{ws.get('name')}」共 {len(entries)} 个窗口：\n  "
                       + "\n  ".join(parts) + more)

    # ---- 动作 ----

    def action_save(self) -> None:
        """把当前布局存成一个新工作区。"""
        items = self.app.capture_current_layout()
        if not items:
            self._set_info("现在没有可保存的窗口（需要至少一个有可见窗口的程序）。")
            return
        name = _TextInputDialog(
            self.app.root, "保存工作区",
            f"当前有 {len(items)} 个窗口。给这套布局起个名字：",
            initial=f"工作区 {len(self.app.workspaces) + 1}").show()
        if not name:
            return
        name = name.strip()
        if not name:
            return
        ws = core.capture_workspace(name, items, now=time.time())
        self.app.workspaces = [w for w in self.app.workspaces if w.get("name") != name]
        self.app.workspaces.append(ws)
        self.app.workspaces = core.normalize_workspaces(self.app.workspaces)
        self.app._schedule_save_settings()
        self.refresh(keep=name)
        self._set_info(f"已保存「{name}」：{len(ws['entries'])} 个窗口。"
                       f"以后选中它点「应用」就能回到这个布局。")

    def action_update(self) -> None:
        """用当前布局覆盖选中的工作区。"""
        ws = self._selected()
        if ws is None:
            self._set_info("请先在列表里选中一个工作区。")
            return
        items = self.app.capture_current_layout()
        if not items:
            self._set_info("现在没有可保存的窗口。")
            return
        name = ws.get("name", "")
        fresh = core.capture_workspace(name, items, now=time.time())
        self.app.workspaces = [fresh if w.get("name") == name else w
                               for w in self.app.workspaces]
        self.app._schedule_save_settings()
        self.refresh(keep=name)
        self._set_info(f"已用当前布局更新「{name}」：{len(fresh['entries'])} 个窗口。")

    def action_delete(self) -> None:
        """删掉选中的工作区（删的是快照，不影响任何窗口）。"""
        ws = self._selected()
        if ws is None:
            self._set_info("请先在列表里选中一个工作区。")
            return
        name = ws.get("name", "")
        if not messagebox.askyesno(APP_NAME, f"删除工作区「{name}」？\n"
                                             f"（只删这份快照，不会动任何窗口）",
                                   parent=self.app.root):
            return
        self.app.workspaces = [w for w in self.app.workspaces if w.get("name") != name]
        self.app._schedule_save_settings()
        self.refresh()
        self._set_info(f"已删除「{name}」。")

    def action_apply(self) -> None:
        """把选中的工作区应用到当前窗口上。"""
        ws = self._selected()
        if ws is None:
            self._set_info("请先在列表里选中一个工作区，再点「应用」。")
            return
        self.app.apply_workspace(ws)


class ProgramCatalogPane:
    """
    程序目录面板：列出系统里已安装 / 可启动的全部程序，并能精准打开。

    为什么需要：原先只能"管理已经在运行的窗口"，无法启动还没运行的程序。
    这里把开始菜单快捷方式 + 注册表 + 正在运行的进程合成一份目录，
    支持搜索、打开、以管理员身份打开、定位文件、导出 CSV。
    """

    def __init__(self, parent: tk.Widget, app: "WinMonApp") -> None:
        self.app = app
        self.items: list[core.ProgramItem] = []
        self.filtered: list[core.ProgramItem] = []
        self.row_of_item: dict[str, core.ProgramItem] = {}
        self.category_row: dict[str, str] = {}          # 分组表头 iid -> 类别 key
        self._icon_cache: dict[str, tk.PhotoImage | None] = {}
        self._search_after = None
        #: 批量启动后"过 1.5 秒重排一次"的 after id（销毁时要一起取消）
        self._apply_after = None
        #: 界面已销毁（后台线程里的收尾回调据此收手，别再往已销毁的 root 上排定时器）
        self._destroyed = False
        self.frame = ttk.Frame(parent)
        self.frame.rowconfigure(3, weight=1)
        self.frame.columnconfigure(0, weight=1)

        # 工具区分三行。右侧面板实际可用宽度约 676px（1280 窗口、4:6 分栏、
        # 再减去滚动条与内边距），下面每行的自然宽度都按实测控制在 600px 以内 ——
        # tests/test_a5_gui_layout_safety.py 的 `_squeezed()` 会守住这条。

        # 第一行：搜索 + 显示过滤 + 扫描/导出
        top = ttk.Frame(self.frame)
        top.grid(row=0, column=0, sticky="ew", padx=6, pady=(6, 2))
        ttk.Label(top, text="搜索程序：", font=UI_FONT).pack(side="left")
        self.search_var = tk.StringVar()
        ent = ttk.Entry(top, textvariable=self.search_var, width=20, font=UI_FONT)
        ent.pack(side="left", padx=(0, 8))
        ent.bind("<KeyRelease>", self._on_search_key)
        self.only_runnable = tk.BooleanVar(value=True)
        ttk.Checkbutton(top, text="仅可启动", variable=self.only_runnable,
                        command=self.apply_filter).pack(side="left", padx=2)
        # 「重新扫描」「导出 CSV…」靠右。注意 Tk 的 pack：先 pack 的 side="right" 最靠右。
        ttk.Button(top, text="重新扫描", width=9, command=self.rescan).pack(
            side="right", padx=2)
        ttk.Button(top, text="导出 CSV…", width=10, command=self.export_csv).pack(
            side="right", padx=2)

        # 第二行：类别多选筛选 + 两个开关
        cats = ttk.Frame(self.frame)
        cats.grid(row=1, column=0, sticky="ew", padx=6, pady=(0, 2))
        # 用按钮而不是 Combobox：Combobox 只能单选，而"同时看游戏和影音"很常见
        self.btn_cats = ttk.Button(cats, text="类别：全部", width=18,
                                   command=self.choose_categories)
        self.btn_cats.pack(side="left", padx=(0, 6))
        self.only_favorites = tk.BooleanVar(value=False)
        ttk.Checkbutton(cats, text="只看收藏", variable=self.only_favorites,
                        command=self.apply_filter).pack(side="left", padx=2)
        self.only_recent = tk.BooleanVar(value=False)
        ttk.Checkbutton(cats, text="只看最近使用", variable=self.only_recent,
                        command=self.apply_filter).pack(side="left", padx=2)
        ttk.Button(cats, text="最近使用…", width=11,
                   command=self.choose_recent).pack(side="left", padx=(6, 0))
        self.filter_info = ttk.Label(cats, text="", font=UI_FONT, foreground="#555")
        self.filter_info.pack(side="right")
        #: 多选出来的类别集合（空集 = 全部类别）
        self.selected_categories: set[str] = set()

        # 第三行：收藏 / 类别管理 + 启动动作
        # 这一行的自然宽度只有约 560px 可用，所以按钮文字取"够懂就行"的最短形式：
        # 「打开」「定位」「自动分类」—— 更长的写法会把最后一个按钮挤窄（实测）。
        acts = ttk.Frame(self.frame)
        acts.grid(row=2, column=0, sticky="ew", padx=6, pady=(0, 2))
        self.btn_fav = ttk.Button(acts, text="☆ 收藏", width=7,
                                  command=self.toggle_favorite_selected)
        self.btn_fav.pack(side="left", padx=1)
        ttk.Button(acts, text="设为类别…", width=10,
                   command=self.set_category_selected).pack(side="left", padx=1)
        ttk.Button(acts, text="自动分类", width=9,
                   command=lambda: self.set_category_selected(clear=True)).pack(
            side="left", padx=1)
        ttk.Separator(acts, orient="vertical").pack(side="left", fill="y", padx=6)
        ttk.Button(acts, text="打开", width=7,
                   command=self.launch_selected).pack(side="left", padx=1)
        ttk.Button(acts, text="以管理员打开", width=12,
                   command=lambda: self.launch_selected(as_admin=True)).pack(side="left", padx=1)
        ttk.Button(acts, text="定位", width=7,
                   command=self.reveal_selected).pack(side="left", padx=1)
        ttk.Button(acts, text="复制路径", width=9,
                   command=self.copy_path).pack(side="left", padx=1)
        # 说明：这一行刻意不再放"目录 N 项"这类计数标签 ——
        # 加上它之后这一行的自然宽度会超过右侧面板的可用宽度（约 560px），
        # Tk 会把最后一个按钮压窄（实测「复制路径」从 73px 被压到 59px）。
        # 计数改放在底部状态栏（app._set_status），扫描进度也走那里。

        # 列表
        cols = ("name", "category", "source", "state", "dir")
        tv = ttk.Treeview(self.frame, columns=cols, show="tree headings",
                          selectmode="extended")
        self.tree = tv
        tv.heading("#0", text="图标")
        tv.heading("name", text="程序名称")
        tv.heading("category", text="类别")
        tv.heading("source", text="来源")
        tv.heading("state", text="状态")
        tv.heading("dir", text="路径 / 目标")
        tv.column("#0", width=40, stretch=False, minwidth=40)
        tv.column("name", width=200, anchor="w")
        tv.column("category", width=84, anchor="center", stretch=False)
        tv.column("source", width=62, anchor="center", stretch=False)
        tv.column("state", width=58, anchor="center", stretch=False)
        tv.column("dir", width=330, anchor="w")
        vs = ttk.Scrollbar(self.frame, orient="vertical", command=tv.yview)
        tv.configure(yscrollcommand=vs.set)
        tv.grid(row=3, column=0, sticky="nsew", padx=(6, 0), pady=(0, 6))
        vs.grid(row=3, column=1, sticky="ns", pady=(0, 6))
        tv.tag_configure("running", background="#d6ecff")
        tv.tag_configure("missing", foreground="#c0392b")
        tv.tag_configure("group", background="#eef2f6")
        tv.tag_configure("fav", foreground="#c47f00")
        tv.bind("<Double-1>", lambda e: self.launch_selected())
        tv.bind("<<TreeviewSelect>>", lambda e: self._sync_fav_button())
        # 右键菜单（B.4）：以前程序目录没有右键菜单，只能靠底部那几个按钮；
        # 这里补上"用参数启动 / 以管理员打开"，参数启动是本次新增的能力入口。
        tv.bind("<Button-3>", self._on_right_click)
        # 只给两个开关挂 trace（类别走多选面板，点「应用」时自己刷新并保存）
        self.only_favorites.trace_add("write", lambda *_: self._on_filter_changed())
        self.only_recent.trace_add("write", lambda *_: self._on_filter_changed())

    def _on_filter_changed(self) -> None:
        self.apply_filter()

    def _on_right_click(self, event) -> None:
        """程序目录的右键菜单（B.4）。先选中鼠标下那一行，再弹菜单。"""
        row = self.tree.identify_row(event.y)
        if row:
            if row not in self.tree.selection():
                self.tree.selection_set(row)
            self.tree.focus(row)
        items = self._selected_items()
        if not items:
            return
        item = items[0]
        menu = tk.Menu(self.app.root, tearoff=0)
        menu.add_command(label=f"打开　{item.name}",
                         command=lambda: self.launch_selected())
        menu.add_command(label="用参数启动…",
                         command=self.launch_with_args_dialog)
        menu.add_command(label="以管理员身份打开",
                         command=lambda: self.launch_selected(as_admin=True))
        menu.add_separator()
        menu.add_command(label="在资源管理器中定位", command=self.reveal_selected)
        menu.add_command(label="复制路径",
                         command=lambda: self.app._copy(item.target))
        menu.add_separator()
        fav = "★ 取消收藏" if self.app.program_prefs.is_favorite(item.target) else "☆ 收藏"
        menu.add_command(label=fav, command=self.toggle_favorite_selected)
        menu.add_command(label="设为类别…", command=self.set_category_selected)
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()
        try:
            self.app.save_settings_now()
        except Exception:
            pass

    # -- 数据 --
    def load_async(self, on_done=None) -> None:
        """
        在后台线程里构建目录，避免界面卡住。

        进度与结果都经 `app._ui_call()` 交回主线程（**工作线程一律不碰 tkinter**）——
        以前这里直接 `self.app.root.after(0, …)`，主线程不在 mainloop 里派发时
        会抛 `RuntimeError: main thread is not in main loop`，而这个异常发生在
        工作线程里，只会把它静默弄死：界面永远停在"正在扫描程序目录…"（2026-10-13 修）。
        """
        import threading

        def worker():
            try:
                items = core.build_program_catalog(
                    progress=lambda m: self.app._ui_call(
                        lambda msg=m: self._progress(msg)))
            except Exception as exc:
                self.app._ui_call(lambda msg=str(exc): self._progress(
                    f"目录构建失败：{msg}"))
                return
            self.app._ui_call(lambda: self._on_loaded(items, on_done))

        threading.Thread(target=worker, daemon=True, name="WinMon-Catalog").start()

    def _progress(self, text: str) -> None:
        """目录构建进度：显示在面板自身的状态标签 + 主状态栏上。"""
        self.filter_info.configure(text=text)
        try:
            self.app._set_status(text)
        except Exception:
            pass

    def _on_loaded(self, items, on_done=None) -> None:
        self.items = items
        self.apply_filter()
        if on_done:
            on_done(items)

    # -- 收藏 / 分类（状态由 WinMonApp 持久化到 winmon_settings.json）--
    def prefs(self) -> core.CatalogPrefs:
        return self.app.program_prefs

    def _chosen_category(self) -> str:
        """兼容旧接口：单选时返回那一个类别 key，多选或全选时返回空串。"""
        return next(iter(self.selected_categories)) if len(self.selected_categories) == 1 else ""

    def _category_label(self) -> str:
        """按钮上显示的类别摘要。"""
        n = len(self.selected_categories)
        if not self.selected_categories:
            return "类别：全部"
        if n == 1:
            return "类别：" + core.CATEGORY_LABELS[sorted(self.selected_categories)[0]]
        # 多选时列出前两个 + 计数，避免按钮过长被挤窄
        names = [core.CATEGORY_LABELS[k] for k in
                 sorted(self.selected_categories, key=lambda x: core.CATEGORY_ORDER.index(x))]
        head = "、".join(names[:2])
        return f"类别：{head} 等 {n} 类"

    def choose_categories(self) -> None:
        """弹出多选面板挑类别（可同时选多个）。"""
        options = [(k, core.CATEGORY_LABELS[k]) for k in core.CATEGORY_ORDER]
        dlg = _MultiSelectDialog(
            self.app.root, "选择要显示的类别",
            "勾选后点「应用」（可多选，不勾任何一项＝全部）：",
            options, self.selected_categories)
        picked = dlg.show()
        if picked is None:
            return
        self.selected_categories = picked
        self.btn_cats.configure(text=self._category_label())
        self.apply_filter()
        self.app._save_settings_soon()
        self.app._set_status(
            "类别筛选：" + ("全部" if not picked else
                            "、".join(core.CATEGORY_LABELS[k] for k in
                                      sorted(picked, key=core.CATEGORY_ORDER.index))))

    def choose_recent(self) -> None:
        """弹出「最近使用」列表，选中一项可直接打开。"""
        prefs = self.prefs()
        recents = prefs.recent_list()
        if not recents:
            self.app._set_status("还没有「最近使用」记录（成功打开过程序后才会记录）")
            return
        entries = []
        for r in recents:
            name = r.get("name") or os.path.basename(r.get("path", "")) or "?"
            entries.append((r.get("path", ""), f"{name}（{_relative_time(r.get('ts'))}）"))
        dlg = _RecentPickerDialog(
            self.app.root, f"最近使用（{len(entries)} 项）",
            "选择要打开的程序（选中后点「打开」按钮即启动）：", entries)
        picked = dlg.show()
        if picked is None:
            return
        idx = int(picked) - 1
        target = entries[idx][0]
        # 在目录里选中对应条目（找不到就提示）
        for iid, it in self.row_of_item.items():
            if (it.target or "").lower() == target.lower():
                self.tree.selection_set(iid)
                self.tree.see(iid)
                self.app._set_status(f"已选中最近使用的：{it.name}")
                return
        self.app._set_status(
            "该项不在当前列表里（可能被筛选条件隐藏了）；"
            f"可直接打开：{target}")

    def apply_filter(self) -> None:
        prefs = self.prefs()
        kw = self.search_var.get().strip().lower()
        only = self.only_runnable.get()
        want_cats = set(self.selected_categories)
        out = []
        for it in self.items:
            if only and not it.runnable:
                continue
            if self.only_favorites.get() and not prefs.is_favorite(it.target, it.name):
                continue
            if self.only_recent.get() and not prefs.is_recent(it.target, it.name):
                continue
            if want_cats and core.category_of_item(
                    it, prefs.category_override(it.target, it.name)) not in want_cats:
                continue
            if kw:
                blob = f"{it.name} {it.target}".lower()
                if kw not in blob:
                    continue
            out.append(it)
        # 排序：收藏 → 最近使用 → 正在运行 → 名称。
        # 这样"我常用的"永远在最上面，不用每次都翻列表。
        recent_order = {prefs.key(r.get("path", ""), r.get("name", "")): i
                        for i, r in enumerate(prefs.recent_list())}

        def sort_key(x):
            fav = 0 if prefs.is_favorite(x.target, x.name) else 1
            rec = recent_order.get(prefs.key(x.target, x.name), 10 ** 6)
            running = 0 if (x.pid and core.is_process_alive(x.pid)) else 1
            return (fav, rec, running, x.name.lower())

        out.sort(key=sort_key)
        self._render(out, group=not (want_cats or self.only_favorites.get()
                                     or self.only_recent.get()))

    def _render(self, items: list, group: bool = True) -> None:
        """
        渲染列表。

        group 为真时按类别分组（类别做可折叠的表头）；为假时平铺 ——
        用户已经选定了某个类别或筛选条件，再分组只会多一层要点开的层级。
        """
        tv = self.tree
        tv.delete(*tv.get_children())
        self.row_of_item.clear()
        self.category_row.clear()
        icons = self.app.icons
        prefs = self.prefs()

        def insert_row(parent_iid, it):
            running = bool(it.pid and core.is_process_alive(it.pid))
            tags = []
            if prefs.is_favorite(it.target, it.name):
                tags.append("fav")
            if running:
                tags.append("running")
            elif not it.runnable:
                tags.append("missing")
            label = it.name + ("  ★" if "fav" in tags else "")
            iid = tv.insert(parent_iid, "end", text="", tags=tuple(tags),
                            values=(label,
                                    core.CATEGORY_LABELS.get(
                                        core.category_of_item(
                                            it, prefs.category_override(it.target, it.name)),
                                        "其他"),
                                    it.source_label,
                                    "运行中" if running else ("可启动" if it.runnable else "不可用"),
                                    it.target))
            self.row_of_item[iid] = it
            try:
                img = self._icon_for(it, icons)
                if img:
                    tv.item(iid, image=img)
            except Exception:
                pass
            return iid

        if group:
            for key, group_items in core.group_items_by_category(
                    items, prefs.overrides()):
                fav_n = sum(1 for x in group_items if prefs.is_favorite(x.target, x.name))
                title = f"{core.CATEGORY_LABELS[key]}（{len(group_items)}）"
                if fav_n:
                    title += f" · 收藏 {fav_n}"
                parent = tv.insert("", "end", text="", open=True, tags=("group",),
                                   values=("", title, "", "", ""))
                self.category_row[parent] = key
                for it in group_items:
                    insert_row(parent, it)
        else:
            for it in items:
                insert_row("", it)

        self.filtered = items
        fav_total = prefs.favorite_count()
        rec_total = prefs.recent_count()
        self.filter_info.configure(text=f"收藏 {fav_total} · 最近 {rec_total}")
        self._sync_fav_button()
        # 当前显示数量放状态栏：面板里那几行工具区宽度紧张，放不下计数标签
        try:
            self.app._set_status(f"程序目录 {len(self.items)} 项，当前显示 {len(items)} 项"
                                 f"　收藏 {fav_total}　最近 {rec_total}")
        except Exception:
            pass

    def _sync_fav_button(self) -> None:
        """收藏按钮的文字跟着选中项的状态变（☆/★）。"""
        items = self._selected_items()
        try:
            if items and self.prefs().is_favorite(items[0].target, items[0].name):
                self.btn_fav.configure(text="★ 已收藏")
            else:
                self.btn_fav.configure(text="☆ 收藏")
        except Exception:
            pass

    def toggle_favorite_selected(self) -> None:
        items = self._selected_items()
        if not items:
            self.app._set_status("请先选择一个程序，再点收藏")
            return
        prefs = self.prefs()
        it = items[0]
        now_fav = prefs.toggle_favorite(it.target, it.name)
        self.app._save_settings_soon()
        self.apply_filter()
        self.app._set_status(f"{it.name}：{'已加入收藏' if now_fav else '已取消收藏'}")

    def set_category_selected(self, clear: bool = False) -> None:
        """把选中项手动归到某个类别（或清除手动设置，回到自动分类）。"""
        items = self._selected_items()
        if not items:
            self.app._set_status("请先选择一个程序，再设置类别")
            return
        prefs = self.prefs()
        it = items[0]
        if clear:
            prefs.set_category(it.target, "", it.name)
            self.app._save_settings_soon()
            self.apply_filter()
            self.app._set_status(
                f"{it.name}：已恢复自动分类（"
                f"{core.CATEGORY_LABELS[core.category_of_item(it)]}）")
            return
        labels = [core.CATEGORY_LABELS[k] for k in core.CATEGORY_ORDER]
        box = _ChoiceDialog(self.app.root, f"设置类别 — {it.name}",
                            "把这个程序归到哪一类？", labels,
                            current=core.CATEGORY_LABELS[
                                core.category_of_item(it, prefs.category_override(
                                    it.target, it.name))])
        chosen = box.show()
        if chosen is None:
            return
        key = next((k for k, text in core.CATEGORY_LABELS.items() if text == chosen), "")
        prefs.set_category(it.target, key, it.name)
        self.app._save_settings_soon()
        self.apply_filter()
        self.app._set_status(f"{it.name}：类别已设为「{chosen}」")

    def _icon_for(self, it, icons: "IconProvider"):
        """程序目录的图标：按 exe 路径缓存，避免每次重建都重新提取。"""
        key = (it.target or "").lower()
        if not key:
            return None
        if key in self._icon_cache:
            return self._icon_cache[key]
        png = core.icon_from_file(it.target, ICON_SIZE) if it.target else ""
        img = None
        if png:
            try:
                img = icons._photos.setdefault(png[:80], tk.PhotoImage(data=png))
            except tk.TclError:
                img = None
        self._icon_cache[key] = img
        return img

    def _on_search_key(self, _event=None) -> None:
        # 输入时合并刷新，避免每敲一个字符都重建列表
        if getattr(self, "_search_after", None):
            try:
                self.app.root.after_cancel(self._search_after)
            except Exception:
                pass
        self._search_after = self.app.root.after(220, self.apply_filter)

    def destroy(self) -> None:
        """
        取消挂起的定时回调。

        为什么需要：搜索框用的是 `after(220, apply_filter)` 防抖；
        批量启动的收尾还会另排一个 `after(1500, apply_filter)`。
        窗口关闭（或测试里销毁 Tk 根窗口）之后这些回调如果还在队列里，
        Tk 会报 `invalid command name "...apply_filter"` —— 本身无害，
        但会让测试输出很吵，也掩盖真正的错误。
        """
        self._destroyed = True
        for attr in ("_search_after", "_apply_after"):
            after_id = getattr(self, attr, None)
            if after_id is None:
                continue
            try:
                self.app.root.after_cancel(after_id)
            except Exception:
                pass
            setattr(self, attr, None)
        try:
            self.frame.destroy()
        except Exception:
            pass

    # -- 动作 --
    def _selected_items(self) -> list:
        tv = self.tree
        return [self.row_of_item[i] for i in tv.selection() if i in self.row_of_item]

    def launch_selected(self, as_admin: bool = False, extra_args: str = "") -> None:
        """
        启动选中的程序。

        `launch_item` 内含 `_wait_running` 轮询（默认 2s + explorer 兜底 4s），
        多选时叠加 —— 以前全在主线程，点一次"打开"界面能冻好几秒（P1-3 修复）。
        现在整体放后台线程，每个结果经 `_ui_call` 回主线程。
        """
        items = self._selected_items()
        if not items:
            self.app._set_status("请先在程序目录里选择一个程序")
            return
        self.app._set_status(f"正在启动选中的 {len(items)} 个程序…")
        import threading

        def worker():
            for it in items:
                if it.pid and core.is_process_alive(it.pid) and not extra_args:
                    # 带参数时不走"已在运行就切前台"——明确要再开一个
                    hwnd = self.app.hwnd_of_pid(it.pid)
                    if hwnd:
                        try:
                            core.user32.SetForegroundWindow(core.w.HWND(hwnd))
                            msg = "已在运行，已切到前台"
                        except Exception:
                            msg = "已在运行（切前台失败）"
                    else:
                        msg = "已在运行"
                    ok = True
                else:
                    ok, msg = core.launch_item(it, as_admin=as_admin,
                                               extra_args=extra_args)
                how = "打开程序(管理员)" if as_admin else "打开程序"
                if extra_args:
                    how += "(带参数)"
                target_txt = (f"{it.name} -> {it.target}"
                              + (f"　参数：{extra_args}" if extra_args else ""))

                # 用默认参数绑定住循环变量，避免回调晚绑定读错
                def done(it=it, how=how, target_txt=target_txt, msg=msg, used=ok):
                    self.app.log_action(how, target_txt,
                                        "成功" if used else f"失败：{msg}")
                    self.app._set_status(f"{it.name}：{msg}")
                    if used:
                        self.app.program_prefs.record_use(it.target, it.name)

                self.app._ui_call(done)

            self.app._ui_call(self._after_launch_all)

        threading.Thread(target=worker, daemon=True, name="WinMon-Launch").start()

    def _after_launch_all(self) -> None:
        """
        批量启动的收尾（**在主线程跑**，由 `launch_selected` 的 worker 经
        `_ui_call` 送回来）。

        为什么不写成 worker 里的闭包（2026-10-16 复查补丁的落地调整）：
        那个闭包里有一句 `self.app.root.after(1500, self.apply_filter)`，
        虽然经 `_ui_call` 送回主线程后才执行（线程安全），
        但 `test_the_three_workers_no_longer_call_root_after` 是**按源码**钉住
        "worker 里不许出现 `root.after`"的 —— 留着就等于把那条守门员改成
        "允许例外"，而那正是它存在的意义。挪出来两边都干净。
        """
        self.app._save_settings_soon()
        if getattr(self, "_destroyed", False):
            return
        try:
            self._apply_after = self.app.root.after(1500, self.apply_filter)
        except Exception:
            self._apply_after = None

    def launch_with_args_dialog(self) -> None:
        """
        「用参数启动…」：问用户要参数，然后用**那个参数**启动（B.4）。

        为什么值得做：`launch_item()` 早就支持 `extra_args` 了，但界面上没入口，
        等于这个能力用户拿不到。对话框里给几个常见例子做提示，但**不做限制**
        （参数完全交给程序自己解释）。
        """
        items = self._selected_items()
        if not items:
            self.app._set_status("请先在程序目录里选择一个程序")
            return
        item = items[0]
        args = _TextInputDialog(
            self.app.root, "用参数启动",
            f"给「{item.name}」加上启动参数：",
            hint="例如：--incognito 　-n 　/high 　\"X:\\某个文件夹\"\n"
                 "（参数原样交给程序；留空 = 和直接打开一样）",
            initial="").show()
        if args is None:
            return
        self.launch_selected(extra_args=str(args).strip())

    def reveal_selected(self) -> None:
        items = self._selected_items()
        if not items:
            return
        ok, msg = core.open_item_location(items[0])
        self.app._set_status(msg)

    def copy_path(self) -> None:
        items = self._selected_items()
        if not items:
            return
        self.app._copy(items[0].target)

    def export_csv(self) -> None:
        if not self.filtered:
            self.app._set_status("当前没有可导出的条目")
            return
        try:
            path = core.catalog_to_csv(self.filtered)
            self.app._set_status(f"已导出 {len(self.filtered)} 条到：{path}")
            core.open_file(path)
        except Exception as exc:
            self.app._set_status(f"导出失败：{exc}")

    def rescan(self) -> None:
        self._icon_cache.clear()
        try:
            os.remove(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                   "winmon_catalog.json"))
        except OSError:
            pass
        self.filter_info.configure(text="正在重新扫描…")
        self.load_async()


class _TextInputDialog:
    """
    让用户输入一行文字的模态对话框（用于「保存工作区」起名字）。

    用自己写的而不是 `tkinter.simpledialog.askstring`：那个弹窗不跟随本程序的
    深色主题，在一堆深色面板之间跳出一个系统灰框很突兀；而且这里需要
    "回车确定 / Esc 取消 / 默认选中原文（便于改名）"这几个小行为。
    """

    def __init__(self, parent: tk.Misc, title: str, message: str,
                 initial: str = "", hint: str = "") -> None:
        self.result: str | None = None
        self.top = tk.Toplevel(parent)
        top = self.top
        top.title(title)
        top.configure(bg="#1b2027")
        top.attributes("-topmost", True)
        top.resizable(False, False)

        wrap = ttk.Frame(top, padding=18)
        wrap.pack(fill="both", expand=True)
        tk.Label(wrap, text=message, font=("Microsoft YaHei UI", 11, "bold"),
                 bg="#1b2027", fg="#eaf2f8", anchor="w", justify="left").pack(anchor="w")
        # 可选的提示行（B.4「用参数启动」用它给出常见参数例子）
        if hint:
            tk.Label(wrap, text=hint, font=("Microsoft YaHei UI", 8), bg="#1b2027",
                     fg="#8fa8bd", anchor="w", justify="left").pack(
                anchor="w", pady=(4, 0))
        self.var = tk.StringVar(value=initial)
        self.entry = tk.Entry(wrap, textvariable=self.var, font=UI_FONT, width=38,
                              bg="#0f1418", fg="#eaf2f8", insertbackground="#eaf2f8",
                              relief="flat", highlightthickness=1,
                              highlightbackground="#3a4a5a")
        self.entry.pack(fill="x", pady=(10, 12))
        self.entry.select_range(0, "end")

        btns = ttk.Frame(wrap)
        btns.pack(fill="x")
        ttk.Button(btns, text="确定 (Enter)", width=13, command=self._ok).pack(side="right")
        ttk.Button(btns, text="取消 (Esc)", width=12, command=self._cancel).pack(
            side="right", padx=(0, 8))
        top.bind("<Return>", lambda _e: self._ok())
        top.bind("<KP_Enter>", lambda _e: self._ok())
        top.bind("<Escape>", lambda _e: self._cancel())

        top.update_idletasks()
        w, h = top.winfo_width(), top.winfo_height()
        sw, sh = top.winfo_screenwidth(), top.winfo_screenheight()
        top.geometry(f"+{max(0, (sw - w) // 2)}+{max(0, (sh - h) // 3)}")
        try:
            top.grab_set()
        except tk.TclError:
            pass
        self.entry.focus_force()
        self.entry.focus_set()

    def _ok(self) -> None:
        self.result = self.var.get()
        self.top.destroy()

    def _cancel(self) -> None:
        self.result = None
        self.top.destroy()

    def show(self) -> str | None:
        self.top.wait_window()
        return self.result


class _ChoiceDialog:
    """
    从一列选项里挑一个的模态对话框（用于「设为类别…」）。

    为什么不用 `tkinter.simpledialog.askstring`：那要用户自己输入类别名，
    容易写错；这里直接把类别列成单选列表，点一下即可。
    回车 = 确定，Esc = 取消。
    """

    def __init__(self, parent: tk.Misc, title: str, message: str,
                 choices: list[str], current: str = "") -> None:
        self.result: str | None = None
        self.top = tk.Toplevel(parent)
        top = self.top
        top.title(title)
        top.configure(bg="#1b2027")
        top.attributes("-topmost", True)
        top.resizable(False, False)

        wrap = ttk.Frame(top, padding=18)
        wrap.pack(fill="both", expand=True)
        tk.Label(wrap, text=message, font=("Microsoft YaHei UI", 11, "bold"),
                 bg="#1b2027", fg="#eaf2f8", anchor="w").pack(anchor="w")

        self.listbox = tk.Listbox(wrap, height=min(10, max(3, len(choices))),
                                 font=UI_FONT, activestyle="none",
                                 bg="#0f1418", fg="#cfe3f0",
                                 selectbackground="#2d6ea8", selectforeground="#ffffff",
                                 relief="flat", highlightthickness=0)
        for c in choices:
            self.listbox.insert("end", c)
        if current in choices:
            idx = choices.index(current)
            self.listbox.selection_set(idx)
            self.listbox.see(idx)
        else:
            self.listbox.selection_set(0)
        self.listbox.pack(fill="both", expand=True, pady=(10, 12))

        btns = ttk.Frame(wrap)
        btns.pack(fill="x")
        ttk.Button(btns, text="确定 (Enter)", width=14,
                   command=self._ok).pack(side="left")
        ttk.Button(btns, text="取消 (Esc)", width=14,
                   command=self._cancel).pack(side="left", padx=(10, 0))

        self.listbox.bind("<Double-1>", lambda e: self._ok())
        top.bind("<Return>", lambda e: self._ok())
        top.bind("<KP_Enter>", lambda e: self._ok())
        top.bind("<Escape>", lambda e: self._cancel())

        top.update_idletasks()
        w, h = top.winfo_width(), top.winfo_height()
        sw, sh = top.winfo_screenwidth(), top.winfo_screenheight()
        top.geometry(f"+{max(0, (sw - w) // 2)}+{max(0, (sh - h) // 3)}")
        try:
            top.grab_set()
        except tk.TclError:
            pass
        top.focus_force()
        self.listbox.focus_set()

    def _ok(self) -> None:
        sel = self.listbox.curselection()
        self.result = self.listbox.get(sel[0]) if sel else None
        self.top.destroy()

    def _cancel(self) -> None:
        self.result = None
        self.top.destroy()

    def show(self) -> str | None:
        self.top.wait_window()
        return self.result


class _MultiSelectDialog:
    """
    多选面板（用于「类别」筛选：可以同时看"游戏娱乐 + 影音播放"）。

    为什么不用下拉框：`ttk.Combobox` 只能单选，而"我想同时看游戏和影音"
    是很自然的需求。这里用一组复选框，点确定后回调生效。
    界面沿用弹窗那套深色卡片风格（tk.Label 才有背景色，见坑 14）。
    """

    def __init__(self, parent: tk.Misc, title: str, message: str,
                 options: list[tuple[str, str]], selected: set[str],
                 on_apply=None) -> None:
        self.result: set[str] | None = None
        self._on_apply = on_apply
        self.top = tk.Toplevel(parent)
        top = self.top
        top.title(title)
        top.configure(bg="#1b2027")
        top.attributes("-topmost", True)
        top.resizable(False, False)

        wrap = ttk.Frame(top, padding=18)
        wrap.pack(fill="both", expand=True)
        tk.Label(wrap, text=message, font=("Microsoft YaHei UI", 11, "bold"),
                 bg="#1b2027", fg="#eaf2f8", anchor="w").pack(anchor="w")

        body = tk.Frame(wrap, bg="#1b2027")
        body.pack(fill="both", expand=True, pady=(10, 12))
        self.vars: dict[str, tk.BooleanVar] = {}
        for key, label in options:
            var = tk.BooleanVar(value=key in selected)
            self.vars[key] = var
            tk.Checkbutton(body, text=label, variable=var, font=UI_FONT,
                           bg="#1b2027", fg="#eaf2f8", selectcolor="#0f1418",
                           activebackground="#1b2027", activeforeground="#ffffff",
                           anchor="w").pack(anchor="w", fill="x")

        btns = ttk.Frame(wrap)
        btns.pack(fill="x")
        ttk.Button(btns, text="全选", width=8, command=self._all).pack(side="left")
        ttk.Button(btns, text="全不选", width=9, command=self._none).pack(
            side="left", padx=(6, 0))
        ttk.Button(btns, text="应用 (Enter)", width=13, command=self._ok).pack(
            side="right")
        ttk.Button(btns, text="取消 (Esc)", width=12, command=self._cancel).pack(
            side="right", padx=(0, 6))

        top.bind("<Return>", lambda e: self._ok())
        top.bind("<KP_Enter>", lambda e: self._ok())
        top.bind("<Escape>", lambda e: self._cancel())

        top.update_idletasks()
        w, h = top.winfo_width(), top.winfo_height()
        sw, sh = top.winfo_screenwidth(), top.winfo_screenheight()
        top.geometry(f"+{max(0, (sw - w) // 2)}+{max(0, (sh - h) // 3)}")
        try:
            top.grab_set()
        except tk.TclError:
            pass
        top.focus_force()

    def _all(self) -> None:
        for var in self.vars.values():
            var.set(True)

    def _none(self) -> None:
        for var in self.vars.values():
            var.set(False)

    def _ok(self) -> None:
        self.result = {k for k, v in self.vars.items() if v.get()}
        if self._on_apply:
            self._on_apply(self.result)
        self.top.destroy()

    def _cancel(self) -> None:
        self.result = None
        self.top.destroy()

    def show(self) -> set[str] | None:
        self.top.wait_window()
        return self.result


class _RecentPickerDialog(_ChoiceDialog):
    """
    最近使用列表：显示「名字（多久以前）」，选中一个后可直接打开。

    与 `_ChoiceDialog` 的区别：标签带相对时间，返回值是条目序号。
    """

    def __init__(self, parent: tk.Misc, title: str, message: str,
                 entries: list[tuple[str, str]]) -> None:
        self._entries = list(entries)
        self.picked_idx: int | None = None
        super().__init__(parent, title, message,
                         [f"{i + 1:>2}. {label}" for i, (_k, label) in
                          enumerate(self._entries)])

    def _ok(self) -> None:
        sel = self.listbox.curselection()
        self.picked_idx = sel[0] if sel else None
        self.result = self.listbox.get(sel[0]) if sel else None
        self.top.destroy()

    def _cancel(self) -> None:
        self.picked_idx = None
        self.result = None
        self.top.destroy()

    def show(self) -> str | None:
        self.top.wait_window()
        return None if self.picked_idx is None else str(self.picked_idx + 1)


class _CleanupDialog:
    """
    「清理程序文件」面板：列出程序自己产生的可清理文件，勾选后删除。

    安全约定（很重要）：
      · 只列**程序自己产生的**文件：缓存 / 错误日志 / 字节码 / 临时导出 / 设置文件；
      · `winmon_settings.json` 是**用户数据**（收藏 / 手动分类 / 最近使用），
        默认**不勾选**，并在界面上写明风险；
      · 真正删除只走 `core.clean_files(keys)` —— 它只删清单里解析出来的路径，
        不接受任意路径参数，所以这个按钮**不可能**删到别的东西。
    """

    def __init__(self, parent: tk.Misc, items: list[dict]) -> None:
        self.result: list[str] | None = None
        self._items = list(items)
        self.top = tk.Toplevel(parent)
        top = self.top
        top.title("清理程序文件")
        top.configure(bg="#1b2027")
        top.attributes("-topmost", True)
        top.resizable(False, False)

        wrap = ttk.Frame(top, padding=18)
        wrap.pack(fill="both", expand=True)
        tk.Label(wrap, text="以下都是 WinMon 自己产生的文件，勾选后点「清理选中」",
                 font=("Microsoft YaHei UI", 11, "bold"),
                 bg="#1b2027", fg="#eaf2f8", anchor="w").pack(anchor="w")
        tk.Label(wrap, text="默认只勾选「删了会自动重建 / 本来就没用」的项；"
                            "设置文件是你的用户数据，默认不勾。",
                 font=("Microsoft YaHei UI", 9), bg="#1b2027", fg="#a9bfd2",
                 anchor="w", justify="left").pack(anchor="w", pady=(6, 10))

        body = tk.Frame(wrap, bg="#1b2027")
        body.pack(fill="both", expand=True)
        self.vars: dict[str, tk.BooleanVar] = {}
        for item in self._items:
            size = item["bytes"]
            size_text = (f"{size / 1024:.0f} KB" if size >= 1024 else f"{size} B")
            state = "" if item["exists"] else "（没有）"
            var = tk.BooleanVar(value=bool(item["default"] and item["exists"]))
            self.vars[item["key"]] = var
            row = tk.Checkbutton(
                body,
                text=f"{item['label']}　{item['count']} 个文件 / {size_text}{state}",
                variable=var, font=UI_FONT, bg="#1b2027", fg="#eaf2f8",
                selectcolor="#0f1418", activebackground="#1b2027",
                activeforeground="#ffffff", anchor="w", justify="left")
            row.pack(anchor="w", fill="x")
            tk.Label(body, text="    " + item["note"], font=("Microsoft YaHei UI", 8),
                     bg="#1b2027", fg="#8fa8bd", anchor="w", justify="left").pack(
                anchor="w", fill="x", pady=(0, 6))

        btns = ttk.Frame(wrap)
        btns.pack(fill="x", pady=(10, 0))
        ttk.Button(btns, text="清理选中 (Enter)", width=16,
                   command=self._ok).pack(side="right")
        ttk.Button(btns, text="取消 (Esc)", width=12,
                   command=self._cancel).pack(side="right", padx=(0, 8))
        top.bind("<Return>", lambda e: self._ok())
        top.bind("<KP_Enter>", lambda e: self._ok())
        top.bind("<Escape>", lambda e: self._cancel())

        top.update_idletasks()
        w, h = top.winfo_width(), top.winfo_height()
        sw, sh = top.winfo_screenwidth(), top.winfo_screenheight()
        top.geometry(f"+{max(0, (sw - w) // 2)}+{max(0, (sh - h) // 3)}")
        try:
            top.grab_set()
        except tk.TclError:
            pass
        top.focus_force()

    def _ok(self) -> None:
        self.result = [k for k, v in self.vars.items() if v.get()]
        self.top.destroy()

    def _cancel(self) -> None:
        self.result = None
        self.top.destroy()

    def show(self) -> list[str] | None:
        self.top.wait_window()
        return self.result


class _SettingsDialog:
    """
    **独立设置界面**（约定："winmon 设计一个独立的设置 UI"）。

    目前只放用户点名的两项（他明确说"暂时先这样，不加入别的东西"）：

      1. **开机自启动** —— 写 `HKCU\\...\\CurrentVersion\\Run`（**不需要管理员权限**）。
         勾选框的初值**读的是注册表真实状态**，不是"上次点了什么"：
         用户可能在任务管理器→启动里禁用过它。写完**回读核对**，失败如实说。
      2. **关闭按钮的行为**：
         · **隐藏到托盘**（用户指定为默认）—— 点 X 只是收起来，音乐照放、监控继续；
         · 结束进程 —— 点 X 就真的退出（顺带把托盘图标清掉）。

    结果通过 `apply()` 回给调用方（保持"只有主线程碰 Tk"的规矩）。
    """

    def __init__(self, parent: tk.Misc, close_mode: str, tray_available: bool,
                 start_minimized: bool = False,
                 history_days: int = HISTORY_DAYS_DEFAULT) -> None:
        self.result: dict | None = None
        self.top = tk.Toplevel(parent)
        top = self.top
        top.title("WinMon 设置")
        top.configure(bg="#1b2027")
        top.attributes("-topmost", True)
        top.resizable(False, False)

        wrap = ttk.Frame(top, padding=18)
        wrap.pack(fill="both", expand=True)
        tk.Label(wrap, text="WinMon 设置", font=("Microsoft YaHei UI", 11, "bold"),
                 bg="#1b2027", fg="#eaf2f8", anchor="w").pack(anchor="w")

        # ---- 1) 开机自启动 ----
        self.autostart = tk.BooleanVar(value=bool(core.get_autostart()))
        tk.Checkbutton(wrap, text="开机自启动（登录后自动运行 WinMon）",
                       variable=self.autostart, font=UI_FONT, bg="#1b2027",
                       fg="#eaf2f8", selectcolor="#0f1418", activebackground="#1b2027",
                       activeforeground="#ffffff", anchor="w").pack(
            anchor="w", fill="x", pady=(12, 0))
        tk.Label(wrap, text=f"　　写入 HKCU 的 Run 项，不需要管理员权限；"
                            f"命令：{core.autostart_command()[:70]}…",
                 font=("Microsoft YaHei UI", 8), bg="#1b2027", fg="#8fa8bd",
                 anchor="w", justify="left", wraplength=560).pack(anchor="w", fill="x")

        # ---- 2) 关闭按钮行为 ----
        tk.Label(wrap, text="点右上角的 × 时：", font=UI_FONT, bg="#1b2027",
                 fg="#eaf2f8", anchor="w").pack(anchor="w", pady=(14, 2))
        self.close_mode = tk.StringVar(value=close_mode)
        tk.Radiobutton(wrap, text="隐藏到托盘（推荐：音乐继续放、监控继续跑）",
                       variable=self.close_mode, value="tray", font=UI_FONT,
                       bg="#1b2027", fg="#eaf2f8", selectcolor="#0f1418",
                       activebackground="#1b2027", activeforeground="#ffffff",
                       anchor="w").pack(anchor="w", fill="x")
        tk.Radiobutton(wrap, text="结束进程（真的退出，托盘图标一起清掉）",
                       variable=self.close_mode, value="exit", font=UI_FONT,
                       bg="#1b2027", fg="#eaf2f8", selectcolor="#0f1418",
                       activebackground="#1b2027", activeforeground="#ffffff",
                       anchor="w").pack(anchor="w", fill="x")
        if not tray_available:
            tk.Label(wrap, text="　（本机托盘不可用，选「隐藏到托盘」会自动退回结束进程）",
                     font=("Microsoft YaHei UI", 8), bg="#1b2027", fg="#e0a458",
                     anchor="w").pack(anchor="w", fill="x")

        # ---- 3) 启动时直接收进托盘 ----
        self.start_minimized = tk.BooleanVar(value=bool(start_minimized))
        tk.Checkbutton(wrap, text="启动时直接收进托盘（界面不弹出来，双击托盘图标再看）",
                       variable=self.start_minimized, font=UI_FONT, bg="#1b2027",
                       fg="#eaf2f8", selectcolor="#0f1418", activebackground="#1b2027",
                       activeforeground="#ffffff", anchor="w").pack(
            anchor="w", fill="x", pady=(12, 0))
        if not tray_available:
            tk.Label(wrap, text="　（托盘不可用时这一项不生效，界面照常显示）",
                     font=("Microsoft YaHei UI", 8), bg="#1b2027", fg="#8fa8bd",
                     anchor="w").pack(anchor="w", fill="x")

        # ---- 4) 操作历史保留天数 ----
        tk.Label(wrap, text="操作历史日志保留：", font=UI_FONT, bg="#1b2027",
                 fg="#eaf2f8", anchor="w").pack(anchor="w", pady=(14, 0))
        row = ttk.Frame(wrap)
        row.pack(anchor="w", fill="x")
        self.history_days = tk.StringVar(value=str(int(history_days)))
        for days, label in HISTORY_DAYS_CHOICES:
            tk.Radiobutton(row, text=label, variable=self.history_days,
                           value=str(days), font=UI_FONT, bg="#1b2027",
                           fg="#eaf2f8", selectcolor="#0f1418",
                           activebackground="#1b2027", activeforeground="#ffffff",
                           anchor="w").pack(side="left", padx=(0, 10))
        tk.Label(wrap, text=f"　　启动时自动删掉超过这个天数的记录（默认 "
                            f"{HISTORY_DAYS_DEFAULT} 天）。"
                            f"清理前会把原文件另存一份 .bak，删错了还能找回来。",
                 font=("Microsoft YaHei UI", 8), bg="#1b2027", fg="#8fa8bd",
                 anchor="w", justify="left", wraplength=560).pack(anchor="w", fill="x")

        #: 自启动那条要写注册表（有副作用），所以点"保存"才生效，并当场回报结果
        self.status = tk.StringVar(value="")
        tk.Label(wrap, textvariable=self.status, font=("Microsoft YaHei UI", 9),
                 bg="#1b2027", fg="#9fd6a0", anchor="w", justify="left",
                 wraplength=560).pack(anchor="w", fill="x", pady=(10, 0))

        btns = ttk.Frame(wrap)
        btns.pack(fill="x", pady=(12, 0))
        ttk.Button(btns, text="保存 (Enter)", width=13, command=self._ok).pack(side="right")
        ttk.Button(btns, text="取消 (Esc)", width=12, command=self._cancel).pack(
            side="right", padx=(0, 8))
        top.bind("<Return>", lambda e: self._ok())
        top.bind("<KP_Enter>", lambda e: self._ok())
        top.bind("<Escape>", lambda e: self._cancel())

        top.update_idletasks()
        w, h = top.winfo_width(), top.winfo_height()
        sw, sh = top.winfo_screenwidth(), top.winfo_screenheight()
        top.geometry(f"+{max(0, (sw - w) // 2)}+{max(0, (sh - h) // 3)}")
        try:
            top.grab_set()
        except tk.TclError:
            pass
        top.focus_force()

    def _ok(self) -> None:
        """
        保存：自启动**真的写注册表并回读**，失败就在这个框里说清楚（不关窗）。

        为什么失败不关窗：这是"有副作用的动作"，用户需要看到到底成没成；
        悄悄关掉再在状态栏飘一句，等于把失败藏起来（铁律 2）。
        """
        want = bool(self.autostart.get())
        now = bool(core.get_autostart())
        if want != now:
            ok, msg = core.set_autostart(want)
            if not ok:
                self.status.set(f"⚠ {msg}")
                return
        self.result = {"autostart": want, "close_mode": self.close_mode.get(),
                       "start_minimized": bool(self.start_minimized.get()),
                       "history_days": int(self.history_days.get() or HISTORY_DAYS_DEFAULT)}
        self.top.destroy()

    def _cancel(self) -> None:
        self.result = None
        self.top.destroy()

    def show(self) -> dict | None:
        self.top.wait_window()
        return self.result


class _HistoryWindow:
    """
    **独立的「操作历史」窗口**（约定：操作历史做一个独立 UI）。

    与主界面上那个折叠面板的区别：
      · 内容来自**日志文件**（`X:\\winmon\\winmon_history.log`），
        **跨会话保留** —— 关掉 WinMon 再打开，以前的记录还在；
      · 有筛选（按动作类型 / 关键词）、表格列、以及"打开日志文件所在文件夹"；
      · 独立窗口，不占主界面的纵向空间（主窗口纵向本来就排满了）。

    安全约定：**「清空列表」只清界面上的列表**；要删磁盘上的留痕必须点
    「清空日志文件」并二次确认 —— 留痕是审计用的，不该被顺手删掉。
    """

    #: 表格列（顺序与宽度）
    #: 时间**到分钟**（用户 2026-10-12 的要求："到分钟记录就行，不用精确到秒"）。
    #:「打开时间」那一列还留着：它是**进程启动时刻**，和"什么时候关了它"配成一对。
    COLS = (("time", "时间", 118), ("open_time", "程序打开时间", 106),
            ("action", "动作", 96), ("target", "对象（程序 + 窗口）", 268),
            ("result", "结果", 300), ("extra", "备注", 130))
    #: 最多显示多少条（日志可能很大，界面没必要全渲染）
    MAX_ROWS = 500

    def __init__(self, parent: tk.Misc, app) -> None:
        self.app = app
        self.top = tk.Toplevel(parent)
        top = self.top
        top.title(f"WinMon 操作历史 — {os.path.basename(app.history.log_path)}")
        top.geometry("1100x560")
        top.minsize(760, 360)
        #: 关窗只是"收起来"，下次再点按钮还能打开（`app._history_window` 会被清空）
        top.protocol("WM_DELETE_WINDOW", self.close)
        self._rows: list[dict] = []
        #: 当前**筛掉之后**还剩哪些（导出要跟着筛选走）；None = 还没刷新过
        self._kept: list[dict] | None = None
        #: 「清空列表」的边界：清空那一刻**最新那条**记录的键。
        #: 之后只显示比它更新的记录 —— 这样"清空列表"真的空得掉，
        #: 而日志文件（审计留痕）一动不动（见 `clear_list` 的说明）。
        self._cleared_boundary: tuple | None = None

        wrap = ttk.Frame(top, padding=(10, 8))
        wrap.pack(fill="both", expand=True)

        # ---- 顶部：说明 + 筛选 ----
        head = ttk.Frame(wrap)
        head.pack(fill="x")
        ttk.Label(head, text="操作历史（留痕：谁在什么时候被关闭 / 结束了）",
                  font=BOLD_FONT).pack(side="left")
        self.count_text = tk.StringVar(value="")
        ttk.Label(head, textvariable=self.count_text, font=UI_FONT,
                  foreground="#555").pack(side="right")

        bar = ttk.Frame(wrap)
        bar.pack(fill="x", pady=(6, 4))
        ttk.Label(bar, text="只看动作：", font=UI_FONT).pack(side="left")
        self.kind = tk.StringVar(value="（全部）")
        self.kind_combo = ttk.Combobox(bar, textvariable=self.kind, state="readonly",
                                       width=12, font=UI_FONT, values=["（全部）"])
        self.kind_combo.pack(side="left", padx=(4, 8))
        self.kind_combo.bind("<<ComboboxSelected>>", lambda _e: self.reload())
        # **按程序筛**（B.1）：从日志的"对象"里抽出程序名（`xxx.exe`）做下拉
        ttk.Label(bar, text="只看程序：", font=UI_FONT).pack(side="left")
        self.program = tk.StringVar(value="（全部）")
        self.program_combo = ttk.Combobox(bar, textvariable=self.program,
                                          state="readonly", width=16, font=UI_FONT,
                                          values=["（全部）"])
        self.program_combo.pack(side="left", padx=(4, 8))
        self.program_combo.bind("<<ComboboxSelected>>", lambda _e: self.reload())
        ttk.Label(bar, text="搜索：", font=UI_FONT).pack(side="left")
        self.query = tk.StringVar(value="")
        ent = ttk.Entry(bar, textvariable=self.query, width=18, font=UI_FONT)
        ent.pack(side="left", padx=(4, 6))
        ent.bind("<KeyRelease>", lambda _e: self.reload())
        ttk.Button(bar, text="刷新", width=6, command=self.reload).pack(side="left", padx=2)
        ttk.Button(bar, text="打开日志文件夹", width=14,
                   command=self.reveal_log).pack(side="right", padx=2)
        ttk.Button(bar, text="导出 CSV…", width=10,
                   command=self.export_csv).pack(side="right", padx=2)
        ttk.Button(bar, text="导出 txt…", width=10,
                   command=self.export).pack(side="right", padx=2)
        ttk.Button(bar, text="复制全部", width=9,
                   command=self.copy).pack(side="right", padx=2)

        # ---- 表格 ----
        body = ttk.Frame(wrap)
        body.pack(fill="both", expand=True)
        tv = ttk.Treeview(body, columns=[c[0] for c in self.COLS], show="headings",
                          selectmode="browse")
        self.tree = tv
        for key, label, width in self.COLS:
            tv.heading(key, text=label)
            tv.column(key, width=width, anchor="w", stretch=(key in ("result", "target")))
        vs = ttk.Scrollbar(body, orient="vertical", command=tv.yview)
        tv.configure(yscrollcommand=vs.set)
        tv.pack(side="left", fill="both", expand=True)
        vs.pack(side="left", fill="y")
        tv.tag_configure("fail", foreground="#c0392b")
        tv.tag_configure("ok", foreground="#1b5e20")

        # ---- 底部：危险动作与说明 ----
        foot = ttk.Frame(wrap)
        foot.pack(fill="x", pady=(6, 0))
        self.status = tk.StringVar(value="")
        tk.Label(foot, textvariable=self.status, font=("Microsoft YaHei UI", 8),
                 fg="#5a6b7b", anchor="w").pack(side="left")
        ttk.Button(foot, text="清空日志文件…", width=14,
                   command=self.clear_log).pack(side="right", padx=2)
        ttk.Button(foot, text="清空列表", width=9,
                   command=self.clear_list).pack(side="right", padx=2)
        ttk.Button(foot, text="关闭", width=7, command=self.close).pack(
            side="right", padx=(2, 8))

        top.bind("<Escape>", lambda _e: self.close())
        top.bind("<F5>", lambda _e: self.reload())
        self.reload()

    # ---- 数据 ----

    def reload(self) -> None:
        """
        从日志文件重新读一遍并按筛选条件刷新表格。

        「清空列表」的清空效果也在这里落地：见 `_cleared_boundary` / `clear_list`。
        """
        rows = self.app.history.load_from_log(limit=self.MAX_ROWS)
        total_in_log = len(rows)
        rows, hidden = self._hide_cleared(rows)
        self._rows = rows
        kinds = sorted({r["action"] for r in rows if r.get("action")})
        try:
            self.kind_combo.configure(values=["（全部）"] + kinds)
        except Exception:
            pass
        # **按程序筛**（B.1）：从"对象"列里抽出程序名（`xxx.exe`，或"工作区「名」"这种标签）
        programs = sorted({self._program_of(r) for r in rows if self._program_of(r)})
        try:
            self.program_combo.configure(values=["（全部）"] + programs)
        except Exception:
            pass
        want_kind = self.kind.get()
        want_prog = self.program.get()
        needle = self.query.get().strip().lower()

        def keep(row) -> bool:
            if want_kind not in ("", "（全部）") and row.get("action") != want_kind:
                return False
            if want_prog not in ("", "（全部）") and self._program_of(row) != want_prog:
                return False
            if needle:
                blob = " ".join(str(row.get(k, "")) for k in
                                ("time", "action", "target", "result", "extra")).lower()
                if needle not in blob:
                    return False
            return True

        kept = [r for r in rows if keep(r)]
        tv = self.tree
        tv.delete(*tv.get_children())
        for r in kept:
            tags = ()
            res = str(r.get("result") or "")
            if "失败" in res or "仍在" in res or "没" in res:
                tags = ("fail",)
            elif res:
                tags = ("ok",)
            tv.insert("", "end", tags=tags, values=(
                r.get("time", ""), r.get("open_time", "") or "—",
                r.get("action", ""), r.get("target", ""),
                res, r.get("extra", "")))
        self._kept = kept
        cleared = f"　·　已按「清空列表」隐藏 {hidden} 条（日志未动）" if hidden else ""
        self.count_text.set(f"显示 {len(kept)} / 日志共 {total_in_log} 条{cleared}")
        err = getattr(self.app.history, "log_error", "")
        days = self.app.history_days.get() if hasattr(self.app, "history_days") else "?"
        self.status.set(f"日志文件：{self.app.history.log_path}"
                        f"　·　保留 {days} 天（启动时自动清理，可在「设置…」里改）"
                        + (f"　⚠ {err}" if err else ""))

    @staticmethod
    def _row_key(row: dict) -> tuple:
        """
        一条记录的"身份"（给「清空列表」当边界标记用）。

        **不用时间戳**：日志时间戳只到分钟（约定），同一分钟里清空之后新产生的
        记录会被一起藏掉，最长要等一分钟才看得见。
        """
        return tuple(str((row or {}).get(k) or "") for k in
                     ("time", "action", "target", "result", "extra", "open_time"))

    def _hide_cleared(self, rows: list[dict]) -> tuple[list[dict], int]:
        """
        按「清空列表」留下的边界，把清空**之前**那些记录从显示里去掉。

        返回 `(要显示的, 被藏起来的条数)`。`rows` 是"最新在前"。
        边界找不到（日志又被清空 / 轮转过 / 被裁掉）就退回"全都显示"并作废边界 ——
        宁可多显示几条，也不要让新记录莫名不见。
        """
        if not self._cleared_boundary or not rows:
            return rows, 0
        for idx, row in enumerate(rows):
            if self._row_key(row) == self._cleared_boundary:
                return rows[:idx], len(rows) - idx
        self._cleared_boundary = None
        return rows, 0

    @staticmethod
    def _program_of(row: dict) -> str:
        """
        从一条记录里抽出"是哪个程序"（给"只看程序"下拉用）。

        规则：对象列形如 `程序名.exe [PID]｜窗口标题`，取 `.exe` 那一段；
        取不到（比如"工作区「X」"、"系统设置"）就退回整列 —— 这样**按程序筛**
        对"恢复工作区"这类记录同样有意义。
        """
        target = str((row or {}).get("target") or "")
        m = re.match(r"\s*([^\s\[\]｜]+\.exe)", target, re.I)
        if m:
            return m.group(1).lower()
        head = target.split("｜")[0].strip()
        return head[:24]

    # ---- 动作 ----

    def copy(self) -> None:
        if not self._rows:
            self.status.set("没有可复制的记录")
            return
        text = "\n".join(self.app.history.format_line(r) for r in self._rows)
        try:
            self.top.clipboard_clear()
            self.top.clipboard_append(text)
            self.status.set(f"已复制 {len(self._rows)} 条到剪贴板")
        except Exception as exc:
            self.status.set(f"复制失败：{exc}")

    def export(self) -> None:
        """导出当前**筛选后**的记录为 txt（B.1：导出要跟着筛选走，别导一堆无关的）。"""
        # 注意 `_kept` 可能是**空列表**（筛完一条不剩）—— 那是"没有可导出的"，
        # 不能因为它是空的就退回导出全部（`or` 会这么干，实测踩到过）。
        rows = self._kept if self._kept is not None else self._rows
        if not rows:
            self.status.set("没有可导出的记录（当前筛选下一条都没有）")
            return
        try:
            path = self.app.history.export(rows=rows)
            self.status.set(f"已导出 {len(rows)} 条：{path}")
            core.open_file(path)
        except Exception as exc:
            self.status.set(f"导出失败：{exc}")

    def export_csv(self) -> None:
        """
        导出为 **CSV**（B.1）。用 UTF-8 **带 BOM**：Excel 双击打开中文才不乱码。
        """
        rows = self._kept if self._kept is not None else self._rows
        if not rows:
            self.status.set("没有可导出的记录（当前筛选下一条都没有）")
            return
        try:
            path = self.app.history.export_csv(rows=rows)
            self.status.set(f"已导出 {len(rows)} 条 CSV：{path}")
            core.open_file(path)
        except Exception as exc:
            self.status.set(f"导出 CSV 失败：{exc}")

    def reveal_log(self) -> None:
        """在资源管理器里定位日志文件（不打开、不修改，只是给用户看在哪）。"""
        path = self.app.history.log_path
        try:
            core.reveal_in_explorer(path)
            self.status.set(f"已在资源管理器中定位：{path}")
        except Exception as exc:
            self.status.set(f"打开位置失败：{exc}（日志在 {path}）")

    def clear_list(self) -> None:
        """
        「清空列表」= **只清掉窗口里显示的列表**，日志文件一动不动（README 与铁律）。

        为什么做成"记一个边界"而不是直接清内存：这个窗口的数据来源是**日志文件**
        （跨会话留痕），所以只调 `app.clear_history()` 再 `reload()` 的话，
        文件内容立刻又被读回来 —— 实测结果就是这个按钮**什么都没清却报告"已清空"**
        （2026-10-13 复现）。现在把"清空那一刻最新那条记录"记成边界，
        只显示比它更新的记录：列表当场空了、磁盘留痕照旧、之后的新记录继续显示。
        """
        self._cleared_boundary = (self._row_key(self._rows[0]) if self._rows else None)
        self.app.clear_history()          # 内存那份（主界面"本次 N 条"）一起清掉
        self.reload()
        self.status.set("已清空窗口里的列表（日志文件未动；之后的新记录会继续显示）")

    def clear_log(self) -> None:
        self.app.clear_history_log()      # 里面有二次确认
        self.reload()

    def close(self) -> None:
        try:
            self.top.destroy()
        finally:
            self.app._history_window = None


class _AiUsageWindow:
    """
    「AI 用量」独立窗口（阶段 2.5 / 阶段 5）。

    只读展示 + **本地**恢复手段：暂停 / 恢复、重置今日、上限翻倍。
    按设计，**MCP 没有任何改预算的工具** —— 钥匙只在人手里。

    用户 2026-10-19 的要求：打开这个窗口必须一眼看见
      · 现在识别到的是哪个 AI（以及依据）；
      · 这个 token 数是谁的（真实 API 回执 / 精确分词 / 估算）；
      · 直连 GLM 有没有就绪。
    """

    def __init__(self, parent: tk.Misc, app) -> None:
        self.app = app
        self.top = tk.Toplevel(parent)
        self.top.title("WinMon AI 识别与用量")
        scale = float(getattr(app, "dpi_scale", 1.0) or 1.0)
        self.top.geometry(f"{int(920 * scale)}x{int(640 * scale)}")
        self.top.minsize(int(720 * scale), int(480 * scale))
        self.top.protocol("WM_DELETE_WINDOW", self.close)
        self.budget = ai.Budget()
        self._after_id = None

        wrap = ttk.Frame(self.top, padding=(10, 8))
        wrap.pack(fill="both", expand=True)
        head = ttk.Frame(wrap)
        head.pack(fill="x")
        ttk.Label(head, text="AI 识别与用量（真实 / 精确 / 估算分栏）",
                  font=BOLD_FONT).pack(side="left")
        self.state_text = tk.StringVar(value="")
        ttk.Label(head, textvariable=self.state_text, font=UI_FONT,
                  foreground="#555").pack(side="right")

        body_holder = ttk.Frame(wrap)
        body_holder.pack(fill="both", expand=True, pady=(8, 8))
        self.body = tk.Text(body_holder, height=20, wrap="word", font=MONO_FONT,
                            relief="solid", borderwidth=1)
        scroll = ttk.Scrollbar(body_holder, orient="vertical", command=self.body.yview)
        self.body.configure(yscrollcommand=scroll.set)
        self.body.pack(side="left", fill="both", expand=True)
        scroll.pack(side="left", fill="y")
        self.body.configure(state="disabled")

        bar = ttk.Frame(wrap)
        bar.pack(fill="x")
        self.pause_btn = ttk.Button(bar, text="暂停 AI 接入", width=14,
                                    command=self.toggle_pause)
        self.pause_btn.pack(side="left", padx=2)
        ttk.Button(bar, text="重置今日用量", width=14,
                   command=self.reset_today).pack(side="left", padx=2)
        ttk.Button(bar, text="上限 ×2", width=10,
                   command=self.raise_limit).pack(side="left", padx=2)
        ttk.Button(bar, text="刷新", width=8,
                   command=self.refresh).pack(side="left", padx=2)
        #: 精确分词开关：把 MCP 工具结果发给智谱分词器计数（默认跟随配置文件）
        self.exact_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(bar, text="MCP 结果精确分词",
                        variable=self.exact_var,
                        command=self.toggle_exact).pack(side="left", padx=(10, 2))
        ttk.Button(bar, text="打开数据目录", width=14,
                   command=self.reveal).pack(side="right", padx=2)
        ttk.Button(bar, text="AI 助手…", width=9,
                   command=lambda: getattr(self.app, "action_ai_assistant",
                                           lambda: None)()).pack(side="right", padx=2)
        note = ttk.Frame(wrap)
        note.pack(fill="x", pady=(4, 0))
        ttk.Label(note, text="恢复预算只能在这里操作；MCP 没有改预算的工具。"
                            "精确分词会把 MCP 工具结果发给智谱分词器。",
                  font=UI_FONT, foreground="#777").pack(side="left")
        self.refresh()
        self._auto_refresh()

    def _auto_refresh(self) -> None:
        """窗口开着时每 3 秒自刷新（关窗必须取消，否则会打 Tcl 噪音）。"""
        self._after_id = None
        try:
            if not self.top.winfo_exists():
                return
            self.refresh()
            self._after_id = self.top.after(3000, self._auto_refresh)
        except tk.TclError:
            self._after_id = None

    def refresh(self) -> None:
        status = self.budget.status()
        label = {"ok": "正常", "warn": "接近上限", "exhausted": "已用尽",
                 "paused": "已暂停", "disabled": "已关闭"}.get(
                     status.get("state"), status.get("state"))
        self.state_text.set(f"当前：{label}")
        self.pause_btn.configure(
            text="恢复 AI 接入" if status.get("paused") else "暂停 AI 接入")
        try:
            info = self.app._ai_provider_snapshot()
        except Exception:
            info = {}
        try:
            tokenizer = provider.tokenizer_config()
            self.exact_var.set(bool(tokenizer is not None and tokenizer.exact_tokens))
        except Exception:
            pass
        follow = str(info.get("follow_ai") or "")
        if info.get("configured") is False:
            direct = (f"当前 AI：{follow or '（还没识别到）'} 没有直连配置 → "
                      "用剪贴板交接；不会调用任何模型")
        elif info.get("configured"):
            if not info.get("enabled"):
                direct = f"{follow} → {info.get('model')} 未启用"
            elif not info.get("key_present"):
                direct = (f"{follow} → {info.get('model')} 缺 API key"
                          f"（环境变量 {info.get('api_key_env') or 'WINMON_AI_API_KEY'}）")
            else:
                direct = (f"跟随当前 AI：{follow} → {info.get('model')} 已就绪"
                          f"（{info.get('kind')}）")
        else:
            direct = "（主界面还没跑识别；2 秒后会自动刷新）"
        lines = [
            "== 当前识别（这是“现在在用谁”的唯一判据）==",
            ai.format_sessions_detail(getattr(self.app, "_ai_sessions", None)),
            "",
            "== 用量与预算 ==",
            ai.format_usage_text(status),
            "",
            "== 直连模型 ==",
            f"  {direct}",
            "  精确分词：MCP 工具结果用目标模型分词器计数；"
            "关掉后这一部分只能估算（协议不提供客户端账单）。",
            "",
            "== 最近记录（最多 5 条）==",
        ]
        source_label = {"provider": "真实 API", "tokenizer": "精确分词",
                        "estimated": "估算"}
        recent = status.get("recent") or []
        if not recent:
            lines.append("（还没有记录）")
        for row in recent[-5:]:
            src = source_label.get(str(row.get("token_source")), "?")
            lines.append(
                f"{row.get('ts', '')}  {row.get('client') or '?'}  "
                f"{row.get('tool') or '-'}  {row.get('tokens', 0)} token（{src}）")
        self.body.configure(state="normal")
        self.body.delete("1.0", "end")
        self.body.insert("1.0", "\n".join(lines))
        self.body.configure(state="disabled")

    def toggle_pause(self) -> None:
        current = bool(self.budget.status().get("paused"))
        self.budget.pause(not current)
        self.refresh()

    def reset_today(self) -> None:
        self.budget.reset_today()
        self.refresh()

    def raise_limit(self) -> None:
        self.budget.raise_limit(2.0)
        self.refresh()

    def toggle_exact(self) -> None:
        """切换"MCP 结果精确分词"（只改一个键，不动 API key / 模型）。"""
        flag = bool(self.exact_var.get())
        ok = provider.set_exact_tokens(flag)
        if not ok:
            self.exact_var.set(not flag)
            self.app._set_status("精确分词需要先有直连配置（winmon_ai_provider.json）")
        else:
            self.app._set_status(f"MCP 结果精确分词已{'开启' if flag else '关闭'}")
        self.refresh()

    def reveal(self) -> None:
        try:
            os.startfile(core.data_dir())
        except Exception as exc:
            self.app._set_status(f"打不开数据目录：{type(exc).__name__}: {exc}")

    def close(self) -> None:
        if self._after_id is not None:
            try:
                self.top.after_cancel(self._after_id)
            except Exception:
                pass
            self._after_id = None
        try:
            self.top.destroy()
        finally:
            self.app._ai_usage_window = None


class _AiAssistantWindow:
    """
    「AI 助手」窗口（阶段 6）：**跟随用户正在用的 AI**。

    它不再固定调用 GLM-5.3：
      · 当前 AI 有它自己的直连配置 → 调用那个模型，显示真实 token；
      · 没有直连配置 → 把现场复制到剪贴板，交给用户正在用的 AI 粘贴；
      · 两条路都不会"偷偷换成另一个模型"。
    窗口里的记录来自 `winmon_ai_chat.jsonl`：直连回答、剪贴板交接、
    以及用户手动保存回来的回答——所以关掉 AI 的对话窗口后，这里仍有留痕。
    """

    def __init__(self, parent: tk.Misc, app) -> None:
        self.app = app
        self.top = tk.Toplevel(parent)
        self.top.title("WinMon AI 助手（跟随当前 AI）")
        scale = float(getattr(app, "dpi_scale", 1.0) or 1.0)
        self.top.geometry(f"{int(900 * scale)}x{int(660 * scale)}")
        self.top.minsize(int(720 * scale), int(480 * scale))
        self.top.protocol("WM_DELETE_WINDOW", self.close)

        wrap = ttk.Frame(self.top, padding=(10, 8))
        wrap.pack(fill="both", expand=True)
        self.header = tk.StringVar(value="正在读取当前 AI…")
        ttk.Label(wrap, textvariable=self.header, font=UI_FONT,
                  foreground="#2a4a66").pack(fill="x")

        #: 没配 key 时给一个"只用于本次运行"的入口：不写盘、不进日志。
        self.key_row = ttk.Frame(wrap)
        self.key_var = tk.StringVar(value="")
        try:
            _pinfo = app._ai_provider_snapshot()
        except Exception:
            _pinfo = {}
        self._key_env = str(_pinfo.get("api_key_env") or "WINMON_AI_API_KEY")
        if (_pinfo.get("configured") and _pinfo.get("enabled")
                and not _pinfo.get("key_present")):
            self.key_row.pack(fill="x", pady=(4, 0))
            ttk.Label(self.key_row, text=f"API key（{self._key_env}，仅本次运行）：",
                      font=UI_FONT, foreground="#7a4a00").pack(side="left")
            ttk.Entry(self.key_row, textvariable=self.key_var, show="*",
                      width=32).pack(side="left", padx=4)
            ttk.Button(self.key_row, text="用于本次运行", width=12,
                       command=self.use_key).pack(side="left")
            ttk.Label(self.key_row, text="（长期用同名环境变量）",
                      font=UI_FONT, foreground="#777").pack(side="left", padx=6)

        body_holder = ttk.Frame(wrap)
        body_holder.pack(fill="both", expand=True, pady=(8, 6))
        self.body = tk.Text(body_holder, height=18, wrap="word", font=MONO_FONT,
                            relief="solid", borderwidth=1)
        scroll = ttk.Scrollbar(body_holder, orient="vertical", command=self.body.yview)
        self.body.configure(yscrollcommand=scroll.set)
        self.body.pack(side="left", fill="both", expand=True)
        scroll.pack(side="left", fill="y")
        self.body.configure(state="disabled")

        ask = ttk.Frame(wrap)
        ask.pack(fill="x", pady=(0, 4))
        self.question = tk.StringVar(value="")
        entry = ttk.Entry(ask, textvariable=self.question, font=UI_FONT)
        entry.pack(side="left", fill="x", expand=True, padx=(0, 4))
        entry.bind("<Return>", lambda _e: self.send())
        ttk.Button(ask, text="分析当前窗口", width=13,
                   command=self.analyze).pack(side="right", padx=2)
        ttk.Button(ask, text="发送", width=8,
                   command=self.send).pack(side="right", padx=2)

        more = ttk.Frame(wrap)
        more.pack(fill="x", pady=(0, 4))
        ttk.Button(more, text="复制现场给 AI", width=13,
                   command=self.handoff).pack(side="left", padx=2)
        ttk.Button(more, text="保存 AI 回复", width=12,
                   command=self.save_reply).pack(side="left", padx=2)
        ttk.Button(more, text="AI 用量…", width=9,
                   command=self.app.action_ai_usage).pack(side="right", padx=2)

        opts = ttk.Frame(wrap)
        opts.pack(fill="x")
        assist = getattr(app, "_ai_assist", {}) or {}
        self.watch_var = tk.BooleanVar(value=bool(assist.get("enabled")))
        self.explain_var = tk.BooleanVar(value=bool(assist.get("auto_explain")))
        ttk.Checkbutton(opts, text="值守（前台变化时检查）",
                        variable=self.watch_var,
                        command=self.toggle_watch).pack(side="left", padx=2)
        ttk.Checkbutton(opts, text="出问题时自动解释",
                        variable=self.explain_var,
                        command=self.toggle_explain).pack(side="left", padx=8)
        interval = int(assist.get("interval_seconds", 300) or 300)
        ttk.Label(wrap, text=f"最快每 {max(1, interval // 60)} 分钟一次；"
                            "只在前台窗口变化或出现新事件时调用，且受预算限制。"
                            "没有直连配置时不会自动调用，只用剪贴板交接。",
                  font=UI_FONT, foreground="#777").pack(fill="x", pady=(2, 0))

        records = ai.load_chat(limit=8)
        if records:
            self.append("== 本地记录（直连回答 / 剪贴板交接 / 手动保存的回复）==")
            for record in records:
                self.append(ai.format_chat_record(record))
        else:
            for line in list(getattr(app, "_ai_log_lines", []) or []):
                self.append(line)
            if not getattr(app, "_ai_log_lines", None):
                self.append("[提示] 「分析当前窗口」会跟随当前 AI：能直连就调用，"
                            "否则把现场复制给你粘贴；不会偷换成别的模型。")
        self.set_header(app._ai_header_text()
                        if hasattr(app, "_ai_header_text") else "")

    def append(self, text: str) -> None:
        self.body.configure(state="normal")
        self.body.insert("end", text + "\n\n")
        self.body.see("end")
        self.body.configure(state="disabled")

    def set_header(self, text: str) -> None:
        if text:
            self.header.set(text)

    def use_key(self) -> None:
        """把用户粘进来的 key 放进**本进程**的对应环境变量（不写任何文件）。"""
        key = (self.key_var.get() or "").strip()
        if not key:
            self.append("[设置] key 为空，没有改动。")
            return
        os.environ[self._key_env or "WINMON_AI_API_KEY"] = key
        self.key_var.set("")
        try:
            self.key_row.pack_forget()
        except Exception:
            pass
        self.append(f"[设置] 已在本进程设置 {self._key_env}"
                    "（不写盘；重启后需重设或改用环境变量）。")
        try:
            self.set_header(self.app._ai_header_text())
        except Exception:
            pass

    def analyze(self) -> None:
        self.app.ai_assistant_request("")

    def send(self) -> None:
        question = (self.question.get() or "").strip()
        if not question:
            self.analyze()
            return
        if self.app.ai_assistant_request(question):
            self.question.set("")

    def handoff(self) -> None:
        """强制走剪贴板交接（即使当前 AI 有直连配置）。"""
        question = (self.question.get() or "").strip()
        if self.app.ai_assistant_request(question, force_handoff=True):
            self.question.set("")

    def save_reply(self) -> None:
        """从剪贴板读取用户复制的 AI 回复，存进本地记录。"""
        try:
            text = self.top.clipboard_get()
        except Exception:
            text = ""
        if not (text or "").strip():
            self.append("[保存] 剪贴板为空：先在 AI 那边复制它的回答，再点一次。")
            return
        self.app.ai_save_reply(text)

    def toggle_watch(self) -> None:
        data = self.app.set_assist(enabled=bool(self.watch_var.get()))
        self.append(f"[设置] 值守：{'开' if data.get('enabled') else '关'}"
                    f"（每 {max(1, int(data.get('interval_seconds', 300)) // 60)} 分钟，"
                    "前台变化时才调用）")

    def toggle_explain(self) -> None:
        data = self.app.set_assist(auto_explain=bool(self.explain_var.get()))
        self.append(f"[设置] 出问题时自动解释：{'开' if data.get('auto_explain') else '关'}")

    def close(self) -> None:
        try:
            self.top.destroy()
        finally:
            self.app._ai_assistant_window = None


class _IdleDialog:
    """
    「闲置程序」清单：列出"开着不用"的程序，勾选后**优雅关闭**。

    安全约定（很重要）：
      · 只走 `core.close_idle_window()` → 只发 `WM_CLOSE`（等价于点窗口的关闭按钮）。
        程序里有未保存的内容时，**它自己会弹"要不要保存"**，决定权回到用户手里；
        它没退出（多半正等你回答）也**不替你强杀**，如实报告。
      · 关键系统进程、外壳、输入法、以及 WinMon 自己在 `core.idle_samples()` 里
        就被过滤掉了 —— 它们根本不会出现在这个清单上。
      · 「稍后再说」= 把这些程序的闲置计时清零，它们会重新计时、到点再提醒。
    """

    def __init__(self, parent: tk.Misc, rows: list[dict], threshold_label: str,
                 auto_close: "tk.BooleanVar") -> None:
        self.result: list[dict] | None = None
        self.auto_close_changed = False
        self._rows = list(rows)
        self._auto_close = auto_close
        self._before = bool(auto_close.get())
        self.top = tk.Toplevel(parent)
        top = self.top
        top.title("闲置程序 — 开了很久但没人用")
        top.configure(bg="#1b2027")
        top.attributes("-topmost", True)
        top.resizable(False, False)

        wrap = ttk.Frame(top, padding=18)
        wrap.pack(fill="both", expand=True)
        tk.Label(wrap, text=f"这些程序连续 {threshold_label} 没有被使用，可以关掉省电",
                 font=("Microsoft YaHei UI", 11, "bold"),
                 bg="#1b2027", fg="#eaf2f8", anchor="w").pack(anchor="w")
        tk.Label(wrap, text="判定依据：窗口不在前台、CPU 几乎为 0。\n"
                            "只会「优雅关闭」（等同于点它的关闭按钮）——有未保存的内容时"
                            "程序会自己问你要不要保存；\n它没退出也不会替你强杀。",
                 font=("Microsoft YaHei UI", 9), bg="#1b2027", fg="#a9bfd2",
                 anchor="w", justify="left").pack(anchor="w", pady=(6, 10))

        body = tk.Frame(wrap, bg="#1b2027")
        body.pack(fill="both", expand=True)
        self.vars: dict[int, tk.BooleanVar] = {}
        for row in self._rows:
            pid = int(row["pid"])
            var = tk.BooleanVar(value=True)          # 默认全勾：用户是奔着"收拾"来的
            self.vars[pid] = var
            title = (row.get("title") or "").strip()
            if len(title) > 46:
                title = title[:45] + "…"
            state = "（已最小化）" if row.get("iconic") else ""
            tk.Checkbutton(
                body,
                text=f"{row.get('name') or ('PID ' + str(pid))}　"
                     f"闲置 {core.format_idle(row['idle'])}{state}",
                variable=var, font=UI_FONT, bg="#1b2027", fg="#eaf2f8",
                selectcolor="#0f1418", activebackground="#1b2027",
                activeforeground="#ffffff", anchor="w", justify="left").pack(
                anchor="w", fill="x")
            tk.Label(body, text=f"    PID {pid}　{title}",
                     font=("Microsoft YaHei UI", 8), bg="#1b2027", fg="#8fa8bd",
                     anchor="w", justify="left").pack(anchor="w", fill="x", pady=(0, 6))

        self._auto_var = tk.BooleanVar(value=self._before)
        tk.Checkbutton(wrap, text="以后到点自动关闭（不再询问）——只优雅关闭，不强杀",
                       variable=self._auto_var, font=UI_FONT, bg="#1b2027",
                       fg="#ffd26b", selectcolor="#0f1418", activebackground="#1b2027",
                       activeforeground="#ffffff", anchor="w").pack(
            anchor="w", pady=(4, 0))

        btns = ttk.Frame(wrap)
        btns.pack(fill="x", pady=(10, 0))
        ttk.Button(btns, text="关闭选中的 (Enter)", width=18,
                   command=self._ok).pack(side="right")
        # **一键最小化**（B.2）：不想关掉、只想先把桌面收干净时用这个。
        # 它比"关闭"温和得多：只是把窗口收起来，程序还在跑、随时能还原。
        ttk.Button(btns, text="最小化选中的", width=14,
                   command=self._minimize).pack(side="right", padx=(0, 8))
        ttk.Button(btns, text="稍后再说 (Esc)", width=14,
                   command=self._cancel).pack(side="right", padx=(0, 8))
        top.bind("<Return>", lambda e: self._ok())
        top.bind("<KP_Enter>", lambda e: self._ok())
        top.bind("<Escape>", lambda e: self._cancel())

        top.update_idletasks()
        w, h = top.winfo_width(), top.winfo_height()
        sw, sh = top.winfo_screenwidth(), top.winfo_screenheight()
        top.geometry(f"+{max(0, (sw - w) // 2)}+{max(0, (sh - h) // 3)}")
        try:
            top.grab_set()
        except tk.TclError:
            pass
        top.focus_force()

    def _sync_auto_close(self) -> None:
        now = bool(self._auto_var.get())
        self._auto_close.set(now)
        self.auto_close_changed = (now != self._before)

    def _minimize(self) -> None:
        """
        **一键最小化**选中的闲置程序（B.2）。

        为什么要有这个（而不是只有"关闭"）：很多"开了很久没用"的程序**不该关**
        —— 里面有未保存的东西、是后台要常驻的、或者只是想先把桌面收干净。
        最小化是**可逆**的，比关闭温和得多，所以给用户这个中间选项。

        结果同样**回读核对**：每个窗口最小化后再问一次系统"它现在是不是最小化了"，
        没成功就如实说（不假装成功）。
        """
        self._sync_auto_close()
        picked = [r for r in self._rows if self.vars[int(r["pid"])].get()]
        self.result = None              # 最小化不算"关闭"，别把结果当成关闭清单
        done, failed = 0, []
        for row in picked:
            hwnd = int(row.get("hwnd") or 0)
            label = row.get("name") or f"PID {row.get('pid')}"
            if not hwnd:
                failed.append(f"{label}（没有窗口句柄）")
                continue
            ok, msg = core.minimize_window(hwnd)
            if ok and core.window_is_minimized(hwnd):
                done += 1
            else:
                failed.append(f"{label}（{msg}）")
        self.minimize_result = (done, failed)
        try:
            self.top.destroy()
        except Exception:
            pass

    def _ok(self) -> None:
        self._sync_auto_close()
        self.result = [r for r in self._rows if self.vars[int(r["pid"])].get()]
        self.top.destroy()

    def _cancel(self) -> None:
        self._sync_auto_close()
        self.result = None
        self.top.destroy()

    def show(self) -> list[dict] | None:
        self.top.wait_window()
        return self.result
class ConfirmDialog:
    """
    确认弹窗：问"是否关闭当前视频"。

    按键约定（按需求定制）：
      · Space  → 确认（等同 Yes）
      · Enter  → 确认（也当"确定"用，符合直觉）
      · Esc    → 取消（等同 No）
      · 鼠标   → 点 Yes 或 No 均可
    窗口置顶并居中，避免被哔哩哔哩窗口挡住而"找不到弹窗"。
    """

    def __init__(self, parent: tk.Misc, title: str, message: str,
                 detail: str = "", yes_text: str = "是，关闭 (Space)",
                 no_text: str = "否，继续播放 (Esc)") -> None:
        self.result = False
        self._closed = False

        top = tk.Toplevel(parent)
        self.top = top
        top.title(title)
        top.configure(bg="#1b2027")
        top.attributes("-topmost", True)
        top.resizable(False, False)
        try:
            top.iconify()
            top.deiconify()
        except tk.TclError:
            pass

        # 容器用 ttk.Frame（它支持 padding 但没有 bg），
        # 里面的文字用 tk.Label（它才有 bg/fg）—— 这样既是"深色卡片"效果又不会报错。
        wrap = ttk.Frame(top, padding=22)
        wrap.pack(fill="both", expand=True)

        # 注意：这里必须用 tk.Label 而不是 ttk.Label。
        # ttk.Label 不支持 background 选项，深色背景不会生效，
        # 结果就是"深色文字压在黑色底上"完全看不清（实测踩到过）。
        tk.Label(wrap, text=message, font=("Microsoft YaHei UI", 15, "bold"),
                 bg="#1b2027", fg="#eaf2f8", anchor="w").pack(anchor="w")
        if detail:
            tk.Label(wrap, text=detail, font=("Microsoft YaHei UI", 10),
                     bg="#1b2027", fg="#c9d9e6", justify="left", anchor="w",
                     wraplength=640).pack(anchor="w", pady=(12, 0))

        btns = ttk.Frame(wrap)
        btns.pack(fill="x", pady=(18, 0))
        self.btn_yes = ttk.Button(btns, text=yes_text, width=24, command=self._yes)
        self.btn_yes.pack(side="left", padx=(0, 12))
        self.btn_no = ttk.Button(btns, text=no_text, width=24, command=self._no)
        self.btn_no.pack(side="left")

        tk.Label(wrap, text="快捷键：Space / Enter = 关闭视频　　Esc = 继续播放　（鼠标点按钮同样有效）",
                 font=("Microsoft YaHei UI", 9), bg="#1b2027", fg="#a9bfd2",
                 anchor="w").pack(anchor="w", pady=(14, 0))

        # 按键绑定：Space 与 Enter 都确认，Esc 取消
        for seq in ("<space>", "<Return>", "<KP_Enter>"):
            top.bind(seq, lambda e: self._yes())
        top.bind("<Escape>", lambda e: self._no())
        self.btn_yes.bind("<space>", lambda e: self._yes())
        self.btn_no.bind("<space>", lambda e: self._yes())

        # 居中 + 抢焦点
        top.update_idletasks()
        w, h = top.winfo_width(), top.winfo_height()
        sw, sh = top.winfo_screenwidth(), top.winfo_screenheight()
        top.geometry(f"+{max(0, (sw - w) // 2)}+{max(0, (sh - h) // 3)}")
        try:
            top.grab_set()
        except tk.TclError:
            pass
        top.focus_force()
        self.btn_yes.focus_set()

    def _yes(self) -> None:
        if self._closed:
            return                          # 已关闭，忽略迟到的键盘事件
        self.result = True
        self._close()

    def _no(self) -> None:
        if self._closed:
            return
        self.result = False
        self._close()

    def _close(self) -> None:
        self._closed = True
        try:
            self.top.grab_release()
        except tk.TclError:
            pass
        self.top.destroy()

    def show(self) -> bool:
        """模态等待用户选择，返回 True 表示确认（关闭视频）。"""
        self.top.wait_window()
        return self.result


# ============================================================================
# 五、主窗口
# ============================================================================

class WinMonApp:
    """主窗口。"""

    # CPU 需要两次采样求差。采样要逐个进程调 GetProcessTimes，进程多时开销可观，
    # 因此限流：只对界面上看得见的进程采样，且两次采样至少间隔 1 秒。
    CPU_SAMPLE_MIN_INTERVAL = 1.0
    # 抓屏在后台线程，上屏必须在主线程（tkinter 非线程安全）。
    # 每次最多处理 MAX_DRAIN 帧，保证界面事件不会被连续上屏饿死。
    MAX_DRAIN = 8

    def _dpi_scale(self) -> float:
        """
        本进程看到的 DPI 缩放比例（96 DPI = 1.0）。

        用 Tk 自己的 `winfo_fpixels("1i")`：它读的就是系统给这个进程的 DPI
        （`enable_dpi_awareness()` 之后与真实渲染一致；实测本机 143.86 → 1.50）。
        读不到就按 1.0。
        """
        try:
            return dpi_scale_from(self.root.winfo_fpixels("1i"))
        except Exception:
            return 1.0

    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        root.title(APP_TITLE)
        # 界面尺寸 = 设计稿 × DPI 缩放（理由见 `scaled_window_size`：DPI 感知的进程里
        # 字体是 1.5 倍，窗口不跟着放大就真有按钮被挤到看不见）。
        self.dpi_scale = self._dpi_scale()
        try:
            win_w, win_h = scaled_window_size(
                self.dpi_scale,
                screen=(root.winfo_screenwidth(), root.winfo_screenheight()))
        except Exception:                                  # pragma: no cover
            win_w, win_h = APP_W, APP_H
        root.geometry(f"{win_w}x{win_h}")
        root.minsize(int(APP_MIN_W * self.dpi_scale), int(APP_MIN_H * self.dpi_scale))
        # P0-2 修复：不绑 WM_DELETE_WINDOW，点 × 走 Tk 默认 destroy，
        # on_close/quit_app 永不执行 → 托盘图标残留、常驻 PowerShell 助手成孤儿、
        # 退出前 800ms 防抖窗口内的设置改动丢失。
        root.protocol("WM_DELETE_WINDOW", self.on_close)

        # ---- 状态 ----
        self.windows: list[core.WinInfo] = []
        self.proc_map: dict[int, dict] = {}
        self.win_by_hwnd: dict[int, core.WinInfo] = {}
        self.row_of_hwnd: dict[int, str] = {}
        self.row_info: dict[str, tuple[str, int, int]] = {}
        #: 分组行 item id → 那个程序名。用来**在重建列表时还原"哪些分组是展开的"**：
        #: Tk 每次 insert 都生成新的 item id（删掉就再也对不上），所以只能用
        #: "程序名"这种稳定的东西当键（2026-10-13 修的真 bug：列表每 2 秒重建一次，
        #: 展开的分组当场被折叠，子窗口行根本点不到）。
        self._group_app_of: dict[str, str] = {}
        self.selected_hwnd: int | None = None
        self.selected_pid: int | None = None
        self.preview_key: int | None = None
        self.gallery_keys: dict[int, str] = {}
        self.gallery_order: list[int] = []
        self.paused = False
        self.follow_selection = tk.BooleanVar(value=True)
        self.only_windowed = tk.BooleanVar(value=True)
        self.show_no_window = tk.BooleanVar(value=False)
        self.show_hidden = tk.BooleanVar(value=False)
        self.live_gallery = tk.BooleanVar(value=True)
        self.size_choice = tk.StringVar(value=PREVIEW_SIZES[1][0])
        self.fps_choice = tk.StringVar(value="很流畅 10 FPS")
        self.search_text = tk.StringVar()
        self.status_text = tk.StringVar(value="正在初始化…")
        self.overview_text = tk.StringVar(value="")
        # 视频联动设置（变量集中在 _init_video_vars，方便"手工搭 app"的场景复用）
        self._init_video_vars()

        # CPU 采样与路径缓存
        self._cpu_prev: dict[int, float] = {}
        self._cpu_prev_t = time.monotonic()
        self._cpu_pct: dict[int, float] = {}
        self._path_cache: dict[int, str] = {}

        # 图标 / 前台 / 历史 / 设置 / 性能统计
        self.icons = IconProvider(ICON_SIZE)
        self.history = core.ActionHistory()
        # 程序目录的用户偏好（收藏 / 最近使用 / 手动分类）。
        # 它自己的内容会一起写进 winmon_settings.json，所以备份/清理只有一处。
        self.program_prefs = core.CatalogPrefs()
        self._fg_hwnd = 0
        #: 窗口操作（激活/最小化）的后台队列与线程；
        #: `_win_op_msg` 是后台线程交回主线程的结果队列（不跨线程调 Tk）
        self._win_op_q: queue.Queue = queue.Queue()
        self._win_op_msg: queue.Queue = queue.Queue()
        #: 媒体会话结果的专用队列（**不能塞进 `_win_op_msg`**：那里是两元组协议）
        self._media_msg: queue.Queue = queue.Queue()
        self._win_op_thread = None
        # 设置文件位置：默认在本程序目录下，可用 WINMON_SETTINGS 环境变量覆盖
        # （给测试用，避免动到用户真实偏好）
        self._settings_path = _settings_path()
        self._settings_after_id = None
        #: 设置文件读不出来时的原因与备份路径（由 `_startup_status` 如实上报，
        #: 绝不静默按默认值跑完再把默认值写回去 —— 见 `load_settings`）
        self._settings_load_error = ""
        self._settings_backup = ""
        #: 工作线程 → 主线程的界面动作队列（见 `_ui_call`：worker 一律不碰 tkinter）
        self._ui_msg: queue.Queue = queue.Queue()
        self._fps_window: list[float] = []
        self._pump_counter = 0
        self._last_capture_cost = 0.0
        self._close_watch: list[tuple[int, int, float, bool]] = []
        self._rebuilding = False
        self._last_preview_req: tuple | None = None
        self._tree_pid_of: dict = {}
        self._tree_sig: tuple | None = None
        # 视频联动状态
        self.video_target: core.WinInfo | None = None
        self.video_watch = False
        self._video_dialog_open = False
        self._video_after_id = None

        # AI 工作伴侣（阶段 1.5）：前台累计 + 本地事件。只读、不联网、不花 token。
        self._ai_enabled = True
        self._ai_pin = os.environ.get("WINMON_AI_PIN", "")
        self._ai_tracker = ai.AiTracker()
        self._ai_incidents = ai.IncidentStore(path=ai.incidents_path())
        self._ai_primary = None
        self._ai_sessions = None
        self._ai_usage_window = None
        self._ai_assistant_window = None
        # AI 用量的界面缓存（2 秒一拍，不必每拍都扫账本）
        self._ai_budget = ai.Budget()
        self._ai_usage_cache = (0.0, None)
        # 直连 AI 助手：默认关闭；开启后会联网并产生**真实** token 费用
        self._ai_assist = ai.load_assist()
        self._ai_jobs: list = []
        self._ai_busy = False
        self._ai_log_lines: list = []
        self._ai_last_handoff: dict = {}
        self._ai_last_watch = time.time()
        self._ai_watch_sig = ""
        self._ai_explained: dict = {}
        # 测试里把 WINMON_AI_NOTIFY 设为 0，避免弹系统通知打扰
        self._ai_notify_enabled = os.environ.get("WINMON_AI_NOTIFY", "1") not in (
            "", "0", "false", "False")

        self.worker = core.CaptureWorker()
        self.worker.start()

        self._build_ui()
        self._bind_events()
        self.load_settings()
        # 读完设置再刷一次媒体应用清单：这样"上次选的那个应用"也在下拉框的值里
        # （否则界面上显示的是一个不在清单里的项，点确定会被重置成"自动"）。
        self._reload_media_app_list()
        # 操作历史日志按设置里的保留天数清一次（默认 30 天）——
        # 放在启动时做，用户不用记着去点"清理"。
        self._prune_history_on_start()
        # 起**常驻**媒体会话助手：它每 3 秒把快照写进文件，之后刷新媒体那一行只是读文件
        # （实测读快照 0.05 ms，而"每次起一个 PowerShell"要约 290 ms）。
        # 失败不影响启动：`collect_media_sessions()` 会自动退回一次性调用。
        try:
            core.start_media_snapshot_helper()
        except Exception:
            pass
        # 旧配置迁移要不要报（C.2）：等界面和状态行都就绪了再说
        try:
            self.root.after(400, self._report_settings_migration)
        except Exception:
            pass
        # 托盘**启动就绪**（约定"点 × 隐藏到托盘"要零延迟）：
        # 在这里就把图标放进通知区域，而不是等到用户第一次点 × 才创建。
        # 失败不影响主流程（只是"关闭到托盘"会退回结束进程，见 on_close）。
        self._setup_tray()

        self._fg_hwnd = core.get_foreground_hwnd()
        self.refresh_processes()
        self.refresh_windows()
        self.root.after(30, self._pump_frames)
        self.root.after(100, self._tick_close_watch)
        self.root.after(250, self._poll_foreground)
        self.root.after(1200, self._periodic_refresh)
        self.root.after(300, lambda: self._set_status(self._startup_status()))
        if self.start_minimized.get() and self._tray_ok:
            # 勾了「启动时直接收进托盘」：直接 withdraw（调用前提是 _tray_ok 已验证、
            # 图标已在托盘，双击图标即可恢复 —— 原注释与实现相反，此处改注释不改代码）
            self.root.after(600, self._start_hidden_to_tray)

    def _startup_status(self) -> str:
        """
        启动时状态栏那一句。

        除了"就绪"，还负责**如实说明**两件用户数据相关的事（都不许静默）：
          · 设置里选过的媒体应用这次在清单里找不到了（程序被卸载/改名）——
            我们会把选择退回「自动」，不吭声的话用户只会觉得"我选好的播放器
            怎么又变自动了"；
          · **设置文件读不出来**（坏 JSON / 权限）—— 这时界面按默认值跑，
            退出时的自动保存还会把默认值写回去，必须告诉用户备份在哪。
        """
        text = f"{APP_NAME} 就绪 · 纯 ctypes 捕获 · 选中程序即可看到实时画面"
        dropped = [x for x in (getattr(self, "_media_pin_dropped", None) or []) if x]
        if dropped:
            text += f"　｜　媒体应用「{'、'.join(dropped)}」这次没找到，已退回「自动」"
        err = getattr(self, "_settings_load_error", "")
        if err:
            backup = os.path.basename(getattr(self, "_settings_backup", "") or "")
            where = f"（原文件已备份为 {backup}）" if backup else "（备份失败，请先别关程序）"
            text += f"　｜　设置文件读不出来，本次按默认值运行{where}：{err}"
        return text

    # ------------------------------------------------------------------
    # 界面构建
    # ------------------------------------------------------------------
    def _init_video_vars(self) -> None:
        """
        视频联动 / 媒体 / 闲置 / 关闭行为相关的界面变量集中在这里创建。

        为什么单独一个方法：本项目有好几处"手工搭一个 app"用来测界面
        （`test_a5` 的布局检查、`test_b2` 的设置往返、`tests/shot.py` 截图），
        它们都得把用到的变量补上。以前是各自照抄一份，**新增一个变量就漏一处**
        （当时为此踩过两次：`video_source_pin`、`video_only_apps`）。
        现在统一调这个方法，加变量只需改这里。
        """
        #: 每次播放检测的采样时长（秒）—— 「监视当前窗口」用它采一段画面看有没有在动
        self.video_probe_seconds = tk.DoubleVar(value=3.0)
        #: 关闭方式：默认「关闭当前窗口」（WM_CLOSE，不依赖前台/键盘注入）。
        #: 注意这里存的是**中文标签**（给用户看的），写设置时转成 slug 存。
        #: **保留**：用户只砍掉了"自动打开视频"，关闭方式仍然要选。
        self.video_close_mode = tk.StringVar(value=_close_mode_label(DEFAULT_VIDEO_CLOSE_MODE))
        # ---- 关闭行为 + 托盘（设置里的那两项）----
        #: `tray` = 点 × 隐藏到托盘；`exit` = 点 × 结束进程（**默认**）
        self.close_mode = tk.StringVar(value=CLOSE_MODE_DEFAULT)
        #: 启动时直接把窗口收进托盘（默认关：首次运行应该看得见界面）
        self.start_minimized = tk.BooleanVar(value=False)
        #: 操作历史日志保留天数（**默认 30 天**，另有 7 / 90 天）——
        #: 启动时按这个把过期记录从日志里删掉，见 `core.prune_history_log()`
        self.history_days = tk.IntVar(value=HISTORY_DAYS_DEFAULT)
        #: 实时总览的**排列顺序**（B.3）：存**窗口标题**而不是 hwnd（hwnd 重启就变）
        self.gallery_order_saved: list[str] = []
        #: 托盘图标 / 托盘消息窗口 / 是否正在轮询托盘消息 / 托盘是否真的可用
        self._tray = None
        self._tray_window = None
        self._tray_polling = False
        #: 托盘轮询的 after id（只允许一条轮询链，见 `_schedule_tray_poll`）
        self._tray_after_id = None
        self._tray_ok = False
        # ---- 闲置程序（"开着不用、白耗电"）----
        # 和上面同样的理由放在这里：所有"手工搭 app"的测试都调 `_init_video_vars()`
        # 来补齐界面变量，新变量加在这一处就不会再漏。
        #: 功能总开关，**默认关**（约定）
        self.idle_alert = tk.BooleanVar(value=False)
        #: 多久算"长期不用"，界面显示中文标签（默认 30 分钟）
        self.idle_choice = tk.StringVar(
            value=core.idle_threshold_label(core.IDLE_DEFAULT_SECONDS))
        #: 到点后是否直接关闭；默认**不关**，只提醒（约定）
        self.idle_auto_close = tk.BooleanVar(value=False)
        #: 闲置计时器（纯逻辑对象，创建它不碰 Tk 也不碰 Win32）
        self.idle_watch = core.IdleWatcher(core.IDLE_DEFAULT_SECONDS)
        #: 已经累计了几拍（用来判断"关掉功能时要清零"）
        self._idle_ticks = 0
        self._idle_dialog_open = False
        #: 提醒过一次之后隔多久才允许**自动**再弹窗（手动点清单不受限制）
        self.idle_snooze_choice = tk.StringVar(
            value=core.idle_snooze_label(core.IDLE_SNOOZE_DEFAULT))
        # ---- 媒体会话（正在播放 / 指定媒体应用 / 自动唤醒）----
        #: 指定哪个媒体应用（下拉框里显示的是中文标签；存设置时存成一个稳定的文本，
        #: 见 `save_settings_now` 里的 `_media_app_persist`）
        self.media_app_pin = tk.StringVar(value=MEDIA_APP_ANY)
        #: 指定的应用没在跑时，是否自动把它唤醒（**默认开**，明确要"无需前置启动"）
        self.media_auto_wake = tk.BooleanVar(value=True)
        #: 上一次读到的会话状态（给"播放/暂停"按钮决定发哪一条命令）
        self._media_playing = False
        self._media_found = False
        #: 上一次选中的会话（失败时的提示要用）
        self._media_last_pick: dict = {}
        #: 后台是否正在读媒体会话（避免叠好几条 PowerShell）
        self._media_busy = False
        #: 上次读媒体会话的时刻（它要起 PowerShell，约 290ms，所以比刷新慢一拍）
        self._media_last = 0.0
        #: 看门狗：上次检查常驻媒体助手的时刻（墙钟秒，见 _periodic_refresh）
        self._media_helper_check = 0.0
        #: 下拉框文本 → MediaApp（由 `_reload_media_app_list` 填）
        self._media_apps: dict = {}
        #: 上一次读到的"有会话的 app id"（`core.session_app_ids()`）。它一变就重算
        #: 下拉框清单 —— 这就是"新打开的播放器自动进清单"（2026-10-16 新增）。
        self._media_session_apps: list[str] = []
        # ---- 工作区快照 ----
        #: `[{name, saved_at, entries:[{name,title,rect,iconic}]}]`，存在设置文件里
        self.workspaces: list[dict] = []
        #: 应用快照时是否顺手把不在快照里的窗口最小化（默认关）
        self.ws_minimize_others = tk.BooleanVar(value=False)

    def _build_ui(self) -> None:
        style = ttk.Style()
        try:
            style.theme_use("vista")
        except tk.TclError:
            pass
        style.configure("Treeview", font=UI_FONT, rowheight=ICON_SIZE + 6)
        style.configure("Treeview.Heading", font=("Microsoft YaHei UI", 9, "bold"))
        style.configure("TNotebook.Tab", font=UI_FONT, padding=(14, 6))
        style.configure("TButton", font=UI_FONT)

        # ---- 顶部工具栏（第一行：搜索 + 显示过滤 + 总览动作） ----
        #
        # 布局踩过的坑（实测）：把 12 个控件全塞进一行时，这一行的自然宽度约 1420px，
        # 超过 1280 的窗口宽度。Tk 不会把控件推出屏幕，而是**压缩**它们，
        # 结果「移除选中总览」被压成「移除」、「加入总览」被压到 1px 彻底看不见 ——
        # 而它们的右边缘仍在窗口内，所以"越界"类检查完全发现不了。
        # 现在按功能拆成两行，并保证每行的自然宽度都小于 1280。
        # tests/test_a5_gui_layout_safety.py 里的 `_squeezed()` 会守住这条约定。
        bar = ttk.Frame(self.root, padding=(8, 6))
        bar.pack(fill="x")
        ttk.Label(bar, text="搜索：", font=UI_FONT).pack(side="left")
        ent = ttk.Entry(bar, textvariable=self.search_text, width=20, font=UI_FONT)
        ent.pack(side="left", padx=(0, 8))
        ent.bind("<KeyRelease>", lambda e: self.refresh_windows())

        ttk.Checkbutton(bar, text="仅显示有窗口", variable=self.only_windowed,
                        command=self.refresh_windows).pack(side="left", padx=2)
        ttk.Checkbutton(bar, text="显示隐藏窗口", variable=self.show_hidden,
                        command=self.refresh_windows).pack(side="left", padx=2)
        ttk.Checkbutton(bar, text="显示无窗口进程", variable=self.show_no_window,
                        command=self.refresh_windows).pack(side="left", padx=2)
        #: 操作历史现在是一个**独立窗口**（原来那个折叠面板会跟主体抢纵向空间）
        ttk.Button(bar, text="操作历史…", width=11,
                   command=self.action_history).pack(side="left", padx=2)

        # 总览动作靠右排。注意 Tk 的 pack：**先 pack 的 side="right" 最靠右**，
        # 所以下面按"从右到左"的视觉顺序依次 pack。
        ttk.Button(bar, text="刷新列表", width=9,
                   command=self.refresh_all).pack(side="right", padx=2)
        #: 独立设置界面（约定）：开机自启动 + 关闭行为
        ttk.Button(bar, text="设置…", width=8,
                   command=self.action_settings).pack(side="right", padx=2)
        ttk.Button(bar, text="移除选中总览", width=13,
                   command=self.gallery_remove_selected).pack(side="right", padx=2)
        ttk.Button(bar, text="加入总览", width=9,
                   command=self.gallery_add_selected).pack(side="right", padx=2)

        # ---- 顶部工具栏（第二行：画面尺寸 / 刷新率 / 跟随选择 / 总览刷新）----
        bar2 = ttk.Frame(self.root, padding=(8, 0))
        bar2.pack(fill="x")
        ttk.Label(bar2, text="画面：", font=UI_FONT).pack(side="left")
        ttk.Combobox(bar2, textvariable=self.size_choice, state="readonly", width=13,
                     values=[s[0] for s in PREVIEW_SIZES], font=UI_FONT).pack(side="left", padx=2)
        ttk.Label(bar2, text="刷新率：", font=UI_FONT).pack(side="left", padx=(8, 0))
        ttk.Combobox(bar2, textvariable=self.fps_choice, state="readonly", width=14,
                     values=[f[0] for f in FPS_CHOICES], font=UI_FONT).pack(side="left", padx=2)
        ttk.Checkbutton(bar2, text="跟随选择", variable=self.follow_selection).pack(
            side="left", padx=(10, 2))
        ttk.Checkbutton(bar2, text="总览实时刷新", variable=self.live_gallery).pack(
            side="left", padx=2)
        ttk.Label(bar2, text="（画面与刷新率只影响预览，越低越省电）", font=UI_FONT,
                  foreground="#888").pack(side="left", padx=(10, 0))

        # ---- 视频联动 + 媒体（合成工具栏里的一行；拆成独立方法，免得 _build_ui 过长）----
        self._build_video_bars()
        # ---- AI 状态条（识别到的 AI + 真实/估算用量 + 直连模型状态）----
        self._build_ai_bar()

        for var in (self.show_hidden, self.show_no_window, self.only_windowed,
                    self.follow_selection, self.live_gallery, self.size_choice, self.fps_choice):
            var.trace_add("write", self._schedule_save_settings)
        for var in (self.video_close_mode, self.media_app_pin, self.media_auto_wake,
                    self.close_mode, self.start_minimized, self.history_days):
            var.trace_add("write", self._schedule_save_settings)

        # ---- 主体：左列表 + 右预览（拆成独立方法，免得 _build_ui 过长）----
        # 用一层容器 + grid 权重放 Panedwindow：这样展开「操作历史」时，
        # **多出来的高度从主体里扣**，而不是让历史面板自己被压扁
        # （原来直接 pack(body, expand=True)，历史面板只剩 12px —— 勾了等于没勾）。
        body_holder = ttk.Frame(self.root)
        body_holder.pack(fill="both", expand=True)
        body_holder.rowconfigure(0, weight=1)
        body_holder.columnconfigure(0, weight=1)
        body = ttk.Panedwindow(body_holder, orient="horizontal")
        body.grid(row=0, column=0, sticky="nsew", padx=8, pady=(0, 4))
        self._body_holder = body_holder

        left = ttk.Frame(body)
        body.add(left, weight=4)
        self._build_left_pane(left)

        right = ttk.Frame(body)
        body.add(right, weight=6)
        right.rowconfigure(0, weight=1)
        right.columnconfigure(0, weight=1)

        self.notebook = ttk.Notebook(right)
        self.notebook.grid(row=0, column=0, sticky="nsew")
        self.preview = PreviewPane(self.notebook, self)
        self.gallery = GalleryPane(self.notebook, self)
        self.proc_tree = ProcessTreePane(self.notebook, self)
        self.catalog = ProgramCatalogPane(self.notebook, self)
        self.workspace = WorkspacePane(self.notebook, self)
        self.notebook.add(self.preview.frame, text="实时预览")
        self.notebook.add(self.gallery.frame, text="实时总览")
        self.notebook.add(self.proc_tree.frame, text="进程树")
        self.notebook.add(self.catalog.frame, text="程序目录")
        self.notebook.add(self.workspace.frame, text="工作区")
        self.workspace.refresh()

        # ---- 底部状态栏 ----
        bottom = ttk.Frame(self.root, padding=(8, 3))
        bottom.pack(fill="x")
        ttk.Label(bottom, textvariable=self.status_text, font=UI_FONT, anchor="w").pack(
            side="left", fill="x", expand=True)
        ttk.Label(bottom, textvariable=self.overview_text, font=MONO_FONT,
                  foreground="#555").pack(side="right")

        # ---- 操作历史：**独立窗口**（约定），主界面不再有折叠面板 ----
        self._history_window = None
        self.history_hint = tk.StringVar(value="")
        ttk.Label(self.root, textvariable=self.history_hint, font=("Microsoft YaHei UI", 8),
                  foreground="#5a6b7b", anchor="w").pack(fill="x", padx=10)

        # ---- 右键菜单 ----
        self.menu = tk.Menu(self.root, tearoff=0, font=UI_FONT)
        self.menu.add_command(label="显示实时画面", command=self.action_show_preview)
        self.menu.add_command(label="加入实时总览", command=self.gallery_add_selected)
        self.menu.add_separator()
        #: 手势表（用户 2026-10-11 定）：单击只选中；双击最小化/还原；
        #: Enter / 中键 / 菜单切到最前。菜单里逐条写清楚，免得只能靠猜。
        self.menu.add_command(label="切换到前台（Enter / 中键 / 双击预览区）",
                              command=self.action_bring_front)
        self.menu.add_command(label="最小化 / 还原（双击列表 或 M 键）",
                              command=self.action_toggle_selected)
        self.menu.add_command(label="最小化选中的全部（Shift+M）",
                              command=self.action_minimize_selected)
        self.menu.add_command(label="还原选中的全部",
                              command=self.action_restore_selected)
        self.menu.add_separator()
        self.menu.add_command(label="关闭窗口（优雅）", command=self.action_close_window)
        self.menu.add_command(label="结束进程（强制）", command=self.action_terminate)
        self.menu.add_command(label="结束进程树（强制）", command=self.action_terminate_tree)
        self.menu.add_separator()
        self.menu.add_command(label="打开文件所在位置", command=self.action_reveal)
        self.menu.add_separator()
        self.menu.add_command(label="闲置程序清理…", command=self.action_idle_dialog)
        self.menu.add_command(label="清理程序文件…", command=self.action_clean_files)
        self.menu.add_command(label="复制 PID",
                              command=lambda: self._copy(str(self.selected_pid or "")))
        self.menu.add_command(label="复制窗口标题", command=self._copy_selected_title)
        self.menu.add_command(label="复制程序路径", command=self._copy_selected_path)

    def _build_left_pane(self, left: tk.Misc) -> None:
        """
        左栏：窗口列表 + 滚动条 + 手势说明/闲置/媒体那几行。

        为什么单独一个方法：全塞进 `_build_ui` 会把它顶过 200 行，
        而 `lint_check.py` 有一条"函数过长"的检查守着这个阈值（当时真报过一次红）。
        """
        columns = ("act", "app", "title", "pid", "mem", "cpu", "state")
        tv = ttk.Treeview(left, columns=columns, show="tree headings",
                          selectmode="extended")
        self.tree = tv
        tv.heading("#0", text="图标")
        tv.heading("act", text="活动")
        tv.heading("app", text="程序")
        tv.heading("title", text="窗口标题")
        tv.heading("pid", text="PID")
        tv.heading("mem", text="内存 工作集/私有")
        tv.heading("cpu", text="CPU")
        tv.heading("state", text="状态")
        #: 列宽的账（实测）：左栏的**请求宽度**决定中间分隔条的位置，右栏拿"剩下的宽度"。
        #: 右栏「程序目录」的动作行需要约 574px，所以左栏请求宽度必须压在约 745px 以内 ——
        #: 这七列加起来 738 是刻意的余量，`tests/test_a5_gui_layout_safety.py` 的
        #: `_squeezed()` 会守住它（当时就因为多加一列把右栏挤坏过一次）。
        tv.column("#0", width=40, stretch=False, minwidth=40)
        tv.column("act", width=44, anchor="center", stretch=False)
        tv.column("app", width=158, anchor="w")
        tv.column("title", width=210, anchor="w")
        tv.column("pid", width=60, anchor="e", stretch=False)
        tv.column("mem", width=98, anchor="e", stretch=False)
        tv.column("cpu", width=54, anchor="e", stretch=False)
        tv.column("state", width=64, anchor="center", stretch=False)

        vs = ttk.Scrollbar(left, orient="vertical", command=tv.yview)
        hs = ttk.Scrollbar(left, orient="horizontal", command=tv.xview)
        tv.configure(yscrollcommand=vs.set, xscrollcommand=hs.set)
        tv.grid(row=0, column=0, sticky="nsew")
        vs.grid(row=0, column=1, sticky="ns")
        hs.grid(row=1, column=0, sticky="ew")
        left.rowconfigure(0, weight=1)
        left.columnconfigure(0, weight=1)
        self._build_left_extras(left)

        tv.tag_configure("noresp", foreground="#c0392b")
        tv.tag_configure("minimized", foreground="#7f8c8d")
        tv.tag_configure("hidden", foreground="#a0a8b0")
        tv.tag_configure("critical", foreground="#8e44ad")
        tv.tag_configure("nowin", foreground="#95a5a6")
        tv.tag_configure("fg", background="#d6ecff")          # 前台窗口高亮
        tv.tag_configure("fg_hidden", background="#eef5fa", foreground="#a0a8b0")

    def _build_left_extras(self, left: tk.Misc) -> None:
        """
        左栏列表下面的两行：手势说明 + 闲置程序开关。

        为什么单独一个方法：塞进 `_build_ui` 会把它顶过 200 行，
        而 `lint_check.py` 有一条"函数过长"的检查守着这个阈值。
        也顺手把"手势规则"和"闲置功能入口"放在一起 —— 它们都是"列表怎么用"的一部分。
        """
        #: 手势说明常驻在列表下面。为什么不做成"鼠标悬停提示"：
        #: 这版把"单击"从"切到最前"改成了"只选中"，**规则变了就得让人看得见**，
        #: 常驻一行字比需要探索才发现的气泡更稳（也少一个自绘 Toplevel 的坑）。
        tk.Label(left, text="单击：选中并预览　双击：最小化/还原　"
                            "Enter/中键：切到最前　悬停看详情　右键：更多",
                 font=("Microsoft YaHei UI", 8), fg="#5a6b7b",
                 anchor="w").grid(row=2, column=0, sticky="ew", pady=(2, 0))

        # ---- 闲置程序开关（**默认关闭**；本工具的职责是"看着"，不是"替你动手"）----
        idle_bar = ttk.Frame(left)
        idle_bar.grid(row=3, column=0, sticky="ew", pady=(3, 0))
        ttk.Checkbutton(idle_bar, text="闲置超时提醒", variable=self.idle_alert,
                        command=self._on_idle_toggle).pack(side="left")
        ttk.Combobox(idle_bar, textvariable=self.idle_choice, state="readonly", width=8,
                     font=UI_FONT,
                     values=[label for label, _s in core.IDLE_CHECK_CHOICES]).pack(
            side="left", padx=(6, 4))
        #: 提醒节流：提醒过一次之后隔多久才允许**自动**再弹窗（手动点清单不受限制）。
        #: 宽度 13 是实测值：最长的条目「本次运行只提醒一次」要 108px，
        #: 而 `Microsoft YaHei UI 9` 下 width=12 只有 107px —— 会差 1px 显示不下，
        #: 所以别为了省地方把它调小（a5 的挤压检查在 1080 宽下仍然全绿）。
        ttk.Label(idle_bar, text="提醒间隔", font=("Microsoft YaHei UI", 8),
                  foreground="#5a6b7b").pack(side="left", padx=(6, 2))
        ttk.Combobox(idle_bar, textvariable=self.idle_snooze_choice, state="readonly",
                     width=13, font=UI_FONT,
                     values=[label for label, _s in core.IDLE_SNOOZE_CHOICES]).pack(
            side="left", padx=(0, 4))
        ttk.Button(idle_bar, text="闲置清单", width=9,
                   command=self.action_idle_dialog).pack(side="left")

    def _build_video_bars(self) -> None:
        """
        **视频联动一行 + 媒体独立一行**（用户 2026-10-12 的要求）。

        原始需求：
          · 「工具栏拥挤，媒体独立占一行，唤醒和媒体键重合，两者结合」；
          · 「删去正在播放的板块，不再识别播放进度」。

        之前把视频与媒体硬塞进同一行，结果两边都挤（媒体键的最后一排按钮被裁掉）。
        现在：
          · 第 1 行「视频联动：监视当前窗口 停止监视 直接关闭视频」—— 只有三个按钮，宽松；
          · 第 2 行「媒体：<曲名·状态> [媒体应用▾] ☑自动唤醒 [播放/暂停] ▶▶ ◀◀」——
            媒体独占一行。

        **"唤醒"与"媒体键"合并**（原来重复，见用户第 1 条）：`播放/暂停` 现在是一个按钮，
        点下去的顺序是：
          1. 目标应用没在跑 → 先唤醒它（仅当勾了「自动唤醒」）；
          2. 把命令**按应用**发给它；
          3. 它没有媒体会话（比如网易云：进程在跑但不发布会话）→ **退回全局媒体键**。
        所以不需要再摆一排"全局媒体键"和一个单独的"启动/唤醒"—— 一条路走到底，
        状态栏会说明这一步实际用了哪一种。上一首/下一首用「◀◀ / ▶▶」（同样按上面的顺序）。
        """
        vbar = ttk.Frame(self.root, padding=(8, 2))
        vbar.pack(fill="x")
        ttk.Label(vbar, text="视频联动：", font=BOLD_FONT).pack(side="left")
        # 主按钮：**监视当前窗口** —— 不依赖任何特定 App，也不需要"打开"什么，
        # 直接判断列表里选中的那个窗口是不是在放视频（见 _monitor_current_window）。
        ttk.Button(vbar, text="监视当前窗口", width=13,
                   command=self._monitor_current_window).pack(side="left", padx=(0, 4))
        ttk.Button(vbar, text="停止监视", width=9,
                   command=self.stop_video_watch).pack(side="left", padx=2)
        ttk.Button(vbar, text="直接关闭视频", width=12,
                   command=self.action_stop_video).pack(side="left", padx=2)
        ttk.Label(vbar, text="（监视后检测到播放会先问你；Space=关闭　Esc=继续）",
                  font=UI_FONT, foreground="#6b7c8c").pack(side="left", padx=(10, 0))

        # ---- 第 2 行：媒体独占一行 ----
        mbar = ttk.Frame(self.root, padding=(8, 2))
        mbar.pack(fill="x", pady=(0, 4))
        ttk.Label(mbar, text="媒体：", font=BOLD_FONT).pack(side="left")
        #: 当前在放什么（曲名 · 歌手 · 状态）。**没有进度** —— 用户已放弃识别进度。
        self.media_text = tk.StringVar(value="（还没读取）")
        #: 这一行的宽度（**字符**单位）：比原来宽一半，好装下真实曲名 + 来源标注
        #: + 多会话那句"还有 N 个在放"。装不下的部分由 `fit_media_text()` 用 `…` 收尾，
        #: 绝不静默切断（理由见那个函数）。
        #: 64 是**实测**出来的：这一行的自然宽度必须留在 1280 以内，
        #: 否则末尾那句"（勾了才替你启动；没有媒体会话时改用媒体键）"会被 Tk 压窄
        #: （`test_a5` 的挤压检查会报红）。
        self.MEDIA_TEXT_WIDTH = 64
        tk.Label(mbar, textvariable=self.media_text, font=UI_FONT, anchor="w",
                 fg="#123", width=self.MEDIA_TEXT_WIDTH).pack(side="left", padx=(0, 8))
        # 右边的控件按"从左到右"的视觉顺序 pack（都是 side="left"，不会互相挤）
        ttk.Label(mbar, text="媒体应用：", font=("Microsoft YaHei UI", 8),
                  foreground="#5a6b7b").pack(side="left")
        self.media_app_combo = ttk.Combobox(mbar, textvariable=self.media_app_pin,
                                           state="readonly", width=22, font=UI_FONT,
                                           values=[MEDIA_APP_ANY])
        self.media_app_combo.pack(side="left", padx=(0, 4))
        # 点下拉框 = 出**多选**面板（tkinter 的 Combobox 只能单选，见 `_edit_media_app_multi`）
        try:
            self.media_app_combo.configure(postcommand=self._edit_media_app_multi)
        except tk.TclError:
            pass
        ttk.Checkbutton(mbar, text="自动唤醒", variable=self.media_auto_wake).pack(
            side="left", padx=(0, 4))
        # 「播放/暂停」：唤醒 → 按应用发命令 → 没会话就退回全局媒体键（见 media_command）
        ttk.Button(mbar, text="播放/暂停", width=10,
                   command=self.media_toggle_play).pack(side="left", padx=(0, 2))
        #: 「◀◀ / ▶▶」= 上一首 / 下一首，走的是同一条"唤醒→按应用→退回媒体键"的路
        ttk.Button(mbar, text="◀◀", width=4,
                   command=lambda: self.media_prev_next("previous")).pack(side="left", padx=1)
        ttk.Button(mbar, text="▶▶", width=4,
                   command=lambda: self.media_prev_next("next")).pack(side="left", padx=1)
        ttk.Label(mbar, text="（勾了才替你启动；没有媒体会话时改用媒体键）",
                  font=("Microsoft YaHei UI", 8), foreground="#8fa8bd").pack(
            side="left", padx=(8, 0))
        # 清单要等控件建好才能填（`_reload_media_app_list` 会 configure 这个下拉框）
        self._reload_media_app_list()

    def _build_ai_bar(self) -> None:
        """
        主界面「AI 状态条」：一行同时回答"现在是谁"和"这个用量是谁的"。

        用户 2026-10-19 的原话：「界面 UI 并没有显示现在识别并正在使用的 AI，
        我怎么知道你说的 AI 用量用的谁的」——所以这行必须常驻，不能藏在用量窗口里。

        文案由 `ai.format_ai_bar()` 生成（纯函数，可单测）；这里只负责布局。
        宽度约定：这一行只有"一个标签 + 两个按钮"，自然宽度远小于 1280，
        `test_a5` 的挤压检查会守住它。
        """
        bar = ttk.Frame(self.root, padding=(8, 2))
        bar.pack(fill="x")
        self.ai_text = tk.StringVar(value="AI：正在识别…")
        ttk.Label(bar, textvariable=self.ai_text, font=UI_FONT,
                  foreground="#2a4a66").pack(side="left")
        ttk.Button(bar, text="AI 助手…", width=9,
                   command=self.action_ai_assistant).pack(side="right", padx=(2, 0))
        ttk.Button(bar, text="AI 用量…", width=9,
                   command=self.action_ai_usage).pack(side="right", padx=2)


    def _app_has_session(self, app_key: str) -> bool:
        """
        当前读到的会话里，有没有属于这个应用的那一个？

        匹配走 `core.media_app_stems_match`（精确优先、包含要求 ≥5 字符）——
        以前的裸包含匹配会让会话里的家族名 `QQ` 命中 `QQMusic`，
        出现"选了 A、控的却是 B"（P2 修复）。
        """
        if not app_key:
            return bool(self._media_found)
        picked = str((self._media_last_pick or {}).get("app") or "")
        if not picked:
            return False
        return any(core.media_app_stems_match(k.strip(), picked)
                   for k in str(app_key).replace("|", ",").replace("、", ",").split(",")
                   if k.strip())

    def media_toggle_play(self) -> None:
        """播放/暂停（按应用）：按上一次读到的状态决定发 play 还是 pause。"""
        self.media_command("pause" if self._media_playing else "play")

    def _edit_media_app_multi(self) -> None:
        """
        「媒体应用」的**多选**编辑（A.2）。

        为什么不用 `ttk.Combobox` 自带的下拉：tkinter 的 Combobox 只支持单选。
        实测（2026-10-12）可以给它挂 `postcommand`，在弹出时显示我们自己的
        勾选列表 —— 所以点下拉框 = 出多选面板，选完把标签用「、」拼起来存进
        `media_app_pin`。执行时 `_app_key_for_media()` 会拼成 `exe1,exe2`，
        读会话那一行就能同时看多个播放器（"还有 N 个在放"）。
        """
        labels = [v for v in self.media_app_combo.cget("values")
                  if v != MEDIA_APP_ANY]
        current_labels = list(self._picked_media_labels())

        pop = tk.Toplevel(self.root)
        pop.overrideredirect(True)
        pop.attributes("-topmost", True)
        pop.configure(bg="#1b2027")
        outer = ttk.Frame(pop, padding=8)
        outer.pack(fill="both", expand=True)
        tk.Label(outer, text="可多选（不勾 = 自动跟随正在放的那个）", bg="#1b2027",
                 fg="#9fd6a0", font=("Microsoft YaHei UI", 8)).pack(anchor="w")
        vars_: dict[str, tk.BooleanVar] = {}
        for label in [MEDIA_APP_ANY] + labels:
            v = tk.BooleanVar(value=(label in current_labels))
            vars_[label] = v
            tk.Checkbutton(outer, text=label, variable=v, anchor="w",
                           bg="#1b2027", fg="#eaf2f8", selectcolor="#0f1418",
                           activebackground="#1b2027", activeforeground="#ffffff",
                           font=UI_FONT).pack(fill="x", anchor="w")

        row = ttk.Frame(outer)
        row.pack(fill="x", pady=(6, 0))

        def apply() -> None:
            picked = [lbl for lbl, v in vars_.items()
                      if v.get() and lbl != MEDIA_APP_ANY]
            self.media_app_pin.set("、".join(picked) if picked else MEDIA_APP_ANY)
            self._save_settings_soon()
            self._set_status(f"媒体应用已选：{self.media_app_pin.get()}")
            pop.destroy()

        ttk.Button(row, text="确定", width=8, command=apply).pack(side="right")
        ttk.Button(row, text="取消", width=8, command=pop.destroy).pack(
            side="right", padx=(0, 6))
        pop.bind("<Escape>", lambda _e: pop.destroy())
        # 弹在下拉框正下方（跟系统下拉的位置一致）
        self.root.update_idletasks()
        pop.update_idletasks()
        x = self.media_app_combo.winfo_rootx()
        y = self.media_app_combo.winfo_rooty() + self.media_app_combo.winfo_height()
        pop.geometry(f"+{x}+{y}")
        try:
            pop.grab_set()                      # 点别处不会误操作
        except tk.TclError:
            pass
        pop.focus_force()

    def _reload_media_app_list(self) -> None:
        """
        把"可唤醒的媒体应用"清单填进下拉框。

        三个来源（**会话优先**，2026-10-16 改）：
          1. **此刻活着的媒体会话**（`self._media_session_apps`）—— 第一真源：
             与"这台机器装了什么""候选表里写了什么"都无关，凡能发布会话的都认得出来；
          2. 开始菜单里真装的（音乐/浏览器两类，`core.installed_media_apps`）
             —— 覆盖用户自己装的、此刻没在放的那些；
          3. 写死的常见候选里"系统上真找得到"的那些 —— 它们带着**进程名**，
             能用进程名匹配媒体会话（开始菜单条目只有快捷方式，拿不到 exe 名）。

        清单本身的组装（去重、补 exe）在**纯函数** `build_media_app_entries()` 里，
        这里只负责取数据、填控件、并把**设置里的选择对齐到清单里的真实标签**
        （理由见 `core.resolve_media_pin()`：标签里的「（运行中）」会随状态变，
        直接比字符串会把用户选好的播放器悄悄退回「自动」）。

        **刻意不在这里读会话**：`_refresh_media()` 每 4 秒已经读回来一份，
        会话集合一变就会回头调本函数（见 `_apply_media_result`）—— 于是
        "新开一个播放器"会自动出现在清单里，而**不必为此多起一次 PowerShell**。
        """
        running = {(info.get("name") or "").lower()
                   for info in core.snapshot_processes().values()}
        if not hasattr(self, "media_app_combo"):
            # 手工搭 app / 还没建控件时被调用：只算清单，不碰控件
            self._media_apps = {MEDIA_APP_ANY: None}
            return
        try:
            found = core.installed_media_apps()
        except Exception:
            found = []
        labels, mapping = build_media_app_entries(
            running, lambda exe: bool(core.media_app_path(exe)), found,
            sessions=getattr(self, "_media_session_apps", ()))
        self._media_apps = mapping
        self._media_apps = mapping
        try:
            self.media_app_combo.configure(values=labels)
        except Exception:
            pass
        resolved, dropped = core.resolve_media_pin(self.media_app_pin.get(), labels,
                                                   MEDIA_APP_ANY)
        #: 这次启动时"设置里选的媒体应用找不到了"的那些（启动那句状态会如实说明）
        self._media_pin_dropped = dropped
        if resolved != self.media_app_pin.get():
            self.media_app_pin.set(resolved)

    def _app_key_for_media(self) -> str:
        """
        把选中的应用换成"用来匹配媒体会话"的关键字（exe 名）。

        **支持多选**（A.2）：设置里存的是"标签1、标签2"，这里拼成
        `exe1,exe2` —— `core.media_session_summary()` 会按逗号拆开逐个匹配。
        """
        keys = []
        for label in self._picked_media_labels():
            app = (getattr(self, "_media_apps", {}) or {}).get(label)
            if app is not None and app.exe:
                keys.append(app.exe)
        return ",".join(keys)

    def _picked_media_labels(self) -> list[str]:
        """设置里存的那串标签 → 列表（多选用"、"分隔）。"""
        raw = str(self.media_app_pin.get() or "")
        return [p.strip() for p in raw.replace(",", "、").split("、") if p.strip()]

    def _picked_media_app(self) -> "core.MediaApp | None":
        """
        当前选中的媒体应用（没选 / 选的是"自动"就返回 None；**多选**时返回第一个）。

        多选的意义在"看"（那一行会显示还有几个在放）和"发命令"（逐个发、逐个报告），
        所以这里保留"第一个"给需要单个应用的路径（比如标题兜底）。
        """
        for label in self._picked_media_labels():
            app = (getattr(self, "_media_apps", {}) or {}).get(label)
            if app is not None:
                return app
        return None

    def _media_app_is_running(self, app: "core.MediaApp") -> bool:
        """
        这个媒体应用**此刻在不在跑**。两条判据（任一成立即可，2026-10-16 新增）：

          · **进程名按主干对得上**（`core.media_app_running`）—— 会话里报的多半不是
            进程名（Edge 报家族名 `MSEdge`、UWP 报 `包名_哈希!App`），直接比字符串
            会把它判成"没在跑"，于是明明正在放歌、WinMon 却去走"唤醒"那条路；
          · **它此刻有活的媒体会话** —— 会话存在本身就说明它在跑，这条兜住
            "进程名压根解析不出来"的 UWP 包（那种情况下 `app.exe` 还是那个 PFN）。

        为什么要单独一个方法：`media_command()` 用它决定"发命令"还是"先唤醒"，
        这个判断以前是 `app.exe.lower() not in running` 一行，错得很隐蔽
        （点「播放/暂停」没反应，还报"它没在运行"）。
        """
        try:
            running = {i.get("name", "").lower()
                       for i in core.snapshot_processes().values()}
        except Exception:
            running = set()
        if core.media_app_running(app.exe, running):
            return True
        want = app.exe or app.label
        return any(core.media_app_stems_match(want, sid)
                   for sid in getattr(self, "_media_session_apps", ()))

    def _refresh_media(self) -> None:
        """
        读一次系统媒体会话并刷新这一行（**在后台线程里做**）。

        为什么要后台：一次调用要起一个 PowerShell（实测约 290ms），
        放主线程会把界面顶住。结果通过队列交回主线程（与窗口操作同一条规矩：
        工作线程一律不碰 tkinter）。
        """
        if self._media_busy:
            return
        self._media_busy = True
        import threading
        if not hasattr(self, "_media_msg"):
            self._media_msg = queue.Queue()      # 半成品 app（测试/截图）兜底

        def worker():
            try:
                sessions = core.collect_media_sessions()
            except Exception as exc:
                sessions = {"ok": False, "error": f"{type(exc).__name__}: {exc}",
                            "sessions": []}
            self._media_msg.put(sessions)

        threading.Thread(target=worker, daemon=True, name="WinMon-Media").start()

    def _title_hint_when_no_session(self) -> str:
        """
        媒体会话里什么都没有时，退一步**从播放器窗口标题**读"现在在放什么"。

        为什么需要（2026-10-12 实测）：Windows 的媒体会话只在应用主动发布时才有内容，
        而**网易云音乐客户端在放歌时也不发布会话**（它的窗口标题就是歌名）。
        没有这条兜底，WinMon 那一行就只会写"没有在播放"，用户看着自己的播放器在放歌
        却读不到名字。

        约定：
          · 只认**选定的应用**，没选就按候选表挑一个"正在运行的播放器"（跳过浏览器 ——
            浏览器标题里混着标签页名，拿它当歌名就是编造）；
          · 一律**标注来源**（"从窗口标题读的"），不冒充系统媒体会话；
          · 读不到就返回空串，由调用方照实说"没有在播放"。
        """
        app = self._picked_media_app()
        candidates = [app] if app is not None else []
        if not candidates:
            try:
                running = {(info.get("name") or "").lower()
                           for info in core.snapshot_processes().values()}
                for label, exe, browser in core.MEDIA_APP_CANDIDATES:
                    if browser or exe.lower() not in running:
                        continue
                    candidates.append(core.MediaApp(label, exe, browser))
            except Exception:
                pass
        for cand in candidates:
            try:
                title, _why = core.title_track_hint(cand.exe)
            except Exception:
                title = ""
            if title:
                return f"{cand.label}：{title}（从窗口标题读的）"
        return ""

    def _set_media_line(self, text: str) -> None:
        """
        写"正在播放"那一行：先按标签宽度裁（装不下就 `…` 收尾），再上屏。

        为什么要过这一道：标签是定宽的（横向不能让长曲名把按钮挤坏），
        而定宽控件会**静默**把文字切掉 —— 来源标注"（从窗口标题读的）"和
        多会话的"还有 N 个在放"以前就是这么消失的（见 `fit_media_text`）。
        """
        self.media_text.set(fit_media_text(text, getattr(self, "MEDIA_TEXT_WIDTH", 64)))

    def _sync_media_session_apps(self, rows) -> None:
        """
        记下"此刻有会话的 app id"；**变了才**重算「媒体应用」下拉框清单。

        为什么单独一个方法：
          · 让"什么时候会重建清单"这条规则只有一处（别在别处再写一遍判断）；
          · 测试可以直接喂一份会话行来驱动它，不必真的起一次媒体会话读取。

        代价与取舍：清单**只列此刻真有会话的**那些（不累积历史）。
        这样"某个播放器放完歌就不在清单里"是对的 —— 它此刻确实不是可识别的媒体；
        用户如果把它钉住过，`resolve_media_pin()` 会如实把这条丢掉的记录报出来
        （启动那句状态栏会写"没找到"，不静默）。
        """
        apps = core.session_app_ids(rows)
        if apps == list(getattr(self, "_media_session_apps", [])):
            return
        self._media_session_apps = apps
        try:
            self._reload_media_app_list()
        except Exception as exc:
            self._set_status(f"媒体应用清单刷新失败：{type(exc).__name__}: {exc}")

    def _apply_media_result(self, data: dict) -> None:
        """
        把后台读到的媒体会话结果画到界面上（**只跑在主线程**）。

        **不再显示进度**：用户已明确放弃"识别播放进度"这项需求，
        所以这里只用曲名/歌手/状态（`media_session_summary` 给的 `text`），
        进度条、时间、百分比全部删掉（`media_progress` 这个变量也没了）。
        """
        self._media_busy = False
        # **会话集合一变就重算「媒体应用」清单**（2026-10-16）：新打开的播放器/浏览器
        # 只要一发布会话就自动进下拉框 —— 这就是"基于所有可识别的媒体"。
        # 在这儿做（而不是让 `_reload_media_app_list` 自己去读）是因为这份会话数据
        # 已经到手了，重算清单不需要再起一次 PowerShell。
        self._sync_media_session_apps((data or {}).get("sessions") or [])
        if not isinstance(data, dict) or not data.get("ok"):
            err = (data or {}).get("error") or "读取失败"
            self._set_media_line(f"媒体会话读取失败：{err}")
            self._media_playing = False
            self._media_last_pick = {}
            return
        info = core.media_session_summary(data.get("sessions") or [],
                                         self._app_key_for_media())
        self._media_found = bool(info.get("found"))
        #: 上一次**实际选中**的那个会话（`media_command` 失败时用它说清"当前是谁"）
        self._media_last_pick = dict(info.get("picked") or {})
        self._media_playing = bool(
            info.get("found") and int((info.get("picked") or {}).get("status") or 0) == 4)
        if not info.get("found"):
            # 用户第 3 条：原来那句"现在没有程序在放东西"看不懂，而且没说是谁。
            # 先试**标题兜底**（网易云这类客户端不发布会话，标题里却有歌名），
            # 读到了就照实显示并标注来源；真的都没有，再照实说"没有在播放"。
            hint = self._title_hint_when_no_session()
            if hint:
                self._set_media_line(hint)
                return
            app = self._picked_media_app()
            self._set_media_line(
                f"（{app.label} 没有在播放的媒体）" if app
                else "（没有程序在播放媒体）")
            return
        # 曲名 · 歌手 · 状态 —— 前面**冠上正在放它的那个程序名**（用户第 2 条：
        # "明确哪个程序它是啥、运行了啥"）。会话里的 app 常是家族名（Edge 报 MSEdge），
        # 能换成用户选的显示名就换。
        picked_app = str(info["picked"].get("app") or "").strip()
        app = self._picked_media_app()
        if app is not None and picked_app and self._app_has_session(self._app_key_for_media()):
            shown = app.label
        else:
            shown = core.media_app_display_name(picked_app) or picked_app
        line = f"{shown}：{info['text']}" if shown else info["text"]
        # **多会话提示**（A.2）：不止一个在放时，补一句"还有 N 个"，
        # 否则用户看不到"另一个播放器其实也在放"。
        others = [o for o in (info.get("others") or []) if o]
        if others:
            line += f"　｜　还有 {len(others)} 个在放（{others[0]}）"
        self._set_media_line(line)

    #: 命令 → 全局媒体键的对应（"按应用发不出去"时退回用）
    MEDIA_KEY_FOR: dict[str, str] = {
        "play": "play_pause", "pause": "play_pause",
        "next": "next", "previous": "prev",
    }

    def media_prev_next(self, slug: str) -> None:
        """
        「◀◀ / ▶▶」：切上一首 / 下一首。

        `smart_media_command` 会逐个会话试命令 + 回读核对（最坏几十秒），
        **必须**放后台线程（P1-1 修复：以前直接在主线程调，界面长时间冻结）。
        """
        self._set_status(f"正在发送「{'上一首' if slug == 'previous' else '下一首'}」…")
        import threading

        def worker():
            ok, msg = core.smart_media_command(
                "prev" if slug == "previous" else "next")
            self._ui_call(lambda: self._after_prev_next(ok, msg, slug))

        threading.Thread(target=worker, daemon=True, name="WinMon-MediaCmd").start()

    def _after_prev_next(self, ok: bool, msg: str, slug: str) -> None:
        self._set_status(msg)
        if ok:
            self.log_action("媒体命令",
                            "上一首" if slug == "previous" else "下一首", msg)
        self._media_busy = False
        self._refresh_media()

    def media_command(self, slug: str) -> None:
        """
        发一条媒体命令（play / pause / next / previous）——
        唤醒、按应用、退回媒体键三步合一。

        **P1-1 修复**：`core.media_command` 内含 `subprocess.run(timeout=8)`，
        以前放在 `root.after` 回调里等于在**主线程**跑 —— 点一次按钮界面最多冻
        8 秒。现在发命令这一步移到后台线程，结果经 `_ui_call` 回主线程。
        """
        app = self._picked_media_app()
        app_key = self._app_key_for_media()
        woke = False
        if app is not None:
            if not self._media_app_is_running(app):
                if not self.media_auto_wake.get():
                    self._set_status(
                        f"{app.label} 没在运行 —— 想让它自动启动就勾上「自动唤醒」")
                    return
                ok, msg = core.launch_media_app(app)
                if not ok:
                    self._set_status(msg)
                    return
                woke = True
                self._set_status(msg + "（等它起来再发命令…）")
                self.log_action("唤醒媒体应用", app.label, msg)
        # 起应用要时间：延迟一拍再发命令，否则应用还没注册媒体会话。
        # label/key 在此刻定格 —— 回调里不再读界面变量（用户可能又改了下拉框）。
        delay = 2500 if woke else 0
        app_label = app.label if app is not None else ""
        self._media_busy = True
        self.root.after(max(1, delay),
                        lambda: self._media_command_async(slug, app_key, app_label))

    def _media_command_async(self, slug: str, app_key: str, app_label: str) -> None:
        """后台线程发媒体命令（8s 超时的 subprocess 不能占着 Tk 主线程）。"""
        import threading

        def worker():
            ok, msg = core.media_command(slug, app_key)
            self._ui_call(lambda: self._after_media_command(
                ok, msg, slug, app_key, app_label))

        threading.Thread(target=worker, daemon=True, name="WinMon-MediaCmd").start()

    def _after_media_command(self, ok: bool, msg: str, slug: str,
                             app_key: str, app_label: str) -> None:
        """主线程应用媒体命令的结果：写状态栏 / 留痕，然后刷新媒体行。"""
        who = app_label or "当前播放器"
        if ok:
            self._set_status(f"{msg}（{who}）")
            self.log_action("媒体命令", who,
                            f"{core.MEDIA_COMMAND_LABEL.get(slug, slug)}：{msg}")
        elif app_label and not self._app_has_session(app_key):
            # 退回全局媒体键（keybd_event 是内存级调用，留在主线程没问题）
            key = self.MEDIA_KEY_FOR.get(slug, "play_pause")
            kok, kmsg = core.send_media_key(key)
            if kok:
                self._set_status(f"{app_label} 没有媒体会话，已改用系统媒体键：{kmsg}")
                self.log_action("媒体键（回退）", core.MEDIA_KEY_LABEL.get(key, key),
                                f"{app_label} 无会话，改发系统媒体键：{kmsg}")
            else:
                self._set_status(f"{msg}；改发媒体键也失败：{kmsg}")
        else:
            # 什么都不做就不写日志，只在状态栏说一句（免得日志全是"其实没发生"）
            self._set_status(
                f"{msg}　（{app_label}有会话但没接受这条命令）" if app_label
                else f"{msg}　（当前没有程序在放东西，可以先用系统媒体键）")
        # 先清 busy 再刷新：顺序反了这一拍刷新会被自己挡掉
        self._media_busy = False
        self._refresh_media()

    def _bind_events(self) -> None:
        tv = self.tree
        tv.bind("<<TreeviewSelect>>", self._on_select)
        # 左栏列表的手势（用户 2026-10-11 定，见 plan_window_click 的说明）：
        #   · **单击不绑任何东西** —— 交给 Tk 默认的选中行为，也就是"只选中 + 右侧出预览"，
        #     刻意不碰任何窗口（这工具的本职是"看着、不打扰"）；
        #   · 双击 = 最小化 ↔ 还原；Enter / 中键 = 切到最前。
        # 注意 `<Enter>` 在 Tk 里是**鼠标移入控件**，键盘回车是 `<Return>`。
        tv.bind("<Double-1>", self._on_list_double_click)
        tv.bind("<Button-2>", self._on_list_middle_click)
        tv.bind("<Return>", self._on_list_enter_key)
        tv.bind("<KP_Enter>", self._on_list_enter_key)
        tv.bind("m", lambda _e: self.action_toggle_selected())
        tv.bind("M", lambda _e: self.action_minimize_selected())
        tv.bind("<Button-3>", self._on_right_click)
        tv.bind("<Delete>", lambda e: self.action_terminate())
        # 悬停说明（延迟弹出、复用同一个 Toplevel；绝不抢焦点）
        self._row_tip = _RowTip(tv, self._row_tip_text)
        self.notebook.bind("<<NotebookTabChanged>>", lambda e: self._on_tab_changed())

    # ------------------------------------------------------------------
    # 状态与历史
    # ------------------------------------------------------------------
    def _set_status(self, text: str) -> None:
        self.status_text.set(text)

    # ------------------------------------------------------------------
    # 操作历史（独立窗口 + 独立日志）
    # ------------------------------------------------------------------
    def _prune_history_on_start(self) -> None:
        """
        启动时按"保留天数"清一次操作历史日志（默认 30 天）。

        为什么值得单独一个方法：清理是**写操作**，失败不能挡启动；
        而且要在状态行里**如实说明**清掉了几条（铁律 2：不静默）。
        """
        try:
            days = int(self.history_days.get() or HISTORY_DAYS_DEFAULT)
        except (TypeError, ValueError, tk.TclError):
            days = HISTORY_DAYS_DEFAULT
        try:
            dropped, note = self.history.prune_log(days)
        except Exception as exc:
            self._set_status(f"清理操作历史失败：{type(exc).__name__}: {exc}")
            return
        if dropped:
            self.log_action("清理历史", f"超过 {days} 天的记录", note)

    def _log_target(self, name: str, pid: int = 0, title: str = "") -> str:
        """
        日志里的"对象"一行：**程序名（带 .exe）+ PID + 窗口标题**。

        用户反馈：「没法明确哪个程序打开哪个程序关闭了」——原来的 target 有的是
        `记事本 - 未命名`（窗口标题，认不出是哪个程序）、有的是 `chrome.exe [1024]`
        （没窗口标题），两种写法混在一起就看不明白了。这里统一成：

            程序名.exe [PID]｜窗口标题

        取不到的部分留空、不编造。所有写历史的地方都该走这个函数。
        """
        exe = str(name or "").strip() or "（未知程序）"
        if pid:
            exe += f" [{int(pid)}]"
        text = str(title or "").strip()
        return f"{exe}｜{text}" if text else exe

    def log_action(self, action: str, target: str, result: str,
                   open_time: str = "") -> None:
        """
        记一条操作历史。

        `self.history.add()` 会**同时**写内存和 `winmon_history.log`
        （用户要的"独立日志记录，放在 D 盘的 winmon 里"）。如果独立历史窗口开着，
        顺手让它刷新一下，不用用户手动点刷新。

        `open_time`：这个程序/窗口**是什么时候打开的**（`core.format_open_time`）。
        写进去之后，日志里"开"和"关"就配成了一对。
        """
        self.history.add(action, target, result, open_time=open_time)
        if self._history_window is not None:
            try:
                self._history_window.reload()
            except Exception:
                pass
        # 状态行顺手报一下"历史有多少条、日志在哪"，让用户知道去哪儿看
        try:
            self.history_hint.set(
                f"操作历史：本次 {len(self.history)} 条 · 日志 {self.history.log_path}"
                + (f"　⚠ {self.history.log_error}" if self.history.log_error else ""))
        except Exception:
            pass

    def clear_history(self) -> None:
        """清掉**界面上的**列表（日志文件不动，见 `clear_history_log`）。"""
        self.history.clear()
        if self._history_window is not None:
            try:
                self._history_window.reload()
            except Exception:
                pass
        self._set_status("界面上的操作记录已清空（日志文件仍在，见「操作历史…」）")

    def clear_history_log(self) -> None:
        """清空**日志文件**（先问一次，因为这是真的删留痕）。"""
        from tkinter import messagebox
        if not messagebox.askyesno(
                "清空操作历史日志",
                f"确定要清空日志文件吗？\n\n{self.history.log_path}\n\n"
                f"（这一步会真的删掉磁盘上的留痕，无法撤销；\n"
                f"只想清掉界面上的列表可以点「清空列表」。）"):
            self._set_status("已取消清空日志")
            return
        ok, msg = self.history.clear_log_file()
        if self._history_window is not None:
            try:
                self._history_window.reload()
            except Exception:
                pass
        self._set_status(msg)

    def action_ai_usage(self) -> None:
        """打开「AI 用量」窗口（已经开着就提到前台）。"""
        win = getattr(self, "_ai_usage_window", None)
        if win is not None:
            try:
                win.top.deiconify()
                win.top.lift()
                win.refresh()
                return
            except Exception:
                self._ai_usage_window = None
        try:
            self._ai_usage_window = _AiUsageWindow(self.root, self)
        except Exception as exc:
            self._set_status(f"打不开「AI 用量」：{type(exc).__name__}: {exc}")

    def action_history(self) -> None:
        """
        打开**独立的操作历史窗口**（约定："对操作历史做一个独立 ui"）。

        内容来自**日志文件**（`winmon_history.log`，跨会话保留），而不是内存里那份
        —— 这样关掉程序再打开也看得到以前的操作。已经开着就把它提到前台。
        """
        if self._history_window is not None:
            try:
                self._history_window.top.deiconify()
                self._history_window.top.lift()
                self._history_window.top.focus_force()
                self._history_window.reload()
                return
            except Exception:
                self._history_window = None
        try:
            self._history_window = _HistoryWindow(self.root, self)
        except Exception as exc:
            self._set_status(f"打开操作历史窗口失败：{type(exc).__name__}: {exc}")
            return
        self._set_status(f"已打开操作历史窗口（日志：{self.history.log_path}）")

    # ------------------------------------------------------------------
    # 设置持久化
    # ------------------------------------------------------------------
    def _backup_broken_settings(self, path: str) -> str:
        """
        把**读不出来**的设置文件另存一份 `<名字>.bad`，返回备份路径（失败则空串）。

        为什么必须备份（2026-10-13 修的真 bug）：保存是"整份覆盖"的。设置文件一旦
        坏掉（手改漏个逗号、磁盘写了一半），旧代码会静默按默认值启动，
        而退出时的自动保存又把默认值写回去 —— 收藏 / 最近使用 / 工作区快照
        **永久消失**，用户还看不到任何提示。备份一份，至少还能手工救回来。
        """
        try:
            backup = path + ".bad"
            if not os.path.exists(backup):        # 别覆盖更早那份好的备份
                shutil.copyfile(path, backup)
            return backup
        except OSError:
            return ""

    def load_settings(self) -> None:
        """
        读回界面偏好。

        **失败不许静默**（2026-10-13 修的真 bug）：以前整个函数被一个裸
        `except Exception: pass` 包着，于是一份坏掉的设置文件会让**所有**设置
        （含收藏/最近使用/工作区）悄悄回到默认值，退出时再把默认值写回去覆盖原文件。
        现在分两段处理：
          · **文件读不出来**（坏 JSON / 编码错 / 权限）→ 备份成 `.bad` 并记下原因，
            界面照常启动（绝不能因为设置坏了就打不开）；
          · 单项读坏了 → 只记原因，其余照常应用（不连坐）。
        原因会由 `_startup_status()` 报到状态栏，并写一条操作历史。
        """
        path = self._settings_path
        self._settings_load_error = ""
        self._settings_backup = ""
        if not os.path.exists(path):
            return
        try:
            import json
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except Exception as exc:
            self._settings_load_error = f"{type(exc).__name__}: {exc}"
            self._settings_backup = self._backup_broken_settings(path)
            return
        try:
            # **旧配置迁移**（C.2）：一次性把历史值/废弃键收拾干净，并写回文件。
            # 落痕走操作历史，用户能看见自己的设置被动过（不悄悄改）。
            data, changes = core.migrate_settings(data)
            self._settings_migration = changes
            for var, key in ((self.show_hidden, "show_hidden"),
                             (self.show_no_window, "show_no_window"),
                             (self.only_windowed, "only_windowed"),
                             (self.follow_selection, "follow_selection"),
                             (self.live_gallery, "live_gallery")):
                if key in data:
                    var.set(bool(data[key]))
            if data.get("size_choice") in [s[0] for s in PREVIEW_SIZES]:
                self.size_choice.set(data["size_choice"])
            if data.get("fps_choice") in [f[0] for f in FPS_CHOICES]:
                self.fps_choice.set(data["fps_choice"])
            # 关闭方式：设置里存的是 slug（含老版本的 "smart" 等），界面显示中文
            if data.get("video_close_mode"):
                self.video_close_mode.set(_close_mode_label(
                    _close_mode_slug(data["video_close_mode"])))
            # 操作历史保留天数：只认白名单（手改/旧版本一律退回默认 30 天）
            if data.get("history_days") in [d for d, _l in HISTORY_DAYS_CHOICES]:
                try:
                    self.history_days.set(int(data["history_days"]))
                except (TypeError, ValueError, tk.TclError):
                    pass
            # 实时总览的排列顺序（B.3）：一串窗口标题；坏数据一律当空
            saved_order = data.get("gallery_order")
            if isinstance(saved_order, list):
                self.gallery_order_saved = [str(x) for x in saved_order if str(x or "").strip()]
            # 程序目录的收藏 / 最近使用 / 手动分类
            self.program_prefs = core.CatalogPrefs.from_dict(data.get("program_prefs"))
            self.program_prefs.prune_recent(max_age_days=60.0)
            # 闲置提醒：开关 / 时长 / 是否自动关闭（默认全关，见 _init_video_vars）
            self.idle_alert.set(bool(data.get("idle_alert", False)))
            if data.get("idle_choice") in [label for label, _s in core.IDLE_CHECK_CHOICES]:
                self.idle_choice.set(data["idle_choice"])
            self.idle_auto_close.set(bool(data.get("idle_auto_close", False)))
            self.idle_watch.set_threshold(_idle_seconds_of(self.idle_choice.get()))
            if data.get("idle_snooze") in [label for label, _s in core.IDLE_SNOOZE_CHOICES]:
                self.idle_snooze_choice.set(data["idle_snooze"])
            self.idle_watch.set_snooze(
                core.idle_snooze_seconds_of(self.idle_snooze_choice.get()))
            # 媒体会话：指定的应用 + 是否自动唤醒。
            # 先让下拉框把清单填好（它依赖"系统上装了哪些"），再按存的标签选中；
            # 那个应用这次没装/找不到就退回"自动"，不让设置把界面卡在无效值上。
            self.media_auto_wake.set(bool(data.get("media_auto_wake", True)))
            pin = data.get("media_app_pin")
            # 先按设置填上，再刷新下拉框清单 —— 顺序反了的话，
            # "手工搭 app"（没建控件）的场景会读不回来（test_b2 的往返用例守着这条）。
            if isinstance(pin, str) and pin:
                self.media_app_pin.set(pin)
            # 关闭行为：只认两个合法值，别的（含旧版本/手改）一律退回默认
            if data.get("close_mode") in (CLOSE_MODE_TRAY, CLOSE_MODE_EXIT):
                self.close_mode.set(data["close_mode"])
            self.start_minimized.set(bool(data.get("start_minimized", False)))
            # 工作区快照（结构可能被手工改坏，统一过一遍 normalize）
            self.workspaces = core.normalize_workspaces(data.get("workspaces"))
            self.ws_minimize_others.set(bool(data.get("ws_minimize_others", False)))
            # 类别筛选也记住（多选；兼容旧版存成单个中文标签的 catalog_category）
            cats = data.get("catalog_categories")
            if isinstance(cats, list):
                self.catalog.selected_categories = {
                    k for k in cats if k in core.CATEGORY_LABELS}
            else:
                old = data.get("catalog_category")
                if old in [core.CATEGORY_LABELS[k] for k in core.CATEGORY_ORDER]:
                    self.catalog.selected_categories = {
                        k for k, v in core.CATEGORY_LABELS.items() if v == old}
            self.catalog.btn_cats.configure(text=self.catalog._category_label())
            self.catalog.only_favorites.set(bool(data.get("catalog_only_favorites", False)))
            self.catalog.only_recent.set(bool(data.get("catalog_only_recent", False)))
        except Exception as exc:
            # 单项读坏了**不连坐**：其余设置照常生效，只把原因记下来如实上报
            self._settings_load_error = f"{type(exc).__name__}: {exc}"

    def save_settings_now(self) -> None:
        if self._settings_after_id:
            try:
                self.root.after_cancel(self._settings_after_id)
            except Exception:
                pass
            self._settings_after_id = None
        try:
            import json
            data = {
                "show_hidden": self.show_hidden.get(),
                "show_no_window": self.show_no_window.get(),
                "only_windowed": self.only_windowed.get(),
                "follow_selection": self.follow_selection.get(),
                "live_gallery": self.live_gallery.get(),
                "size_choice": self.size_choice.get(),
                "fps_choice": self.fps_choice.get(),
                # 存 slug（不是界面上的中文），这样设置文件跨版本都认得住
                "video_close_mode": _close_mode_slug(self.video_close_mode.get()),
                # 操作历史保留天数（7 / 30 / 90）
                "history_days": int(self.history_days.get()),
                # 设置结构版本（C.2）：写回去，下次启动就知道不用再迁移了
                "settings_version": core.SETTINGS_VERSION,
                # 实时总览的排列顺序（B.3）：存窗口标题（hwnd 重启就变）
                "gallery_order": list(self.gallery_order_saved),
                # 闲置提醒（默认关闭；约定默认不开启）
                "idle_alert": bool(self.idle_alert.get()),
                "idle_choice": self.idle_choice.get(),
                "idle_auto_close": bool(self.idle_auto_close.get()),
                "idle_snooze": self.idle_snooze_choice.get(),
                # 媒体会话：指定的播放器/浏览器 + 是否在它没跑时自动唤醒。
                # 存**中文标签**而不是 MediaApp 对象（设置文件里只能是 JSON 基本类型）。
                "media_app_pin": self.media_app_pin.get(),
                "media_auto_wake": bool(self.media_auto_wake.get()),
                # 设置界面里的"关闭行为"（tray = 隐藏到托盘 / exit = 结束进程）
                # 与"启动时直接收进托盘"
                "close_mode": self.close_mode.get(),
                "start_minimized": bool(self.start_minimized.get()),
                # 工作区快照 + 应用时的附加选项
                "workspaces": self.workspaces,
                "ws_minimize_others": bool(self.ws_minimize_others.get()),
                "program_prefs": self.program_prefs.to_dict(),
                "catalog_categories": sorted(self.catalog.selected_categories,
                                             key=lambda k: core.CATEGORY_ORDER.index(k))
                if hasattr(self, "catalog") else [],
                "catalog_only_favorites": bool(self.catalog.only_favorites.get()),
                "catalog_only_recent": bool(self.catalog.only_recent.get()),
            }
            with open(self._settings_path, "w", encoding="utf-8") as fh:
                json.dump(data, fh, ensure_ascii=False, indent=2)
            self._settings_error = ""
        except Exception as exc:
            # 这里以前是 `except Exception: pass` —— 结果是"设置静默地不保存"，
            # 用户只会觉得"我改的东西怎么没记住"，完全看不到原因。
            # 当时真被这个坑到一次：新增一个设置项后，测试里那份"残缺的 app"
            # 缺这个属性 → 保存整体失败 → 连文件都没生成，而日志里什么都没有。
            # 现在：原因记进 `self._settings_error`（可断言），并且**对可行动的原因**
            # 打一条 stderr（pythonw 下看不到，但命令行与测试看得到）。
            #
            # 例外：目标**目录都不存在**时不吭声。这在实际使用里只有一种情况 ——
            # 测试把 WINMON_SETTINGS 指向临时目录、随后把临时目录删了，
            # 而 800ms 的延迟保存此刻才落地。那种"位置已经没了"报了也没用，
            # 反而会在每次跑测试时刷 5 行噪音，把真正的失败淹掉。
            self._settings_error = f"{type(exc).__name__}: {exc}"
            missing_dir = isinstance(exc, FileNotFoundError) and not os.path.isdir(
                os.path.dirname(os.path.abspath(self._settings_path)))
            if not missing_dir:
                try:
                    print(f"[WinMon] 设置保存失败：{self._settings_error}", file=sys.stderr)
                except Exception:
                    pass
                try:
                    setter = getattr(self, "_set_status", None)
                    if callable(setter):
                        setter(f"设置没能保存：{self._settings_error}")
                except Exception:
                    pass

    def _schedule_save_settings(self, *_args) -> None:
        if self._settings_after_id:
            try:
                self.root.after_cancel(self._settings_after_id)
            except Exception:
                pass
        self._settings_after_id = self.root.after(800, self.save_settings_now)

    def _report_settings_migration(self) -> None:
        """
        把"旧配置被迁移 / 设置文件读不出来"这两件事**如实报出来**并留痕（C.2）。

        为什么不能静默：用户下次打开设置界面会发现某些项变了样（比如关闭方式
        从"暂停并返回"变成"关闭当前窗口"的写法），不说明的话会以为程序自己乱改设置；
        而"设置文件读不出来"更严重 —— 界面按默认值跑，退出时的自动保存还会把
        默认值写回去，必须告诉他备份在哪。所以状态栏说一句、操作历史记一条。
        """
        err = getattr(self, "_settings_load_error", "")
        if err:
            backup = getattr(self, "_settings_backup", "") or "（备份失败）"
            try:
                self.log_action("设置读取失败", os.path.basename(self._settings_path),
                                f"{err}；已按默认值启动，原文件备份：{backup}")
            except Exception:
                pass
        changes = getattr(self, "_settings_migration", None)
        if not changes:
            return
        brief = "；".join(changes[:3]) + ("…" if len(changes) > 3 else "")
        self._set_status(f"已升级旧设置（{len(changes)} 项）：{brief}")
        self.log_action("设置迁移", f"{len(changes)} 项", brief)
        # 迁移结果立刻写回文件（这样下次启动不必再迁一遍）
        try:
            self.save_settings_now()
        except Exception:
            pass

    def _save_settings_soon(self) -> None:
        """
        立刻把设置写盘（用于收藏 / 类别这类"点一下就该记住"的操作）。

        不用 `after(800, …)` 延迟合并：用户改完收藏可能马上关窗口，
        延迟保存会丢掉这次改动。
        """
        self.save_settings_now()

    # ------------------------------------------------------------------
    # 列表刷新
    # ------------------------------------------------------------------
    def refresh_all(self) -> None:
        self.refresh_processes()
        self.refresh_windows()
        self._refresh_preview_header()

    def refresh_processes(self, force_cpu: bool = False) -> None:
        self.proc_map = core.snapshot_processes()
        now = time.monotonic()
        if not force_cpu and (now - self._cpu_prev_t) < self.CPU_SAMPLE_MIN_INTERVAL:
            return
        dt = max(0.05, now - self._cpu_prev_t)
        nproc = max(1, os.cpu_count() or 1)
        wanted = {wi.pid for wi in self.windows} | set(self._cpu_prev)
        for pid in wanted:
            if pid not in self.proc_map:
                continue
            t = core.query_process_cpu_time(pid)
            if t is None:
                self._cpu_pct[pid] = 0.0
                continue
            prev = self._cpu_prev.get(pid)
            if prev is not None:
                pct = (t - prev) / dt / nproc * 100.0
                self._cpu_pct[pid] = max(0.0, min(999.0, pct))
            self._cpu_prev[pid] = t
        self._cpu_prev_t = now
        # 私有提交（任务管理器「提交大小」列）分档查询（P2 优化）：
        #   · 与「有窗口进程」同名的进程（Edge/Chrome 的后台兄弟进程）：分组行的
        #     合计内存要算它们，总要查；
        #   · 其余系统进程：只有「显示无窗口进程 / 进程树」视图在用时才查 ——
        #     省掉每 2 秒几百次 OpenProcess + GetProcessMemoryInfo。
        names = {wi.proc.lower() for wi in self.windows if wi.proc}
        need_full = self.show_no_window.get() or (
            hasattr(self, "notebook") and self.notebook.index("current") == 2)
        for pid in self.proc_map:
            info = self.proc_map[pid]
            if not need_full and (info.get("name") or "").lower() not in names:
                continue
            _path, wss, priv = core.query_process_detail(pid, with_private=True)
            if wss:
                info["mem_kb"] = wss
            info["mem_private_kb"] = priv
        alive = set(self.proc_map)
        for d in (self._cpu_prev, self._cpu_pct, self._path_cache):
            for k in [k for k in d if k not in alive]:
                d.pop(k, None)

    def refresh_windows(self) -> None:
        """枚举窗口并重建列表（与命令行共用 core.collect_desktop，避免两套逻辑）。"""
        keep = self.selected_hwnd
        self.windows = core.collect_desktop(include_cloaked=True, with_detail=True)
        self.win_by_hwnd = {wi.hwnd: wi for wi in self.windows}
        for wi in self.windows:
            if wi.path:
                self._path_cache[wi.pid] = wi.path
            wi.cpu = self._cpu_pct.get(wi.pid, 0.0)
        self._rebuild_tree(keep)
        self._update_overview_text()

    def _row_is_open(self, iid: str) -> bool:
        """这一行现在是展开的吗？（读不到就当没展开，绝不抛异常。）"""
        try:
            return bool(self.tree.item(iid, "open"))
        except Exception:
            return False

    def _rebuild_tree(self, keep_hwnd: int | None) -> None:
        tv = self.tree
        self._rebuilding = True          # 抑制重建过程中的选择事件
        try:
            # **先记住哪些分组是展开的**，重建后原样还原。
            # 不记的话：列表每 2 秒重建一次，用户刚展开的分组立刻被折叠成
            # `open=False`（子窗口行点都点不到）—— 2026-10-13 修掉的真 bug。
            # 键用**程序名**：Tk 不会复用删掉的 item id，拿 id 比对永远是 False。
            expanded_groups: set[str] = set()
            for iid, info in self.row_info.items():
                if not info or info[0] != "group":
                    continue
                app = self._group_app_of.get(iid)
                if app and self._row_is_open(iid):
                    expanded_groups.add(app)
            tv.delete(*tv.get_children())
            self.row_of_hwnd.clear()
            self.row_info.clear()
            self._group_app_of.clear()

            needle = self.search_text.get().strip().lower()

            def match(wi: core.WinInfo) -> bool:
                if wi.is_hidden_surface and not self.show_hidden.get():
                    return False
                if not needle:
                    return True
                return (needle in wi.title.lower() or needle in wi.proc.lower()
                        or needle in wi.real_name.lower() or needle == str(wi.pid))

            vis = [wi for wi in self.windows if match(wi)]
            by_app: dict[str, list[core.WinInfo]] = {}
            for wi in vis:
                by_app.setdefault(wi.display_name, []).append(wi)

            for app in sorted(by_app, key=lambda s: s.lower()):
                items = by_app[app]
                if not self.only_windowed.get():
                    for wi in sorted(items, key=lambda x: -x.mem_kb):
                        self._insert_window_row("", wi)
                    continue
                if len(items) == 1:
                    self._insert_window_row("", items[0])
                else:
                    # 组行的合计刻意**按可执行文件名把同名进程全部算进来**，
                    # 而不是只算这几个窗口的进程 —— 这正是任务管理器主列表的口径
                    # （Edge 会显示十来个渲染进程的合计）。用户拿 WinMon 与任务管理器
                    # 对比时，展开看单进程、收起看合计，两个数就都能对上。
                    # 见 HANDOFF 坑 39。
                    names = {wi.proc.lower() for wi in items if wi.proc}
                    group_pids = {x.pid for x in items}
                    for pid, info in self.proc_map.items():
                        if (info.get("name") or "").lower() in names:
                            group_pids.add(pid)
                    total_mem = sum(self.proc_map.get(p, {}).get("mem_kb", 0)
                                    for p in group_pids)
                    total_mem_priv = sum(self.proc_map.get(p, {}).get("mem_private_kb", 0)
                                         for p in group_pids)
                    total_cpu = sum(self._cpu_pct.get(p, 0.0) for p in group_pids)
                    pids = sorted({x.pid for x in items})
                    parent = tv.insert("", "end", text="", values=(
                        "", f"{app} （{len(items)} 个窗口 / {len(group_pids)} 个进程）", "",
                        ",".join(str(p) for p in pids[:4]),
                        core._fmt_mem_pair(total_mem, total_mem_priv),
                        f"{total_cpu:.1f}%", ""), open=(app in expanded_groups))
                    self.row_info[parent] = ("group", pids[0], 0)
                    self._group_app_of[parent] = app
                    for wi in sorted(items, key=lambda x: x.title.lower()):
                        self._insert_window_row(parent, wi)

            if self.show_no_window.get():
                windowed_pids = {wi.pid for wi in self.windows}
                orphans = [(pid, info) for pid, info in self.proc_map.items()
                           if pid not in windowed_pids and info["name"]]
                orphans.sort(key=lambda kv: -(kv[1]["mem_kb"] or 0))
                for pid, info in orphans[:400]:
                    if needle and needle not in info["name"].lower() and needle != str(pid):
                        continue
                    item = tv.insert("", "end", text="", tags=("nowin",), values=(
                        "", info["name"], "（无可见窗口）", pid,
                        core._fmt_mem_pair(info["mem_kb"], info.get("mem_private_kb", 0)),
                        f"{self._cpu_pct.get(pid, 0.0):.1f}%", "后台"))
                    self.row_info[item] = ("nowin", pid, 0)

            # 恢复选中；窗口已消失则清空选择，避免操作到已不存在的 PID
            if keep_hwnd is not None and keep_hwnd in self.row_of_hwnd:
                item = self.row_of_hwnd[keep_hwnd]
                tv.selection_set(item)
            else:
                if keep_hwnd is not None and self.selected_hwnd == keep_hwnd:
                    self.selected_hwnd = None
                    self.selected_pid = None
                tv.selection_remove(*tv.selection())
        finally:
            self._rebuilding = False
        # 窗口刷新完之后，把总览墙按**记过的顺序**排一遍（B.3）
        try:
            self._gallery_apply_saved_order()
        except Exception:
            pass

    def _insert_window_row(self, parent: str, wi: core.WinInfo) -> None:
        tags = []
        is_fg = (wi.hwnd == self._fg_hwnd)
        if wi.is_hidden_surface:
            tags.append("fg_hidden" if is_fg else "hidden")
        elif wi.hung:
            tags.append("noresp")
        elif wi.iconic:
            tags.append("minimized")
        elif is_fg:
            tags.append("fg")
        if wi.proc.lower() in core.CRITICAL_PROCESSES:
            tags.append("critical")

        label = wi.real_name
        if wi.is_frame_host:
            label = f"{label}（应用外壳）"
        item = self.tree.insert(
            parent, "end", text="", tags=tuple(tags),
            image=self.icons.photo(wi) or "",
            values=(act_text(wi, is_fg), label, wi.title, wi.pid,
                    core._fmt_mem_pair(wi.mem_kb, wi.mem_private_kb),
                    f"{wi.cpu:.1f}%", wi.state))
        self.row_of_hwnd[wi.hwnd] = item
        self.row_info[item] = ("window", wi.pid, wi.hwnd)

    def _update_overview_text(self) -> None:
        shown = [wi for wi in self.windows if not wi.is_hidden_surface]
        n_hidden = len(self.windows) - len(shown)
        n_app = len({wi.display_name for wi in shown})
        extra = f" · 隐藏 {n_hidden}" if n_hidden else ""
        self.overview_text.set(
            f"程序 {n_app} · 窗口 {len(shown)}{extra} · 进程 {len(self.proc_map)} · "
            f"总览 {len(self.gallery_keys)} · 上屏 {self._current_fps():.1f} FPS")

    # ------------------------------------------------------------------
    # 前台窗口监听（用于高亮）
    # ------------------------------------------------------------------
    def _poll_foreground(self) -> None:
        """较高频率地跟踪前台窗口，让"当前活动窗口"能立刻高亮。"""
        try:
            fg = core.get_foreground_hwnd()
            if fg != self._fg_hwnd:
                self._fg_hwnd = fg
                self._update_foreground_marks()
            self._ai_observe_foreground(fg)
        except Exception:
            pass
        self.root.after(700, self._poll_foreground)

    # ------------------------------------------------------------------
    # AI 工作伴侣（阶段 1.5）：识别主 AI + 本地事件
    # ------------------------------------------------------------------
    def _ai_observe_foreground(self, fg: int) -> None:
        """喂一拍给前台累计器（每 700ms 一次；只记内存，不落盘）。"""
        tracker = getattr(self, "_ai_tracker", None)
        if tracker is None:
            return
        wi = None
        try:
            wi = self.win_by_hwnd.get(fg)
        except Exception:
            wi = None
        key = ai.session_key_for_window(wi) if wi is not None else ""
        try:
            tracker.observe(time.monotonic(), key)
        except Exception:
            pass

    def _ai_tick(self) -> None:
        """
        每 2 秒一拍：算主 AI、跑本地检测器、刷新 AI 状态条、必要时弹提醒。

        本地这一层**不调用任何模型**（0 token、不联网）；只有用户显式开启
        「AI 助手 / 值守」时，才由 `_ai_assistant_tick` 发起直连调用。
        """
        if not getattr(self, "_ai_enabled", False):
            return
        try:
            res = ai.identify_ai_sessions(
                self.windows, getattr(self, "proc_map", {}) or {},
                self._ai_tracker, getattr(self, "_fg_hwnd", 0),
                pin=getattr(self, "_ai_pin", ""))
            self._ai_primary = res.get("primary")
            self._ai_sessions = res
            self._update_ai_bar(res)
            candidates = ai.detect_incidents(
                self.windows, getattr(self, "proc_map", {}) or {},
                disk_usage=ai.disk_usage_for_data_dir())
            fresh = self._ai_incidents.observe(candidates)
            for inc in fresh:
                self._ai_notify(inc)
            self._ai_assistant_tick(res, fresh)
        except Exception:
            # 这一层坏了不能影响主界面：静默跳过，下一拍再试
            pass

    # ------------------------------------------------------------------
    # AI 状态条 / 直连助手（阶段 5：界面可见 + 真实用量）
    # ------------------------------------------------------------------
    def _ai_status_snapshot(self, force: bool = False) -> dict:
        """预算与用量快照（2 秒一拍；账本很小，5 秒缓存足够）。"""
        now = time.monotonic()
        cached = getattr(self, "_ai_usage_cache", (0.0, None))
        if not force and cached[1] is not None and now - cached[0] < 5.0:
            return cached[1]
        try:
            status = self._ai_budget.status()
        except Exception:
            status = {}
        self._ai_usage_cache = (now, status)
        return status

    def _ai_direct_target(self) -> tuple:
        """
        当前识别到的 AI → 它自己的直连配置（找不到就 `None`）。

        这是"跟随用户正在用的 AI"的唯一入口：**绝不在识别到别的 AI 时偷偷调 GLM**。
        """
        primary = (getattr(self, "_ai_sessions", None) or {}).get("primary") or {}
        name = str(primary.get("name") or "")
        if not name:
            return None, ""
        try:
            return provider.resolve_for_ai(name), name
        except Exception:
            return None, name

    def _ai_recent_mcp_client(self) -> str:
        """最近 15 分钟有没有 AI 客户端真的通过 MCP 调过 WinMon。"""
        try:
            rows = (self._ai_budget.status() or {}).get("recent") or []
        except Exception:
            return ""
        now = time.time()
        for row in reversed(rows):
            if row.get("mode") != "mcp":
                continue
            try:
                age = now - float(row.get("t", 0) or 0)
            except (TypeError, ValueError):
                continue
            name = str(row.get("client") or "")
            if age <= 900 and name and name != "WinMon direct":
                return name
        return ""

    def _ai_provider_snapshot(self, force: bool = False) -> dict:
        """当前 AI 的直连状态（不读 key 内容，只读"key 在不在"）。"""
        cfg, name = self._ai_direct_target()
        if cfg is None:
            return {"configured": False, "follow_ai": name}
        info = cfg.describe()
        info["configured"] = True
        info["follow_ai"] = name
        return info

    def _update_ai_bar(self, res=None) -> None:
        """把"当前是谁 + 真实/估算用量 + 直连状态"写进主界面那一行。"""
        if not hasattr(self, "ai_text"):
            return
        try:
            if res is None:
                res = getattr(self, "_ai_sessions", None)
            text = ai.format_ai_bar((res or {}).get("primary"),
                                    self._ai_status_snapshot(),
                                    self._ai_provider_snapshot())
            self.ai_text.set(text)
        except Exception:
            pass

    def _ai_context(self) -> str:
        """采集一份窗口现场（只在用户点「分析」或值守到点时调用）。"""
        incidents = {"incidents": [inc.to_dict()
                                   for inc in self._ai_incidents.open_incidents()[:5]]}
        return ai.build_work_context(
            getattr(self, "windows", []) or [], getattr(self, "proc_map", {}) or {},
            getattr(self, "_fg_hwnd", 0), getattr(self, "_ai_sessions", None) or {},
            incidents)

    def _ai_log(self, text: str) -> None:
        """AI 助手的运行记录：窗口开着就追加，没开也留在内存里（最多 200 条）。"""
        line = f"[{time.strftime('%H:%M:%S')}] {text}"
        try:
            self._ai_log_lines.append(line)
            if len(self._ai_log_lines) > 200:
                del self._ai_log_lines[:50]
        except Exception:
            return
        win = getattr(self, "_ai_assistant_window", None)
        if win is not None:
            try:
                win.append(line)
            except Exception:
                pass

    def _ai_notify_text(self, title: str, text: str, severity: str = "info") -> None:
        """AI 助手的主动提醒：托盘气泡优先，退回状态栏。"""
        if not getattr(self, "_ai_notify_enabled", True):
            return
        if getattr(self, "_tray_ok", False) and getattr(self, "_tray", None) is not None:
            try:
                if self._tray.notify(title, text, severity=severity):
                    return
            except Exception:
                pass
        try:
            self._set_status(f"{title}：{text}")
        except Exception:
            pass

    def _ai_foreground_signature(self) -> str:
        """前台窗口签名：用它判断"值守"这一拍有没有必要再花 token。"""
        hwnd = int(getattr(self, "_fg_hwnd", 0) or 0)
        wi = None
        try:
            wi = self.win_by_hwnd.get(hwnd)
        except Exception:
            wi = None
        title = (getattr(wi, "title", "") or "")[:80] if wi is not None else ""
        return f"{hwnd}:{title}" if hwnd else ""

    def _ai_enqueue_job(self, job: dict) -> None:
        """把一次直连调用排队（同一时刻只跑一个，避免并发烧 token）。"""
        if len(self._ai_jobs) >= 3:
            return
        self._ai_jobs.append(job)
        self._ai_pump_jobs()

    def _ai_pump_jobs(self) -> None:
        if getattr(self, "_ai_busy", False) or not self._ai_jobs:
            return
        job = self._ai_jobs[0]
        cfg = job.get("cfg")
        if cfg is None:
            cfg, _name = self._ai_direct_target()
            job["cfg"] = cfg
        if cfg is None:
            self._ai_jobs.clear()
            self._ai_log("当前 AI 没有可用的直连配置；不会调用任何模型。")
            return
        bad = cfg.validate()
        if bad:
            self._ai_jobs.clear()
            self._ai_log(f"{job.get('ai') or '当前 AI'} 直连不可用：{bad}")
            return
        if not self._ai_budget.check(400).get("allowed"):
            self._ai_jobs.clear()
            self._ai_log("预算已暂停/用尽，本次 AI 助手调用没有发出。")
            return
        self._ai_jobs.pop(0)
        self._ai_busy = True
        threading.Thread(target=self._ai_worker, args=(job, cfg),
                         daemon=True, name="winmon-ai-assist").start()

    def _ai_worker(self, job: dict, cfg) -> None:
        """后台线程：只做网络调用；结果一律交回主线程（不碰 tkinter）。"""
        budget = ai.Budget()
        try:
            if job.get("kind") == "incident":
                out = provider.explain_incident(job.get("incident") or {},
                                                config=cfg, budget=budget)
            else:
                out = provider.call_model(job.get("prompt") or "", config=cfg,
                                          budget=budget,
                                          purpose=str(job.get("kind") or "assist"))
        except Exception as exc:
            out = {"ok": False, "code": "worker",
                   "error": f"{type(exc).__name__}: {exc}"}
        self._ui_call(lambda: self._ai_job_done(job, out))

    def _ai_job_done(self, job: dict, out: dict) -> None:
        self._ai_busy = False
        self._ai_usage_cache = (0.0, None)       # 账本刚变，下一拍重读
        ai_name = str(job.get("ai") or "当前 AI")
        if not out.get("ok"):
            self._ai_log(f"{ai_name} 调用失败：{out.get('error')}")
            self._set_status(f"AI 助手：{out.get('error')}")
        else:
            body = (out.get("text") or "").strip() or "（模型没有返回文字）"
            total = int(out.get("tokens", 0) or 0)
            usage = (f"{out.get('model') or '?'}：输入 {out.get('tokens_in', 0)} / "
                     f"输出 {out.get('tokens_out', 0)} = {total} token（API 回执）")
            self._ai_log(f"{body}\n— 跟随 {ai_name} · {usage}")
            self._set_status(f"AI 助手（{ai_name}）：{body[:70]}")
            ai.append_chat({
                "mode": "direct", "ai": ai_name, "model": out.get("model") or "",
                "question": job.get("question") or "现场分析", "answer": body,
                "tokens": total, "tokens_in": out.get("tokens_in", 0),
                "tokens_out": out.get("tokens_out", 0),
            })
            if job.get("kind") in ("watch", "incident"):
                self._ai_notify_text(f"{ai_name} 的建议", body[:180], "info")
            if job.get("kind") == "incident" and job.get("key"):
                self._ai_explained[str(job["key"])] = time.time()
        self._ai_pump_jobs()

    def _ai_assistant_tick(self, res: dict, fresh: list) -> None:
        """
        值守 / 自动解释的调度（每 2 秒被 `_ai_tick` 调一次）。

        只有"用户正在用的 AI"存在、且它的直连配置可用时才会自动调用；
        没有配置就只保留本地事件提醒，绝不用别的模型顶替。
        """
        win = getattr(self, "_ai_assistant_window", None)
        if win is not None:
            try:
                win.set_header(self._ai_header_text())
            except Exception:
                pass
        assist = getattr(self, "_ai_assist", {}) or {}
        cfg, ai_name = self._ai_direct_target()
        usable = bool(cfg is not None and not cfg.validate())
        if fresh and assist.get("auto_explain") and usable:
            for inc in fresh:
                key = inc.key
                if key in self._ai_explained:
                    continue
                self._ai_enqueue_job({
                    "kind": "incident", "key": key, "cfg": cfg, "ai": ai_name,
                    "incident": inc.to_dict(),
                    "question": f"本地事件：{inc.kind}",
                })
        if assist.get("enabled") and usable:
            now = time.time()
            try:
                interval = int(assist.get("interval_seconds", 300))
            except (TypeError, ValueError):
                interval = 300
            sig = self._ai_foreground_signature()
            if sig and sig != self._ai_watch_sig and now - self._ai_last_watch >= interval:
                self._ai_last_watch = now
                self._ai_watch_sig = sig
                question = ("现在是自动值守：用一两句话指出此刻最值得注意的一件事；"
                            "没有异常就回答“一切正常”。")
                prompt = provider.assistant_prompt(self._ai_context(), question)
                self._ai_enqueue_job({"kind": "watch", "cfg": cfg, "ai": ai_name,
                                      "question": question, "prompt": prompt})
        self._ai_pump_jobs()

    def _ai_header_text(self) -> str:
        cfg, ai_name = self._ai_direct_target()
        status = self._ai_status_snapshot()
        usage = (f"真实 {int(status.get('provider_tokens', 0) or 0):,} · "
                 f"精确 {int(status.get('tokenizer_tokens', 0) or 0):,} token")
        if not ai_name:
            return f"未识别到正在使用的 AI；先打开一个 AI 窗口。｜ {usage}"
        if cfg is None:
            return f"当前 AI：{ai_name}（没有直连配置）→ 剪贴板交接 ｜ {usage}"
        bad = cfg.validate()
        if bad:
            state = "缺 API key" if ("key" in bad or "API key" in bad) else bad
        else:
            state = "已就绪"
        return f"跟随当前 AI：{ai_name} → {cfg.model} · {state} ｜ {usage}"

    def set_assist(self, enabled: bool | None = None,
                   auto_explain: bool | None = None) -> dict:
        """改值守开关（界面调用；只写本地设置文件）。"""
        data = dict(getattr(self, "_ai_assist", {}) or {})
        if enabled is not None:
            data["enabled"] = bool(enabled)
            if enabled:
                self._ai_last_watch = time.time()
                self._ai_watch_sig = ""
        if auto_explain is not None:
            data["auto_explain"] = bool(auto_explain)
        self._ai_assist = ai.save_assist(data)
        return self._ai_assist

    def _ai_handoff(self, question: str = "") -> bool:
        """
        把现场**交给用户正在用的那个 AI**：复制一段自包含的提示词到剪贴板。

        这条路径不调用任何模型，所以不会出现"用户不用 GLM 却调了 GLM"。
        WinMon 看不到对方回答；用户可以把回答复制回来保存。
        """
        _cfg, ai_name = self._ai_direct_target()
        if not ai_name:
            self._ai_log("没有识别到正在使用的 AI；先打开一个 AI 窗口，"
                         "或用 WINMON_AI_PIN 指定。")
            self._set_status("AI 助手：没有识别到正在使用的 AI")
            return False
        primary = (getattr(self, "_ai_sessions", None) or {}).get("primary") or {}
        via_mcp = (str(primary.get("source") or "") == "mcp_parent"
                   or bool(self._ai_recent_mcp_client()))
        prompt = provider.handoff_prompt(self._ai_context(), question,
                                         ai_name=ai_name, via_mcp=via_mcp)
        copied = ai.copy_to_clipboard(prompt)
        record = ai.append_chat({
            "mode": "handoff", "ai": ai_name,
            "question": question or "现场交接",
            "note": "copied" if copied else "copy_failed",
        })
        self._ai_last_handoff = {"id": record.get("id", ""), "ai": ai_name}
        self._ai_log(
            f"已把现场{'复制到剪贴板' if copied else '生成（复制失败）'}，交给 {ai_name}。"
            + ("它已通过 MCP 接入，可以直接读 WinMon 工具。" if via_mcp else "")
            + " WinMon 看不到这次回复；回答后可点「保存回复」。")
        self._set_status(
            f"已交给 {ai_name}：{'粘贴到它的对话里即可' if copied else '剪贴板复制失败'}"
            f"（本次不调用任何模型）")
        return True

    def ai_assistant_request(self, question: str = "",
                             force_handoff: bool = False) -> bool:
        """
        「AI 助手」的手动提问：**跟随当前识别到的 AI**。

        能拿到该 AI 自己的直连配置就调用它；拿不到就复制现场给用户粘贴，
        绝不回退到"另一个模型"。
        """
        cfg, ai_name = self._ai_direct_target()
        if not ai_name:
            self._ai_log("没有识别到正在使用的 AI；先打开一个 AI 窗口。")
            self._set_status("AI 助手：没有识别到正在使用的 AI")
            return False
        if cfg is not None and not force_handoff:
            bad = cfg.validate()
            if not bad:
                if getattr(self, "_ai_busy", False) or self._ai_jobs:
                    self._ai_log("上一条还在处理，等它回来再发（避免并发烧 token）。")
                    return False
                prompt = provider.assistant_prompt(self._ai_context(), question)
                self._ai_log(f"已发给 {ai_name} → {cfg.model}…")
                self._ai_enqueue_job({
                    "kind": "manual", "ai": ai_name, "cfg": cfg,
                    "question": question or "现场分析", "prompt": prompt,
                })
                return True
            if "key" not in bad and "API key" not in bad:
                self._ai_log(f"{ai_name} 的直连不可用：{bad}；改用剪贴板交接。")
        return self._ai_handoff(question)

    def ai_save_reply(self, text: str = "") -> bool:
        """把用户从当前 AI 那边复制回来的回答存进本地记录。"""
        body = (text or "").strip()
        if not body:
            self._set_status("保存回复：内容为空")
            return False
        last = getattr(self, "_ai_last_handoff", {}) or {}
        ai_name = str(last.get("ai") or "")
        if not ai_name:
            _cfg, ai_name = self._ai_direct_target()
        ai_name = ai_name or "当前 AI"
        ai.append_chat({"mode": "reply", "ai": ai_name, "answer": body,
                        "question": "用户从 AI 侧复制回来的回答"})
        self._ai_log(f"已保存 {ai_name} 的回复（{len(body)} 字）。")
        self._set_status(f"已保存 {ai_name} 的回复")
        return True

    def action_ai_assistant(self) -> None:
        """打开「AI 助手」窗口（已经开着就提到前台）。"""
        win = getattr(self, "_ai_assistant_window", None)
        if win is not None:
            try:
                win.top.deiconify()
                win.top.lift()
                win.top.focus_force()
                win.set_header(self._ai_header_text())
                return
            except Exception:
                self._ai_assistant_window = None
        try:
            self._ai_assistant_window = _AiAssistantWindow(self.root, self)
            self._set_status("AI 助手已打开：跟随当前 AI；有直连则调用，"
                             "否则复制现场供你粘贴")
        except Exception as exc:
            self._set_status(f"打不开「AI 助手」：{type(exc).__name__}: {exc}")

    def _ai_notify(self, inc) -> None:
        """本地提醒：优先托盘气泡；托盘不可用就退回状态栏。"""
        if not getattr(self, "_ai_notify_enabled", True):
            return
        text = ai.incident_notification_text(inc)
        if getattr(self, "_tray_ok", False) and getattr(self, "_tray", None) is not None:
            try:
                if self._tray.notify("WinMon 提醒", text, severity=inc.severity):
                    return
            except Exception:
                pass
        try:
            self._set_status(text)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # 左栏列表：单击切到最前 / 双击最小化↔还原
    # ------------------------------------------------------------------

    def _row_window_at(self, event):
        """
        取出鼠标位置下的"窗口行" → `(item_id, WinInfo)`。

        分组行（按应用分组时的父行）、"无窗口进程"行、表头都返回 `(None, None)`：
        那些行没有窗口可切，点它们应当保持 Tk 原有的展开 / 选中行为。
        """
        try:
            if self.tree.identify_region(event.x, event.y) not in ("tree", "cell"):
                return None, None
            item = self.tree.identify_row(event.y)
        except tk.TclError:
            return None, None
        if not item:
            return None, None
        kind, _pid, hwnd = self.row_info.get(item, ("", 0, 0))
        if kind != "window" or not hwnd:
            return None, None
        return item, self.win_by_hwnd.get(hwnd)

    @staticmethod
    def _window_is_iconic(hwnd: int) -> bool:
        """
        实时问系统"这个窗口现在是不是最小化的"。

        不能读列表里缓存的 `WinInfo.iconic`（最多 2 秒前的快照）：
        我们刚把它最小化过，下一拍用旧值就会**再最小化一次**，
        "双击切换"就失灵了。这个查询极便宜，每次点击都实问。
        """
        return core.window_is_minimized(hwnd)

    def _on_list_double_click(self, event) -> None:
        """双击列表里的窗口行 → 已最小化就唤醒还原，否则最小化它。"""
        _item, wi = self._row_window_at(event)
        if wi is None:
            return
        self._run_click_action(
            plan_window_click("double", self._window_is_iconic(wi.hwnd)), wi)

    def _on_list_middle_click(self, event) -> None:
        """中键单击 → 把那个窗口切到最前（和浏览器中键一样形成肌肉记忆）。"""
        _item, wi = self._row_window_at(event)
        if wi is None:
            return
        self._run_click_action(plan_window_click("foreground"), wi)

    def _on_list_enter_key(self, _event=None) -> None:
        """列表里按 Enter → 把选中的窗口切到最前（替代原来的"单击就切前台"）。"""
        self.action_bring_front()

    def action_toggle_selected(self) -> None:
        """对选中的窗口逐个"最小化 ↔ 还原"（右键菜单 / M 键）。"""
        self._batch_window_action("toggle")

    def action_minimize_selected(self) -> None:
        """把选中的窗口全部最小化。"""
        self._batch_window_action("minimize")

    def action_restore_selected(self) -> None:
        """把选中的窗口全部还原并切到最前。"""
        self._batch_window_action("restore")

    def _selected_windows(self) -> list:
        """列表里当前选中的**窗口行**（分组行与无窗口行会被跳过）。"""
        out = []
        for item in self.tree.selection():
            kind, _pid, hwnd = self.row_info.get(item, ("", 0, 0))
            if kind == "window" and hwnd and hwnd in self.win_by_hwnd:
                out.append(self.win_by_hwnd[hwnd])
        return out

    def _batch_window_action(self, mode: str) -> None:
        """
        对选中的多个窗口一次性做动作 —— 相当于"一键收拾桌面"。

        每个窗口单独判定（`toggle` 时已最小化的还原、可见的最小化），
        并且在**后台队列**里逐个执行，所以选区很大时界面也不会卡。
        """
        targets = self._selected_windows()
        if not targets:
            self._set_status("请先在左侧列表里选中至少一个窗口（按住 Ctrl / Shift 可多选）")
            return
        planned: list[tuple[str, int]] = []
        for wi in targets:
            if mode == "minimize":
                action = CLICK_MINIMIZE
            elif mode == "restore":
                action = CLICK_ACTIVATE
            else:
                action = plan_window_click("double", self._window_is_iconic(wi.hwnd))
            if action != CLICK_NONE:
                planned.append((action, wi.hwnd))
        if not planned:
            return
        for action, hwnd in planned:
            self._queue_window_op(action, hwnd, coalesce=False)
        self._set_status(f"正在处理 {len(planned)} 个窗口…")

    def _run_click_action(self, action: str, wi: core.WinInfo) -> None:
        """把决策结果交给后台去执行。"""
        if action == CLICK_NONE or wi is None:
            return
        self._queue_window_op(action, wi.hwnd)

    #: 窗口操作线程空闲多久就退出（下一次点击会再起一个，没必要常驻）
    WIN_OP_IDLE = 5.0

    def _queue_window_op(self, action: str, hwnd: int, coalesce: bool = True) -> None:
        """
        把窗口操作交给后台线程。

        为什么要后台：激活走的是"附加输入队列 → 等一会儿 → 回读核对 → 可能重试"，
        一口气能花掉几百毫秒；放在 Tk 主线程里，每次操作界面都会顿一下。

        `coalesce=True`（单击/双击这种单一动作）**只保留最新一个请求** ——
        连点几下列表时中间那些已经没有意义了。
        `coalesce=False`（批量操作）**保留全部** —— 否则"把选中的 5 个窗口都最小化"
        会被合并成只处理最后一个，这是个很隐蔽的 bug。
        """
        import threading
        if coalesce:
            while not self._win_op_q.empty():
                try:
                    self._win_op_q.get_nowait()
                except queue.Empty:
                    break
        self._win_op_q.put((action, hwnd))
        if self._win_op_thread is not None and self._win_op_thread.is_alive():
            return
        self._win_op_thread = threading.Thread(
            target=self._window_op_worker, daemon=True, name="WinMon-WinOp")
        self._win_op_thread.start()

    def _window_op_worker(self) -> None:
        """
        后台执行窗口操作。

        结果**不直接调 Tk**，而是塞进 `_win_op_msg` 让主线程去取 ——
        从非主线程调用 tkinter 是不安全的（实测还可能把工作线程卡住，
        连带把排在后面的窗口操作一起拖住）；项目里其它后台线程
        （捕获、目录构建）也都是"用队列交回主线程"的做法，这里保持一致。
        """
        while True:
            try:
                action, hwnd = self._win_op_q.get(timeout=self.WIN_OP_IDLE)
            except queue.Empty:
                return
            try:
                if action == CLICK_MINIMIZE:
                    ok, msg = core.minimize_window(hwnd)
                else:
                    ok, msg = core.activate_window(hwnd)
            except Exception as exc:
                ok, msg = False, f"窗口操作失败：{type(exc).__name__}: {exc}"
            # 激活/最小化失败只报状态栏，**不进操作历史** ——
            # 前台锁定偶尔会拒绝，每条都留痕会把历史刷成噪音，真正该查的事反而被淹掉。
            hist = None
            # 结果交给主线程显示（不跨线程碰 Tk）；第二项是可选的操作历史
            self._win_op_msg.put((msg, hist))

    def _drain_window_op_msgs(self) -> None:
        """
        在主线程里应用后台窗口操作的结论：更新状态栏，需要留痕的写进操作历史。

        后台线程只往队列里塞 `(状态栏文字, 操作历史三元组或 None)`，
        所有 Tk 操作与历史记录都留在主线程做。
        """
        for _ in range(16):
            try:
                msg, hist = self._win_op_msg.get_nowait()
            except queue.Empty:
                return
            if hist:
                try:
                    self.log_action(*hist)
                except Exception:
                    pass
            self._set_status(msg)

    def _drain_media_msgs(self) -> None:
        """
        在主线程里应用后台读到的**媒体会话**结果。

        单独一条队列（而不是复用 `_win_op_msg`）：那条队列的每个元素都是
        `(状态栏文字, 历史三元组)` 两元组，塞别的形状进去会当场把整个刷新打断。
        """
        for _ in range(4):
            try:
                data = self._media_msg.get_nowait()
            except queue.Empty:
                return
            except AttributeError:
                # 用 `__new__` 造出来的"半成品 app"（测试/截图工具会这么干）没有这条队列；
                # 这里顺手补一个，免得整个刷新循环被一个属性缺失打断。
                self._media_msg = queue.Queue()
                return
            try:
                self._apply_media_result(data)
            except Exception as exc:
                self._set_status(f"媒体信息显示失败：{type(exc).__name__}: {exc}")

    # ------------------------------------------------------------------
    # 闲置程序（"开着不用、白耗电"）
    # ------------------------------------------------------------------
    #
    # 用户的原话：某个程序长期开着、只耗电不使用，希望"建议关闭，或超过指定时间帮忙关闭"。
    # 所以这里做成**两步**：到点先在状态栏提示 + 弹「闲置清单」让你勾选；
    # 只有你显式打开「自动关闭」开关，它才会自己动手（而且只优雅关闭，绝不强杀）。

    def _on_idle_toggle(self) -> None:
        """勾选框变化：开启时从零开始计时，关闭时把计时清零。"""
        self.idle_watch.reset()
        self._idle_ticks = 0
        self.idle_watch.set_threshold(_idle_seconds_of(self.idle_choice.get()))
        if self.idle_alert.get():
            self._set_status(f"闲置提醒已开启：连续 {self.idle_choice.get()} 没人用的程序会被列出来")
        else:
            self._set_status("闲置提醒已关闭")
        self._schedule_save_settings()

    def _check_idle(self, dt: float) -> None:
        """
        推进一次闲置计时（由 `_periodic_refresh` 驱动，约 2 秒一拍）。

        没勾选这个功能就**不累计**、并把计时清零 ——
        否则"刚勾上就立刻弹一堆"，用户根本不知道它从什么时候开始算的。
        """
        if not self.idle_alert.get():
            if self._idle_ticks:
                self.idle_watch.reset()
                self._idle_ticks = 0
            return
        want = _idle_seconds_of(self.idle_choice.get())
        if want != int(self.idle_watch.threshold):
            self.idle_watch.set_threshold(want)
        snooze = core.idle_snooze_seconds_of(self.idle_snooze_choice.get())
        self.idle_watch.set_snooze(snooze)
        newly = self.idle_watch.observe(
            dt, core.idle_samples(self._cpu_pct, procs=self.proc_map,
                                  windows=self.windows))
        self._idle_ticks += 1
        candidates = self.idle_watch.candidates()
        if not candidates:
            return
        if self.idle_auto_close.get():
            if newly:
                self._auto_close_idle(newly)
            return
        # 节流：提醒过一次之后，按 `idle_snooze_choice` 那档决定多久内不再**自动**弹窗
        # （间隔式看秒数；「每天只提醒一次」看自然日是否翻篇；「本次运行只提醒一次」看有没有弹过）。
        # 手动点「闲置清单」不受限制 —— 用户主动要看，就该给他看。
        # 为什么需要它：多个程序会在不同时刻陆续越过阈值，不节流的话
        # 你刚关掉一个清单、过几分钟又被另一个弹一次。
        now = time.monotonic()
        today = _local_day() if snooze == core.IDLE_SNOOZE_DAILY else ""
        may = self.idle_watch.may_notify(now, today, dialog_open=self._idle_dialog_open)
        if may:
            # **只有真的提醒了才推进"上次提醒时刻"**（间隔档就是靠它计时）。
            # 这里原来是无条件调用的，理由是"每日档要靠它推进 last_notice_day" ——
            # 但那让间隔档彻底失效：`_check_idle` 每 2 秒一拍，每拍都把时刻刷成"现在"，
            # `(now - last_notice) >= snooze` 于是永远不成立，30 分钟/1 小时/2 小时/4 小时
            # 这四档都变成"只会响一次"（2026-10-13 复现并修掉，见
            # test_b9 的 test_interval_mode_can_fire_again_with_realistic_ticks）。
            # 每日档不需要那次无条件调用：真弹过时 `note_popup` 已经把
            # `last_popup_day` 记成今天，同一自然日不会再弹（两道闸门都在 `may_notify` 里）。
            self.idle_watch.note_popup(today)
            self.idle_watch.note_notified(now, today)
        if may:
            self._set_status(
                f"{len(candidates)} 个程序闲置超过 {self.idle_choice.get()}，"
                f"已弹出清单等你确认")
            self.action_idle_dialog()
            return
        self._set_status(
            f"{len(candidates)} 个程序闲置超过 {self.idle_choice.get()}"
            f"（点「闲置清单」查看；{core.idle_snooze_hint(snooze)}）")

    def _auto_close_idle(self, pids: list) -> None:
        """
        到点自动关闭（**只有用户显式打开那个开关才会走到这里**）。

        只发 `WM_CLOSE`（等价于点窗口的关闭按钮）：程序若有未保存内容会自己弹确认，
        把决定权交回用户；它没退出也**不替你强杀**，如实回报。
        关闭动作在后台线程里做（每个窗口最多等 4 秒核对），否则界面会僵住。
        """
        import threading
        rows = {r["pid"]: r for r in self.idle_watch.candidates()}
        targets = []
        for pid in pids:
            info = rows.get(pid) or {}
            hwnd = info.get("hwnd") or self.hwnd_of_pid(pid)
            if not hwnd:
                continue
            targets.append((info.get("name") or f"PID {pid}", int(pid), int(hwnd)))
        if not targets:
            return
        self._set_status(f"闲置超时，正在优雅关闭 {len(targets)} 个程序…")

        def worker():
            for name, pid, hwnd in targets:
                try:
                    _ok, msg = core.close_idle_window(hwnd)
                except Exception as exc:
                    msg = f"关闭失败：{type(exc).__name__}: {exc}"
                self._win_op_msg.put((f"闲置关闭：{name} — {msg}",
                                      ("闲置关闭", f"{name} [{pid}]", msg)))

        threading.Thread(target=worker, daemon=True, name="WinMon-Idle").start()

    def action_idle_dialog(self) -> None:
        """打开「闲置程序」清单（也可从右键菜单 / 「闲置清单」按钮进来）。"""
        rows = self.idle_watch.candidates()
        if not rows:
            self._set_status(
                "现在没有闲置到阈值的程序"
                if self.idle_alert.get() else
                "请先勾选「闲置超时提醒」（默认关闭，本工具不会自己动手）")
            return
        if self._idle_dialog_open:
            return
        self._idle_dialog_open = True
        try:
            dlg = _IdleDialog(self.root, rows, self.idle_choice.get(),
                              auto_close=self.idle_auto_close)
            chosen = dlg.show()
        finally:
            self._idle_dialog_open = False
        if dlg.auto_close_changed:
            self._idle_auto_close_changed()
        # **一键最小化**的结果（B.2）：单独处理，别和"关闭"混在一起
        mini = getattr(dlg, "minimize_result", None)
        if mini is not None:
            done, failed = mini
            msg = f"已最小化 {done} 个闲置程序窗口"
            if failed:
                msg += f"；{len(failed)} 个没成功（{failed[0]}）"
            self._set_status(msg)
            self.log_action("闲置最小化", f"{done} 个窗口",
                            msg if not failed else f"{msg}｜失败：{'；'.join(failed[:3])}")
            return
        if chosen is None:
            # 「稍后再说」：把这些程序的闲置时长清零，它们会重新计时、重新提醒
            self.idle_watch.reset([r["pid"] for r in rows])
            self._set_status("已稍后再提醒（计时已重新开始）")
            return
        if not chosen:
            return
        self._close_idle_items(chosen)

    def _idle_auto_close_changed(self) -> None:
        """「以后到点自动关闭」勾选状态变了。"""
        self._schedule_save_settings()
        if self.idle_auto_close.get():
            self._set_status("已开启「到点自动关闭」：只优雅关闭（等同点关闭按钮），"
                             "有未保存内容时程序会自己问你")
        else:
            self._set_status("已改回「只提醒」：到点只列清单，不会自己关")

    def _close_idle_items(self, items: list) -> None:
        """关闭用户在闲置清单里勾选的程序（逐个优雅关闭 + 留痕）。"""
        import threading
        targets = [(it.get("name") or f"PID {it['pid']}", int(it["pid"]),
                    int(it.get("hwnd") or 0)) for it in items]
        self._set_status(f"正在优雅关闭 {len(targets)} 个闲置程序…")

        def worker():
            for name, pid, hwnd in targets:
                if not hwnd:
                    hwnd = self.hwnd_of_pid(pid)
                if not hwnd:
                    self._win_op_msg.put((f"{name}：没有可关闭的窗口",
                                          ("闲置关闭", f"{name} [{pid}]", "没有可用窗口")))
                    continue
                try:
                    _ok, msg = core.close_idle_window(hwnd)
                except Exception as exc:
                    msg = f"关闭失败：{type(exc).__name__}: {exc}"
                self._win_op_msg.put((f"{name} — {msg}",
                                      ("闲置关闭", f"{name} [{pid}]", msg)))

        threading.Thread(target=worker, daemon=True, name="WinMon-Idle").start()

    # ------------------------------------------------------------------
    # 工作区快照（"写代码 / 看视频 / 打游戏"各存一套布局，一键切回去）
    # ------------------------------------------------------------------

    def capture_current_layout(self) -> list[dict]:
        """
        把当前列表里的窗口拍成"可存快照"的形式。

        跳过 WinMon 自己、以及没有实际矩形的窗口；标题为空也照样记
        （有的窗口确实没标题，靠可执行文件名照样能对上）。
        """
        items = []
        mine = os.getpid()
        for wi in self.windows:
            # `is_own_window` 同时排掉"本进程"与"本机另一个 WinMon 实例" ——
            # 只比 pid 会漏掉后者（实测：用户开着旧实例时，它被抓进了快照，
            # 那样"恢复布局"就会去挪另一个 WinMon 的窗口）。
            if not wi.hwnd or core.is_own_window(wi.title, wi.pid, mine):
                continue
            try:
                rect = core.get_window_rect(wi.hwnd)
            except Exception:
                rect = None
            if not rect:
                continue
            items.append({"name": wi.proc or wi.real_name, "title": wi.title or "",
                          "rect": list(rect), "iconic": bool(wi.iconic)})
        return items

    def apply_workspace(self, snapshot: dict) -> None:
        """应用一个工作区快照（界面上的「应用」按钮走这里）。"""
        self._apply_workspace_snapshot(snapshot)

    @staticmethod
    def _describe_windows(rows, limit: int = 5) -> str:
        """
        把窗口清单说成一行：`chrome.exe｜GitHub · code.exe｜main.py`。

        用户 2026-10-12 的要求：「明确哪个程序它是啥、运行了啥」——
        恢复工作区这类批量动作原来只写了一句统计（"已还原 3 个"），
        看不出到底动了哪些程序，所以这里把**程序名 + 窗口标题**列出来。
        超过 `limit` 个尾部省略，避免一行刷屏。
        """
        rows = [r for r in (rows or []) if isinstance(r, dict)]
        parts = []
        for row in rows[:limit]:
            name = str(row.get("name") or "").strip()
            title = str(row.get("title") or "").strip()
            if name and title:
                parts.append(f"{name}｜{title}")
            elif name or title:
                parts.append(name or title)
        text = " · ".join(parts)
        rest = len(rows) - len(rows[:limit])
        if rest > 0:
            text += f" · 等 {rest} 个"
        return text

    def _apply_workspace_snapshot(self, snapshot: dict) -> None:
        """
        应用一个工作区快照（保存过的窗口位置/大小）。

        在**后台线程**里做（要挪十几个窗口、每个都要核对，放主线程会僵住），
        结果用同一个队列交回主线程写状态栏与操作历史。
        """
        import threading
        plan = core.plan_workspace_apply(snapshot, self.windows)
        name = snapshot.get("name", "")
        if not plan["restore"] and not plan["minimize"]:
            self._set_status(
                f"「{name}」里的程序现在一个都没开 —— 恢复只对**已经开着的**窗口生效，"
                f"不会替你启动程序")
            return
        minimize_others = bool(self.ws_minimize_others.get())
        keep = [it["hwnd"] for it in plan["restore"] + plan["minimize"]]
        windows_snapshot = list(self.windows)
        self._set_status(
            f"正在恢复「{name}」：还原 {len(plan['restore'])} 个 · "
            f"最小化 {len(plan['minimize'])} 个…")

        def worker():
            result = core.apply_workspace_plan(plan)
            extra = 0
            if minimize_others:
                extra = core.minimize_windows_except(keep, windows_snapshot)
            # 日志里写清**具体是哪些程序**（用户第 2 条：明确哪个程序运行了啥）
            detail = []
            restored = self._describe_windows(plan["restore"])
            if restored:
                detail.append(f"还原：{restored}")
            minimized = self._describe_windows(plan["minimize"])
            if minimized:
                detail.append(f"最小化：{minimized}")
            missing = plan.get("missing") or []
            if missing:
                names = self._describe_windows(missing, limit=4)
                detail.append(f"{len(missing)} 个没开（{names}）")
            failed = result.get("failed") or []
            if failed:
                detail.append(f"{len(failed)} 个失败（{self._describe_windows(failed, 3)}）")
            if extra:
                detail.append(f"顺带收起其它 {extra} 个窗口")

            parts = [f"已还原 {result['done']} 个"]
            if result["minimized"]:
                parts.append(f"最小化 {result['minimized']} 个")
            if extra:
                parts.append(f"顺带收起其它 {extra} 个")
            if missing:
                parts.append(f"{len(missing)} 个没开")
            if failed:
                parts.append(f"{len(failed)} 个失败")
            msg = f"工作区「{name}」：" + " · ".join(parts)
            self._win_op_msg.put((msg, ("恢复工作区", f"工作区「{name}」",
                                        "；".join(detail) or msg)))

        threading.Thread(target=worker, daemon=True, name="WinMon-Workspace").start()

    def _row_tip_text(self, item: str) -> str:
        """
        鼠标停在某一行上时显示的说明。

        顺带把**这一行正在被计时的闲置时长**也写出来 —— 否则"它到底算不算闲置、
        已经算了多久"只有弹出清单那一刻才看得到，用户没法核对判定是否合理。
        """
        kind, pid, hwnd = self.row_info.get(item, ("", 0, 0))
        if kind != "window" or not hwnd:
            return ""
        wi = self.win_by_hwnd.get(hwnd)
        if wi is None:
            return ""
        lines = [f"{wi.real_name}　{wi.state}",
                 (wi.title or "（无标题）")[:70],
                 f"PID {pid}　CPU {wi.cpu:.1f}%",
                 core.mem_explain(wi.mem_kb, wi.mem_private_kb)]
        if self.idle_alert.get():
            idle = self.idle_watch.idle_seconds(pid)
            if idle >= 60:
                lines.append(f"已闲置 {core.format_idle(idle)}"
                             f"（满 {self.idle_choice.get()} 后提醒）")
            else:
                lines.append(f"当前按「在用」计时（闲置满 {self.idle_choice.get()} 后提醒）")
        lines.append("单击：选中预览　双击：最小化/还原　Enter/中键：切到最前")
        return "\n".join(lines)

    def _update_foreground_marks(self) -> None:
        """只改行的样式与「活动」标记，不重建整棵树（成本极低）。"""
        for hwnd, item in self.row_of_hwnd.items():
            if not self.tree.exists(item):
                continue
            wi = self.win_by_hwnd.get(hwnd)
            if wi is None:
                continue
            is_fg = hwnd == self._fg_hwnd
            tags = []
            if wi.is_hidden_surface:
                tags.append("fg_hidden" if is_fg else "hidden")
            elif wi.hung:
                tags.append("noresp")
            elif wi.iconic:
                tags.append("minimized")
            elif is_fg:
                tags.append("fg")
            if wi.proc.lower() in core.CRITICAL_PROCESSES:
                tags.append("critical")
            try:
                # 「活动」列也要跟着变 —— 只改 tag 的话，"● 当前活动"要等下一次
                # 整表重建（最多 2 秒）才更新，看起来就像高亮和标记对不上。
                values = list(self.tree.item(item, "values"))
                if values:
                    values[0] = act_text(wi, is_fg)
                    self.tree.item(item, tags=tuple(tags), values=tuple(values))
                else:
                    self.tree.item(item, tags=tuple(tags))
            except tk.TclError:
                continue

    # ------------------------------------------------------------------
    # 进程树
    # ------------------------------------------------------------------
    def refresh_process_tree(self, force: bool = False) -> None:
        """按父子关系重建进程树（层级无变化时跳过，避免打断用户展开的节点）。"""
        tv = self.proc_tree.tree
        children = core.build_process_tree(self.proc_map)
        window_pids = {wi.pid for wi in self.windows}
        # 只展示「有窗口的进程」及其所有祖先，避免刷出几百行无关系统进程
        keep: set[int] = set()
        for pid in window_pids:
            cur, guard = pid, 0
            while cur and cur in self.proc_map and guard < 40:
                keep.add(cur)
                cur = self.proc_map[cur]["ppid"]
                guard += 1
        sig = (len(self.proc_map), frozenset((p, self.proc_map[p]["name"]) for p in keep))
        if not force and sig == self._tree_sig:
            return
        self._tree_sig = sig

        # 记住哪些节点是展开的 —— **记 pid，不记 item id**（2026-10-13 修的真 bug）：
        # Tk 不会复用删掉的 item id（实测 insert→I001、删掉再 insert→I003），
        # 所以原来那句 `tv.exists(iid)` 永远为假，"恢复展开"从来没生效过：
        # 任何进程启动/退出都会重建一次树，用户展开的节点当场被收回去。
        expanded_pids: set[int] = set()
        for iid, pid in self._tree_pid_of.items():
            try:
                if tv.exists(iid) and tv.item(iid, "open"):
                    expanded_pids.add(pid)
            except Exception:
                pass
        tv.delete(*tv.get_children())
        self._tree_pid_of.clear()

        win_title: dict[int, str] = {}
        for wi in self.windows:
            if not wi.is_frame_host:
                win_title.setdefault(wi.pid, wi.title)

        def add(pid: int, parent_item: str, depth: int) -> None:
            if depth > 12:
                return
            info = self.proc_map.get(pid)
            if not info:
                return
            has_win = pid in window_pids
            label = info["name"] or f"PID {pid}"
            if has_win and win_title.get(pid):
                label += f"  —  {win_title[pid][:34]}"
            tags = []
            if pid == os.getpid():
                tags.append("self")
            elif has_win:
                tags.append("haswin")
            if (info["name"] or "").lower() in core.CRITICAL_PROCESSES:
                tags.append("critical")
            item = tv.insert(parent_item, "end", text=label, tags=tuple(tags),
                             open=(depth < 1),
                             values=(pid, "有" if has_win else "—",
                                     core._fmt_mem(info.get("mem_kb", 0)),
                                     core._fmt_mem(info.get("mem_private_kb", 0))
                                     if info.get("mem_private_kb") else "—",
                                     f"{self._cpu_pct.get(pid, 0.0):.1f}%"))
            self._tree_pid_of[item] = pid
            for k in sorted(k for k in children.get(pid, []) if k in keep):
                add(k, item, depth + 1)

        roots = [p for p in keep if self.proc_map.get(p, {}).get("ppid") not in keep]
        for p in sorted(roots, key=lambda x: (self.proc_map.get(x, {}).get("name") or "").lower()):
            add(p, "", 0)
        for pid in expanded_pids:
            # 按 pid 找回重建后的新 item 再展开（旧的 item id 早就失效了）
            for iid, got in self._tree_pid_of.items():
                if got == pid:
                    try:
                        tv.item(iid, open=True)
                    except Exception:
                        pass
                    break
        self.proc_tree.set_info(
            f"共 {len(self.proc_map)} 个进程，其中 {len(window_pids)} 个有可见窗口；"
            f"树上显示 {len(keep)} 个相关进程（含父进程链）")

    def action_kill_tree_selection(self) -> None:
        """结束进程树里选中的进程及其全部子孙（执行前列出影响范围）。"""
        sel = self.proc_tree.tree.selection()
        pids = [self._tree_pid_of[i] for i in sel if i in self._tree_pid_of]
        if not pids:
            self._set_status("请先在进程树里选中一个进程")
            return
        children = core.build_process_tree(self.proc_map)
        victims: list[int] = []
        for pid in pids:
            for p in core.collect_descendants(pid, children) + [pid]:
                if p not in victims:
                    victims.append(p)
        if os.getpid() in victims:
            messagebox.showerror(APP_NAME, "选中范围包含 WinMon 自身，已阻止该操作。",
                                 parent=self.root)
            return
        names = [f"{self.proc_map.get(p, {}).get('name', '?')} [{p}]" for p in victims]
        preview = "\n".join("  · " + n for n in names[:18])
        if len(names) > 18:
            preview += f"\n  … 另有 {len(names) - 18} 个"
        if len(pids) == 1:
            name = self.proc_map.get(pids[0], {}).get("name", "?")
            title = f"结束进程树 — {name} [{pids[0]}]"
            head = f"将强制结束 {name} [{pids[0]}] 及其全部子进程，共 {len(victims)} 个：\n\n"
        else:
            title = f"结束进程树 — 已选 {len(pids)} 个进程"
            head = f"将强制结束选中的 {len(pids)} 个进程及其子进程，共 {len(victims)} 个：\n\n"
        if not messagebox.askyesno(title, head + preview + "\n\n未保存的数据将会丢失，确定继续吗？",
                                   icon="warning", parent=self.root):
            return
        ok_count, failures = core.terminate_process_tree_multi(victims, self.proc_map)
        target = ", ".join(
            self._log_target(self.proc_map.get(p, {}).get("name", "?"), p, "")
            for p in pids)
        result = f"成功 {ok_count}/{len(victims)}" + (f"，失败 {len(failures)}" if failures else "")
        self.log_action("结束进程树", target, result)
        self._set_status(f"结束进程树：{result}")
        if failures:
            self._set_status("部分失败：" + "；".join(failures[:3]))
        self._tree_sig = None
        self.root.after(300, self.refresh_all)

    # ------------------------------------------------------------------
    # 选择与预览
    # ------------------------------------------------------------------
    def _on_select(self, _event=None) -> None:
        if getattr(self, "_rebuilding", False):
            return
        sel = self.tree.selection()
        if not sel:
            return
        info = self.row_info.get(sel[0])
        if not info:
            return
        kind, pid, hwnd = info
        if kind == "window" and hwnd:
            self.selected_hwnd = hwnd
            self.selected_pid = pid
            if self.follow_selection.get():
                self.start_preview(hwnd)
        elif kind in ("nowin", "group"):
            self.selected_pid = pid
            self.selected_hwnd = None
            if kind == "nowin" and self.follow_selection.get():
                self.stop_preview()
                self.preview.set_title(f"{self.proc_map.get(pid, {}).get('name', 'PID')} （无窗口）")
                self.preview.set_meta(f"PID {pid} · 该进程没有可见窗口，无法捕获画面")
                self.preview.set_stats("")
                self.preview.clear("此进程没有可见窗口\n\n仍可使用下方按钮结束它。")
        self._refresh_preview_header()

    def start_preview(self, hwnd: int) -> None:
        wi = self.win_by_hwnd.get(hwnd)
        if not wi:
            return
        self.preview_key = hwnd
        self._last_preview_req = None
        title = wi.title if len(wi.title) <= 80 else wi.title[:79] + "…"
        self.preview.set_title(f"{wi.real_name}   —   {title}")
        self._refresh_preview_header()
        self._request_preview_frame(force=True)

    def stop_preview(self) -> None:
        if self.preview_key is not None:
            self.worker.forget(("win", self.preview_key))
            self.preview_key = None
        self._last_preview_req = None
        self.preview.clear("请选择左侧列表中的程序")

    def _refresh_preview_header(self) -> None:
        hwnd = self.selected_hwnd
        if not hwnd or hwnd not in self.win_by_hwnd:
            return
        wi = self.win_by_hwnd[hwnd]
        parts = [f"PID {wi.pid}", f"{wi.rect_w}×{wi.rect_h}", wi.state]
        if wi.path:
            parts.append(os.path.basename(wi.path))
        if wi.always_on_top:
            parts.append("置顶窗口")
        if wi.hwnd == self._fg_hwnd:
            parts.append("当前活动")
        self.preview.set_meta(" · ".join(parts))
        hints = []
        if wi.iconic:
            hints.append("窗口已最小化：显示的是它最小化之前的最后一帧内容。")
        if wi.hung:
            hints.append("该窗口当前无响应。")
        if wi.is_hidden_surface:
            hints.append("这是被系统隐藏的窗口，画面通常为空白。")
        if core.is_process_elevated(wi.pid) and wi.pid != os.getpid():
            hints.append("该进程权限高于本程序，若无法结束请以管理员身份重新运行 WinMon。")
        self.preview.set_hint(" ".join(hints))

    def _current_interval_s(self) -> float:
        for name, ms in FPS_CHOICES:
            if name == self.fps_choice.get():
                return ms / 1000.0
        return 0.1

    def _current_out_size(self) -> tuple[int, int]:
        for name, w_, h_ in PREVIEW_SIZES:
            if name == self.size_choice.get():
                return w_, h_
        return PREVIEW_SIZES[1][1], PREVIEW_SIZES[1][2]

    def _request_preview_frame(self, force: bool = False) -> None:
        """向捕获线程登记预览需求（参数无变化时不重复提交）。"""
        hwnd = self.preview_key
        if hwnd is None:
            return
        # P2 优化：隐藏到托盘时画面没人看，不取帧（恢复时会 force 一帧）
        try:
            if str(self.root.state()) == "withdrawn":
                return
        except Exception:
            pass
        wi = self.win_by_hwnd.get(hwnd)
        if not wi:
            return
        ow, oh = self._current_out_size()
        sw, sh = max(16, wi.rect_w), max(16, wi.rect_h)
        interval = 1.0 if self.paused else self._current_interval_s()
        req = (hwnd, ow, oh, sw, sh, round(interval, 3))
        if not force and req == self._last_preview_req:
            return
        self._last_preview_req = req
        self.worker.request(("win", hwnd), hwnd, ow, oh, sw, sh, interval)

    def _on_tab_changed(self) -> None:
        idx = self.notebook.index("current")
        if idx == 1:
            self._sync_gallery_requests()
        elif idx == 2:
            self.refresh_process_tree(force=True)
        elif idx == 3:
            self._ensure_catalog_loaded()
        elif idx == 4:
            self.workspace.refresh()          # 工作区：进页面时刷新（可能有窗口开/关了）
        else:
            self._request_preview_frame(force=True)

    def _ensure_catalog_loaded(self) -> None:
        """程序目录按需加载：首次切到该页时后台构建，之后只做轻量刷新。"""
        if self.catalog.items:
            self.catalog.apply_filter()
            return
        self.catalog._progress("正在扫描程序目录…")
        self.catalog.load_async(on_done=lambda items: self._set_status(
            f"程序目录已就绪：共 {len(items)} 个程序（"
            f"{len([i for i in items if i.runnable])} 个可启动）"))

    # ------------------------------------------------------------------
    # 实时总览
    # ------------------------------------------------------------------
    def on_gallery_reordered(self, order: list) -> None:
        """
        总览墙被拖拽重排后调这里（B.3）：同步顺序并**记进设置**。

        `order` 是"格子 key"（就是 hwnd）的新顺序。用户下次打开程序时，
        用 `gallery_order_saved`（存的是**窗口标题**，因为 hwnd 每次都不一样）
        把先前的顺序排回来。
        """
        new_order = [k for k in order if k in self.gallery_keys]
        if new_order == self.gallery_order:
            return
        self.gallery_order = new_order
        # 存"标题顺序"而不是 hwnd：hwnd 是临时的，重启后对不上
        self.gallery_order_saved = [self.gallery_keys.get(k, "") for k in new_order]
        self._schedule_save_settings()
        self._set_status(f"实时总览顺序已记住（{len(new_order)} 个）")

    def _gallery_apply_saved_order(self) -> None:
        """
        把设置里记下的总览顺序**应用到当前的格子**上（B.3）。

        为什么在窗口刷新之后做：`refresh_windows()` 会重建 `gallery_keys`（按枚举顺序），
        这里按记过的标题顺序把它重排一遍，界面才会维持用户拖出来的样子。
        """
        if not getattr(self, "gallery_order_saved", None):
            return
        if not self.gallery_keys:
            return
        want = []
        for title in self.gallery_order_saved:
            for hwnd, cur in self.gallery_keys.items():
                if cur == title and hwnd not in want:
                    want.append(hwnd)
                    break
        rest = [h for h in self.gallery_keys if h not in want]
        order = want + rest
        if order == list(self.gallery_keys):
            return
        self.gallery_keys = {h: self.gallery_keys[h] for h in order}
        self.gallery_order = list(self.gallery_keys)
        self.gallery.tiles = {h: self.gallery.tiles[h] for h in order
                              if h in self.gallery.tiles}
        self.gallery.relayout()

    def gallery_add_selected(self) -> None:
        sel = [s for s in self.tree.selection() if self.row_info.get(s, ("",))[0] == "window"]
        if not sel:
            self._set_status("请先在左侧列表中选择一个或多个程序（Ctrl / Shift 多选）")
            return
        added = 0
        for item in sel:
            _kind, pid, hwnd = self.row_info[item]
            if not hwnd or hwnd in self.gallery_keys:
                continue
            wi = self.win_by_hwnd.get(hwnd)
            title = wi.real_name if wi else f"PID {pid}"
            self.gallery_keys[hwnd] = title
            self.gallery_order.append(hwnd)
            mem = core._fmt_mem(wi.mem_kb) if wi else ""
            self.gallery.add(hwnd, f"{title} [{pid}]", mem)
            added += 1
        if added:
            self._set_status(f"已加入 {added} 个程序到实时总览")
            self._sync_gallery_requests()
            self.notebook.select(1)

    def gallery_remove_selected(self) -> None:
        sel = [s for s in self.tree.selection() if self.row_info.get(s, ("",))[0] == "window"]
        removed = 0
        for item in sel:
            _kind, _pid, hwnd = self.row_info[item]
            if hwnd in self.gallery_keys:
                self.gallery_keys.pop(hwnd, None)
                if hwnd in self.gallery_order:
                    self.gallery_order.remove(hwnd)
                self.gallery.remove(hwnd)
                self.worker.forget(("tile", hwnd))
                removed += 1
        if removed:
            self._set_status(f"已从总览移除 {removed} 个程序")

    def _sync_gallery_requests(self) -> None:
        # P2 优化：隐藏到托盘时总览没人看，先停掉所有格子的取帧请求
        try:
            if str(self.root.state()) == "withdrawn":
                for hwnd in self.gallery_keys:
                    self.worker.forget(("tile", hwnd))
                return
        except Exception:
            pass
        if self.notebook.index("current") != 1:
            for hwnd in self.gallery_keys:
                self.worker.forget(("tile", hwnd))
            return
        if not (self.live_gallery.get() and not self.paused):
            for hwnd in self.gallery_keys:
                self.worker.forget(("tile", hwnd))
            return
        tw, th = max(16, TILE_W), max(16, TILE_H - 40)
        for hwnd in self.gallery_keys:
            wi = self.win_by_hwnd.get(hwnd)
            if not wi:
                continue
            self.worker.request(("tile", hwnd), hwnd, tw, th,
                                max(16, wi.rect_w), max(16, wi.rect_h), 0.5)

    # ------------------------------------------------------------------
    # 帧泵
    # ------------------------------------------------------------------
    def _ui_call(self, fn) -> None:
        """
        让**工作线程**安全地把一件事交回主线程去做。

        为什么不能直接 `self.root.after(0, fn)`（2026-10-13 修的真缺陷）：
        从非主线程调 tkinter 要靠 Tcl 的线程编组，主线程不在 `mainloop()` 里派发时
        会抛 `RuntimeError: main thread is not in main loop`；而这个异常发生在
        **工作线程内部**，只会把它静默弄死 —— 实测表现是程序目录面板永远停在
        "正在扫描程序目录…"，没有任何提示（`tests/shot.py`、自动化驱动、
        以及"定时器正在跑但主循环刚好在退出"的时刻都会碰到）。
        这里统一走一条队列，由主线程的定时泵 `_pump_frames`（10ms 一次）取走执行，
        与项目里其它后台线程（窗口操作 / 媒体读取）的做法一致。
        """
        if threading.current_thread() is threading.main_thread():
            fn()                       # 主线程直接做，不必绕队列
            return
        try:
            self._ui_msg.put_nowait(fn)
        except Exception:
            pass

    def _drain_ui_msgs(self) -> None:
        """把工作线程交回来的界面动作在主线程里执行（见 `_ui_call`）。"""
        for _ in range(self.MAX_DRAIN):
            try:
                fn = self._ui_msg.get_nowait()
            except queue.Empty:
                return
            try:
                fn()
            except Exception as exc:
                self._set_status(f"界面回调出错：{type(exc).__name__}: {exc}")

    def _pump_frames(self) -> None:
        for _ in range(self.MAX_DRAIN):
            try:
                key, frame, error = self.worker.result_queue.get_nowait()
            except queue.Empty:
                break
            try:
                self._apply_frame(key, frame, error)
            except Exception as exc:
                self._set_status(f"画面刷新出错：{type(exc).__name__}: {exc}")
        self._drain_window_op_msgs()
        self._drain_media_msgs()
        self._drain_ui_msgs()
        self._request_preview_frame()
        self._pump_counter += 1
        if self._pump_counter % 40 == 0:
            self._sync_gallery_requests()
            self._update_overview_text()
        self.root.after(10, self._pump_frames)

    def _apply_frame(self, key, frame, error) -> None:
        kind, ident = key
        if kind == "win":
            if ident != self.preview_key:
                return
            if error or frame is None:
                self.preview.set_stats("取帧失败")
                if error:
                    self.preview.set_hint(f"无法捕获画面：{error}")
                return
            raw, ow, oh = frame
            # 抽样 + zoom 放大：把 Python 侧像素生成量降到 1/scale²
            scale = 2 if ow >= 240 else 1
            t0 = time.perf_counter()
            data = core.bgr_to_tk_hex_data(raw, ow, oh, scale)
            t1 = time.perf_counter()
            self.preview.set_frame(data, (ow + scale - 1) // scale,
                                   (oh + scale - 1) // scale, scale)
            t2 = time.perf_counter()
            self._last_capture_cost = (t1 - t0) * 1000
            self._record_frame()
            wi = self.win_by_hwnd.get(ident)
            name = wi.real_name if wi else f"PID {ident}"
            self.preview.set_stats(
                f"{ow}×{oh} · 生图{self._last_capture_cost:.0f}ms · "
                f"上屏{(t2 - t1) * 1000:.0f}ms · {name}")
            self._update_overview_text()
        elif kind == "tile":
            if ident not in self.gallery_keys:
                return
            if error or frame is None:
                self.gallery.set_error(ident, error or "取帧失败")
                return
            raw, ow, oh = frame
            data = core.bgr_to_tk_hex_data(raw, ow, oh, 1)
            self.gallery.set_frame(ident, data, ow, oh)
            self._record_frame()

    def _record_frame(self) -> None:
        now = time.monotonic()
        self._fps_window.append(now)
        if len(self._fps_window) > 60:
            del self._fps_window[:-60]

    def _current_fps(self) -> float:
        if len(self._fps_window) < 2:
            return 0.0
        recent = [t for t in self._fps_window if t > time.monotonic() - 2.0]
        if len(recent) < 2:
            return 0.0
        span = recent[-1] - recent[0]
        return (len(recent) - 1) / span if span > 0 else 0.0

    # ------------------------------------------------------------------
    # 周期刷新与关闭监视
    # ------------------------------------------------------------------
    #: 闲置计时每一拍按多少秒累计。用固定值而不是"两次刷新的真实间隔"，
    #: 是为了让"30 分钟"就是实打实的 30 分钟 —— 界面卡顿/挂起不会把计时算歪。
    IDLE_TICK_SECONDS = 2.0

    def _periodic_refresh(self) -> None:
        try:
            core._PROC_POOL.purge_dead()      # 顺手回收已退出进程的句柄
            self.refresh_processes()
            self.refresh_windows()
            self._refresh_preview_header()
            if self.notebook.index("current") == 2:
                self.refresh_process_tree()
            self._request_preview_frame()
            self._check_idle(self.IDLE_TICK_SECONDS)
            # AI 工作伴侣：识别主 AI + 本地事件（只读、0 token；提醒走托盘气泡）
            self._ai_tick()
            # 媒体会话：比界面刷新慢一拍（它要起 PowerShell，约 290ms，在后台线程做）
            now = time.monotonic()
            if now - self._media_last >= MEDIA_POLL_SECONDS:
                self._media_last = now
                self._refresh_media()
            # P1-5 看门狗：助手 -Watch 1800s 到期/崩溃后快照会过期，这里把它拉起来。
            # start_media_snapshot_helper 自带判重；快照只要助手活着就每 3 秒刷新，
            # 所以"读不到新鲜快照"就是真死了，直接重启它（间隔 30s 查一次）。
            wall = time.time()
            if wall - self._media_helper_check >= 30.0:
                self._media_helper_check = wall
                try:
                    if core.read_media_snapshot() is None:
                        core.start_media_snapshot_helper()
                except Exception:
                    pass
        except Exception as exc:
            self._set_status(f"刷新失败：{exc}")
        finally:
            self.root.after(2000, self._periodic_refresh)

    def _tick_close_watch(self) -> None:
        """
        检查「优雅关闭」的窗口是否真的退出，超时未退出则提示改用强制结束。

        同时看「窗口是否还在」与「进程是否还活着」：有的程序响应关闭后会先销毁窗口、
        进程稍后才退出，只看窗口会误报"已退出"。
        """
        now = time.monotonic()
        still = []
        for hwnd, pid, t0, warned in self._close_watch:
            window_gone = not core.user32.IsWindow(core.w.HWND(hwnd))
            process_gone = not core.is_process_alive(pid)
            name = self.proc_map.get(pid, {}).get("name", f"PID {pid}")
            if window_gone and process_gone:
                self._set_status(f"窗口与进程均已正常退出（PID {pid}）")
                self.log_action("关闭窗口", self._log_target(name, pid), "已退出（优雅）")
                continue
            if window_gone and not process_gone:
                if now - t0 < 10.0:
                    still.append((hwnd, pid, t0, warned))
                    continue
                if not warned:
                    self._set_status(f"窗口已关闭，但 PID {pid} 进程仍在后台运行。")
                    self.log_action("关闭窗口", self._log_target(name, pid), "窗口关闭、进程仍在")
                still.append((hwnd, pid, t0, True))
                continue
            if now - t0 < 3.0:
                still.append((hwnd, pid, t0, warned))
                continue
            if not warned:
                self._set_status(f"PID {pid} 未响应关闭请求；如需要请点击「结束进程」强制终止。")
                self.log_action("关闭窗口", self._log_target(name, pid), "未响应（超时）")
            still.append((hwnd, pid, t0, True))
        self._close_watch = still
        self.root.after(400, self._tick_close_watch)
    def _monitor_current_window(self) -> None:
        """
        监视**列表里选中的那个窗口**：先判断它是不是在放视频，是就开始监视。

        这是"不依赖特定 App"的那条路（原来的「打开视频并监视播放」只认哔哩哔哩客户端，
        而客户端里的卡片又点不动，等于两头不通）。现在你选中什么就判断什么：
        视频客户端、浏览器、本地播放器都行；判不出来就如实说不像视频，不假装成功。
        """
        wi = self._selected_window()
        if wi is None:
            self._set_status("请先在左侧列表里选中一个窗口（视频客户端 / 浏览器 / 播放器都行）")
            return
        # 目标最小化（或被隐藏到托盘）时先唤起：隐藏窗口抓不到画面，监视等于白跑
        try:
            visible = bool(core.user32.IsWindowVisible(core.w.HWND(wi.hwnd)))
        except Exception:
            visible = True
        if wi.iconic or not visible:
            ok, msg = core.restore_window(wi)
            self._set_status(msg)
            self.log_action("唤起窗口", self._log_target(wi.proc or wi.real_name, wi.pid, wi.title),
                            msg, open_time=core.format_open_time(wi.pid))
        try:
            probe_seconds = max(1.0, float(self.video_probe_seconds.get() or 2.0))
        except (TypeError, ValueError):
            probe_seconds = 2.0
        self._set_status(f"正在判断「{wi.real_name}」是否在放视频…")

        def worker():
            try:
                probe = core.probe_window_as_video(wi, probe_seconds=probe_seconds)
            except Exception as exc:
                msg = f"判断失败：{type(exc).__name__}: {exc}"
                self._ui_call(lambda: self._set_status(msg))
                return
            self._ui_call(lambda: self._on_probe_result(probe, wi))

        import threading
        threading.Thread(target=worker, daemon=True, name="WinMon-Probe").start()

    def _on_probe_result(self, probe: dict, wi) -> None:
        is_video = bool(probe.get("is_video"))
        self.log_action("识别窗口", f"{wi.real_name} [{wi.pid}]", probe.get("reason", ""))
        if not is_video:
            self.video_watch = False
            self._set_status(f"没有开始监视：{probe.get('reason', '')}")
            return
        self.video_target = wi
        self.video_watch = True
        self._set_status(f"{probe.get('reason', '')}；开始监视（检测到播放再关闭会先问你）")
        self._schedule_video_check(0.5)

    def _target_allowed(self, wi) -> tuple[bool, str]:
        """
        现在允许对**这个窗口**做出反应吗？

        用户已经删掉「仅监视视频类 App / 指定 App」那两个筛选选项
        （视频自动识别那套一并放弃），所以这里**一律放行**：由用户自己用
        「监视当前窗口」指定要看哪一个，WinMon 不再替他筛。

        保留这个函数（而不是把调用点也删掉）是为了让"以后想加回筛选"只有一处要改。
        """
        return True, ""

    def _looks_like_player(self, wi: core.WinInfo) -> bool:
        """这个窗口像不像"在放视频"的播放器（用进程类型 + 标题双重判断）。"""
        try:
            path, _ = core.query_process_detail(wi.pid)
        except Exception:
            path = ""
        _app, kind = core.classify_video_app(wi.proc, path)
        return kind != "unknown" or core.looks_like_video_title(wi.title)

    def _video_check_playing(self) -> None:
        """后台检测是否在播放；播放中则回主线程弹窗询问。"""
        wi = self.video_target
        if not self.video_watch or wi is None or not core.is_process_alive(wi.pid):
            self.video_watch = False
            return
        if self._video_dialog_open:
            return
        # **采样时长在主线程先读出来**：tk 变量只能主线程碰（工作线程读它会踩
        # Tcl 的线程编组，轻则阻塞、重则抛 RuntimeError）
        try:
            probe_seconds = float(self.video_probe_seconds.get())
        except Exception:
            probe_seconds = 3.0

        def worker():
            try:
                det = core.VideoActivityDetector(wi)
                try:
                    playing, info = det.is_playing(probe_seconds)
                finally:
                    det.close()
            except Exception as exc:
                msg = f"播放检测失败：{exc}"
                self._ui_call(lambda: self._set_status(msg))
                self._ui_call(lambda: self._schedule_video_check(2.5))
                return
            self._ui_call(lambda: self._on_video_detected(playing, info))

        import threading
        threading.Thread(target=worker, daemon=True, name="WinMon-VideoCheck").start()

    def _on_video_detected(self, playing: bool, info: dict) -> None:
        wi = self.video_target
        if not self.video_watch or wi is None:
            return
        if not playing:
            self._set_status(f"等待视频开始播放…（{info.get('reason', '')}）")
            self._schedule_video_check(2.5)
            return
        # 弹窗前再确认一次筛选设置：用户中途改了「仅视频类 App / 指定 App」，
        # 已经在跑的那次监视也要立刻遵守（否则"不影响其他"就是空话）。
        allowed, why_not = self._target_allowed(wi)
        if not allowed:
            self.video_watch = False
            self._set_status(f"检测到播放，但按当前筛选设置不响应：{why_not}")
            self.log_action("关闭视频", f"{wi.real_name} [{wi.pid}]",
                            f"按筛选设置跳过：{why_not}")
            return
        self._video_dialog_open = True
        title = (info.get("title") or "").strip() or "（未能读到视频标题）"
        mode = _close_mode_slug(self.video_close_mode.get())
        # 弹窗必须写清楚"点是之后到底会发生什么"：
        # 默认是关闭**当前窗口**（而不是只暂停/只返回），按钮上就照着写。
        ask, yes_text = _video_dialog_text(mode)
        self._set_status(f"检测到视频播放中：{title[:40]}")
        self.log_action("识别播放", f"{wi.real_name} [{wi.pid}]", info.get("reason", ""))
        try:
            dlg = ConfirmDialog(
                self.root,
                f"视频播放中 — {ask}",
                f"检测到视频正在播放，{ask}",
                detail=(f"窗口：{wi.real_name}\n视频：{title}\n"
                        f"判定依据：{info.get('reason', '')}\n\n"
                        f"点「是」= {_close_mode_label(mode)}；点「否」= 继续播放\n"
                        + ("（若该程序只是把窗口最小化到托盘，会改为结束进程 —— "
                           "否则视频会在后台继续播放）\n" if mode == "window" else "")
                        + "（视频会继续播放，慢慢选即可）"),
                yes_text=yes_text)
            answer = dlg.show()
        finally:
            self._video_dialog_open = False
        if not self.video_watch:
            return
        if answer:
            # P1-2 修复：关闭动作（4s 轮询核对 + 可能升级结束进程树）移到后台线程。
            # 先停监视，避免关闭过程中又触发一次播放检测。
            self.video_watch = False
            self._stop_video_in_background(wi, mode)
        else:
            self.log_action("关闭视频", self._log_target(wi.proc or wi.real_name, wi.pid, wi.title),
                            "用户选择继续播放",
                            open_time=core.format_open_time(wi.pid))
            self._set_status("已选择继续播放；可再次点「监视当前窗口」重新开始监视")
            self.video_watch = False

    def _schedule_video_check(self, seconds: float) -> None:
        if not self.video_watch:
            return
        if self._video_after_id:
            try:
                self.root.after_cancel(self._video_after_id)
            except Exception:
                pass
        self._video_after_id = self.root.after(int(seconds * 1000), self._video_check_playing)

    def stop_video_watch(self) -> None:
        self.video_watch = False
        if self._video_after_id:
            try:
                self.root.after_cancel(self._video_after_id)
            except Exception:
                pass
            self._video_after_id = None
        self._set_status("已停止视频监视")

    def action_clean_files(self) -> None:
        """清理程序自己产生的文件（弹窗里逐项勾选，见 `_CleanupDialog` 的安全约定）。"""
        try:
            items = core.list_cleanable_files()
        except Exception as exc:
            self._set_status(f"统计可清理文件失败：{type(exc).__name__}: {exc}")
            return
        if not any(d["exists"] for d in items):
            self._set_status("没有需要清理的文件（程序目前是干净的）")
            return
        dlg = _CleanupDialog(self.root, items)
        keys = dlg.show()
        if keys is None:
            self._set_status("已取消清理")
            return
        if not keys:
            self._set_status("没有勾选任何清理项（设置文件是你的数据，默认不勾）")
            return
        removed, freed, failures = core.clean_files(keys)
        msg = f"已清理 {removed} 个文件，释放 {freed / 1024:.0f} KB"
        if failures:
            msg += f"；{len(failures)} 项失败（{failures[0]}）"
        self._set_status(msg)
        self.log_action("清理文件", f"{len(keys)} 类 / {removed} 个文件", msg)

    def _stop_video_in_background(self, wi, mode: str) -> None:
        """
        后台执行「停止/关闭视频」（P1-2 修复）。

        `close_video_window` 要轮询最长 4 秒核对，还可能升级为结束整棵进程树；
        以前直接在主线程跑，用户点完"是，关闭窗口"界面会冻住好几秒。
        结果经 `_ui_call` 回主线程写状态栏与操作历史。
        """
        import threading
        self._set_status(f"正在执行「{_close_mode_label(mode)}」：{wi.real_name}…")

        def worker():
            # 先取打开时间：进程结束后就取不到了
            opened = core.format_open_time(wi.pid)
            ok, msg = core.stop_video_in_window(wi, mode=mode)

            def done():
                self._set_status(f"{msg}（{wi.real_name}）")
                self.log_action(
                    "关闭视频",
                    self._log_target(wi.proc or wi.real_name, wi.pid, wi.title),
                    f"{_close_mode_label(mode)}：{msg}",
                    open_time=opened)

            self._ui_call(done)

        threading.Thread(target=worker, daemon=True, name="WinMon-VideoStop").start()

    def action_stop_video(self) -> None:
        """
        直接停止当前视频（不弹窗，用于手动干预）。

        来源优先用**正在监视的那个窗口**；没有的话退到"列表里选中的窗口"
        （不再有"自动挑视频来源"那套 —— 用户已放弃自动识别/自动打开）。
        """
        wi = self.video_target
        if wi is None:
            candidate = self._selected_window()
            if candidate is not None and self._looks_like_player(candidate):
                wi = candidate
        if wi is None:
            self._set_status("没有视频来源窗口：请先在左侧选中那个播放器/浏览器窗口")
            return
        mode = _close_mode_slug(self.video_close_mode.get())
        self.video_watch = False
        self._stop_video_in_background(wi, mode)

    # ------------------------------------------------------------------
    # 动作
    # ------------------------------------------------------------------
    def _selected_window(self) -> core.WinInfo | None:
        if self.selected_hwnd and self.selected_hwnd in self.win_by_hwnd:
            return self.win_by_hwnd[self.selected_hwnd]
        return None

    def hwnd_of_pid(self, pid: int) -> int:
        """按 PID 找它的可见窗口句柄（用于把已运行的程序切到前台）。"""
        for wi in self.windows:
            if wi.pid == pid and not wi.is_hidden_surface:
                return wi.hwnd
        return 0

    def action_show_preview(self) -> None:
        wi = self._selected_window()
        if not wi:
            self._set_status("请选择一个有窗口的程序")
            return
        self.notebook.select(0)
        self.start_preview(wi.hwnd)

    def action_bring_front(self) -> None:
        """
        把选中的窗口切到前台（工具条按钮 / 右键菜单 / 预览区双击都走这里）。

        改成走后台队列 + `core.activate_window`（组合拳 + 回读核对 + 重试）——
        原来这里是裸调 `SetForegroundWindow`，而它受前台锁定限制**会静默失败**，
        用户看到的就是"点了没反应"。
        """
        wi = self._selected_window()
        if not wi:
            self._set_status("请选择左侧列表中的一个程序")
            return
        self._queue_window_op(CLICK_ACTIVATE, wi.hwnd)

    def action_minimize_window(self) -> None:
        """最小化选中的窗口；已经最小化的就还原（右键菜单，与双击列表同一套规则）。"""
        wi = self._selected_window()
        if not wi:
            self._set_status("请选择左侧列表中的一个程序")
            return
        self._run_click_action(
            plan_window_click("double", self._window_is_iconic(wi.hwnd)), wi)

    def action_close_window(self) -> None:
        wi = self._selected_window()
        if not wi:
            self._set_status("请选择左侧列表中的一个程序")
            return
        ok, msg = core.graceful_close_window(wi.hwnd)
        target = self._log_target(wi.proc or wi.real_name, wi.pid, wi.title)
        if ok:
            self._close_watch.append((wi.hwnd, wi.pid, time.monotonic(), False))
            self._set_status(f"已向「{wi.title[:30]}」发送关闭请求（等同点击窗口关闭按钮）")
            self.log_action("关闭窗口", target, "已请求")
        else:
            self._set_status(msg)
            self.log_action("关闭窗口", target, f"失败：{msg}")

    def action_terminate(self) -> None:
        self._terminate_selected(tree=False)

    def action_terminate_tree(self) -> None:
        self._terminate_selected(tree=True)

    def _terminate_selected(self, tree: bool) -> None:
        pid = self.selected_pid
        name = ""
        if pid and pid in self.proc_map:
            name = self.proc_map[pid]["name"]
        elif self.selected_hwnd and self.selected_hwnd in self.win_by_hwnd:
            name = self.win_by_hwnd[self.selected_hwnd].proc
        if not pid:
            self._set_status("请选择左侧列表中的一个程序")
            return
        if pid == os.getpid():
            messagebox.showwarning(APP_NAME, "不能结束 WinMon 自己。", parent=self.root)
            return
        low = name.lower()
        if low in core.CRITICAL_PROCESSES:
            messagebox.showerror(
                APP_NAME,
                f"「{name}」(PID {pid}) 是 Windows 关键系统进程。\n\n"
                "结束它会导致系统蓝屏、桌面崩溃或立即重启，WinMon 已阻止此操作。",
                parent=self.root)
            self._set_status(f"已阻止结束关键系统进程 {name}")
            self.log_action("结束进程", f"{name} [{pid}]", "已阻止（关键系统进程）")
            return
        extra = ""
        if low in core.RISKY_PROCESSES:
            extra = ("\n\n注意：这是系统外壳 / 输入相关进程，结束它会让任务栏、开始菜单"
                     "或输入法暂时消失（通常会自动重启）。")
        if core.is_process_elevated(pid):
            extra += "\n\n该进程以管理员权限运行，可能需要在管理员模式下运行 WinMon 才能结束它。"
        wi = self.win_by_hwnd.get(self.selected_hwnd) if self.selected_hwnd else None
        target = self._log_target(name or "?", pid, wi.title if wi else "")
        scope = "该进程及其全部子进程" if tree else "该进程"
        win_title = "(无)"
        if wi:
            win_title = wi.title[:50] + "…" if len(wi.title) > 50 else wi.title
        msg = (f"确定要强制结束{scope}吗？\n\n"
               f"程序：{name or '(未知)'}\nPID：{pid}\n窗口：{win_title}\n\n"
               f"未保存的数据将会丢失。{extra}")
        if not messagebox.askyesno(f"结束进程 — {name or pid}", msg,
                                   icon="warning", parent=self.root):
            return
        if tree:
            killed, failures = core.terminate_process_tree(pid, self.proc_map)
            result = f"成功 {killed} 个" + (f"，失败 {len(failures)}" if failures else "")
            self._set_status(f"结束进程树：{result}")
            self.log_action("结束进程树", target, result)
            if failures:
                self._set_status("部分失败：" + "；".join(failures[:3]))
        else:
            ok, msg2, _err = core.terminate_process(pid)
            self._set_status(f"{'成功' if ok else '失败'}：{msg2}（{name or pid}）")
            self.log_action("结束进程", target,
                            "成功" if ok else f"失败：{msg2}")
        self.root.after(120, self.refresh_all)

    def action_reveal(self) -> None:
        wi = self._selected_window()
        path = wi.path if wi else self._path_cache.get(self.selected_pid or 0, "")
        if not path:
            self._set_status("未获取到程序路径")
            return
        core.reveal_in_explorer(path)
        self._set_status(f"已在资源管理器中定位：{path}")

    def toggle_pause(self) -> None:
        self.paused = not self.paused
        self.preview.btn_pause.configure(text="继续刷新" if self.paused else "暂停刷新")
        self._set_status("已暂停画面刷新" if self.paused else "已恢复画面刷新")
        self._last_preview_req = None
        self._sync_gallery_requests()

    def _copy(self, text: str) -> None:
        if not text:
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        self._set_status(f"已复制：{text[:60]}")

    def _copy_selected_title(self) -> None:
        wi = self._selected_window()
        self._copy(wi.title if wi else "")

    def _copy_selected_path(self) -> None:
        wi = self._selected_window()
        self._copy(wi.path if wi else "")

    def _on_right_click(self, event) -> None:
        item = self.tree.identify_row(event.y)
        if item:
            if item not in self.tree.selection():
                self.tree.selection_set(item)
            self._on_select()
            try:
                self.menu.tk_popup(event.x_root, event.y_root)
            finally:
                self.menu.grab_release()

    def on_close(self) -> None:
        """
        点右上角 ×（或 Alt+F4）时的行为 —— 由设置里的「关闭方式」决定。

        用户的要求：「关闭改为隐藏至最小托盘或结束进程，**默认为后者**」
        （"后者"指"结束进程"）。但"隐藏到托盘"更符合"监控 + 放音乐"的使用场景，
        所以设置框里把托盘那条标成"推荐"，默认值仍然按用户说的走
        （`CLOSE_MODE_DEFAULT`，见测试钉住）。
        """
        mode = self.close_mode.get() if hasattr(self, "close_mode") else CLOSE_MODE_EXIT
        if mode == CLOSE_MODE_TRAY:
            ok, msg = self._hide_to_tray()
            if ok:
                self._set_status(msg)
                return
            self._set_status(f"{msg} —— 已改为直接退出")
        self.quit_app()

    def _setup_tray(self) -> bool:
        """
        把托盘图标装好（消息窗口 + 图标），供"关闭到托盘 / 启动时收进托盘"使用。

        **启动时就做**（不再等用户第一次点 ×）：这样"隐藏到托盘"是零延迟的，
        而且用户能一眼看到 WinMon 在托盘里、右键就能退出。
        返回是否就绪；失败时把原因记在 `_tray_error`，`on_close` 会据此退回"结束进程"。
        """
        self._tray_error = ""
        if not self._tray_available():
            self._tray_error = "托盘不可用（Shell_NotifyIcon 加载失败）"
            return False
        try:
            hwnd = self._tray_hwnd()
            if not hwnd:
                self._tray_error = "拿不到主窗口句柄"
                return False
            self._tray_window = core.TrayMessageWindow(hwnd)
            ok, msg = self._tray_window.install()
            if not ok:
                self._tray_window = None
                self._tray_error = msg
                return False
            tray = core.TrayIcon(hwnd, _icon_path(), "WinMon")
            ok2, msg2 = tray.add()
            if not ok2:
                # 图标加不上就把窗口过程恢复回去，别留一个半成品
                try:
                    self._tray_window.uninstall()
                finally:
                    self._tray_window = None
                self._tray_error = msg2
                return False
            self._tray = tray
            self._tray_ok = True
            self._tray_polling = True
            self._schedule_tray_poll()
            return True
        except Exception as exc:
            self._tray_error = f"{type(exc).__name__}: {exc}"
            self._tray_ok = False
            return False

    def _start_hidden_to_tray(self) -> None:
        """
        「启动时直接收进托盘」：把主窗口藏掉（图标已经在托盘里了）。

        只在托盘确实可用时才会被调用（见 `__init__`）——
        否则用户点开程序却看不到界面，会以为程序没启动。
        """
        try:
            self.root.withdraw()
            self._set_status("已启动并在托盘里运行（双击托盘图标显示窗口）")
        except Exception:
            pass

    def _hide_to_tray(self) -> tuple[bool, str]:
        """隐藏主窗口 + 放入托盘。失败会**把窗口放回来**（不能让它凭空消失）。"""
        try:
            if not self._tray_available():
                return False, "托盘不可用"
            if not self._tray_ok:
                if not self._setup_tray():
                    return False, self._tray_error or "托盘初始化失败"
            self.root.withdraw()
            self._tray_polling = True
            self._schedule_tray_poll()
            return True, "已隐藏到托盘（双击图标恢复窗口，右键出菜单）"
        except Exception as exc:
            try:
                self.root.deiconify()
            except Exception:
                pass
            return False, f"隐藏到托盘失败：{type(exc).__name__}: {exc}"

    def _tray_available(self) -> bool:
        try:
            core.shell32.Shell_NotifyIconW
            core.user32.CreateWindowExW
            return True
        except Exception:
            return False

    def _tray_hwnd(self) -> int:
        """主窗口的顶层句柄（托盘菜单选择会转发到它）。"""
        try:
            return int(core.user32.GetAncestor(
                core.w.HWND(self.root.winfo_id()), core.GA_ROOT) or self.root.winfo_id())
        except Exception:
            try:
                return int(self.root.winfo_id())
            except Exception:
                return 0

    def _poll_tray(self) -> None:
        """
        取走托盘消息窗口转发过来的命令（显示 / 退出）。

        为什么是"轮询取消息"而不是让 Tk 处理：托盘回调落在我们自己的消息窗口里，
        Tk 的消息循环看不到它。所以托盘窗口把用户的选择 `PostMessage` 到主窗口，
        这里每隔 400ms 用 `PeekMessageW` 把它取走 —— 简单、可靠、不动 Tk 的循环。
        """
        self._tray_after_id = None          # 本次回调已经触发，挂起标记作废
        if not self._tray_polling:
            return
        try:
            for cmd in (self._tray_window.pump() if self._tray_window else []):
                if cmd == core.IDM_TRAY_EXIT:
                    self._tray_polling = False
                    self.quit_app()
                    return
                if cmd == core.IDM_TRAY_SHOW:
                    self.restore_from_tray()
                elif cmd == core.IDM_TRAY_AI_USAGE:
                    self.action_ai_usage()
        except Exception as exc:
            self._set_status(f"托盘消息处理失败：{type(exc).__name__}: {exc}")
        if self._tray_polling:
            self._schedule_tray_poll()

    def _schedule_tray_poll(self) -> None:
        """
        排下一次托盘轮询 —— **只在没有挂起的时候排**。

        为什么必须这样（真缺陷）：`_hide_to_tray()` 以前无条件
        `after(400, _poll_tray)`，而 `_poll_tray` 自己也会接着排。于是每
        "隐藏 → 恢复 → 再隐藏"一次就多一条轮询链，托盘消息的处理频率成倍增加
        （用户看不出来，但定时器越积越多）。现在只保留一条。
        """
        if getattr(self, "_tray_after_id", None) is not None:
            return
        try:
            self._tray_after_id = self.root.after(400, self._poll_tray)
        except Exception:
            self._tray_after_id = None

    def restore_from_tray(self) -> None:
        """
        从托盘恢复窗口。

        **图标保留**：现在托盘图标是启动就常驻的（用户随时能看到 WinMon 在运行），
        所以"显示窗口"不该把图标摘掉 —— 摘掉之后下次点 × 还得重新装一遍。
        """
        try:
            self.root.deiconify()
            self.root.lift()
            self.root.focus_force()
        except Exception:
            pass
        self._set_status("已从托盘恢复")
        # 隐藏期间预览是跳过的：恢复显示后立刻补一帧
        self._last_preview_req = None
        self._request_preview_frame(force=True)

    def _teardown_tray(self) -> None:
        """退出前收拾托盘：停轮询 + 摘图标 + 恢复被接管的窗口过程。"""
        if getattr(self, "_tray_after_id", None) is not None:
            try:
                self.root.after_cancel(self._tray_after_id)
            except Exception:
                pass
            self._tray_after_id = None
        if self._tray is not None:
            try:
                self._tray.remove()
            except Exception:
                pass
            self._tray = None
        if self._tray_window is not None:
            try:
                self._tray_window.uninstall()
            except Exception:
                pass
            self._tray_window = None
        self._tray_ok = False
        self._tray_polling = False

    def _cancel_pending_after(self) -> None:
        """
        取消所有还没触发的 `after` 定时器 —— 销毁窗口**之前**必须做。

        不做会怎样（实测）：`root.destroy()` 之后 Tcl 还会派发这些定时器，
        向 stderr 打印一串 `invalid command name "..._pump_frames"` / `"..._poll_tray"`
        —— 从控制台启动、退出时就会看到这些红字。它们都是"每 10~1200ms 再排一次"
        的常驻循环，窗口都销毁了，收掉才是正确语义。
        """
        try:
            pending = self.root.tk.splitlist(self.root.tk.call("after", "info"))
        except Exception:
            return
        for after_id in pending:
            try:
                self.root.after_cancel(after_id)
            except Exception:
                pass

    def quit_app(self) -> None:
        """真正退出：收托盘图标 → 停常驻助手 → 存设置 → 收线程与句柄 → 销毁窗口。"""
        self._teardown_tray()
        # 收掉我们起的那个常驻媒体助手（只收自己起的那个）
        try:
            core.stop_media_snapshot_helper()
        except Exception:
            pass
        try:
            self.save_settings_now()
        except Exception:
            pass
        # 悬停提示条是独立的 Toplevel，得显式收掉，否则会留下一个孤儿小窗
        try:
            self._row_tip.destroy()
        except Exception:
            pass
        try:
            core._PROC_POOL.close_all()
        except Exception:
            pass
        try:
            self.worker.shutdown()
        except Exception:
            pass
        self._cancel_pending_after()
        self.root.destroy()

    def action_settings(self) -> None:
        """
        打开**独立设置界面**。

        目前四项（用户点名要的）：开机自启动 / 点 × 的行为 /
        启动时直接收进托盘 / 操作历史日志保留天数。
        """
        dlg = _SettingsDialog(self.root, self.close_mode.get(), self._tray_available(),
                              bool(self.start_minimized.get()),
                              int(self.history_days.get()))
        got = dlg.show()
        if not got:
            self._set_status("设置已取消（未做任何改动）")
            return
        self.close_mode.set(got["close_mode"])
        self.start_minimized.set(bool(got["start_minimized"]))
        self.history_days.set(int(got["history_days"]))
        self._schedule_save_settings()
        tip = ("关闭窗口：隐藏到托盘" if got["close_mode"] == CLOSE_MODE_TRAY
               else "关闭窗口：结束进程")
        boot = "；启动时收进托盘" if got["start_minimized"] else ""
        # 改了保留天数就**当场清一次**，别等下次启动（否则用户以为没生效）
        pruned = ""
        try:
            dropped, note = self.history.prune_log(int(self.history_days.get()))
            if dropped:
                pruned = f"；{note}"
        except Exception as exc:
            pruned = f"；清理日志失败：{type(exc).__name__}: {exc}"
        if self._history_window is not None:
            try:
                self._history_window.reload()
            except Exception:
                pass
        self._set_status(f"设置已保存 —— {tip}{boot}；"
                         f"日志保留 {self.history_days.get()} 天{pruned}；"
                         f"开机自启动：{'开' if got['autostart'] else '关'}")
        self.log_action("设置", "系统设置",
                        f"{tip}{boot}；日志保留 {self.history_days.get()} 天；"
                        f"自启动={'开' if got['autostart'] else '关'}")


# ============================================================================
# 六、命令行：窗口清单扫描
# ============================================================================

def scan_windows(as_json: bool = False, show_hidden: bool = False) -> int:
    """
    命令行扫描当前屏幕上的窗口并打印清单。

        python winmon.py --scan          文本清单
        python winmon.py --scan --all    同时列出屏幕上看不到的隐藏窗口
        python winmon.py --json          输出 JSON（便于脚本二次处理）
    """
    import json
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    t0 = time.perf_counter()
    wins = core.collect_desktop(include_cloaked=True)
    elapsed = (time.perf_counter() - t0) * 1000
    procs = core.snapshot_processes()

    visible = [wi for wi in wins if not wi.is_hidden_surface]
    hidden = [wi for wi in wins if wi.is_hidden_surface]
    shown = wins if show_hidden else visible

    if as_json:
        pw, ph = core.get_physical_screen_size()
        print(json.dumps({
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
            "screen": {"physical_width": pw, "physical_height": ph,
                       "logical_width": core.user32.GetSystemMetrics(0),
                       "logical_height": core.user32.GetSystemMetrics(1)},
            "counts": {"desktop_windows": len(visible), "hidden_windows": len(hidden),
                       "total_processes": len(procs)},
            "windows": [{
                "app": wi.real_name,
                "process": wi.proc,
                "pid": wi.pid,
                "title": wi.title,
                "class": wi.cls,
                "size": [wi.rect_w, wi.rect_h],
                "state": wi.state,
                "hidden": wi.is_hidden_surface,
                "frame_host": wi.is_frame_host,
                "always_on_top": wi.always_on_top,
                "path": wi.path,
                "memory_kb": wi.mem_kb,
                #: 私有提交（KB）—— 任务管理器「提交大小」列的口径
                "memory_private_kb": wi.mem_private_kb,
                "hwnd": hex(wi.hwnd),
            } for wi in shown],
        }, ensure_ascii=False, indent=2))
        return 0

    pw, ph = core.get_physical_screen_size()
    lw = core.user32.GetSystemMetrics(0)
    scale = pw / lw if lw else 1.0
    print(f"扫描时间 : {time.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"屏幕分辨率: {pw}×{ph}（物理像素）"
          + (f"，显示缩放 {scale * 100:.0f}% → 逻辑 {lw}×"
             f"{core.user32.GetSystemMetrics(1)}" if abs(scale - 1) > 0.01 else ""))
    print(f"桌面窗口  : {len(visible)} 个（屏幕上真实可见）"
          + (f" + {len(hidden)} 个隐藏窗口" if not show_hidden else ""))
    print(f"系统进程  : {len(procs)} 个        （扫描耗时 {elapsed:.0f} ms）")
    print()

    by_app: dict[str, list[core.WinInfo]] = {}
    for wi in shown:
        by_app.setdefault(wi.real_name, []).append(wi)

    print("=" * 118)
    idx = 0
    for app, items in sorted(by_app.items(), key=lambda kv: kv[0].lower()):
        idx += 1
        for j, wi in enumerate(items):
            tag = "（后台隐藏，屏幕上看不到）" if wi.is_hidden_surface else ""
            if j == 0:
                print(f"{idx}. {app} {tag}")
            else:
                print(f"   └─ 同一软件的另一个窗口 {tag}")
            print(f"      窗口标题 : {wi.title}")
            print(f"      进程/PID : {wi.proc or '?'}  [{wi.pid}]   "
                  f"内存(工作集) {core._fmt_mem(wi.mem_kb)}   "
                  f"私有提交 {core._fmt_mem(wi.mem_private_kb)}   状态 {wi.state}"
                  + ("   [置顶]" if wi.always_on_top else ""))
            print(f"      尺寸/类名: {wi.rect_w}×{wi.rect_h}   {wi.cls}")
            if wi.path:
                print(f"      程序路径 : {wi.path}")
        print("-" * 118)
    n_app_visible = len({wi.real_name for wi in visible})
    print(f"合计：屏幕上 {len(visible)} 个窗口（{n_app_visible} 个软件）"
          + (f"，另有 {len(hidden)} 个后台隐藏窗口（用 --all 查看）"
             if not show_hidden and hidden
             else f"，其中 {len(hidden)} 个为后台隐藏窗口" if hidden else ""))
    return 0


# ============================================================================
# 七、自检
# ============================================================================
#
# 说明：这里曾有一个 `snapshot_window(pid, path)` 辅助函数（把某进程主窗口存成 PPM），
# 但清理死代码时确认它**从未被任何地方调用**（selftest / smoke_test 都有自己的
# 截图逻辑，dev 工具用 tests/shot.py），因此删除，避免误导后来者。
# 需要抓窗口存图请用 `tests/shot.py`（带 PNG 编码，更好用）。


def selftest() -> int:
    """
    无界面全链路自检：枚举 → 捕获 → 渲染 → 图标 → 终止。

    **坑（当时踩到）**：窗口标题可能是控制台编码放不下的字符
    （实测某个播放器标题里的 `▶` 就让 `print` 抛 `UnicodeEncodeError: 'gbk' codec
    can't encode character '\\u25b6'`，自检在中途崩掉、退出码 1，看起来像"程序坏了"）。
    所以进来先把 stdout/stderr 切成 utf-8 + `errors="replace"` ——
    宁可显示成问号，也不能让一条标题把整个自检打死。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    import subprocess
    print(f"=== {APP_NAME} 自检 ===")
    print(f"Python     : {sys.version.split()[0]}  ({sys.executable})")
    core.enable_dpi_awareness()
    print("DPI 感知   : 已启用")

    wins = core.enum_windows()
    mine = os.getpid()
    n_hidden = sum(1 for w in wins if w.is_hidden_surface)
    print(f"[1/8] 窗口枚举 : 发现 {len(wins)} 个窗口（其中 {n_hidden} 个为隐藏窗口）")
    for wi in wins[:6]:
        tag = "（本程序自身）" if wi.pid == mine else ("（隐藏）" if wi.is_hidden_surface else "")
        print(f"        · {wi.proc:<28} {wi.rect_w:>5}×{wi.rect_h:<5} {wi.title[:34]}{tag}")
    if not wins:
        print("        没有可见窗口，后续测试跳过")
        return 0

    cand = [wi for wi in wins if wi.pid != mine and not wi.is_hidden_surface
            and wi.rect_w > 320 and wi.rect_h > 240]
    target = cand[0] if cand else wins[0]
    print(f"[2/8] 画面捕获 : 目标 {target.proc} / {target.title[:30]}")
    cap = core.WindowCapturer()
    try:
        raw, ow, oh = cap.grab_window(target.hwnd, target.rect_w, target.rect_h, 480, 300)
        sample = raw[::503]
        ratio = 100.0 * sum(1 for b in sample if b) / max(1, len(sample))
        print(f"        抓取成功 {ow}×{oh}，像素 {len(raw)} 字节，非零采样 {ratio:.1f}%")
    except Exception as exc:
        print(f"        抓取失败：{type(exc).__name__}: {exc}")
        return 2
    finally:
        cap.release()

    t0 = time.perf_counter()
    data = core.bgr_to_tk_hex_data(raw, ow, oh, 2)
    dt = (time.perf_counter() - t0) * 1000
    print(f"[3/8] 画面渲染 : 生成 {(ow + 1) // 2}×{(oh + 1) // 2} 抽样像素串，"
          f"{len(data) / 1024:.0f} KB，耗时 {dt:.1f} ms")

    png = core.IconCache(ICON_SIZE).get(target)
    print("[4/8] 图标提取 : "
          + (f"成功，PNG {len(png)} 字节" if png else "该程序没有可用图标"))

    print("[5/8] 终止能力 : 启动临时 sleep 进程验证…")
    try:
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    except Exception as exc:
        print(f"        无法启动测试进程：{exc}")
        return 0
    time.sleep(0.8)
    alive_before = core.is_process_alive(child.pid)
    print(f"        测试进程 PID {child.pid}，is_process_alive={alive_before}（应为 True）")
    ok, msg, _err = core.terminate_process(child.pid)
    time.sleep(0.4)
    alive_after = core.is_process_alive(child.pid)
    if ok and not alive_after:
        print(f"        [OK] 强制结束成功：{msg}（退出码 {child.poll()}）")
        result = 0
    else:
        print(f"        [!!] 结束异常：{msg}；alive_after={alive_after}")
        result = 3
    print(f"句柄池     : {core._PROC_POOL.stats()}")
    # ---- [6..8]/8：随程序一起发出去的那几份文件到底在不在、是不是新版 ----
    # 这三项**查磁盘上的真文件**（核心层与界面层用同一套写法，单文件版生成后
    # 也走这一段：那边的 `core.media_session_helper_path()` 会先把内嵌的那份落盘）。
    # 所以名字一律带 `core.` 前缀 —— 源项目里它们不在本模块，合并版里才是同一个模块。
    helper = core.media_session_helper_path()
    icon_p = _icon_path()
    res_ok = os.path.exists(helper) and bool(icon_p) and os.path.exists(icon_p)
    print(f"[6/8] 随附文件   : "
          + ("OK" if res_ok else "缺失！")
          + f"  ps1={'有' if os.path.exists(helper) else '无'}"
          + f"  ico={'有' if icon_p and os.path.exists(icon_p) else '无'}")
    # 自启动命令里最后那个被引号包住的路径必须真实存在，否则开机会静默失败
    auto_cmd = core.autostart_command()
    m = re.search(r'"([^"]+)"\s*$', auto_cmd)
    script_ref = m.group(1) if m else ""
    auto_ok = bool(getattr(sys, "frozen", False)) or (
        bool(script_ref) and os.path.exists(script_ref))
    print(f"[7/8] 自启动命令 : {'OK' if auto_ok else '指向不存在的脚本！'}  {auto_cmd[:90]}")
    # [8/8] ps1 必须是 v2（原子写快照）—— P1-4/P2-8 的守门员
    try:
        with open(helper, "r", encoding="utf-8", errors="replace") as fh:
            ps1_text = fh.read()
        ps1_marked = core._MEDIA_HELPER_MARKER in ps1_text
        ps1_atomic = "File]::Replace($tmp, $path" in ps1_text
        ps1_ok = bool(ps1_marked and ps1_atomic)
        print(f"[8/8] 桥接脚本   : "
              + ("OK（v2 原子写）" if ps1_ok else "仍是旧版（非原子写）！")
              + f"  标记={'有' if ps1_marked else '无'}"
              + f"  原子写={'有' if ps1_atomic else '无'}")
    except OSError as exc:
        print(f"[8/8] 桥接脚本   : 读不到 {helper}：{exc}")
    print("=== 自检结束 ===")
    return result


def smoke_test(seconds: float = 6.0) -> int:
    """界面自检：启动窗口，自动选中目标程序取帧，并对三个标签页截图后退出。"""
    import tempfile
    core.enable_dpi_awareness()
    out_dir = os.path.join(tempfile.gettempdir(), "winmon-smoke")
    os.makedirs(out_dir, exist_ok=True)
    out_base = os.path.join(out_dir, "winmon")

    root = tk.Tk()
    app = WinMonApp(root)

    def pick_target() -> None:
        # 只挑真正显示在屏幕上的窗口：排除 cloaked 隐藏窗口与透明分层窗口
        skip = {"textinputhost.exe", "applicationframehost.exe"}
        cands = [wi for wi in app.windows
                 if wi.pid != os.getpid() and wi.rect_w > 400 and wi.rect_h > 300
                 and not wi.iconic and not wi.is_hidden_surface
                 and wi.proc.lower() not in skip]
        if not cands:
            cands = [wi for wi in app.windows if wi.pid != os.getpid() and not wi.iconic]
        if not cands:
            print("没有合适的测试目标窗口")
            return
        cands.sort(key=lambda x: -x.rect_w * x.rect_h)
        wi = cands[0]
        item = app.row_of_hwnd.get(wi.hwnd)
        if item:
            app.tree.selection_set(item)
            app.tree.see(item)
        app.start_preview(wi.hwnd)
        print(f"预览目标：{wi.real_name} / {wi.title[:40]}  {wi.rect_w}×{wi.rect_h}")
        app.gallery_add_selected()

    def snapshot(tag: str = "") -> None:
        try:
            hwnd = core.H(core.user32.GetAncestor(core.w.HWND(root.winfo_id()), core.GA_ROOT))
            rc = core.w.RECT()
            core.user32.GetWindowRect(core.w.HWND(hwnd), ctypes.byref(rc))
            ww, hh = rc.right - rc.left, rc.bottom - rc.top
            cap = core.WindowCapturer()
            try:
                raw, ow, oh = cap.grab_window(hwnd, ww, hh, ww, hh)
            finally:
                cap.release()
            path = out_base + (f"_{tag}" if tag else "") + ".ppm"
            with open(path, "wb") as fh:
                fh.write(b"P6 %d %d 255\n" % (ow, oh))
                buf = bytearray(len(raw))
                for i in range(0, len(raw), 3):
                    buf[i] = raw[i + 2]
                    buf[i + 1] = raw[i + 1]
                    buf[i + 2] = raw[i]
                fh.write(bytes(buf))
            print(f"界面截图已保存：{path}  ({ow}×{oh})")
        except Exception as exc:
            print(f"截图失败：{type(exc).__name__}: {exc}")

    def finish() -> None:
        """收尾：停媒体助手 → 取消所有定时器 → 销毁窗口。

        不取消定时器的话，`destroy()` 之后 Tcl 还会派发它们，
        控制台上会出现一串 `invalid command name "..._pump_frames"`。
        """
        core.stop_media_snapshot_helper()
        app._cancel_pending_after()
        root.destroy()

    root.after(700, pick_target)
    root.after(2000, lambda: app.notebook.select(1))
    root.after(3400, lambda: snapshot("gallery"))
    root.after(3600, lambda: app.notebook.select(2))
    root.after(4800, lambda: snapshot("proctree"))
    root.after(5000, lambda: app.notebook.select(0))
    root.after(int(seconds * 1000) - 800, lambda: snapshot("preview"))
    root.after(int(seconds * 1000), finish)
    root.mainloop()
    print(f"后台捕获线程共取帧 {app.worker.frames_done} 次；句柄池 {core._PROC_POOL.stats()}")
    return 0


def ai_sessions_cli(as_json: bool = False) -> int:
    """
    命令行：识别"现在在用哪个 AI"（只读，不联网）。

    判据与界面 / MCP 完全同一套；区别是命令行只能看**这一拍**，
    所以"最近 30 分钟前台时长"是 0 —— 累计时长需要 WinMon 界面在跑。
    """
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    res = ai.collect_ai_sessions(pin=os.environ.get("WINMON_AI_PIN", ""))
    if as_json:
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return 0
    print(ai.format_sessions_text(res))
    primary = res.get("primary")
    if primary:
        print(f"  进程：{primary.get('proc') or '?'}  PID {primary.get('pid')}"
              f"  来源 {primary.get('source')}")
        print(f"  资源占比：CPU {primary.get('cpu_share')} / 内存 {primary.get('mem_share')}"
              f"（只用于并列时打破平局）")
    for other in (res.get("others") or []):
        print(f"  候选：{other.get('name')}（{other.get('kind')}，"
              f"score {other.get('score')}）")
    if res.get("ambiguous"):
        print("  注意：前两名分差很小，建议用 WINMON_AI_PIN 或设置里的「主 AI」指定")
    print("  说明：命令行是一次性查询，看不到「最近 30 分钟前台时长」；"
          "累计时长需要 WinMon 界面在跑。")
    return 0


def incidents_cli(as_json: bool = False, limit: int = 20) -> int:
    """命令行：列出本地事件（只读；不写事件文件）。"""
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    res = ai.collect_incidents(limit=limit)
    if as_json:
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return 0
    print(ai.format_incidents_text(res.get("incidents") or []))
    for inc in (res.get("incidents") or [])[:10]:
        book = inc.get("playbook_id") or ""
        if book:
            print(f"      剧本：{book}（用 py winmon.py --mcp-selftest 或 MCP 读取详情）")
    return 0


def ai_usage_cli(as_json: bool = False) -> int:
    """命令行：AI 识别与用量（真实 / 精确 / 估算分栏）。"""
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    status = ai.Budget().status()
    sessions = ai.collect_ai_sessions(pin=os.environ.get("WINMON_AI_PIN", ""))
    if as_json:
        print(json.dumps({"sessions": sessions, "budget": status},
                         ensure_ascii=False, indent=2))
    else:
        print(ai.format_sessions_detail(sessions))
        print()
        print(ai.format_usage_text(status))
        print("\n（恢复预算只能在 WinMon 界面里操作；MCP 没有改预算的工具）")
    return 0


def ai_context_cli(copy_to_clip: bool = False) -> int:
    """命令行：生成一段给网页端 AI 的上下文包（剪贴板桥）。"""
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    text = ai.build_context_pack()
    if copy_to_clip:
        ok = ai.copy_to_clipboard(text)
        print("已复制到剪贴板。" if ok else "复制失败（内容仍会打印在下面）。",
              file=sys.stderr)
    print(text)
    return 0


def ai_ask_cli(prompt: str) -> int:
    """命令行：直连模型 API（可选，默认关闭；会先过预算）。"""
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    if not (prompt or "").strip():
        print("用法：py winmon.py --ai-ask \"你的问题\"", file=sys.stderr)
        return 2
    out = provider.call_model(prompt, budget=ai.Budget(),
                              purpose="cli")
    if not out.get("ok"):
        print(f"直连 AI 不可用：{out.get('error')}", file=sys.stderr)
        if out.get("code") == "config":
            print("需要：WINMON_AI_DIRECT=1、WINMON_AI_API_KEY；"
                  "可选 WINMON_AI_PROVIDER=openai|anthropic|zhipu、WINMON_AI_MODEL"
                  "（智谱默认 glm-5.3）、WINMON_AI_REASONING_EFFORT=low|high|max。",
                  file=sys.stderr)
        return 3 if out.get("code") == "budget" else 2
    print(out.get("text") or "")
    print(f"\n[用量] 输入 {out.get('tokens_in', 0)} / 输出 {out.get('tokens_out', 0)}"
          f" token（精确）· {out.get('model')}")
    return 0


def ai_chat_cli(as_json: bool = False, limit: int = 20) -> int:
    """命令行：查看 AI 助手的本地记录（直连回答 / 交接 / 手动保存的回复）。"""
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    rows = ai.load_chat(limit=max(1, int(limit)))
    if as_json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return 0
    if not rows:
        print("还没有记录。打开「AI 助手…」做一次分析/交接，或用 --ai-assist。")
        return 0
    for row in rows:
        print(ai.format_chat_record(row))
        print()
    print(f"（共 {len(rows)} 条；文件：{ai.chat_path()}）")
    return 0


def ai_assist_cli(question: str = "", as_json: bool = False,
                  copy_to_clip: bool = False) -> int:
    """
    命令行：跟随当前识别到的 AI。

      · 它有自己的直连配置 → 调用它，打印真实 `usage`；
      · 没有直连配置 → 生成一段"交给它"的现场提示（可 `--copy` 到剪贴板），
        **绝不回退到别的模型**。
    """
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    wins = core.collect_desktop(include_cloaked=True, with_detail=True)
    procs = core.snapshot_processes()
    fg = core.get_foreground_hwnd()
    sessions = ai.identify_ai_sessions(wins, procs, None, fg,
                                       pin=os.environ.get("WINMON_AI_PIN", ""))
    primary = sessions.get("primary") or {}
    name = str(primary.get("name") or "")
    if not name:
        print("没有识别到正在使用的 AI；先打开一个 AI 窗口，"
              "或用 WINMON_AI_PIN 指定。", file=sys.stderr)
        return 2
    context = ai.build_work_context(wins, procs, fg, sessions,
                                    ai.collect_incidents(limit=5))
    cfg = provider.resolve_for_ai(name)
    bad = cfg.validate() if cfg is not None else "没有为这个 AI 配置直连"
    if cfg is not None and not bad:
        prompt = provider.assistant_prompt(context, question)
        out = provider.call_model(prompt, config=cfg, budget=ai.Budget(),
                                  purpose="assist_cli")
        if out.get("ok"):
            body = out.get("text") or ""
            ai.append_chat({"mode": "direct", "ai": name,
                            "model": out.get("model") or "",
                            "question": question or "现场分析", "answer": body,
                            "tokens": out.get("tokens", 0),
                            "tokens_in": out.get("tokens_in", 0),
                            "tokens_out": out.get("tokens_out", 0)})
        if as_json:
            print(json.dumps({"ok": out.get("ok"), "mode": "direct", "ai": name,
                              "error": out.get("error", ""), "text": out.get("text", ""),
                              "model": out.get("model", ""), "context": context,
                              "tokens_in": out.get("tokens_in", 0),
                              "tokens_out": out.get("tokens_out", 0),
                              "tokens": out.get("tokens", 0)},
                             ensure_ascii=False, indent=2))
            return 0 if out.get("ok") else (3 if out.get("code") == "budget" else 2)
        if not out.get("ok"):
            print(f"跟随 {name} 的直连失败：{out.get('error')}", file=sys.stderr)
            return 3 if out.get("code") == "budget" else 2
        print(out.get("text") or "")
        print(f"\n[跟随] {name} → {out.get('model')}")
        print(f"[用量] 输入 {out.get('tokens_in', 0)} / 输出 {out.get('tokens_out', 0)}"
              f" = {out.get('tokens', 0)} token（API 回执）")
        return 0

    # 没有直连：交接给用户正在用的 AI，不调用任何模型
    prompt = provider.handoff_prompt(
        context, question, ai_name=name,
        via_mcp=str(primary.get("source") or "") == "mcp_parent")
    copied = ai.copy_to_clipboard(prompt) if copy_to_clip else False
    ai.append_chat({"mode": "handoff", "ai": name,
                    "question": question or "现场交接",
                    "note": "copied" if copied else "cli"})
    if as_json:
        print(json.dumps({"ok": True, "mode": "handoff", "ai": name,
                          "reason": bad, "context": context, "text": prompt,
                          "copied": copied, "tokens_in": 0, "tokens_out": 0,
                          "tokens": 0}, ensure_ascii=False, indent=2))
        return 0
    print(f"当前 AI：{name}（{bad}）")
    print("没有调用任何模型；下面是交给它的现场提示。"
          + ("（已复制到剪贴板）" if copied else "（加 --copy 可复制到剪贴板）"))
    print("\n" + prompt)
    return 0


def ai_explain_cli() -> int:
    """命令行：让直连模型解释最近一条本地事件。"""
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    incidents = ai.collect_incidents(limit=5).get("incidents") or []
    if not incidents:
        print("当前没有打开的本地事件，不需要解释。")
        return 0
    out = provider.explain_incident(incidents[0], budget=ai.Budget())
    if not out.get("ok"):
        print(f"直连 AI 不可用：{out.get('error')}", file=sys.stderr)
        return 3 if out.get("code") == "budget" else 2
    print(out.get("text") or "")
    print(f"\n[用量] 输入 {out.get('tokens_in', 0)} / 输出 {out.get('tokens_out', 0)}"
          f" token（精确）· 事件 {out.get('incident_id')}")
    return 0


USAGE_TEXT = """WinMon 1.0 —— 桌面程序实时监控与终止（纯 ctypes + tkinter，零第三方依赖）

用法：python winmon.py [选项]

不传任何选项 = 启动图形界面。

窗口 / 进程：
  --json                以 JSON 输出窗口清单（脚本里好处理）
  --all                 连"后台隐藏"的窗口一起列（配合 --json / --scan）
  --scan, --list        输出人看的窗口清单
  --catalog             列出程序目录（开始菜单 + 注册表 + 正在运行的）
      --category <名>     只看某一类（如：影音播放 / 浏览器 / 开发工具）
      --favorites         只看收藏
      --recent            只看最近使用
  --find <关键词>       按关键词查找程序
  --open <关键词>       按关键词精准打开程序
      --admin             以管理员身份打开

维护：
  --clean               列出程序自己产生的可清理文件（只看，不删）
      --dry-run           同上（显式表示"只看"）
      --yes               真的清理"默认勾选"的那些（缓存 / 日志 / 字节码 / 临时导出）
      --settings          清理时连设置一起清（**会丢收藏 / 分类 / 最近使用**）
  --idle                列出"长期开着、没人用"的程序（只看，不关）
      --idle-minutes N    换个闲置阈值（分钟）

诊断：
  --selftest            无界面全链路自检（枚举 → 捕获 → 渲染 → 图标 → 终止）
  --smoke               开个隐藏窗口做一次冒烟测试
  --version             打印版本
  -h, --help, /?        显示这份说明

AI / MCP（只读；直连 API 默认关闭）：
  --ai-sessions         识别现在在用哪个 AI（桌面客户端 / 网页端）
  --incidents           列出本地事件（无响应窗口 / 磁盘告急 / MCP 工具失败）
  --ai-usage            AI 识别与用量（真实 / 精确 / 估算分栏）
  --ai-chat             查看 AI 助手本地记录（直连回答 / 交接 / 手动保存）
  --ai-context          生成给网页 AI 的上下文包（--copy 复制到剪贴板）
  --ai-assist [问题]    跟随当前 AI 读窗口现场；无直连则生成交接文本
      --copy             交接文本复制到剪贴板（不调用任何模型）
  --ai-ask <问题>       直连模型 API（需 WINMON_AI_DIRECT=1 + API key）
  --ai-explain          让直连模型解释最近一条本地事件
      WINMON_AI_PROVIDER   openai（默认）/ anthropic / zhipu（GLM-5.3）
      WINMON_AI_MODEL      覆盖模型名（智谱默认 glm-5.3）
  --mcp-stdio           以 MCP 服务器身份跑在 stdio 上（给 AI 客户端挂）
  --mcp-selftest        MCP 协议与工具的自检（不需要客户端）
  --mcp-http            本地 HTTP MCP（只绑 127.0.0.1；给网页端/HTTP 客户端）
      --port N            端口（默认 8765）
      --expire-minutes N  token 过期分钟数（默认不过期；公网建议 30）
      WINMON_AI_PIN=名字  指定主 AI（环境变量）

示例：
  python winmon.py                     启动界面
  python winmon.py --json --all        把含隐藏窗口的清单打印成 JSON
  python winmon.py --open 记事本        打开记事本
  python winmon.py --clean --dry-run   看看有哪些东西可以清理
"""


def main() -> int:
    if sys.platform != "win32":
        print("WinMon 仅支持 Windows。", file=sys.stderr)
        return 1
    # 控制台编码（当时实测踩到）：Windows 的默认控制台编码是 GBK，
    # 而我们的输出里到处是中文（用法说明、类别名"影音播放"、程序名…），
    # 直接 print 会 UnicodeEncodeError 或变成乱码。进来先切成 UTF-8 + replace。
    # 为什么宁可显示成问号：一条带特殊符号的**窗口标题**不该把整个命令打死
    # （`selftest()` 里早就有同样的处理，这里提到最前面让所有入口都受益）。
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    # 运行时数据（设置 / 缓存 / 日志 / 媒体快照）都放在**用户数据目录**里，
    # 不在程序目录里（见 `core.data_dir()`）。这里做两件事：
    #   ① 把旧版本留在程序目录里的那些文件**一次性搬过去**
    #      （先校验副本再删原件，搬不动就原样留着 —— 见 `core.migrate_legacy_data_files`）；
    #   ② 确保数据目录存在。
    # 为什么放在这么靠前：下面任何一个分支（--catalog / --clean / --selftest / 开界面）
    # 都可能要写这些文件。
    core.migrate_legacy_data_files()
    core.ensure_data_dir()
    args = sys.argv[1:]
    #: 命令行认得的开关（下面"未知参数"与"漏值"两处都要用，所以先定义）。
    KNOWN = {"--version", "--selftest", "--smoke", "--scan", "--list", "--json", "--all",
             "--catalog", "--programs", "--category", "--favorites", "--recent", "--clean",
             "--dry-run", "--yes", "--idle", "--idle-minutes", "--minutes", "--find",
             "--open", "--admin", "--help", "-h", "/?", "-?", "help",
             "--settings", "--clean-all",
             "--ai-sessions", "--incidents", "--mcp-stdio", "--mcp-selftest",
             "--mcp-http", "--port", "--expire-minutes",
             "--ai-usage", "--ai-chat", "--ai-context", "--copy", "--ai-ask", "--ai-explain",
             "--ai-assist"}

    def _looks_like_flag(a: str) -> bool:
        """像个开关吗？（`-5` 这种**数字**不算 —— 它是 `--idle-minutes -5` 的值。）"""
        if not a.startswith("-"):
            return False
        try:
            float(a)
            return False
        except ValueError:
            return True

    def _arg_missing_value(flag: str) -> bool:
        """
        `--flag` 出现了、但**没给出值**吗？

        为什么值得单独判（2026-10-13 修的真 bug）：漏值时原来会**静默按默认值跑** ——
        `--category` 打印整个目录并退出 0（看起来像成功了），
        `--idle-minutes abc` 按 30 分钟算。这与"认不出来的参数要报错"是同一类问题
        （HANDOFF 坑 76：不能静默改变用户意图）。`--flag=值` 也算给了值（空值算漏）。
        """
        for i, a in enumerate(args):
            if a == flag:
                if i + 1 >= len(args):
                    return True
                return _looks_like_flag(args[i + 1])   # 后面跟的是另一个开关 = 漏值
            if a.startswith(flag + "="):
                return not a.split("=", 1)[1].strip()
        return False

    def _need_value(flag: str) -> bool:
        """漏值时打一句人话并返回 True（调用方直接 `return 2`）。"""
        if not _arg_missing_value(flag):
            return False
        print(f"{flag} 后面需要一个值。", file=sys.stderr)
        print("用 python winmon.py --help 看可用选项。", file=sys.stderr)
        return True

    def _has_flag(flag: str) -> bool:
        """
        `--flag` 或 `--flag=值` 都算"给了这个开关"。

        为什么必须两种都认（2026-10-13 修的真 bug）：`_arg_value()` 从第一版起就支持
        `--flag=值` 写法，可 main() 的分支判断一直只查 `flag in args` ——
        于是 `py winmon.py --category=影音播放` / `--idle-minutes=5`
        **既不走对应分支、也不算未知参数**，一路掉到"起界面"（用户看到的是没反应）。
        这与以前"`--category` 单独用不生效"是同一类病。
        """
        return any(a == flag or a.startswith(flag + "=") for a in args)

    # `-h` / `--help` / `/?` 必须**最先**处理：以前没实现，于是它掉进"起界面"那条路，
    # 用户看到的是"命令没反应/像卡死"（当时实测发现的真 bug）。
    if any(a in ("-h", "--help", "/?", "-?", "help") for a in args):
        print(USAGE_TEXT)
        return 0
    if "--version" in args:
        print(f"{APP_RELEASE}（WinMon {APP_VER}）")
        return 0
    if "--selftest" in args:
        return selftest()
    if "--smoke" in args:
        return smoke_test()
    # MCP / AI 伴侣：都必须先于"未知参数"与单实例判断，且不创建 GUI
    if "--mcp-selftest" in args:
        return mcp.selftest()
    if "--mcp-stdio" in args:
        return mcp.stdio_main()
    if "--mcp-http" in args:
        try:
            port = int(_arg_value(args, "--port") or 8765)
            expire = int(_arg_value(args, "--expire-minutes") or 0)
        except ValueError:
            print("--port / --expire-minutes 需要一个整数", file=sys.stderr)
            return 2
        return mcp.http_main(port=port, expire_minutes=max(0, expire))
    if _has_flag("--ai-sessions"):
        return ai_sessions_cli(as_json=("--json" in args))
    if _has_flag("--incidents"):
        return incidents_cli(as_json=("--json" in args))
    if _has_flag("--ai-usage"):
        return ai_usage_cli(as_json=("--json" in args))
    if _has_flag("--ai-chat"):
        return ai_chat_cli(as_json=("--json" in args))
    if _has_flag("--ai-context"):
        return ai_context_cli(copy_to_clip=("--copy" in args))
    if _has_flag("--ai-assist"):
        question = _arg_value(args, "--ai-assist")
        if question and _looks_like_flag(question):
            question = ""
        return ai_assist_cli(question or "", as_json=("--json" in args),
                             copy_to_clip=("--copy" in args))
    if _has_flag("--ai-ask"):
        if _need_value("--ai-ask"):
            return 2
        return ai_ask_cli(_arg_value(args, "--ai-ask"))
    if "--ai-explain" in args:
        return ai_explain_cli()
    # `--all` 单独用时也走清单（以前只有 --scan/--json/--list 才算，`--all` 会掉进界面）
    if any(a in args for a in ("--scan", "--json", "--list", "--all")):
        return scan_windows(as_json=("--json" in args), show_hidden=("--all" in args))
    if _has_flag("--catalog") or _has_flag("--programs") or _has_flag("--category") \
            or "--favorites" in args or "--recent" in args:
        if _need_value("--category"):
            return 2
        return list_catalog_cli(category=_arg_value(args, "--category"),
                                only_favorites=("--favorites" in args),
                                only_recent=("--recent" in args))
    if "--clean" in args or "--dry-run" in args:
        return clean_cli(dry_run=("--dry-run" in args))
    if "--idle" in args or _has_flag("--idle-minutes") or _has_flag("--minutes"):
        for flag in ("--idle-minutes", "--minutes"):
            if _need_value(flag):
                return 2
        raw = _arg_value(args, "--idle-minutes") or _arg_value(args, "--minutes")
        if raw:
            try:
                minutes = int(raw)
            except ValueError:
                # `--idle-minutes abc` 以前会静默按 30 分钟算 —— 那是替用户改了阈值
                print(f"--idle-minutes 需要一个整数（给的是 {raw!r}）", file=sys.stderr)
                print("用 python winmon.py --help 看可用选项。", file=sys.stderr)
                return 2
        else:
            minutes = core.IDLE_DEFAULT_SECONDS // 60
        return idle_cli(minutes=max(1, minutes))
    if _has_flag("--find"):
        if _need_value("--find"):
            return 2
        return find_programs_cli(_arg_value(args, "--find"))
    if _has_flag("--open"):
        if _need_value("--open"):
            return 2
        return open_program_cli(_arg_value(args, "--open"), as_admin=("--admin" in args))
    # ---- 认不出来的参数：**报错退出，不要静默起界面**（当时实测的真问题）----
    #
    # 以前没这一段：`--unknown-flag`、`--category` 后面忘了给值、`--idle-minutes abc`
    # 这些都会**一路掉到"起界面"**那条路。命令行里看到的现象是"没反应/像卡死"，
    # 而且自动化里会把界面悄悄拉起来（实测跑一次参数测试就多出两个 WinMon 实例）。
    # （`KNOWN` 与 `_looks_like_flag` 在函数开头定义 —— "漏值"那几处也要用。）
    # 注意 `-5` 这种**参数值**不能被当成未知开关（`--idle-minutes -5` 实测踩到）：
    # 认不出来的只算"以 - 开头、且不是数字"的那些。
    unknown = [a for a in args
               if _looks_like_flag(a) and a.split("=", 1)[0] not in KNOWN]
    if unknown:
        print(f"不认识的参数：{' '.join(unknown)}", file=sys.stderr)
        print("用 python winmon.py --help 看可用选项。", file=sys.stderr)
        return 2
    # ---- 单实例（C.1）----
    #
    # 以前开两个实例会有两个托盘图标、两份后台助手、两份媒体轮询，用户还得自己
    # 分辨哪个是哪个（实测跑一次参数测试就多出两个实例）。现在的规矩：
    # **第二个实例把已有窗口带到前台，然后自己退出**，并如实说明。
    # 判据是内核互斥体（不是"枚举窗口找自己"—— 那个会漏掉藏在托盘里的实例）。
    claimed, why = core.claim_single_instance()
    if not claimed:
        ok, msg = core.bring_existing_winmon_front()
        print(f"{why}；{msg}", file=sys.stderr)
        print("（如果确实想再开一个，先关掉正在运行的那个。）", file=sys.stderr)
        return 0
    core.enable_dpi_awareness()
    root = tk.Tk()
    app = WinMonApp(root)
    root.mainloop()
    core.release_single_instance()
    return 0


def _arg_value(args: list[str], flag: str) -> str:
    """取 --flag 后面的值（支持 --flag=值 与 --flag 值 两种写法）。"""
    for i, a in enumerate(args):
        if a == flag and i + 1 < len(args):
            return args[i + 1]
        if a.startswith(flag + "="):
            return a.split("=", 1)[1]
    return ""


def list_catalog_cli(category: str = "", only_favorites: bool = False,
                     only_recent: bool = False) -> int:
    """
    列出程序目录（系统里装了哪些可启动的程序）。

        --catalog                    全部（按类别分组）
        --catalog --category 游戏     只看某一类
        --catalog --favorites        只看收藏
        --catalog --recent           只看最近使用

    收藏 / 最近使用读的是 winmon_settings.json 里 `program_prefs`，
    与图形界面共用同一份数据。
    """
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    t0 = time.perf_counter()
    items = core.build_program_catalog()
    prefs = _load_prefs()
    cat_key = _category_key(category)

    if category and not cat_key:
        print(f"未知类别「{category}」。可用类别："
              + "、".join(core.CATEGORY_LABELS[k] for k in core.CATEGORY_ORDER))
        return 2

    selected = []
    for it in items:
        if only_favorites and not prefs.is_favorite(it.target, it.name):
            continue
        if only_recent and not prefs.is_recent(it.target, it.name):
            continue
        if cat_key and core.category_of_item(
                it, prefs.category_override(it.target, it.name)) != cat_key:
            continue
        selected.append(it)

    note = ""
    if only_favorites:
        note = "（仅收藏）"
    elif only_recent:
        note = "（仅最近使用）"
    elif cat_key:
        note = f"（类别：{core.CATEGORY_LABELS[cat_key]}）"
    print(f"程序目录：共 {len(items)} 项，显示 {len(selected)} 项{note}"
          f"（{time.perf_counter() - t0:.2f}s）")
    if not selected:
        print("没有符合条件的程序。")
        return 1

    recent_order = {prefs.key(r.get("path", ""), r.get("name", "")): i
                    for i, r in enumerate(prefs.recent_list())}

    def state_of(it):
        return "运行中" if (it.pid and core.is_process_alive(it.pid)) else (
            "可启动" if it.runnable else "不可用")

    def print_rows(rows):
        for it in rows:
            fav = " ★" if prefs.is_favorite(it.target, it.name) else ""
            name = it.name if len(it.name) <= 36 else it.name[:35] + "…"
            print(f"{name:<38}{fav:<3} {it.source_label:<8} {state_of(it):<6} {it.target}")

    # 分组展示（只有"全部"时分组；其它情况本来就是挑出来的子集）
    if not (cat_key or only_favorites or only_recent):
        for key, group in core.group_items_by_category(selected, prefs.overrides()):
            print(f"\n== {core.CATEGORY_LABELS[key]}（{len(group)}）==")
            print(f"{'名称':<38} {'来源':<8} {'状态':<6} 目标")
            print("-" * 118)
            group.sort(key=lambda x: (0 if prefs.is_favorite(x.target, x.name) else 1,
                                      recent_order.get(prefs.key(x.target, x.name), 10 ** 6),
                                      x.name.lower()))
            print_rows(group)
    else:
        print(f"{'名称':<38} {'来源':<8} {'状态':<6} 目标")
        print("-" * 118)
        selected.sort(key=lambda x: (recent_order.get(prefs.key(x.target, x.name), 10 ** 6),
                                     x.name.lower()))
        print_rows(selected)
    return 0


def _settings_path() -> str:
    """
    设置文件位置。

    默认在**运行时数据目录**（`core.data_dir()`）里 —— 不再写在程序目录里
    （理由见 `core.data_dir()` 那一节：程序目录只留源与文档，
    别人 clone 下来跑一次也不会把工作区弄脏）。

    支持用环境变量 `WINMON_SETTINGS` 覆盖 —— 测试要用临时文件验证
    "收藏 / 最近使用会被持久化"，不能去动用户真实的偏好文件。
    """
    override = os.environ.get("WINMON_SETTINGS")
    if override:
        return override
    return core.data_file("winmon_settings.json")


def _load_prefs() -> "core.CatalogPrefs":
    """从 winmon_settings.json 里读程序目录偏好（收藏/最近使用/手动分类）。"""
    try:
        import json
        with open(_settings_path(), "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return core.CatalogPrefs.from_dict(data.get("program_prefs"))
    except Exception:
        return core.CatalogPrefs()


def _category_key(text: str) -> str:
    """把"游戏娱乐"/"游戏"/"game" 这类写法解析成类别 key；解析不出返回空串。"""
    t = (text or "").strip().lower()
    if not t:
        return ""
    for key, label in core.CATEGORY_LABELS.items():
        if t == key or t == label.lower():
            return key
    for key, label in core.CATEGORY_LABELS.items():
        if t in label.lower() or label.lower().startswith(t):
            return key
    return ""


def find_programs_cli(keyword: str) -> int:
    """按关键词查找程序。"""
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    if not keyword:
        print("用法：winmon.py --find <关键词>")
        return 2
    items = core.build_program_catalog()
    hits = core.find_programs(keyword, items, limit=20)
    if not hits:
        print(f"没有找到匹配「{keyword}」的程序（目录共 {len(items)} 项）")
        return 1
    print(f"匹配「{keyword}」的程序（{len(hits)} 项）：")
    for i, it in enumerate(hits, 1):
        mark = "运行中" if (it.pid and core.is_process_alive(it.pid)) else (
            "可启动" if it.runnable else "不可用")
        print(f"  [{i}] {it.name}   ({mark})")
        print(f"      目标：{it.target}")
        if it.args:
            print(f"      参数：{it.args}")
    return 0


def idle_cli(minutes: int = 30, sample_seconds: float = 1.0) -> int:
    """
    列出"开着不用"的程序（判定与界面完全同一套逻辑）。

        py winmon.py --idle                    :: 按默认 30 分钟判定
        py winmon.py --idle --idle-minutes 5   :: 换个阈值（调试用，几分钟就够）

    这是**只看不动手**的入口：一个程序都不会被关掉。
    判定依据（**现在只有两个**"在干活"的信号，任一成立就重新计时）：
    窗口是不是前台 / CPU 占用。
    2026-10-13 删掉了第三个"有没有声音输出"—— 那条依赖的音频会话枚举在本机恒为 0
    （见 HANDOFF 的删除说明），留着只会让人以为它在起作用。
    """
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    # 命令行是一次性判定，所以自己采一次 CPU（界面上那份是跟着刷新周期滚动的）
    pids = {wi.pid for wi in core.enum_windows(include_cloaked=True)
            if not wi.is_hidden_surface}
    cpu = core.sample_cpu_percent(pids, seconds=max(0.2, sample_seconds))
    samples = core.idle_samples(cpu)
    print(f"作用范围：{len(samples)} 个有可见窗口的程序（无窗口的后台进程不参与）")
    # "闲置时长"命令行里没法凭空知道历史，所以这里给的是**当前这一拍的判定依据**，
    # 而不是"已经闲了多久"—— 要看真实累计时长请用界面（它是持续计时的）。
    idle_now = [{"pid": p, "name": s["name"], "title": s["title"],
                 "cpu": s["cpu"], "iconic": s["iconic"]}
                for p, s in samples.items()
                if not s["foreground"]]
    print(f"\n此刻**没有在用**的程序（连续闲置满 {minutes} 分钟后界面会提醒）："
          f"{len(idle_now)} 个")
    if not idle_now:
        print("  （没有：所有有窗口的程序都在前台）")
    for s in sorted(idle_now, key=lambda r: r["name"].lower()):
        mark = "已最小化" if s["iconic"] else "窗口在后台"
        title = (s["title"] or "")[:44]
        print(f"  {s['name']:<26}{mark:<10}CPU {s['cpu']:>5.1f}%   PID {s['pid']:<7}{title}")
    print("\n（本命令只看不动手；要关请用界面的「闲置清单」）")
    return 0


def clean_cli(dry_run: bool = False) -> int:
    """
    列出（并清理）程序自己产生的文件。

        py winmon.py --clean             :: 列出可清理项与大小
        py winmon.py --clean --dry-run   :: 只看不删（与上面等价，显式说明用）
        py winmon.py --clean --yes       :: 清理"默认勾选"的那些（缓存/日志/字节码/临时导出）
        py winmon.py --clean --yes --settings  :: **连设置一起清**（会丢收藏/分类/最近使用）

    安全约定：只删 `core.CLEANABLE_ITEMS` 里解析出来的路径，
    不接受任意路径参数 —— 这个入口不可能删到别的东西。
    """
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    items = core.list_cleanable_files()
    total = sum(d["bytes"] for d in items if d["exists"])
    print(f"可清理项 {len(items)} 类，合计约 {total / 1024:.0f} KB：")
    default_keys = []
    for d in items:
        mark = "默认勾选" if d["default"] else "不勾选（用户数据）"
        state = "" if d["exists"] else "  ← 当前没有"
        print(f"  [{d['key']}] {d['label']}：{d['count']} 个文件 / "
              f"{d['bytes'] / 1024:.0f} KB（{mark}）{state}")
        print(f"        {d['note']}")
        if d["default"] and d["exists"]:
            default_keys.append(d["key"])
    if dry_run or not ("--yes" in sys.argv[1:] or "--clean-all" in sys.argv[1:]):
        print("\n（只是列出，没有删除；加 --yes 才会清理默认勾选的项）")
        return 0
    keys = list(default_keys)
    if "--settings" in sys.argv[1:]:
        keys.append("settings")
    if not keys:
        print("\n没有需要清理的内容。")
        return 0
    removed, freed, failures = core.clean_files(keys)
    print(f"\n已清理 {removed} 个文件，释放 {freed / 1024:.0f} KB（{len(keys)} 类）")
    for f in failures:
        print(f"  失败：{f}")
    return 0 if not failures else 1


def open_program_cli(keyword: str, as_admin: bool = False) -> int:
    """按关键词精准打开程序（等价于双击它的开始菜单图标）。"""
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    if not keyword:
        print("用法：winmon.py --open <关键词> [--admin]")
        return 2
    items = core.build_program_catalog()
    hits = core.find_programs(keyword, items, limit=5)
    if not hits:
        print(f"没有找到匹配「{keyword}」的程序")
        return 1
    target = hits[0]
    if len(hits) > 1:
        print(f"匹配到 {len(hits)} 个，选择最匹配的一项：")
        for it in hits[:5]:
            print(f"    · {it.name}  ->  {it.target}")
    print(f"正在启动：{target.name}")
    print(f"     目标：{target.target}")
    ok, msg = core.launch_item(target, as_admin=as_admin)
    print(("[OK] " if ok else "[失败] ") + msg)
    return 0 if ok else 3


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except BaseException:
        # 用 pythonw 启动时没有控制台，异常会静默消失；这里落盘一份日志便于排查。
        # 放**运行时数据目录**（不是程序目录）：程序目录里不该出现这种运行痕迹。
        import traceback
        detail = traceback.format_exc()
        try:
            core.ensure_data_dir()          # 目录可能还没建（例如就崩在启动早期）
            log_path = core.data_file("winmon_error.log")
            with open(log_path, "w", encoding="utf-8") as fh:
                fh.write(f"{APP_NAME} {APP_VER}\n{time.strftime('%Y-%m-%d %H:%M:%S')}\n\n{detail}")
        except Exception:
            pass
        try:
            ctypes.windll.user32.MessageBoxW(
                None, f"WinMon 启动失败：\n\n{detail[-1200:]}", "WinMon 错误", 0x10)
        except Exception:
            pass
        sys.exit(1)
