# -*- coding: utf-8 -*-
"""
B40 —— 现代视觉层：无衬线字体、胶囊标签、圆角浮片与目录滚动。

用户 2026-10-21 的第二轮界面要求：
  · 按键圆角要为果冻回弹服务；点击“实时预览”等按钮不能出现虚线焦点框；
  · 字体换成现代无衬线；按钮要轻盈，边框改为大范围低透明阴影；
  · 整个窗口/主要表面有圆角，而不是早期 WinForms / WPF 默认控件；
  · 程序目录名称不能盖到图标上，并且要能左右滑动。

配色在 2026-10-21 晚改为 9 套设计主题；默认「晴日天绿」。
"""
from __future__ import annotations

import os
import sys
import time
import unittest
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import _helpers as h                     # noqa: E402


class TestModernPaletteAndType(unittest.TestCase):
    def tearDown(self):
        import winmon
        winmon.apply_theme("sunlit")

    def test_nine_design_themes_and_default(self):
        import winmon
        winmon.apply_theme("sunlit")
        self.assertEqual(winmon.THEME_DEFAULT, "sunlit")
        self.assertEqual(len(winmon.THEME_ORDER), 9)
        self.assertEqual(winmon.UI_BG, "#FAFDF6")
        self.assertEqual(winmon.UI_INK, "#1B2A1F")
        self.assertEqual(winmon.UI_ACCENT, "#2F7F55")
        self.assertEqual(winmon.UI_CARDS["media"]["tag_bg"],
                         winmon.UI_ACCENT)
        self.assertEqual(winmon.UI_MEDIA_BTN_BG, winmon.UI_ACCENT)
        self.assertFalse(hasattr(winmon, "_THEME_PALETTES_LEGACY"))
        self.assertFalse(hasattr(winmon, "_THEME_PALETTES_MODERN"))

    def test_ui_font_is_a_modern_sans_serif(self):
        import winmon
        self.assertEqual(winmon.UI_FONT[0], winmon.UI_FONT_FAMILY)
        self.assertIn(winmon.UI_FONT_FAMILY, winmon.UI_FONT_CANDIDATES)
        self.assertNotIn("Serif", winmon.UI_FONT_FAMILY)
        self.assertTrue(winmon.resolve_ui_font_family())
        self.assertIn(str(winmon.UI_FONT_SIZE), str(winmon.UI_FONT))

    def test_all_design_themes_have_readable_cards(self):
        import winmon
        for theme in winmon.THEME_ORDER:
            winmon.apply_theme(theme)
            for key in ("media", "ai", "mcp", "key", "workspace",
                        "behavior", "proactive"):
                self.assertIn(key, winmon.UI_CARDS)
                self.assertTrue(winmon.UI_CARDS[key]["bg"])


@h.needs_tk
class TestModernWidgets(unittest.TestCase):
    def setUp(self):
        import tkinter as tk
        import winmon
        self.winmon = winmon
        winmon.apply_theme("sunlit")
        self.root = tk.Tk()
        self.root.geometry("900x560+20+20")
        self.addCleanup(h.destroy_root, self.root)
        self.addCleanup(winmon.apply_theme, "light")

    def test_button_focus_draws_no_dashed_or_extra_frame(self):
        btn = self.winmon.RoundedButton(
            self.root, text="实时预览", kind="accent", width=140, height=34)
        btn.pack()
        self.root.update()
        before = [(btn.itemcget(item, "outline"),
                   btn.itemcget(item, "width"))
                  for item in btn.find_all()
                  if "jelly-body" in btn.gettags(item)]
        btn.focus_set()
        self.root.update()
        after = [(btn.itemcget(item, "outline"),
                  btn.itemcget(item, "width"))
                 for item in btn.find_all()
                 if "jelly-body" in btn.gettags(item)]
        self.assertEqual(before, after,
                         "点击/聚焦不应改变按钮描边（不能出现虚线焦点框）")
        self.assertFalse(hasattr(btn, "_has_focus"))

    def test_release_has_visible_jelly_overshoot(self):
        btn = self.winmon.RoundedButton(
            self.root, text="回弹", kind="accent", width=110, height=34)
        btn.pack()
        self.root.update()
        btn._squash = 1.0
        btn._animate_release()
        lowest = 0.0
        deadline = time.time() + 0.9
        while time.time() < deadline:
            self.root.update()
            lowest = min(lowest, float(btn._squash))
            if btn._anim_after is None:
                break
            time.sleep(0.01)
        self.assertLess(lowest, -0.08,
                        "回弹必须越过静止位（果冻感），不能只是线性收回")
        self.assertLess(abs(btn._squash), 0.02)

    def test_tabs_are_painted_capsules_without_native_focus_box(self):
        from tkinter import ttk
        style = ttk.Style(self.root)
        style.layout("Hidden.TNotebook.Tab", [])
        nb = ttk.Notebook(self.root, style="Hidden.TNotebook")
        for label in ("实时预览", "实时总览", "程序目录"):
            nb.add(ttk.Frame(nb), text=label)
        nb.pack(fill="both", expand=True)
        strip = self.winmon.TabStrip(
            self.root, nb, [nb.tab(i, "text") for i in range(len(nb.tabs()))])
        strip.pack(fill="x")
        self.root.update()
        self.assertIsInstance(strip, self.winmon.TabStrip)
        self.assertEqual(int(strip.cget("highlightthickness")), 0,
                         "自绘标签不能有虚线焦点环")
        self.assertEqual(strip.winfo_class(), "Canvas")
        strip._select(2)
        self.root.update()
        self.assertEqual(nb.index("current"), 2)
        self.assertEqual(strip._selected, 2)

    def test_catalog_icon_gutter_and_horizontal_scrollbar(self):
        app = SimpleNamespace(
            root=self.root, dpi_scale=1.5,
            _set_status=lambda _text: None)
        pane = self.winmon.ProgramCatalogPane(self.root, app)
        pane.frame.pack(fill="both", expand=True)
        self.root.update()
        self.assertEqual(str(pane._catalog_hscroll.cget("orient")),
                         "horizontal")
        self.assertTrue(pane.tree.cget("xscrollcommand"),
                        "列表必须把横向滚动条接上")
        self.assertGreaterEqual(int(pane.tree.column("#0", "width")), 80,
                                "图标列要放得下 150% 下的缩进和图标，不能压到名称")
        self.assertGreaterEqual(int(pane.tree.column("name", "width")), 370)
        self.assertNotEqual(tuple(pane.tree.xview()), (0.0, 1.0),
                            "列总宽应超过面板，横向滚动才有可滑动范围")

    def test_rounded_button_font_tracks_live_font_change(self):
        import winmon
        old = winmon.UI_FONT_SIZE
        self.addCleanup(winmon.set_font_size, old)
        winmon.set_font_size(9)
        btn = winmon.RoundedButton(
            self.root, text="实时预览", kind="accent", height=32)
        btn.pack()
        self.root.update_idletasks()
        old_width = int(btn.cget("width"))
        winmon.apply_font_size_live(self.root, 12)
        self.root.update_idletasks()
        self.assertIn("12", str(btn._font),
                      "自绘圆角按钮必须跟字号实时变化，不能等重启")
        self.assertGreater(int(btn.cget("width")), old_width,
                           "字号变大后按钮自然宽度也要跟着放大")

    def test_custom_canvas_labels_follow_language_switch(self):
        import tkinter as tk
        from tkinter import ttk
        import winmon
        import winmon_i18n as i18n
        old_lang = i18n.get_language()
        self.addCleanup(i18n.set_language, old_lang)

        i18n.set_language("zh")
        nb = ttk.Notebook(self.root)
        for label in ("实时预览", "实时总览"):
            nb.add(ttk.Frame(nb), text=label)
        strip = winmon.TabStrip(self.root, nb,
                                [nb.tab(i, "text") for i in range(len(nb.tabs()))])
        btn = winmon.RoundedButton(self.root, text="加入总览", kind="ghost")
        badge = winmon.RoundedBadge(self.root, "媒体", fill="#0f6b5c",
                                    fg="#ffffff", bg="#ffffff")
        i18n.set_language("en")
        i18n.translate_widgets(self.root)
        strip.retranslate()

        self.assertEqual(btn.cget("text"), "Add view")
        self.assertEqual(badge.cget("text"), "Media")
        self.assertEqual(strip._labels, ["Live preview", "Live overview"],
                         "自绘胶囊不能停在中文快照")

    def test_tabs_fit_minimum_width_even_at_large_font(self):
        import tkinter as tk
        from tkinter import ttk
        import winmon
        old = winmon.UI_FONT_SIZE
        self.addCleanup(winmon.set_font_size, old)
        winmon.set_font_size(12)
        nb = ttk.Notebook(self.root)
        labels = ["Live preview", "Live overview", "Process tree",
                  "Programs", "Workspace", "AI workspace"]
        for label in labels:
            nb.add(ttk.Frame(nb), text=label)
        strip = winmon.TabStrip(self.root, nb, labels)
        strip.configure(width=600, height=40)
        strip.pack()
        self.root.update_idletasks()
        rects, total = strip._layout()
        self.assertLessEqual(total, 600, "标签总宽不能超过画布")
        self.assertLessEqual(rects[-1][2], 600,
                             "最后一项不能被推到画布外")
        for x1, _y1, x2, _y2, _label in rects:
            self.assertGreaterEqual(x2 - x1, 32,
                                    "再挤也要保留可点的最小宽度")

    def test_rail_button_has_soft_shadow_room_and_no_hard_rim(self):
        rail_bg = self.winmon.mix_color(
            self.winmon.UI_BG, self.winmon.UI_SURFACE_ALT, 0.72)
        btn = self.winmon.RoundedButton(
            self.root, text="加入总览", kind="rail", bg=rail_bg,
            height=32, radius=16, padx=13, lead=self.winmon.UI_ACCENT,
            shadow_room=5)
        btn.pack()
        self.root.update()
        shadows, body = [], []
        for item in btn.find_all():
            tags = btn.gettags(item)
            if "jelly-shadow" in tags:
                shadows.append(item)
            if "jelly-body" in tags:
                body.append(item)
        self.assertGreaterEqual(len(shadows), 5,
                                "栏内按钮需要多层低透明投影")
        self.assertTrue(body)
        self.assertEqual(str(btn.itemcget(body[0], "outline")), "",
                         "栏内按钮不能再用硬描边，层次交给阴影和高光")

    def test_soft_panel_and_badge_are_rounded_canvas_layers(self):
        panel = self.winmon.SoftPanel(self.root, "#ffffff",
                                      radius=18, shadow=4)
        panel.pack(fill="x", padx=10, pady=10)
        badge = self.winmon.RoundedBadge(
            panel.content, "媒体", fill="#0f6b5c", fg="#ffffff",
            bg="#ffffff")
        badge.pack()
        self.root.update()
        self.assertTrue(panel.content.winfo_ismapped())
        self.assertGreater(len(panel.canvas.find_all()), 1)
        self.assertGreater(len(badge.find_all()), 1)
        self.assertGreaterEqual(badge.winfo_width(), badge.winfo_reqwidth() - 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
