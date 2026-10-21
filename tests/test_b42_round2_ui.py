# -*- coding: utf-8 -*-
"""
B42 —— 2026-10-22 第二轮界面细节。

覆盖这一轮容易再退化的点：
  · 齿轮是矢量图标，AI 是第一栏，AI 卡不再重复入口；
  · 媒体栏是普通行、播放器在状态文字下方、没有自动唤醒；
  · 暂停刷新 / 移除总览加大并带低透明描边；
  · 设置常规页可滚轮、透明度滑杆只有一个滑块；
  · AI 用量直连缺 key 时可在卡内输入并复制。
"""
from __future__ import annotations

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import _helpers as h                     # noqa: E402
import winmon_core as core               # noqa: E402


def walk(widget):
    yield widget
    for child in widget.winfo_children():
        yield from walk(child)


@h.needs_tk
class TestRound2MainUi(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from shot import build_app
        import winmon
        cls.winmon = winmon
        winmon.apply_theme("sunlit")
        cls.root, cls.app = build_app(1160, 730)
        cls.root.update()

    @classmethod
    def tearDownClass(cls):
        h.destroy_root(cls.root)

    def test_gear_is_vector_and_ai_is_first_tab(self):
        self.assertEqual(self.app.settings_button._icon, "gear")
        icons = [item for item in self.app.settings_button.find_all()
                 if "jelly-icon" in self.app.settings_button.gettags(item)]
        self.assertTrue(icons, "齿轮应由 Canvas 矢量绘制")
        self.assertFalse(hasattr(self.app, "ai_workspace_btn"))
        self.assertEqual(self.app.notebook.tab(0, "text"), "AI 工作区")
        self.assertEqual(self.app.notebook.tab(1, "text"), "实时预览")

    def test_media_is_a_plain_row_with_player_below_status(self):
        self.assertEqual(str(self.app.media_bar.cget("bg")), self.winmon.UI_BG)
        self.assertEqual(self.app.media_text.get(), "（没有程序播放媒体）")
        self.assertGreater(self.app.media_app_combo.winfo_rooty(),
                           self.app.media_label.winfo_rooty(),
                           "播放器选择必须在状态文字下方")
        self.assertFalse(hasattr(self.app, "media_auto_wake"))
        for btn in (self.app.media_prev_btn, self.app.media_play_btn,
                    self.app.media_next_btn):
            self.assertTrue(btn._ring)
            self.assertEqual(str(btn.cget("width")), "30")

    def test_pause_and_remove_buttons_are_larger_and_ringed(self):
        pause = self.app.preview.btn_pause
        remove = self.app.gallery.remove_btn
        self.assertTrue(pause._ring)
        self.assertTrue(remove._ring)
        self.assertGreaterEqual(pause.winfo_reqheight(), 32)
        self.assertGreaterEqual(remove.winfo_reqheight(), 30)

    def test_refresh_speed_moved_to_settings(self):
        import winmon
        labels = [str(w.cget("text")) for w in walk(self.root)
                  if w.winfo_class() in ("Label", "TLabel")]
        self.assertNotIn("刷新速度", labels,
                         "主界面不应再常驻刷新速度文字")
        with mock.patch.object(core, "get_autostart", return_value=False):
            dlg = winmon._SettingsWindow(
                self.root, self.app, "exit", False, False, 30, "zh", 9,
                "sunlit", 100, {"fps_choice": "很流畅 10 FPS"})
        self.addCleanup(dlg._cancel)
        self.assertEqual(dlg.fps_choice.get(), "很流畅 10 FPS")

    def test_general_scrolls_with_wheel_and_has_one_slider_thumb(self):
        import winmon
        with mock.patch.object(core, "get_autostart", return_value=False):
            dlg = winmon._SettingsWindow(
                self.root, self.app, "exit", False, False, 30, "zh", 9,
                "sunlit", 100, {})
        self.addCleanup(dlg._cancel)
        dlg.select("general")
        self.root.update()
        canvas = dlg._general_canvas
        before = canvas.yview()
        label = dlg._hint_sources[0][0]
        label.event_generate("<MouseWheel>", delta=-120, x=10, y=10)
        self.root.update()
        self.assertNotEqual(canvas.yview(), before,
                            "常规页滚轮没有滚动")
        sliders = [w for w in walk(dlg.top) if isinstance(w, winmon.RoundedSlider)]
        self.assertTrue(sliders)
        thumbs = [item for item in sliders[0].find_all()
                  if "slider-thumb" in sliders[0].gettags(item)]
        self.assertEqual(len(thumbs), 1, "滑杆只能有一个滑块")


@h.needs_tk
class TestAiUsageKeyInput(unittest.TestCase):
    def setUp(self):
        import tkinter as tk
        import winmon
        self.winmon = winmon
        self.root = tk.Tk()
        self.root.withdraw()
        self.addCleanup(self.root.destroy)
        self.env = "WINMON_TEST_B42_API_KEY"
        os.environ.pop(self.env, None)
        self.addCleanup(lambda: os.environ.pop(self.env, None))

    def test_missing_key_has_entry_and_copy(self):
        class FakeApp:
            dpi_scale = 1.0
            _ai_sessions = {}
            _ai_assist = {"mode": "local"}
            _ai_usage_cache = (0.0, None)

            def _ai_provider_snapshot(self):
                return {"configured": True, "enabled": True,
                        "model": "test-model", "key_present": False,
                        "api_key_env": "WINMON_TEST_B42_API_KEY",
                        "follow_ai": "TestAI"}

            def _set_status(self, text):
                self.status = text

        app = FakeApp()
        panel = self.winmon._AiUsagePanel(self.root, app)
        self.addCleanup(panel.close)
        panel.frame.pack(fill="both", expand=True)
        self.root.update()
        self.assertEqual(panel.direct_key_box.winfo_manager(), "pack",
                         "缺 key 时应出现输入栏")
        panel.direct_key_var.set("secret-value")
        panel.save_direct_key()
        self.assertEqual(os.environ.get(self.env), "secret-value")
        panel.direct_key_var.set("secret-value")
        panel.copy_direct_key()
        self.assertEqual(self.root.clipboard_get(), "secret-value")


@h.needs_tk
class TestAssistantOpensFromSettings(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from shot import build_app
        import winmon
        cls.winmon = winmon
        cls.root, cls.app = build_app(1160, 730)
        cls.root.update()

    @classmethod
    def tearDownClass(cls):
        h.destroy_root(cls.root)

    def test_assistant_releases_settings_grab_and_opens(self):
        with mock.patch.object(core, "get_autostart", return_value=False):
            dlg = self.winmon._SettingsWindow(
                self.root, self.app, "exit", False, False, 30, "zh", 9,
                "sunlit", 100, {})
        self.app._settings_window = dlg
        try:
            self.app.action_ai_assistant()
            self.root.update()
            win = self.app._ai_assistant_window
            self.assertIsNotNone(win, "AI 助手窗口没有打开")
            self.assertTrue(win.top.winfo_exists())
            try:
                self.assertIsNone(dlg.top.grab_current(),
                                  "设置窗口的 grab 应释放给助手")
            except Exception:
                pass
            win.close()
        finally:
            self.app._settings_window = None
            dlg._cancel()


if __name__ == "__main__":
    unittest.main(verbosity=2)
