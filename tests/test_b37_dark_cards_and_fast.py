# -*- coding: utf-8 -*-
"""
B37 —— 深色高亮卡 + `--fast` 测试筛选。

2026-10-20 用户决定：
  · 先不做“跟随系统”实时监听，随后进一步删掉整个「跟随系统」选项；
  · 其余建议继续做完 —— 其中两项就是**深色主题下的高亮卡**和**测试提速**。

这里守两件事：
  1. 深色主题的媒体/AI/接入卡必须真的变深色，且文字对比度够；
     不能只是把页面刷黑、卡片仍留着刺眼的浅色块。
  2. `run_tests.py --fast` 只跳过带 slow 标记的端到端用例；
     标记工具本身零依赖，不影响普通发现。
"""
from __future__ import annotations

import os
import sys
import tkinter as tk
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import _helpers as h                     # noqa: E402
import _marks                            # noqa: E402
import run_tests                         # noqa: E402

CARD_KEYS = ("media", "ai", "mcp", "key", "workspace", "behavior", "proactive")


def _channel(value: int) -> float:
    c = value / 255.0
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def luminance(color: str) -> float:
    """WCAG 相对亮度（只认 #rrggbb）。"""
    raw = color.lstrip("#")
    r, g, b = (int(raw[i:i + 2], 16) for i in (0, 2, 4))
    return 0.2126 * _channel(r) + 0.7152 * _channel(g) + 0.0722 * _channel(b)


def contrast(a: str, b: str) -> float:
    """前景/背景对比度；WCAG AA 正文要求 ≥ 4.5。"""
    la, lb = sorted((luminance(a), luminance(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def walk(widget):
    yield widget
    for child in widget.winfo_children():
        yield from walk(child)


class TestCardPalettes(unittest.TestCase):
    def tearDown(self):
        import winmon
        winmon.apply_theme("light")

    def test_every_theme_has_all_highlight_cards(self):
        import winmon
        for theme in ("light", "dark"):
            winmon.apply_theme(theme)
            for key in CARD_KEYS:
                self.assertIn(key, winmon.UI_CARDS, f"{theme} 缺少 {key} 卡片")
                card = winmon.UI_CARDS[key]
                for field in ("bg", "fg", "muted", "border"):
                    self.assertTrue(card.get(field), f"{theme}/{key} 缺 {field}")

    def test_dark_cards_are_dark_and_text_keeps_contrast(self):
        import winmon
        winmon.apply_theme("dark")
        for key in CARD_KEYS:
            card = winmon.UI_CARDS[key]
            self.assertNotEqual(card["bg"], "#000000",
                                f"{key} 卡不能和纯黑页面糊在一起")
            self.assertLess(luminance(card["bg"]), 0.18,
                            f"{key} 卡在深色主题下仍是浅色底")
            for field in ("fg", "muted", "state", "warn"):
                if card.get(field):
                    self.assertGreaterEqual(
                        contrast(card[field], card["bg"]), 4.5,
                        f"{key}.{field} 在深色卡片上对比度不足")
            if card.get("tag_bg") and card.get("tag_fg"):
                self.assertGreaterEqual(contrast(card["tag_fg"], card["tag_bg"]), 4.5,
                                        f"{key} 色标文字对比度不足")
            for prefix in ("state_ok", "state_warn"):
                bg, fg = card.get(f"{prefix}_bg"), card.get(f"{prefix}_fg")
                if bg and fg:
                    self.assertGreaterEqual(contrast(fg, bg), 4.5,
                                            f"{key}.{prefix} 对比度不足")

    def test_connect_status_pairs_are_readable_in_both_themes(self):
        import winmon
        for theme in ("light", "dark"):
            winmon.apply_theme(theme)
            card = winmon.UI_CARDS["connect"]
            for prefix in ("ok", "warn"):
                self.assertGreaterEqual(
                    contrast(card[f"{prefix}_fg"], card[f"{prefix}_bg"]), 4.5,
                    f"{theme}/{prefix} 接入状态条对比度不足")

    def test_cards_are_dark_glass_and_change_between_themes(self):
        import winmon
        winmon.apply_theme("light")
        light = {key: winmon.UI_CARDS[key]["bg"] for key in CARD_KEYS}
        light_page = winmon.UI_BG
        winmon.apply_theme("dark")
        dark_page = winmon.UI_BG
        self.assertNotEqual(light_page, dark_page,
                            "两套主题必须有不同的页面底色")
        for key, old_bg in light.items():
            self.assertNotEqual(winmon.UI_CARDS[key]["bg"], old_bg,
                                f"{key} 卡在深色主题下没有换底色")
            self.assertLess(luminance(old_bg), 0.25,
                            f"{key} 卡的默认版本也应是深蓝玻璃，不是浅色补丁")
            self.assertGreater(luminance(old_bg), luminance(light_page),
                               f"{key} 卡必须比页面亮一档，否则没有卡片层次")
            self.assertGreater(luminance(winmon.UI_CARDS[key]["bg"]),
                               luminance(dark_page),
                               f"{key} 卡在深色主题下也要比纯黑页亮")


class _Case:
    def __init__(self, name: str, marked: bool = False):
        self._name = name
        self._marked = marked

    def id(self) -> str:
        return self._name

    def __call__(self):
        """让 unittest.TestSuite 接受这个轻量替身（只用于过滤测试）。"""
        return None

    def __getattr__(self, item):
        if item == _marks.SLOW_ATTR and self._marked:
            return "假的慢用例"
        raise AttributeError(item)


class TestFastFilter(unittest.TestCase):
    def test_slow_decorator_marks_class_and_method(self):
        @_marks.slow("类级")
        class Marked(unittest.TestCase):
            def test_one(self):
                pass

        class Methods(unittest.TestCase):
            @_marks.slow("方法级")
            def test_slow(self):
                pass

            def test_fast(self):
                pass

        self.assertTrue(_marks.is_slow(Marked("test_one")))
        self.assertEqual(_marks.slow_reason(Marked("test_one")), "类级")
        self.assertTrue(_marks.is_slow(Methods("test_slow")))
        self.assertFalse(_marks.is_slow(Methods("test_fast")))

    def test_partition_keeps_all_fast_cases_in_order(self):
        cases = [_Case("a"), _Case("b", True), _Case("c")]
        fast, slow = run_tests.partition_slow(cases)
        self.assertEqual([case.id() for case in fast], ["a", "c"])
        self.assertEqual([case.id() for case in slow], ["b"])

    def test_select_cases_filters_keyword_then_fast(self):
        suite = unittest.TestSuite([
            _Case("test_alpha"), _Case("test_beta", True), _Case("test_gamma")])
        cases = run_tests.select_cases(suite, "test_")
        self.assertEqual(len(cases), 3)
        fast, slow = run_tests.partition_slow(cases)
        self.assertEqual(len(fast), 2)
        self.assertEqual(len(slow), 1)
        self.assertEqual([case.id() for case in run_tests.select_cases(suite, "beta")],
                         ["test_beta"])

    def test_run_tests_exposes_fast_switch_in_help(self):
        with open(os.path.abspath(run_tests.__file__), encoding="utf-8") as fh:
            source = fh.read()
        self.assertIn("--fast", source)
        self.assertIn("partition_slow", source)


@h.needs_tk
class TestDarkConnectWindow(unittest.TestCase):
    class FakeApp:
        dpi_scale = 1.0
        _ai_assist = {"mode": "local", "break_enabled": True,
                      "workspace_dir": "", "enabled": False,
                      "auto_explain": False}
        _ai_connect_window = None

        def __init__(self):
            self.saved = []

        def _ai_provider_snapshot(self):
            return {"api_key_env": "WINMON_AI_API_KEY"}

        def _ai_connection_snapshot(self):
            return {"kind": "clipboard", "ai": "DeepSeek"}

        def set_assist(self, **kw):
            self.saved.append(kw)
            self._ai_assist.update(kw)
            return dict(self._ai_assist)

        def action_ai_connect(self):
            pass

        def _update_ai_bar(self):
            pass

    def setUp(self):
        import winmon
        import winmon_i18n as i18n
        self.winmon = winmon
        self.i18n = i18n
        i18n.set_language("zh")
        self.addCleanup(i18n.set_language, "zh")
        winmon.apply_theme("dark")
        self.addCleanup(winmon.apply_theme, "light")
        self.root = tk.Tk()
        self.root.withdraw()
        self.addCleanup(h.destroy_root, self.root)
        self.app = self.FakeApp()

    def test_all_card_surfaces_use_dark_palette(self):
        win = self.winmon._AiConnectWindow(self.root, self.app)
        self.addCleanup(win.close)
        self.root.update_idletasks()
        cards = self.winmon.UI_CARDS
        self.assertEqual(str(win.status_frame.cget("bg")),
                         cards["connect"]["warn_bg"])
        self.assertEqual(str(win.mcp_state_label.cget("bg")), cards["mcp"]["bg"])
        self.assertEqual(str(win.config_text.cget("bg")),
                         cards["mcp"]["code_bg"])
        light_bgs = {self.winmon._THEME_PALETTES["light"]["UI_CARDS"][key]["bg"]
                     for key in CARD_KEYS}
        found = []
        for widget in walk(win.top):
            if widget.winfo_class() not in ("Frame", "Label", "Text", "Canvas"):
                continue
            try:
                bg = str(widget.cget("bg"))
            except tk.TclError:
                continue
            if bg in light_bgs:
                found.append((widget.winfo_class(), bg))
        self.assertEqual(found, [], "深色接入窗口里还残留浅色卡片底")


if __name__ == "__main__":
    unittest.main(verbosity=2)
