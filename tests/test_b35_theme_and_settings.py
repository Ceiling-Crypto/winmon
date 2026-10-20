# -*- coding: utf-8 -*-
"""
B35 —— 颜色选择（普通 / 深色 / 跟随系统）与「设置」三合一。

用户 2026-10-20 的要求：
  · AI 用量、设置、说明合并，主界面只保留一个「设置」入口；
  · 深色模式要可用，尤其是媒体行右侧 3 个按键不能糊在卡片里；
  · 窗口标题只叫 WinMon，不再挂“实时监控与终止”的旧后缀；
  · 请求关闭与强制结束本质不同，必须在界面上说清楚。

这里守住可程序化的部分：色板切换、系统跟随、三标签页、媒体按键样式、
实时重建、标题文案、两个操作的解释。
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import _helpers as h                     # noqa: E402
import winmon_i18n as i18n               # noqa: E402


class TestThemeSelection(unittest.TestCase):
    def tearDown(self):
        import winmon
        winmon.apply_theme("light")
        i18n.set_language("zh")

    def test_normalize_accepts_aliases_and_rejects_junk(self):
        import winmon
        self.assertEqual(winmon.normalize_theme_mode("normal"), "light")
        self.assertEqual(winmon.normalize_theme_mode("white"), "light")
        self.assertEqual(winmon.normalize_theme_mode("auto"), "system")
        self.assertEqual(winmon.normalize_theme_mode("dark"), "dark")
        self.assertEqual(winmon.normalize_theme_mode("blue"), "light")

    def test_dark_palette_is_black_with_white_text(self):
        import winmon
        self.assertEqual(winmon.apply_theme("dark"), "dark")
        self.assertEqual(winmon.UI_BG, "#000000")
        self.assertEqual(winmon.UI_INK, "#ffffff")
        self.assertNotEqual(winmon.UI_SURFACE_ALT, winmon.UI_BG)
        self.assertEqual(winmon.THEME_PREFERENCE, "dark")
        self.assertEqual(winmon.UI_MEDIA_BTN_BG, "#0f6b5c",
                         "媒体按键在浅绿卡片上仍要用深绿底白字")

    def test_system_mode_reads_windows_setting(self):
        import winmon
        with mock.patch.object(winmon, "detect_system_theme",
                               return_value="dark"):
            self.assertEqual(winmon.apply_theme("system"), "dark")
        self.assertEqual(winmon.THEME_PREFERENCE, "system")
        self.assertEqual(winmon.UI_BG, "#000000")

    def test_read_theme_mode_from_settings(self):
        import winmon
        with tempfile.TemporaryDirectory(prefix="winmon-b35-theme-") as tmp:
            path = os.path.join(tmp, "settings.json")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write('{"theme": "dark"}')
            self.assertEqual(winmon.read_theme_mode(path), "dark")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write('{"theme": "broken"}')
            self.assertEqual(winmon.read_theme_mode(path), "light")

    def test_labels_follow_language(self):
        import winmon
        i18n.set_language("zh")
        self.assertEqual([slug for slug, _ in winmon.theme_choices()],
                         ["light", "dark", "system"])
        self.assertIn("普通", winmon.theme_display("light"))
        i18n.set_language("en")
        self.assertIn("Light", winmon.theme_display("light"))
        self.assertIn("Follow", winmon.theme_display("system"))


class TestBrandingAndActionMeaning(unittest.TestCase):
    def test_window_title_is_only_winmon(self):
        import winmon
        i18n.set_language("zh")
        title = i18n.t("app.title", ver=winmon.APP_VER)
        self.assertEqual(title, "WinMon 1.0")
        self.assertNotIn("—", title)
        self.assertNotIn("终止", title)
        self.assertFalse(hasattr(winmon, "APP_TITLE"),
                         "旧的带后缀标题常量已经删除，避免又被用回去")

    def test_close_and_terminate_are_different(self):
        import winmon
        labels = dict(winmon.PREVIEW_ACTIONS)
        self.assertIn("请求关闭", labels["close"])
        self.assertIn("强制结束", labels["terminate"])
        close_detail = winmon.preview_action_detail("close")
        end_detail = winmon.preview_action_detail("terminate")
        self.assertIn("保存", close_detail)
        self.assertIn("丢失", end_detail)
        self.assertNotEqual(close_detail, end_detail)


@h.needs_tk
class TestSettingsMergedWindow(unittest.TestCase):
    class FakeApp:
        dpi_scale = 1.0
        _ai_sessions = None

        def _ai_provider_snapshot(self):
            return {}

        def _set_status(self, _text):
            pass

        def action_ai_assistant(self):
            pass

    def setUp(self):
        import tkinter as tk
        import winmon
        winmon.apply_theme("light")
        i18n.set_language("zh")
        self.root = tk.Tk()
        self.root.withdraw()
        self.addCleanup(self.root.destroy)
        self.addCleanup(winmon.apply_theme, "light")
        self.addCleanup(i18n.set_language, "zh")
        self.winmon = winmon

    def _open(self):
        with mock.patch.object(self.winmon.core, "get_autostart",
                               return_value=False):
            dlg = self.winmon._SettingsWindow(
                self.root, self.FakeApp(), "exit", False, False, 30,
                "zh", 9, "light")
        self.addCleanup(dlg._cancel)
        return dlg

    def test_one_settings_window_has_three_tabs(self):
        dlg = self._open()
        labels = [dlg.nb.tab(i, "text") for i in range(len(dlg.nb.tabs()))]
        self.assertEqual(labels, ["常规", "AI 用量", "使用说明"])
        self.assertEqual(dlg.top.title(), "WinMon 设置")
        self.assertFalse(hasattr(self.winmon, "_AiUsageWindow"))
        self.assertFalse(hasattr(self.winmon, "_GuideWindow"))

    def test_theme_choice_is_saved_and_panels_close(self):
        dlg = self._open()
        self.assertEqual(dlg.theme.get(), "普通（白底）")
        dlg.theme.set("深色（黑底）")
        with mock.patch.object(self.winmon.core, "get_autostart",
                               return_value=False):
            dlg._ok()
        self.assertEqual(dlg.result["theme"], "dark")
        self.assertFalse(dlg.usage._alive,
                         "关设置时必须取消 AI 用量标签页的 3 秒定时器")

    def test_guide_and_usage_are_reachable_from_the_same_window(self):
        dlg = self._open()
        dlg.select("guide", "ai")
        self.assertEqual(dlg.nb.index("current"), 2)
        self.assertEqual(dlg.guide.nb.index("current"), 1)
        dlg.select("ai")
        self.assertEqual(dlg.nb.index("current"), 1)
        self.assertIn("预算", dlg.usage.body.get("1.0", "end"))


@h.needs_tk
class TestDarkMainWindow(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import tkinter as tk
        import winmon
        import winmon_core as core
        cls.winmon = winmon
        cls.root = tk.Tk()
        cls.root.geometry("1160x730+0+0")
        winmon.apply_theme("dark")
        app = cls.app = winmon.WinMonApp.__new__(winmon.WinMonApp)
        app.root = cls.root
        app._settings_path = os.path.join(tempfile.mkdtemp(prefix="winmon-b35-"),
                                          "settings.json")
        app._settings_after_id = None
        app.history = core.ActionHistory(log_path=os.path.join(
            tempfile.mkdtemp(prefix="winmon-b35-"), "history.log"))
        app.show_hidden = tk.BooleanVar(value=False)
        app.show_no_window = tk.BooleanVar(value=False)
        app.only_windowed = tk.BooleanVar(value=True)
        app.follow_selection = tk.BooleanVar(value=True)
        app.live_gallery = tk.BooleanVar(value=True)
        app.fps_choice = tk.StringVar(value="很流畅 10 FPS")
        app.search_text = tk.StringVar()
        app.status_text = tk.StringVar(value="就绪")
        app.overview_text = tk.StringVar(value="")
        app._fg_hwnd = 0
        app._initial_preview_done = True
        app._cpu_prev = {}
        app._cpu_prev_t = 0.0
        app._cpu_pct = {}
        app._path_cache = {}
        app._init_video_vars()
        app._build_ui()
        app.dpi_scale = 1.0
        cls.root.update()

    @classmethod
    def tearDownClass(cls):
        h.destroy_root(cls.root)
        cls.winmon.apply_theme("light")

    def test_page_is_black_but_media_card_stays_highlighted(self):
        self.assertEqual(str(self.root.cget("background")), "#000000")
        self.assertNotEqual(str(self.app.media_label.cget("bg")), "#000000")
        self.assertNotEqual(str(self.app.ai_state_label.cget("bg")), "#000000")

    def test_three_media_buttons_use_high_contrast_style(self):
        from tkinter import ttk
        style = ttk.Style(self.root)
        self.assertEqual(style.lookup("Media.TButton", "background"),
                         self.winmon.UI_MEDIA_BTN_BG)
        self.assertEqual(style.lookup("Media.TButton", "foreground"),
                         self.winmon.UI_MEDIA_BTN_FG)
        for btn in (self.app.media_play_btn, self.app.media_prev_btn,
                    self.app.media_next_btn):
            self.assertEqual(str(btn.cget("style")), "Media.TButton")

    def test_checkbox_and_action_popup_are_visible_on_black(self):
        normal = self.root._winmon_check_images["normal"]
        self.assertNotEqual(tuple(normal.get(7, 7)), (0, 0, 0),
                            "未选框不能和纯黑背景糊成一块")
        pane = self.app.preview
        self.addCleanup(pane.close_actions)
        pane.open_actions()
        close_row, _box, close_txt, _detail, _col = pane._action_rows["close"]
        end_row = pane._action_rows["terminate"][0]
        self.assertEqual(str(close_row.cget("bg")), self.winmon.UI_ACCENT)
        self.assertEqual(str(close_txt.cget("fg")), self.winmon.UI_ON_ACCENT)
        self.assertEqual(str(end_row.cget("bg")), self.winmon.UI_BG)

    def test_z_live_theme_rebuild_restores_the_checkbox_and_buttons(self):
        self.winmon.apply_theme("light")
        self.app.selected_hwnd = None
        self.app.preview_key = None
        # 这里只验证“换色后控件确实重建了”；真实窗口数据刷新由主流程测试覆盖。
        try:
            with mock.patch.object(self.app, "refresh_processes"), \
                    mock.patch.object(self.app, "refresh_windows"):
                self.app._rebuild_ui_for_theme(0)
            self.root.update_idletasks()
            self.assertEqual(str(self.root.cget("background")), "#ffffff")
            self.assertEqual(str(self.app.media_play_btn.cget("style")),
                             "Media.TButton")
            self.assertTrue(self.app.action_rail.winfo_exists())
        finally:
            # 这个类共用一套控件；把状态还给深色，避免依赖测试执行顺序。
            self.winmon.apply_theme("dark")
            with mock.patch.object(self.app, "refresh_processes"), \
                    mock.patch.object(self.app, "refresh_windows"):
                self.app._rebuild_ui_for_theme(0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
