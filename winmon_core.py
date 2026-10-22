# -*- coding: utf-8 -*-
"""
WinMon 核心层 —— Win32 窗口/进程采集、画面捕获、进程结束、图标提取
=====================================================================

这一层完全不依赖 tkinter，可以被任何 Python 程序当库使用：

    import winmon_core as core
    wins = core.collect_desktop()              # 当前屏幕上所有可见窗口
    frame = core.WindowCapturer().grab_window(hwnd, w, h, 480, 300)
    core.terminate_process(pid)
    print(core.get_system_summary())

图形界面在 winmon.py，命令行工具在 scan_windows.py / close_others.py。

实现要点（都是在本机实测后定下来的）：
  * 捕获：PrintWindow(PW_RENDERFULLCONTENT) + GDI StretchBlt 缩放 + 24 位 DIB 取像素
    —— 对遮挡窗口同样有效；DWM 缩略图实测不渲染，故未采用
  * 句柄：64 位下 GDI/USER 句柄高 32 位是符号扩展的，统一按 32 位掩码归一化
  * 进程信息：句柄池 + 创建时间校验，防止 PID 复用读到错误进程
  * 图标：SHGetFileInfoW / WM_GETICON 取 HICON，自己编码 PNG（不依赖 Pillow）
"""

from __future__ import annotations

import base64
import ctypes
import json
import locale
import os
import queue
import shutil
import struct
import subprocess
import sys
import threading
import time
import zlib
from ctypes import wintypes as w

#: 本程序所在目录（**只有源与文档**；运行时的数据见 `data_dir()`）。
APP_DIR = os.path.dirname(os.path.abspath(__file__))

# ============================================================================
# 零点五、运行时数据目录（设置 / 缓存 / 日志 / 媒体快照）
# ============================================================================
#
# 为什么不再直接写在程序目录里（2026-10-16 改）：
#   · 程序目录是**源**（+ 文档）。跑一下就在里面冒出缓存、日志、快照，
#     对"只想试用一下"的人是纯粹的垃圾，也会把 git 工作区弄脏
#     （clone 下来跑一次，`git status` 就有一堆没跟踪的文件）；
#   · Windows 的标准位置是每个用户自己的 `%LOCALAPPDATA%`，这才是"用户数据"该待的地方。
#
# 但仍然要照顾"绿色程序"这条卖点（README 写着"整个文件夹拷过去就能跑"）：
#   · 设 `WINMON_DATA_DIR=<某个目录>` 就回到**便携模式**，数据跟着那个目录走；
#   · 旧的（写在程序目录里的）数据文件会被**一次性迁移**过去，见
#     `migrate_legacy_data_files()` —— 用户不用手动搬。

#: 运行时数据目录的覆盖变量（**便携模式** / 测试用）
DATA_DIR_ENV = "WINMON_DATA_DIR"

#: 旧版本写在**程序目录**里的那些文件名（迁移用）。
#: 只搬这些已知名字 —— 迁移代码绝不去猜、也不碰别的文件。
LEGACY_DATA_NAMES: tuple[str, ...] = (
    "winmon_settings.json",
    "winmon_catalog.json",
    "winmon_history.log",
    "winmon_history.log.1",
    "winmon_history.log.bak",
    "winmon_media.jsonl",
    "winmon_media.jsonl.tmp",
    "winmon_error.log",
)


def data_dir() -> str:
    """
    运行时数据目录（设置 / 程序目录缓存 / 操作历史日志 / 媒体快照 / 错误日志）。

    优先级：
      1. 环境变量 `WINMON_DATA_DIR`（**便携模式**：数据跟着你指定的目录走）；
      2. `%LOCALAPPDATA%\\WinMon`（Windows 标准位置：程序目录保持干净）；
      3. 退回程序目录（拿不到 LOCALAPPDATA 时的兜底，例如非 Windows 或精简环境）。

    **只算路径，不建目录** —— 建目录由 `ensure_data_dir()` 负责，
    这样"读不到就算了"的调用方（例如读快照）不会因为一次读取就创出目录来。
    """
    override = os.environ.get(DATA_DIR_ENV)
    if override:
        return os.path.abspath(override)
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA") or ""
    if base:
        return os.path.join(base, "WinMon")
    return APP_DIR


def ensure_data_dir() -> str:
    """确保数据目录存在（写文件之前调），返回它的路径；建不出来也不抛异常。"""
    path = data_dir()
    try:
        os.makedirs(path, exist_ok=True)
    except OSError:
        # 建不出来（只读介质 / 权限）就退回程序目录 —— 宁可写在旁边，
        # 也不要让"设置存不下来"变成静默的数据丢失。
        return APP_DIR
    return path


def data_file(name: str) -> str:
    """数据目录下某个文件的完整路径（**只拼路径，不建目录**）。"""
    return os.path.join(data_dir(), str(name))


def legacy_data_file(name: str) -> str:
    """旧版本的位置：程序目录下的同名文件（迁移与兼容读取用）。"""
    return os.path.join(APP_DIR, str(name))


def migrate_legacy_data_files(src_dir: str = "", dst_dir: str = "") -> list[str]:
    """
    把旧版本留在**程序目录**里的数据文件搬到数据目录（一次性兼容迁移）。

    三条安全约定：
      · **只搬已知名字**（`LEGACY_DATA_NAMES`），不猜、不递归、不碰别的文件；
      · 目标已存在就**跳过**（绝不覆盖新数据）；
      · **先校验副本再删原件**（大小 + 逐字节比对），任何一步失败就原样留着，
        并把失败原因回报给调用方 —— 宁可目录里多一个旧文件，也不能丢用户的数据。

    返回"已搬过去"的文件名列表。失败的不在里面（调用方按需打印）。
    搬完原文件就没了，程序目录恢复干净。
    """
    src = src_dir or APP_DIR
    dst = dst_dir or data_dir()
    if os.path.abspath(src) == os.path.abspath(dst):
        return []                       # 便携模式：本来就在一处，没什么可搬
    moved: list[str] = []
    try:
        os.makedirs(dst, exist_ok=True)
    except OSError:
        return moved
    for name in LEGACY_DATA_NAMES:
        old = os.path.join(src, name)
        new = os.path.join(dst, name)
        if not os.path.isfile(old) or os.path.exists(new):
            continue
        try:
            shutil.copy2(old, new)
            if (os.path.getsize(old) != os.path.getsize(new)
                    or open(old, "rb").read() != open(new, "rb").read()):
                continue                # 副本不对：留着原件，下次再说
            os.remove(old)
            moved.append(name)
        except OSError:
            continue
    return moved


# ============================================================================
# 一、Win32 API 绑定
# ============================================================================

user32 = ctypes.WinDLL("user32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)


class _LASTINPUTINFO(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.c_uint),
                ("dwTime", ctypes.c_uint)]


def seconds_since_last_input() -> float:
    """
    距离上一次键盘/鼠标输入有多少秒（用于"该休息了"的本地判断）。

    读不到就返回 0（当作"刚刚有输入"），绝不因此把用户判成闲置。
    """
    try:
        info = _LASTINPUTINFO()
        info.cbSize = ctypes.sizeof(_LASTINPUTINFO)
        if not user32.GetLastInputInfo(ctypes.byref(info)):
            return 0.0
        tick = int(kernel32.GetTickCount()) & 0xFFFFFFFF
        delta = (tick - int(info.dwTime)) & 0xFFFFFFFF
        return max(0.0, min(86400.0, delta / 1000.0))
    except Exception:
        return 0.0

# ---- 常量 ----
GW_OWNER = 4
GW_HWNDFIRST = 0
GW_HWNDNEXT = 2
GA_ROOT = 2
GWL_STYLE = -16
GWL_EXSTYLE = -20
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_APPWINDOW = 0x00040000
WS_EX_TOPMOST = 0x00000008
GCLP_HICON = -14
GCLP_HICONSM = -34
ICON_SMALL = 0
ICON_BIG = 1
ICON_SMALL2 = 2
WM_GETICON = 0x007F
WM_SETICON = 0x0080
WM_CLOSE = 0x0010
SHGFI_ICON = 0x000000100
SHGFI_LARGEICON = 0x000000000
SHGFI_SMALLICON = 0x000000001
PW_RENDERFULLCONTENT = 0x00000002
COLORONCOLOR = 3
HALFTONE = 4
BI_RGB = 0
DIB_RGB_COLORS = 0
SRCCOPY = 0x00CC0020
BLACKNESS = 0x00FF0062
SW_RESTORE = 9
SW_SHOW = 5
SW_MINIMIZE = 6
SWP_NOZORDER = 0x0004
SWP_NOACTIVATE = 0x0010
SWP_SHOWWINDOW = 0x0040
#: 虚拟桌面（所有显示器合起来）的范围
SM_XVIRTUALSCREEN, SM_YVIRTUALSCREEN = 76, 77
SM_CXVIRTUALSCREEN, SM_CYVIRTUALSCREEN = 78, 79
DWMWA_CLOAKED = 14
PROCESS_TERMINATE = 0x0001
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
STILL_ACTIVE = 259
TH32CS_SNAPPROCESS = 0x00000002
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
MAX_PATH_LONG = 32768


def H(value) -> int:
    """把任意 Win32 句柄归一化为 32 位无符号整数。

    GDI/USER 句柄在 64 位进程下常表现为 0xFFFFFFFF????????（高 32 位符号扩展），
    而 wintypes.HDC 等类型按 64 位校验，直接传递会抛 OverflowError。
    Windows 句柄实际只有低 32 位有意义，掩码后即可安全传入。
    """
    if not value:
        return 0
    if isinstance(value, int):
        return value & 0xFFFFFFFF
    return ctypes.cast(value, ctypes.c_void_p).value & 0xFFFFFFFF


# ---- 函数原型 ----
WNDENUMPROC = ctypes.WINFUNCTYPE(w.BOOL, w.HWND, w.LPARAM)

user32.EnumWindows.argtypes = [WNDENUMPROC, w.LPARAM]
user32.EnumWindows.restype = w.BOOL
user32.IsWindow.argtypes = [w.HWND]
user32.IsWindowVisible.argtypes = [w.HWND]
user32.IsIconic.argtypes = [w.HWND]
user32.GetWindow.argtypes = [w.HWND, w.UINT]
user32.GetWindow.restype = w.HWND
user32.GetAncestor.argtypes = [w.HWND, w.UINT]
user32.GetAncestor.restype = w.HWND
user32.GetForegroundWindow.restype = w.HWND
user32.GetWindowTextLengthW.argtypes = [w.HWND]
user32.GetWindowTextW.argtypes = [w.HWND, w.LPWSTR, ctypes.c_int]
user32.GetClassNameW.argtypes = [w.HWND, w.LPWSTR, ctypes.c_int]
user32.GetWindowThreadProcessId.argtypes = [w.HWND, ctypes.POINTER(w.DWORD)]
user32.GetWindowRect.argtypes = [w.HWND, ctypes.POINTER(w.RECT)]
user32.GetClientRect.argtypes = [w.HWND, ctypes.POINTER(w.RECT)]
user32.GetWindowLongPtrW.argtypes = [w.HWND, ctypes.c_int]
user32.GetWindowLongPtrW.restype = ctypes.c_ssize_t
user32.GetClassLongPtrW.argtypes = [w.HWND, ctypes.c_int]
user32.GetClassLongPtrW.restype = ctypes.c_ssize_t
user32.SendMessageW.argtypes = [w.HWND, w.UINT, w.WPARAM, w.LPARAM]
user32.SendMessageW.restype = ctypes.c_ssize_t
user32.PrintWindow.argtypes = [w.HWND, w.HDC, w.UINT]
user32.PrintWindow.restype = w.BOOL
user32.GetWindowDC.argtypes = [w.HWND]
user32.GetWindowDC.restype = w.HDC
user32.GetDC.argtypes = [w.HWND]
user32.GetDC.restype = w.HDC
user32.ReleaseDC.argtypes = [w.HWND, w.HDC]
user32.PostMessageW.argtypes = [w.HWND, w.UINT, w.WPARAM, w.LPARAM]
# ---- 托盘（通知区域图标）相关：**必须显式声明 argtypes** ----
# 不声明的话 ctypes 会把 `WNDCLASSW` / 菜单句柄当 int 传，得到的返回值是垃圾
# （实测：`RegisterClassW` 返回一个假 atom、随后 `CreateWindowExW` 报错误码 0）。
#: 类名（每个进程注册一次）
user32.RegisterClassW.argtypes = [ctypes.c_void_p]
user32.RegisterClassW.restype = w.WORD
user32.UnregisterClassW.argtypes = [w.LPCWSTR, w.HINSTANCE]
user32.CreateWindowExW.argtypes = [w.DWORD, w.LPCWSTR, w.LPCWSTR, w.DWORD,
                                   ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                   ctypes.c_int, w.HWND, w.HMENU, w.HINSTANCE,
                                   w.LPVOID]
user32.CreateWindowExW.restype = w.HWND
user32.DestroyWindow.argtypes = [w.HWND]
user32.DestroyWindow.restype = w.BOOL
user32.DefWindowProcW.argtypes = [w.HWND, w.UINT, w.WPARAM, w.LPARAM]
user32.DefWindowProcW.restype = ctypes.c_ssize_t
user32.PeekMessageW.argtypes = [ctypes.c_void_p, w.HWND, w.UINT, w.UINT, w.UINT]
user32.PeekMessageW.restype = w.BOOL
user32.CreatePopupMenu.argtypes = []
user32.CreatePopupMenu.restype = w.HMENU
user32.AppendMenuW.argtypes = [w.HMENU, w.UINT, ctypes.c_size_t, w.LPCWSTR]
user32.AppendMenuW.restype = w.BOOL
user32.TrackPopupMenu.argtypes = [w.HMENU, w.UINT, ctypes.c_int, ctypes.c_int,
                                  ctypes.c_int, w.HWND, ctypes.c_void_p]
user32.TrackPopupMenu.restype = w.BOOL
user32.DestroyMenu.argtypes = [w.HMENU]
user32.GetCursorPos.argtypes = [ctypes.c_void_p]
user32.SetForegroundWindow.argtypes = [w.HWND]
user32.LoadIconW.argtypes = [w.HINSTANCE, w.LPCWSTR]
user32.LoadIconW.restype = w.HICON
user32.SetWindowLongPtrW.argtypes = [w.HWND, ctypes.c_int, ctypes.c_void_p]
user32.SetWindowLongPtrW.restype = ctypes.c_void_p
user32.CallWindowProcW.argtypes = [ctypes.c_void_p, w.HWND, w.UINT, w.WPARAM, w.LPARAM]
user32.CallWindowProcW.restype = ctypes.c_ssize_t
kernel32.GetModuleHandleW.argtypes = [w.LPCWSTR]
kernel32.GetModuleHandleW.restype = w.HMODULE
#: `SetWindowLongPtrW` 的子索引（x64 只有 Ptr 版，x86 没有 GWLP_* 的区分）
GWLP_WNDPROC = -4
# 注意：shell32 相关的声明放在它被加载之后（见下面 `shell32 = ctypes.WinDLL(...)` 附近），
# 这里引用会 NameError。
user32.SetForegroundWindow.argtypes = [w.HWND]
user32.ShowWindow.argtypes = [w.HWND, ctypes.c_int]
user32.BringWindowToTop.argtypes = [w.HWND]
#: 恢复工作区快照时要"只挪位置、不改前后顺序、也不抢焦点"，
#: 所以用 SetWindowPos 而不是 MoveWindow（后者对最大化窗口会先还原、语义不同）。
user32.SetWindowPos.argtypes = [w.HWND, w.HWND, ctypes.c_int, ctypes.c_int,
                                ctypes.c_int, ctypes.c_int, w.UINT]
user32.SetWindowPos.restype = w.BOOL
user32.IsHungAppWindow.argtypes = [w.HWND]
user32.GetSystemMetrics.argtypes = [ctypes.c_int]
user32.EnumDisplaySettingsW.argtypes = [w.LPCWSTR, w.DWORD, ctypes.c_void_p]
user32.EnumDisplaySettingsW.restype = w.BOOL
user32.SetProcessDPIAware.restype = w.BOOL
# 注意：DrawIconEx 导出在 user32 而不是 gdi32
user32.DrawIconEx.argtypes = [w.HDC, ctypes.c_int, ctypes.c_int, w.HANDLE,
                              ctypes.c_int, ctypes.c_int, w.UINT, w.HANDLE, w.UINT]
user32.DrawIconEx.restype = w.BOOL
user32.DestroyIcon.argtypes = [w.HANDLE]
user32.DestroyIcon.restype = w.BOOL
user32.keybd_event.argtypes = [w.BYTE, w.BYTE, w.DWORD, ctypes.c_void_p]
user32.keybd_event.restype = None
user32.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]
user32.SetCursorPos.restype = w.BOOL
user32.GetCursorPos.argtypes = [ctypes.POINTER(w.POINT)]
user32.GetCursorPos.restype = w.BOOL
user32.mouse_event.argtypes = [w.DWORD, w.DWORD, w.DWORD, w.DWORD, ctypes.c_void_p]
user32.mouse_event.restype = None
user32.WindowFromPoint.argtypes = [w.POINT]
user32.WindowFromPoint.restype = w.HWND
user32.ClientToScreen.argtypes = [w.HWND, ctypes.POINTER(w.POINT)]
user32.ClientToScreen.restype = w.BOOL
user32.ScreenToClient.argtypes = [w.HWND, ctypes.POINTER(w.POINT)]
user32.ScreenToClient.restype = w.BOOL
user32.IsIconic.argtypes = [w.HWND]
user32.IsIconic.restype = w.BOOL
user32.SetActiveWindow.argtypes = [w.HWND]
user32.SetActiveWindow.restype = w.HWND
user32.SetFocus.argtypes = [w.HWND]
user32.SetFocus.restype = w.HWND
user32.AttachThreadInput.argtypes = [w.DWORD, w.DWORD, w.BOOL]
user32.AttachThreadInput.restype = w.BOOL
user32.GetWindowThreadProcessId.restype = w.DWORD
kernel32.GetCurrentThreadId.argtypes = []
kernel32.GetCurrentThreadId.restype = w.DWORD

gdi32.CreateCompatibleDC.argtypes = [w.HDC]
gdi32.CreateCompatibleDC.restype = w.HDC
gdi32.CreateCompatibleBitmap.argtypes = [w.HDC, ctypes.c_int, ctypes.c_int]
gdi32.CreateCompatibleBitmap.restype = w.HBITMAP
gdi32.CreateDIBSection.argtypes = [w.HDC, ctypes.c_void_p, w.UINT,
                                   ctypes.POINTER(ctypes.c_void_p), w.HANDLE, w.DWORD]
gdi32.CreateDIBSection.restype = w.HBITMAP
gdi32.SelectObject.argtypes = [w.HDC, w.HGDIOBJ]
gdi32.SelectObject.restype = w.HGDIOBJ
gdi32.DeleteObject.argtypes = [w.HGDIOBJ]
gdi32.DeleteDC.argtypes = [w.HDC]
gdi32.StretchBlt.argtypes = [w.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                             w.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, w.DWORD]
gdi32.SetStretchBltMode.argtypes = [w.HDC, ctypes.c_int]
gdi32.SetBrushOrgEx.argtypes = [w.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_void_p]
gdi32.PatBlt.argtypes = [w.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, w.DWORD]
gdi32.PatBlt.restype = w.BOOL

kernel32.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]
kernel32.OpenProcess.restype = w.HANDLE
kernel32.CloseHandle.argtypes = [w.HANDLE]
kernel32.TerminateProcess.argtypes = [w.HANDLE, w.UINT]
kernel32.QueryFullProcessImageNameW.argtypes = [w.HANDLE, w.DWORD, w.LPWSTR, ctypes.POINTER(w.DWORD)]
kernel32.CreateToolhelp32Snapshot.argtypes = [w.DWORD, w.DWORD]
kernel32.CreateToolhelp32Snapshot.restype = w.HANDLE
kernel32.Process32FirstW.argtypes = [w.HANDLE, ctypes.c_void_p]
kernel32.Process32NextW.argtypes = [w.HANDLE, ctypes.c_void_p]
kernel32.GetProcessTimes.argtypes = [w.HANDLE, ctypes.c_void_p, ctypes.c_void_p,
                                     ctypes.POINTER(w.FILETIME), ctypes.POINTER(w.FILETIME)]
kernel32.GetExitCodeProcess.argtypes = [w.HANDLE, ctypes.POINTER(w.DWORD)]
kernel32.GetExitCodeProcess.restype = w.BOOL
kernel32.GetCurrentProcessId.restype = w.DWORD
kernel32.GetCurrentProcess.argtypes = []
kernel32.GetCurrentProcess.restype = w.HANDLE


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", w.DWORD),
        ("cntUsage", w.DWORD),
        ("th32ProcessID", w.DWORD),
        ("th32DefaultHeapID", ctypes.c_void_p),
        ("th32ModuleID", w.DWORD),
        ("cntThreads", w.DWORD),
        ("th32ParentProcessID", w.DWORD),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", w.DWORD),
        ("szExeFile", w.WCHAR * 260),
    ]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", w.DWORD), ("biWidth", ctypes.c_long), ("biHeight", ctypes.c_long),
                ("biPlanes", w.WORD), ("biBitCount", w.WORD), ("biCompression", w.DWORD),
                ("biSizeImage", w.DWORD), ("biXPelsPerMeter", ctypes.c_long),
                ("biYPelsPerMeter", ctypes.c_long), ("biClrUsed", w.DWORD),
                ("biClrImportant", w.DWORD)]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", w.DWORD * 3)]


class PROCESS_MEMORY_COUNTERS(ctypes.Structure):
    _fields_ = [("cb", w.DWORD), ("PageFaultCount", w.DWORD),
                ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t)]


class PROCESS_MEMORY_COUNTERS_EX(ctypes.Structure):
    """
    `PROCESS_MEMORY_COUNTERS` 的扩展版 —— 多出来的 `PrivateUsage` 就是任务管理器的
    「提交大小」列（私有提交，单位字节）。

    为什么必须用它：基础版只给工作集（`WorkingSetSize`），而**工作集里混着大量
    共享页**（DLL、字体、共享内存）。同一个进程，WinMon 按工作集显示会比
    任务管理器的「内存」列大 —— 实测 Edge 主进程 234MB vs 私有工作集 63MB。
    用户拿两个数一对，就会以为"WinMon 的数据不准"。多给一个私有口径才能解释清楚。
    """
    _fields_ = [("cb", w.DWORD), ("PageFaultCount", w.DWORD),
                ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t),
                ("PrivateUsage", ctypes.c_size_t)]


try:
    dwmapi = ctypes.WinDLL("dwmapi")
    dwmapi.DwmGetWindowAttribute.argtypes = [w.HWND, w.DWORD, ctypes.c_void_p, w.DWORD]
    dwmapi.DwmGetWindowAttribute.restype = ctypes.c_long
except OSError:  # pragma: no cover
    dwmapi = None

try:
    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
except OSError:  # pragma: no cover
    shell32 = None

if shell32 is not None:
    class SHFILEINFOW(ctypes.Structure):
        _fields_ = [("hIcon", w.HANDLE), ("iIcon", ctypes.c_int),
                    ("dwAttributes", w.DWORD), ("szDisplayName", w.WCHAR * 260),
                    ("szTypeName", w.WCHAR * 80)]

    shell32.SHGetFileInfoW.argtypes = [w.LPCWSTR, w.DWORD, ctypes.POINTER(SHFILEINFOW),
                                       w.UINT, ctypes.c_uint]
    shell32.SHGetFileInfoW.restype = ctypes.c_void_p
    # 托盘（通知区域图标）：同样必须显式声明，否则结构体地址会被当 int 截断。
    # 注意 `LoadImageW` 在 **user32** 里（不是 shell32 —— 写错会 AttributeError）
    shell32.Shell_NotifyIconW.argtypes = [w.DWORD, ctypes.c_void_p]
    shell32.Shell_NotifyIconW.restype = w.BOOL
    user32.LoadImageW.argtypes = [w.HINSTANCE, w.LPCWSTR, w.UINT,
                                  ctypes.c_int, ctypes.c_int, w.UINT]
    user32.LoadImageW.restype = w.HANDLE

try:
    _psapi = ctypes.WinDLL("psapi")
    # 注意第三/第二个参数用 `c_void_p` 而不是 `POINTER(PROCESS_MEMORY_COUNTERS)`：
    # 后者会把 EX 版结构卡死在 ctypes 的类型检查上
    # （`ArgumentError: expected LP_PROCESS_MEMORY_COUNTERS instance`），
    # 而 EX 版多出来的 `PrivateUsage` 读的就是任务管理器「提交大小」口径。
    # 之前这个错误一直被 `except Exception` 吞成 ("", 0, 0)，表现为"内存突然全是 0"。
    _psapi.GetProcessMemoryInfo.argtypes = [w.HANDLE, ctypes.c_void_p, w.DWORD]
    _psapi.GetProcessMemoryInfo.restype = w.BOOL
except OSError:  # pragma: no cover
    _psapi = None


# ============================================================================
# 二、窗口与进程采集
# ============================================================================

# 绝对禁止结束的系统关键进程（结束会导致蓝屏或系统不可用）
CRITICAL_PROCESSES = {
    "system", "registry", "memory compression", "idle",
    "smss.exe", "csrss.exe", "wininit.exe", "winlogon.exe", "services.exe", "lsass.exe",
    "lsaiso.exe", "svchost.exe", "fontdrvhost.exe", "dwm.exe", "wudfhost.exe",
    "ntoskrnl.exe", "secure system", "memcompression", "audiodg.exe", "sihost.exe",
}
# 结束会严重影响使用、需要额外确认的进程
RISKY_PROCESSES = {"explorer.exe", "shellexperiencehost.exe", "startmenuexperiencehost.exe",
                   "searchhost.exe", "taskmgr.exe", "ctfmon.exe", "textinputhost.exe"}


class WinInfo:
    """一个可见顶层窗口的信息。"""

    __slots__ = ("hwnd", "pid", "title", "cls", "rect_w", "rect_h",
                 "iconic", "cloaked", "hung", "proc", "path", "mem_kb", "mem_private_kb",
                 "cpu", "always_on_top")

    def __init__(self, hwnd, pid, title, cls, rw, rh, iconic, cloaked, hung):
        self.hwnd = hwnd
        self.pid = pid
        self.title = title
        self.cls = cls
        self.rect_w = rw
        self.rect_h = rh
        self.iconic = iconic
        self.cloaked = cloaked
        self.hung = hung
        self.proc = ""
        self.path = ""
        self.mem_kb = 0
        #: 私有提交（KB）—— 任务管理器「提交大小」列的口径（见 `query_process_memory`）
        self.mem_private_kb = 0
        self.cpu = 0.0
        self.always_on_top = False

    # ---- 特殊窗口识别 ----

    @property
    def is_frame_host(self) -> bool:
        """UWP（应用商店应用）的外框窗口。

        这类窗口由通用宿主 ApplicationFrameHost.exe 拥有，真正画界面的是另一个进程
        （例如「设置」界面的 SystemSettings.exe）。若不识别，同一个应用会被算成两个。
        """
        return self.cls == "ApplicationFrameWindow"

    @property
    def is_hidden_surface(self) -> bool:
        """被 DWM 隐藏（cloaked）的窗口。

        典型是 UWP 的占位/后台窗口：虽然 IsWindowVisible 为真、也有尺寸，
        但用户屏幕上根本看不到（输入法宿主 TextInputHost.exe 就是这种）。
        """
        return self.cloaked

    @property
    def real_name(self) -> str:
        """真正在为用户显示界面的软件名。

        对 UWP 外框窗口，进程名是 ApplicationFrameHost.exe，没有意义，
        因此改用窗口标题（即应用名，如「设置」）。
        """
        if self.is_frame_host:
            return self.title or self.proc or f"PID {self.pid}"
        return self.proc or f"PID {self.pid}"

    @property
    def state(self) -> str:
        if self.hung:
            return "无响应"
        if self.is_hidden_surface:
            return "后台隐藏"
        if self.iconic:
            return "已最小化"
        return "运行中"

    @property
    def display_name(self) -> str:
        return self.real_name


def _window_cloaked(hwnd: int) -> bool:
    if dwmapi is None:
        return False
    val = ctypes.c_int(0)
    try:
        rc = dwmapi.DwmGetWindowAttribute(w.HWND(hwnd), DWMWA_CLOAKED,
                                          ctypes.byref(val), ctypes.sizeof(val))
    except Exception:
        return False
    return rc == 0 and val.value != 0


def enum_windows(include_cloaked: bool = True) -> list[WinInfo]:
    """枚举桌面上所有「可见、非工具窗口、有标题」的顶层窗口。"""
    found: list[WinInfo] = []

    @WNDENUMPROC
    def _cb(hwnd, _lparam):
        h = H(hwnd)
        if not user32.IsWindowVisible(w.HWND(h)):
            return True
        if user32.GetWindow(w.HWND(h), GW_OWNER):
            return True
        ex = user32.GetWindowLongPtrW(w.HWND(h), GWL_EXSTYLE)
        if ex & WS_EX_TOOLWINDOW and not (ex & WS_EX_APPWINDOW):
            return True
        n = user32.GetWindowTextLengthW(w.HWND(h))
        if n <= 0:
            return True
        buf = ctypes.create_unicode_buffer(n + 1)
        user32.GetWindowTextW(w.HWND(h), buf, n + 1)
        title = buf.value.strip()
        if not title:
            return True
        pid = w.DWORD(0)
        user32.GetWindowThreadProcessId(w.HWND(h), ctypes.byref(pid))
        cls = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(w.HWND(h), cls, 256)
        cloaked = _window_cloaked(h)
        if cloaked and not include_cloaked:
            return True
        rc = w.RECT()
        user32.GetWindowRect(w.HWND(h), ctypes.byref(rc))
        info = WinInfo(
            hwnd=h, pid=pid.value, title=title, cls=cls.value,
            rw=max(0, rc.right - rc.left), rh=max(0, rc.bottom - rc.top),
            iconic=bool(user32.IsIconic(w.HWND(h))),
            cloaked=cloaked,
            hung=bool(user32.IsHungAppWindow(w.HWND(h))),
        )
        info.always_on_top = bool(ex & WS_EX_TOPMOST)
        found.append(info)
        return True

    user32.EnumWindows(_cb, 0)
    return found


def get_foreground_hwnd() -> int:
    """当前前台（用户正在操作）的顶层窗口句柄。"""
    try:
        h = H(user32.GetForegroundWindow())
        if not h:
            return 0
        return H(user32.GetAncestor(w.HWND(h), GA_ROOT)) or h
    except Exception:
        return 0


def get_window_pid(hwnd: int) -> int:
    """窗口属于哪个进程（只读；取不到返回 0）。"""
    try:
        pid = w.DWORD(0)
        user32.GetWindowThreadProcessId(w.HWND(int(hwnd)), ctypes.byref(pid))
        return int(pid.value)
    except Exception:
        return 0


def get_window_icon_handle(hwnd: int) -> int:
    """取窗口的大图标句柄（优先窗口自己的图标，其次窗口类图标）。借用，不销毁。"""
    for kind in (ICON_BIG, ICON_SMALL2, ICON_SMALL):
        try:
            h = H(user32.SendMessageW(w.HWND(hwnd), WM_GETICON, kind, 0))
            if h:
                return h
        except Exception:
            pass
    try:
        for idx in (GCLP_HICON, GCLP_HICONSM):
            h = H(user32.GetClassLongPtrW(w.HWND(hwnd), idx))
            if h:
                return h
    except Exception:
        pass
    return 0


def snapshot_processes() -> dict[int, dict]:
    """用 Toolhelp32 快照获取全部进程的 PID / 名称 / 父 PID / 线程数。"""
    result: dict[int, dict] = {}
    snap = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if not snap or H(snap) == H(INVALID_HANDLE_VALUE):
        return result
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        ok = kernel32.Process32FirstW(snap, ctypes.byref(entry))
        while ok:
            result[entry.th32ProcessID] = {
                "name": entry.szExeFile,
                "ppid": entry.th32ParentProcessID,
                "threads": entry.cntThreads,
                "mem_kb": 0,
                "mem_private_kb": 0,
                "path": "",
            }
            ok = kernel32.Process32NextW(snap, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snap)
    return result


# ============================================================================
# 三、进程句柄池与进程信息查询
# ============================================================================

class ProcessHandlePool:
    """
    进程句柄池：缓存 OpenProcess 得到的句柄，用于反复查询进程信息。

    实测结论（不要被直觉误导）：
      本机公平对比「池化 vs 每次 OpenProcess」各 120 次完整查询，
      池化 3.51ms、非池化 3.24ms —— 池化反而慢约 9%（每次多约 2.3μs，
      因为要额外校验创建时间来防 PID 复用）。所以这个池子的意义**不是提速**。

    它真正的价值是正确性与可维护性：
      · 防 PID 复用：PID 会被回收再利用。若不校验，缓存中属于旧进程的句柄
        可能被当成新进程读取（显示错误路径/内存，甚至结束错对象）。
      · 句柄集中管理：所有句柄有明确归属与统一释放出口，不会泄漏。
      · 退出清理可增量进行，不必每轮重新枚举整个进程表。

    线程安全：主线程与捕获线程都会用到，内部加锁。
    """

    MAX_ENTRIES = 512

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._entries: dict[int, tuple[int, int]] = {}   # pid -> (handle, create_time)
        self.reuse_detected = 0

    @staticmethod
    def _create_time(h) -> int:
        creation, exit_, kernel, user = (w.FILETIME(), w.FILETIME(), w.FILETIME(), w.FILETIME())
        if kernel32.GetProcessTimes(h, ctypes.byref(creation), ctypes.byref(exit_),
                                    ctypes.byref(kernel), ctypes.byref(user)):
            return (creation.dwHighDateTime << 32) | creation.dwLowDateTime
        return 0

    @staticmethod
    def _alive(h) -> bool:
        """
        句柄对应的进程是否仍在运行。

        刻意不用 WaitForSingleObject：进程句柄要等它必须先具备 SYNCHRONIZE 权限，
        而本程序按最小权限只申请了 PROCESS_QUERY_LIMITED_INFORMATION（不含 SYNCHRONIZE），
        实测 WaitForSingleObject 会直接返回 WAIT_FAILED(0xFFFFFFFF)，
        会把活着的进程全部误判成已退出（这是开发中真实踩到的 bug）。
        GetExitCodeProcess 只需查询权限，用它判断 STILL_ACTIVE 最稳妥。
        """
        code = w.DWORD(0)
        if kernel32.GetExitCodeProcess(h, ctypes.byref(code)):
            return code.value == STILL_ACTIVE
        return False

    def _evict_if_needed(self) -> None:
        while len(self._entries) > self.MAX_ENTRIES:
            pid, (h, _) = next(iter(self._entries.items()))
            self._entries.pop(pid, None)
            kernel32.CloseHandle(h)

    def _drop_locked(self, pid: int) -> None:
        entry = self._entries.pop(pid, None)
        if entry:
            kernel32.CloseHandle(entry[0])

    def acquire(self, pid: int) -> tuple[int, int]:
        """
        取得 (句柄, 创建时间)。返回的句柄由池子持有，调用方**不要**关闭它。
        创建时间为 0 表示这是不入池的临时句柄，需用 release_temp 关闭。
        """
        if pid <= 0:
            return 0, 0
        with self._lock:
            cached = self._entries.get(pid)
            if cached is not None:
                h, ct = cached
                if ct and self._alive(h) and self._create_time(h) == ct:
                    self._entries.pop(pid, None)
                    self._entries[pid] = (h, ct)      # 移到末尾 = 最近使用
                    return h, ct
                self.reuse_detected += 1              # 已退出或 PID 被复用
                self._drop_locked(pid)

            h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if not h:
                return 0, 0
            ct = self._create_time(h)
            if not ct:
                return h, 0                           # 无法校验 → 不入池
            self._entries[pid] = (h, ct)
            self._evict_if_needed()
            return h, ct

    @staticmethod
    def release_temp(h, create_time: int) -> None:
        """关闭未入池的临时句柄。"""
        if h and not create_time:
            kernel32.CloseHandle(h)

    def drop(self, pid: int) -> None:
        with self._lock:
            self._drop_locked(pid)

    def purge_dead(self) -> int:
        with self._lock:
            dead = [pid for pid, (h, _) in self._entries.items() if not self._alive(h)]
            for pid in dead:
                self._drop_locked(pid)
        return len(dead)

    def close_all(self) -> None:
        with self._lock:
            for h, _ in self._entries.values():
                kernel32.CloseHandle(h)
            self._entries.clear()

    def stats(self) -> dict:
        with self._lock:
            return {"cached": len(self._entries), "max": self.MAX_ENTRIES,
                    "reuse_detected": self.reuse_detected}


_PROC_POOL = ProcessHandlePool()


def query_process_detail(pid: int, pool: ProcessHandlePool | None = None,
                         with_private: bool = False) -> tuple:
    """
    返回 `(可执行文件完整路径, 工作集内存 KB)`；`with_private=True` 时返回三项，
    第三项是私有提交 KB（任务管理器「提交大小」列）。

    为什么要这个开关：界面每 2 秒就要给**全部进程**补一次内存，
    但"私有提交"只有真的会显示时才值得多读一次（`GetProcessMemoryInfo` 一次就够，
    这里是同一个句柄上的第二次调用，几乎没有额外开销 —— 留这个开关是为了
    让不需要它的调用方保持原来的行为与测试断言）。
    """
    if pid <= 0:
        return ("", 0, 0) if with_private else ("", 0)
    p = pool or _PROC_POOL
    h, ct = p.acquire(pid)
    if not h:
        return ("", 0, 0) if with_private else ("", 0)
    try:
        path = ""
        buf = ctypes.create_unicode_buffer(MAX_PATH_LONG)
        size = w.DWORD(MAX_PATH_LONG)
        if kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            path = buf.value
        mem = 0
        private = 0
        if _psapi is not None:
            if with_private:
                pmc = PROCESS_MEMORY_COUNTERS_EX()
            else:
                pmc = PROCESS_MEMORY_COUNTERS()
            pmc.cb = ctypes.sizeof(pmc)
            if _psapi.GetProcessMemoryInfo(h, ctypes.byref(pmc), pmc.cb):
                mem = int(pmc.WorkingSetSize) // 1024
                if with_private:
                    private = int(pmc.PrivateUsage) // 1024
        return (path, mem, private) if with_private else (path, mem)
    except Exception:
        return ("", 0, 0) if with_private else ("", 0)
    finally:
        ProcessHandlePool.release_temp(h, ct)


def query_process_start_time(pid: int) -> float:
    """
    进程的**启动时刻**（epoch 秒；拿不到返回 0）。

    用途：操作历史里写清"这个程序是什么时候打开的"（约定：
    "明确哪个程序打开、哪个程序关闭了"）。用 `GetProcessTimes` 的创建时间 +
    当前时间反推 —— 那个时间戳是 1601 年起的 FILETIME。
    """
    if pid <= 0:
        return 0.0
    h, ct = _PROC_POOL.acquire(pid)
    if not h:
        return 0.0
    try:
        creation, exit_, kernel, user = (w.FILETIME(), w.FILETIME(),
                                         w.FILETIME(), w.FILETIME())
        if not kernel32.GetProcessTimes(h, ctypes.byref(creation), ctypes.byref(exit_),
                                        ctypes.byref(kernel), ctypes.byref(user)):
            return 0.0
        ticks = (creation.dwHighDateTime << 32) | creation.dwLowDateTime
        if not ticks:
            return 0.0
        # FILETIME 的单位是 100 纳秒，起点 1601-01-01
        return _filetime_to_epoch(ticks)
    except Exception:
        return 0.0
    finally:
        ProcessHandlePool.release_temp(h, ct)


def _filetime_to_epoch(ticks: int) -> float:
    """FILETIME（100ns，1601 起）→ epoch 秒。"""
    return ticks / 10_000_000.0 - 11644473600.0


def format_open_time(pid: int, with_date: bool = True) -> str:
    """
    把进程启动时刻说成人话：`10:24:31`（今天）或 `10-11 22:03`（不是今天）。

    日志里写"打开时间"要短、还要能一眼看出是不是今天开的。
    """
    ts = query_process_start_time(pid)
    if ts <= 0:
        return ""
    try:
        started = time.localtime(ts)
    except (ValueError, OSError):
        return ""
    today = time.localtime()
    same_day = (started.tm_year, started.tm_yday) == (today.tm_year, today.tm_yday)
    if same_day or not with_date:
        return time.strftime("%H:%M:%S", started)
    return time.strftime("%m-%d %H:%M", started)


def query_process_cpu_time(pid: int) -> float | None:
    """返回进程累计 CPU 时间（秒）；无权访问时 None（复用句柄池）。"""
    h, ct = _PROC_POOL.acquire(pid)
    if not h:
        return None
    try:
        creation, exit_, kernel, user = (w.FILETIME(), w.FILETIME(), w.FILETIME(), w.FILETIME())
        if kernel32.GetProcessTimes(h, ctypes.byref(creation), ctypes.byref(exit_),
                                    ctypes.byref(kernel), ctypes.byref(user)):
            k = (kernel.dwHighDateTime << 32) | kernel.dwLowDateTime
            u = (user.dwHighDateTime << 32) | user.dwLowDateTime
            return (k + u) / 1e7
        return None
    finally:
        ProcessHandlePool.release_temp(h, ct)


def query_process_ppid(pid: int) -> int:
    """查某个进程的父 PID（读进程快照，必定拿得到）。"""
    return snapshot_processes().get(pid, {}).get("ppid", 0)


def is_process_elevated(pid: int) -> bool:
    """粗略判断目标进程是否以管理员权限运行（决定我们能否结束它）。"""
    h = kernel32.OpenProcess(PROCESS_TERMINATE, False, pid)
    if not h:
        return True
    kernel32.CloseHandle(h)
    return False


def is_process_alive(pid: int) -> bool:
    """判断进程是否仍存在（比 OpenProcess 更可靠，能识别已退出但句柄仍开的情况）。"""
    if pid <= 0:
        return False
    h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return False
    try:
        code = w.DWORD(0)
        if kernel32.GetExitCodeProcess(h, ctypes.byref(code)):
            return code.value == STILL_ACTIVE
        return True
    finally:
        kernel32.CloseHandle(h)


def collect_desktop(include_cloaked: bool = True,
                    with_detail: bool = True) -> list[WinInfo]:
    """
    一次性采集桌面上所有窗口的完整信息。

    对每个窗口补齐：真实软件名、程序路径、内存占用。
    进程名以系统进程快照为准（不受权限限制），路径/内存尽力而为，
    这样即使目标进程权限更高，列表里也不会出现空白程序名。
    """
    procs = snapshot_processes()
    wins = enum_windows(include_cloaked=include_cloaked)
    for wi in wins:
        info = procs.get(wi.pid)
        snap_name = info["name"] if info else ""
        path, mem, mem_priv = ("", 0, 0)
        if with_detail:
            path, mem, mem_priv = query_process_detail(wi.pid, with_private=True)
        wi.path = path or ""
        wi.mem_kb = mem
        wi.mem_private_kb = mem_priv
        wi.proc = snap_name or (os.path.basename(path) if path else "")
        if info is not None:
            info["mem_kb"] = mem
            info["mem_private_kb"] = mem_priv
            info["path"] = wi.path
    return wins


def get_system_summary(include_cloaked: bool = True) -> dict:
    """给命令行/界面用的一句话概览。"""
    wins = collect_desktop(include_cloaked=include_cloaked)
    visible = [wi for wi in wins if not wi.is_hidden_surface]
    hidden = [wi for wi in wins if wi.is_hidden_surface]
    pw, ph = get_physical_screen_size()
    return {
        "screen": {"physical": [pw, ph],
                   "logical": [user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)]},
        "visible_windows": len(visible),
        "hidden_windows": len(hidden),
        "apps": sorted({wi.real_name for wi in visible}),
        "processes": len(snapshot_processes()),
    }
def get_physical_screen_size() -> tuple[int, int]:
    """
    显示器物理像素分辨率。

    本程序声明了 DPI 感知，GetSystemMetrics 返回的是"逻辑分辨率"
    （例如 150% 缩放时 2560×1600 会报成 1707×1067）。
    这里用 EnumDisplaySettings 取真实物理分辨率，避免数字对不上。
    """
    class DEVMODEW(ctypes.Structure):
        _fields_ = [("dmDeviceName", w.WCHAR * 32), ("dmSpecVersion", w.WORD),
                    ("dmDriverVersion", w.WORD), ("dmSize", w.WORD), ("dmDriverExtra", w.WORD),
                    ("dmFields", w.DWORD), ("dmOrientation", ctypes.c_short),
                    ("dmPaperSize", ctypes.c_short), ("dmPaperLength", ctypes.c_short),
                    ("dmPaperWidth", ctypes.c_short), ("dmScale", ctypes.c_short),
                    ("dmCopies", ctypes.c_short), ("dmDefaultSource", ctypes.c_short),
                    ("dmPrintQuality", ctypes.c_short), ("dmColor", ctypes.c_short),
                    ("dmDuplex", ctypes.c_short), ("dmYResolution", ctypes.c_short),
                    ("dmTTOption", ctypes.c_short), ("dmCollate", ctypes.c_short),
                    ("dmFormName", w.WCHAR * 32), ("dmLogPixels", w.WORD),
                    ("dmBitsPerPel", w.DWORD), ("dmPelsWidth", w.DWORD),
                    ("dmPelsHeight", w.DWORD), ("dmDisplayFlags", w.DWORD),
                    ("dmDisplayFrequency", w.DWORD)]

    try:
        dm = DEVMODEW()
        dm.dmSize = ctypes.sizeof(DEVMODEW)
        if user32.EnumDisplaySettingsW(None, 0xFFFFFFFF, ctypes.byref(dm)):
            return int(dm.dmPelsWidth), int(dm.dmPelsHeight)
    except Exception:
        pass
    return user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)


# ============================================================================
# 四、画面捕获
# ============================================================================
#
# 捕获流程：
#   1) PrintWindow(PW_RENDERFULLCONTENT) 让窗口把完整内容画进内存位图
#      —— 该标志让窗口自行渲染，所以被遮挡的窗口也能取到画面
#   2) StretchBlt 直接画进 24 位 DIB（同时完成缩放，全程 C 层）
#   3) ctypes.string_at 一次性取出裸像素，交给调用方
# 每个被捕获的窗口缓存自己的 DC/位图，避免每帧重复创建。


class WindowCapturer:
    """针对单个窗口的 GDI 捕获器（含资源缓存）。"""

    def __init__(self) -> None:
        self.hdc = self.mdc = self.bmp = self.sdc = self.sbmp = None
        self.src_w = self.src_h = 0
        self.out_w = self.out_h = 0
        self.bits = ctypes.c_void_p()
        self.pitch = 0
        self.full_hdc = None

    def release(self) -> None:
        try:
            if self.sbmp:
                gdi32.DeleteObject(w.HGDIOBJ(self.sbmp))
            if self.sdc:
                gdi32.DeleteDC(w.HDC(self.sdc))
            if self.bmp:
                gdi32.DeleteObject(w.HGDIOBJ(self.bmp))
            if self.mdc:
                gdi32.DeleteDC(w.HDC(self.mdc))
            if self.hdc:
                user32.ReleaseDC(None, w.HDC(self.hdc))
            if self.full_hdc:
                user32.ReleaseDC(None, w.HDC(self.full_hdc))
        except Exception:
            pass
        self.hdc = self.mdc = self.bmp = self.sdc = self.sbmp = self.full_hdc = None
        self.src_w = self.src_h = self.out_w = self.out_h = 0
        self.bits = ctypes.c_void_p()

    def _ensure_source(self, hwnd: int, sw: int, sh: int) -> None:
        if self.bmp and self.src_w == sw and self.src_h == sh and self.hdc:
            return
        if self.bmp:
            gdi32.DeleteObject(w.HGDIOBJ(self.bmp))
            gdi32.DeleteDC(w.HDC(self.mdc))
            user32.ReleaseDC(None, w.HDC(self.hdc))
            self.bmp = self.mdc = self.hdc = None
        self.hdc = H(user32.GetWindowDC(w.HWND(hwnd)))
        if not self.hdc:
            raise OSError("GetWindowDC 失败")
        self.mdc = H(gdi32.CreateCompatibleDC(w.HDC(self.hdc)))
        self.bmp = H(gdi32.CreateCompatibleBitmap(w.HDC(self.hdc), sw, sh))
        gdi32.SelectObject(w.HDC(self.mdc), w.HGDIOBJ(self.bmp))
        self.src_w, self.src_h = sw, sh

    def _ensure_target(self, ow: int, oh: int) -> None:
        if self.sbmp and self.out_w == ow and self.out_h == oh:
            return
        if self.sbmp:
            gdi32.DeleteObject(w.HGDIOBJ(self.sbmp))
            gdi32.DeleteDC(w.HDC(self.sdc))
            self.sbmp = self.sdc = None
        self.sdc = H(gdi32.CreateCompatibleDC(w.HDC(self.mdc)))
        bi = BITMAPINFO()
        hdr = bi.bmiHeader
        hdr.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        hdr.biWidth = ow
        hdr.biHeight = -oh              # 负值 = 自上而下存储
        hdr.biPlanes = 1
        hdr.biBitCount = 24             # GDI 自动把 BGRA 转成 24 位 BGR
        hdr.biCompression = BI_RGB
        hdr.biSizeImage = ow * oh * 3
        bits = ctypes.c_void_p()
        self.sbmp = H(gdi32.CreateDIBSection(w.HDC(self.sdc), ctypes.byref(bi),
                                            DIB_RGB_COLORS, ctypes.byref(bits), None, 0))
        if not self.sbmp:
            raise OSError("CreateDIBSection 失败")
        gdi32.SelectObject(w.HDC(self.sdc), w.HGDIOBJ(self.sbmp))
        self.bits = bits
        self.pitch = (ow * 3 + 3) & ~3   # DIB 行按 4 字节对齐
        self.out_w, self.out_h = ow, oh

    def _read_bits(self, ow: int, oh: int) -> bytes:
        raw = ctypes.string_at(self.bits, self.pitch * oh)
        if self.pitch != ow * 3:
            raw = b"".join(raw[y * self.pitch:y * self.pitch + ow * 3] for y in range(oh))
        return raw

    def grab_window(self, hwnd: int, sw: int, sh: int, ow: int, oh: int,
                    quality: bool = False) -> tuple[bytes, int, int]:
        """捕获窗口画面。返回 (BGR 裸像素, 宽, 高)。"""
        ow, oh = max(16, min(ow, sw)), max(16, min(oh, sh))
        self._ensure_source(hwnd, sw, sh)
        target = w.HWND(hwnd)
        if not user32.PrintWindow(target, w.HDC(self.mdc), PW_RENDERFULLCONTENT):
            # 少数老程序 / 特殊表面不认 PW_RENDERFULLCONTENT；退回普通
            # PrintWindow 往往还能画出客户区，比安静地交一张黑图好。
            user32.PrintWindow(target, w.HDC(self.mdc), 0)
        self._ensure_target(ow, oh)
        gdi32.SetStretchBltMode(w.HDC(self.sdc), HALFTONE if quality else COLORONCOLOR)
        gdi32.SetBrushOrgEx(w.HDC(self.sdc), 0, 0, None)
        gdi32.StretchBlt(w.HDC(self.sdc), 0, 0, ow, oh,
                         w.HDC(self.mdc), 0, 0, sw, sh, SRCCOPY)
        return self._read_bits(ow, oh), ow, oh

    def grab_screen(self, sw: int, sh: int, ow: int, oh: int,
                    quality: bool = False) -> tuple[bytes, int, int]:
        """捕获整个桌面（用于「整屏监控」）。"""
        ow, oh = max(16, min(ow, sw)), max(16, min(oh, sh))
        if not self.full_hdc:
            self.full_hdc = H(user32.GetDC(None))
            if not self.full_hdc:
                raise OSError("GetDC(桌面) 失败")
        if not self.sdc or not self.sbmp:
            self.mdc = H(gdi32.CreateCompatibleDC(w.HDC(self.full_hdc)))
            self._ensure_target(ow, oh)
        elif self.out_w != ow or self.out_h != oh:
            self._ensure_target(ow, oh)
        gdi32.SetStretchBltMode(w.HDC(self.sdc), HALFTONE if quality else COLORONCOLOR)
        gdi32.StretchBlt(w.HDC(self.sdc), 0, 0, ow, oh,
                         w.HDC(self.full_hdc), 0, 0, sw, sh, SRCCOPY)
        return self._read_bits(ow, oh), ow, oh


class CaptureWorker(threading.Thread):
    """
    后台捕获线程。

    主线程通过 request() 提交「谁、多大、什么频率」的取帧需求，
    线程按各自频率节流取帧，结果通过 result_queue 交回主线程上屏。
    """

    def __init__(self) -> None:
        super().__init__(daemon=True, name="WinMon-Capture")
        self._want: dict = {}
        self._last: dict = {}
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self.result_queue: queue.Queue = queue.Queue(maxsize=8)
        self._stop = False
        self._capturers: dict = {}
        self.frames_done = 0

    def request(self, key, hwnd, out_w, out_h, src_w, src_h, min_interval,
                quality=False) -> None:
        with self._lock:
            self._want[key] = (hwnd, out_w, out_h, src_w, src_h, min_interval, quality)
        self._wake.set()

    def forget(self, key) -> None:
        with self._lock:
            self._want.pop(key, None)
            self._last.pop(key, None)
        self._wake.set()

    def clear_requests(self) -> None:
        with self._lock:
            self._want.clear()
            self._last.clear()
        self._wake.set()

    def shutdown(self) -> None:
        self._stop = True
        self._wake.set()

    def run(self) -> None:
        idle_rounds = 0
        while not self._stop:
            now = time.monotonic()
            with self._lock:
                want = list(self._want.items())
                stale = [k for k in self._capturers if k not in self._want]
            for key in stale:
                cap = self._capturers.pop(key, None)
                if cap:
                    cap.release()

            did_work = False
            for key, (hwnd, ow, oh, sw, sh, interval, quality) in want:
                if now - self._last.get(key, 0.0) < interval:
                    continue
                self._last[key] = now
                cap = self._capturers.get(key)
                if cap is None:
                    cap = WindowCapturer()
                    self._capturers[key] = cap
                try:
                    if hwnd is None or hwnd == 0 or key == "__SCREEN__":
                        sw2 = user32.GetSystemMetrics(0)
                        sh2 = user32.GetSystemMetrics(1)
                        frame = cap.grab_screen(sw2, sh2, ow, oh, quality)
                    else:
                        frame = cap.grab_window(hwnd, sw, sh, ow, oh, quality)
                    self._publish(key, frame, None)
                    self.frames_done += 1
                    did_work = True
                except Exception as exc:            # 单帧失败不影响整体
                    self._publish(key, None, f"{type(exc).__name__}: {exc}")
                    cap.release()
                    self._capturers.pop(key, None)

            if not did_work:
                idle_rounds += 1
                self._wake.wait(timeout=0.005 if idle_rounds < 20 else 0.05)
            else:
                idle_rounds = 0
            self._wake.clear()

        for cap in self._capturers.values():
            cap.release()
        self._capturers.clear()

    def _publish(self, key, frame, error) -> None:
        item = (key, frame, error)
        try:
            self.result_queue.put_nowait(item)
        except queue.Full:
            try:
                self.result_queue.get_nowait()      # 丢弃旧帧，保留最新
            except queue.Empty:
                pass
            try:
                self.result_queue.put_nowait(item)
            except queue.Full:
                pass


_HEX_TABLE = {i: "%02x" % i for i in range(256)}


def bgr_to_tk_hex_data(bgr: bytes, ow: int, oh: int, scale: int = 1) -> str:
    """
    把 BGR 裸像素转成 tkinter PhotoImage.put 需要的十六进制颜色串。

    scale > 1 时按「每 scale 个像素取 1 个」抽样（严格按整像素取样），
    得到的图像再用 zoom(scale) 放大回目标尺寸 —— 实测这比直接生成全尺寸像素快 5 倍以上。
    输出格式：每行形如 "{#rrggbb #rrggbb ...}"，行之间以空格分隔。
    """
    stride = ow * 3
    scale = max(1, int(scale))
    sampled = bytearray()
    for y in range(0, oh, scale):
        base = y * stride
        row = bgr[base:base + stride]
        sampled += b"".join(row[i:i + 3] for i in range(0, stride, 3 * scale))
    sw = (ow + scale - 1) // scale
    sh = (oh + scale - 1) // scale
    hx = sampled.decode("latin-1").translate(_HEX_TABLE)
    row_chars = sw * 6
    rows = []
    for y in range(sh):
        s = y * row_chars
        rows.append("{" + " ".join("#" + hx[k:k + 6] for k in range(s, s + row_chars, 6)) + "}")
    return " ".join(rows)


def bgr_to_png(bgr: bytes, ow: int, oh: int, level: int = 1) -> bytes:
    """
    把 BGR 裸像素编码成 PNG（只用 zlib，不依赖 Pillow）。

    为什么预览改用 PNG：`PhotoImage.put()` 解析 640×400 的十六进制颜色串
    实测约 **80~190ms/帧**，抓取线程 10 FPS 时队列永远积压，用户看到的是
    几秒前的旧画面；Tk 自带的 PNG 解码器只要 **4~8ms**，而且不用抽样，
    可以按画布原始分辨率上屏。PNG 编码本身约 3ms（level=1）。
    """
    ow, oh = max(1, int(ow)), max(1, int(oh))
    stride = ow * 3
    if len(bgr) < stride * oh:
        raise ValueError(f"像素数据不足：需要 {stride * oh} 字节，实际 {len(bgr)}")
    # 每行前加一个 filter 字节 0（None），再按 RGB 顺序排列。
    # 用切片赋值让 C 层完成通道交换，比逐像素 Python 循环快一个数量级。
    raw = bytearray((stride + 1) * oh)
    for y in range(oh):
        src = y * stride
        dst = y * (stride + 1)
        raw[dst] = 0
        row = bgr[src:src + stride]
        rgb = bytearray(stride)
        rgb[0::3] = row[2::3]
        rgb[1::3] = row[1::3]
        rgb[2::3] = row[0::3]
        raw[dst + 1:dst + 1 + stride] = rgb

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", ow, oh, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(bytes(raw), max(0, min(9, int(level)))))
            + chunk(b"IEND", b""))


# ============================================================================
# 五、进程结束
# ============================================================================

def terminate_process(pid: int) -> tuple[bool, str, int]:
    """强制结束进程。返回 (是否成功, 提示信息, 错误码)。"""
    if pid <= 0:
        return False, "无效的 PID", 87
    h = kernel32.OpenProcess(PROCESS_TERMINATE, False, pid)
    if not h:
        err = ctypes.get_last_error()
        if err == 5:
            return False, "拒绝访问：目标进程权限更高，请以管理员身份运行本程序后重试。", err
        return False, f"无法打开进程（错误码 {err}）", err
    try:
        if kernel32.TerminateProcess(h, 1):
            _PROC_POOL.drop(pid)
            return True, "已强制结束", 0
        err = ctypes.get_last_error()
        return False, f"结束失败（错误码 {err}）", err
    finally:
        kernel32.CloseHandle(h)


def graceful_close_window(hwnd: int) -> tuple[bool, str]:
    """向窗口发送关闭消息，让它正常退出（等同点击窗口关闭按钮）。"""
    if not user32.PostMessageW(w.HWND(hwnd), WM_CLOSE, 0, 0):
        err = ctypes.get_last_error()
        return False, f"发送关闭消息失败（错误码 {err}）"
    return True, "已发送关闭请求"


def activate_window(hwnd: int, retries: int = 3, settle: float = 0.15
                    ) -> tuple[bool, str]:
    """
    把窗口**唤醒并切到前台**（最小化的先还原），返回 (是否成功, 说明)。

    为什么不能只用 `SetForegroundWindow`：Windows 有"前台锁定"规则，
    直接调用经常**静默失败**（返回非 0 也可能没生效），同一个窗口时灵时不灵。
    这里用实测有效的组合拳：

      1. `ShowWindow(SW_RESTORE)` 取消最小化
      2. `AttachThreadInput` 把本线程挂到目标窗口的输入队列上 —— 这是绕过前台锁最可靠的一步
      3. `BringWindowToTop` + `SetForegroundWindow` + `SetActiveWindow` + `SetFocus`
      4. **回读 `GetForegroundWindow()` 核对**，没成功就重试

    第 4 步很关键：不核对的话会把"调用没报错"当成"切换成功了"，
    而用户看到的其实是窗口根本没动。
    """
    if not hwnd or not user32.IsWindow(w.HWND(hwnd)):
        return False, "窗口已经不在了"
    target = w.HWND(hwnd)
    was_minimized = bool(user32.IsIconic(target))
    attempts = max(1, int(retries))
    last_why = "Windows 前台锁定拒绝了这次切换，再点一次通常就成功"
    for _ in range(attempts):
        try:
            if user32.IsIconic(target):
                user32.ShowWindow(target, SW_RESTORE)
            cur_thread = int(kernel32.GetCurrentThreadId())
            # 关键：**要挂到"当前前台窗口"所在线程的输入队列上**。
            # 只挂目标窗口的线程是不够的 —— 实测在"本进程不是前台进程"时
            # （例如别的程序正拿着前台），SetForegroundWindow 会被 Windows 拒绝。
            # 挂到前台线程之后，本线程就属于前台的输入上下文，才被允许改前台窗口。
            # 目标线程也一起挂：它有输入队列时切换更顺。
            fg = H(user32.GetForegroundWindow())
            fg_thread = int(user32.GetWindowThreadProcessId(w.HWND(fg), None)) if fg else 0
            target_thread = int(user32.GetWindowThreadProcessId(target, None))
            attached: list[int] = []
            for tid in {fg_thread, target_thread}:
                if tid and tid != cur_thread:
                    if user32.AttachThreadInput(cur_thread, tid, True):
                        attached.append(tid)
            try:
                user32.BringWindowToTop(target)
                user32.SetForegroundWindow(target)
                user32.SetActiveWindow(target)
                user32.SetFocus(target)
            finally:
                for tid in attached:
                    user32.AttachThreadInput(cur_thread, tid, False)
            if settle > 0:
                time.sleep(settle)
            if H(user32.GetForegroundWindow()) == hwnd:
                title = get_window_title(hwnd)[:40]
                extra = "（最小化的窗口已一并还原）" if was_minimized else ""
                return True, f"已切换到前台：{title}{extra}"
        except Exception as exc:
            last_why = f"切换时出错：{type(exc).__name__}: {exc}"
            if settle > 0:
                time.sleep(settle)
    return False, last_why


def window_is_minimized(hwnd: int) -> bool:
    """
    窗口**当前**是不是最小化的 —— 实时问系统。

    为什么不能图省事用列表里缓存的 `WinInfo.iconic`：那是最多 2 秒前的快照。
    而双击是"最小化 / 还原"的切换，我们刚把它最小化过，下一拍拿旧快照就会
    再最小化一次（切换失灵）。这个调用极便宜，直接用实时的。
    """
    try:
        return bool(hwnd) and bool(user32.IsIconic(w.HWND(hwnd)))
    except Exception:
        return False


def minimize_window(hwnd: int, settle: float = 0.12) -> tuple[bool, str]:
    """
    最小化窗口，返回 (是否成功, 说明)。

    同样要**回读 `IsIconic` 核对**：`ShowWindow` 对某些窗口（挡住了最小化的、
    或正在处理模态框的）会返回成功却什么都不发生。
    """
    if not hwnd or not user32.IsWindow(w.HWND(hwnd)):
        return False, "窗口已经不在了"
    target = w.HWND(hwnd)
    if user32.IsIconic(target):
        return True, "它本来就是最小化的"
    user32.ShowWindow(target, SW_MINIMIZE)
    if settle > 0:
        time.sleep(settle)
    if user32.IsIconic(target):
        return True, f"已最小化：{get_window_title(hwnd)[:40]}"
    return False, "这个窗口不接受最小化（它可能开着模态对话框）"


def build_process_tree(processes: dict[int, dict]) -> dict[int, list[int]]:
    """构建 父PID -> [子PID] 的映射。"""
    children: dict[int, list[int]] = {}
    for pid, info in processes.items():
        children.setdefault(info["ppid"], []).append(pid)
    return children


def collect_descendants(pid: int, children: dict[int, list[int]],
                        limit: int = 400) -> list[int]:
    """广度优先收集全部子孙进程 PID。"""
    out: list[int] = []
    stack = list(children.get(pid, []))
    while stack and len(out) < limit:
        cur = stack.pop()
        out.append(cur)
        stack.extend(children.get(cur, []))
    return out


def _process_depth(pid: int, processes: dict[int, dict]) -> int:
    """进程在树中的深度（用于决定结束顺序）。"""
    depth = 0
    cur = pid
    while depth < 64:
        info = processes.get(cur)
        if not info:
            break
        parent = info.get("ppid")
        if not parent or parent == cur or parent not in processes:
            break
        cur = parent
        depth += 1
    return depth


def terminate_process_tree_multi(victims: list[int],
                                 processes: dict[int, dict]) -> tuple[int, list[str]]:
    """
    强制结束一批已算好的 PID。返回 (成功数, 失败信息列表)。

    **每一个没结束的都要进失败清单**，包括"拒绝访问"（2026-10-13 修的真 bug）：
    以前那句 `elif "拒绝访问" not in msg` 把它悄悄滤掉了，于是界面只显示
    "成功 1/4"、操作历史也只记这个数 —— 用户完全不知道为什么少了三个
    （而"权限不够"恰恰是有行动价值的提示：用管理员身份再试一次）。
    """
    ok_count = 0
    failures: list[str] = []
    # 先杀更深的子孙，再杀祖先，避免父进程退出后子进程被孤儿化
    for target in sorted(set(victims), key=lambda p: -_process_depth(p, processes)):
        name = processes.get(target, {}).get("name", f"PID {target}")
        ok, msg, _ = terminate_process(target)
        if ok:
            ok_count += 1
        else:
            failures.append(f"{name}({target}): {msg}")
    return ok_count, failures


def terminate_process_tree(pid: int, processes: dict[int, dict]) -> tuple[int, list[str]]:
    """结束进程及其全部子孙。"""
    children = build_process_tree(processes)
    victims = collect_descendants(pid, children) + [pid]
    return terminate_process_tree_multi(victims, processes)


def reveal_in_explorer(path: str) -> None:
    """在资源管理器中定位文件。"""
    if not path or not os.path.exists(path):
        return
    try:
        import subprocess
        subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])
    except Exception:
        pass


def open_file(path: str) -> None:
    if not path or not os.path.exists(path):
        return
    try:
        os.startfile(path)  # type: ignore[attr-defined]
    except Exception:
        pass


# ============================================================================
# 六、程序图标（HICON -> PNG base64）
# ============================================================================
#
# tkinter 只接受 PhotoImage，而 PhotoImage 可以直接吃 base64 PNG 数据，
# 所以这里把 HICON 画进 32 位 DIB、取出 BGRA、再做一份 PNG。
# 自己写 PNG 编码（十几行）是为了不引入 Pillow ——
# 本程序最大的优势是"零依赖、拷过去就能跑"。


def _png_chunk(tag: bytes, data: bytes) -> bytes:
    import zlib as _zlib
    return (struct.pack(">I", len(data)) + tag + data
            + struct.pack(">I", _zlib.crc32(tag + data) & 0xFFFFFFFF))


def encode_png_rgba(rgba: bytes, width: int, height: int) -> bytes:
    """把 RGBA 裸像素编码成 PNG（只用 zlib）。"""
    import zlib as _zlib
    stride = width * 4
    raw = bytearray()
    for y in range(height):
        raw.append(0)                       # 每行的 filter 字节
        raw += rgba[y * stride:(y + 1) * stride]
    return (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
        + _png_chunk(b"IDAT", _zlib.compress(bytes(raw), 9))
        + _png_chunk(b"IEND", b"")
    )


def hicon_to_base64_png(hicon: int, size: int = 32) -> str:
    """
    把图标句柄渲染成 base64 PNG。

    做法：建一个 32 位自上而下的 DIB（CreateDIBSection 可直接拿到像素指针），
    用 DrawIconEx 把图标画上去（它能正确保留透明通道），再读出 BGRA。
    """
    if not hicon:
        return ""
    hdc_screen = mdc = bmp = None
    old = None
    try:
        hdc_screen = H(user32.GetDC(None))
        if not hdc_screen:
            return ""
        mdc = H(gdi32.CreateCompatibleDC(w.HDC(hdc_screen)))
        bi = BITMAPINFO()
        hdr = bi.bmiHeader
        hdr.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        hdr.biWidth = size
        hdr.biHeight = -size
        hdr.biPlanes = 1
        hdr.biBitCount = 32
        hdr.biCompression = BI_RGB
        hdr.biSizeImage = size * size * 4
        bits = ctypes.c_void_p()
        bmp = H(gdi32.CreateDIBSection(w.HDC(mdc), ctypes.byref(bi), DIB_RGB_COLORS,
                                       ctypes.byref(bits), None, 0))
        if not bmp or not bits:
            return ""
        old = gdi32.SelectObject(w.HDC(mdc), w.HGDIOBJ(bmp))
        gdi32.PatBlt(w.HDC(mdc), 0, 0, size, size, BLACKNESS)
        if not user32.DrawIconEx(w.HDC(mdc), 0, 0, w.HANDLE(hicon), size, size, 0, None, 0x0003):
            return ""
        raw = ctypes.string_at(bits, size * size * 4)
        mv = memoryview(raw)
        rgba = bytearray(size * size * 4)
        for i in range(0, len(raw), 4):      # BGRA -> RGBA
            rgba[i] = mv[i + 2]
            rgba[i + 1] = mv[i + 1]
            rgba[i + 2] = mv[i]
            rgba[i + 3] = mv[i + 3] if mv[i + 3] else 255
        return base64.b64encode(encode_png_rgba(bytes(rgba), size, size)).decode("ascii")
    except Exception:
        return ""
    finally:
        try:
            if mdc and old:
                gdi32.SelectObject(w.HDC(mdc), old)
        except Exception:
            pass
        if bmp:
            gdi32.DeleteObject(w.HGDIOBJ(bmp))
        if mdc:
            gdi32.DeleteDC(w.HDC(mdc))
        if hdc_screen:
            user32.ReleaseDC(None, w.HDC(hdc_screen))


def icon_from_file(path: str, size: int = 32) -> str:
    """用 SHGetFileInfoW 从 exe 文件取图标，返回 base64 PNG。"""
    if not path or shell32 is None:
        return ""
    info = SHFILEINFOW()
    flags = SHGFI_ICON | (SHGFI_LARGEICON if size > 16 else SHGFI_SMALLICON)
    rc = shell32.SHGetFileInfoW(path, 0, ctypes.byref(info), ctypes.sizeof(info), flags)
    if not rc or not info.hIcon:
        return ""
    h = H(info.hIcon)
    try:
        return hicon_to_base64_png(h, size)
    finally:
        user32.DestroyIcon(w.HANDLE(h))     # SHGetFileInfoW 给的图标由我们负责销毁


class IconCache:
    """
    程序图标缓存（纯数据层，不依赖 tkinter）。

    取图顺序（同一个软件只取一次，之后走缓存）：
      1. 窗口自己登记的图标（WM_GETICON 或窗口类图标）—— 最贴合实际显示
      2. 用 SHGetFileInfoW 从 exe 文件取 —— 最通用，几乎一定能拿到
    """

    def __init__(self, size: int = 32) -> None:
        self.size = size
        self._by_path: dict[str, str] = {}
        self._by_hwnd: dict[int, str] = {}

    def get(self, wi) -> str:
        """取某个窗口对应的图标（base64 PNG，失败返回空串）。"""
        hwnd = wi.hwnd
        cached = self._by_hwnd.get(hwnd)
        if cached is not None:
            return cached
        png = ""
        h = get_window_icon_handle(hwnd)
        if h:
            png = hicon_to_base64_png(h, self.size)
        if not png and wi.path:
            key = wi.path.lower()
            if key in self._by_path:
                png = self._by_path[key]
            else:
                png = icon_from_file(wi.path, self.size)
                self._by_path[key] = png
        self._by_hwnd[hwnd] = png
        return png

    def forget_hwnd(self, hwnd: int) -> None:
        self._by_hwnd.pop(hwnd, None)

    def clear(self) -> None:
        self._by_hwnd.clear()


def _fmt_mem(kb: int) -> str:
    if not kb:
        return "—"
    if kb < 1024:
        return f"{kb} KB"
    if kb < 1024 * 1024:
        return f"{kb / 1024:.1f} MB"
    return f"{kb / 1024 / 1024:.2f} GB"


def _fmt_mem_pair(wss_kb: int, private_kb: int) -> str:
    """
    列表「内存」列用的紧凑格式：`229.0M / 61.7M`（**工作集 / 私有提交**）。

    两个一起给，用户才能把 WinMon 与任务管理器的差异自己对上：
    斜杠前是工作集（含与别的进程共享的 DLL/共享页，必然偏大），
    斜杠后是私有提交（任务管理器「提交大小」列的口径）。

    为什么中间是光秃秃一个 `/` 而不是「｜私」这类带字的分隔符：
    实测 `Microsoft YaHei UI 9` 下 `229.0M｜私 61.7M` 要 107px，
    而这个列在 1280 宽的窗口里只有 98px —— 会显示成"229.0M｜私 61.…"。
    去掉重复出现的"私"字之后是 90px，正好放得下（含 GB 级）
    见 `test_b9`/a5 里对列宽的断言与 HANDOFF 坑 39。
    私有值拿不到时只显示工作集（不显示误导性的 0）。
    """
    def short(kb: int) -> str:
        if kb <= 0:
            return "—"
        if kb >= 1024 * 1024:
            return f"{kb / 1024 / 1024:.2f}G"
        return f"{kb / 1024:.1f}M"

    if private_kb <= 0:
        return short(wss_kb)
    return f"{short(wss_kb)}/{short(private_kb)}"


def mem_explain(wss_kb: int, private_kb: int) -> str:
    """把两个内存口径说成人话（悬停提示与预览页共用）。"""
    if not wss_kb and not private_kb:
        return "内存：读不到（进程可能已退出或权限更高）"
    return (f"内存：工作集 {_fmt_mem(wss_kb)}（含与其它进程共享的 DLL/共享内存页）"
            f"　私有提交 {_fmt_mem(private_kb)}（任务管理器「提交大小」列的口径）")


def enable_dpi_awareness() -> None:
    """必须在创建窗口前调用，否则界面在高 DPI 下会模糊。"""
    try:
        ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
        return
    except Exception:
        pass
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
        return
    except Exception:
        pass
    try:
        user32.SetProcessDPIAware()
    except Exception:
        pass


# ============================================================================
# 七、程序目录：枚举系统里"装了什么"，并能精准启动
# ============================================================================
#
# 两个数据源互补：
#   1) 开始菜单 .lnk 快捷方式 —— 记录了目标 exe、启动参数、工作目录，
#      正是"双击图标"的行为，是**精准启动**最可靠的入口（约 110 项）
#   2) 注册表 Uninstall 键 —— 覆盖没有开始菜单项的程序（约 118 项）
#
# 关于解析 .lnk：曾尝试用 COM 的 IShellLinkW::GetPath 直接解析（免依赖、更快），
# 但实测本机（Windows 11）GetPath 对任何快捷方式都返回 S_OK 却把缓冲区写成指针垃圾值，
# 而同一接口的 GetDescription / GetWorkingDirectory 都正常 —— 说明 GetPath 已不返回
# 可用字符串。因此改用 PowerShell 的 WScript.Shell 批量解析：
# 110 个快捷方式约 0.4 秒，且不依赖 ScriptBlock 之外的任何组件。


class ProgramItem:
    """程序目录里的一项（一个可启动的程序）。"""

    __slots__ = ("name", "target", "args", "workdir", "source", "publisher",
                 "install_location", "icon_ref", "pid")

    def __init__(self, name, target="", args="", workdir="", source="startmenu",
                 publisher="", install_location="", icon_ref="", pid=0):
        self.name = name
        self.target = target            # 要启动的 exe / 快捷方式
        self.args = args
        self.workdir = workdir
        self.source = source            # startmenu / registry
        self.publisher = publisher
        self.install_location = install_location
        self.icon_ref = icon_ref
        self.pid = pid                  # 若该程序此刻正在运行，记录其 PID

    @property
    def source_label(self) -> str:
        return {"startmenu": "开始菜单", "registry": "注册表", "running": "运行中"}.get(
            self.source, self.source)

    @property
    def exists(self) -> bool:
        return bool(self.target) and os.path.exists(self.target)

    @property
    def runnable(self) -> bool:
        """能否直接启动：有目标且目标存在（.lnk 也算）。"""
        return bool(self.target) and os.path.exists(self.target)

    @property
    def ext(self) -> str:
        return os.path.splitext(self.target)[1].lower() if self.target else ""

    def to_dict(self) -> dict:
        return {"name": self.name, "target": self.target, "args": self.args,
                "workdir": self.workdir, "source": self.source,
                "publisher": self.publisher, "install_location": self.install_location,
                "icon_ref": self.icon_ref, "pid": self.pid}


# ---------------------------------------------------------------------------
# 程序分类：把目录里的 100 多项分成可用的几类
# ---------------------------------------------------------------------------
#
# 为什么用"关键词 + 安装路径"而不是读注册表的类别字段：
# 注册表 Uninstall 键**没有**可靠的类别信息（Publisher 是发行商不是类别，
# 而 DisplayName 常常只有软件名）。实测最有效的信号是：
#   1) 软件名 / 快捷方式名里的关键词（"Steam"、"微信"、"Visual Studio"）
#   2) 安装路径里的目录名（X:\Steam\... 、C:\Program Files\NVIDIA...）
# 命中即归类，同类内按 (优先级, 关键词) 排在前面的赢；都不命中归入"其他"。
#
# 归类是**纯建议**：用户可以手动改，手动结果存在
# winmon_settings.json 的 program_categories 里，并且优先级高于自动分类。

#: 程序目录缓存格式版本。改动"怎么构建目录"的逻辑（去重、归类、字段）时**必须**加一，
#: 否则用户的旧缓存会继续生效，看起来像改动没起作用。
CATALOG_CACHE_VERSION = 5

#: 「程序目录」里允许出现的可启动目标类型。
#:
#: 为什么需要过滤：注册表 Uninstall 键的 DisplayIcon 有时指向**非程序文件**
#: （实测出现 "License" -> COPYING.txt、"7-Zip Help" -> 7-zip.chm、
#: "Python 3.14 Manuals" -> index.html）。它们出现在"程序目录"里既点不动
#: 也不是用户想启动的东西，只会让列表变噪。
#:
#: 注意 .msc 要保留：它是 Windows 管理控制台文档，双击能打开真正的管理工具
#: （Computer Management、Event Viewer 等），属于"可启动"。
CATALOG_TARGET_EXTS = (
    ".exe", ".com", ".bat", ".cmd", ".msc", ".lnk", ".url",
    ".ps1", ".vbs", ".jar", ".py", ".pyw",
)


PROGRAM_CATEGORIES = [
    ("game", "游戏娱乐"),
    ("media", "影音播放"),
    ("browser", "浏览器"),
    ("office", "办公文档"),
    ("dev", "开发工具"),
    ("download", "下载传输"),
    ("security", "安全防护"),
    ("system", "系统工具"),
    ("other", "其他"),
]
CATEGORY_LABELS = dict(PROGRAM_CATEGORIES)
CATEGORY_ORDER = [key for key, _label in PROGRAM_CATEGORIES]
CATEGORY_OTHER = "other"

#: (类别, 关键词)。关键词一律小写；匹配前会把候选文本里的空格与连字符去掉，
#: 所以 "visualstudiocode" 这类关键词能命中 "Visual Studio Code"。
CATEGORY_KEYWORDS = (
    ("game", (
        "steam", "epicgames", "epic games", "wegame", "battle.net", "ubisoft",
        "origin", "ea app", "genshin", "原神", "崩坏", "mihoyo", "miHoYo",
        "wuthering", "鸣潮", "league of legends", "英雄联盟", "valorant",
        "minecraft", "我的世界", "roblox", "gog galaxy", "playnite",
        "mumu", "ldplayer", "雷电模拟器", "bluestacks", "gameloop",
        "加速器", "游戏盒", "4399", "yysls", "starrail",
    )),
    ("media", (
        "vlc", "potplayer", "mpc-hc", "kmplayer", "foobar", "spotify",
        "cloudmusic", "网易云音乐", "qqmusic", "qq音乐", "kugou", "酷狗",
        "bilibili", "哔哩哔哩", "iqiyi", "爱奇艺", "tencentvideo", "腾讯视频",
        "youku", "优酷", "douyin", "抖音", "obs", "audacity", "剪映", "jianying",
        "格式工厂", "audition", "premiere", "photoshop", "illustrator",
        "potplayer", "music", "player", "影音", "播放器", "看图", "视频",
    )),
    ("browser", (
        "chrome", "google chrome", "edge", "msedge", "firefox", "brave",
        "opera", "vivaldi", "360se", "360浏览器", "qqbrowser", "qq浏览器",
        "sogouexplorer", "搜狗浏览器", "maxthon", "遨游", "browser", "浏览器",
    )),
    ("office", (
        "office", "word", "excel", "powerpoint", "outlook", "onenote",
        "wps", "kingsoft", "acrobat", "adobe reader", "foxit", "福昕",
        "notion", "obsidian", "typora", "onedrive", "百度网盘", "阿里云盘",
        "印象笔记", "evernote", "xmind", "mindmaster", "calibre",
    )),
    ("dev", (
        "visualstudio", "visual studio", "vscode", "code", "pycharm",
        "intellij", "goland", "clion", "android studio", "eclipse",
        "git", "github", "sourcetree", "tortoisegit", "docker", "postman",
        "navicat", "dbeaver", "python", "nodejs", "jdk", "java", "anaconda",
        "xampp", "wireshark", "fiddler", "ida", "ghidra", "unity", "unreal",
        "deepseek", "cursor", "sublime", "notepad++", "putty", "mobaxterm",
        "vmware", "virtualbox", "wsl", "terminal", "cmder", "dev-cpp",
    )),
    ("download", (
        "thunder", "迅雷", "idm", "internet download manager", "qbittorrent",
        "utorrent", "bitcomet", "aria2", "motrix", "free download manager",
        "filezilla", "winscp", "xftp", "百度网盘", "115", "夸克",
    )),
    ("security", (
        "360", "腾讯电脑管家", "qqpcmgr", "huorong", "火绒", "kaspersky",
        "卡巴斯基", "avast", "avg", "bitdefender", "nod32", "eset",
        "malwarebytes", "defender", "antivirus", "安全卫士", "杀毒",
    )),
    ("system", (
        "nvidia", "amd", "intel", "realtek", "driver", "驱动",
        "logitech", "罗技", "razer", "雷蛇", "corsair", "海盗船",
        "ccleaner", "geek uninstall", "dism++", "rufus", "ventoy",
        "7-zip", "7zip", "winrar", "bandizip", "peazip", "解压缩",
        "everything", "listary", "snipaste", "sharex", "powertoys",
        "任务管理器", "control panel", "控制面板", "设备管理器",
        # Windows 自带管理工具：名字是英文，但都在 system32 下
        "system32", "windows\\system32", "administrative tools",
        "command prompt", "cmd.exe", "charmap", "copilot",
        "disk cleanup", "event viewer", "component services",
        "computer management", "performance monitor", "resource monitor",
        "system configuration", "registry editor", "services.msc",
        "device manager", "defragment", "dfrgui", "cleanmgr",
        "windows kits", "windows performance toolkit",
        "discord", "zoom", "teams", "slack", "feishu", "飞书", "dingtalk",
        "钉钉", "wechat", "微信", "weixin", "qq", "telegram", "whatsapp",
        "输入法", "sogou", "搜狗", "tapTap", "uuyc", "远程",
    )),
)


def classify_program(name: str, target: str = "", install_location: str = "",
                     override: str = "") -> str:
    """
    给一个程序归类，返回类别 key（见 `PROGRAM_CATEGORIES`）。

    override 是用户手动指定的类别，优先级最高（不认识的值会被忽略）。

    匹配策略（按可靠性从高到低）：
      1) 用户手动指定
      2) 关键词命中**软件名**（最强信号："Steam"、"微信"）
      3) 关键词命中**安装路径或 exe 路径**（覆盖名字看不出类别的软件）
    关键词按类别顺序匹配，命中第一个即返回；都不命中归入"其他"。
    """
    if override and override in CATEGORY_LABELS:
        return override

    def norm(s: str) -> str:
        return (s or "").lower().replace(" ", "").replace("-", "").replace("_", "")

    name_l = norm(name)
    # exe 文件名也要参与：有些软件名是中文而 exe 名是英文（反之亦然）
    path_l = norm(target) + "|" + norm(os.path.basename(target or "")) + "|" + norm(install_location)

    for cat, words in CATEGORY_KEYWORDS:
        for w in words:
            key = norm(w)
            if key and key in name_l:
                return cat
    for cat, words in CATEGORY_KEYWORDS:
        for w in words:
            key = norm(w)
            if key and key in path_l:
                return cat
    return CATEGORY_OTHER


def category_of_item(item: "ProgramItem", override: str = "") -> str:
    """`ProgramItem` 版的归类（把三个字段一起交给 classify_program）。"""
    return classify_program(item.name, item.target, item.install_location, override)


def group_items_by_category(items: list, overrides: dict | None = None) -> list[tuple[str, list]]:
    """
    按类别分组，返回 [(类别 key, [条目...])]，顺序与 `PROGRAM_CATEGORIES` 一致。

    空类别不会出现在结果里（界面上就不用显示空分组）。
    """
    overrides = overrides or {}
    buckets: dict[str, list] = {}
    for it in items:
        key = category_of_item(it, overrides.get((it.target or "").lower(), ""))
        buckets.setdefault(key, []).append(it)
    out = []
    for key in CATEGORY_ORDER:
        group = buckets.pop(key, None)
        if group:
            out.append((key, group))
    # 理论上不会有（classify 只返回表里的 key），但不丢数据更稳妥
    for key, group in buckets.items():
        if group:
            out.append((key, group))
    return out


# ---------------------------------------------------------------------------
# 收藏 / 最近使用：纯数据层，状态由调用方持久化
# ---------------------------------------------------------------------------

class CatalogPrefs:
    """
    程序目录的用户偏好：收藏、最近使用、手动分类。

    刻意不自己读写文件：主程序把它的 `to_dict()` 结果一起写进
    winmon_settings.json 的 `program_prefs` 里 —— 这样用户的偏好和界面设置
    在同一个文件里，备份/清理都只有一处。核心层保持"无副作用、可当库用"。

    键的选择：收藏与最近使用都用 **exe 全路径小写**。
    不用软件名（会重名、会变），也不用 PID（会变）。
    """

    __slots__ = ("favorites", "recents", "categories", "max_recent")

    def __init__(self, max_recent: int = 30) -> None:
        self.favorites: list[str] = []                  # exe 全路径（原样大小写）
        self.recents: list[dict] = []                   # [{path, name, ts}]
        self.categories: dict[str, str] = {}            # exe 全路径(小写) -> 类别 key
        self.max_recent = max(1, int(max_recent))

    # -- 键 --
    @staticmethod
    def key(target: str, name: str = "") -> str:
        """偏好键：优先用 exe 全路径；没有路径时退回软件名（UWP 之类）。"""
        t = (target or "").strip()
        if t:
            return t.lower()
        return ("name:" + (name or "").strip().lower()) if name else ""

    # -- 收藏 --
    def is_favorite(self, target: str, name: str = "") -> bool:
        k = self.key(target, name)
        return bool(k) and any(self.key(f) == k for f in self.favorites)

    def toggle_favorite(self, target: str, name: str = "") -> bool:
        """切换收藏状态，返回切换后是否已收藏。"""
        k = self.key(target, name)
        if not k:
            return False
        for i, f in enumerate(self.favorites):
            if self.key(f) == k:
                del self.favorites[i]
                return False
        # 存 key() 的结果而不是原始路径：没有 exe 路径的条目（UWP）会退化成
        # "name:软件名"，这样下次查找时 key() 才能再算出同样的值。
        self.favorites.append(k)
        return True

    def add_favorite(self, target: str, name: str = "") -> bool:
        if not self.is_favorite(target, name):
            return self.toggle_favorite(target, name)
        return True

    def remove_favorite(self, target: str, name: str = "") -> None:
        k = self.key(target, name)
        self.favorites = [f for f in self.favorites if self.key(f) != k]

    def favorite_count(self) -> int:
        return len(self.favorites)

    # -- 最近使用 --
    def record_use(self, target: str, name: str = "", ts: float | None = None) -> None:
        """记录一次"打开"。同一程序只保留最近一次（按 key 去重并提到最前）。"""
        k = self.key(target, name)
        if not k:
            return
        ts = time.time() if ts is None else float(ts)
        self.recents = [r for r in self.recents if self.key(r.get("path", ""),
                                                          r.get("name", "")) != k]
        self.recents.insert(0, {"path": (target or "").strip(), "name": name or "",
                                "ts": ts})
        del self.recents[self.max_recent:]

    def prune_recent(self, max_age_days: float = 60.0,
                     now: float | None = None) -> int:
        """清掉太久没用的记录，返回清掉的条数。"""
        if not max_age_days or max_age_days <= 0:
            return 0
        now = time.time() if now is None else float(now)
        limit = now - max_age_days * 86400.0
        before = len(self.recents)
        self.recents = [r for r in self.recents
                        if float(r.get("ts") or 0) >= limit]
        return before - len(self.recents)

    def recent_list(self) -> list[dict]:
        """最近使用（新的在前）。"""
        return list(self.recents)

    def recent_count(self) -> int:
        return len(self.recents)

    def is_recent(self, target: str, name: str = "") -> bool:
        k = self.key(target, name)
        return bool(k) and any(self.key(r.get("path", ""), r.get("name", "")) == k
                               for r in self.recents)

    # -- 手动分类 --
    def category_override(self, target: str, name: str = "") -> str:
        return self.categories.get(self.key(target, name), "")

    def set_category(self, target: str, category: str, name: str = "") -> None:
        """手动指定类别；category 为空或未知则清除手动设置（回到自动分类）。"""
        k = self.key(target, name)
        if not k:
            return
        if category in CATEGORY_LABELS:
            self.categories[k] = category
        else:
            self.categories.pop(k, None)

    def overrides(self) -> dict[str, str]:
        return dict(self.categories)

    # -- 持久化（只是纯数据转换，不碰文件）--
    def to_dict(self) -> dict:
        return {"favorites": list(self.favorites),
                "recents": [dict(r) for r in self.recents],
                "categories": dict(self.categories)}

    @classmethod
    def from_dict(cls, data, max_recent: int = 30) -> "CatalogPrefs":
        """从 settings 里的 dict 还原；任何脏数据都容忍（不抛异常）。"""
        p = cls(max_recent=max_recent)
        if not isinstance(data, dict):
            return p
        favs = data.get("favorites")
        if isinstance(favs, list):
            p.favorites = [str(x) for x in favs if isinstance(x, (str, int))]
        recents = data.get("recents")
        if isinstance(recents, list):
            for r in recents:
                if isinstance(r, dict) and r.get("path"):
                    try:
                        p.recents.append({"path": str(r.get("path", "")),
                                          "name": str(r.get("name", "")),
                                          "ts": float(r.get("ts") or 0)})
                    except (TypeError, ValueError):
                        continue
            p.recents.sort(key=lambda r: -r["ts"])
            del p.recents[p.max_recent:]
        cats = data.get("categories")
        if isinstance(cats, dict):
            p.categories = {str(k).lower(): str(v) for k, v in cats.items()
                            if str(v) in CATEGORY_LABELS}
        return p


def start_menu_dirs() -> list[str]:
    """所有开始菜单目录（当前用户 + 所有用户）。"""
    out = []
    for base in (os.environ.get("APPDATA"), os.environ.get("ProgramData")):
        if base:
            d = os.path.join(base, r"Microsoft\Windows\Start Menu\Programs")
            if os.path.isdir(d):
                out.append(d)
    return out


def scan_start_menu() -> list[dict]:
    """枚举开始菜单里的 .lnk（不解析目标，解析交给 resolve_shortcuts）。"""
    out = []
    for d in start_menu_dirs():
        for root, _dirs, files in os.walk(d):
            for f in files:
                if f.lower().endswith(".lnk"):
                    name = os.path.splitext(f)[0]
                    if "卸载" in name or "uninstall" in name.lower():
                        continue                    # 卸载入口不作为可启动程序
                    out.append({"name": name, "lnk": os.path.join(root, f),
                                "source": "startmenu"})
    return out


_PS_RESOLVE = r"""
$s = New-Object -ComObject WScript.Shell
$lines = Get-Content -LiteralPath $env:WINMON_LNK_LIST -Encoding UTF8
$r = foreach ($p in $lines) {
  if ($p) {
    try {
      $l = $s.CreateShortcut($p)
      [pscustomobject]@{ p=$p; t=$l.TargetPath; a=$l.Arguments; w=$l.WorkingDirectory }
    } catch { }
  }
}
$r | ConvertTo-Json -Compress
"""

# 输入路径含中文，必须同时解决"输入"和"输出"两个编码陷阱：
#   输入：Windows PowerShell 5.1 一律用**系统代码页**（简中为 GBK）解码 stdin，
#         即使先执行 [Console]::InputEncoding=[Text.Encoding]::UTF8 也无效（实测）。
#         中文路径会变成"鍝斿摡鍝斿摡"这种乱码 → 改写临时 UTF-8 文件 + Get-Content -Encoding UTF8。
#   输出：管道重定向时 [Console]::OutputEncoding 默认仍是本地代码页，
#         中文目标路径（如 X:\Program Files\bilibili\哔哩哔哩.exe）会被编码成非 UTF-8，
#         在 Python 侧解出来是乱码 → 导致 os.path.exists 失败、程序被误判为"目标不存在"。
#         所以在脚本开头强制把 OutputEncoding 与 $OutputEncoding 都设为 UTF-8。
# 下面这段前缀只对 Windows PowerShell 5.1 需要；PowerShell 7+ 本来就是 UTF-8，
# 但重复设置是无害的，故统一加上。
_PS_UTF8_PREFIX = ("[Console]::OutputEncoding=[Text.Encoding]::UTF8;"
                   "$OutputEncoding=[Text.Encoding]::UTF8;")


def _powershell_command(script: str, extra_env: dict | None = None) -> tuple[list[str], dict]:
    """构造 PowerShell 调用参数与附加环境变量（尽量保证中文编码正确）。"""
    ps = "pwsh" if shutil.which("pwsh") else "powershell"
    env = dict(os.environ)
    if extra_env:
        env.update(extra_env)
    if ps == "powershell":
        script = _PS_UTF8_PREFIX + script
    return [ps, "-NoProfile", "-NonInteractive", "-Command", script], env


def _json_str(value) -> str:
    """
    取出 JSON 里的字符串值。

    Windows PowerShell 5.1 的 ConvertTo-Json 有时会把字符串属性序列化成
    {"value": "...", "Count": n} 这样的包装对象（尤其是有扩展属性时），
    这里统一拆包，避免调用方拿到 dict 而报错。
    """
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        for key in ("value", "Value"):
            if key in value:
                return _json_str(value[key])
        return ""
    if isinstance(value, list):
        return _json_str(value[0]) if value else ""
    return str(value)


def resolve_shortcuts(paths: list[str], timeout: float = 120.0) -> dict[str, dict]:
    """
    批量解析 .lnk 的目标 / 参数 / 工作目录。

    返回 {lnk 路径: {"target","args","workdir"}}。解析失败不会抛异常，
    只是该条目不出现在结果里（调用方按"没有目标"处理）。
    """
    if not paths:
        return {}
    import json
    import subprocess
    import tempfile

    list_file = os.path.join(tempfile.gettempdir(),
                             f"winmon_lnk_{os.getpid()}_{int(time.time() * 1000)}.txt")
    try:
        with open(list_file, "w", encoding="utf-8") as fh:
            fh.write("\n".join(paths))
        cmd, env = _powershell_command(_PS_RESOLVE, {"WINMON_LNK_LIST": list_file})
        kw = {}
        if hasattr(subprocess, "CREATE_NO_WINDOW"):
            kw["creationflags"] = subprocess.CREATE_NO_WINDOW
        proc = subprocess.run(cmd, capture_output=True, timeout=timeout,
                              env=env, **kw)
    except Exception:
        return {}
    finally:
        try:
            os.remove(list_file)
        except OSError:
            pass
    text = (proc.stdout or b"").decode("utf-8", "replace").strip()
    if not text:
        return {}
    try:
        data = json.loads(text)
    except Exception:
        return {}
    if isinstance(data, dict):
        data = [data]
    elif not isinstance(data, list):
        return {}                     # 例如某些 PowerShell 版本空结果输出 null
    out = {}
    for it in data:
        # 元素也得是对象：JSON 顶层是数组但里面混进数字/字符串时（PowerShell 输出被
        # 别的东西污染过就可能这样），`it.get` 会抛 AttributeError，把整次目录扫描
        # 掀掉 —— 与上面那条"顶层不是 list/dict 就返回 {}"是同一条规矩：**读不出来
        # 就当没有**，绝不抛异常给调用方（本项由 test_b11 的 payload 覆盖）。
        if not isinstance(it, dict):
            continue
        p = _json_str(it.get("p"))
        if p:
            out[p] = {"target": _json_str(it.get("t")), "args": _json_str(it.get("a")),
                      "workdir": _json_str(it.get("w"))}
    return out


def scan_registry_programs() -> list[dict]:
    """枚举注册表里已安装的程序（没有开始菜单项的程序靠它补齐）。"""
    out = []
    try:
        import winreg
    except ImportError:  # pragma: no cover
        return out
    seen = set()
    for hive, flag in ((winreg.HKEY_LOCAL_MACHINE, winreg.KEY_WOW64_64KEY),
                       (winreg.HKEY_LOCAL_MACHINE, winreg.KEY_WOW64_32KEY),
                       (winreg.HKEY_CURRENT_USER, 0)):
        try:
            key = winreg.OpenKey(hive, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall",
                                 0, winreg.KEY_READ | flag)
        except OSError:
            continue
        try:
            count = winreg.QueryInfoKey(key)[0]
            for i in range(count):
                try:
                    sub = winreg.EnumKey(key, i)
                    with winreg.OpenKey(key, sub) as sk:
                        def val(nm):
                            try:
                                return winreg.QueryValueEx(sk, nm)[0]
                            except OSError:
                                return ""
                        name = val("DisplayName")
                        if not name:
                            continue
                        # 过滤系统补丁/组件（没有卸载信息或名字异常的不列入）
                        if val("SystemComponent") in (1, "1"):
                            continue
                        if name.lower() in seen:
                            continue
                        seen.add(name.lower())
                        out.append({
                            "name": name,
                            "icon": val("DisplayIcon"),
                            "install": val("InstallLocation"),
                            "uninstall": val("UninstallString"),
                            "publisher": val("Publisher"),
                            "source": "registry",
                        })
                except OSError:
                    continue
        finally:
            key.Close()
    return out


def _exe_from_icon_ref(icon_ref: str) -> str:
    """注册表的 DisplayIcon 常形如 "C:\\a\\b.exe,0" 或带引号，取出其中的 exe 路径。"""
    if not icon_ref:
        return ""
    s = icon_ref.strip().strip('"')
    if s.lower().endswith(".ico"):
        return ""
    # 去掉 ",0" 这类图标索引
    if "," in s:
        head, _, tail = s.rpartition(",")
        if tail.strip().lstrip("-").isdigit():
            s = head.strip().strip('"')
    return s if s.lower().endswith(".exe") else ""


def build_program_catalog(use_cache: bool = True,
                          cache_path: str = "",
                          progress=None) -> list[ProgramItem]:
    """
    构建程序目录（合并开始菜单 + 注册表 + 当前正在运行的程序）。

    use_cache 为真时，若缓存文件比所有快捷方式都新，就直接用缓存，
    避免每次启动都去解析 100 多个快捷方式。
    cache_path 默认在**运行时数据目录**（`data_dir()`）下的 `winmon_catalog.json`
    —— 不再写在程序目录里（理由见 `data_dir()` 那一节）。
    progress 是一个可调用对象，用于向界面回报进度文字。
    """
    import json
    if not cache_path:
        cache_path = data_file("winmon_catalog.json")
        ensure_data_dir()               # 默认位置：写之前确保数据目录在

    def say(msg):
        if progress:
            try:
                progress(msg)
            except Exception:
                pass

    shortcuts = scan_start_menu()
    say(f"扫描到 {len(shortcuts)} 个开始菜单快捷方式")

    # ---- 尝试读缓存 ----
    if use_cache and os.path.exists(cache_path):
        try:
            newest_lnk = max((os.path.getmtime(s["lnk"]) for s in shortcuts), default=0)
            if os.path.getmtime(cache_path) >= newest_lnk:
                with open(cache_path, "r", encoding="utf-8") as fh:
                    raw = json.load(fh)
                # 缓存格式版本不一致就作废重扫。
                # 为什么需要：改进去重/归类逻辑后，旧缓存里的重复项会继续显示，
                # 让人误以为"改动没生效"（实测踩过一次）。
                if raw.get("v") != CATALOG_CACHE_VERSION:
                    raise ValueError("缓存格式版本不一致，重新扫描")
                items = [ProgramItem(**{k: v for k, v in d.items()
                                        if k in ProgramItem.__slots__})
                         for d in raw.get("items", [])]
                if items:
                    say(f"使用缓存目录（{len(items)} 项）")
                    # 缓存路径也要过滤：旧缓存可能是"过滤规则加入之前"写的，
                    # 而且过滤规则本身以后还可能调整
                    items = dedupe_catalog(filter_non_programs(_merge_running(items))[0])
                    return items
        except Exception:
            pass

    # ---- 解析快捷方式目标 ----
    say(f"正在解析 {len(shortcuts)} 个快捷方式的目标…")
    t0 = time.perf_counter()
    resolved = resolve_shortcuts([s["lnk"] for s in shortcuts])
    say(f"快捷方式解析完成（{len(resolved)}/{len(shortcuts)}，{time.perf_counter() - t0:.1f}s）")

    items: list[ProgramItem] = []
    for s in shortcuts:
        r = resolved.get(s["lnk"], {})
        target = r.get("target", "")
        if not target:
            continue                       # 解析不到目标（如 UWP 应用）暂不列入
        if not os.path.exists(target):
            continue                       # 目标已失效，跳过，避免"点了没反应"
        items.append(ProgramItem(
            name=s["name"], target=target, args=r.get("args", ""),
            workdir=r.get("workdir", ""), source="startmenu"))

    # 同名去重（保留第一个）
    seen_names = {it.name.lower() for it in items}

    # ---- 注册表补齐 ----
    for reg in scan_registry_programs():
        if reg["name"].lower() in seen_names:
            continue
        exe = _exe_from_icon_ref(reg.get("icon", ""))
        if not exe or not os.path.exists(exe):
            # 没有可启动 exe 的只在"安装位置"里找一个主 exe
            exe = _guess_main_exe(reg.get("install", ""), reg["name"])
        if not exe:
            continue
        items.append(ProgramItem(
            name=reg["name"], target=exe, source="registry",
            publisher=reg.get("publisher", ""),
            install_location=reg.get("install", ""), icon_ref=reg.get("icon", "")))
        seen_names.add(reg["name"].lower())

    say(f"程序目录构建完成：共 {len(items)} 项")

    # 组合顺序很重要：先去重 → 并入"正在运行" → 再去重 → 最后过滤非程序条目。
    #   · 先去重：合并三个数据源的重复项（实测 112 -> 97）
    #   · 再并入运行中：绿色软件/免安装程序也能被管理
    #   · 再去重：`_merge_running` 可能把运行中进程补成新条目，从而重新引入重复
    #   · 最后过滤：注册表来源会带进 .txt/.chm/.html 这类非程序条目
    items = dedupe_catalog(_merge_running(dedupe_catalog(items)))
    items, dropped = filter_non_programs(items)
    if dropped:
        say(f"已过滤 {len(dropped)} 个非程序条目：{'、'.join(dropped[:4])}"
            + ("…" if len(dropped) > 4 else ""))

    # ---- 写缓存（写最终结果，保证"缓存加载"与"全新扫描"给出同样的东西）----
    try:
        with open(cache_path, "w", encoding="utf-8") as fh:
            json.dump({"v": CATALOG_CACHE_VERSION,
                       "built": time.strftime("%Y-%m-%d %H:%M:%S"),
                       "items": [it.to_dict() for it in items]}, fh, ensure_ascii=False)
    except Exception:
        pass

    return items


def is_runnable_target(path: str) -> bool:
    """
    判断一个路径是否属于"用户真的能启动"的程序目标。

    用于过滤注册表来源里混进来的文档（License -> .txt、7-Zip Help -> .chm 等）。
    规则：
      · 没有扩展名 → 不算（避免把目录、无扩展名文件当真）
      · 扩展名在 `CATALOG_TARGET_EXTS` 里 → 算
      · 其它一律不算

    注意：**不做"存在性"检查**，那是调用方的事（有的目标确实会失效，
    但界面上标注"不可用"比直接消失更有用）。
    """
    ext = os.path.splitext(path or "")[1].lower()
    if not ext:
        return False
    return ext in CATALOG_TARGET_EXTS


def filter_non_programs(items: list[ProgramItem]) -> tuple[list[ProgramItem], list[str]]:
    """
    剔除"目标不是可启动程序"的条目，返回 (保留的条目, 被剔除的说明)。

    单独返回说明是为了：
      · 命令行 `--catalog --verbose` 能告诉用户"过滤掉了什么"
      · 测试能断言"确实过滤了 7-Zip Help 这类条目"
    """
    kept: list[ProgramItem] = []
    dropped: list[str] = []
    for it in items:
        target = it.target or ""
        if not target:
            # 没有目标的条目（UWP 之类）保留：界面上另有用处（提示从开始菜单启动）
            kept.append(it)
            continue
        if is_runnable_target(target):
            kept.append(it)
            continue
        ext = os.path.splitext(target)[1].lower() or "(无扩展名)"
        dropped.append(f"{it.name} -> {ext}")
    return kept, dropped


def dedupe_catalog(items: list[ProgramItem]) -> list[ProgramItem]:
    """
    去掉"同一个程序出现在多条"的重复项，返回新列表。

    为什么需要：三个数据源会互相重叠 —— 实测同一台机器上
    `X:\\steam\\steam.exe` 与 `X:\\steam\\Steam.exe`（只差大小写）、
    以及名称带版本号的 `网易云音乐` 与 `网易云音乐` 会各出现一条，
    界面上看着像装了两份。

    判据按可靠性排序：
      1) 目标路径（小写）完全相同 → 重复
      2) 安装位置 + 软件名（小写）完全相同 → 重复
         （覆盖"同目录下不同版本号 exe"这种情况）
    保留**先出现的**那条（开始菜单项优先，它带启动参数与工作目录），
    但会把后面重复项里"多出来的信息"补进保留项（例如只能在注册表项里拿到的
    发行商 / 安装位置），避免去重反而丢信息。
    """
    out: list[ProgramItem] = []
    by_path: dict[str, ProgramItem] = {}
    by_name_install: dict[tuple[str, str], ProgramItem] = {}

    for it in items:
        path_key = (it.target or "").lower()
        name_key = (it.name or "").lower()
        install_key = (it.install_location or "").lower()
        dup = None
        if path_key:
            dup = by_path.get(path_key)
        if dup is None and name_key and install_key:
            dup = by_name_install.get((name_key, install_key))
        if dup is not None:
            # 把重复项里更有信息量的字段补进保留项
            for field in ("publisher", "install_location", "icon_ref", "workdir", "args"):
                if not getattr(dup, field, "") and getattr(it, field, ""):
                    setattr(dup, field, getattr(it, field))
            if not dup.pid and it.pid:
                dup.pid = it.pid
            continue
        out.append(it)
        if path_key:
            by_path[path_key] = it
        if name_key and install_key:
            by_name_install[(name_key, install_key)] = it
    return out


def _guess_main_exe(install_location: str, name: str) -> str:
    """在安装目录里猜一个主程序 exe（优先名字相近的）。"""
    if not install_location or not os.path.isdir(install_location):
        return ""
    try:
        exes = [os.path.join(install_location, f)
                for f in os.listdir(install_location)
                if f.lower().endswith(".exe")]
    except OSError:
        return ""
    if not exes:
        return ""
    key = "".join(ch for ch in name.lower() if ch.isalnum())[:6]
    for e in exes:
        base = "".join(ch for ch in os.path.basename(e).lower() if ch.isalnum())
        if key and key in base:
            return e
    # 退而求其次：取名字最短的（通常就是主程序而非辅助工具）
    return min(exes, key=lambda p: len(os.path.basename(p)))


def _looks_like_path_title(title: str) -> bool:
    """
    判断窗口标题其实是「一个文件路径」而不是软件名。

    为什么需要：Electron 应用（哔哩哔哩客户端、某个 AI 客户端等）在窗口刚创建、
    标题还没设好时，会短暂把 exe 完整路径当标题；命令行 `start "标题" exe` 用错参数
    也会造成同样结果。若不识别，这类窗口会被当成一个新"程序"混进目录
    （实测出现过一条名为 "X:\\Program Files\\bilibili\\哔哩哔哩.exe" 的假程序）。
    """
    t = (title or "").strip().strip('"')
    if not t or len(t) < 4:
        return False
    low = t.lower()
    if low.endswith((".exe", ".lnk", ".bat", ".cmd", ".com", ".msi")):
        return True
    return (":\\" in t or ":/" in t) and ("\\" in t or "/" in t)


# 不该出现在「程序目录」里的运行中进程：它们是系统内部组件，
# 用户不会去"启动"它们，混进来只会让目录变吵（要管理它们请用窗口列表/进程树）。
CATALOG_EXCLUDED_RUNNING = {
    "explorer.exe", "systemsettings.exe", "textinputhost.exe",
    "applicationframehost.exe", "shellexperiencehost.exe",
    "startmenuexperiencehost.exe", "searchhost.exe", "runtimebroker.exe",
    "dwm.exe", "sihost.exe", "ctfmon.exe", "taskhostw.exe", "msedgewebview2.exe",
}


def _merge_running(items: list[ProgramItem]) -> list[ProgramItem]:
    """
    把"当前正在运行"的程序并入目录（可切前台/结束，而不只是启动）。

    关联顺序（从最可靠到最宽松）：
      1) 进程的可执行文件全路径 == 目录条目的 target
      2) 进程 exe 文件名 == target 的文件名
      3) 进程 exe 去掉扩展名的文件名 == 条目名称
    只用进程名去比会漏（同一程序常在别处出现），只比路径又会漏掉
    "快捷方式指向 A、实际跑的是 B"的情况，所以三级都试一遍。
    已经匹配上的运行进程不会再补成新条目，否则会看到同名的两条。
    """
    try:
        by_name: dict[str, int] = {}          # 窗口显示名 -> pid
        by_path: dict[str, int] = {}          # exe 全路径(小写) -> pid
        by_base: dict[str, int] = {}          # exe 文件名(小写) -> pid
        for wi in enum_windows(include_cloaked=True):
            if wi.is_frame_host or _looks_like_path_title(wi.title):
                continue
            if not wi.pid:
                continue
            name = wi.real_name
            by_name.setdefault(name.lower(), wi.pid)
            path, _ = query_process_detail(wi.pid)
            if path:
                by_path.setdefault(path.lower(), wi.pid)
                by_base.setdefault(os.path.basename(path).lower(), wi.pid)

        matched_pids: set[int] = set()
        for it in items:
            # **先把上次扫描留下的 pid 作废**（2026-10-13 修的真 bug）：
            # pid 会跨会话留在 `winmon_catalog.json` 里，而系统会**回收**进程号 ——
            # 昨天网易云占着 4321，今天 4321 可能是别的程序。不归零的话，
            # 界面上那一行会显示"运行中"，点「打开」直接被短路成"已在运行"、
            # 把**别的程序**的窗口切到前台，操作历史还记一条"成功"。
            # 只有这一轮真的匹配到活进程，pid 才重新有值。
            it.pid = 0
            pid = 0
            if it.target:
                low = it.target.lower()
                pid = by_path.get(low, 0) or by_base.get(os.path.basename(low), 0)
            if not pid:
                pid = by_name.get(it.name.lower(), 0)
                if not pid:
                    for nm, p in by_name.items():
                        if nm.startswith(it.name.lower()) or it.name.lower() in nm:
                            pid = p
                            break
            if pid:
                it.pid = pid
                matched_pids.add(pid)

        # 运行中但目录里没有的（例如绿色软件），补进来；已匹配的不重复添加
        known_paths = {(it.target or "").lower() for it in items if it.target}
        known_bases = {os.path.basename(p) for p in known_paths}
        known_names = {it.name.lower() for it in items}
        for lname, pid in by_name.items():
            if pid in matched_pids:
                continue
            if lname in known_names:
                continue
            path, _ = query_process_detail(pid)
            if not path:
                continue
            base = os.path.basename(path).lower()
            if base in CATALOG_EXCLUDED_RUNNING:
                continue
            if path.lower() in known_paths or base in known_bases:
                continue
            items.append(ProgramItem(name=os.path.splitext(os.path.basename(path))[0],
                                     target=path, source="running", pid=pid))
            matched_pids.add(pid)
    except Exception:
        pass
    return items


def normalize_program_query(text) -> str:
    """
    程序搜索的统一归一化：大小写、空格、路径分隔符和标点都不参与匹配。

    为什么需要它：同一个程序在 Windows 里至少有三个名字 ——
    开始菜单里的「网易云音乐」、exe 的 `cloudmusic.exe`、窗口的
    `Cloud Music`。用户不该记得自己输入的是哪一种；去掉空格与符号后，
    `cloud music`、`CloudMusic`、`cloudmusic.exe` 会得到同一个 key。
    """
    value = str(text or "").casefold()
    return "".join(ch for ch in value if ch.isalnum())


def program_search_blob(item: ProgramItem) -> str:
    """
    一个目录条目参与搜索的全部文本（原名 + exe + 安装路径 + 发行商 + 类别）。

    不缓存：目录只有一百多项，每次筛选重建时重算是毫秒级；缓存反而会
    在用户手动改类别后读到旧值。
    """
    target = item.target or ""
    base = os.path.basename(target)
    stem = os.path.splitext(base)[0]
    category = CATEGORY_LABELS.get(category_of_item(item), "")
    return " ".join((
        item.name or "", target, base, stem,
        item.install_location or "", item.publisher or "", category,
    ))


def program_matches_query(item: ProgramItem, query: str) -> bool:
    """目录条目的模糊匹配；空查询视为全部匹配。"""
    needle = normalize_program_query(query)
    if not needle:
        return True
    return needle in normalize_program_query(program_search_blob(item))


def search_program_items(items: list[ProgramItem], query: str,
                         limit: int = 0) -> list[ProgramItem]:
    """
    按用户输入给程序目录排序（纯逻辑，可单测）。

    排序：名称/exe 完全相等 > 名称/exe 开头 > 名称/exe 包含 > 路径/发行商/类别包含。
    同级里可启动的、名字更短的优先。`limit <= 0` 返回全部匹配。
    """
    needle = normalize_program_query(query)
    if not needle:
        return []
    scored = []
    for it in items:
        name = normalize_program_query(it.name)
        target = it.target or ""
        base = normalize_program_query(os.path.basename(target))
        stem = normalize_program_query(os.path.splitext(os.path.basename(target))[0])
        if needle in (name, base, stem):
            rank = 0
        elif name.startswith(needle) or base.startswith(needle) or stem.startswith(needle):
            rank = 1
        elif needle in name or needle in base or needle in stem:
            rank = 2
        elif needle in normalize_program_query(program_search_blob(it)):
            rank = 3
        else:
            continue
        bonus = 0 if it.runnable else 1
        scored.append((rank, bonus, len(it.name or ""), (it.name or "").lower(), it))
    scored.sort(key=lambda x: x[:4])
    found = [it for *_rest, it in scored]
    return found if int(limit or 0) <= 0 else found[:int(limit)]


def find_programs(keyword: str, items: list[ProgramItem] | None = None,
                  limit: int = 20) -> list[ProgramItem]:
    """
    按名称 / 路径模糊查找程序，用于"精准打开某个程序"。

    排序：名称完全相等 > 名称以关键词开头 > 名称包含 > 路径包含；
    同一档次内"名字更短"的优先（更可能是主程序而不是辅助工具）。
    """
    if items is None:
        items = build_program_catalog()
    kw = (keyword or "").strip().lower()
    if not kw:
        return []
    scored = []
    for it in items:
        name = it.name.lower()
        base = os.path.basename(it.target).lower() if it.target else ""
        if kw == name or kw == base or kw == os.path.splitext(base)[0]:
            rank = 0
        elif name.startswith(kw) or base.startswith(kw):
            rank = 1
        elif kw in name or kw in base:
            rank = 2
        elif kw in (it.target or "").lower():
            rank = 3
        else:
            continue
        bonus = 0 if it.runnable else 1          # 可启动的排前面
        scored.append((rank, bonus, len(it.name), it))
    scored.sort(key=lambda x: (x[0], x[1], x[2]))
    return [it for *_rest, it in scored[:limit]]


def _proc_names_of(path: str) -> set[str]:
    """目标路径对应的进程名集合（用于判断"是否真的起来了"）。"""
    base = os.path.basename(path or "").lower()
    names = {base}
    if base.endswith(".exe"):
        names.add(base[:-4])
    return names


def is_program_running(path: str, procs: dict | None = None) -> bool:
    """
    目标 exe 当前是否在运行。

    **优先按完整路径比**（2026-10-13 修的真 bug）：只比 exe 文件名的话，
    `C:\\A\\app.exe` 在跑会让 `X:\\B\\app.exe` 也被判成"已在运行" ——
    `launch_item()` 于是**什么都不启动**，只回一句"已在运行，已尝试切到前台"，
    还把**那个程序**的窗口切到前台（两个目录各装一份、便携版与安装版并存都会碰到）。

    Toolhelp 快照里只有文件名，所以命中同名进程时要**现查一次完整路径**
    （每次一个 OpenProcess，通常只有一两个候选）。只有**查不到路径**时
    才退回按名字算命中 —— 宁可宽松（可能不启动），也不要因为查不到就重复启动。
    """
    if not path:
        return False
    names = _proc_names_of(path)
    want = os.path.normcase(os.path.abspath(path)) if os.path.isabs(path) else ""
    if procs is None:
        procs = snapshot_processes()
    nameless_hit = False
    for pid, info in procs.items():
        nm = (info.get("name") or "").lower()
        if nm not in names and not (nm.endswith(".exe") and nm[:-4] in names):
            continue
        got = str(info.get("path") or "")
        if not got:
            try:
                got, _wss = query_process_detail(pid)
            except Exception:
                got = ""
        if got:
            if not want or os.path.normcase(os.path.abspath(got)) == want:
                return True
            continue                 # 同名但**不是这个程序**：不算它
        nameless_hit = True          # 名字对得上，但拿不到路径
    return nameless_hit


def start_via_shell(path: str, args: str = "", workdir: str = "") -> tuple[bool, str]:
    """
    交给操作系统的 shell 去启动（explorer.exe）。

    为什么需要这条路径：
      实测某些 Electron 应用（如哔哩哔哩）从脚本进程直接 ShellExecute 时会
      在初始化前就退出，而用 explorer.exe 打开它的快捷方式/程序则可以正常拉起。
      explorer 是真正的 shell 宿主，由它转交启动最贴近"用户双击"的语义，
      连"前台激活已运行实例"这类行为也由系统正确处理。

    **`args` 送不进去 —— 这是实测结论，不是偷懒**（2026-10-13）：
      · `explorer.exe "<某个 .cmd>"`            → 程序真的起来了（argv 为空）；
      · `explorer.exe "<同一个 .cmd>" 参数1 参数2` → **什么都没启动**（连进程都没有）。
      也就是说 explorer 只认"给它一个路径"，多给的参数既不会转交、还会把启动搞坏。
      所以**带参数**时这里如实拒绝（返回 False + 原因），由调用方决定怎么办 ——
      绝不假装带着参数启动了。`workdir` 只影响"起始目录"（shell 决定），
      不挡启动，但会在返回的说明里写清楚。
    """
    if not path:
        return False, "没有可执行目标"
    if args:
        return False, ("系统 shell（explorer）没法带参数启动"
                       "（实测：`explorer.exe \"程序\" 参数` 连程序都不启动）")
    import subprocess
    kw = {}
    if hasattr(subprocess, "CREATE_NO_WINDOW"):
        kw["creationflags"] = subprocess.CREATE_NO_WINDOW
    try:
        subprocess.Popen(["explorer.exe", path], **kw)
        note = "（起始目录由系统 shell 决定）" if workdir else ""
        return True, f"已通过系统 shell（explorer）启动{note}"
    except Exception as exc:
        return False, f"explorer 启动失败：{type(exc).__name__}: {exc}"


def launch_item(item: ProgramItem, as_admin: bool = False,
                extra_args: str = "", verify: bool = True,
                settle: float = 2.0, always_shell: bool = False) -> tuple[bool, str]:
    """
    启动一个程序（"精准打开"），并可选地确认它真的起来了。

    启动策略（实战验证后确定，见 README「启动策略」）：
      1) ShellExecuteW（普通用 open 动词、提权用 runas 动词）—— 与双击图标语义一致，
         能正确处理工作目录、启动参数、文件关联与 UAC 提权；
      2) 若它报告成功、但短时间内没看到对应进程，就改用 explorer.exe 再启动一次。
         实测哔哩哔哩这类 Electron 应用从脚本进程直接 ShellExecute 可能"静默失败"
         （进程在初始化前退出），而**交给 explorer（真正的 shell 宿主）能稳定拉起**；
      3) 目标已在运行时不再重复启动，而是切到前台 —— 很多程序有单实例机制，
         重复启动只会让新进程交接后立刻退出，看起来像"没反应"。

    always_shell=True 可直接走 explorer 路径（用于排除 ShellExecute 的干扰）。
    as_admin=True 只走 runas，不做 explorer 兜底（explorer 无法代提权）。
    """
    target = item.target
    if not target:
        return False, "该项没有可执行目标（可能是 UWP 应用，请从开始菜单启动）"
    if not os.path.exists(target):
        return False, f"目标不存在：{target}"
    args = " ".join(x for x in (item.args, extra_args) if x).strip()
    workdir = item.workdir if item.workdir and os.path.isdir(item.workdir) else ""

    # ---- 已经在运行：有可见窗口就切前台；只有后台进程则继续启动 ----
    #
    # 2026-10-21 修 Edge 打不开：Edge 的“启动预加载 / 后台扩展”会留下
    # msedge.exe 进程但没有任何顶层窗口（任务管理器里 5 个进程、MainWindowHandle=0）。
    # 旧逻辑只要 is_program_running() 为真就 return "已在运行，已尝试切到前台"，
    # 根本没调用 ShellExecute —— 用户看到的就是“点打开没反应”。
    # 正确语义是：没有可见窗口不算“已经打开”，继续请求一个窗口。
    already_background = False
    if not extra_args and not always_shell:
        procs = snapshot_processes()
        if is_program_running(target, procs):
            hwnd = _find_window_for_target(target, procs)
            if hwnd:
                try:
                    user32.ShowWindow(w.HWND(hwnd), SW_RESTORE)
                    user32.SetForegroundWindow(w.HWND(hwnd))
                except Exception:
                    pass
                return True, f"{item.name} 已在运行，已尝试切到前台"
            already_background = True

    if always_shell:
        ok, msg = start_via_shell(target, args, workdir)
        if not ok:
            return False, msg
        if not verify:
            return True, f"已通过系统 shell 启动 {item.name}"
        if _wait_running(target, 3.0):
            return True, f"已启动 {item.name}（经系统 shell）"
        return True, f"已请求启动 {item.name}（未能确认进程是否出现）"

    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    shell32.ShellExecuteW.argtypes = [w.HWND, w.LPCWSTR, w.LPCWSTR, w.LPCWSTR,
                                      w.LPCWSTR, ctypes.c_int]
    shell32.ShellExecuteW.restype = ctypes.c_void_p
    verb = "runas" if as_admin else "open"
    SW_SHOWNORMAL = 1
    try:
        ret = shell32.ShellExecuteW(None, verb, target, args or None,
                                    workdir or None, SW_SHOWNORMAL)
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"
    code = (ret or 0) & 0xFFFFFFFF
    if code <= 32:
        reasons = {2: "文件未找到", 3: "路径未找到", 5: "拒绝访问", 26: "共享冲突",
                   27: "文件关联不完整", 28: "DDE 超时", 29: "DDE 失败",
                   30: "DDE 忙", 31: "无关联程序", 32: "DLL 未找到"}
        if code == 5 and not as_admin:
            return False, "拒绝访问：该程序需要管理员权限，请用「以管理员身份打开」"
        return False, f"启动失败（ShellExecute 返回 {code}：{reasons.get(code, '未知错误')}）"

    if as_admin or not verify:
        return True, f"已启动 {item.name}" + ("（管理员）" if as_admin else "")

    if already_background:
        # 后台进程已经在，但刚才没有可见窗口：不等“进程存在”（它一直存在），
        # 只等一个**新的可见顶层窗口**。这是 Edge 场景的关键判据。
        # 等待从 3 秒放宽到 4 秒：冷启动 + 扩展加载时首个窗口可能晚于 3 秒，
        # 过早走 explorer 兜底会再请求一次启动，用户可能看到多余标签页。
        hwnd = _wait_window_for_target(target, max(4.0, settle + 2.0), procs)
        if hwnd:
            return True, f"{item.name} 已在后台运行，已打开窗口"
        ok, msg = start_via_shell(target, args, workdir)
        if ok:
            hwnd = _wait_window_for_target(target, 4.0, procs)
            if hwnd:
                return True, f"{item.name} 已在后台运行，已通过系统 shell 打开窗口"
        return True, (f"{item.name} 正在后台运行；已请求打开窗口，"
                      f"但未确认可见窗口（可再点一次，或从开始菜单打开）")

    if _wait_running(target, max(0.3, settle)):
        return True, f"已启动 {item.name}"

    # ---- 兜底：交给 explorer 再启动一次 ----
    # **带参数时不走这条兜底**：explorer 送不进参数
    # （实测给它参数它连程序都不启动），硬兜底的结果要么是"根本没起来"、
    # 要么是"起来一个没有参数的第二实例"，而状态栏还会报"兜底成功"。
    # 与其假报成功，不如照实说清楚（用户可以在界面里去掉参数再试）。
    if args:
        return True, (f"已请求启动 {item.name}（未能确认进程出现；"
                      f"带参数启动不走 explorer 兜底 —— 它送不进参数。"
                      f"如果它没起来，请手动启动一次，或去掉参数再试）")
    ok, msg = start_via_shell(target, args, workdir)
    if not ok:
        return True, f"已请求启动 {item.name}（未确认进程出现，兜底也失败：{msg}）"
    if _wait_running(target, 4.0):
        note = "（起始目录由系统 shell 决定）" if workdir else ""
        return True, f"已启动 {item.name}（ShellExecute 未确认，经系统 shell 兜底成功）{note}"
    return True, f"已请求启动 {item.name}（未能确认进程是否出现）"


def _wait_window_for_target(path: str, seconds: float,
                            procs: dict | None = None) -> int:
    """
    等目标程序出现一个**可见顶层窗口**，返回 hwnd（没有则 0）。

    和 `_wait_running()` 的区别：后台常驻进程（Edge 启动预加载、Steam 托盘）
    一直有进程，所以“进程出现了”不能证明“窗口打开了”。这里只认窗口。
    每次重查窗口列表；调用方传进来的进程快照只当提示，窗口若属于快照里
    没有的新 pid，`_find_window_for_target()` 会现查完整路径再判断。
    """
    import time as _time
    snapshot = procs if procs is not None else snapshot_processes()
    deadline = _time.monotonic() + max(0.2, seconds)
    while _time.monotonic() < deadline:
        hwnd = _find_window_for_target(path, snapshot)
        if hwnd:
            return hwnd
        _time.sleep(0.15)
    return 0


def _wait_running(path: str, seconds: float) -> bool:
    """在给定时间内轮询，目标进程是否出现。"""
    import time as _time
    deadline = _time.monotonic() + max(0.1, seconds)
    while _time.monotonic() < deadline:
        _time.sleep(0.15)
        if is_program_running(path):
            return True
    return False


def _find_window_for_target(target: str, procs: dict) -> int:
    """
    找目标程序当前可见的顶层窗口（用于"已在运行时切到前台"）。

    **先按完整路径找，找不到才退回按 exe 名找**（2026-10-13 修的真 bug）：
    同名但不同目录的两个程序同时开着时，原来先比名字就直接返回了 ——
    于是"打开 A"会把 **B 的窗口**切到前台（用户看到的是"点 A 跳出来 B"）。
    另外顺带省掉了"对每个窗口都查一次进程路径"的开销（只有同名进程才查）。
    """
    base = os.path.basename(target or "").lower()
    if not base:
        return 0
    want = (os.path.normcase(os.path.abspath(target))
            if os.path.isabs(target or "") else "")
    by_name = 0
    for wi in enum_windows(include_cloaked=True):
        if wi.is_hidden_surface or not wi.pid:
            continue
        info = procs.get(wi.pid)
        is_named = bool(info and (info.get("name") or "").lower() == base)
        if info and not is_named:
            continue                      # 进程名就对不上，不可能是它
        path, _ = query_process_detail(wi.pid)
        if want and path and os.path.normcase(os.path.abspath(path)) == want:
            return wi.hwnd                # 路径完全对上：就是它
        if not by_name and is_named:
            by_name = wi.hwnd             # 只有名字对得上：先记着当兜底
    return by_name


def open_item_location(item: ProgramItem) -> tuple[bool, str]:
    """在资源管理器中定位该程序。"""
    target = item.target
    if not target or not os.path.exists(target):
        return False, "目标不存在，无法定位"
    reveal_in_explorer(target)
    return True, f"已定位：{target}"


def catalog_to_csv(items: list[ProgramItem], path: str = "") -> str:
    """把程序目录导出为 CSV（UTF-8 BOM，方便 Excel 直接打开）。"""
    import csv
    if not path:
        import tempfile
        path = os.path.join(tempfile.gettempdir(),
                            f"winmon_programs_{time.strftime('%Y%m%d_%H%M%S')}.csv")
    with open(path, "w", encoding="utf-8-sig", newline="") as fh:
        wr = csv.writer(fh)
        wr.writerow(["名称", "目标", "启动参数", "工作目录", "来源", "发行商",
                     "安装位置", "是否运行中", "PID"])
        for it in items:
            wr.writerow([it.name, it.target, it.args, it.workdir, it.source_label,
                         it.publisher, it.install_location,
                         "是" if it.pid and is_process_alive(it.pid) else "否",
                         it.pid or ""])
    return path
def get_window_title(hwnd: int) -> str:
    """取窗口标题。"""
    try:
        n = user32.GetWindowTextLengthW(w.HWND(hwnd))
        buf = ctypes.create_unicode_buffer(max(2, n + 1))
        user32.GetWindowTextW(w.HWND(hwnd), buf, n + 1)
        return buf.value
    except Exception:
        return ""


# 播放器窗口标题的播放态特征
# 注意：应用**首页**的标题是 "哔哩哔哩 (゜-゜)つロ 干杯~-bilibili"，
# 它同样含有 "bilibili" 字样，所以不能简单用"包含 bilibili"来判断播放态
# （这里曾误判过一次）。真正的播放态是"标题=视频名 + _哔哩哔哩"，例如：
#   【方舟阴间怪】数值怪都容易退环境吗？…_哔哩哔哩_bilibili
_VIDEO_TITLE_SUFFIXES = ("_哔哩哔哩", "_bilibili", "-bilibili.com")

# 明显不是"某个视频"的标题（应用首页 / 空白页等）
_NON_VIDEO_TITLES = ("哔哩哔哩", "bilibili", "")

#: 窗口标题末尾的"应用名尾巴"。浏览器会把自己缀在网页标题后面，必须剥掉，
#: 否则「某视频_哔哩哔哩_bilibili - 个人 - Microsoft Edge」既匹配不上
#: `_VIDEO_TITLE_SUFFIXES`（结尾是 Microsoft Edge 而不是 _哔哩哔哩），
#: 又会被下面的通用兜底排除（标题里含 "bilibili"）—— 两头落空，
#: 于是"浏览器里正在放视频"永远判不出来。当时实测踩到过。
_WINDOW_TAIL_NAMES = ("microsoft edge", "google chrome", "mozilla firefox",
                      "firefox", "edge", "chrome", "internet explorer",
                      "个人", "personal", "私人")

#: 尾巴里出现这些 → 这是个**浏览器**窗口，里面的网页标题不能想当然当成视频
_BROWSER_TAIL_NAMES = ("microsoft edge", "google chrome", "mozilla firefox",
                       "firefox", "edge", "chrome", "internet explorer")

#: 窗口标题里可能混进的**不可见字符**（零宽空格等）。
#: 实测 Edge 的标题里就有 U+200B：`'… - 个人 - Microsoft\u200b Edge'`，
#: 它会让"应用名尾巴"的字符串比较莫名失败 —— 这类字符在排查时**看不见**，
#: 所以只能从原则上先剥掉再比对。
_INVISIBLE_CHARS = "\u200b\u200c\u200d\u2060\ufeff\u00ad\u180e\u200e\u200f"


def _strip_invisible(text: str) -> str:
    """去掉零宽/双向控制等不可见字符（只用于内部匹配，不改显示用原文）。"""
    for ch in _INVISIBLE_CHARS:
        if ch in text:
            text = text.replace(ch, "")
    return text


def _split_app_tail(title: str) -> tuple[str, bool]:
    """
    剥掉标题末尾的应用名尾巴，返回 (剩下的标题, 是否是浏览器窗口)。

    '某视频_哔哩哔哩_bilibili - 个人 - Microsoft Edge'
        -> ('某视频_哔哩哔哩_bilibili', True)
    '无标题 - 记事本'（记事本不在尾巴表里）
        -> ('无标题 - 记事本', False)

    注意：先把**不可见字符**去掉。实测 Edge 的标题是
    `'… - 个人 - Microsoft\\u200b Edge'` —— 里面夹了一个零宽空格（U+200B）！
    不处理它，尾巴就匹配不上，"这是浏览器窗口"的判断随之失效，
    于是浏览器里的普通网页会被当成视频（当时在真环境里踩到）。
    """
    t = _strip_invisible((title or "").strip())
    browser = False
    while True:
        head, sep, last = t.rpartition(" - ")
        if not sep:
            break
        key = last.strip().lower().rstrip(".")
        if key not in _WINDOW_TAIL_NAMES:
            break
        if key in _BROWSER_TAIL_NAMES:
            browser = True
        t = head.strip()
    return t, browser

# 通用标题判定的黑名单：这些是文档/工具类程序的标题特征，不是视频。
#
# 为什么需要精度：这条通用规则本意是"兜住别家播放器"，但浏览器和编辑器窗口
# 也在监视范围内，一旦把「无标题 - 记事本」这种标题判成"正在放视频"，
# 就会在完全没放视频的时候弹出"是否关闭当前视频"，比漏判更烦人。
# 所以这里宁可保守：只认明确的播放页特征，其余一律不认。
_NON_VIDEO_APP_SUFFIXES = (
    "记事本", "notepad", "写字板", "wordpad", "word", "excel", "powerpoint",
    "wps", "画图", "mspaint", "计算器", "calculator", "设置", "settings",
    "任务管理器", "task manager", "visual studio code", "code", "pycharm",
    "sublime text", "notepad++", "终端", "terminal", "powershell", "cmd",
    "命令提示符", "资源管理器", "explorer", "regedit", "注册表编辑器",
    "chrome", "edge", "firefox",
)
# 这些标题前缀是"占位标题"，无论后缀是什么都不算视频
_PLACEHOLDER_TITLE_PREFIXES = ("无标题", "untitled", "新标签页", "new tab", "about:blank")


def video_title_strength(title: str) -> str:
    """
    标题"像在放视频"的强度，返回 "strong" / "weak" / "none"。

    为什么要分强度：这个函数有两个用途，要求不一样 ——
      · 判断"某个已知播放器窗口里是不是在放视频"（`looks_like_video_title`）：
        宽一点没关系，反正对象已经是播放器；
      · 判断"**某个陌生窗口**算不算视频来源"（`detect_video_sources`）：
        必须严，否则任何标题较长的中文窗口都会被算成视频来源。
    实测踩到：某个 AI 客户端的窗口标题「继续开发 winmon 监控工具 — …」
    只有 8 个汉字，就被那条"长中文标题"启发式判成了播放页，
    于是它成了"得分最高的视频来源"，弹窗里还写着"当前标题已是播放页" ——
    如果用户顺手点了「是，关闭窗口」，关掉的就是正在干活的编辑器。
    所以：**"strong" 才算铁证**（播放页后缀 / BV 号），"weak" 只是启发式。
    """
    t, browser_window = _split_app_tail(title)
    if not t:
        return "none"
    low = t.lower()
    # 首页 / 应用自身标题排除
    if low in [x.lower() for x in _NON_VIDEO_TITLES]:
        return "none"
    if low.startswith("哔哩哔哩 (") or low.startswith("bilibili ("):
        return "none"
    if any(low.endswith(sfx) for sfx in _VIDEO_TITLE_SUFFIXES):
        return "strong"                       # 「视频名_哔哩哔哩」这种播放页标准标题
    import re
    if re.search(r"\bBV[0-9A-Za-z]{8,}", t):
        return "strong"                       # 标题里带 BV 号
    if browser_window:
        # 浏览器里的**普通网页**不能算视频。老实现会走到下面的通用兜底，
        # 于是「百度一下，你就知道 - Microsoft Edge」这种长中文标题被误判成
        # "正在放视频"，明明没放视频也会弹"是否关闭当前视频"。
        # 浏览器窗口只认上面的明确播放特征（播放页后缀 / BV 号）。
        return "none"
    # ---- 通用兜底（针对非哔哩哔哩的播放器）----
    # 先排除"明确不是视频"的标题，避免把记事本/浏览器窗口当成播放器。
    if low.startswith(_PLACEHOLDER_TITLE_PREFIXES):
        return "none"
    tail = t.rsplit(" - ", 1)[-1].rsplit(" — ", 1)[-1].strip().lower()
    if tail and any(tail == app or tail.startswith(app) for app in _NON_VIDEO_APP_SUFFIXES):
        return "none"
    # 中文长标题且不含应用名 → 视为"像视频名"，但只是启发式（weak）
    if len(t) >= 8 and "哔哩哔哩" not in t and "bilibili" not in low:
        if len([c for c in t if "\u4e00" <= c <= "\u9fff"]) >= 6:
            return "weak"
    return "none"


def looks_like_video_title(title: str) -> bool:
    """
    判断窗口标题是否像"正在播放某个视频"。

    判据（都要求不是首页标题）：
      · 先剥掉标题末尾的应用名尾巴（`_split_app_tail`），例如
        「某视频_哔哩哔哩_bilibili - 个人 - Microsoft Edge」→ 只看「某视频_哔哩哔哩_bilibili」
      · 以 _哔哩哔哩 / _bilibili / -bilibili.com 结尾 —— 播放页的标准标题格式（strong）
      · 标题里带 BV 号（BV 开头 + 数字）（strong）
      · 中文长标题（>=8 个字符且不含"哔哩哔哩"这种应用名）—— 只对**非浏览器**窗口成立，
        而且只是启发式（weak）；浏览器里的普通网页不能靠这条判成视频
        （否则"百度一下，你就知道 - Microsoft Edge"会被误判，
        然后无缘无故弹"是否关闭当前视频"）

    要区分"铁证"与"启发式"的场景请直接用 `video_title_strength()`。
    """
    return video_title_strength(title) != "none"


class WindowMotionDetector:
    """
    通过 PrintWindow 逐帧比对，检测窗口内"画面是否在持续变化"。

    用途：判断视频是否在播放（播放时画面持续变化；静止页面几乎不变）。
    """

    def __init__(self, hwnd: int, src_w: int, src_h: int,
                 out_w: int = 320, out_h: int = 200) -> None:
        self.hwnd = hwnd
        self.src_w = max(16, src_w)
        self.src_h = max(16, src_h)
        self.out_w = out_w
        self.out_h = out_h
        self._cap = WindowCapturer()
        self._prev: bytes | None = None

    def close(self) -> None:
        self._cap.release()

    def sample(self) -> float:
        """抓一帧并与上一帧比对，返回平均绝对差（0 表示完全没变）。"""
        raw, _ow, _oh = self._cap.grab_window(self.hwnd, self.src_w, self.src_h,
                                              self.out_w, self.out_h)
        if self._prev is None or len(self._prev) != len(raw):
            self._prev = raw
            return 0.0
        prev = self._prev
        self._prev = raw
        total = 0
        n = 0
        # 抽样比较即可，省 CPU
        for k in range(0, len(raw), 11):
            total += abs(raw[k] - prev[k])
            n += 1
        return total / max(1, n)

    def measure(self, seconds: float = 2.0, interval: float = 0.35) -> tuple[float, list[float]]:
        """在给定时间内连续采样，返回 (平均差异, 每帧差异列表)。"""
        series: list[float] = []
        t0 = time.monotonic()
        while time.monotonic() - t0 < seconds:
            series.append(self.sample())
            time.sleep(interval)
        if not series:
            return 0.0, []
        # 第一帧没有参照，差异必然是 0，统计时去掉
        body = series[1:] if len(series) > 1 else series
        return sum(body) / max(1, len(body)), series


class VideoActivityDetector:
    """
    综合判断某个窗口里"是否有视频在播放"。

    判定依据（按可靠性排序）：
      1) 窗口标题是否像播放中的视频（哔哩哔哩会设成视频名）
      2) 画面是否在持续变化（连续多帧都有明显差异）
      3) 该进程是否有活跃音频会话（有声音基本可以确定在播放）
    任一强信号成立即判定为播放中；只有画面轻微变化（如鼠标悬停动效）不算。
    """

    MOTION_PLAY_THRESHOLD = 1.5      # 平均差异超过它认为"画面明显在动"
    MOTION_STRONG = 3.0              # 强信号阈值

    def __init__(self, wi: "WinInfo") -> None:
        self.wi = wi
        self.detector = WindowMotionDetector(wi.hwnd, wi.rect_w, wi.rect_h)

    def close(self) -> None:
        self.detector.close()

    def is_playing(self, motion_seconds: float = 2.0) -> tuple[bool, dict]:
        """
        返回 `(是否在播放, 判定依据明细)`。

        判定只看两件事：**窗口标题** + **画面是否持续变化**。
        2026-10-13 删掉了第三条"该进程有没有音频会话"——
        实测那个 API 在本机恒返回 0 个会话（连自己造的出声源也是 0），
        等于一条永远不成立的证据，留着反而误导（决定重写后再议，见 HANDOFF）。
        """
        title = get_window_title(self.wi.hwnd)
        title_hit = looks_like_video_title(title)
        avg, series = self.detector.measure(motion_seconds)

        # 连续帧都在动才算"持续变化"（避免把单次重绘当成播放）
        moving_frames = sum(1 for v in series if v > 0.6)
        sustained = moving_frames >= max(2, len(series) // 2)

        playing = False
        reason = []
        if avg >= self.MOTION_STRONG and sustained:
            playing = True
            reason.append(f"画面持续变化(平均{avg:.2f})")
        if title_hit and avg >= self.MOTION_PLAY_THRESHOLD:
            playing = True
            reason.append("标题为视频名且画面在动")
        if not playing:
            if title_hit:
                reason.append("标题像视频，但画面静止")
            elif avg > 0.6:
                reason.append(f"画面轻微变化({avg:.2f})，不足以判定播放")
            else:
                reason.append("画面静止")
        # 注意：返回里**不再有 `audio_peak`**（那条信号 2026-10-13 已删，见 `is_playing`
        # 的说明）—— 判定依据只剩标题与画面变化，明细里也就只报这两样。
        return playing, {"title": title, "title_hit": title_hit, "avg_diff": avg,
                         "series": series, "reason": "；".join(reason)}


# ============================================================================
# 八点五、UI Automation：读取窗口内的真实元素（不依赖任何第三方库）
# ============================================================================
#
# 为什么需要它（当时实测得到的结论）：
#
# 1) 原方案靠"按 Tab N 格 + 回车"来打开视频，格数是猜的。实测首页有 10 个视频卡片，
#    光靠 Tab 计数无法知道"第 3 个"到底是哪个视频，也无法向用户确认。
#    UIA 能直接读出每个卡片的**标题 + 播放量 + 弹幕数 + 时长**，
#    于是"打开第一排第三个"可以变成"打开标题为 X 的那个"，可复核、可回显。
#
# 2) HANDOFF 里曾记录"必须给目标进程加 --force-renderer-accessibility 才暴露无障碍树"。
#    当时实测该结论**不成立**：哔哩哔哩（Electron）默认状态下
#    `FindAll(Descendants)` 就能拿到 221 个元素，视频卡片信息完整。
#    原因是 Windows 的 UIA 在客户端查询时会促使 Chromium 按需启用无障碍支持。
#
# 3) 重要限制（实测）：UIA 能**读**，但**不能**替应用完成点击 ——
#    视频卡片的 `InvokePattern.Invoke()` 与 `LegacyIAccessible.DoDefaultAction()`
#    都返回 S_OK/E_FAIL 但页面毫无变化；`SetFocus` 直接返回 E_FAIL。
#    所以 UIA 在本项目里的正确定位是"精确识别 + 校验"，
#    真正的"点击"仍需真实用户操作（或可用的输入注入环境）。
#
# 实现方式：只加载 Windows 自带的 uiautomationcore.dll，用 ctypes 按 vtable 下标调用。
# 不引入 comtypes/pywinauto/uiautomation 等第三方库 —— 零依赖是项目的硬约定。

_UIA_CLSID = "{FF48DBA4-60EF-4201-AA87-54103EEF594E}"
_UIA_IID = "{30CBE57D-D9D0-452A-AB13-7AC5AC4825EE}"

# IUIAutomation 的 vtable 下标（IUnknown 占 0..2）
_UIA_VT_ELEMENT_FROM_HANDLE = 6
_UIA_VT_CREATE_TRUE_CONDITION = 21
# IUIAutomationElement 的 vtable 下标
_UIA_EL_FIND_ALL = 6
_UIA_EL_GET_CURRENT_PROPERTY_VALUE = 10
_UIA_EL_GET_CURRENT_PATTERN = 16
# IUIAutomationElementArray
_UIA_ARR_LENGTH = 3
_UIA_ARR_GET = 4

UIA_NamePropertyId = 30005
UIA_BoundingRectanglePropertyId = 30001
UIA_IsOffscreenPropertyId = 30022
UIA_TreeScope_Descendants = 0x4
UIA_InvokePatternId = 10000
#: ValuePattern：实测能读出 Hyperlink 的 href（哔哩哔哩视频卡片的真实链接）
UIA_ValuePatternId = 10002

#: 视频卡片标题的形态：`标题 [标记] 播放量 弹幕数 时长`（哔哩哔哩首页/推荐页的卡片名）。
#:
#: 实测这个"名字"并不稳定：悬停/显示过投屏按钮的卡片会变成
#: `标题 哔哩哔哩播放器 23.2万 2142 20:50` —— 中间多出一段标记，
#: 于是"播放量紧跟标题"的严格写法会**漏掉整张卡片**（实测踩到）。
#:
#: 注意别用 `.+?` 之类的宽松后缀：那样 `带空格的 标题 也有 5.6万 139 08:12`
#: 的标题会被截成 `带空格的 标题`（"也有"被当成标记吃掉）——实测踩过这个反例。
#: 所以这里只允许**已知的标记词**。
_VIDEO_CARD_MARKERS = ("哔哩哔哩播放器", "播放器", "广告")
VIDEO_CARD_RE = (r"^(?P<title>.+?)\s+(?:(?:"
                 + "|".join(_VIDEO_CARD_MARKERS)
                 + r")\s+)?(?P<views>\d+(?:\.\d+)?[万亿]?)\s+"
                 r"(?P<danmu>\d+)\s+"
                 r"(?P<dur>\d{1,2}:\d{2}(?::\d{2})?)$")
def _uia_guid(guid_str: str):
    import uuid

    class GUID(ctypes.Structure):
        _fields_ = [("d1", w.DWORD), ("d2", w.WORD), ("d3", w.WORD),
                    ("d4", ctypes.c_ubyte * 8)]
    g = GUID()
    u = uuid.UUID(guid_str)
    g.d1, g.d2, g.d3 = u.time_low, u.time_mid, u.time_hi_version
    g.d4 = (ctypes.c_ubyte * 8)(*u.bytes[8:])
    return g


def _com_release(ptr) -> None:
    """
    Release 一个 COM 接口指针（`IUnknown::Release` 是 vtable 下标 2）。

    为什么必须做（2026-10-13 修的真 bug）：UIA 的**每个出参**都是引用计数的
    —— 根元素、条件、元素数组、数组里每个元素、Pattern 接口。以前一个都没 Release，
    于是每次「监视当前窗口」都会泄漏上千个代理对象，而且那些引用会**跨进程**
    一直揪着目标程序的无障碍树不放（对方也会跟着多占内存）。
    重复 Release / 传空指针都是安全的（这里先判空）。
    """
    try:
        if ptr is None:
            return
        value = ptr.value if isinstance(ptr, ctypes.c_void_p) else int(ptr)
        if not value:
            return
        live = ctypes.c_void_p(value)
        vtbl = ctypes.cast(live, ctypes.POINTER(
            ctypes.POINTER(ctypes.c_void_p))).contents
        fn = ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)(vtbl[2])
        fn(live)
    except Exception:
        pass


class UiaClient:
    """
    极简 UI Automation 客户端（纯 ctypes，只用到本项目需要的那几个方法）。

    所有接口指针都用 `c_void_p`，按 vtable 下标取函数地址 —— 这样不用定义
    完整的接口结构体，也不依赖 comtypes。任何一步失败都返回空结果而不抛异常，
    因为 UIA 只是"增强信号"，不能让主流程崩。

    **用完要收**：这个对象持有 `IUIAutomation`，并且 `CoInitializeEx` 过；
    `elements()` 交出来的元素引用由**调用方**负责释放。推荐写法：

        with UiaClient() as uia:
            for el in uia.elements(hwnd):
                ...
             （`release_elements()` 由 `names()`/`try_invoke()` 内部处理；
               自己调 `elements()` 时用 `uia.release_elements(els)`）
    """

    def __init__(self) -> None:
        self._uia = ctypes.c_void_p()
        self._ole32 = None
        self._oleaut = None
        self.available = False
        self.error = ""
        #: `CoInitializeEx` 是不是**我们**做的（只有 S_OK/S_FALSE 才需要配对释放）
        self._co_initialized = False
        try:
            self._oleaut = ctypes.WinDLL("oleaut32")
            ctypes.WinDLL("uiautomationcore")           # 确认组件存在
            self._ole32 = ctypes.WinDLL("ole32")
            hr_init = self._ole32.CoInitializeEx(None, 2)   # 2 = COINIT_APARTMENTTHREADED
            self._co_initialized = int(hr_init or 0) in (0, 1)   # S_OK / S_FALSE
            hr = self._ole32.CoCreateInstance(
                ctypes.byref(_uia_guid(_UIA_CLSID)), None, 1,
                ctypes.byref(_uia_guid(_UIA_IID)), ctypes.byref(self._uia))
            if hr != 0 or not self._uia.value:
                self.error = f"CoCreateInstance(CUIAutomation) hr={hr}"
                _com_release(self._uia)
                self._uia = ctypes.c_void_p()
            else:
                self.available = True
        except Exception as exc:
            self.error = f"{type(exc).__name__}: {exc}"

    # -- 生命周期 --

    def release(self) -> None:
        """收掉 `IUIAutomation` 与 `CoInitializeEx`（可重复调用，安全）。"""
        _com_release(self._uia)
        self._uia = ctypes.c_void_p()
        self.available = False
        if self._co_initialized and self._ole32 is not None:
            try:
                self._ole32.CoUninitialize()
            except Exception:
                pass
        self._co_initialized = False

    @staticmethod
    def release_elements(elements) -> None:
        """释放 `elements()` 交出来的那批元素引用（调用方的责任，见类说明）。"""
        for el in elements or []:
            _com_release(el)

    def __enter__(self) -> "UiaClient":
        return self

    def __exit__(self, *_exc) -> None:
        self.release()

    # -- 低层调用 --
    @staticmethod
    def _vt(ptr, index, restype, *argtypes):
        vtbl = ctypes.cast(ptr, ctypes.POINTER(
            ctypes.POINTER(ctypes.c_void_p))).contents
        return ctypes.WINFUNCTYPE(restype, ctypes.c_void_p, *argtypes)(vtbl[index])

    def _variant(self, element, prop_id):
        var = (ctypes.c_byte * 32)()
        ctypes.memset(var, 0, 32)
        self._vt(element, _UIA_EL_GET_CURRENT_PROPERTY_VALUE, ctypes.c_long,
                 ctypes.c_int, ctypes.c_void_p)(element, prop_id, var)
        return var

    def _prop_str(self, element, prop_id) -> str:
        var = self._variant(element, prop_id)
        try:
            addr = ctypes.addressof(var)
            if ctypes.c_ushort.from_address(addr).value == 8:          # VT_BSTR
                bstr = ctypes.c_void_p.from_address(addr + 8).value
                if not bstr:
                    return ""
                return ctypes.wstring_at(bstr) or ""
            return ""
        finally:
            # 该谁释放谁释放：BSTR 由 VariantClear 统一回收（别自己 SysFreeString，
            # 否则 VARIANT 里还留着那个指针，再清一次就是二次释放）
            self._variant_clear(var)

    def _variant_clear(self, var) -> None:
        """`VariantClear`：把 VARIANT 里持有的 BSTR / SAFEARRAY 释放掉。"""
        try:
            self._oleaut.VariantClear(ctypes.byref(var))
        except Exception:
            pass

    def _prop_rect(self, element):
        var = self._variant(element, UIA_BoundingRectanglePropertyId)
        try:
            addr = ctypes.addressof(var)
            if ctypes.c_ushort.from_address(addr).value == 0x2005:     # VT_ARRAY|VT_R8
                psa = ctypes.c_void_p.from_address(addr + 8).value
                if not psa:
                    return None
                # SAFEARRAY: cDims(2) fFeatures(2) cbElements(4) cLocks(4) pvData(8)
                pv = ctypes.c_void_p.from_address(psa + 16).value
                if not pv:
                    return None
                return tuple((ctypes.c_double * 4).from_address(pv))   # 取出来就是副本
            return None
        finally:
            self._variant_clear(var)

    def element_url(self, element) -> str:
        """
        读出元素的链接地址（UIA 的 ValuePattern::CurrentValue）。

        实测：哔哩哔哩首页的**视频卡片是 Hyperlink**，`ValuePattern.CurrentValue`
        就是它的 href（形如 `https://www.bilibili.com/video/BV1s9RrBfEGp?...`）。
        这是当时最重要的发现之一 —— 有了 URL，
        "打开指定视频"就不再依赖"点击"（点击在本环境被拦），
        可以交给系统/浏览器直接打开。
        """
        handle = ctypes.c_void_p()
        try:
            hr = self._vt(element, _UIA_EL_GET_CURRENT_PATTERN, ctypes.c_long,
                          ctypes.c_int, ctypes.POINTER(ctypes.c_void_p))(
                element, UIA_ValuePatternId, ctypes.byref(handle))
            if hr != 0 or not handle.value:
                return ""
            bstr = ctypes.c_void_p()
            hr2 = self._vt(handle, 4, ctypes.c_long,          # get_CurrentValue
                           ctypes.POINTER(ctypes.c_void_p))(handle, ctypes.byref(bstr))
            if hr2 != 0 or not bstr.value:
                return ""
            text = ctypes.wstring_at(bstr.value)
            try:
                self._oleaut.SysFreeString(bstr)
            except Exception:
                pass
            return text or ""
        except Exception:
            return ""
        finally:
            _com_release(handle)          # Pattern 接口也是引用计数的，用完就还

    def elements(self, hwnd: int) -> list:
        """
        返回窗口内的全部 UIA 元素（失败返回空列表）。

        **调用方负责释放这批元素**（`release_elements()`）—— 它们是引用计数的；
        根元素 / 条件 / 元素数组这三个中间对象由本方法自己在 `finally` 里还掉。
        """
        if not self.available:
            return []
        root = ctypes.c_void_p()
        cond = ctypes.c_void_p()
        arr = ctypes.c_void_p()
        try:
            self._vt(self._uia, _UIA_VT_ELEMENT_FROM_HANDLE, ctypes.c_long,
                     ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p))(
                self._uia, w.HWND(hwnd), ctypes.byref(root))
            if not root.value:
                return []
            self._vt(self._uia, _UIA_VT_CREATE_TRUE_CONDITION, ctypes.c_long,
                     ctypes.POINTER(ctypes.c_void_p))(self._uia, ctypes.byref(cond))
            self._vt(root, _UIA_EL_FIND_ALL, ctypes.c_long, ctypes.c_int,
                     ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p))(
                root, UIA_TreeScope_Descendants, cond, ctypes.byref(arr))
            if not arr.value:
                return []
            count = ctypes.c_int(0)
            self._vt(arr, _UIA_ARR_LENGTH, ctypes.c_long,
                     ctypes.POINTER(ctypes.c_int))(arr, ctypes.byref(count))
            out = []
            for i in range(count.value):
                el = ctypes.c_void_p()
                self._vt(arr, _UIA_ARR_GET, ctypes.c_long, ctypes.c_int,
                         ctypes.POINTER(ctypes.c_void_p))(arr, i, ctypes.byref(el))
                if el.value:
                    out.append(el)          # GetElement 会给一份引用，归调用方
            return out
        except Exception:
            return []
        finally:
            # 中间对象用完立刻还 —— 数组还掉不会影响上面已经取出来的元素引用
            _com_release(arr)
            _com_release(cond)
            _com_release(root)

    # -- 上层功能 --
    def names(self, hwnd: int) -> list[str]:
        """窗口内所有元素的名字（去空）。"""
        els = self.elements(hwnd)
        try:
            return [n for n in (self._prop_str(el, UIA_NamePropertyId)
                                for el in els) if n]
        finally:
            self.release_elements(els)
    def try_invoke(self, hwnd: int, title_substr: str) -> tuple[bool, str]:
        """
        尝试对标题匹配的卡片调用 UIA 的 Invoke。

        **注意（实测结论）**：在哔哩哔哩桌面端上，即使返回 S_OK，
        页面也不会真的跳转 —— 应用的无障碍层只是"记录"了这次调用。
        所以这个函数返回 True 只代表"模式调用成功"，
        调用方必须自行校验页面是否真的变了（例如比对窗口标题或画面）。

        保留这个能力是因为：不同应用/版本的行为不一样，
        一旦某个目标支持，这就是最干净的"无鼠标"操作方式。

        匹配策略：优先匹配"卡片容器"（名字里带播放量与时长），
        因为页面里同一个标题往往还有更短的重复元素（标题文本、作者行等），
        直接匹配短元素会命中错的那个。
        """
        if not self.available:
            return False, self.error or "UIA 不可用"
        needle = (title_substr or "").strip().lower()
        if not needle:
            return False, "没有给出标题关键词"
        import re
        card_pattern = re.compile(VIDEO_CARD_RE)
        els = self.elements(hwnd)
        handle = ctypes.c_void_p()
        try:
            card = None
            fallback = None
            for el in els:
                name = self._prop_str(el, UIA_NamePropertyId)
                if not name or needle not in name.lower():
                    continue
                if card_pattern.match(name.strip()):
                    card = (el, name)          # 卡片容器最合适：找到就直接用它
                    break
                if fallback is None:
                    fallback = (el, name)      # 先记着，但**继续往后找**更好的
            picked = card or fallback
            if picked is None:
                return False, f"没有找到标题包含「{title_substr}」的元素"
            el, name = picked
            hr = self._vt(el, _UIA_EL_GET_CURRENT_PATTERN, ctypes.c_long,
                          ctypes.c_int, ctypes.POINTER(ctypes.c_void_p))(
                el, UIA_InvokePatternId, ctypes.byref(handle))
            if hr != 0 or not handle.value:
                return False, "该元素不支持 Invoke 模式"
            hr2 = self._vt(handle, 3, ctypes.c_long)(handle)          # Invoke()
            if hr2 != 0:
                return False, f"Invoke 调用失败 hr={hr2}"
            return True, f"已请求应用激活「{name[:30]}」（需自行校验是否生效）"
        finally:
            # 元素与 Pattern 接口都是引用计数的：这个函数借了就一定要还
            _com_release(handle)
            self.release_elements(els)


class WindowKeySender:
    """向指定窗口发送键盘/鼠标输入（用于操作播放器窗口）。"""

    VK_TAB = 0x09
    VK_RETURN = 0x0D
    VK_ESCAPE = 0x1B
    VK_SPACE = 0x20
    VK_LEFT = 0x25
    VK_RIGHT = 0x27
    VK_HOME = 0x24
    KEYEVENTF_KEYUP = 0x0002
    VK_CONTROL = 0x11
    VK_W = 0x57

    #: Windows 消息方式的按键（见 docstring 里的"消息通路"说明）
    WM_KEYDOWN = 0x0100
    WM_KEYUP = 0x0101
    WM_CHAR = 0x0102

    def __init__(self, hwnd: int) -> None:
        self.hwnd = hwnd

    def render_widget(self) -> int:
        """
        找到 Chromium/Electron 的渲染子窗口（Chrome_RenderWidgetHostHWND）。

        为什么要它：当时实测发现**输入注入（SendInput/keybd_event）在受限环境里
        到不了其他进程**，而 `PostMessage(WM_KEYDOWN)` 发给这个渲染窗口时
        Chromium 会处理（实测 PageDown 能真实滚动哔哩哔哩页面，UIA 卡片矩形随之变化）。
        找不到时退回主窗口。
        """
        found = []

        def walk(h, depth=0):
            kids = []

            @WNDENUMPROC
            def cb(ch, _l):
                kids.append(H(ch))
                return True
            user32.EnumChildWindows(w.HWND(h), cb, 0)
            for k in kids:
                buf = ctypes.create_unicode_buffer(256)
                user32.GetClassNameW(w.HWND(k), buf, 256)
                if "RenderWidgetHost" in buf.value:
                    found.append(k)
                if depth < 4:
                    walk(k, depth + 1)
        try:
            walk(self.hwnd)
        except Exception:
            pass
        return found[0] if found else self.hwnd

    def key_message(self, vk: int, target: int = 0, hold: float = 0.05) -> None:
        """
        用**窗口消息**发一次按键（WM_KEYDOWN + WM_KEYUP）。

        与 `key()`（keybd_event）的区别：这条通路不经过系统输入队列，
        因此在"输入注入被限制"的环境里仍可能生效（实测对 Chromium 有效）。
        注意它**不保证**被应用接受：合成消息不是真实输入，
        很多前端框架会忽略合成的点击/激活（实测哔哩哔哩的卡片就不会被打开）。
        """
        hwnd = w.HWND(target or self.render_widget())
        user32.PostMessageW(hwnd, self.WM_KEYDOWN, vk, 0x001C0001)
        time.sleep(hold)
        user32.PostMessageW(hwnd, self.WM_KEYUP, vk, 0xC01C0001)
        time.sleep(0.12)

    def type_message(self, text: str, target: int = 0) -> None:
        """用 WM_CHAR 发送文本（实测可到达其他进程的编辑框）。"""
        hwnd = w.HWND(target or self.render_widget())
        for ch in text:
            user32.PostMessageW(hwnd, self.WM_CHAR, ord(ch), 0)
            time.sleep(0.03)

    def focus(self) -> bool:
        """
        把窗口切到前台并激活，否则键盘事件不会生效。

        实现全在 `activate_window()` 里（AttachThreadInput 组合拳 + 回读核对 +
        失败重试），这里只是它的薄包装 —— 以前这段逻辑在这里各写了一份，
        列表单击/双击也要用同一套，所以抽出去共用。
        `settle=0.4` 是历史值：键盘注入前多等一会儿更稳（见 HANDOFF 输入通路那节）。
        """
        return activate_window(self.hwnd, retries=3, settle=0.4)[0]

    def key(self, vk: int, hold: float = 0.05) -> None:
        user32.keybd_event(vk, 0, 0, None)
        time.sleep(hold)
        user32.keybd_event(vk, 0, self.KEYEVENTF_KEYUP, None)
        time.sleep(0.12)

    def combo(self, mod_vk: int, vk: int) -> None:
        user32.keybd_event(mod_vk, 0, 0, None)
        time.sleep(0.05)
        self.key(vk)
        user32.keybd_event(mod_vk, 0, self.KEYEVENTF_KEYUP, None)
        time.sleep(0.12)


# ============================================================================
# 三之六、媒体键（"在 WinMon 里切歌 / 暂停"）
# ============================================================================
#
# 为什么用**媒体键**而不是去点播放器界面上的按钮：
#   Windows 把 `VK_MEDIA_*` 这几个虚拟键直接交给**当前拥有媒体会话的应用**
#   —— 网易云、QQ音乐、Spotify、以及浏览器（YouTube / 哔哩哔哩网页版都会注册
#   媒体会话，这就是键盘上多媒体键能切网页歌的原因）。所以 WinMon 只要发一次
#   媒体键，就等于用户按了键盘上的那个键，**不需要知道界面长什么样、
#   也不需要知道按钮在哪个坐标**。
#
# 为什么不去猜按钮坐标（像素/模板匹配）：那种做法随着网站改版、主题、缩放、
# 广告弹层立刻失效，而且"点错了"的后果不可预知（可能点到删除、关注）。
# 媒体会话是应用**主动发布**的稳定接口，属于系统级约定。
#
# 注意：媒体键是**全局**的 —— 它作用于"当前活跃的媒体会话"，
# 不是 WinMon 列表里选中的那一行。所以界面上的说明必须写清楚这一点，
# 不能让用户以为"选中的是 A 却在控制 B"。

#: 允许发送的媒体键（白名单：只这几个，别的 VK 一律拒绝）。
#: 做成白名单而不是"传什么发什么"，是为了让"这个函数只干切歌这一件事"
#: 成为一条能被测试钉住的规则。
MEDIA_KEYS: tuple[tuple[str, str, int], ...] = (
    ("prev", "上一首", 0xB1),          # VK_MEDIA_PREV_TRACK
    ("play_pause", "播放/暂停", 0xB3),  # VK_MEDIA_PLAY_PAUSE
    ("next", "下一首", 0xB0),          # VK_MEDIA_NEXT_TRACK
)

#: 媒体**会话命令**（发给某个具体会话）→ 给用户看的中文名。
#: 与上面那排"媒体键"区分开：那是键盘注入、作用于当前活跃的播放器。
MEDIA_COMMAND_LABEL = {
    "play": "播放", "pause": "暂停", "next": "下一首", "previous": "上一首",
}

#: slug -> VK
MEDIA_KEY_VK = {slug: vk for slug, _label, vk in MEDIA_KEYS}
#: slug -> 中文标签
MEDIA_KEY_LABEL = {slug: label for slug, label, _vk in MEDIA_KEYS}

KEYEVENTF_KEYUP = 0x0002


#: 媒体键 → 对应的**会话命令**（先走这条：实测它才可靠，见 `smart_media_command`）。
#: `play_pause` 例外：它是**切换**，要按当前状态决定发 play 还是 pause
#: （写成固定的 "play" 就错了 —— 正在放的时候发 play 当然一动不动，当时实测踩到）。
MEDIA_KEY_TO_COMMAND = {"next": "next", "prev": "previous"}


def media_session_snapshot() -> dict:
    """
    读一次"当前活跃会话"的快照：`{"app","title","status"}`（读不到就全空）。

    用途：`smart_media_command()` 用它做**发命令前后的对比** ——
    只有状态（或曲目）真的变了，才算"生效"。
    """
    got = collect_media_sessions()
    rows = [s for s in (got.get("sessions") or []) if isinstance(s, dict)]
    if not rows:
        return {"app": "", "title": "", "status": -1}
    # 优先"正在播放"的那个，否则第一个
    pick = next((s for s in rows if int(s.get("status") or 0) == 4), rows[0])
    return {"app": str(pick.get("app") or ""), "title": str(pick.get("title") or ""),
            "status": int(pick.get("status") or 0)}


def session_app_ids(rows) -> list[str]:
    """
    会话行 → **去重后的 app id 列表**（正在播放的排前面）。**纯逻辑**，不碰系统。

    与 `media_session_app_ids()` 的区别：这个吃**已经读到的那份行**。
    界面本来每 4 秒就有一份（`collect_media_sessions()`），算"有哪些媒体应用"
    时不该为了同一件事再付一次 290ms（起一次 PowerShell）。
    坏行（不是 dict / status 不是数字）一律跳过，绝不抛异常。
    """
    def _status(row) -> int:
        try:
            return int(row.get("status") or 0)
        except (TypeError, ValueError):
            return 0

    def _play_order(row) -> tuple:
        """
        排序键：**正在播放的排最前面**，其余按状态数值倒序。

        为什么不能直接写 `-status`（2026-10-16 发现的老 bug）：状态是
        `4 = 正在播放 / 5 = 已暂停`，倒序会把**已暂停**排在**正在播放**前面 ——
        与"正在播放的排前面"这句承诺正好相反。这条顺序现在真的会被用户看到
        （下拉框里会话条目就按它排），所以先按"是不是 4"分组。
        """
        status = _status(row)
        return (0 if status == 4 else 1, -status)

    clean = [r for r in (rows or []) if isinstance(r, dict)]
    ids: list[str] = []
    for row in sorted(clean, key=_play_order):
        app = str(row.get("app") or "").strip()
        if app and app not in ids:
            ids.append(app)
    return ids


def media_session_app_ids() -> list[str]:
    """
    现在**有哪些会话**（按 app id 去重，正在播放的排前面）。

    用途：`smart_media_command()` 逐个会话试命令（A.3）—— 第一个会话
    "没有下一首"时，可以接着试下一个播放器，而不是直接报失败。
    读不到就返回空列表（调用方会退回"不指定应用"的那次调用）。

    自己读一次会话；手上已经有会话行时请直接用 `session_app_ids()`。
    """
    got = collect_media_sessions()
    return session_app_ids(got.get("sessions") or [])


def smart_media_command(key: str, timeout: float = 8.0,
                        verify_seconds: float = 3.0) -> tuple[bool, str]:
    """
    「切歌 / 播放暂停」的**首选实现**：先按会话发命令，再按键兜底，最后**回读核对**。

    ## 为什么改成这样（2026-10-12 实测）

    | 做法 | 实测结果 |
    | --- | --- |
    | `keybd_event(0xB3)` （原来的实现） | 连发两次，会话状态**一动不动** → **没生效** |
    | GSMTC 会话命令 `TryPauseAsync/TryPlayAsync` | pause 后变"已暂停"、play 后变"正在播放" → **生效** |

    所以键盘注入这条路在有些机器/环境下是**投递成功但没人理**（`keybd_event` 本身
    不报错），而走系统媒体会话 API 是**真的能控制播放器**的。

    ## 三步（每一步都如实回报，不假装成功）

      1. 先记一份会话快照（状态 + 曲名）；
      2. 对**当前活跃会话**发对应命令（`play` / `next` / `previous`）；
      3. **回读核对**：状态或曲名变了才算生效；没变就退回键盘媒体键，
         并把"按键也未必有人理"这件事**写在返回值里**（而不是笼统说"已发送"）。

    返回 `(是否确认生效, 说明)`。注意第 3 步里"按键"这一路**无法核对**
    （系统不给回执），所以那时返回的文案会明确说"未确认"。
    """
    slug = str(key or "").strip().lower()
    if slug not in MEDIA_KEY_VK:
        return False, f"不认识的媒体键：{slug!r}（只支持 {'/'.join(MEDIA_KEY_VK)}）"
    label = MEDIA_KEY_LABEL.get(slug, slug)
    before = media_session_snapshot()
    if before["status"] < 0:
        # 一个会话都没有：没有可控制的对象，按键也几乎肯定没人理 —— 如实说
        _, msg = send_media_key(slug)
        return False, (f"{msg}；但**当前没有任何程序发布媒体会话**，"
                       f"大概率不会有人响应（请先让播放器开始播放）")

    cmd = MEDIA_KEY_TO_COMMAND.get(slug)
    if slug == "play_pause":
        # 切换：正在放 → 暂停；否则 → 播放。**不能写死成 play**
        # （正在播放时发 play 是空操作，实测会让核对判定成"没生效"）
        cmd = "pause" if before["status"] == 4 else "play"
    if cmd:
        # **逐个会话试**（A.3）：选中的应用可能有多个会话，或者第一个会话
        # 压根没有下一首 —— 那就换下一个会话试，而不是直接报"没变化"。
        tried = []
        for app_id in media_session_app_ids():
            core_ok, _m = media_command(cmd, app_id=app_id, timeout=timeout)
            if not core_ok:
                continue
            tried.append(app_id)
            deadline = time.monotonic() + max(0.5, verify_seconds)
            while time.monotonic() < deadline:
                time.sleep(0.4)
                now = media_session_snapshot()
                changed = (now["status"] != before["status"]
                           or (now["title"] and before["title"]
                               and now["title"] != before["title"]))
                if changed:
                    state = MEDIA_STATUS_NAMES.get(now["status"], "?")
                    extra = f"（试了 {len(tried)} 个会话）" if len(tried) > 1 else ""
                    return True, (f"{label}已生效（{now['app'] or '当前播放器'}："
                                  f"{state}）{extra}")
            # 这个会话没反应 → 继续下一个
        # 所有会话都试过、都没变化 —— 别糊弄，退回按键并说明**常见原因**
        _, msg = send_media_key(slug)
        if slug in ("next", "prev"):
            guess = ("（可能每个播放列表里都没有下一首/上一首，这就是正常的）"
                     if len(tried) > 1 else
                     "（如果当前播放列表里没有下一首/上一首，这就是正常的）")
        else:
            guess = "（有些播放器不接受会话命令，这种情况只能用它的界面按钮）"
        who = f"试过 {len(tried)} 个会话都没反应；" if len(tried) > 1 else ""
        return False, (f"{who}会话命令被接受但状态没变化{guess}；"
                       f"已改发键盘媒体键（{msg}）—— 这一路无法核对是否生效")
    _, msg = send_media_key(slug)
    return False, msg


def send_media_key(slug: str) -> tuple[bool, str]:
    """
    发一次**键盘媒体键**（`keybd_event`）—— 现在只是**兜底**手段。

    2026-10-12 实测：这条路"投递成功但没人理"是可能的（`keybd_event` 不报错，
    播放器却完全没反应），所以切歌/暂停的**首选**是 `smart_media_command()`
    （走系统媒体会话 API，能回读核对）。

    **如实报告**：这里只能确认"按键已经交给系统"，**无法**确认目标程序真的换了歌
    —— 系统不提供回执。所以返回的说明写的是"已发送"，不是"已切换"。
    """
    slug = str(slug or "").strip().lower()
    vk = MEDIA_KEY_VK.get(slug)
    if vk is None:
        return False, f"不认识的媒体键：{slug!r}（只支持 {'/'.join(MEDIA_KEY_VK)}）"
    try:
        user32.keybd_event(vk, 0, 0, None)
        user32.keybd_event(vk, 0, KEYEVENTF_KEYUP, None)
    except Exception as exc:
        return False, f"发送失败：{type(exc).__name__}: {exc}"
    return True, f"已发送「{MEDIA_KEY_LABEL[slug]}」媒体键"


# ============================================================================
# 三之八、开机自启动 + 托盘（设置界面用）
# ============================================================================
#
# 约定（当时）：「winmon 设计一个独立的设置 UI，设置里加入开机自启动选项
# 和关闭改为隐藏至最小托盘或结束进程，默认为后者」。
#
# 两件事都在这一节里：
#   · 开机自启动 = 往 `HKCU\...\CurrentVersion\Run` 写一个值（写到 **HKCU**，
#     不需要管理员权限；HKLM 那个要提权，不该由监控工具偷偷做）。
#   · 托盘 = `Shell_NotifyIcon` + 一个隐藏的消息窗口（纯 ctypes，零依赖）。
#
# 自启动这条**有副作用**，所以照铁律 2 来：写完**回读核对**，把读到的原样回报，
# 而不是"点一下就报成功"。

#: 自启动注册表位置（HKCU，不需要管理员）
AUTOSTART_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
#: 值的名字（用户能在"任务管理器 → 启动"里按这个名字认出来）
AUTOSTART_NAME = "WinMon"


def autostart_command() -> str:
    """
    开机要执行的命令行。

    · 打包成 exe（`sys.frozen`）→ 直接写 exe 路径；
    · 以 `.py` 跑 → 用 `pythonw.exe`（**没有控制台窗口**）+ 脚本路径，
      并且把路径用引号包起来（路径里有空格也不会被拆开）。
    """
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}"'
    exe = sys.executable or "pythonw.exe"
    if exe.lower().endswith("python.exe"):
        cand = exe[:-len("python.exe")] + "pythonw.exe"
        if os.path.exists(cand):
            exe = cand
    script = os.path.join(APP_DIR, "winmon.py")
    return f'"{exe}" "{script}"'


def get_autostart() -> str:
    """
    读回"当前开机自启动"里登记的命令行；没有就返回空串。

    为什么要有"读回"这一步：勾选框必须反映**注册表里真实的状态**，
    而不是我们上次点了什么 —— 用户可能在任务管理器里禁用过它。
    """
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, AUTOSTART_KEY) as k:
            value, _type = winreg.QueryValueEx(k, AUTOSTART_NAME)
        return str(value or "")
    except FileNotFoundError:
        return ""
    except OSError:
        return ""


def set_autostart(enabled: bool) -> tuple[bool, str]:
    """
    打开/关闭开机自启动，**写完回读核对**。

    返回 `(是否真的处于期望状态, 说明)`。失败时说明里带原因，
    界面照抄即可 —— 不静默失败、不假报成功。
    """
    want = autostart_command()
    try:
        import winreg
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, AUTOSTART_KEY, 0,
                                winreg.KEY_SET_VALUE) as k:
            if enabled:
                winreg.SetValueEx(k, AUTOSTART_NAME, 0, winreg.REG_SZ, want)
            else:
                try:
                    winreg.DeleteValue(k, AUTOSTART_NAME)
                except FileNotFoundError:
                    pass
    except OSError as exc:
        return False, f"写注册表失败：{type(exc).__name__}: {exc}"

    got = get_autostart()
    if enabled:
        if got == want:
            return True, f"已打开开机自启动（任务管理器→启动 里可看到「{AUTOSTART_NAME}」）"
        return False, f"写了但回读不一致：期望 {want!r}，实际 {got!r}"
    if got:
        return False, f"关闭失败：注册表里还留着 {got!r}"
    return True, "已关闭开机自启动"


# ---- 托盘（通知区域图标）----
#: 给托盘图标发消息的自定义消息号（WM_APP + 1）
WM_TRAY = 0x8001
NIM_ADD, NIM_MODIFY, NIM_DELETE = 0, 1, 2
NIF_MESSAGE, NIF_ICON, NIF_TIP = 0x01, 0x02, 0x04
#: 鼠标消息
WM_LBUTTONUP, WM_RBUTTONUP, WM_LBUTTONDBLCLK = 0x0202, 0x0205, 0x0203
WM_CLOSE, WM_DESTROY = 0x0010, 0x0002
#: 菜单命令
IDM_TRAY_SHOW, IDM_TRAY_EXIT = 40001, 40002
#: 托盘菜单里的「设置…」（AI 用量和使用说明都在设置里）
IDM_TRAY_SETTINGS = 40003
#: 托盘菜单里的「显示 / 隐藏桌宠」（桌宠被隐藏后还能从这里叫回来）
IDM_TRAY_PET = 40004

#: 窗口过程的原型（x64 上 LRESULT/WPARAM/LPARAM 都是 64 位；回调里拿到的是**无符号**值，
#: 所以别把它原样再传给需要**有符号**参数的 API —— 会 OverflowError）
WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_longlong, w.HWND, w.UINT,
                             ctypes.c_ulonglong, ctypes.c_longlong)


class TrayMessageWindow:
    """
    托盘图标的宿主：**接管 Tk 主窗口的窗口过程**，在里面处理托盘回调。

    为什么用"子类化"而不是自己注册窗口类（这是实测踩出来的）：
    自建窗口类在本机**注册成功却建不出窗口** —— `RegisterClassExW` 返回了 atom
    （重复注册报 1410 = 已注册，说明第一次成功），可 `CreateWindowExW` 一律返回 0
    且 `GetLastError()` 也是 0，换 `WNDCLASSEXW`、换父窗口 `HWND_MESSAGE`、
    换样式都没用；而用系统类名 `STATIC` 建窗口立刻成功。
    **但把托盘图标挂到 Tk 主窗口自己的 hwnd 上完全正常**，所以走这条路：
    `SetWindowLongPtrW(GWLP_WNDPROC)` 接管窗口过程 → 自己的过程里处理 `WM_TRAY`
    → 其余消息**原样转给旧过程**（`CallWindowProcW`），Tk 完全不受影响。

    用法（同一线程、消息循环已在跑）：
        host = TrayMessageWindow(tk_hwnd)
        host.install()      # 接管窗口过程
        tray = TrayIcon(host.hwnd, icon, "WinMon"); tray.add()
        ... host.pump() 取用户选的菜单项 ...
        host.uninstall()    # 退出前恢复原过程
    """

    def __init__(self, hwnd: int) -> None:
        self.hwnd = int(hwnd)
        self.error = ""
        #: 必须留引用：回调被回收会让系统调用到野指针 → 整个程序崩
        self._proc = WNDPROC(self._wndproc)
        self._old_proc = 0
        self.installed = False
        #: 窗口过程收到的托盘命令。**直接用这个列表传，不再走 PostMessage+Peek**：
        #: 实测 Post 到主窗口的自定义消息会被 Tk 自己的消息泵吃掉
        #: （`PeekMessageW` 单独测能取到，一经过 `root.update()` 就没了），
        #: 而窗口过程本来就在主线程被调用，直接追加到列表最简单也最稳。
        self.commands: list[int] = []

    def install(self) -> tuple[bool, str]:
        """把主窗口的窗口过程换成我们的（记下旧的，用于转交）。"""
        if not self.hwnd:
            return False, "拿不到窗口句柄"
        if self.installed:
            return True, "已经接管过了"
        try:
            user32.SetWindowLongPtrW.restype = ctypes.c_void_p
            old = user32.SetWindowLongPtrW(w.HWND(self.hwnd), GWLP_WNDPROC,
                                           ctypes.cast(self._proc, ctypes.c_void_p))
            if not old:
                return False, f"SetWindowLongPtrW 失败（错误码 {ctypes.get_last_error()}）"
            self._old_proc = int(old)
            self.installed = True
            return True, "已接管窗口过程（托盘消息可用了）"
        except Exception as exc:
            self.error = f"{type(exc).__name__}: {exc}"
            return False, f"接管窗口过程失败：{self.error}"

    def _wndproc(self, hwnd, msg, wparam, lparam):
        try:
            if (int(msg) & 0xFFFF) == WM_TRAY:
                event = int(lparam) & 0xFFFF
                if event in (WM_RBUTTONUP, WM_LBUTTONUP, WM_LBUTTONDBLCLK):
                    cmd = IDM_TRAY_SHOW if event == WM_LBUTTONDBLCLK else self._menu()
                    if cmd:
                        # 窗口过程就在主线程（Tk 的消息泵里）被调用，
                        # 所以直接记下来即可，界面动作由 `_poll_tray` 去做。
                        self.commands.append(int(cmd))
                return 0
        except Exception:
            pass
        # 其余消息**一律转给原过程**，否则 Tk 的窗口会失灵
        return self._call_old(hwnd, msg, wparam, lparam)

    def _call_old(self, hwnd, msg, wparam, lparam):
        if self._old_proc:
            try:
                return user32.CallWindowProcW(ctypes.c_void_p(self._old_proc),
                                              w.HWND(hwnd), msg, wparam, lparam)
            except Exception:
                return 0
        return 0

    def _menu(self) -> int:
        try:
            hmenu = user32.CreatePopupMenu()
            user32.AppendMenuW(hmenu, 0x0000, IDM_TRAY_SHOW, "显示 winmon1.0")
            user32.AppendMenuW(hmenu, 0x0000, IDM_TRAY_PET, "显示 / 隐藏桌宠")
            user32.AppendMenuW(hmenu, 0x0000, IDM_TRAY_SETTINGS, "设置…")
            user32.AppendMenuW(hmenu, 0x0800, 0, None)          # MF_SEPARATOR
            user32.AppendMenuW(hmenu, 0x0000, IDM_TRAY_EXIT, "退出")
            pt = w.POINT()
            user32.GetCursorPos(ctypes.byref(pt))
            # 必须先 SetForegroundWindow，否则点菜单外面它不会消失
            user32.SetForegroundWindow(w.HWND(self.hwnd))
            cmd = user32.TrackPopupMenu(hmenu, 0x0100 | 0x0002,   # RETURNCMD|RIGHTBUTTON
                                        pt.x, pt.y, 0, w.HWND(self.hwnd), None)
            user32.PostMessageW(w.HWND(self.hwnd), 0x0000, 0, 0)  # WM_NULL：修菜单残留
            user32.DestroyMenu(hmenu)
            return int(cmd or 0)
        except Exception:
            return 0

    def pump(self) -> list[int]:
        """
        取走待处理的托盘命令（由主窗口的定时器调用，见 `WinMonApp._poll_tray`）。

        命令是窗口过程直接记下来的（见 `commands` 的注释：走 PostMessage 会被
        Tk 的消息泵吃掉，实测过）。这里只是"取走并清空"。
        """
        out = list(self.commands)
        self.commands.clear()
        return out

    def uninstall(self) -> None:
        """恢复原窗口过程（退出前必须调，否则 Tk 销毁窗口时会调到我们的回调）。"""
        if self.installed and self._old_proc:
            try:
                user32.SetWindowLongPtrW(w.HWND(self.hwnd), GWLP_WNDPROC,
                                         ctypes.c_void_p(self._old_proc))
            except Exception:
                pass
        self.installed = False

    def destroy(self) -> None:
        self.uninstall()


class NOTIFYICONDATAW(ctypes.Structure):
    """`NOTIFYICONDATAW`（按 x64 的实际大小与对齐写，末尾留余量给 szInfo 等字段）。"""
    _fields_ = [("cbSize", w.DWORD), ("hWnd", w.HWND), ("uID", w.UINT),
                ("uFlags", w.UINT), ("uCallbackMessage", w.UINT),
                ("hIcon", w.HICON), ("szTip", w.WCHAR * 128),
                ("dwState", w.DWORD), ("dwStateMask", w.DWORD),
                ("szInfo", w.WCHAR * 256), ("uTimeoutOrVersion", w.UINT),
                ("szInfoTitle", w.WCHAR * 64), ("dwInfoFlags", w.DWORD),
                ("guidItem", ctypes.c_byte * 16), ("hBalloonIcon", w.HICON)]


#: 托盘气泡用的标志（`NIF_INFO` 表示 szInfo/szInfoTitle 有效）
NIF_INFO = 0x10
NIIF_INFO, NIIF_WARNING, NIIF_ERROR = 0x1, 0x2, 0x3


class TrayIcon:
    """
    一个托盘图标：加图标 / 弹右键菜单 / 移除。

    为什么自己写（零依赖）：`Shell_NotifyIcon` 就是标准 API，几十行 ctypes 就够，
    不值得为它破"只用标准库"这条规矩。

    用法（必须在**有消息循环的线程**里创建）：
        tray = TrayIcon(hwnd, icon_path, "winmon1.0")
        tray.add()
        ... 菜单由 TrayMessageWindow 接管（它知道该弹哪几项）...
        tray.notify("标题", "正文")      # 可选的托盘气泡
        tray.remove()
    """

    def __init__(self, hwnd: int, icon_path: str = "", tip: str = "winmon1.0",
                 uid: int = 1) -> None:
        self.hwnd = int(hwnd)
        self.uid = int(uid)
        self.tip = str(tip)[:127]
        self.icon_path = icon_path
        self.ok = False
        self.error = ""
        self._hicon = 0
        self._nid = None

    def _load_icon(self) -> int:
        if self.icon_path and os.path.exists(self.icon_path):
            try:
                # LR_LOADFROMFILE=0x10 | LR_DEFAULTSIZE=0x40（在 user32 里）
                self._hicon = int(user32.LoadImageW(None, ctypes.c_wchar_p(self.icon_path),
                                                    1, 0, 0, 0x10 | 0x40) or 0)
            except Exception:
                self._hicon = 0
        if not self._hicon:
            # 退路：用系统自带的"应用程序"图标（IDI_APPLICATION = 32512）
            try:
                self._hicon = int(user32.LoadIconW(None, w.LPCWSTR(32512)) or 0)
            except Exception:
                self._hicon = 0
        return self._hicon

    def _build(self) -> "NOTIFYICONDATAW":
        nid = NOTIFYICONDATAW()
        nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
        nid.hWnd = w.HWND(self.hwnd)
        nid.uID = self.uid
        nid.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
        nid.uCallbackMessage = WM_TRAY
        nid.hIcon = w.HICON(self._load_icon())
        nid.szTip = self.tip
        self._nid = nid
        return nid

    def add(self) -> tuple[bool, str]:
        """加图标。**回读核对**：调完再查一次它是否真在。"""
        try:
            nid = self._build()
            sent = bool(shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(nid)))
        except Exception as exc:
            self.error = f"{type(exc).__name__}: {exc}"
            return False, f"加托盘图标失败：{self.error}"
        if not sent:
            self.error = f"Shell_NotifyIconW(NIM_ADD) 返回 0（错误码 {ctypes.get_last_error()}）"
            return False, f"加托盘图标失败：{self.error}"
        # NIM_ADD 返回非 0 就是成功；`Shell_NotifyIconGetRect` 只是加分项
        # （有些环境下这个查询 API 被限制，拿它当真会误报失败）
        self.ok = True
        return True, "已放入托盘（双击图标恢复窗口，右键出菜单）"

    def notify(self, title: str = "winmon1.0", text: str = "", severity: str = "info",
               timeout_ms: int = 5000) -> bool:
        """
        弹一条托盘气泡（Win10/11 上是"通知"）。

        为什么用它当本地提醒：不需要额外窗口、不需要 AI、进程活着就能弹；
        失败也不影响主流程（调用方自己退回状态栏）。标题与正文都会被截断到
        `NOTIFYICONDATAW` 的字段长度，避免 ctypes 写入越界。
        """
        if not self.ok:
            return False
        try:
            nid = NOTIFYICONDATAW()
            nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
            nid.hWnd = w.HWND(self.hwnd)
            nid.uID = self.uid
            nid.uFlags = NIF_INFO
            nid.szInfo = (text or "")[:255]
            nid.szInfoTitle = (title or "winmon1.0")[:63]
            nid.dwInfoFlags = {"warn": NIIF_WARNING, "error": NIIF_ERROR}.get(
                severity, NIIF_INFO)
            nid.uTimeoutOrVersion = max(1000, min(30000, int(timeout_ms)))
            return bool(shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(nid)))
        except Exception:
            return False

    def remove(self) -> None:
        try:
            nid = NOTIFYICONDATAW()
            nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
            nid.hWnd = w.HWND(self.hwnd)
            nid.uID = self.uid
            shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(nid))
            self.ok = False
        except Exception:
            pass



# ============================================================================
# 三之七、媒体会话（"现在在放什么" + 只对某一个播放器发命令）
# ============================================================================
#
# 媒体键能"切歌"，但说不出"当前是什么"。要说清楚，就得读 Windows 的**媒体会话**
# （GSMTC）—— 浏览器里的网页播放器、网易云、QQ音乐、Spotify 都会往里发布
# 曲名/歌手/播放状态/进度与时长，而且能**只对其中某一个**会话发命令。
#
# 为什么用 PowerShell 而不是 ctypes（这次真的实测过，别再来回试）：
#   · `RoActivateInstance(类名)`            → `E_NOTIMPL`（这个类不是这么激活的）
#   · `RoGetActivationFactory(类名)`        → **可以**（注意参数必须是
#     `WindowsCreateString` 建的真 HSTRING；传 C 字符串会得到 `E_INVALIDARG`，
#     很容易误判成"系统不让用"）
#   · 但真正的入口是**静态方法** `RequestAsync()`，它挂在 statics 接口上，
#     而那个 IID 只存在于 `Windows.Media.winmd`（二进制 GuidAttribute）。
#   · Windows 自己的 WinRT 投影**知道**这个 IID，它就在
#     `%WINDIR%\Microsoft.NET\Framework64\v4.0.30319\System.Runtime.WindowsRuntime.dll`
#     —— 用 PowerShell 调它，既不引第三方依赖，也不用自己写 ECMA-335 元数据读取器。
#
# 实测（本机，Edge 放音频）：`sessions=1 app=MSEdge
# title=soundhelix.com/examples/mp3/SoundHelix-Song-1.mp3 status=Playing pos=1 dur=373`
# 一次调用的开销约 290ms —— 所以**不要每 2 秒都调**，见界面的轮询间隔。

#: 播放状态（`GlobalSystemMediaTransportControlsSessionPlaybackStatus` 的整数值）
MEDIA_STATUS_NAMES = {0: "已关闭", 1: "已打开", 2: "正在切换",
                      3: "已停止", 4: "正在播放", 5: "已暂停"}

#: 媒体命令 → 脚本里的动作名
MEDIA_COMMANDS = {"play": "play", "pause": "pause",
                  "next": "next", "previous": "previous"}

#: 「媒体应用」的**显示名映射 + 常见程序补充**：(显示名, 进程名, 是否浏览器)。
#:
#: ⚠ 这张表**不是准入门槛**（2026-10-16 改）。以前下拉框里只列这张表里的程序，
#: 于是"用户装了个表里没有的播放器"或"换一台机器"就认不出来 —— 范围被个人环境框死了。
#: 现在的**第一真源是系统媒体会话（GSMTC）**：任何应用只要发布了会话，
#: `media_app_from_session()` 就会把它变成一条可选的媒体应用，与这张表无关。
#:
#: 这张表现在只干两件事：
#:   · 把会话里报的**家族名**换成用户看得懂的名字（`MSEdge` → `Microsoft Edge`）；
#:   · 当会话**不在**（此刻没在放东西）时，让用户仍能指定这些常见播放器。
#: 浏览器也列在里面 —— 明确要"可以指定 Edge 这类浏览器"，
#: 因为网页播放器（YouTube / 哔哩哔哩网页版）同样会注册媒体会话。
MEDIA_APP_CANDIDATES: tuple[tuple[str, str, bool], ...] = (
    ("网易云音乐", "cloudmusic.exe", False),
    ("QQ音乐", "QQMusic.exe", False),
    ("酷狗音乐", "KuGou.exe", False),
    ("酷我音乐", "KwMusic.exe", False),
    ("Spotify", "Spotify.exe", False),
    ("foobar2000", "foobar2000.exe", False),
    ("PotPlayer", "PotPlayerMini64.exe", False),
    ("VLC", "vlc.exe", False),
    ("哔哩哔哩", "哔哩哔哩.exe", False),
    ("Microsoft Edge", "msedge.exe", True),
    ("Google Chrome", "chrome.exe", True),
    ("Firefox", "firefox.exe", True),
)


class MediaApp:
    """一个"可以被 WinMon 识别/控制"的媒体应用。"""

    __slots__ = ("label", "exe", "browser", "path", "kind")

    def __init__(self, label: str, exe: str, browser: bool = False,
                 path: str = "", kind: str = "") -> None:
        self.label = str(label)
        self.exe = str(exe)
        self.browser = bool(browser)
        self.path = str(path)
        #: 下拉框里那个括号里的词。空串 = 按 `browser` 推断成「浏览器 / 播放器」；
        #: 非空 = **我们确实知道它是从哪来的**（现在只有从会话发现的，
        #: 见 `MEDIA_KIND_SESSION`）—— 用来避免把会话里认不出来的应用**假称**成播放器。
        self.kind = str(kind)

    @property
    def key(self) -> str:
        """设置文件里存这个（**存 exe 名，不存中文标签**，跨版本都认得住）。"""
        return self.exe.lower()

    @property
    def title(self) -> str:
        """下拉框里显示的文字。"""
        return f"{self.label}（{self.kind or ('浏览器' if self.browser else '播放器')}）"

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"MediaApp({self.label!r}, {self.exe!r}, browser={self.browser})"


#: `MediaApp.kind` 的取值：这一条是**从活的媒体会话里发现的**（不是候选表、
#: 也不是开始菜单）。用它而不是「播放器」，是因为"它能发布会话"这件事我们确定，
#: "它是不是播放器"我们并不知道 —— 宁可说得少，也不编造。
MEDIA_KIND_SESSION = "媒体会话"

#: 媒体应用名字里那些**会变**的后缀。
#:   · `（浏览器）/（播放器）/（媒体会话）` 由 `MediaApp.title` 拼；
#:   · `（运行中）/（菜单）` 由界面拼（取决于"此刻在不在跑"和"这条是从哪来的"）。
#: 比对"用户上次选的是哪一个"之前**必须先去干净**，否则会出现：
#: 选的时候 Edge 在跑（标签带「（运行中）」）、下次启动时没跑（不带）→ 直接比字符串
#: 就对不上 → 选择被静默退回「自动」（2026-10-13 复现并修掉，见 `resolve_media_pin`）。
#: 同理：同一个程序可能这次是**从会话**发现的（带「（媒体会话）」）、
#: 下次是从开始菜单发现的（带「（播放器）（菜单）」）—— 主干一比就还是同一个。
MEDIA_NAME_SUFFIXES: tuple[str, ...] = ("（运行中）", "（菜单）",
                                       "（浏览器）", "（播放器）", "（媒体会话）")


def media_app_stem(text: str) -> str:
    """
    媒体应用标签 → **稳定的主干**（去掉上面那些会变的后缀、去空白、转小写）。

    只用于"这是不是同一个程序"的比对与去重，**不用于显示**。
    只吃**结尾**的已知后缀，所以 `我的（浏览器）播放器` 这种名字不会被误伤。
    """
    out = str(text or "").strip()
    changed = True
    while changed:              # 后缀可能叠着：`…（播放器）（菜单）`
        changed = False
        for suffix in MEDIA_NAME_SUFFIXES:
            if out.endswith(suffix):
                out = out[: -len(suffix)].strip()
                changed = True
    return out.lower()


def media_app_exe_for_label(label: str) -> str:
    """
    按显示名在写死的候选表里找**进程名**；找不到返回空串。

    为什么需要：开始菜单里的条目只有 `.lnk`，`installed_media_apps()` 拿不到 exe 名
    （`MediaApp.exe` 是空串）。而"按应用发命令"是靠 exe 名去匹配媒体会话的 ——
    空 exe 会让 `media_command(slug, "")` 发给**第一个会话**，
    用户看到的就是"我选了 A，控的却是 B"。名字能对上候选项时补上，这条通路才成立。
    """
    stem = media_app_stem(label)
    if not stem:
        return ""
    for cand_label, exe, _browser in MEDIA_APP_CANDIDATES:
        if media_app_stem(cand_label) == stem:
            return exe
    return ""


def split_media_labels(raw: str) -> list[str]:
    """把设置里那串"标签1、标签2"拆成列表（逗号 / 顿号 / 竖线都认）。"""
    text = str(raw or "").replace(",", "、").replace("|", "、")
    return [p.strip() for p in text.split("、") if p.strip()]


def resolve_media_pin(pin: str, labels, any_label: str) -> tuple[str, list[str]]:
    """
    把设置里存的媒体应用标签**对齐成当前清单里的真实标签**（纯逻辑，可单测）。

    ## 为什么必须做这一步（2026-10-13 复现的真 bug）

    下拉框的标签里带着**会变的后缀**：「（运行中）」取决于那个程序此刻在不在跑。
    用户选的时候 Edge 在跑，存进设置的是 `Microsoft Edge（浏览器）（运行中）`；
    下次启动时 Edge 还没起来，清单里只有 `Microsoft Edge（浏览器）（菜单）` ——
    界面原来的 `if pin not in labels` 直接比字符串，于是把选择**静默退回「自动」**。
    用户只会觉得"我选好的播放器怎么又变自动了"，而"按应用发命令"也跟着失效
    （这正是 HANDOFF 坑 67 那一类："设置项没真的接进代码路径"）。

    这里按**主干**（`media_app_stem()`）比对，并且换回**当前清单里的真实标签**
    —— `_picked_media_app()` / `_app_key_for_media()` 是拿标签查字典的，
    写成别的形式反而查不到。

    返回 `(应当写回 media_app_pin 的值, 对不上的那些标签)`：
      · 一个都对不上 → 退回 `any_label`（与以前一致：别把界面卡在无效值上）；
      · 部分对得上 → **保住对得上的那些**，只丢掉真的找不到的（不连坐）。
    """
    labels = [str(x) for x in (labels or [])]
    found = [x for x in split_media_labels(pin) if x != any_label]
    if not found:
        return any_label, []
    stems = [(lbl, media_app_stem(lbl)) for lbl in labels if lbl and lbl != any_label]
    kept: list[str] = []
    dropped: list[str] = []
    for want in found:
        hit = want if want in labels else ""
        if not hit:
            stem = media_app_stem(want)
            hit = next((lbl for lbl, s in stems if s and s == stem), "")
        if hit:
            if hit not in kept:
                kept.append(hit)
        else:
            dropped.append(want)
    if not kept:
        return any_label, dropped
    return "、".join(kept), dropped


def media_app_path(exe: str, processes: dict | None = None) -> str:
    """
    找可执行文件的完整路径（用于进程名校验/会话匹配）。

    顺序：正在运行的进程 → Program Files 常见位置 → 开始菜单快捷方式。
    找不到就返回空串。

    `processes` 可以传入一份已经取好的 `snapshot_processes()`。启动时一次
    组装媒体清单会问十来个 exe，每次都重新枚举整张进程表会白花上百毫秒；
    调用方共用同一份快照即可。不传则保持原行为（自己取一份）。
    """
    exe = str(exe or "")
    if not exe:
        return ""
    low = exe.lower()
    # 1) 已经在跑：直接问系统
    try:
        procs = processes if processes is not None else snapshot_processes()
        for pid, info in procs.items():
            if (info.get("name") or "").lower() == low:
                path, _mem = query_process_detail(pid)
                if path and os.path.exists(path):
                    return path
    except Exception:
        pass
    # 2) 常见安装位置
    roots = [os.environ.get("ProgramFiles", r"C:\Program Files"),
             os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
             os.environ.get("LOCALAPPDATA", "")]
    for root in roots:
        if not root:
            continue
        for sub in ("", "Microsoft", "Windows Media Player", "Music", "Player"):
            cand = os.path.join(root, sub, exe)
            if os.path.exists(cand):
                return cand
    # 3) 交给系统/资源管理器按名字解析（不弹窗，失败就算了）
    return ""


def installed_media_apps(limit: int = 24) -> list["MediaApp"]:
    """
    从**开始菜单**里找出"可能放东西"的程序（音乐播放器 + 浏览器）。

    为什么要这一步（约定"可自行选择指定音乐 app 或 Edge 这类浏览器"）：
    只靠写死的候选表，用户装的播放器只要不在表里就选不到。开始菜单是 Windows
    自己的"已安装程序"清单，比猜安装路径靠谱得多。

    只返回**能从菜单启动的条目**，并且要过两道筛：
      · 类别必须落在 `media` / `browser` 两类里；
      · 排除"卸载 / 帮助 / 反馈 / 官网"这类**伴随条目**（实测不排的话，
        网易云音乐的目录里会冒出「卸载网易云音乐」「使用问题反馈」这些"媒体应用"）。
    宁可少列，也不要把一堆无关程序塞进下拉框。
    """
    out: list[MediaApp] = []
    seen: set[str] = set()
    #: 这些词出现在菜单项名里就跳过（伴随条目，不是能放东西的程序）
    junk = ("卸载", "uninstall", "帮助", "help", "readme", "说明", "反馈",
            "官网", "主页", "website", "更新", "update", "修复", "repair",
            "许可", "license", "文档", "documentation")
    try:
        dirs = []
        for env in ("ProgramData", "APPDATA"):
            base = os.environ.get(env)
            if base:
                dirs.append(os.path.join(base, r"Microsoft\Windows\Start Menu\Programs"))
        for root in dirs:
            if not os.path.isdir(root):
                continue
            for base, _dirs, files in os.walk(root):
                for fn in files:
                    if not fn.lower().endswith((".lnk", ".exe")):
                        continue
                    name = os.path.splitext(fn)[0]
                    low = name.lower()
                    if any(word in low for word in junk):
                        continue
                    path = os.path.join(base, fn)
                    cat = classify_program(name, path)
                    if cat not in ("media", "browser"):
                        continue
                    key = name.strip().lower()
                    if not key or key in seen:
                        continue
                    seen.add(key)
                    out.append(MediaApp(name, "", cat == "browser", path))
                    if len(out) >= limit:
                        return out
    except Exception:
        return out
    return out


def media_app_stems_match(a: str, b: str) -> bool:
    """
    两个"应用标识"（exe 名 / 家族名，任意大小写、可带可不带 .exe）是否指同一个程序。

    先**精确**（去掉 .exe 后完全相等）；再做**包含**匹配，但要求两边都 ≥5 个
    字符 —— 否则会话里的家族名 `QQ` 会被 `QQMusic` 包含命中，出现
    "选了 A、控的却是 B"（本函数是 P2 修复的统一匹配入口）。
    """
    x = str(a or "").lower().removesuffix(".exe")
    y = str(b or "").lower().removesuffix(".exe")
    if not x or not y:
        return False
    if x == y:
        return True
    return len(x) >= 5 and len(y) >= 5 and (x in y or y in x)


def media_app_running(exe: str, running) -> bool:
    """
    这个应用**此刻在不在跑**（`running` 是一个"小写进程名"的可迭代对象）。

    为什么不能直接比字符串（2026-10-16 改）：会话里报的 app id 常常**不是进程名**
    —— Edge 报家族名 `MSEdge`，UWP 报 `包名_哈希!App`。以前用
    `exe.lower() in running` 判，这两种都会被判成"没在跑"，后果是用户点
    「播放/暂停」时 WinMon 误判没有目标、直接改发全局媒体键，
    或者干脆说"它没在运行"—— 而它正在放歌。
    改走 `media_app_stems_match`：精确优先，包含匹配要求两边都 ≥5 字符
    （防 `QQ` 命中 `QQMusic` 那种"选了 A、控的却是 B"）。
    """
    want = str(exe or "").strip()
    if not want:
        return False
    return any(media_app_stems_match(want, str(name)) for name in (running or ()))


def media_app_from_session(app_id: str, running=None) -> "MediaApp | None":
    """
    把一个**活的媒体会话**里的 `SourceAppUserModelId` 变成一条可选的媒体应用。

    这是"基于**所有**可识别的媒体"的落地（2026-10-16）。以前下拉框只列
    `MEDIA_APP_CANDIDATES` ＋ 开始菜单里媒体/浏览器两类的条目 —— 范围被"这台机器
    装了什么"和"表里写了什么"双重框死。现在**会话本身就是第一真源**：
    谁能发布会话，谁就自动出现在清单里，并且能被精确控制（与候选表无关）。

    三条约定：
      · **名字**：候选表里认得出就用显示名（`MSEdge` → `Microsoft Edge`），
        认不出就**原样用会话里的 id** —— 不编造一个好听的名字；
      · **进程名**：会话报的多半不是进程名。能对上"正在跑的进程"就换成真进程名
        （这样 `media_app_path()` 和进程匹配都用得上），
        对不上就原样留着 —— 命令照样能按 app id 精确下发（`media_session.ps1`
        对 app id 是宽松匹配）；
      · **类别**：候选表说它是浏览器就是浏览器；认不出来的**不假装**成播放器，
        标成 `MEDIA_KIND_SESSION`（「媒体会话」）—— 用户一眼能看出这条是从
        系统媒体会话里发现的。
    """
    raw = str(app_id or "").strip()
    if not raw:
        return None
    label, exe, browser, known = raw, "", False, False
    for cand_label, cand_exe, cand_browser in MEDIA_APP_CANDIDATES:
        if media_app_stems_match(raw, cand_exe):
            label, exe, browser, known = cand_label, cand_exe, cand_browser, True
            break
    if not known:
        browser = any(media_app_stems_match(raw, b) for b in BROWSER_PROCESSES)
    if not exe:
        exe = next((str(name) for name in sorted(running or ())
                    if media_app_stems_match(raw, str(name))), raw)
    return MediaApp(label, exe, browser, "", "" if known else MEDIA_KIND_SESSION)


def media_app_display_name(app_id: str) -> str:
    """
    把媒体会话里的 `SourceAppUserModelId` 换成人看得懂的名字。

    会话里报的常是**家族名**（实测 Edge 报 `MSEdge`），直接显示像乱码。
    按候选表找显示名；找不到就原样返回（**不编造**）。
    匹配走 `media_app_stems_match`（精确优先，防 QQ↔QQMusic 误命中）。
    """
    raw = str(app_id or "").strip()
    if not raw:
        return ""
    for label, exe, _browser in MEDIA_APP_CANDIDATES:
        if media_app_stems_match(raw, exe):
            return label
    return raw


#: 桥接脚本的版本标记（写在脚本首部注释里）。用来区分「旧版脚本（该升级）」
#: 和「用户手工改过的脚本（绝不能动）」—— 见 --selftest 的 [8/8] 守门员。
_MEDIA_HELPER_MARKER = "WinMon media helper v2"


def media_session_helper_path() -> str:
    """媒体会话桥接脚本的路径（`media_session.ps1`，与主程序同目录）。"""
    return os.path.join(APP_DIR, "media_session.ps1")


#: 常驻快照文件（助手每几秒往里写一行 JSON）。可用环境变量覆盖（**给测试用**）
MEDIA_SNAPSHOT_ENV = "WINMON_MEDIA_SNAPSHOT"
#: 快照"还算新鲜"的秒数：比助手的间隔（3 秒）宽一些，留出抖动余量
MEDIA_SNAPSHOT_TTL = 6.0
#: 助手进程句柄（由 `start_media_snapshot_helper()` 放进来，退出时用它收尾）
_media_helper_proc = None


def media_snapshot_path() -> str:
    """常驻快照文件的完整路径（默认在运行时数据目录；可用环境变量覆盖）。"""
    override = os.environ.get(MEDIA_SNAPSHOT_ENV)
    if override:
        return override
    return data_file("winmon_media.jsonl")


def read_media_snapshot(max_age: float = MEDIA_SNAPSHOT_TTL) -> dict | None:
    """
    读常驻助手写下的**最后一行**快照；够新就返回，否则返回 None。

    为什么要有这条快照路（当时新增，A.1 的落地做法）：
      · 起一次 PowerShell 约 **290 ms**（实测），以前每 4 秒都得起一个 —— 界面只能等它；
      · 现在助手**常驻**、每 3 秒把一行 JSON 写进文件，Python 读文件实测 **0.05 ms**。
        于是"刷新媒体那一行"在正常情况下**完全不需要起进程**。
    读不到/过期/JSON 坏，一律返回 None（调用方会退回一次性调用）—— 绝不抛异常。
    """
    path = media_snapshot_path()
    try:
        if not os.path.exists(path):
            return None
        if time.time() - os.path.getmtime(path) > max(0.5, max_age):
            return None
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            # 只读文件尾部：快照是"一行一条"，取最后一条就够，不必读整个文件
            back = min(size, 16384)
            fh.seek(size - back)
            tail = fh.read(back).decode("utf-8", "replace")
        line = [ln for ln in tail.splitlines() if ln.strip().startswith("{")]
        if not line:
            return None
        data = json.loads(line[-1])
        if not isinstance(data, dict):
            return None
        data.setdefault("sessions", [])
        return data
    except Exception:
        return None


def start_media_snapshot_helper(watch_seconds: int = 1800,
                                interval_ms: int = 3000) -> tuple[bool, str]:
    """
    启动**常驻**媒体会话助手（每 `interval_ms` 往快照文件写一行）。

    返回 `(是否已发起, 说明)`。已经有一个在跑就不重复启动
    （判据：**快照文件还新鲜**，或者我们手里的进程还活着）——避免堆一串 PowerShell。
    """
    global _media_helper_proc
    helper = media_session_helper_path()
    if not os.path.exists(helper):
        return False, f"缺少 {os.path.basename(helper)}"
    if _media_helper_proc is not None and _media_helper_proc.poll() is None:
        return True, "已经在跑"
    if read_media_snapshot() is not None:
        return True, "已有一个在写快照"
    try:
        # 助手要往 `-Snapshot` 那个路径写文件 —— 先确保它所在目录存在。
        # 用**快照路径自己的目录**（而不是 `data_dir()`）：测试会把
        # `WINMON_MEDIA_SNAPSHOT` 指到临时目录，那时不该去建真实的数据目录。
        os.makedirs(os.path.dirname(os.path.abspath(media_snapshot_path())), exist_ok=True)
    except OSError:
        pass
    try:
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        _media_helper_proc = subprocess.Popen(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", helper,
             "-Snapshot", media_snapshot_path(), "-Watch", str(int(watch_seconds)),
             "-IntervalMs", str(int(interval_ms))],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=flags)
    except Exception as exc:
        return False, f"启动失败：{type(exc).__name__}: {exc}"
    return True, "已启动常驻助手（快照模式）"


def stop_media_snapshot_helper() -> None:
    """退出前收掉常驻助手（**只收我们自己起的那个**）。"""
    global _media_helper_proc
    proc, _media_helper_proc = _media_helper_proc, None
    if proc is None:
        return
    try:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except Exception:
                proc.kill()
    except Exception:
        pass


def collect_media_sessions(timeout: float = 8.0, prefer_snapshot: bool = True) -> dict:
    """
    读一次系统媒体会话，返回
    `{"ok": bool, "error": str, "sessions": [{app,title,artist,album,status}, ...]}`。

    **顺序**：先看常驻快照（0.05 ms），没有再退回一次性调用助手（约 290 ms）。
    这样"刷新界面"在正常情况下不碰进程，而助手万一挂了也能自动降级、不至于显示不出东西。

    **只读**：不切歌、不改任何东西。任何失败都变成 `ok=False` + `error`，
    绝不抛异常（媒体信息只是"增强"，不能让刷新流程崩掉）。

    输出编码：见 `_decode_helper_output()` —— PowerShell 的管道输出是**系统 ANSI 代码页**，
    不是 UTF-8（中文曲名乱码就是这儿来的）。
    """
    if prefer_snapshot:
        quick = read_media_snapshot()
        if quick is not None:
            return quick
    try:
        raw, err_raw, code = _run_media_helper(timeout=timeout)
    except FileNotFoundError as exc:
        return {"ok": False, "error": str(exc), "sessions": []}
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}", "sessions": []}
    text = _decode_helper_output(raw)
    if not text:
        err = _decode_helper_output(err_raw)[:200]
        return {"ok": False, "error": err or f"没有输出（退出码 {code}）",
                "sessions": []}
    try:
        data = json.loads(text.splitlines()[-1])
    except Exception as exc:
        return {"ok": False, "error": f"输出不是 JSON：{exc}", "sessions": []}
    if not isinstance(data, dict):
        return {"ok": False, "error": "输出格式不对", "sessions": []}
    data.setdefault("sessions", [])
    data.setdefault("ok", False)
    data.setdefault("error", "")
    return data


#: 这些进程的标题**不能**当曲名用：
#: 浏览器标题里有标签页名和浏览器名（"网易云音乐 和另外 2 个页面 - … - Microsoft Edge"），
#: 拿它当歌名就是编造；它们的播放状态本来就该从媒体会话里读。
TITLE_SOURCE_SKIP = ("msedge.exe", "chrome.exe", "firefox.exe", "iexplore.exe")


def find_media_app_window(exe: str) -> "WinInfo | None":
    """
    找这个媒体应用的**主窗口**（有标题、非隐藏面、面积最大的那个）。

    用途：网易云这类**不向 Windows 发布媒体会话**的播放器，只能从它的窗口标题
    里读曲名（实测：客户端在放歌时，标题就是 `歌手 - 歌名`）。
    找不到返回 None（不编造）。
    """
    want = str(exe or "").lower().strip()
    if not want:
        return None
    procs = snapshot_processes()
    best = None
    best_area = 0
    for wi in enum_windows(include_cloaked=True):
        if (procs.get(wi.pid, {}).get("name") or "").lower() != want:
            continue
        if not wi.title or wi.is_hidden_surface:
            continue
        area = int(wi.rect_w or 0) * int(wi.rect_h or 0)
        if area > best_area:
            best, best_area = wi, area
    return best


def title_track_hint(exe: str) -> tuple[str, str]:
    """
    从播放器的**窗口标题**里猜"现在在放什么"。

    返回 `(标题, 是哪来的)`；读不到就返回 `("", 原因)`。

    为什么要这条兜底（2026-10-12 实测）：
      · Windows 的媒体会话（GSMTC）只在应用**主动发布**时才有内容；
      · 实测**网易云音乐客户端在放歌时也不发布会话**（它的窗口标题是歌名，
        但 `collect_media_sessions()` 里没有它）—— 于是 WinMon 那一行只能显示"没有在播放"；
      · 而浏览器标题里混着标签页名，**不能**当曲名（会把"网易云音乐 和另外 2 个页面"
        显示成歌名），所以浏览器一律跳过、只对客户端做这条兜底。
    可靠性说明：这条是"标题栏猜测"，**不如媒体会话可靠**，所以界面会标注来源，
    让用户知道这是从标题读来的、不是系统会话给的。
    """
    exe = str(exe or "").lower().strip()
    if not exe:
        return "", "没有指定应用"
    if exe in TITLE_SOURCE_SKIP:
        return "", "浏览器标题里混着标签页名，不能当曲名"
    wi = find_media_app_window(exe)
    if wi is None:
        return "", "没找到它的窗口（可能没在运行或窗口已隐藏）"
    title = str(wi.title or "").strip()
    if not title:
        return "", "它的窗口没有标题"
    return title, "窗口标题"


def _decode_helper_output(raw: bytes) -> str:
    """
    解码 PowerShell 桥接脚本的**管道输出**。

    ## 为什么不能直接 `decode("utf-8")`（当时实测的真 bug）

    Windows PowerShell 5.1 往**管道**里写文本时，用的是**系统 ANSI 代码页**
    （中文 Windows = GBK / CP936），**不是 UTF-8** —— 于是中文曲名/歌手全变成乱码。
    实测证据（十六进制，一首叫《有我呢》的歌）：

        title 原始字节 = d3 d0 ce d2 c4 d8      ← GBK
        UTF-8 解码     = '������'                 ← WinMon 以前就是这么解的（乱码）
        GBK   解码     = '有我呢'                 ← 正确

    所以这里按"哪种解出来像正经 JSON"来判断，顺序是：
      1. **UTF-8**（严格）—— 如果哪天脚本改成显式输出 UTF-8，这条会先命中；
      2. **系统 ANSI 代码页**（中文 Windows 上是 cp936）。注意不能用
         `locale.getpreferredencoding()`：Python 以 `-X utf8` / `PYTHONUTF8=1`
         启动时它返回 utf-8，GBK 兜底会静默失效（测试里已经复现）。
         `locale.getencoding()` 在 UTF-8 模式下仍返回系统 ANSI 代码页；
      3. 都不行就 UTF-8 + `replace`（宁可留个问号，也不要抛异常）。

    这样修的好处：**不依赖 `chcp`**（那个只影响它自己那个控制台，管不到新起的进程），
    也不要求脚本改输出格式，两边任一改动都不会再烂。
    """
    data = raw or b""
    if not data:
        return ""
    try:
        ansi = locale.getencoding()
    except AttributeError:                       # Python < 3.11
        ansi = locale.getpreferredencoding(False) or "utf-8"
    if ansi.lower().replace("-", "").replace("_", "") in ("utf8", "utf"):
        # 有些 3.10 及更早版本没有 getencoding()，或者发行版仍把 UTF-8 模式
        # 报成系统编码；直接问 Windows 的 ANSI 代码页（本机 = 936）。
        try:
            acp = int(ctypes.windll.kernel32.GetACP())
            if acp:
                ansi = f"cp{acp}"
        except Exception:
            pass
    encodings = ["utf-8"]
    if ansi and ansi.lower().replace("-", "").replace("_", "") not in ("utf8", "utf"):
        encodings.append(ansi)
    for enc in encodings:
        try:
            text = data.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
        if text.lstrip().startswith("{"):
            return text.strip()
    return data.decode("utf-8", "replace").strip()


def _run_media_helper(extra_args: list[str] | None = None,
                      timeout: float = 8.0) -> tuple[bytes, bytes, int]:
    """跑一次桥接脚本，返回 `(stdout 原始字节, stderr 原始字节, 退出码)`。"""
    helper = media_session_helper_path()
    if not os.path.exists(helper):
        raise FileNotFoundError(f"缺少 {os.path.basename(helper)}")
    cmd = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", helper]
    cmd += list(extra_args or [])
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    got = subprocess.run(cmd, capture_output=True, timeout=timeout,
                         creationflags=flags)
    return got.stdout or b"", got.stderr or b"", int(got.returncode or 0)
def media_command(slug: str, app_id: str = "", timeout: float = 8.0) -> tuple[bool, str]:
    """
    对**某一个**媒体会话发命令（`play` / `pause` / `next` / `previous`）。

    与 `send_media_key` 的区别：媒体键是"全局按键"（交给当前活跃的播放器），
    这条是**按应用**发的（`app_id` 用应用 id 模糊匹配，空 = 第一个会话）。
    仍然**如实报告**：返回的是"应用接受了/拒绝了命令"，不是"歌已经换了"。
    """
    slug = str(slug or "").strip().lower()
    if slug not in MEDIA_COMMANDS:
        return False, f"不认识的媒体命令：{slug!r}（只支持 {'/'.join(MEDIA_COMMANDS)}）"
    args = ["-Command", MEDIA_COMMANDS[slug]]
    if app_id:
        args += ["-App", str(app_id)]
    try:
        raw, _err, _code = _run_media_helper(args, timeout=timeout)
    except FileNotFoundError as exc:
        return False, str(exc)
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"
    # 编码同样不能硬按 UTF-8 解（理由见 `_decode_helper_output()`）
    text = _decode_helper_output(raw)
    try:
        data = json.loads(text.splitlines()[-1]) if text else {}
    except Exception:
        data = {}
    if not data.get("command_ok"):
        msg = data.get("command_msg") or data.get("error") or "命令没有生效"
        return False, f"命令未生效：{msg}"
    return True, f"应用接受了「{slug}」命令"


def media_session_summary(sessions: list, app_key: str = "") -> dict:
    """
    从会话列表里挑出"该显示哪一个"，并整理成界面直接可用的一行。

    挑选顺序：
      1. 用户指定的应用（`app_key` 是 exe 名或应用 id 片段）—— 只在它确实有会话时选它；
      2. 正在播放的（status == 4）；
      3. 第一个有会话的。

    **纯逻辑**（不碰系统），所以能直接单测。`found` 为假表示"一个会话都没有"。
    """
    rows = [s for s in (sessions or []) if isinstance(s, dict)]
    if not rows:
        return {"found": False, "picked": None, "playing": 0, "count": 0,
                "text": "没有程序在放东西"}

    def _stem(text: str) -> str:
        """去掉 `.exe` 后缀并小写 —— 用于宽松匹配。"""
        t = str(text or "").lower().strip()
        return t[:-4] if t.endswith(".exe") else t

    def _stems(key: str) -> list[str]:
        """把 `app_key` 拆成若干"主干"：支持**多选**（逗号/顿号/竖线分隔）。"""
        out = []
        for piece in str(key or "").replace("|", ",").replace("、", ",").split(","):
            s = _stem(piece)
            if s:
                out.append(s)
        return out

    keys = _stems(app_key)

    def matches(row) -> bool:
        """
        这一行是不是用户指定的那个（**或那几个**）应用？

        **必须按"去掉 .exe 后的主干"比**（实测踩到）：媒体会话里的
        `SourceAppUserModelId` 常常是**家族名**、不带 .exe
        —— Edge 报的是 `MSEdge`，而用户在下拉框里选的是 `msedge.exe`。
        只按 `.exe` 比会永远匹配不上，表现为"点播放没反应，还说没有会话"。

        2026-10-12 起支持多选：`app_key` 里可以有多个主干（"msedge,cloudmusic"）。
        """
        app = _stem(row.get("app"))
        if not app:
            return False
        # P2 修复：统一走 media_app_stems_match（精确优先、包含要求 ≥5 字符），
        # 防止会话家族名 `QQ` 被 `QQMusic` 包含命中（"选了 A、控的却是 B"）。
        return any(media_app_stems_match(want, app) for want in keys)

    picked = None
    if keys:
        picked = next((r for r in rows if matches(r)), None)
    if picked is None:
        picked = next((r for r in rows if int(r.get("status") or 0) == 4), None)
    if picked is None:
        picked = rows[0]

    playing = sum(1 for r in rows if int(r.get("status") or 0) == 4)
    parts = []
    title = str(picked.get("title") or "").strip()
    artist = str(picked.get("artist") or "").strip()
    if title:
        parts.append(title)
    if artist:
        parts.append(artist)
    if not parts:
        parts.append(f"（{picked.get('app') or '未知'} 没有提供曲名）")
    status = int(picked.get("status", -1))
    parts.append(MEDIA_STATUS_NAMES.get(status, f"状态 {status}"))
    if app_key and not matches(picked):
        parts.append(f"⚠ 不是指定的应用（当前是 {picked.get('app') or '?'}）")
    # 注意：**不再返回进度字段**（用户已放弃"识别播放进度"这项需求）。
    # 会话里如果还带着 position/duration，那是原始数据，界面不读也不再加工。
    #
    # 多会话提示（A.2）：有**不止一个**会话时，除了显示挑中的那个，还给出
    # `others`（其余会话的"程序名：曲名"），界面可以在后面加一句"还有 N 个在放"。
    def _label(row) -> str:
        app = media_app_display_name(row.get("app")) or str(row.get("app") or "?")
        t = str(row.get("title") or "").strip()
        return f"{app}：{t}" if t else app

    others = [_label(r) for r in rows if r is not picked]
    return {"found": True, "picked": picked, "playing": playing, "count": len(rows),
            "others": others, "labels": [_label(r) for r in rows],
            "text": " · ".join(parts)}


#: 不该被当成"播放器窗口"的进程：浏览器标题里也常含 bilibili
#: （实测用 Edge 打开视频页后，标题是「…_哔哩哔哩_bilibili - 个人 - Microsoft Edge」，
#: 于是 `find_video_window("bilibili")` 会误认成哔哩哔哩客户端）。
BROWSER_PROCESSES = {
    "msedge.exe", "chrome.exe", "firefox.exe", "brave.exe", "opera.exe",
    "vivaldi.exe", "iexplore.exe", "360se.exe", "360chrome.exe",
    "qqbrowser.exe", "sogouexplorer.exe", "maxthon.exe",
}


def find_video_window(keyword: str = "bilibili",
                      require_visible: bool = True) -> "WinInfo | None":
    """
    找出播放器窗口（默认为哔哩哔哩**客户端**）。

    注意：进程名是纯中文的 "哔哩哔哩.exe"，里面**没有** "bilibili" 字样，
    所以要同时比对窗口标题与可执行文件路径，不能只看进程名。

    反过来也要小心：浏览器标题里同样会出现 "bilibili"
    （例如 Edge 打开视频页后标题为「…_哔哩哔哩_bilibili - 个人 - Microsoft Edge」），
    因此**浏览器进程一律排除** —— 否则会拿浏览器窗口去发键盘消息，白费功夫。
    """
    kw = (keyword or "").lower()
    procs = snapshot_processes()
    best = None
    for wi in enum_windows(include_cloaked=True):
        if require_visible and wi.is_hidden_surface:
            continue
        if wi.rect_w < 400 or wi.rect_h < 300:
            continue
        proc_name = (procs.get(wi.pid, {}).get("name") or "").lower()
        if proc_name in BROWSER_PROCESSES:
            continue
        path, _ = query_process_detail(wi.pid)
        haystack = " ".join([proc_name, wi.title or "", path or ""]).lower()
        if kw and kw not in haystack:
            continue
        if best is None or wi.rect_w * wi.rect_h > best.rect_w * best.rect_h:
            best = wi
    return best
def close_video_window(wi: "WinInfo", timeout: float = 4.0,
                       escalate_tray: bool = True) -> tuple[bool, str]:
    """
    关闭"正在放视频的那个窗口"，并且**核对真的关掉了**。

    实测会碰到三种结局，必须分开处理（否则会假报成功）：
      1. **窗口被销毁** —— 浏览器就是这种（Edge 关掉后主窗口进程都没了）→ 成功。
      2. **窗口隐藏、进程还活着** —— 实测哔哩哔哩客户端：
         `WM_CLOSE` 只是**最小化到托盘**（`IsWindowVisible` 变 False、句柄仍在、
         6 个进程全都活着），也就是**视频很可能还在后台出声**。
         这时"关闭窗口"并没有达到"关掉视频"的目的，所以默认**升级为结束进程树**。
      3. **窗口仍然可见** —— 多半是弹了"确认退出"对话框。
         这时**不擅自结束进程**（那会绕过用户的确认），如实报告并把决定权交回用户
         （程序里还有「结束进程」可用）。
    """
    hwnd = wi.hwnd
    if not hwnd or not user32.IsWindow(w.HWND(hwnd)):
        return True, "窗口已经不在了"
    ok, msg = graceful_close_window(hwnd)
    if not ok:
        return False, msg
    deadline = time.time() + max(0.0, timeout)
    while time.time() < deadline:
        if not user32.IsWindow(w.HWND(hwnd)):
            return True, "已关闭当前窗口（核对：窗口已消失）"
        time.sleep(0.15)

    name = wi.proc or wi.title or f"PID {wi.pid}"
    if bool(user32.IsWindowVisible(w.HWND(hwnd))):
        return False, (f"已发送关闭请求，但 {timeout:.0f}s 后窗口仍在"
                       f"（可能弹出了“确认退出”对话框，需要手动确认；"
                       f"也可以用「结束进程」强制关掉）")
    if not escalate_tray:
        return False, f"{name} 把窗口隐藏到了托盘，进程仍在运行（视频可能还在后台出声）"
    procs = snapshot_processes()
    ok_count, failures = terminate_process_tree(wi.pid, procs)
    if not is_process_alive(wi.pid):
        extra = f"；{len(failures)} 个子进程未能结束" if failures else ""
        return True, (f"{name} 只是把窗口隐藏到了托盘（并没有真的退出），"
                      f"已改为结束进程：共结束 {ok_count} 个{extra}"
                      f"（否则视频会在后台继续播放）")
    return False, (f"{name} 隐藏到托盘，且结束进程未完全成功"
                   f"（结束 {ok_count} 个，失败 {failures[:2]}）；进程仍在（PID {wi.pid}）")


def stop_video_in_window(wi: "WinInfo", mode: str = "smart") -> tuple[bool, str]:
    """
    停止 / 关闭当前视频。

    mode：
      "window" **关闭当前窗口**（给窗口发 WM_CLOSE，等价于点右上角的 ×）—— 推荐方式：
               不依赖前台、不依赖键盘注入（本环境注入不进去），而且关没关可以直接核对；
      "close"  关闭标签页（Ctrl+W，需要窗口真的在前台，本环境常常做不到）；
      "smart"  先暂停（空格），再返回上一页（Backspace）；
      "pause"  只暂停；
      "back"   只返回上一页。
    """
    if mode == "window":
        return close_video_window(wi)
    if mode not in ("close", "smart", "pause", "back"):
        # 认不出来的一律按最稳妥的"关窗口"处理（旧设置里可能存着别的值）
        return close_video_window(wi)
    sender = WindowKeySender(wi.hwnd)
    if not sender.focus():
        return False, "无法把窗口切到前台（键盘输入不会生效）"
    VK = WindowKeySender
    if mode == "pause":
        sender.key(VK.VK_SPACE)
        return True, "已发送暂停（空格）"
    if mode == "back":
        sender.key(0x08)                       # Backspace
        return True, "已发送返回上一页（Backspace）"
    if mode == "close":
        sender.combo(VK.VK_CONTROL, VK.VK_W)
        return True, "已发送关闭标签页（Ctrl+W）"
    # smart：先暂停再返回
    sender.key(VK.VK_SPACE)
    time.sleep(0.5)
    sender.key(0x08)
    return True, "已暂停并返回上一页"


# ============================================================================
# 八点六、视频来源识别 + "怎么精准打开"决策
# ============================================================================
#
# 为什么要有这一节（当时把链路重新梳理了一遍）：
#
# 1) 老实现只有一个来源：哔哩哔哩**客户端**（`find_video_window("bilibili")`
#    还刻意排除浏览器，因为浏览器标题里也有 bilibili，会把键盘消息发错地方）。
#    于是「打开视频并监视播放」有两条死路：
#      · 客户端没开 → 直接"没有找到哔哩哔哩窗口"；
#      · 客户端开着 → 靠键盘注入在客户端里点卡片，而本环境**注入不进去**
#        （见 HANDOFF 输入通路边界表），即使焦点正确、回车发出去了，页面也不动。
#
# 2) 真正可用的那条路早就存在（UIA 读卡片 href → 交给浏览器打开，
#    实测能把视频页打开），但它只挂在「列出视频(UIA)」的弹窗上，没接进主流程。
#
# 3) 所以这里把两件事拆开、做成可单测的纯逻辑：
#    · `detect_video_sources()` 回答"视频可能在哪个程序里"（可多个，带打分和依据）；
#    · `plan_video_open()`      回答"对它该用什么方式精准打开"（URL / 本地文件 / 只能手动点）。
#    GUI 只执行这份计划并把依据回显给用户，不再自己拍脑袋。
#
# 4) 多视频软件并存时（浏览器 + 客户端 + 本地播放器）不再靠"猜一个"：
#    先按证据打分排序（标题像播放页 / 有声音 / 是不是客户端），
#    存在并列候选时弹窗让用户选，并可以把选择**记下来**下次直接用。

#: 视频来源分类表：可执行文件名（小写）→ (显示名, 类别)
#:
#: 类别决定了"怎么打开"：
#:   browser  浏览器   → 用视频链接（href）打开，并在那个窗口里监视播放
#:   client   客户端   → 优先取它的卡片 href 交给浏览器；客户端内部点击做不到
#:   local    本地播放器 → 需要文件路径，用 "播放器.exe 文件" 打开
#:
#: 为什么必须按**可执行文件路径**认，而不是窗口类名：哔哩哔哩客户端与
#: 某个 AI 客户端都是 Electron、窗口类名都是 `Chrome_WidgetWin_1`（坑 39），
#: 只看类名会挑错窗口。
VIDEO_APP_TABLE: dict[str, tuple[str, str]] = {
    "msedge.exe": ("Microsoft Edge", "browser"),
    "chrome.exe": ("Google Chrome", "browser"),
    "firefox.exe": ("Firefox", "browser"),
    "brave.exe": ("Brave", "browser"),
    "opera.exe": ("Opera", "browser"),
    "vivaldi.exe": ("Vivaldi", "browser"),
    "360se.exe": ("360 安全浏览器", "browser"),
    "360chrome.exe": ("360 极速浏览器", "browser"),
    "qqbrowser.exe": ("QQ 浏览器", "browser"),
    "sogouexplorer.exe": ("搜狗浏览器", "browser"),
    "哔哩哔哩.exe": ("哔哩哔哩客户端", "client"),
    "bilibili.exe": ("哔哩哔哩客户端", "client"),
    "douyin.exe": ("抖音客户端", "client"),
    "qqlive.exe": ("腾讯视频", "client"),
    "qiyiplayer.exe": ("爱奇艺", "client"),
    "iqiyi.exe": ("爱奇艺", "client"),
    "youkudesktop.exe": ("优酷", "client"),
    "potplayermini64.exe": ("PotPlayer", "local"),
    "potplayermini.exe": ("PotPlayer", "local"),
    "potplayer.exe": ("PotPlayer", "local"),
    "vlc.exe": ("VLC", "local"),
    "mpv.exe": ("mpv", "local"),
    "mpc-hc64.exe": ("MPC-HC", "local"),
    "mpc-be64.exe": ("MPC-BE", "local"),
    "qqplayer.exe": ("QQ 影音", "local"),
    "tmph.exe": ("迅雷影音", "local"),
}


def classify_video_app(proc: str, path: str = "") -> tuple[str, str]:
    """
    判断某个进程属于哪一类视频来源，返回 (显示名, 类别)。

    类别：browser / client / local / unknown。
    先看可执行文件路径，再看进程名（中文进程名如「哔哩哔哩.exe」在个别接口
    里可能拿不到路径，所以要兜一下）。
    """
    names: list[str] = []
    exe = os.path.basename(path or "").lower()
    if exe:
        names.append(exe)
    p = (proc or "").lower().strip()
    if p:
        names.append(p if p.endswith(".exe") else p + ".exe")
    for n in names:
        if n in VIDEO_APP_TABLE:
            return VIDEO_APP_TABLE[n]
    for n in names:
        for key, val in VIDEO_APP_TABLE.items():
            if key and key in n:
                return val
    if any(n in BROWSER_PROCESSES for n in names):
        return ("浏览器", "browser")
    return ("其他程序", "unknown")
def browser_page_hints(hwnd: int, limit: int = 1500) -> dict:
    """
    从**浏览器窗口**里读"这个页面是什么"的线索（UIA，纯标准库）。

    为什么不能只看标题（当时真环境实测）：
      · 标题里可能夹**零宽字符**（Edge 的 `Microsoft\\u200b Edge`）；
      · 多标签时标题会变成「<当前标签> 和另外 2 个页面 - 个人 - Microsoft Edge」；
      · 而且标题里根本不一定有 "bilibili" 字样（视频页标题是视频名 + 频道后缀）。
    但 UIA 暴露的页面元素里有**真实链接**（实测读到
    `https://space.bilibili.com/...`、`https://live.bilibili.com/...`），
    标签栏本身也是 UIA 元素（名字就是标签标题）—— 这些比标题硬得多。

    返回 {"urls": [...], "video_url": "", "bilibili": bool}；
    读不到（没有 UIA / 不是浏览器）就返回空结果，不抛异常。
    """
    import re
    out = {"urls": [], "video_url": "", "bilibili": False}
    if not hwnd:
        return out
    client = UiaClient()
    if not client.available:
        client.release()
        return out
    urls: list[str] = []
    els = client.elements(hwnd)
    try:
        for i, el in enumerate(els):
            if i >= limit:
                break
            try:
                name = client._prop_str(el, UIA_NamePropertyId) or ""
            except Exception:
                continue
            if not name:
                continue
            low = name.lower()
            if "bilibili" in low or "哔哩哔哩" in name:
                out["bilibili"] = True
            for u in re.findall(r"https?://[^\s\"'<>）)]+", name):
                urls.append(u)
            if not out["video_url"]:
                m = re.search(r"(BV[0-9A-Za-z]{10})", name)
                if m and ("video" in low or "bilibili" in low):
                    out["video_url"] = f"https://www.bilibili.com/video/{m.group(1)}"
    except Exception:
        pass
    finally:
        # 用完就还：元素引用 + 这个客户端持有的 IUIAutomation / COM 初始化
        client.release_elements(els)
        client.release()
    seen = set()
    for u in urls:
        key = u.split("?")[0]
        if key not in seen:
            seen.add(key)
            out["urls"].append(u)
    out["urls"] = out["urls"][:20]
    return out
def close_window_and_verify(hwnd: int, timeout: float = 4.0,
                            poll: float = 0.15) -> tuple[bool, str]:
    """
    关闭一个窗口，并**核对它真的消失了**。

    与 `stop_video_in_window(mode="close")`（Ctrl+W）的区别：
    这里是 `WM_CLOSE`，等价于点窗口右上角的 ×，
    **不需要窗口在前台、也不需要键盘注入** —— 因此在本环境里比合成按键可靠得多，
    而且"关没关掉"可以直接查（`IsWindow`），不用猜。
    """
    if not hwnd or not user32.IsWindow(w.HWND(hwnd)):
        return True, "窗口已经不在了"
    ok, msg = graceful_close_window(hwnd)
    if not ok:
        return False, msg
    deadline = time.time() + max(0.0, timeout)
    while time.time() < deadline:
        if not user32.IsWindow(w.HWND(hwnd)):
            return True, "已关闭当前窗口（核对：窗口已消失）"
        time.sleep(max(0.02, poll))
    return False, (f"已发送关闭请求，但 {timeout:.0f}s 后窗口仍在"
                   f"（可能弹出了“确认关闭”对话框，需要手动确认）")


def probe_window_as_video(wi: "WinInfo", probe_seconds: float = 2.0,
                          require_video_app: bool = False,
                          only_app: str = "") -> dict:
    """
    判断**这个窗口**是不是在放视频 —— 「监视当前窗口」的核心。

    为什么要它：原来的「打开视频并监视播放」只认哔哩哔哩客户端，
    而客户端里的卡片又点不动（真实输入被拦），等于两头不通。
    现在反过来：**你选中哪个窗口，程序就判断它是不是在放视频**，
    是就开始监视 —— 不管它是视频客户端、浏览器，还是本地播放器。

    过滤规则对应界面上的两个选项（都不勾 = 任何窗口都认）：
      require_video_app=True → 不是已知视频类 App 的窗口，一律不判为视频；
      only_app 非空          → 只认这一个进程（例如只认 msedge.exe）。

    判定为视频的条件（任一成立）：
      · 画面在持续变化 / 有音频输出（`VideoActivityDetector`，主要依据）
      · 标题是**明确的播放页特征**（视频名 + 播放页后缀，或带 BV 号）
      · 页面里有视频链接（UIA 读到的 href）
    返回 {"is_video", "reason", "app", "kind", "proc",
          "playing", "detail"}，`reason` 是给人看的依据（可直接回显到状态栏）。
    """
    proc = (wi.proc or "").lower()
    if not proc:
        try:
            proc = (snapshot_processes().get(wi.pid, {}).get("name") or "").lower()
        except Exception:
            proc = ""
    path = wi.path or ""
    if not path:
        try:
            path = query_process_detail(wi.pid)[0] or ""
        except Exception:
            path = ""
    app, kind = classify_video_app(proc, path)
    kind_label = {"browser": "浏览器", "client": "视频客户端",
                  "local": "本地播放器"}.get(kind, "其他程序")
    title = wi.title or ""
    strength = video_title_strength(title)

    def result(is_video: bool, reason: str, **extra) -> dict:
        base = {"is_video": is_video, "reason": reason, "app": app, "kind": kind,
                "proc": proc, "playing": False,
                "detail": {"title": title, "strength": strength, "hints": {},
                           "detector": {}, "evidence": []}}
        base.update(extra)
        return base

    # ---- 两个界面选项先过滤 ----
    if only_app and proc != only_app.lower():
        return result(False, f"这个窗口不是指定的 App（当前只响应 {only_app}）")
    if require_video_app and kind == "unknown":
        return result(False, f"「{app}」不是已知的视频类 App（已勾选「仅视频类 App」）")

    evidence = [f"来源类型：{kind_label}"]
    if strength == "strong":
        evidence.append("标题是明确的播放页特征")
    elif strength == "weak":
        evidence.append("标题像视频名（启发式）")
    hints: dict = {}
    if kind in ("browser", "client"):
        try:
            hints = browser_page_hints(wi.hwnd)
        except Exception:
            hints = {}
        if hints.get("video_url"):
            evidence.append(f"页面里有视频链接 {hints['video_url'].split('?')[0]}")
        elif hints.get("bilibili"):
            evidence.append("页面里是哔哩哔哩内容")

    det = VideoActivityDetector(wi)
    try:
        playing, info = det.is_playing(probe_seconds)
    except Exception as exc:
        playing, info = False, {"reason": f"检测失败：{type(exc).__name__}: {exc}"}
    finally:
        det.close()
    if playing:
        evidence.append(info.get("reason", ""))

    is_video = bool(playing or strength == "strong" or hints.get("video_url"))
    if is_video:
        reason = "判定为视频：" + "；".join(x for x in evidence if x)
    else:
        reason = (f"没看出这个窗口在放视频（{info.get('reason', '')}）"
                  f"；来源类型：{kind_label}")
    out = result(is_video, reason)
    out["playing"] = playing
    out["detail"].update({"hints": hints, "detector": info, "evidence": evidence})
    return out
def restore_window(wi: "WinInfo", activate: bool = True) -> tuple[bool, str]:
    """
    把最小化的窗口**唤起**（恢复显示），可选再切到前台。

    "在程序内唤起最小化的 App"就是它。为什么监视前需要：
    窗口最小化（或被隐藏到托盘）时 `PrintWindow` 抓不到真画面，
    播放判定会一直是"画面静止"，监视等于白跑。
    """
    hwnd = w.HWND(wi.hwnd)
    if not user32.IsWindow(hwnd):
        return False, "窗口已经不在了"
    restored = False
    try:
        if user32.IsIconic(hwnd):
            user32.ShowWindow(hwnd, SW_RESTORE)
            restored = True
        else:
            user32.ShowWindow(hwnd, SW_SHOW)      # 被隐藏的（托盘）也拉回来
        user32.BringWindowToTop(hwnd)
    except Exception as exc:
        return False, f"唤起失败：{type(exc).__name__}: {exc}"
    front = False
    if activate:
        try:
            front = WindowKeySender(wi.hwnd).focus()
        except Exception:
            front = False
    if restored:
        return True, f"已把最小化的窗口唤起{'并切到前台' if front else '（未能切到前台）'}"
    return True, f"窗口本来就可见{'，已切到前台' if front else ''}"


# ============================================================================
# 八点七、可清理文件（程序自己产生的，清理功能用）
# ============================================================================
#
# 设计原则：**只清理"程序自己产生的"文件**，而且把"用户数据"单独标出来。
# 「清理」最容易出的事就是"顺手把用户的收藏和分类删了"，所以
# `winmon_settings.json`（收藏 / 手动分类 / 最近使用 / 界面偏好）默认**不勾选**，
# 并且在界面上写明风险；其余都是删了会自动重建、或本来就没用的东西。

#: (key, 显示名, 相对路径, 默认勾选, 说明, 去哪个目录找)
#:
#: 最后一项是 2026-10-16 加的：从那以后"运行时的数据"不再写在程序目录里
#: （见 `data_dir()`），所以清理清单也得跟着分家：
#:   · "data"  = 运行时数据目录（`data_dir()`）—— 缓存 / 日志 / 快照 / 设置；
#:   · "app"   = 程序目录（`APP_DIR`）—— 字节码缓存必须待在源旁边，解释器认这个位置；
#:   · "other" = 特殊来源（系统临时目录里的导出记录）。
CLEANABLE_ITEMS: tuple[tuple[str, str, str, bool, str, str], ...] = (
    ("catalog_cache", "程序目录缓存", "winmon_catalog.json", True,
     "删掉后下次重新扫描系统（约 0.4 秒）；不影响任何设置", "data"),
    ("error_log", "错误日志", "winmon_error.log", True,
     "只在启动失败时生成；删掉不影响运行", "data"),
    ("pycache", "字节码缓存（主程序）", "__pycache__", True,
     "解释器会自动重建；删掉不影响运行", "app"),
    ("tests_pycache", "字节码缓存（测试）", os.path.join("tests", "__pycache__"), True,
     "解释器会自动重建；删掉不影响运行", "app"),
    ("temp_exports", "导出的操作记录（系统临时目录）", "", True,
     "「导出操作记录」生成的 winmon_history_*.txt", "other"),
    ("media_snapshot", "媒体会话快照", "winmon_media.jsonl", True,
     "常驻媒体助手每 3 秒写一行的运行缓存；删掉不影响运行，下一次写入会自动重建",
     "data"),
    ("history_log", "操作历史日志", "winmon_history.log", False,
     "**这是留痕记录**（谁在什么时候被关闭/结束了）：删掉就没了。"
     "只想清掉界面上的列表请用「操作历史…」窗口里的「清空列表」", "data"),
    ("settings", "界面偏好 + 收藏 / 最近使用", "winmon_settings.json", False,
     "**这是你的用户数据**：删掉会丢收藏、手动分类、最近使用与界面偏好", "data"),
)


def list_cleanable_files(root: str = "", app_root: str = "") -> list[dict]:
    """
    列出程序产生的**可清理文件**（含实际大小、文件数与风险说明）。

    两个只给测试用的覆盖参数（真实运行一律不传）：
      · `root`     —— 覆盖"运行时数据目录"（缓存 / 日志 / 快照 / 设置在这里）；
      · `app_root` —— 覆盖"程序目录"（字节码缓存那一项在这里；解释器只认
        源旁边那个 `__pycache__`，所以它没跟着数据一起搬走）。

    返回 [{key, label, note, default, paths, count, bytes, exists}]。
    界面直接照着这份清单渲染，**不会删清单以外的东西**。
    """
    import glob
    import tempfile
    roots = {"data": root or data_dir(), "app": app_root or APP_DIR, "other": ""}
    out: list[dict] = []
    for key, label, rel, default, note, where in CLEANABLE_ITEMS:
        base = roots.get(where, "")
        paths: list[str] = []
        if key == "temp_exports":
            paths = sorted(glob.glob(os.path.join(tempfile.gettempdir(),
                                                  "winmon_history_*.txt")))
        elif key == "history_log":
            # 日志 + 轮转出来的备份一起列（用户点这一项就是想清干净留痕）
            for cand in (os.path.join(base, HISTORY_LOG_NAME),
                         os.path.join(base, HISTORY_LOG_NAME + ".1")):
                if os.path.exists(cand):
                    paths.append(cand)
        elif rel and base:
            target = os.path.join(base, rel)
            if os.path.isdir(target):
                for base_dir, _dirs, files in os.walk(target):
                    paths += [os.path.join(base_dir, f) for f in files]
            elif os.path.exists(target):
                paths.append(target)
        size = 0
        for p in paths:
            try:
                size += os.path.getsize(p)
            except OSError:
                pass
        out.append({"key": key, "label": label, "note": note, "default": default,
                    "paths": paths, "count": len(paths), "bytes": size,
                    "exists": bool(paths)})
    return out


def clean_files(keys, root: str = "", app_root: str = "") -> tuple[int, int, list[str]]:
    """
    清理指定的项（key 来自 `CLEANABLE_ITEMS`）。

    **只删清单里解析出来的路径** —— 不接受任意路径参数，避免"清理"变成删错东西。
    `root` / `app_root` 与 `list_cleanable_files()` 同义（只给测试用）。
    返回 (删除的文件数, 释放的字节数, 失败说明列表)。
    """
    wanted = {str(k) for k in keys}
    items = {d["key"]: d for d in list_cleanable_files(root, app_root)}
    removed = freed = 0
    failures: list[str] = []
    for key in wanted:
        item = items.get(key)
        if item is None:
            failures.append(f"未知清理项：{key}")
            continue
        for p in item["paths"]:
            try:
                size = os.path.getsize(p)
            except OSError:
                size = 0
            try:
                os.remove(p)
                removed += 1
                freed += size
            except OSError as exc:
                failures.append(f"{os.path.basename(p)}：{exc}")
        # 目录型（字节码缓存）：文件删完后把空目录也收掉。
        # 它固定待在**程序目录**（解释器只认源旁边那个 `__pycache__`），
        # 所以这里不能用 `root`（那是"运行时数据目录"的覆盖值）。
        if key in ("pycache", "tests_pycache"):
            rel = dict((k, r) for k, _l, r, _d, _n, _w in CLEANABLE_ITEMS)[key]
            target = os.path.join(app_root or APP_DIR, rel)
            try:
                if os.path.isdir(target):
                    for base, dirs, _files in os.walk(target, topdown=False):
                        for d in dirs:
                            os.rmdir(os.path.join(base, d))
                    os.rmdir(target)
            except OSError as exc:
                failures.append(f"目录 {os.path.basename(target)}：{exc}")
    return removed, freed, failures


# ============================================================================
# 八点九、闲置程序（"开着不用、白耗电"的识别）
# ============================================================================
#
# 用户要解决的事：某个程序长期开着、又没人用，白占内存和电。
# 这里只做**判定**（纯逻辑，可单测），关不关由界面层决定。
#
# 判据是三个"在干活"的信号，**任一成立就重新计时**：
#   1. 它的窗口是前台窗口（你正在用它）
#   2. 它的 CPU 占用高于阈值（它在算东西）
#   3. 它在放声音（音频会话有输出）
# 三个都不成立才累加"闲置时长"，越过阈值才算候选。
#
# 为什么不做成"看 CPU 就够"：一个停在视频页的播放器 CPU 可能是 0，
# 反过来一个后台索引器 CPU 很高但窗口从不出现，
# 那也是"在用"，不该动。

#: CPU 占用高于这个百分比就算"在干活"（按整机核数归一化后的值）
IDLE_CPU_BUSY_PERCENT = 1.0

#: 闲置多久算"长期不用"（界面上的下拉选项，第一个是默认值）
IDLE_CHECK_CHOICES: tuple[tuple[str, int], ...] = (
    ("30 分钟", 30 * 60), ("1 小时", 60 * 60), ("2 小时", 2 * 60 * 60),
    ("4 小时", 4 * 60 * 60),
)
IDLE_DEFAULT_SECONDS = IDLE_CHECK_CHOICES[0][1]

#: 提醒过一次之后，隔多久才允许**自动**再弹窗（手动点「闲置清单」不受它限制）。
#: 为什么需要：多个程序会在不同时刻陆续越过阈值，不节流的话你刚关掉一个清单、
#: 过几分钟又被另一个弹一次；一个长期挂着的程序在「稍后再说」之后也会周期性再弹。
#:
#: 取值有**三种**语义，别只看数值大小：
#:   · > 0        —— 间隔秒数（`4 小时`）
#:   · IDLE_SNOOZE_RUN_ONCE (-1) —— 本次运行只提醒一次
#:   · IDLE_SNOOZE_DAILY (0)     —— **每个自然日只提醒一次**（跨过零点才算新的一天）
IDLE_SNOOZE_CHOICES: tuple[tuple[str, float], ...] = (
    ("30 分钟", 1800.0), ("1 小时", 3600.0), ("2 小时", 7200.0),
    ("4 小时", 14400.0), ("每天只提醒一次", 0.0), ("本次运行只提醒一次", -1.0),
)
IDLE_SNOOZE_DEFAULT = 3600.0

#: "每个自然日只提醒一次"的哨兵值。为什么不能用固定秒数（比如 86400）代替：
#: 用户说的"每天一次"是**日历意义上的每一天** —— 早上 9 点提醒过，第二天早上 9 点
#: 之前就该能再提醒一次，而不是"必须满 24 小时"。固定间隔在早上 8 点提醒过之后，
#: 第二天 8 点前什么都不说，第二天一整天也可能只提醒一次（取决于起始时刻），
#: 与用户的直觉对不上。所以这一档按**日期**判定，不看秒数。
IDLE_SNOOZE_DAILY = 0.0
#: "本次运行只提醒一次"的哨兵值。
IDLE_SNOOZE_RUN_ONCE = -1.0

#: 闲置清理**绝不**碰的进程。在「关键系统进程」之外，再排除一批
#: "关掉了会让整个桌面很难受、但又不是系统关键进程"的东西
#: （外壳、开始菜单、搜索、输入法、桌面窗口管理器）。
IDLE_PROTECTED_PROCESSES = CRITICAL_PROCESSES | {
    "explorer.exe", "shellexperiencehost.exe", "startmenuexperiencehost.exe",
    "searchhost.exe", "searchapp.exe", "textinputhost.exe", "ctfmon.exe",
    "dwm.exe", "sihost.exe", "taskhostw.exe", "runtimebroker.exe",
}


def idle_threshold_label(seconds: int) -> str:
    """把秒数换回下拉框里的中文标签（认不出来就给个「N 分钟」）。"""
    for label, value in IDLE_CHECK_CHOICES:
        if int(seconds) == value:
            return label
    return f"{max(1, int(seconds)) // 60} 分钟"


def idle_snooze_label(seconds: float) -> str:
    """把"节流间隔"的秒数换回下拉框里的中文标签。"""
    for label, value in IDLE_SNOOZE_CHOICES:
        if float(seconds) == value:
            return label
    return IDLE_SNOOZE_CHOICES[1][0]


def idle_snooze_seconds_of(label: str) -> float:
    """下拉框中文 → 节流间隔秒数（认不出来就用默认值）。"""
    for text, value in IDLE_SNOOZE_CHOICES:
        if text == label:
            return float(value)
    return IDLE_SNOOZE_DEFAULT


def idle_snooze_hint(seconds: float) -> str:
    """
    "被节流时"状态栏上那句说明 —— 按节流方式给不同说法。

    为什么不能直接拼 `自动提醒间隔 {标签}`：两个新档位根本不是"间隔"
    （`每天只提醒一次` / `本次运行只提醒一次`），拼出来读着别扭、也没说清
    "什么时候会再提醒"。这里把三种语义分开说，用户不用猜。
    """
    value = float(seconds)
    if value == IDLE_SNOOZE_DAILY:
        return "今天不再自动弹窗（明天起再提醒）"
    if value < 0:
        return "本次运行不再自动弹窗"
    return f"自动提醒间隔 {idle_snooze_label(value)}"


def sample_cpu_percent(pids, seconds: float = 1.0) -> dict[int, float]:
    """
    对一组 pid 采两次 CPU 时间，算出占用百分比（按整机核数归一化）。

    独立的采样函数：界面上那份是"跟着刷新周期滚动的"，命令行做一次性判定时
    需要自己采。`seconds` 太小时精度会差（CPU 时间粒度约 15.6ms），所以别小于 0.2。
    """
    pids = [int(p) for p in pids if p]
    if not pids or seconds <= 0:
        return {}
    nproc = max(1, os.cpu_count() or 1)
    first = {p: query_process_cpu_time(p) for p in pids}
    time.sleep(seconds)
    out: dict[int, float] = {}
    for p in pids:
        a, b = first.get(p), query_process_cpu_time(p)
        if a is None or b is None:
            out[p] = 0.0
            continue
        out[p] = max(0.0, min(999.0, (b - a) / seconds / nproc * 100.0))
    return out


#: 自己人：winmon1.0 主窗口的标题前缀（`winmon1.0`）。
#: 要排掉"自己"的地方不能只比 pid：**同一个程序可能开着不止一个实例**
#: （界面在跑、命令行又在判定；或者用户就开了两个），那些实例的 pid 当然不等于本进程。
WINMON_TITLE_PREFIX = "winmon1.0"


def is_own_window(title: str, pid: int = 0, mine: int = 0) -> bool:
    """
    这个窗口是不是 WinMon 自己的（本进程，**或者本机另一个 WinMon 实例**）？

    只比 pid 会漏掉后者 —— 实测踩到过两次：`--idle` 把运行中的 WinMon 列成了
    "闲置程序"，工作区快照也把它拍进了布局（那样"恢复布局"会挪动另一个 WinMon 的窗口）。
    """
    if pid and mine and int(pid) == int(mine):
        return True
    return str(title or "").startswith(WINMON_TITLE_PREFIX)


# ---- 单实例（C.1）----
#
# 为什么用**命名互斥体**而不是"枚举窗口找自己"：窗口枚举会漏（窗口可能还没建出来、
# 或者被隐藏到托盘里），而互斥体是内核对象 —— 只要那个进程还活着，第二次
# `CreateMutexW` 就会返回 `ERROR_ALREADY_EXISTS`，判据非常硬。
# 名字放 `Local\` 命名空间：**同一登录会话内**才算重复，多用户/多会话互不干扰。
WINMON_MUTEX_NAME = "Local\\WinMon-1.0-single-instance"
_single_instance_handle = 0

# 显式声明这几个 API：不声明的话 ctypes 会拿 Python str 去猜参数类型，
# 名字里的反斜杠一旦被当成字节串就会得到 `ERROR_INVALID_NAME (123)`
# （实测：名字写成 `Local\\\\WinMon` 时真的返回 0 句柄 + 错误 123）。
try:
    kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    kernel32.SetLastError.argtypes = [ctypes.c_ulong]
    kernel32.GetLastError.restype = ctypes.c_ulong
    kernel32.ReleaseMutex.argtypes = [ctypes.c_void_p]
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
except Exception:
    pass


def claim_single_instance(name: str = "") -> tuple[bool, str]:
    """
    抢占"本机只有一个 WinMon"的名额。

    返回 `(是否抢到, 说明)`：
      · 抢到 → 说明当前是**第一个**实例，句柄由本模块持有到进程结束；
      · 没抢到 → 说明**已经有 WinMon 在跑**，调用方应当把已有窗口带到前台然后退出。

    任何失败（极少数环境不让建内核对象）都按"抢到"处理 —— **宁可多开，也不要
    因为一个辅助机制把程序拦在门外**。
    """
    global _single_instance_handle
    mutex_name = name or WINMON_MUTEX_NAME
    try:
        kernel32.SetLastError(0)
        handle = kernel32.CreateMutexW(None, True, mutex_name)
        last = kernel32.GetLastError()
        if not handle:
            return True, f"CreateMutexW 失败（错误 {last}）—— 按允许启动处理"
        if last == 183:                      # ERROR_ALREADY_EXISTS
            kernel32.CloseHandle(handle)
            return False, "已经有一个 WinMon 在运行"
        _single_instance_handle = int(handle)
        return True, "本进程是唯一实例"
    except Exception as exc:
        return True, f"{type(exc).__name__}: {exc}（按允许启动处理）"


def release_single_instance() -> None:
    """退出前放开名额（进程结束本来也会自动放开，这里显式做一次更干净）。"""
    global _single_instance_handle
    handle, _single_instance_handle = _single_instance_handle, 0
    if handle:
        try:
            kernel32.ReleaseMutex(handle)
            kernel32.CloseHandle(handle)
        except Exception:
            pass


def bring_existing_winmon_front() -> tuple[bool, str]:
    """
    找**已经在跑的那个 WinMon** 窗口并把它带到前台（C.1 用）。

    从托盘里藏着的实例也能找到（`enum_windows(include_cloaked=True)` 会列出隐藏窗口），
    找到后**先还原再切前台**；找不到就如实说。
    """
    mine = os.getpid()
    best = None
    for wi in enum_windows(include_cloaked=True):
        if not str(wi.title or "").startswith(WINMON_TITLE_PREFIX):
            continue
        if wi.pid == mine:
            continue
        if best is None or (wi.rect_w * wi.rect_h) > (best.rect_w * best.rect_h):
            best = wi
    if best is None:
        return False, "没找到正在运行的 WinMon 窗口（可能它刚退出）"
    try:
        if best.iconic or best.is_hidden_surface:
            restore_window(best, activate=False)
        ok, msg = activate_window(best)
        return bool(ok), msg or f"已切换到正在运行的 WinMon（PID {best.pid}）"
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"


def idle_samples(cpu_by_pid: dict | None = None, fg_hwnd: int = 0,
                 include_hidden: bool = False,
                 procs: dict | None = None, windows=None) -> dict[int, dict]:
    """
    整理出 `IdleWatcher` 要的观测值：`{pid: {...}}`。

    作用范围（用户确认过）：**只包含有可见窗口的程序**。
    托盘/服务这类没有窗口的后台进程一律不参与。

    P2 优化：`procs` / `windows` 可由调用方传入 —— GUI 的刷新周期里刚枚举过一遍
    （refresh_processes / refresh_windows），别在同一拍里再枚举第二次；
    不传则自己枚举（命令行入口 idle_cli 仍走这条）。
    同一进程有多个窗口时合并成一条（有任一个窗口是前台就算"在用"）。
    """
    procs = snapshot_processes() if procs is None else procs
    fg = fg_hwnd or get_foreground_hwnd()
    cpu_by_pid = cpu_by_pid or {}
    mine = os.getpid()
    out: dict[int, dict] = {}
    wins = enum_windows(include_cloaked=True) if windows is None else windows
    for wi in wins:
        if wi.is_hidden_surface and not include_hidden:
            continue
        if is_own_window(wi.title, wi.pid, mine):
            continue                      # 自己（含本机其它 WinMon 实例）
        name = procs.get(wi.pid, {}).get("name") or wi.proc or f"PID {wi.pid}"
        if name.lower() in IDLE_PROTECTED_PROCESSES:
            continue
        entry = out.get(wi.pid)
        if entry is None:
            out[wi.pid] = {
                "name": name,
                "title": wi.title or "",
                "hwnd": wi.hwnd,
                "foreground": (wi.hwnd == fg),
                "cpu": float(cpu_by_pid.get(wi.pid, 0.0)),
                "iconic": bool(wi.iconic),
            }
        else:
            # 同进程多窗口：前台取"或"，标题取第一个非空的
            entry["foreground"] = entry["foreground"] or (wi.hwnd == fg)
            if not entry["title"] and wi.title:
                entry["title"] = wi.title
            if entry["iconic"] and not wi.iconic:
                entry["iconic"] = False
                entry["hwnd"] = wi.hwnd
    return out


class IdleWatcher:
    """
    跟踪"每个程序连续多久没被使用" —— **纯逻辑**，不碰 Win32，所以能直接单测。

    用法：定时调 `observe(dt, samples)`，它会返回**这一拍刚好越过阈值**的 pid
    （每个程序只在越线时返回一次，避免反复弹窗）；界面再用 `candidates()` 取清单。
    """

    def __init__(self, threshold: float = IDLE_DEFAULT_SECONDS,
                 busy_cpu: float = IDLE_CPU_BUSY_PERCENT,
                 snooze: float = IDLE_SNOOZE_DEFAULT) -> None:
        self.threshold = float(threshold)
        self.busy_cpu = float(busy_cpu)
        self.snooze = float(snooze)
        self.idle: dict[int, float] = {}
        self.meta: dict[int, dict] = {}
        self.fired: set[int] = set()
        #: 上次**因为闲置而自动弹窗**的时刻（单调时钟秒）。间隔式节流就靠它。
        self.last_notice = 0.0
        #: 结构化节流用：上次"已经通知过"是在哪个自然日（`YYYY-MM-DD`；空 = 还没通知过）。
        #: 为什么要两个日期字段：`每天只提醒一次` 要区分两种"今天已经通知过" ——
        #:   · `last_notice_day`：今天已经给过交代（**状态栏提示也算**）→ 当天不再弹窗。
        #:     它同时也是"跨零点那一拍"的判据：昨天提示过不算今天提示过。
        #:   · `last_popup_day` ：今天真的弹过窗 → 当天不再弹第二次（独立一道闸门：
        #:     万一将来有人改错了上面那条，也不会退化成"反复弹窗"）。
        #: 用**日期字符串**而不是时间戳，是因为本模块刻意不依赖墙上时钟：
        #: 日期由界面层算好传进来（`winmon._local_day()`），这里保持纯逻辑、可确定性单测。
        self.last_notice_day = ""
        self.last_popup_day = ""

    # ---- 配置 ----

    def set_threshold(self, seconds: float) -> None:
        """
        改时长。**不清空已累计的闲置时间**（那对用户是"我明明等了半小时，怎么又从头算"），
        但会清掉"已提醒过"的标记，好让立刻满足新阈值的那批能重新被提示。
        """
        self.threshold = float(seconds)
        self.fired.clear()

    def set_snooze(self, seconds: float) -> None:
        """
        改"提醒后隔多久才再自动弹窗"。

        只有**真的换了档**才清掉"今天交代过/弹过"的日期记录，而且保留单调时钟那份
        `last_notice`。两条都是踩出来的：

          · 界面每 2 秒就会把下拉框当前值推一次（`_check_idle` 里），所以"每次都清"
            等于把每日档的节流整个取消 —— 第二拍又会弹一次窗，实测就是这个现象；
          · 从「每天只提醒一次」换回「30 分钟」时若留着日期记录，用户会看到
            "我刚改成 30 分钟，它却说今天不再弹"；改档是明确意图，应当立刻按新档位生效；
          · 但清零 `last_notice` 会让换档瞬间直接弹一次（间隔没满也不管），
            那就成了"改设置 = 打扰你一次"。留着它，换档后按新间隔正常计时。
        """
        new = float(seconds)
        if new != self.snooze:
            self.last_notice_day = ""
            self.last_popup_day = ""
        self.snooze = new

    # ---- 提醒节流（纯时间计算，`now` / `today` 由调用方给，所以能确定性单测） ----

    def may_notify(self, now: float, today: str = "", dialog_open: bool = False) -> bool:
        """
        现在允许**自动**弹窗吗？

        三种节流语义分开处理（哨兵值见 `IDLE_SNOOZE_DAILY` / `IDLE_SNOOZE_RUN_ONCE`）：

          · 间隔式（`snooze > 0`）：
            `last_notice == 0` = **还从来没提醒过** → 任何时候都该允许。
            不能写成 `now - last_notice >= snooze`：那样在一个"刚启动"的程序里
            （`now` 还很小）第一次提醒会被自己挡掉，属于典型的"用哨兵值当时间戳"的坑。
            提醒过之后 → 看间隔够不够。
          · `本次运行只提醒一次`（`snooze < 0`）→ 弹过就永不再自动弹。
          · `每天只提醒一次`（`snooze == IDLE_SNOOZE_DAILY`）→ 按**自然日**判定：
            日期变了才允许再弹，而且**当天弹过就不再弹**（`last_popup_day` 记着）。
            跨零点那一拍放行，条件里带的 `last_popup_day != today` 正是防这个：
            昨天弹过不算今天弹过。反过来，只是状态栏提示过（没弹窗）不算数 ——
            否则跨零点时那句"今天不再自动弹窗"会和新一天的弹窗撞在一起。
            没给 `today` 时一律不放行 —— 宁可少弹一次，也不要"以为拦截了其实没有"。

        `dialog_open`：清单已经开着时**不**抢第二遍（否则新一波越线会在旧清单上再叠一个）。
        手动点「闲置清单」按钮不走这个判断 —— 用户主动要看，就该给他看。
        """
        if self.snooze == IDLE_SNOOZE_DAILY:
            if not today or dialog_open:
                return False
            fresh_day = (self.last_notice_day != today)
            popped_today = (self.last_popup_day == today)
            return fresh_day and not popped_today
        if dialog_open:
            return False
        if self.last_notice == 0.0:
            return True
        if self.snooze < 0:
            return False
        return (now - self.last_notice) >= self.snooze

    def note_notified(self, now: float, today: str = "") -> None:
        """
        记下"刚刚通知过"（状态栏提示与弹窗都算），用于节流。

        `today` 只在「每天只提醒一次」那档有意义：它把这一天记成"已经交代过了"。
        """
        self.last_notice = float(now)
        if today:
            self.last_notice_day = str(today)

    def note_popup(self, today: str = "") -> None:
        """记下"刚刚真的弹过窗" —— 「每天只提醒一次」靠它区分"弹过"和"只提示过"。"""
        if today:
            self.last_popup_day = str(today)

    # ---- 观测 ----

    def observe(self, dt: float, samples: dict) -> list[int]:
        """
        推进 `dt` 秒。返回这一拍**刚越过阈值**的 pid 列表。

        `samples` 里没有出现的 pid 视为已经退出，直接忘掉。
        """
        newly: list[int] = []
        if dt > 0:
            for pid, s in samples.items():
                self.meta[pid] = {"name": s.get("name", ""), "title": s.get("title", ""),
                                  "hwnd": s.get("hwnd", 0), "iconic": bool(s.get("iconic"))}
                # 判"在用"只有两个信号：**在前台** 或 **CPU 超过阈值**。
                # 2026-10-13 删掉了第三个"在放声音"——那个 API 在本机恒为 0
                # （见 `enum_audio_sessions` 的删除说明），等于没有。
                # 取舍：放歌的播放器 CPU 通常有几个百分点，`busy_cpu` 能挡住它；
                # 万一遇到"放着歌却 CPU 为 0"的播放器，它会出现在闲置清单里，
                # 但**关闭仍需用户确认**（默认只提醒），不会自己动手。
                busy = bool(s.get("foreground")) or \
                    float(s.get("cpu") or 0.0) > self.busy_cpu
                if busy:
                    self.idle[pid] = 0.0
                    self.fired.discard(pid)
                    continue
                self.idle[pid] = self.idle.get(pid, 0.0) + dt
                if self.idle[pid] >= self.threshold and pid not in self.fired:
                    self.fired.add(pid)
                    newly.append(pid)
        self.forget(set(samples))
        return newly

    def forget(self, alive) -> None:
        """把已经不在的进程忘掉（否则闲置时长会一直挂在内存里）。"""
        alive = {int(p) for p in alive}
        for pid in [p for p in self.idle if p not in alive]:
            self.idle.pop(pid, None)
            self.meta.pop(pid, None)
            self.fired.discard(pid)

    def reset(self, pids=None) -> None:
        """把闲置时长清零（"稍后再说"用：重置了才会重新计时、重新提醒）。"""
        for pid in (list(self.idle) if pids is None else list(pids)):
            self.idle[pid] = 0.0
            self.fired.discard(pid)

    # ---- 查询 ----

    def idle_seconds(self, pid: int) -> float:
        """某个进程已经闲置了多久（没记录就是 0）。"""
        return float(self.idle.get(int(pid), 0.0))

    def candidates(self) -> list[dict]:
        """超过阈值的程序，闲置最久的排前面（界面清单直接用这个）。"""
        rows = []
        for pid, secs in self.idle.items():
            if secs < self.threshold:
                continue
            info = self.meta.get(pid, {})
            rows.append({"pid": pid, "idle": secs, "name": info.get("name", ""),
                         "title": info.get("title", ""), "hwnd": info.get("hwnd", 0),
                         "iconic": info.get("iconic", False)})
        rows.sort(key=lambda r: -r["idle"])
        return rows


def format_idle(seconds: float) -> str:
    """把秒数说成人话：`47 分钟` / `2 小时 5 分钟`。"""
    total = max(0, int(seconds))
    hours, mins = divmod(total // 60, 60)
    if hours:
        return f"{hours} 小时 {mins} 分钟" if mins else f"{hours} 小时"
    if mins:
        return f"{mins} 分钟"
    return f"{total} 秒"


def close_idle_window(hwnd: int, timeout: float = 4.0) -> tuple[bool, str]:
    """
    关掉一个"闲置"程序的窗口：**只发 WM_CLOSE（等价于点它的关闭按钮）**。

    为什么不直接结束进程：这类程序很可能有未保存的内容。
    `WM_CLOSE` 会让它自己弹"要不要保存"，把决定权交回用户；
    如果它没退出（多半正等着你回答），**也不替你强杀** —— 如实回报就好。
    这条和「关闭视频窗口」的约定是一致的（HANDOFF 坑 55）。
    """
    if not hwnd or not user32.IsWindow(w.HWND(hwnd)):
        return False, "窗口已经不在了"
    return close_window_and_verify(hwnd, timeout=timeout)


# ============================================================================
# 八点十、工作区快照（把"一组窗口的位置与最小化状态"存下来，一键恢复）
# ============================================================================
#
# 用户要的是："写代码 / 看视频 / 打游戏"各存一套布局，一键切过去。
#
# 设计上的两个关键取舍：
#   1. **匹配靠"可执行文件名 + 标题"，不靠持久化的 hwnd** ——
#      窗口句柄在下一次运行里必然不同，存下来毫无意义。
#   2. **只做"能对上"的那部分，剩下的如实报告**：某个程序这次没开，
#      不该当成失败，也不该"顺便帮你启动"（那是另一个功能，而且很危险）。

#: 最多存多少个工作区（防止设置文件无限膨胀）
WS_MAX_COUNT = 20
#: 每个工作区最多记多少个窗口
WS_MAX_ENTRIES = 60
#: 快照里的窗口矩形最小可信尺寸：比这个还小的多半是"存的时候正好在动画/被折叠"
WS_MIN_W, WS_MIN_H = 120, 80


def normalize_workspaces(raw) -> list[dict]:
    """
    把设置文件里读出来的工作区数据洗干净（坏数据丢弃、缺字段补默认）。

    为什么要单独一步：设置文件是**用户可编辑的文本**，也可能是旧版本写的。
    直接信它会在界面里炸出一堆 KeyError；这里统一变成"结构一定正确"的列表。
    """
    out: list[dict] = []
    if not isinstance(raw, list):
        return out
    for item in raw:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        entries = []
        for e in item.get("entries") or []:
            if not isinstance(e, dict):
                continue
            rect = e.get("rect")
            if not (isinstance(rect, (list, tuple)) and len(rect) == 4):
                continue
            try:
                rect = [int(v) for v in rect]
            except (TypeError, ValueError):
                continue
            entries.append({
                "name": str(e.get("name") or ""),
                "title": str(e.get("title") or ""),
                "rect": rect,
                "iconic": bool(e.get("iconic")),
            })
        out.append({"name": name, "saved_at": float(item.get("saved_at") or 0.0),
                    "entries": entries[:WS_MAX_ENTRIES]})
    return out[:WS_MAX_COUNT]


def window_record(wi) -> dict:
    """
    把"窗口"统一成 dict —— `plan_workspace_apply` / `capture_workspace` 的入参。

    为什么必须有这一步（**这是真踩到的坑**）：生产代码传进来的是 `WinInfo` 对象，
    它的字段叫 `proc`、**没有 `name`、也没有 `rect`**；而快照里存的是 dict
    （字段叫 `name`、带 `rect`）。第一版在两种形态上各写了一套取值，
    结果**只测了 dict 那一半**：走真实路径时每条记录的可执行文件名都取成了空串，
    全部落进"这个程序没开"，表现为"点应用毫无反应"而且不报任何错。
    统一成一种形态之后，匹配逻辑只需要面对一种数据结构。
    """
    if isinstance(wi, dict):
        getter = (lambda k: wi.get(k))
    else:
        getter = (lambda k: getattr(wi, k, None))
    name = getter("name") or getter("proc") or getter("real_name") or ""
    return {
        "hwnd": int(getter("hwnd") or 0),
        "pid": int(getter("pid") or 0),
        "title": str(getter("title") or ""),
        "name": str(name),
        "iconic": bool(getter("iconic")),
        "rect": getter("rect"),
    }


def capture_workspace(name: str, windows, now: float = 0.0) -> dict:
    """
    把当前的一组窗口拍成快照。

    只保留**尺寸可信**的窗口：正在做动画、或者被折叠成一条的窗口记下来没有意义，
    恢复时反而会把它挪到奇怪的位置。

    没有 `rect` 但有句柄的记录会**当场问系统要一次**（这样直接传 `WinInfo` 也能用）。
    """
    entries = []
    for wi in windows:
        rec = window_record(wi)
        rect = rec.get("rect")
        if not rect and rec["hwnd"]:
            rect = get_window_rect(rec["hwnd"])
        if not rect or len(rect) != 4:
            continue
        x, y, cw, ch = (int(v) for v in rect)
        if cw < WS_MIN_W or ch < WS_MIN_H:
            continue
        entries.append({"name": rec["name"], "title": rec["title"],
                        "rect": [x, y, cw, ch], "iconic": rec["iconic"]})
        if len(entries) >= WS_MAX_ENTRIES:
            break
    return {"name": str(name).strip(), "saved_at": float(now), "entries": entries}


def plan_workspace_apply(snapshot: dict, windows) -> dict:
    """
    把快照和"现在实际开着的窗口"对上，算出要做什么（**纯函数**，可单测）。

    匹配顺序：
      1. 同一个可执行文件名 + **标题完全相同** —— 最可靠（记事本、播放器这类）
      2. 同一个可执行文件名里还没被用掉的窗口（按顺序补） —— 浏览器那种标题会变的靠这步
      3. 对不上的 → 归入 `missing`，报告出来（**不替你启动程序**）

    返回 `{"restore": [{hwnd, rect, name, title}], "minimize": [...], "missing": [...]}`
    """
    pool: dict[str, list] = {}
    for wi in windows:
        rec = window_record(wi)
        pool.setdefault(rec["name"].lower(), []).append(rec)

    restore, minimize, missing = [], [], []

    for entry in snapshot.get("entries") or []:
        key = str(entry.get("name", "") or "").lower()
        cands = pool.get(key) or []
        chosen = None
        for cand in cands:                      # 先按标题精确配
            if cand["title"] and cand["title"] == entry.get("title"):
                chosen, how = cand, "title"
                break
        else:
            if cands:                           # 再按"同名进程里还没用掉的"
                chosen, how = cands[0], "name"
        if chosen is None:
            missing.append({"name": entry.get("name", ""), "title": entry.get("title", "")})
            continue
        pool[key].remove(chosen)
        item = {"hwnd": int(chosen["hwnd"] or 0), "rect": list(entry.get("rect") or []),
                "name": entry.get("name", ""), "title": entry.get("title", ""),
                "matched_by": how}
        (minimize if entry.get("iconic") else restore).append(item)
    return {"restore": restore, "minimize": minimize, "missing": missing}


def get_window_rect(hwnd: int) -> tuple[int, int, int, int] | None:
    """
    取窗口的位置与大小，返回 `(x, y, 宽, 高)`（拿不到就 None）。

    用 `DwmGetWindowAttribute(DWMWA_EXTENDED_FRAME_BOUNDS)` 会更准（不含不可见的
    阴影边框），但那需要额外处理失败回退；对"恢复布局"这个用途来说，
    `GetWindowRect` 的偏差（几十像素的透明边框）无关紧要，**少一层失败模式更重要**。
    """
    if not hwnd or not user32.IsWindow(w.HWND(hwnd)):
        return None
    r = w.RECT()
    if not user32.GetWindowRect(w.HWND(hwnd), ctypes.byref(r)):
        return None
    return int(r.left), int(r.top), int(r.right - r.left), int(r.bottom - r.top)


def virtual_screen_rect() -> tuple[int, int, int, int]:
    """所有显示器合起来的虚拟桌面矩形 `(x, y, w, h)`（拿不到就给主屏）。"""
    try:
        x = user32.GetSystemMetrics(SM_XVIRTUALSCREEN)
        y = user32.GetSystemMetrics(SM_YVIRTUALSCREEN)
        cw = user32.GetSystemMetrics(SM_CXVIRTUALSCREEN)
        ch = user32.GetSystemMetrics(SM_CYVIRTUALSCREEN)
        if cw > 0 and ch > 0:
            return x, y, cw, ch
    except Exception:
        pass
    w, h = get_physical_screen_size()
    return 0, 0, w, h


def clamp_rect_to_screen(rect) -> list[int]:
    """
    把矩形夹回虚拟桌面内，并保证它至少有一部分可见。

    为什么要夹：快照可能是"插着外接显示器时存的"，拔掉之后那些坐标就在屏幕外了 ——
    直接照着挪，窗口会跑到看不见的地方，用户会以为"东西丢了"。
    """
    vx, vy, vw, vh = virtual_screen_rect()
    x, y, cw, ch = (int(v) for v in rect)
    cw = max(WS_MIN_W, min(cw, vw))
    ch = max(WS_MIN_H, min(ch, vh))
    x = max(vx, min(x, vx + vw - cw))
    y = max(vy, min(y, vy + vh - ch))
    return [x, y, cw, ch]


def move_window(hwnd: int, rect) -> tuple[bool, str]:
    """
    把窗口挪到指定位置/大小（**不改前后顺序、不抢焦点**）。

    返回 (是否成功, 说明)。失败时**不改动窗口状态** —— 与其挪到一个奇怪的地方，
    不如什么都不做再如实告诉用户。
    """
    if not hwnd or not user32.IsWindow(w.HWND(hwnd)):
        return False, "窗口已经不在了"
    x, y, cw, ch = clamp_rect_to_screen(rect)
    ok = user32.SetWindowPos(w.HWND(hwnd), None, x, y, cw, ch,
                             SWP_NOZORDER | SWP_NOACTIVATE)
    if not ok:
        return False, f"移动失败（错误码 {ctypes.get_last_error()}）"
    return True, f"已放到 ({x}, {y}) {cw}×{ch}"


def apply_workspace_plan(plan: dict) -> dict:
    """
    执行 `plan_workspace_apply` 的结果。返回 `{done, failed, minimized}`。

    **只做两件事**：把该还原的还原并挪到位、把该最小化的最小化。
    不启动缺失的程序、不动计划外的窗口 —— 恢复布局不该有惊喜。
    """
    done, failed = 0, []
    for item in plan.get("restore") or []:
        hwnd, rect = item.get("hwnd", 0), item.get("rect") or []
        if not hwnd or not user32.IsWindow(w.HWND(hwnd)):
            failed.append(f"{item.get('name', '')}：窗口已经不在了")
            continue
        if user32.IsIconic(w.HWND(hwnd)):
            user32.ShowWindow(w.HWND(hwnd), SW_RESTORE)
        ok, msg = move_window(hwnd, rect) if len(rect) == 4 else (True, "只还原")
        if ok:
            done += 1
        else:
            failed.append(f"{item.get('name', '')}：{msg}")
    minimized = 0
    for item in plan.get("minimize") or []:
        hwnd = item.get("hwnd", 0)
        if not hwnd or not user32.IsWindow(w.HWND(hwnd)):
            failed.append(f"{item.get('name', '')}：窗口已经不在了")
            continue
        ok, _msg = minimize_window(hwnd, settle=0.05)
        if ok:
            minimized += 1
        else:
            failed.append(f"{item.get('name', '')}：最小化失败")
    return {"done": done, "minimized": minimized, "failed": failed}


def minimize_windows_except(keep_hwnds, windows) -> int:
    """
    把不在 `keep_hwnds` 里的窗口都最小化（"一键收拾桌面"用）。

    保护名单里的进程、以及 WinMon 自己的窗口一律跳过。
    """
    keep = {int(h) for h in keep_hwnds}
    mine = os.getpid()
    n = 0
    for wi in windows:
        if wi.hwnd in keep or is_own_window(wi.title, wi.pid, mine):
            continue
        if (wi.proc or "").lower() in IDLE_PROTECTED_PROCESSES:
            continue
        try:
            if minimize_window(wi.hwnd, settle=0.03)[0]:
                n += 1
        except Exception:
            continue
    return n


# ============================================================================
# 九、操作历史（有副作用动作的审计记录）
# ============================================================================

#: 操作历史的日志文件名（放在项目目录，即 `项目目录\`）
HISTORY_LOG_NAME = "winmon_history.log"
#: 日志超过这个大小就轮转（留一份 `.1`）—— 长期开着不至于无限膨胀
HISTORY_LOG_MAX_BYTES = 2 * 1024 * 1024


#: 设置文件的"结构版本"。变了就要走一次迁移（C.2）。
#:   1 = 2026-10-12：清掉"自动打开视频/视频筛选/播放进度"那批键，
#:       并把关闭方式这类历史值统一成 slug。
SETTINGS_VERSION = 1

#: 已经不存在、应当从设置文件里删掉的键（连同它们的历史值一起清）
LEGACY_SETTING_KEYS: tuple[str, ...] = (
    "video_tabs", "video_countdown_choice", "video_source_pin",
    "video_only_apps", "video_app_pin", "video_target_url", "video_target_title",
)

#: 「关闭视频」的 slug 白名单（与 winmon.py 的 VIDEO_CLOSE_MODES 一致）。
#: 放一份在这里是为了让**迁移逻辑不依赖界面模块**（core 不能 import winmon）。
CLOSE_MODE_SLUGS: tuple[str, ...] = ("window", "close", "pause", "back", "smart")
#: 旧中文标签 → slug（历史设置文件里存的是中文）
CLOSE_MODE_LABELS: dict[str, str] = {
    "关闭当前窗口": "window", "关闭当前标签页": "close",
    "暂停视频": "pause", "返回上一页": "back", "暂停并返回": "smart",
}


def migrate_settings(data: dict) -> tuple[dict, list[str]]:
    """
    把旧版本的设置"升"到当前结构（C.2）。

    返回 `(新设置, 改了什么的说明列表)`；**不修改入参**（调用方拿到的是新字典）。

    为什么要做（而不是继续靠"读的时候顺手兼容一下"）：
      · 兼容映射只解决"还认得"，旧键会**永远留在文件里**，越积越乱；
      · 用户改过的坏值（比如 `history_days: "很久"`、`close_mode: 123`）也该被
        收拾成合法值，而不是每次读的时候再判断一次；
      · 迁移**顺手说明改了什么**，并把这次迁移写进操作历史 —— 用户能看见自己的
        设置被动过，而不是悄悄变。
    """
    if not isinstance(data, dict):
        return {}, ["设置文件内容不是对象，已重置为空设置"]
    out = dict(data)
    changes: list[str] = []
    version = out.get("settings_version")
    try:
        version = int(version)
    except (TypeError, ValueError):
        version = 0

    # 1) 清掉已经不存在的键（它们对应的功能已按约定删除）
    for key in LEGACY_SETTING_KEYS:
        if key in out:
            out.pop(key, None)
            changes.append(f"删除已废弃的设置项 {key}")

    # 2) 关闭方式：中文标签/未知值 → 规范 slug
    raw_close = out.get("video_close_mode")
    if raw_close is not None:
        slug = str(raw_close).strip()
        if slug in CLOSE_MODE_LABELS:
            out["video_close_mode"] = CLOSE_MODE_LABELS[slug]
            changes.append(f"关闭方式「{slug}」→ {out['video_close_mode']}")
        elif slug not in CLOSE_MODE_SLUGS:
            out["video_close_mode"] = "window"
            changes.append(f"关闭方式「{slug}」不认识 → 退回 window")
    # 点 × 的行为（tray/exit）与上面不是一回事，单独校一遍
    if "close_mode" in out and out["close_mode"] not in ("tray", "exit"):
        changes.append(f"关闭行为「{out['close_mode']}」不合法 → 退回 exit")
        out["close_mode"] = "exit"

    # 3) 日志保留天数：非法值退回默认 30
    days = out.get("history_days")
    if days is not None:
        try:
            days_int = int(days)
        except (TypeError, ValueError):
            days_int = None
        if days_int not in (7, 30, 90):
            out["history_days"] = 30
            changes.append(f"日志保留「{days}」不合法 → 30 天")

    # 4) 类型明显不对的容器字段：一律换成空，别让界面拿到字符串去当列表用
    for key, want in (("workspaces", list), ("gallery_order", list),
                      ("program_prefs", dict)):
        if key in out and not isinstance(out[key], want):
            changes.append(f"{key} 类型不对（{type(out[key]).__name__}）→ 已清空")
            out[key] = want()

    # 5) 旧版单选类别 catalog_category → 新版多选 catalog_categories（转换，不丢数据）
    old_cat = out.get("catalog_category")
    if old_cat:
        key = next((k for k, v in CATEGORY_LABELS.items() if v == old_cat), "")
        if key and "catalog_categories" not in out:
            out["catalog_categories"] = [key]
            changes.append(f"类别筛选「{old_cat}」→ 转为多选列表")
        out.pop("catalog_category", None)
        changes.append("删除旧版单选类别键 catalog_category")
    if version != SETTINGS_VERSION:
        out["settings_version"] = SETTINGS_VERSION
        changes.insert(0, f"设置结构从 v{version} 升到 v{SETTINGS_VERSION}")
    return out, changes


def history_log_path() -> str:
    """
    操作历史日志的完整路径。

    可用环境变量 `WINMON_HISTORY_LOG` 覆盖（**给测试用**：测试不该往用户真实的
    历史日志里写东西）。默认在**运行时数据目录**（`data_dir()`）下的
    `winmon_history.log` —— 不再写在程序目录里。
    """
    override = os.environ.get("WINMON_HISTORY_LOG")
    if override:
        return override
    return data_file(HISTORY_LOG_NAME)


class ActionHistory:
    """
    记录"谁在什么时候被关闭/结束了"，用于事后追溯。

    只记录有副作用的动作（关闭窗口、结束进程、结束进程树），
    以及它们的结果（成功/失败/未响应）。纯查询操作不入账，避免噪声。

    **两条线**：
      · 内存里一份（`items`，给界面看，有上限）；
      · **磁盘上一份**（`winmon_history.log`，只追加、跨会话保留）——
        每次 `add()` 都会追加一行到日志文件，关掉程序也还在，
        方便事后查"这是什么时候被谁关掉的"。
      磁盘那份放在**运行时数据目录**里（见 `history_log_path()`），
      不在程序目录里。
    """

    def __init__(self, limit: int = 500, log_path: str = "",
                 log_enabled: bool = True) -> None:
        self.limit = limit
        self._items: list[dict] = []
        #: 日志文件路径；空 = 用默认位置（项目目录）。写失败会置 `log_error` 并**不再重试**
        #: （只读介质/目录被删这类情况重试也没用，但错误要留痕给界面显示）
        self.log_path = log_path or history_log_path()
        self.log_enabled = bool(log_enabled)
        self.log_error = ""
        self._log_failed = False

    # ---- 写入 ----

    def add(self, action: str, target: str, result: str, extra: str = "",
            open_time: str = "") -> dict:
        """
        记一条操作。

        `open_time`（可选）：**这个窗口/程序是什么时候打开（启动）的**
        —— 用户要"明确哪个程序打开、哪个程序关闭了"，把"开"和"关"配成一对，
        日志里一眼就能看出"它开了多久就被关了"。取不到就写空。
        """
        item = {"ts": time.time(), "time": self.timestamp(),
                "action": action, "target": target, "result": result,
                "extra": extra, "open_time": open_time}
        self._items.append(item)
        if len(self._items) > self.limit:
            del self._items[:self.limit // 3]
        self._append_log(item)
        return item

    @staticmethod
    def timestamp() -> str:
        """
        记录用的时间戳：`YYYY-MM-DD HH:MM`（**到分钟**）。

        用户 2026-10-12 的要求："时间到分钟记录就行，不用精确到秒"。
        秒虽然更细，但日志里一列 19 个字符反而看不清重点；
        同一分钟内发生的事按顺序排下来就够用了（日志本来就是追加写的）。
        """
        return time.strftime("%Y-%m-%d %H:%M")

    def _append_log(self, item: dict) -> None:
        """
        把一条记录追加到日志文件（**失败只记错，不抛异常**）。

        为什么不让它抛：记日志是"辅助功能"，不能因为磁盘满/目录被删
        就把"关闭窗口"这种主流程打断。错误留在 `log_error` 里，界面会显示。
        """
        if not self.log_enabled or self._log_failed:
            return
        try:
            self._rotate_if_needed()
            with open(self.log_path, "a", encoding="utf-8", newline="\n") as fh:
                fh.write(self.format_line(item) + "\n")
        except Exception as exc:
            self._log_failed = True
            self.log_error = f"{type(exc).__name__}: {exc}"

    def _rotate_if_needed(self) -> None:
        """日志超过上限就把它改名成 `.1`（覆盖旧的 `.1`）。"""
        try:
            if os.path.getsize(self.log_path) < HISTORY_LOG_MAX_BYTES:
                return
        except OSError:
            return                      # 文件不存在/读不到：没什么可轮转的
        try:
            backup = self.log_path + ".1"
            if os.path.exists(backup):
                os.remove(backup)
            os.replace(self.log_path, backup)
        except OSError:
            pass

    @staticmethod
    def format_line(item: dict) -> str:
        """
        一条记录写成一行：`时间 \\t 动作 \\t 对象 \\t 结果 \\t 备注 \\t 打开时间`。

        用制表符而不是表格线：用记事本打开也能看，而且 `parse_line` 能原样读回来。
        换行/制表符在写入前会被替换掉（否则一条记录会被拆成两行，读回来就错位了）。
        "打开时间"这一列让**开/关配成一对**（约定"明确哪个程序打开了"）。
        """
        def clean(text) -> str:
            return str(text if text is not None else "").replace("\t", " ").replace("\r", " ").replace("\n", " ")
        parts = [clean(item.get("time")), clean(item.get("action")),
                 clean(item.get("target")), clean(item.get("result"))]
        extra = clean(item.get("extra"))
        opened = clean(item.get("open_time"))
        # 两列都要给：不然后面那列会错位（宁可写空字符串）
        if extra or opened:
            parts.append(extra)
        if opened:
            parts.append(opened)
        return "\t".join(parts)

    @staticmethod
    def parse_line(line: str) -> dict | None:
        """把日志里的一行读回成记录（认不出来就返回 None，不抛异常）。"""
        text = (line or "").rstrip("\n")
        if not text.strip() or text.lstrip().startswith("#"):
            return None
        cols = text.split("\t")
        if len(cols) < 4:
            return None
        return {"time": cols[0], "action": cols[1], "target": cols[2],
                "result": cols[3], "extra": cols[4] if len(cols) > 4 else "",
                "open_time": cols[5] if len(cols) > 5 else "", "ts": 0.0}

    @staticmethod
    def parse_time(text: str) -> float:
        """
        把记录里的时间戳解析成 epoch 秒；认不出来返回 0。

        **三种历史格式都要认**（老日志不能因为换了格式就读不出来）：
          `2026-10-12 13:40`（现在这种，到分钟）
          `2026-10-12 13:40:08`（曾经到秒）
          `2026-10-12 13:40:08.123`（曾经带毫秒）

        踩过的坑：别写成 `mktime(strptime(s, "...%f"))` —— 实测（Python 3.14 + Windows）
        `%f` 解析出的小数秒放进 `struct_time` 就被截掉了，`mktime` 一算只剩整秒。
        所以小数部分**从字符串里单独取**再相加。
        """
        s = str(text or "").strip()
        if not s:
            return 0.0
        frac = 0.0
        if "." in s:
            head, _, tail = s.partition(".")
            digits = "".join(ch for ch in tail if ch.isdigit())
            if digits:
                frac = int(digits[:6].ljust(6, "0")) / 1_000_000.0
            s = head
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
            try:
                return time.mktime(time.strptime(s, fmt)) + frac
            except ValueError:
                continue
        return 0.0

    def load_from_log(self, path: str = "", limit: int = 400) -> list[dict]:
        """
        读日志文件（默认读 `self.log_path`），返回**最新的在前**的记录。

        坏行直接跳过 —— 用户完全可能用记事本改过这个文件。
        """
        target = path or self.log_path
        out: list[dict] = []
        try:
            with open(target, encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    item = self.parse_line(line)
                    if item is not None:
                        item["ts"] = self.parse_time(item.get("time", ""))
                        out.append(item)
        except OSError as exc:
            self.log_error = f"{type(exc).__name__}: {exc}"
            return []
        return list(reversed(out[-limit:]))

    def prune_log(self, days: int, path: str = "", now: float = 0.0) -> tuple[int, str]:
        """
        删掉日志里**超过 `days` 天**的记录（约定：默认 30 天自动清理）。

        返回 `(删掉的行数, 说明)`。约定：
          · `days <= 0` = 不清理（保留全部）；
          · 时间戳解析不出来的行**保留**（宁可留着，也不要因为格式怪就删记录）；
          · 清理前先把原文件另存为 `<日志>.bak`（一次），万一删错了还能找回来；
          · 只有真的有行被删才重写文件（避免每次启动都动磁盘）。
        """
        if int(days) <= 0:
            return 0, "保留天数设为 0，未清理"
        target = path or self.log_path
        if not os.path.exists(target):
            return 0, "日志文件不存在，无需清理"
        cutoff = (now or time.time()) - int(days) * 86400
        kept: list[str] = []
        dropped = 0
        unparsable = 0
        try:
            with open(target, encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    raw = line.rstrip("\n")
                    item = self.parse_line(raw)
                    if item is None:
                        kept.append(raw)          # 空行/注释/坏行：原样留着
                        continue
                    stamp = self.parse_time(item.get("time", ""))
                    if stamp <= 0:
                        unparsable += 1
                        kept.append(raw)          # 认不出时间的也留着
                    elif stamp >= cutoff:
                        kept.append(raw)
                    else:
                        dropped += 1
            if dropped:
                try:
                    shutil.copyfile(target, target + ".bak")
                except OSError:
                    pass
                with open(target, "w", encoding="utf-8", newline="\n") as fh:
                    fh.write("\n".join(kept) + ("\n" if kept else ""))
        except OSError as exc:
            return 0, f"清理失败：{type(exc).__name__}: {exc}"
        note = f"已清理 {dropped} 条超过 {days} 天的记录"
        if unparsable:
            note += f"（{unparsable} 条时间认不出来，已保留）"
        return dropped, note

    # ---- 读/导出 ----

    def __len__(self) -> int:
        return len(self._items)

    @property
    def items(self) -> list[dict]:
        return list(self._items)

    def clear(self) -> None:
        """
        清空内存里的记录。

        **不动日志文件**：日志是"留痕"，用户点"清空"多半只是想清掉界面上的列表；
        真要清磁盘上的记录是另一件事（独立窗口里有单独的「清空日志文件」按钮，
        而且会先问一次）。这符合"留痕"这条铁律。
        """
        self._items.clear()

    def clear_log_file(self) -> tuple[bool, str]:
        """清空（删除）日志文件本身。返回 (是否成功, 说明)。"""
        try:
            if os.path.exists(self.log_path):
                os.remove(self.log_path)
            self._log_failed = False
            self.log_error = ""
            return True, "日志文件已清空"
        except OSError as exc:
            return False, f"清空日志失败：{type(exc).__name__}: {exc}"

    def as_text(self, newest_first: bool = True) -> str:
        rows = list(reversed(self._items)) if newest_first else self._items
        return "\n".join(self.format_line(it) for it in rows)

    def _rows_for(self, rows=None) -> list[dict]:
        """
        导出用哪批记录：`rows` 给了就用它（界面"筛选后导出"），否则用内存里的全部。

        顺序统一按**时间正序**（日志的书写顺序，读起来像流水账）。
        """
        if rows is not None:      # 不能用 truthy：筛选后 0 条 ≠ 没筛选
            return [r for r in rows if isinstance(r, dict)][::-1]   # 界面是"最新在前"
        return self.items

    def export(self, path: str = "", rows=None) -> str:
        """导出为 txt（可传 `rows` 只导筛选后的那批）。"""
        if not path:
            import tempfile
            path = os.path.join(tempfile.gettempdir(),
                                f"winmon_history_{time.strftime('%Y%m%d_%H%M%S')}.txt")
        picked = self._rows_for(rows)
        with open(path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write("# WinMon 操作记录\n")
            fh.write(f"# 导出时间：{time.strftime('%Y-%m-%d %H:%M:%S')}\n")
            fh.write(f"# 记录数：{len(picked)}\n\n")
            fh.write("\n".join(self.format_line(it) for it in picked))
            fh.write("\n")
        return path

    def export_csv(self, path: str = "", rows=None) -> str:
        """
        导出为 **CSV**（B.1）。

        两个刻意的选择：
          · **UTF-8 with BOM**：Excel 双击打开时中文才不会乱码（无 BOM 会被当 ANSI）；
          · 用 `csv` 模块写：备注里的引号/逗号/换行都按 CSV 规则转义，不会把表格撑坏。
        """
        import csv
        import tempfile
        if not path:
            path = os.path.join(tempfile.gettempdir(),
                                f"winmon_history_{time.strftime('%Y%m%d_%H%M%S')}.csv")
        picked = self._rows_for(rows)
        with open(path, "w", encoding="utf-8-sig", newline="") as fh:
            writer = csv.writer(fh)
            writer.writerow(["时间", "程序打开时间", "动作", "对象", "结果", "备注"])
            for it in picked:
                writer.writerow([it.get("time", ""), it.get("open_time", ""),
                                 it.get("action", ""), it.get("target", ""),
                                 it.get("result", ""), it.get("extra", "")])
        return path
