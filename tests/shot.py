# -*- coding: utf-8 -*-
"""
界面/画面截图工具（开发辅助，不属于测试用例）。

用途：把 WinMon 自己的界面或任意窗口抓成 PNG，用于确认布局与画面效果。
它是纯标准库实现的（不用 Pillow），与项目本体一致。

用法（在 winmon 目录下）
------------------------
    py tests\\shot.py                      :: 主界面「实时预览」页
    py tests\\shot.py --tab 3              :: 切到第 4 个标签页（程序目录）
    py tests\\shot.py --width 1080         :: 换个宽度截图（验证窄窗口布局）
    py tests\\shot.py --catalog            :: 截图前真正构建一次程序目录
    py tests\\shot.py --out shot.png       :: 指定输出文件名

注意：文件名刻意不叫 `test_*.py`，否则会被 `unittest discover` 当成测试收集。
"""

from __future__ import annotations

import argparse
import ctypes
import os
import struct
import sys
import tempfile
import time
import zlib

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import winmon_core as core           # noqa: E402


def encode_png_rgb(rgb: bytes, width: int, height: int) -> bytes:
    """把 RGB 裸像素编码成 PNG（只用 zlib，不依赖 Pillow）。"""
    stride = width * 3
    raw = bytearray()
    for y in range(height):
        raw.append(0)                                  # 每行的 filter 字节
        raw += rgb[y * stride:(y + 1) * stride]

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(bytes(raw), 6))
            + chunk(b"IEND", b""))


def build_app(width: int, height: int):
    """
    只构建界面（`_build_ui`），不启动采集线程与定时器 —— 截图不需要它们，
    而且没有后台线程时脚本更容易干净退出。
    """
    import tkinter as tk
    import winmon

    root = tk.Tk()
    root.title(f"WinMon {winmon.APP_VER} — 桌面程序实时监控与终止")
    root.geometry(f"{width}x{height}+0+0")
    app = winmon.WinMonApp.__new__(winmon.WinMonApp)
    app.root = root
    # 设置与历史日志都写进临时目录：截图工具也不该碰用户真实数据、
    # 更不该在程序目录里留下文件（程序目录只留源与文档，2026-10-16 起）
    data_tmp = tempfile.mkdtemp(prefix="winmon-shot-")
    app.history = core.ActionHistory(log_path=os.path.join(data_tmp, "winmon_history.log"))
    app.program_prefs = core.CatalogPrefs()
    app.icons = winmon.IconProvider(winmon.ICON_SIZE)
    app._settings_path = os.path.join(data_tmp, "winmon_settings.json")
    app._settings_after_id = None
    app.show_hidden = tk.BooleanVar(value=False)
    app.show_no_window = tk.BooleanVar(value=False)
    app.only_windowed = tk.BooleanVar(value=True)
    app.follow_selection = tk.BooleanVar(value=True)
    app.live_gallery = tk.BooleanVar(value=True)
    app.history_visible = tk.BooleanVar(value=False)
    app.fps_choice = tk.StringVar(value="很流畅 10 FPS")
    app.search_text = tk.StringVar()
    app.status_text = tk.StringVar(value="WinMon 就绪（截图用）")
    app.overview_text = tk.StringVar(value="窗口 3 · 程序 3 · 进程 285")
    app._init_video_vars()          # 视频联动 / 媒体 / 关闭行为等变量统一在这里创建
    app.video_close_mode.set("smart")
    app._build_ui()
    root.update_idletasks()
    root.update()
    return root, app


def main() -> int:
    ap = argparse.ArgumentParser(description="WinMon 界面截图工具")
    ap.add_argument("--width", type=int, default=1120)
    ap.add_argument("--height", type=int, default=720)
    ap.add_argument("--font", type=int, default=0,
                    help="字号大小（0 = 用默认 9）")
    ap.add_argument("--tab", type=int, default=0,
                    help="标签页序号：0 实时预览 / 1 实时总览 / 2 进程树 / 3 程序目录")
    ap.add_argument("--catalog", action="store_true",
                    help="截图前真正构建一次程序目录（让目录页有内容）")
    ap.add_argument("--out", default="", help="输出 PNG 路径")
    args = ap.parse_args()

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    if args.font:
        import winmon
        winmon.set_font_size(args.font)
    root, app = build_app(args.width, args.height)
    if args.tab:
        app.notebook.select(args.tab)
        root.update()
    if args.catalog or args.tab == 3:
        items = core.build_program_catalog(use_cache=True)
        app.catalog._on_loaded(items)
        root.update()

    root.attributes("-topmost", True)
    root.lift()
    root.update()
    time.sleep(0.6)                                    # 等窗口真的画出来
    root.update()

    hwnd = core.H(core.user32.GetAncestor(core.w.HWND(root.winfo_id()), core.GA_ROOT))
    rc = core.w.RECT()
    core.user32.GetWindowRect(core.w.HWND(hwnd), ctypes.byref(rc))
    ww, hh = rc.right - rc.left, rc.bottom - rc.top
    cap = core.WindowCapturer()
    try:
        raw, ow, oh = cap.grab_window(hwnd, ww, hh, ww, hh, quality=True)
    finally:
        cap.release()

    rgb = bytearray(len(raw))                          # BGR -> RGB
    for i in range(0, len(raw), 3):
        rgb[i], rgb[i + 1], rgb[i + 2] = raw[i + 2], raw[i + 1], raw[i]

    out = args.out or os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        f"shot-tab{args.tab}-{args.width}.png")
    with open(out, "wb") as fh:
        fh.write(encode_png_rgb(bytes(rgb), ow, oh))
    print(f"已写出 {out}（{ow}×{oh}）")
    root.destroy()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
