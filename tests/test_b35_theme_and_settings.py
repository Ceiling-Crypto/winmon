# -*- coding: utf-8 -*-
"""
B35 —— 设计主题与「设置」三合一。

用户 2026-10-21 的决定：
  · 旧普通/深色两套删掉，改用设计主题；
  · 只换颜色，按键的果冻回弹、圆角、阴影不动；
  · 旧设置里的 light / dark / system 要安全迁移；
  · 2026-10-22 收敛为 6 套，旧 dark 映射到「深色」；
  · 窗口标题只叫 winmon1.0；关闭窗口与强制结束必须说清楚。

这里守住可程序化的部分：6 套色板、旧值迁移、三标签页、媒体按键样式、
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
        winmon.apply_theme("cobalt")
        i18n.set_language("zh")

    def test_design_themes_and_default(self):
        import winmon
        self.assertEqual(len(winmon.THEME_ORDER), 6)
        self.assertEqual(winmon.THEME_DEFAULT, "cobalt")
        self.assertEqual(winmon.apply_theme("cobalt"), "cobalt")
        self.assertEqual(winmon.THEME_PREFERENCE, "cobalt")
        self.assertEqual(winmon.UI_BG, "#F3F8FE")
        self.assertEqual(winmon.UI_ACCENT, "#064ED0")
        self.assertEqual(winmon.UI_INK, "#0B1B2B")
        self.assertEqual(
            [slug for slug, _label in winmon.theme_choices()],
            list(winmon.THEME_ORDER))
        self.assertEqual(len({slug for slug, _label in winmon.theme_choices()}),
                         6, "主题 slug 不能重复")
        self.assertEqual(winmon.apply_theme("edge_dark"), "edge_dark")
        self.assertEqual(winmon.UI_BG, "#202124")
        self.assertEqual(winmon.UI_ACCENT, "#2F6FCC")

    def test_normalize_accepts_aliases_and_rejects_junk(self):
        import winmon
        for old in ("normal", "white", "light", "auto", "system"):
            self.assertEqual(winmon.normalize_theme_mode(old), "cobalt",
                             f"旧值 {old} 应迁移到默认主题")
        for old in ("dark", "black", "night"):
            self.assertEqual(winmon.normalize_theme_mode(old), "edge_dark")
        self.assertEqual(winmon.normalize_theme_mode("snow"), "snow")
        for removed in ("sunlit", "amber", "coral", "cloud"):
            self.assertEqual(winmon.normalize_theme_mode(removed), "cobalt",
                             f"已删除的 {removed} 应回默认钴蓝")
        self.assertEqual(winmon.normalize_theme_mode("blue"), "cobalt")
        self.assertEqual(winmon.normalize_theme_mode(""), "cobalt")
        self.assertFalse(hasattr(winmon, "THEME_SYSTEM"))
        self.assertFalse(hasattr(winmon, "detect_system_theme"))

    def test_old_settings_values_migrate_safely(self):
        """旧设置里可能还存着 light / dark / system；不能卡启动。"""
        import winmon
        with tempfile.TemporaryDirectory(prefix="winmon-b35-legacy-") as tmp:
            path = os.path.join(tmp, "settings.json")
            for old, want in (("system", "cobalt"), ("light", "cobalt"),
                              ("dark", "edge_dark"), ("sunlit", "cobalt"),
                              ("amber", "cobalt"), ("coral", "cobalt"),
                              ("cloud", "cobalt")):
                with open(path, "w", encoding="utf-8") as fh:
                    fh.write('{"theme": "%s"}' % old)
                self.assertEqual(winmon.read_theme_mode(path), want)
        self.assertEqual(winmon.apply_theme("system"), "cobalt")
        self.assertEqual(winmon.THEME_PREFERENCE, "cobalt")
        self.assertEqual(winmon.UI_BG, "#F3F8FE")

    def test_read_theme_mode_from_settings(self):
        import winmon
        with tempfile.TemporaryDirectory(prefix="winmon-b35-theme-") as tmp:
            path = os.path.join(tmp, "settings.json")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write('{"theme": "snow"}')
            self.assertEqual(winmon.read_theme_mode(path), "snow")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write('{"theme": "broken"}')
            self.assertEqual(winmon.read_theme_mode(path), "cobalt")

    def test_labels_follow_language_and_are_design_names(self):
        import winmon
        i18n.set_language("zh")
        labels = dict(winmon.theme_choices())
        self.assertEqual(labels["cobalt"], "钴蓝")
        self.assertEqual(labels["snow"], "山雪")
        self.assertEqual(labels["edge_dark"], "深色")
        self.assertNotIn("cloud", labels)
        for label in labels.values():
            self.assertEqual(len(label), 2, "中文主题名统一为两个字")
        self.assertNotIn("月行水上", "".join(labels.values()),
                         "主题名应是设计名，不是专辑名")
        i18n.set_language("en")
        english = dict(winmon.theme_choices())
        self.assertEqual(english["cobalt"], "Cobalt")
        self.assertEqual(english["edge_dark"], "Dark")
        self.assertNotIn("Follow", winmon.theme_display("cobalt"))


class TestBrandingAndActionMeaning(unittest.TestCase):
    def test_window_title_is_only_winmon(self):
        import winmon
        i18n.set_language("zh")
        title = i18n.t("app.title", ver=winmon.APP_VER)
        self.assertEqual(title, "winmon1.0")
        self.assertEqual(winmon.APP_NAME, "winmon1.0")
        self.assertNotIn("WinMon", title)
        self.assertNotIn("—", title)
        self.assertNotIn("终止", title)
        self.assertFalse(hasattr(winmon, "APP_TITLE"),
                         "旧的带后缀标题常量已经删除，避免又被用回去")

    def test_proactive_heading_has_no_hotkey_or_default_note(self):
        """2026-10-20 用户要求：主动搭子标题旁不再挂「C，默认关」。"""
        for lang in ("zh", "en"):
            i18n.set_language(lang)
            text = i18n.t("connect.proactive")
            self.assertNotIn("默认关", text)
            self.assertNotIn("off by default", text.lower())
            self.assertNotIn("C，", text)
            self.assertNotIn("C,", text)
        i18n.set_language("zh")

    def test_icon_pack_has_modern_sizes_and_valid_dib_frames(self):
        """图标容器要同时含小尺寸和 256，且每一帧都是 32 位 DIB（不是 PNG）。"""
        import struct
        import winmon
        path = winmon._icon_path()
        self.assertTrue(path and os.path.exists(path), path)
        with open(path, "rb") as fh:
            data = fh.read()
        self.assertEqual(data[:4], b"\x00\x00\x01\x00")
        count = struct.unpack_from("<H", data, 4)[0]
        sizes = set()
        for i in range(count):
            entry = 6 + 16 * i
            width = data[entry] or 256
            length, offset = struct.unpack_from("<II", data, entry + 8)
            sizes.add(width)
            self.assertNotEqual(data[offset:offset + 8],
                                b"\x89PNG\r\n\x1a\n", f"{width}px 不能是 PNG-in-ICO")
            (header_size, dib_w, dib_h, planes, bits,
             _compression, image_size) = struct.unpack_from(
                "<IiiHHII", data, offset)
            self.assertEqual((header_size, dib_w, dib_h, planes, bits),
                             (40, width, width * 2, 1, 32))
            self.assertEqual(image_size, width * width * 4)
            mask_stride = ((width + 31) // 32) * 4
            self.assertEqual(length, 40 + image_size + mask_stride * width)
        self.assertTrue({16, 32, 48, 256}.issubset(sizes), sizes)

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
                "zh", 9, "cobalt")
        self.addCleanup(dlg._cancel)
        return dlg

    def test_one_settings_window_has_four_tabs(self):
        dlg = self._open()
        labels = [dlg.nb.tab(i, "text") for i in range(len(dlg.nb.tabs()))]
        self.assertEqual(labels, ["常规", "AI 用量", "使用说明", "关于"])
        self.assertEqual(dlg.top.title(), "winmon1.0 设置")
        self.assertFalse(hasattr(self.winmon, "_AiUsageWindow"))
        self.assertFalse(hasattr(self.winmon, "_GuideWindow"))

    def test_theme_choice_is_saved_and_panels_close(self):
        dlg = self._open()
        labels = [label for _slug, label in self.winmon.theme_choices()]
        self.assertEqual(list(dlg._theme_by_label), labels,
                         "设置里应列出全部设计主题")
        self.assertEqual(dlg.theme.get(), "钴蓝")
        dlg.theme.set("山雪")
        with mock.patch.object(self.winmon.core, "get_autostart",
                               return_value=False):
            dlg._ok()
        self.assertEqual(dlg.result["theme"], "snow")
        self.assertFalse(dlg.usage._alive,
                         "关设置时必须取消 AI 用量标签页的 3 秒定时器")

    def test_guide_and_usage_are_reachable_from_the_same_window(self):
        dlg = self._open()
        dlg.select("guide", "ai")
        self.assertEqual(dlg.nb.index("current"), 2)
        self.assertEqual(dlg.guide.nb.index("current"), 1)
        dlg.select("ai")
        self.assertEqual(dlg.nb.index("current"), 1)
        self.assertIn("预算", dlg.usage.budget_text.get())


@h.needs_tk
class TestThemeMainWindow(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import tkinter as tk
        import winmon
        import winmon_core as core
        cls.winmon = winmon
        cls.root = tk.Tk()
        cls.root.geometry("1160x730+0+0")
        winmon.apply_theme("cobalt")
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
        cls.winmon.apply_theme("cobalt")

    def test_page_and_status_band_stay_distinct(self):
        self.assertEqual(str(self.root.cget("background")), "#F3F8FE")
        ai = self.winmon.UI_CARDS["ai"]
        #: 媒体和 AI 共用一条中性状态带，底色是 surface，不是页面。
        self.assertEqual(str(self.app.media_label.cget("bg")),
                         self.winmon.UI_SURFACE)
        self.assertEqual(str(self.app.ai_identity_label.cget("bg")), ai["bg"])
        self.assertEqual(str(self.app.ai_state_label.cget("bg")),
                         ai["state_warn_bg"])
        self.assertEqual(str(self.app.ai_workspace._state_label.cget("bg")),
                         ai["state_warn_bg"])

    def test_three_media_buttons_use_the_same_compact_style(self):
        for btn in (self.app.media_play_btn, self.app.media_prev_btn,
                    self.app.media_next_btn):
            self.assertIsInstance(btn, self.winmon.RoundedButton)
            self.assertTrue(btn._bare, "媒体键应是裸按钮，只在悬停时浮出薄底")
            self.assertEqual(str(btn.cget("width")), "30")
            self.assertEqual(str(btn.cget("height")), "30")

    def test_checkbox_and_top_right_controls_are_visible_on_light(self):
        normal = self.root._winmon_check_images["normal"]
        self.assertNotEqual(tuple(normal.get(7, 7)), (0, 0, 0),
                            "未选框不能和纯黑背景糊成一块")
        self.assertTrue(self.app.search_field.winfo_exists())
        self.assertEqual(str(self.app.settings_button.cget("text")), "⚙")
        self.assertFalse(hasattr(self.app.preview, "action_button"),
                         "预览下方的操作条已经按用户要求删除")

    def test_z_live_theme_rebuild_restores_the_checkbox_and_buttons(self):
        self.winmon.apply_theme("neon")
        self.app.selected_hwnd = None
        self.app.preview_key = None
        # 这里只验证“换色后控件确实重建了”；真实窗口数据刷新由主流程测试覆盖。
        try:
            with mock.patch.object(self.app, "refresh_processes"), \
                    mock.patch.object(self.app, "refresh_windows"):
                self.app._rebuild_ui_for_theme(0)
            self.root.update_idletasks()
            self.assertEqual(str(self.root.cget("background")),
                             self.winmon.UI_BG)
            self.assertIsInstance(self.app.media_play_btn,
                                  self.winmon.RoundedButton)
            self.assertTrue(self.app.media_play_btn._bare)
            self.assertTrue(self.app.search_field.winfo_exists())
            self.assertTrue(self.app.settings_button.winfo_exists())
        finally:
            # 这个类共用一套控件；把状态还回默认，避免依赖测试执行顺序。
            self.winmon.apply_theme("cobalt")
            with mock.patch.object(self.app, "refresh_processes"), \
                    mock.patch.object(self.app, "refresh_windows"):
                self.app._rebuild_ui_for_theme(0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
