# -*- coding: utf-8 -*-
"""
A4 —— GUI 与判定逻辑：标题判定、确认弹窗。

对应 HANDOFF.md：
  · 坑 14：`ttk.Label` 不支持 `background`，深色卡片里的文字必须用 `tk.Label`，
           否则"深色字压黑底"完全看不清
  · 坑 15：`tk.Frame` 没有 `padding` 参数，要 `ttk.Frame(padding=…)` 包 `tk.Label`
  · 坑 19：哔哩哔哩**首页**标题里也有 "bilibili"，不能据此判成"在播放"
  · 弹窗按键：Space / Enter = 关闭视频，Esc = 继续播放，
           倒计时结束自动按"否，继续播放"处理（无人值守时不误关）
"""

from __future__ import annotations

import ctypes
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import _helpers as h                     # noqa: E402
import winmon_core as core               # noqa: E402
from _helpers import PROJECT_DIR         # noqa: E402


# ===========================================================================
# 标题判定（纯逻辑，不需要图形会话）
# ===========================================================================
VIDEO_TITLES = [
    # 哔哩哔哩播放页的标准标题：视频名 + 后缀
    "【方舟阴间怪】数值怪都容易退环境吗？_哔哩哔哩_bilibili",
    "某个视频_哔哩哔哩",
    "某视频 - 哔哩哔哩 (゜-゜)つロ 干杯~-bilibili_哔哩哔哩",
    "How to write a parser_bilibili",
    "Watch this video now -bilibili.com",
    # 带 BV 号
    "随便看看 BV1xx411c7mD",
    # 长中文标题（>=6 个汉字，且不含应用名）
    "深度学习入门到放弃的第一课",
]

NON_VIDEO_TITLES = [
    "",
    "   ",
    "哔哩哔哩",
    "bilibili",
    "哔哩哔哩 (゜-゜)つロ 干杯~-bilibili",              # 坑 19：首页标题
    "bilibili (゜-゜)つロ 干杯~",
    "记事本",
    "无标题 - 记事本",
    "新标签页",
    "03",
]


class TestVideoTitleDetection(unittest.TestCase):
    """播放判定不能只看"标题里有 bilibili"（坑 19）。"""

    def test_video_titles(self):
        for t in VIDEO_TITLES:
            with self.subTest(title=t):
                self.assertTrue(core.looks_like_video_title(t), f"应判为视频：{t}")

    def test_non_video_titles(self):
        for t in NON_VIDEO_TITLES:
            with self.subTest(title=t):
                self.assertFalse(core.looks_like_video_title(t), f"不应判为视频：{t}")

    def test_homepage_title_is_the_trap(self):
        """把坑 19 单独钉住：首页标题含 bilibili 但必须判 False。"""
        home = "哔哩哔哩 (゜-゜)つロ 干杯~-bilibili"
        self.assertIn("bilibili", home.lower())
        self.assertFalse(core.looks_like_video_title(home))
        # 播放页标题是"视频名 + 后缀"，形如「某视频_哔哩哔哩」
        self.assertTrue(core.looks_like_video_title("【某视频】第一讲_哔哩哔哩"))

    def test_accepts_empty_and_none(self):
        self.assertFalse(core.looks_like_video_title(None))
        self.assertFalse(core.looks_like_video_title(""))
        self.assertFalse(core.looks_like_video_title("   "))


# ===========================================================================
# 确认弹窗
# ===========================================================================
def walk(widget):
    """深度遍历控件树（测试用：找文字/按钮）。"""
    yield widget
    for child in widget.winfo_children():
        yield from walk(child)


def texts_of(widget) -> list[str]:
    """收集控件树里所有 Label / Button 的显示文字。"""
    out = []
    for child in walk(widget):
        try:
            kind = child.winfo_class()
        except Exception:
            continue
        if kind in ("Label", "TButton", "Button", "Checkbutton", "TCheckbutton"):
            try:
                out.append(str(child.cget("text")))
            except Exception:
                pass
    return out


@h.needs_tk
class TestConfirmDialog(unittest.TestCase):
    """弹窗的按键约定（**没有倒计时** —— 那套自动确认 2026-10-16 作为死代码删掉了）。"""

    def setUp(self):
        import tkinter as tk
        self.tk = tk
        import winmon
        self.winmon = winmon
        self.root = tk.Tk()
        self.root.withdraw()
        self.addCleanup(self.root.destroy)

    def _dialog(self, **kw):
        d = self.winmon.ConfirmDialog(
            self.root, "测试弹窗", "是否关闭当前视频？",
            detail="这是详情文本", **kw)
        d.top.update_idletasks()
        self.addCleanup(lambda: self._destroy(d))
        return d

    @staticmethod
    def _destroy(d):
        try:
            d.top.destroy()
        except Exception:
            pass

    # -- 按键 --
    def test_space_confirms(self):
        d = self._dialog()
        d.top.event_generate("<space>")
        d.top.update()
        self.assertTrue(d.result, "Space 应当确认关闭")
        self.assertTrue(d._closed)

    def test_enter_confirms(self):
        d = self._dialog()
        d.top.event_generate("<Return>")
        d.top.update()
        self.assertTrue(d.result, "Enter 应当确认关闭")

    def test_escape_cancels(self):
        d = self._dialog()
        d.top.event_generate("<Escape>")
        d.top.update()
        self.assertFalse(d.result, "Esc 应当表示继续播放")

    def test_button_yes_and_no(self):
        d = self._dialog()
        d.btn_yes.invoke()
        self.assertTrue(d.result)

        d2 = self._dialog()
        d2.btn_no.invoke()
        self.assertFalse(d2.result)

    def test_late_events_after_close_are_ignored(self):
        """已关闭的弹窗收到迟到的按键不能翻转结果（否则会误关视频）。"""
        d = self._dialog()
        d.btn_yes.invoke()
        self.assertTrue(d.result)
        d._no()
        self.assertTrue(d.result, "迟到的 _no 不应该改结果")
        d._yes()
        self.assertTrue(d.result)

    # -- 倒计时（已删：死代码）--
    def test_no_countdown_widget_or_timer(self):
        """
        「N 秒后自动选否」那套逻辑已作为死代码删除（复查报告 P3）。

        判据是复查时实测出来的：**从来没有任何调用点传过 `seconds > 0`**
        （全项目只有 `_video_check_playing` 建过一个 ConfirmDialog，用的是默认值），
        也就是说那段倒计时永远不会被激活。

        为什么值得留一条测试钉住"别回来"：那段代码表面很贴心，但一个会替你自动
        按「是」的弹窗，一旦被谁顺手接上，就是**用户没点过也把他的视频关了**。
        """
        import inspect

        params = inspect.signature(self.winmon.ConfirmDialog.__init__).parameters
        self.assertNotIn("seconds", params, "ConfirmDialog 又开始接受 seconds 了")
        d = self._dialog()
        for name in ("countdown", "_remaining", "_timer_id", "_tick"):
            with self.subTest(name=name):
                self.assertFalse(hasattr(d, name), f"{name} 又回来了")

    # -- 外观（坑 14 / 15）--
    @staticmethod
    def _red_pixels(widget, color=(0xD3, 0x2F, 0x2F)):
        """
        把控件所在窗口抓下来，数一数目标颜色到底占了多少像素。

        为什么要抓屏：`cget("background")` 对 ttk 控件**会返回你设的值**，
        所以"能不能读到配置"证明不了任何事。实测（Tk 8.6/9.0 + Python 3.14）：
        `ttk.Label(background=…)` 参数被接受、cget 也返回该颜色，
        **但渲染出来仍是主题灰 #f3f3f3** —— 比"参数不被支持"更隐蔽。
        只有数像素才能真正区分"生效"与"被静默忽略"。

        返回值是"容忍 40 灰阶误差的匹配像素数"。
        """
        import tkinter as tk
        top = widget.winfo_toplevel()
        top.update()
        top.update_idletasks()
        root_hwnd = core.H(core.user32.GetAncestor(core.w.HWND(top.winfo_id()),
                                                   core.GA_ROOT))
        rc = core.w.RECT()
        core.user32.GetWindowRect(core.w.HWND(root_hwnd), ctypes.byref(rc))
        ww, hh = max(16, rc.right - rc.left), max(16, rc.bottom - rc.top)
        cap = core.WindowCapturer()
        try:
            raw, ow, oh = cap.grab_window(root_hwnd, ww, hh, ww, hh)
        finally:
            cap.release()
        hit = 0
        for i in range(0, len(raw), 3):
            b, g, r = raw[i], raw[i + 1], raw[i + 2]
            if abs(r - color[0]) <= 40 and abs(g - color[1]) <= 40 and abs(b - color[2]) <= 40:
                hit += 1
        return hit

    def test_ttk_widgets_are_not_given_background_colors(self):
        """
        坑 14 的确定性写法（不做像素实验）。

        实测补充说明：`ttk.Label(background=…)` 的参数**会被接受**、`cget` 也返回该颜色，
        但渲染是否生效取决于主题与父容器 —— 同一段代码在主窗口里被忽略、
        在 Toplevel 里却生效。这种"时灵时不灵"正是坑：看起来配好了，换个位置就失效。
        所以项目约定是：**要背景色就用 tk.Label**，绝不给 ttk 控件传 background。
        这条用 AST 静态检查，不受主题影响。
        """
        import ast

        src = open(os.path.join(PROJECT_DIR, "winmon.py"), encoding="utf-8").read()
        tree = ast.parse(src)
        offenders = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            if not (isinstance(fn, ast.Attribute) and fn.attr in
                    ("Label", "Frame", "Button", "Checkbutton", "Entry")):
                continue
            if not (isinstance(fn.value, ast.Name) and fn.value.id == "ttk"):
                continue
            for kw in node.keywords:
                if kw.arg in ("background", "bg"):
                    offenders.append(f"第 {node.lineno} 行 ttk.{fn.attr}({kw.arg}=…)")
        self.assertEqual(offenders, [],
                         "给 ttk 控件设背景色会被主题忽略（坑 14）：" + "；".join(offenders))

    #: 抓屏重试次数与间隔。抓一个刚建出来的 Tk 窗口是"要等系统真的合成"的通路，
    #: 实测偶发抓到全黑（**不是代码坏了**）—— 全量跑 380 用例时约有一半概率撞上，
    #: 单跑这个文件却几乎不失败。假红灯和噪音一样会让"跑测试全绿"这条纪律失效，
    #: 所以这里重试；**一次都数不到红像素才判失败**，判据本身没有放松。
    CAPTURE_RETRIES = 8
    CAPTURE_GAP = 0.15

    def test_tk_label_background_is_actually_rendered(self):
        """对照实验：tk.Label 的背景是真生效的（所以深色卡片必须用它）。"""
        import tkinter as tk

        top = tk.Toplevel(self.root)
        top.geometry("200x100+120+120")
        self.addCleanup(top.destroy)
        top.update_idletasks()

        lb = tk.Label(top, text="", bg="#d32f2f")
        lb.place(x=10, y=10, width=180, height=80)
        top.update()

        hit = 0
        for attempt in range(self.CAPTURE_RETRIES):
            top.update()
            hit = max(hit, self._red_pixels(lb))
            if hit > 1000:
                break
            time.sleep(self.CAPTURE_GAP)
        self.assertGreater(hit, 1000,
                           f"tk.Label 的红底没渲染出来（{self.CAPTURE_RETRIES} 次抓屏"
                           f"最多只数到 {hit} 个红像素）")

    def test_confirm_dialog_uses_tk_label_for_dark_card(self):
        """
        弹窗的深色卡片必须由 tk.Label 承担。

        判据：弹窗里存在"深色背景 + 浅色文字"的 tk.Label。
        注意不能只比较 cget —— 见上面 ttk 的教训。
        """
        d = self._dialog()
        dark = []
        for child in walk(d.top):
            try:
                if child.winfo_class() != "Label":
                    continue
                bg = str(child.cget("background"))
                fg = str(child.cget("foreground"))
                if bg == "#1b2027" and fg:
                    dark.append((bg, fg))
            except Exception:
                pass
        self.assertTrue(dark, "弹窗里没有找到深色背景的 tk.Label（深色字压黑底会看不清）")

    def test_dialog_is_topmost_and_centered(self):
        d = self._dialog()
        self.assertTrue(d.top.attributes("-topmost"))
        geo = d.top.geometry()
        self.assertRegex(geo, r"^\d+x\d+\+-?\d+\+-?\d+$", f"geometry 异常：{geo}")

    def test_dialog_shows_message_and_detail(self):
        d = self._dialog()
        all_text = " ".join(texts_of(d.top))
        self.assertIn("是否关闭当前视频", all_text)
        self.assertIn("这是详情文本", all_text)
        self.assertIn("Space", all_text, "弹窗里应当写明快捷键说明")

    def test_custom_button_texts(self):
        d = self._dialog(yes_text="确定 (Space)", no_text="取消 (Esc)")
        self.assertEqual(d.btn_yes.cget("text"), "确定 (Space)")
        self.assertEqual(d.btn_no.cget("text"), "取消 (Esc)")

    def test_space_on_focused_button_confirms(self):
        """焦点在按钮上时按 Space 也要确认（按钮默认抢焦点）。"""
        d = self._dialog()
        d.btn_no.event_generate("<space>")
        d.top.update()
        self.assertTrue(d.result)


if __name__ == "__main__":
    unittest.main(verbosity=2)
