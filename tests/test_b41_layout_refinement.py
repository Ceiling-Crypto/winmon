# -*- coding: utf-8 -*-
"""
B41 —— 2026-10-21 主界面减负与设置收口。

守住这一轮的行为边界：
  · 搜索/设置在右上角，主界面不再有动作竖栏、加入总览、刷新列表；
  · 视频联动默认收起，设置里打开后才占一行；
  · 窗口过滤、闲置提醒、操作历史/清理都从设置进入；
  · 实时总览自己带「移除总览」，并且按点选的格子删；
  · 预览/总览两个标签有不同图标，但仍共用同一套 Notebook 行为。
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import _helpers as h                     # noqa: E402


def walk(widget):
    yield widget
    for child in widget.winfo_children():
        yield from walk(child)


@h.needs_tk
class TestMainWindowRefinement(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import tkinter as tk
        import winmon
        import winmon_core as core
        cls.winmon = winmon
        winmon.apply_theme("cobalt")
        cls.root = tk.Tk()
        cls.root.geometry("1160x730+0+0")
        app = cls.app = winmon.WinMonApp.__new__(winmon.WinMonApp)
        app.root = cls.root
        app._settings_path = os.path.join(tempfile.mkdtemp(prefix="winmon-b41-"),
                                          "settings.json")
        app._settings_after_id = None
        app.history = core.ActionHistory(log_path=os.path.join(
            tempfile.mkdtemp(prefix="winmon-b41-"), "history.log"))
        app.show_hidden = tk.BooleanVar(value=False)
        app.show_no_window = tk.BooleanVar(value=False)
        app.only_windowed = tk.BooleanVar(value=True)
        app.follow_selection = tk.BooleanVar(value=True)
        app.live_gallery = tk.BooleanVar(value=True)
        app.fps_choice = tk.StringVar(value="很流畅 10 FPS")
        app.search_text = tk.StringVar()
        app.status_text = tk.StringVar(value="就绪")
        app.overview_text = tk.StringVar(value="")
        app._init_video_vars()
        app.gallery_keys = {}
        app.gallery_order = []
        app._build_ui()
        app.dpi_scale = 1.0
        app._set_status = lambda _text: None
        cls.root.update()

    @classmethod
    def tearDownClass(cls):
        h.destroy_root(cls.root)
        cls.winmon.apply_theme("cobalt")

    def test_main_has_search_gear_and_no_action_rail(self):
        self.assertFalse(hasattr(self.app, "action_rail"))
        self.assertEqual(str(self.app.settings_button.cget("text")), "⚙")
        self.assertTrue(self.app.search_field.winfo_exists())
        labels = [str(w.cget("text")) for w in walk(self.root)
                  if w.winfo_class() in ("Button", "TButton", "Label", "TLabel")]
        for gone in ("加入总览", "刷新列表", "操作历史…", "清理程序文件…"):
            self.assertNotIn(gone, labels, f"主界面不应再常驻「{gone}」")

    def test_video_bar_is_opt_in(self):
        bar = self.app.video_bar
        self.assertEqual(bar.winfo_manager(), "",
                         "视频联动默认应收起")
        self.app.video_link_visible.set(True)
        self.app._apply_video_visibility()
        self.root.update()
        self.assertEqual(bar.winfo_manager(), "pack")
        self.app.video_link_visible.set(False)
        self.app._apply_video_visibility()
        self.root.update()
        self.assertEqual(bar.winfo_manager(), "")

    def test_right_click_menu_has_refresh(self):
        self.assertEqual(self.app.menu.entrycget(0, "label"), "刷新列表")

    def test_preview_and_overview_have_distinct_glyphs(self):
        self.assertTrue(self.app.tab_strip._display_label(0).startswith("▷"))
        self.assertTrue(self.app.tab_strip._display_label(1).startswith("▦"))
        self.assertEqual(self.app.notebook.tab(0, "text"), "实时预览")
        self.assertEqual(self.app.notebook.tab(1, "text"), "实时总览")
        self.assertEqual(self.app.notebook.tab(
            self.winmon.TAB_AI, "text"), "AI 工作区")

    def test_gallery_remove_uses_the_selected_tile(self):
        self.app.gallery.add(9001, "测试窗口", "说明")
        self.app.gallery_keys[9001] = "测试窗口"
        try:
            self.app.gallery.select(9001)
            self.assertEqual(self.app.gallery.selected_key, 9001)
            self.app.gallery_remove_selected()
            self.assertNotIn(9001, self.app.gallery.tiles)
            self.assertNotIn(9001, self.app.gallery_keys)
        finally:
            self.app.gallery_keys.pop(9001, None)
            self.app.gallery.remove(9001)

    def test_settings_save_the_moved_options(self):
        from unittest import mock as _mock
        prefs = {"only_windowed": True, "video_link_visible": False,
                 "idle_alert": False, "idle_choice": "30 分钟",
                 "idle_snooze": "1 小时"}
        with _mock.patch.object(self.winmon.core, "get_autostart",
                                return_value=False):
            dlg = self.winmon._SettingsWindow(
                self.root, self.app, "exit", False, False, 30, "zh", 9,
                "cobalt", 100, prefs)
            dlg.only_windowed.set(False)
            dlg.video_link_visible.set(True)
            dlg.idle_alert.set(True)
            dlg.idle_choice.set("2 小时")
            dlg._ok()
        got = dlg.result
        self.assertIsNotNone(got)
        self.assertFalse(got["window_prefs"]["only_windowed"])
        self.assertTrue(got["video_link_visible"])
        self.assertTrue(got["idle_alert"])
        self.assertEqual(got["idle_choice"], "2 小时")

    def test_settings_hold_the_moved_options(self):
        from unittest import mock as _mock
        prefs = {
            "only_windowed": True, "show_hidden": False,
            "show_no_window": False, "follow_selection": True,
            "live_gallery": True, "video_link_visible": False,
            "idle_alert": False, "idle_choice": "30 分钟",
            "idle_snooze": "1 小时",
        }
        with _mock.patch.object(self.winmon.core, "get_autostart",
                                return_value=False):
            dlg = self.winmon._SettingsWindow(
                self.root, self.app, "exit", False, False, 30, "zh", 9,
                "cobalt", 100, prefs)
        self.addCleanup(dlg._cancel)
        texts = [str(w.cget("text")) for w in walk(dlg.top)
                 if w.winfo_class() in ("Label", "TLabel", "TButton", "Button",
                                        "TCheckbutton")]
        for want in ("窗口列表", "主界面内容", "维护", "显示运行窗口",
                     "显示视频联动操作行", "闲置提醒", "操作历史…",
                     "清理程序文件…"):
            self.assertIn(want, texts)


if __name__ == "__main__":
    unittest.main(verbosity=2)
