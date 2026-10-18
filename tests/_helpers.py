# -*- coding: utf-8 -*-
"""
WinMon 测试公共辅助（仅标准库）。

设计原则
--------
1. **零第三方依赖**：只用 `unittest` / `ctypes` / `subprocess` / `tkinter`，
   与项目本体保持一致。
2. **不可运行的用例要"跳过"而不是"失败"**：本项目大量依赖真实桌面环境
   （可见窗口、图形会话、可交互的 shell）。由非交互式 shell 或服务会话运行时，
   这些前提不成立，应当 skip 并说明原因，而不是给出假红灯。
3. **测试自己负责"不留垃圾"**：临时进程一定会被回收（`sleep_child` 用
   addCleanup 兜底），临时文件写在工作区内的临时目录里。

放在这里的都是"多个测试文件共用"的东西；单个文件独有的逻辑就写在那个文件里。
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as w
import os
import subprocess
import sys
import tempfile
import time
import unittest

# ---------------------------------------------------------------------------
# 路径：让测试文件能 import 到项目本体（winmon_core / winmon）
# ---------------------------------------------------------------------------
TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(TESTS_DIR)
if PROJECT_DIR not in sys.path:
    sys.path.insert(0, PROJECT_DIR)

import winmon_core as core                                    # noqa: E402

#: 项目里的六个 Python 源文件（测试会逐个做语法/编码检查）。
#: 新增顶层模块时记得加进来 —— 否则它的编码损坏不会被 test_a1 拦住。
SOURCE_FILES = ["winmon_core.py", "winmon.py", "winmon_ai.py", "winmon_mcp.py",
                "winmon_ai_provider.py", "scan_windows.py", "close_others.py"]

#: 测试期间不要弹系统通知（AI 事件检测是新的常驻逻辑，别打扰跑测试的人）
os.environ.setdefault("WINMON_AI_NOTIFY", "0")
#: 事件文件指到系统临时目录，别写用户真实的 %LOCALAPPDATA%\WinMon
os.environ.setdefault("WINMON_AI_INCIDENTS",
                      os.path.join(tempfile.gettempdir(), "winmon-test-incidents.json"))
#: 用量/预算/直连配置也指到系统临时目录（测试不写用户真实数据）
os.environ.setdefault("WINMON_AI_USAGE",
                      os.path.join(tempfile.gettempdir(), "winmon-test-usage.jsonl"))
os.environ.setdefault("WINMON_AI_BUDGET",
                      os.path.join(tempfile.gettempdir(), "winmon-test-budget.json"))
os.environ.setdefault("WINMON_AI_PROVIDER_FILE",
                      os.path.join(tempfile.gettempdir(), "winmon-test-provider.json"))

#: 中文最多的文档，也一起守着编码约定。
#:
#: 为什么需要：当时用 PowerShell 的
#: `Get-Content -Raw | Set-Content -Encoding UTF8` 批量替换文档里的数字时，
#: **内容没坏、但被加上了 UTF-8 BOM** —— 正是坑 1 说的那类操作。
#: BOM 会让某些工具误判编码，项目约定是不带 BOM。
#:
#: `CHANGELOG.md` 是 2026-10-16 加的：它同样是面向人的中文文档，
#: 一样会被"批量改数字"这类操作碰到，所以一并纳入守卫。
#: `MCP.md` 同理（它是方案文档，中文最多的一份）。
DOC_FILES = ["README.md", "HANDOFF.md", "CHANGELOG.md", "MCP.md"]


# ---------------------------------------------------------------------------
# tkinter 可用性
# ---------------------------------------------------------------------------
def tkinter_available() -> tuple[bool, str]:
    """
    探测 tkinter 能否真的开窗口。

    返回 (可用, 原因)。不可用时原因是给人看的说明文字。
    只 import tkinter 是不够的：无图形会话（Session 0 / 无桌面）时
    `tk.Tk()` 会抛 TclError，因此这里实测一次。
    """
    try:
        import tkinter as tk
    except Exception as exc:                                   # pragma: no cover
        return False, f"无法 import tkinter：{exc}"
    root = None
    try:
        root = tk.Tk()
        root.withdraw()
        return True, ""
    except Exception as exc:
        return False, f"tk.Tk() 失败（多半是没有可用的图形会话）：{exc}"
    finally:
        if root is not None:
            try:
                root.destroy()
            except Exception:
                pass


_TK_STATE: tuple[bool, str] | None = None


def tk_state() -> tuple[bool, str]:
    """缓存一次探测结果，避免每个用例都开一次窗口。"""
    global _TK_STATE
    if _TK_STATE is None:
        _TK_STATE = tkinter_available()
    return _TK_STATE


def needs_tk(obj):
    """类装饰器：Tk 不可用时跳过整个 TestCase（附原因）。"""
    ok, why = tk_state()
    return unittest.skipUnless(ok, why or "需要 tkinter 图形会话")(obj)


def cancel_after(root) -> None:
    """
    取消一个 Tk 根窗口上所有挂起的 `after` 回调。

    为什么需要：测试里很多地方直接 `root.destroy()`。只要还有挂起的定时器
    （搜索防抖 / 批量启动收尾 / 托盘轮询 / 预览泵），Tcl 会在窗口销毁后继续派发，
    往 stderr 打印 `invalid command name "…apply_filter"`。单个模块跑时通常看不到，
    **全量跑时后面的模块一建 Tk 就会把这些残留打出来** —— 真红灯会被它淹掉。
    """
    try:
        pending = root.tk.splitlist(root.tk.call("after", "info"))
    except Exception:
        return
    for after_id in pending:
        try:
            root.after_cancel(after_id)
        except Exception:
            pass


def destroy_root(root) -> None:
    """先取消定时器、再销毁根窗口（测试收尾统一走这里）。"""
    cancel_after(root)
    try:
        root.destroy()
    except Exception:
        pass


def shutdown_app(app) -> None:
    """
    收尾一个真的 `WinMonApp`：按 `on_close` 的顺序走（存设置 → 停线程 → 销毁窗口）。

    原来 test_b3 / test_b8（两处）/ test_b10 各自抄了一份，顺序稍有出入就会
    变成"设置没存 / 线程没停 / 定时器还在跑"的随机红灯 —— 所以只留这一处。

    兜底分支必须**先取消定时器再 destroy**：否则 Tcl 会在窗口销毁后继续派发
    `_pump_frames` / `_poll_tray` 这些常驻循环，往 stderr 打印
    `invalid command name "…"`（全量跑时上百行噪音，真红灯会被淹掉）。
    """
    try:
        app.on_close()
        return
    except Exception:
        pass
    try:
        app.worker.shutdown()
    except Exception:
        pass
    try:
        app._cancel_pending_after()
    except Exception:
        pass
    try:
        app.root.destroy()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# 临时子进程
# ---------------------------------------------------------------------------
def sleep_child(seconds: int = 120) -> subprocess.Popen:
    """启动一个"什么都不做"的子进程，用于验证终止能力。"""
    flags = 0
    if os.name == "nt":
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return subprocess.Popen([sys.executable, "-c",
                             f"import time; time.sleep({int(seconds)})"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            creationflags=flags)


def spawn_cleanup(testcase: unittest.TestCase, proc: subprocess.Popen) -> subprocess.Popen:
    """登记一个子进程，确保用例结束后一定把它收掉（测试失败也不漏）。"""
    def _kill():
        try:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=5)
        except Exception:
            pass
    testcase.addCleanup(_kill)
    return proc


def wait_alive(pid: int, seconds: float = 3.0) -> bool:
    """
    轮询等待进程"出现并存活"。

    为什么不用固定 sleep：进程创建到能被 OpenProcess 打开之间有几十毫秒的窗口，
    固定 sleep 会偶发假失败。这里轮询到真出现为止（超时返回 False）。
    """
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if core.is_process_alive(pid):
            return True
        time.sleep(0.05)
    return False


def wait_dead(pid: int, seconds: float = 5.0) -> bool:
    """轮询等待进程消失；超时返回 False。"""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if not core.is_process_alive(pid):
            return True
        time.sleep(0.05)
    return False


# ---------------------------------------------------------------------------
# 桌面窗口探测
# ---------------------------------------------------------------------------
def desktop_windows(visible_only: bool = True) -> list:
    """当前桌面上的窗口列表（枚举失败时返回空列表，不抛异常）。"""
    try:
        wins = core.collect_desktop(include_cloaked=True, with_detail=False)
    except Exception:
        return []
    if visible_only:
        wins = [wi for wi in wins if not wi.is_hidden_surface]
    return wins


def pick_capture_target(min_w: int = 320, min_h: int = 240):
    """
    挑一个适合做抓屏测试的窗口。

    优先级：非本进程、非隐藏、非最小化、尺寸够大。找不到返回 None
    （调用方应当 skip）。
    """
    mine = os.getpid()
    cands = [wi for wi in desktop_windows(visible_only=True)
             if wi.pid != mine and not wi.iconic
             and wi.rect_w >= min_w and wi.rect_h >= min_h]
    if not cands:
        return None
    cands.sort(key=lambda x: -x.rect_w * x.rect_h)
    return cands[0]


def pick_window_for_pid(pid: int):
    """找一个属于指定 PID 的可见窗口（找不到返回 None）。"""
    for wi in desktop_windows(visible_only=True):
        if wi.pid == pid:
            return wi
    return None


# ---------------------------------------------------------------------------
# 进程树枚举（测试自己实现一份，避免"用被测代码验证被测代码"）
# ---------------------------------------------------------------------------
TH32CS_SNAPPROCESS = 0x00000002


class _PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [("dwSize", w.DWORD), ("cntUsage", w.DWORD),
                ("th32ProcessID", w.DWORD), ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
                ("th32ModuleID", w.DWORD), ("cntThreads", w.DWORD),
                ("th32ParentProcessID", w.DWORD), ("pcPriClassBase", ctypes.c_long),
                ("dwFlags", w.DWORD), ("szExeFile", ctypes.c_wchar * 260)]


def independent_ppid_map() -> dict[int, int]:
    """
    用独立的 Toolhelp 调用枚举 pid -> ppid。

    刻意不复用 `core.query_process_ppid`：验证"父子关系"这类结论时，
    拿被测实现去对照被测实现等于没验。这份实现只用于交叉验证。
    """
    out: dict[int, int] = {}
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateToolhelp32Snapshot.restype = w.HANDLE
    snap = k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    snap = core.H(snap)
    if not snap or snap == core.H(-1):
        return out
    try:
        entry = _PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(entry)
        ok = k32.Process32FirstW(w.HANDLE(snap), ctypes.byref(entry))
        while ok:
            out[int(entry.th32ProcessID)] = int(entry.th32ParentProcessID)
            ok = k32.Process32NextW(w.HANDLE(snap), ctypes.byref(entry))
    finally:
        k32.CloseHandle(w.HANDLE(snap))
    return out


# ---------------------------------------------------------------------------
# PNG 结构校验（测试用，独立实现，不复用 core 里的编码器）
# ---------------------------------------------------------------------------
def parse_png(data: bytes) -> dict:
    """
    极简 PNG 解析：返回 {width, height, bit_depth, color_type, chunks}。

    同时校验每个 chunk 的 CRC 与 IEND 结尾 —— 这样"图标是不是一张合法 PNG"
    就是可判定的，而不是"看起来像"。
    """
    import struct
    import zlib

    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("PNG 签名不正确")
    pos = 8
    out = {"chunks": [], "width": 0, "height": 0, "bit_depth": 0, "color_type": 0}
    while pos + 8 <= len(data):
        (length,) = struct.unpack(">I", data[pos:pos + 4])
        tag = data[pos + 4:pos + 8]
        body = data[pos + 8:pos + 8 + length]
        if len(body) != length:
            raise ValueError(f"chunk {tag!r} 长度不足")
        (crc,) = struct.unpack(">I", data[pos + 8 + length:pos + 12 + length])
        if crc != (zlib.crc32(tag + body) & 0xFFFFFFFF):
            raise ValueError(f"chunk {tag!r} CRC 校验失败")
        out["chunks"].append(tag.decode("ascii", "replace"))
        if tag == b"IHDR":
            out["width"], out["height"], out["bit_depth"], out["color_type"] = \
                struct.unpack(">IIBB", body[:10])
        pos += 12 + length
    if pos != len(data):
        raise ValueError("PNG 数据尾部有多余字节")
    if out["chunks"][-1:] != ["IEND"]:
        raise ValueError("PNG 未以 IEND 结束")
    return out


def nonzero_ratio(raw: bytes, step: int = 503) -> float:
    """裸像素里非零字节的比例（抓屏是否"全黑"的判据）。"""
    sample = raw[::step]
    if not sample:
        return 0.0
    return 100.0 * sum(1 for b in sample if b) / len(sample)
