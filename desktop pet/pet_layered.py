# -*- coding: utf-8 -*-
"""
逐像素 alpha 的 Windows 分层窗口
==============================

为什么不用 Tk 的 `-transparentcolor`
-----------------------------------
颜色键只能做“全透明 / 不透明”二选一，窗口仍是一个矩形；DWM 在移动、
缩放或显卡合成时可能给矩形边缘补一圈阴影/黑边。`SetWindowRgn` 能裁形状，
但每帧重建 HRGN 很贵，而且轮廓外仍然可能被 DWM 画出阴影。

这里改用 Windows 原生分层窗口：
  · 32 位 DIB（BGRA，预乘 alpha）；
  · GDI+ 把精灵和粒子画进 DIB；
  · `UpdateLayeredWindow(..., ULW_ALPHA)` 一次性提交；
  · Windows 按 alpha 做命中测试——alpha=0 的像素点击穿透，只有身体能点。

窗口本身没有颜色键、没有窗口区域、没有 DWM 阴影，所以不会再出现
“透明边框 / 黑框 / 黑影”。这一套与 dsh-pet 的 `transparent: true,
frame: false, hasShadow: false` 是同一个思路，只是渲染器换成 GDI+。

只依赖标准库 `ctypes` + 系统 `gdiplus.dll`，不引入 Pillow。
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import os

#: 32bpp 预乘 alpha（GDI+ PixelFormat32bppPARGB）
_PIXEL_FORMAT_PARGB = 0x000E200B
#: `UpdateLayeredWindow` 的 ULW_ALPHA
_ULW_ALPHA = 0x00000002

_WM_PAINT = 0x000F
_WM_ERASEBKGND = 0x0014
_WM_NCPAINT = 0x0085


class LayeredSurfaceError(RuntimeError):
    """GDI+/分层窗口不可用。"""


class _GdiplusStartupInput(ctypes.Structure):
    _fields_ = [
        ("GdiplusVersion", ctypes.c_uint32),
        ("DebugEventCallback", ctypes.c_void_p),
        ("SuppressBackgroundThread", wintypes.BOOL),
        ("SuppressExternalCodecs", wintypes.BOOL),
    ]


class _RectF(ctypes.Structure):
    _fields_ = [
        ("X", ctypes.c_float),
        ("Y", ctypes.c_float),
        ("Width", ctypes.c_float),
        ("Height", ctypes.c_float),
    ]


class _PointF(ctypes.Structure):
    _fields_ = [("X", ctypes.c_float), ("Y", ctypes.c_float)]


class _BitmapInfoHeader(ctypes.Structure):
    _fields_ = [
        ("biSize", wintypes.DWORD),
        ("biWidth", wintypes.LONG),
        ("biHeight", wintypes.LONG),
        ("biPlanes", wintypes.WORD),
        ("biBitCount", wintypes.WORD),
        ("biCompression", wintypes.DWORD),
        ("biSizeImage", wintypes.DWORD),
        ("biXPelsPerMeter", wintypes.LONG),
        ("biYPelsPerMeter", wintypes.LONG),
        ("biClrUsed", wintypes.DWORD),
        ("biClrImportant", wintypes.DWORD),
    ]


class _BitmapInfo(ctypes.Structure):
    _fields_ = [("bmiHeader", _BitmapInfoHeader),
                ("bmiColors", wintypes.DWORD * 3)]


class _BlendFunction(ctypes.Structure):
    _fields_ = [
        ("BlendOp", ctypes.c_ubyte),
        ("BlendFlags", ctypes.c_ubyte),
        ("SourceConstantAlpha", ctypes.c_ubyte),
        ("AlphaFormat", ctypes.c_ubyte),
    ]


class _PaintStruct(ctypes.Structure):
    _fields_ = [
        ("hdc", wintypes.HDC),
        ("fErase", wintypes.BOOL),
        ("rcPaint", wintypes.RECT),
        ("fRestore", wintypes.BOOL),
        ("fIncUpdate", wintypes.BOOL),
        ("rgbReserved", ctypes.c_ubyte * 32),
    ]


_gdiplus = None
_token = ctypes.c_void_p()


def _proto(dll, name, args, rest):
    fn = getattr(dll, name)
    fn.argtypes = args
    fn.restype = rest
    return fn


def _load_gdiplus():
    """惰性加载 GDI+ 并注册 flat API 签名（只做一次）。"""
    global _gdiplus, _token
    if _gdiplus is not None:
        return _gdiplus
    if os.name != "nt":
        raise LayeredSurfaceError("逐像素透明窗口只支持 Windows")
    try:
        gp = ctypes.WinDLL("gdiplus")
    except OSError as exc:
        raise LayeredSurfaceError(f"加载 gdiplus.dll 失败：{exc}") from exc

    startup = _GdiplusStartupInput(1, None, False, False)
    status = gp.GdiplusStartup(ctypes.byref(_token),
                               ctypes.byref(startup), None)
    if status != 0:
        raise LayeredSurfaceError(f"GdiplusStartup 失败：status={status}")

    ptr = ctypes.c_void_p
    _proto(gp, "GdipCreateBitmapFromFile",
           [wintypes.LPCWSTR, ctypes.POINTER(ptr)], ctypes.c_int)
    _proto(gp, "GdipCreateBitmapFromScan0",
           [ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
            ptr, ctypes.POINTER(ptr)], ctypes.c_int)
    _proto(gp, "GdipGetImageGraphicsContext",
           [ptr, ctypes.POINTER(ptr)], ctypes.c_int)
    _proto(gp, "GdipGraphicsClear", [ptr, ctypes.c_uint32], ctypes.c_int)
    _proto(gp, "GdipDrawImageI",
           [ptr, ptr, ctypes.c_int, ctypes.c_int], ctypes.c_int)
    _proto(gp, "GdipSetSmoothingMode", [ptr, ctypes.c_int], ctypes.c_int)
    _proto(gp, "GdipSetTextRenderingHint", [ptr, ctypes.c_int], ctypes.c_int)
    _proto(gp, "GdipSetInterpolationMode", [ptr, ctypes.c_int], ctypes.c_int)
    _proto(gp, "GdipGetImageWidth",
           [ptr, ctypes.POINTER(ctypes.c_uint)], ctypes.c_int)
    _proto(gp, "GdipGetImageHeight",
           [ptr, ctypes.POINTER(ctypes.c_uint)], ctypes.c_int)
    _proto(gp, "GdipDisposeImage", [ptr], ctypes.c_int)
    _proto(gp, "GdipDeleteGraphics", [ptr], ctypes.c_int)
    _proto(gp, "GdipCreateFontFamilyFromName",
           [wintypes.LPCWSTR, ptr, ctypes.POINTER(ptr)], ctypes.c_int)
    _proto(gp, "GdipCreateFont",
           [ptr, ctypes.c_float, ctypes.c_int, ctypes.c_int,
            ctypes.POINTER(ptr)], ctypes.c_int)
    _proto(gp, "GdipCreateStringFormat",
           [ctypes.c_int, wintypes.WORD, ctypes.POINTER(ptr)], ctypes.c_int)
    _proto(gp, "GdipSetStringFormatAlign", [ptr, ctypes.c_int], ctypes.c_int)
    _proto(gp, "GdipSetStringFormatLineAlign",
           [ptr, ctypes.c_int], ctypes.c_int)
    _proto(gp, "GdipCreateSolidFill",
           [ctypes.c_uint32, ctypes.POINTER(ptr)], ctypes.c_int)
    _proto(gp, "GdipDrawString",
           [ptr, wintypes.LPCWSTR, ctypes.c_int, ptr,
            ctypes.POINTER(_RectF), ptr, ptr], ctypes.c_int)
    _proto(gp, "GdipDeleteFont", [ptr], ctypes.c_int)
    _proto(gp, "GdipDeleteFontFamily", [ptr], ctypes.c_int)
    _proto(gp, "GdipDeleteStringFormat", [ptr], ctypes.c_int)
    _proto(gp, "GdipDeleteBrush", [ptr], ctypes.c_int)
    _proto(gp, "GdipCreatePath", [ctypes.c_int, ctypes.POINTER(ptr)],
           ctypes.c_int)
    _proto(gp, "GdipAddPathPolygon",
           [ptr, ctypes.POINTER(_PointF), ctypes.c_int], ctypes.c_int)
    _proto(gp, "GdipFillPath", [ptr, ptr, ptr], ctypes.c_int)
    _proto(gp, "GdipDeletePath", [ptr], ctypes.c_int)
    _proto(gp, "GdipCreatePen1",
           [ctypes.c_uint32, ctypes.c_float, ctypes.c_int,
            ctypes.POINTER(ptr)], ctypes.c_int)
    _proto(gp, "GdipDrawEllipse",
           [ptr, ptr, ctypes.c_float, ctypes.c_float,
            ctypes.c_float, ctypes.c_float], ctypes.c_int)
    _proto(gp, "GdipFillEllipse",
           [ptr, ptr, ctypes.c_float, ctypes.c_float,
            ctypes.c_float, ctypes.c_float], ctypes.c_int)
    _proto(gp, "GdipDeletePen", [ptr], ctypes.c_int)
    _gdiplus = gp
    return gp


class LayerImage:
    """一张已经交给 GDI+ 解码的精灵图（由 LayeredSurface 持有）。"""

    __slots__ = ("_surface", "handle", "_w", "_h", "path")

    def __init__(self, surface, handle, width: int, height: int, path: str):
        self._surface = surface
        self.handle = handle
        self._w = int(width)
        self._h = int(height)
        self.path = path

    def width(self) -> int:
        return self._w

    def height(self) -> int:
        return self._h


def rgb(value: str | int) -> int:
    """`#rrggbb` / 0xRRGGBB → 0xRRGGBB（忽略 alpha）。"""
    if isinstance(value, int):
        return value & 0xFFFFFF
    text = str(value).strip().lstrip("#")
    if len(text) == 3:
        text = "".join(ch * 2 for ch in text)
    return int(text, 16) & 0xFFFFFF


def argb(rgb_value: int, alpha: int = 255) -> int:
    alpha = max(0, min(255, int(alpha)))
    return ((alpha & 0xFF) << 24) | (int(rgb_value) & 0xFFFFFF)


class LayeredSurface:
    """
    一个窗口的 GDI+ 画布。

    每帧：`begin()` → `draw_*()` → `present()`。像素格式是预乘 alpha，
    所以 `UpdateLayeredWindow` 不需要再做颜色键/区域处理。
    """

    def __init__(self, hwnd: int, width: int, height: int):
        self._gp = _load_gdiplus()
        self.hwnd = int(hwnd)
        self._user32 = ctypes.WinDLL("user32")
        self._gdi32 = ctypes.WinDLL("gdi32")
        self._proto_gdi()
        self._w = max(1, int(width))
        self._h = max(1, int(height))
        self._screen_dc = self._user32.GetDC(None)
        if not self._screen_dc:
            raise LayeredSurfaceError("GetDC(NULL) 失败")
        self._mem_dc = self._gdi32.CreateCompatibleDC(self._screen_dc)
        if not self._mem_dc:
            self._user32.ReleaseDC(None, self._screen_dc)
            raise LayeredSurfaceError("CreateCompatibleDC 失败")
        self._dib = None
        self._old_bitmap = None
        self._bits = ctypes.c_void_p()
        self._bitmap = ctypes.c_void_p()
        self._graphics = ctypes.c_void_p()
        self._images: dict[str, LayerImage] = {}
        self._font_family = ctypes.c_void_p()
        self._format = ctypes.c_void_p()
        self._fonts: dict[int, ctypes.c_void_p] = {}
        self._present_count = 0
        self._last_error = ""
        self._create_buffers()
        self._init_text()

    # ---- Win32 签名 ----

    def _proto_gdi(self) -> None:
        u, g = self._user32, self._gdi32
        _proto(u, "GetDC", [wintypes.HWND], wintypes.HDC)
        _proto(u, "ReleaseDC", [wintypes.HWND, wintypes.HDC], ctypes.c_int)
        _proto(u, "UpdateLayeredWindow",
               [wintypes.HWND, wintypes.HDC,
                ctypes.POINTER(wintypes.POINT),
                ctypes.POINTER(wintypes.SIZE), wintypes.HDC,
                ctypes.POINTER(wintypes.POINT), wintypes.DWORD,
                ctypes.POINTER(_BlendFunction), wintypes.DWORD],
               wintypes.BOOL)
        _proto(g, "CreateCompatibleDC", [wintypes.HDC], wintypes.HDC)
        _proto(g, "DeleteDC", [wintypes.HDC], wintypes.BOOL)
        _proto(g, "SelectObject",
               [wintypes.HDC, wintypes.HGDIOBJ], wintypes.HGDIOBJ)
        _proto(g, "CreateDIBSection",
               [wintypes.HDC, ctypes.POINTER(_BitmapInfo), wintypes.UINT,
                ctypes.POINTER(ctypes.c_void_p), wintypes.HANDLE,
                wintypes.DWORD], wintypes.HBITMAP)
        _proto(g, "DeleteObject", [wintypes.HGDIOBJ], wintypes.BOOL)

    # ---- 缓冲区 ----

    def _create_buffers(self) -> None:
        bmi = _BitmapInfo()
        bmi.bmiHeader.biSize = ctypes.sizeof(_BitmapInfoHeader)
        bmi.bmiHeader.biWidth = self._w
        bmi.bmiHeader.biHeight = -self._h  # 负数 = top-down
        bmi.bmiHeader.biPlanes = 1
        bmi.bmiHeader.biBitCount = 32
        bmi.bmiHeader.biCompression = 0  # BI_RGB
        bits = ctypes.c_void_p()
        dib = self._gdi32.CreateDIBSection(
            self._screen_dc, ctypes.byref(bmi), 0,
            ctypes.byref(bits), None, 0)
        if not dib:
            raise LayeredSurfaceError("CreateDIBSection 失败")
        old = self._gdi32.SelectObject(self._mem_dc, dib)
        if not old:
            self._gdi32.DeleteObject(dib)
            raise LayeredSurfaceError("SelectObject(DIB) 失败")

        bitmap = ctypes.c_void_p()
        status = self._gp.GdipCreateBitmapFromScan0(
            self._w, self._h, self._w * 4, _PIXEL_FORMAT_PARGB,
            bits, ctypes.byref(bitmap))
        if status != 0:
            self._gdi32.SelectObject(self._mem_dc, old)
            self._gdi32.DeleteObject(dib)
            raise LayeredSurfaceError(
                f"GdipCreateBitmapFromScan0 失败：status={status}")
        graphics = ctypes.c_void_p()
        status = self._gp.GdipGetImageGraphicsContext(
            bitmap, ctypes.byref(graphics))
        if status != 0:
            self._gp.GdipDisposeImage(bitmap)
            self._gdi32.SelectObject(self._mem_dc, old)
            self._gdi32.DeleteObject(dib)
            raise LayeredSurfaceError(
                f"GdipGetImageGraphicsContext 失败：status={status}")

        self._dib = dib
        self._old_bitmap = old
        self._bits = bits
        self._bitmap = bitmap
        self._graphics = graphics
        self._gp.GdipSetSmoothingMode(graphics, 4)       # AntiAlias
        self._gp.GdipSetTextRenderingHint(graphics, 4)   # AntiAliasGridFit
        self._gp.GdipSetInterpolationMode(graphics, 5)   # HighQualityBicubic
        self._gp.GdipGraphicsClear(graphics, 0)

    def _destroy_buffers(self) -> None:
        if self._graphics:
            self._gp.GdipDeleteGraphics(self._graphics)
            self._graphics = ctypes.c_void_p()
        if self._bitmap:
            self._gp.GdipDisposeImage(self._bitmap)
            self._bitmap = ctypes.c_void_p()
        if self._dib:
            if self._old_bitmap:
                self._gdi32.SelectObject(self._mem_dc, self._old_bitmap)
                self._old_bitmap = None
            self._gdi32.DeleteObject(self._dib)
            self._dib = None
        self._bits = ctypes.c_void_p()

    # ---- 文字 ----

    def _init_text(self) -> None:
        for name in ("Segoe UI Symbol", "Segoe UI", "Arial"):
            family = ctypes.c_void_p()
            if self._gp.GdipCreateFontFamilyFromName(
                    name, None, ctypes.byref(family)) == 0:
                self._font_family = family
                break
        if not self._font_family:
            raise LayeredSurfaceError("创建字体族失败")
        fmt = ctypes.c_void_p()
        if self._gp.GdipCreateStringFormat(
                0, 0, ctypes.byref(fmt)) != 0:
            raise LayeredSurfaceError("创建 StringFormat 失败")
        self._gp.GdipSetStringFormatAlign(fmt, 1)      # Center
        self._gp.GdipSetStringFormatLineAlign(fmt, 1)  # Center
        self._format = fmt

    def _font(self, px: int) -> ctypes.c_void_p:
        px = max(1, int(px))
        font = self._fonts.get(px)
        if font:
            return font
        font = ctypes.c_void_p()
        status = self._gp.GdipCreateFont(
            self._font_family, float(px), 1, 2, ctypes.byref(font))
        if status != 0:
            # 极端情况下退回常规字重
            status = self._gp.GdipCreateFont(
                self._font_family, float(px), 0, 2, ctypes.byref(font))
        if status != 0:
            raise LayeredSurfaceError(f"创建字体失败：{px}px")
        self._fonts[px] = font
        return font

    # ---- 图像 ----

    def image(self, path: str) -> LayerImage:
        path = os.path.abspath(path)
        cached = self._images.get(path)
        if cached is not None:
            return cached
        handle = ctypes.c_void_p()
        status = self._gp.GdipCreateBitmapFromFile(
            os.fspath(path), ctypes.byref(handle))
        if status != 0:
            raise LayeredSurfaceError(f"读取精灵失败：{path}")
        width = ctypes.c_uint()
        height = ctypes.c_uint()
        self._gp.GdipGetImageWidth(handle, ctypes.byref(width))
        self._gp.GdipGetImageHeight(handle, ctypes.byref(height))
        image = LayerImage(self, handle, width.value, height.value, path)
        self._images[path] = image
        return image

    # ---- 绘制 ----

    def resize(self, width: int, height: int) -> None:
        width = max(1, int(width))
        height = max(1, int(height))
        if width == self._w and height == self._h:
            return
        self._destroy_buffers()
        self._w, self._h = width, height
        self._create_buffers()
        # 新 DIB 初始全透明；保留 images/fonts 缓存。
        self._gp.GdipSetSmoothingMode(self._graphics, 4)
        self._gp.GdipSetTextRenderingHint(self._graphics, 4)
        self._gp.GdipSetInterpolationMode(self._graphics, 5)

    def begin(self) -> None:
        self._gp.GdipGraphicsClear(self._graphics, 0)

    def draw_image(self, image: LayerImage, x: int, y: int) -> None:
        self._gp.GdipDrawImageI(self._graphics, image.handle,
                                int(x), int(y))

    def draw_text(self, text: str, cx: float, cy: float, px: int,
                  color: int, alpha: int = 255) -> None:
        brush = ctypes.c_void_p()
        if self._gp.GdipCreateSolidFill(
                argb(color, alpha), ctypes.byref(brush)) != 0:
            return
        try:
            rect = _RectF(float(cx) - 60.0, float(cy) - 40.0, 120.0, 80.0)
            self._gp.GdipDrawString(
                self._graphics, str(text), -1, self._font(px),
                ctypes.byref(rect), self._format, brush)
        finally:
            self._gp.GdipDeleteBrush(brush)

    def draw_polygon(self, points: list[tuple[float, float]],
                     color: int, alpha: int = 255) -> None:
        if len(points) < 3:
            return
        brush = ctypes.c_void_p()
        path = ctypes.c_void_p()
        if self._gp.GdipCreateSolidFill(
                argb(color, alpha), ctypes.byref(brush)) != 0:
            return
        try:
            if self._gp.GdipCreatePath(0, ctypes.byref(path)) != 0:
                return
            array = (_PointF * len(points))(
                *[_PointF(float(x), float(y)) for x, y in points])
            self._gp.GdipAddPathPolygon(path, array, len(points))
            self._gp.GdipFillPath(self._graphics, brush, path)
        finally:
            if path:
                self._gp.GdipDeletePath(path)
            self._gp.GdipDeleteBrush(brush)

    def draw_ellipse(self, x: float, y: float, w: float, h: float,
                     color: int, width: float = 1.0,
                     alpha: int = 255) -> None:
        pen = ctypes.c_void_p()
        if self._gp.GdipCreatePen1(
                argb(color, alpha), float(max(0.5, width)), 2,
                ctypes.byref(pen)) != 0:
            return
        try:
            self._gp.GdipDrawEllipse(
                self._graphics, pen, float(x), float(y),
                float(w), float(h))
        finally:
            self._gp.GdipDeletePen(pen)

    def fill_ellipse(self, x: float, y: float, w: float, h: float,
                     color: int, alpha: int = 255) -> None:
        brush = ctypes.c_void_p()
        if self._gp.GdipCreateSolidFill(
                argb(color, alpha), ctypes.byref(brush)) != 0:
            return
        try:
            self._gp.GdipFillEllipse(
                self._graphics, brush, float(x), float(y),
                float(w), float(h))
        finally:
            self._gp.GdipDeleteBrush(brush)

    # ---- 提交 / 查询 ----

    def present(self) -> bool:
        size = wintypes.SIZE(self._w, self._h)
        src = wintypes.POINT(0, 0)
        blend = _BlendFunction(0, 0, 255, 1)  # AC_SRC_OVER, AC_SRC_ALPHA
        ok = bool(self._user32.UpdateLayeredWindow(
            wintypes.HWND(self.hwnd), self._screen_dc, None,
            ctypes.byref(size), self._mem_dc, ctypes.byref(src),
            0, ctypes.byref(blend), _ULW_ALPHA))
        if ok:
            self._present_count += 1
        else:
            self._last_error = "UpdateLayeredWindow 失败"
        return ok

    def sample_alpha(self, x: int, y: int) -> int:
        """读取 DIB 中某个像素的 alpha（测试/诊断用）。"""
        if not self._bits or not (0 <= x < self._w and 0 <= y < self._h):
            return 0
        offset = (int(y) * self._w + int(x)) * 4
        data = ctypes.cast(self._bits, ctypes.POINTER(ctypes.c_ubyte))
        return int(data[offset + 3])

    def sample_rgba(self, x: int, y: int) -> tuple[int, int, int, int]:
        if not self._bits or not (0 <= x < self._w and 0 <= y < self._h):
            return (0, 0, 0, 0)
        offset = (int(y) * self._w + int(x)) * 4
        data = ctypes.cast(self._bits, ctypes.POINTER(ctypes.c_ubyte))
        return (int(data[offset + 2]), int(data[offset + 1]),
                int(data[offset]), int(data[offset + 3]))

    @property
    def size(self) -> tuple[int, int]:
        return self._w, self._h

    @property
    def present_count(self) -> int:
        return self._present_count

    @property
    def last_error(self) -> str:
        return self._last_error

    def close(self) -> None:
        for image in self._images.values():
            self._gp.GdipDisposeImage(image.handle)
        self._images.clear()
        for font in self._fonts.values():
            self._gp.GdipDeleteFont(font)
        self._fonts.clear()
        if self._format:
            self._gp.GdipDeleteStringFormat(self._format)
            self._format = ctypes.c_void_p()
        if self._font_family:
            self._gp.GdipDeleteFontFamily(self._font_family)
            self._font_family = ctypes.c_void_p()
        self._destroy_buffers()
        if self._mem_dc:
            self._gdi32.DeleteDC(self._mem_dc)
            self._mem_dc = None
        if self._screen_dc:
            self._user32.ReleaseDC(None, self._screen_dc)
            self._screen_dc = None


class ChildPaintSuppressor:
    """
    阻止 Tk 的客户区子窗口重绘背景。

    分层窗口的画面由 `UpdateLayeredWindow` 提供；但 Tk 在移动/刷新时仍会
    给子窗口发 `WM_ERASEBKGND`/`WM_PAINT`，把底色（例如品红）画在精灵上面。
    这里用 `SetWindowSubclass` 吞掉这两个消息，鼠标消息照常交给 Tk，
    所以事件绑定、拖动和右键都保持不变。
    """

    def __init__(self, hwnd: int):
        self.hwnd = int(hwnd)
        self._comctl32 = ctypes.WinDLL("comctl32")
        self._user32 = ctypes.WinDLL("user32")
        _proto(self._comctl32, "DefSubclassProc",
               [wintypes.HWND, wintypes.UINT, wintypes.WPARAM,
                wintypes.LPARAM], ctypes.c_ssize_t)
        _proto(self._comctl32, "SetWindowSubclass",
               [wintypes.HWND, ctypes.c_void_p, ctypes.c_size_t,
                ctypes.c_size_t], wintypes.BOOL)
        _proto(self._comctl32, "RemoveWindowSubclass",
               [wintypes.HWND, ctypes.c_void_p, ctypes.c_size_t],
               wintypes.BOOL)
        _proto(self._user32, "BeginPaint",
               [wintypes.HWND, ctypes.POINTER(_PaintStruct)],
               wintypes.HDC)
        _proto(self._user32, "EndPaint",
               [wintypes.HWND, ctypes.POINTER(_PaintStruct)],
               wintypes.BOOL)
        self._proc = ctypes.WINFUNCTYPE(
            ctypes.c_ssize_t, wintypes.HWND, wintypes.UINT,
            wintypes.WPARAM, wintypes.LPARAM,
            ctypes.c_size_t, ctypes.c_size_t)(self._wndproc)
        ok = self._comctl32.SetWindowSubclass(
            wintypes.HWND(self.hwnd),
            ctypes.cast(self._proc, ctypes.c_void_p), 1, 0)
        if not ok:
            raise LayeredSurfaceError("SetWindowSubclass 失败")

    def _wndproc(self, hwnd, msg, wparam, lparam, _uid, _ref):
        if msg == _WM_ERASEBKGND:
            return 1
        if msg == _WM_NCPAINT:
            return 0
        if msg == _WM_PAINT:
            ps = _PaintStruct()
            self._user32.BeginPaint(wintypes.HWND(hwnd), ctypes.byref(ps))
            self._user32.EndPaint(wintypes.HWND(hwnd), ctypes.byref(ps))
            return 0
        return self._comctl32.DefSubclassProc(
            wintypes.HWND(hwnd), msg, wparam, lparam)

    def close(self) -> None:
        if not self.hwnd:
            return
        try:
            self._comctl32.RemoveWindowSubclass(
                wintypes.HWND(self.hwnd),
                ctypes.cast(self._proc, ctypes.c_void_p), 1)
        except Exception:
            pass
        self.hwnd = 0
