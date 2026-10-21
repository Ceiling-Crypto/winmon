# -*- coding: utf-8 -*-
"""
B37 —— 深色中性卡 + `--fast` 测试筛选。

2026-10-22 第四轮起，媒体/AI/MCP/KEY 等不再铺整块栏目色；所有卡统一
surface + line，栏目色只留在小徽标和状态文字上。这里守两件事：
  1. 10 套主题的卡片都跟当前 surface 走，深色主题下发白即失败；
     文字/状态仍要满足 4.5:1 对比度。
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
        winmon.apply_theme("sunlit")

    def test_every_theme_has_all_neutral_cards(self):
        import winmon
        for theme in winmon.THEME_ORDER:
            winmon.apply_theme(theme)
            for key in CARD_KEYS:
                self.assertIn(key, winmon.UI_CARDS, f"{theme} 缺少 {key} 卡片")
                card = winmon.UI_CARDS[key]
                for field in ("bg", "fg", "muted", "border"):
                    self.assertTrue(card.get(field), f"{theme}/{key} 缺 {field}")

    def test_all_theme_cards_keep_contrast(self):
        import winmon
        for theme in winmon.THEME_ORDER:
            winmon.apply_theme(theme)
            page = winmon.UI_BG
            dark = luminance(page) < 0.35
            for key in CARD_KEYS:
                card = winmon.UI_CARDS[key]
                self.assertNotEqual(card["bg"], page,
                                    f"{theme}/{key} 卡和页面同色")
                if dark:
                    self.assertLess(luminance(card["bg"]), 0.35,
                                    f"{theme}/{key} 深色卡不应发白")
                else:
                    self.assertGreater(luminance(card["bg"]), 0.7,
                                       f"{theme}/{key} 卡应是浅色高亮卡")
                for field in ("fg", "muted", "state", "warn"):
                    if card.get(field):
                        self.assertGreaterEqual(
                            contrast(card[field], card["bg"]), 4.5,
                            f"{theme}/{key}.{field} 对比度不足")
                if card.get("tag_bg") and card.get("tag_fg"):
                    self.assertGreaterEqual(
                        contrast(card["tag_fg"], card["tag_bg"]), 4.5,
                        f"{theme}/{key} 色标文字对比度不足")
                for prefix in ("state_ok", "state_warn"):
                    bg, fg = card.get(f"{prefix}_bg"), card.get(f"{prefix}_fg")
                    if bg and fg:
                        self.assertGreaterEqual(
                            contrast(fg, bg), 4.5,
                            f"{theme}/{key}.{prefix} 对比度不足")

    def test_connect_status_pairs_are_readable_in_all_themes(self):
        import winmon
        for theme in winmon.THEME_ORDER:
            winmon.apply_theme(theme)
            card = winmon.UI_CARDS["connect"]
            for prefix in ("ok", "warn"):
                self.assertGreaterEqual(
                    contrast(card[f"{prefix}_fg"], card[f"{prefix}_bg"]), 4.5,
                    f"{theme}/{prefix} 接入状态条对比度不足")

    def test_neutral_cards_track_surface_and_never_tint(self):
        """2026-10-22 第四轮：卡片不再用栏目色铺底，全部跟 surface / line 走。"""
        import winmon
        for theme in winmon.THEME_ORDER:
            winmon.apply_theme(theme)
            for key in CARD_KEYS:
                card = winmon.UI_CARDS[key]
                self.assertEqual(card["bg"], winmon.UI_SURFACE,
                                 f"{theme}/{key} 仍是有色高亮卡")
                self.assertEqual(card["border"], winmon.UI_LINE,
                                 f"{theme}/{key} 仍带栏目色边框")
                self.assertEqual(card["fg"], winmon.UI_INK)


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
class TestThemeConnectWindow(unittest.TestCase):
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
        winmon.apply_theme("snow")
        self.addCleanup(winmon.apply_theme, "sunlit")
        self.root = tk.Tk()
        self.root.withdraw()
        self.addCleanup(h.destroy_root, self.root)
        self.app = self.FakeApp()

    def test_all_card_surfaces_use_current_theme_palette(self):
        win = self.winmon._AiConnectWindow(self.root, self.app)
        self.addCleanup(win.close)
        self.root.update_idletasks()
        cards = self.winmon.UI_CARDS
        #: 接入状态从整块色条收成“中性卡 + 语义色文字”。
        self.assertEqual(str(win.status_frame.cget("bg")),
                         self.winmon.UI_SURFACE)
        self.assertEqual(str(win.status_label.cget("bg")),
                         self.winmon.UI_SURFACE)
        self.assertIn(str(win.status_label.cget("fg")),
                      (cards["connect"]["ok_fg"],
                       cards["connect"]["warn_fg"]))
        self.assertEqual(str(win.mcp_state_label.cget("bg")), cards["mcp"]["bg"])
        self.assertEqual(str(win.config_text.cget("bg")),
                         cards["mcp"]["code_bg"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
