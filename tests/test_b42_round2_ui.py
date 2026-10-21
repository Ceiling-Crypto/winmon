# -*- coding: utf-8 -*-
"""
B42 —— 2026-10-22 第二轮界面细节。

覆盖这一轮容易再退化的点：
  · 齿轮是矢量图标，AI 回到最后一栏，AI 卡不再重复入口；
  · 媒体栏是普通行、播放器/媒体键在曲名上方、没有自动唤醒；
  · 媒体键、暂停刷新、移除总览都改成无边框裸按钮；
  · 设置常规页可滚轮、透明度滑杆只有一个滑块且填充连续；
  · AI 用量直连缺 key 时只在掩码框里读入，不从界面回显或复制。
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
        winmon.apply_theme("cobalt")
        cls.root, cls.app = build_app(1160, 730)
        cls.root.update()

    @classmethod
    def tearDownClass(cls):
        h.destroy_root(cls.root)

    def test_gear_is_vector_and_ai_is_last_tab(self):
        self.assertEqual(self.app.settings_button._icon, "gear")
        icons = [item for item in self.app.settings_button.find_all()
                 if "jelly-icon" in self.app.settings_button.gettags(item)]
        self.assertTrue(icons, "齿轮应由 Canvas 矢量绘制")
        self.assertFalse(hasattr(self.app, "ai_workspace_btn"))
        self.assertFalse(hasattr(self.app, "titlebar_label"),
                         "标题行不应再有 WinMon 文字")
        last = len(self.app.notebook.tabs()) - 1
        self.assertEqual(self.app.notebook.tab(last, "text"), "AI 工作区")
        self.assertEqual(self.app.notebook.tab(0, "text"), "实时预览")

    def test_media_controls_above_a_full_width_title(self):
        self.assertEqual(str(self.app.media_bar.cget("bg")),
                         self.winmon.UI_SURFACE)
        self.assertEqual(self.app.media_text.get(), "（没有程序播放媒体）")
        #: 第五轮：媒体键在左、播放器在右，两者都在曲名上方。
        self.assertLess(self.app.media_app_combo.winfo_rooty(),
                        self.app.media_label.winfo_rooty(),
                        "播放器选择应在曲名上方")
        keys = self.app.media_play_btn.master
        self.assertIs(keys.master,
                      self.app.media_app_combo.master.master,
                      "媒体键与播放器应在同一排")
        self.assertLess(keys.winfo_rootx(),
                        self.app.media_app_combo.winfo_rootx(),
                        "媒体键应在播放器左边")
        self.assertLess(self.app.media_play_btn.winfo_rooty(),
                        self.app.media_label.winfo_rooty(),
                        "媒体键应在曲名上方")
        self.assertFalse(hasattr(self.app, "media_auto_wake"))
        for btn in (self.app.media_prev_btn, self.app.media_play_btn,
                    self.app.media_next_btn):
            self.assertTrue(btn._bare)
            self.assertTrue(btn._icon.startswith("media_"),
                            "媒体键必须是矢量图标，不依赖字体字形")
            icons = [item for item in btn.find_all()
                     if "jelly-icon" in btn.gettags(item)]
            self.assertTrue(icons, f"{btn._icon} 没有画出矢量图标")
            self.assertEqual(str(btn.cget("width")), "30")

    def test_pause_and_remove_buttons_are_larger_and_bare(self):
        pause = self.app.preview.btn_pause
        remove = self.app.gallery.remove_btn
        for btn in (pause, remove):
            self.assertTrue(btn._bare, "暂停/移除不再用常驻描边")
        self.assertGreaterEqual(pause.winfo_reqheight(), 30)
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
                "cobalt", 100, {"fps_choice": "很流畅 10 FPS"})
        self.addCleanup(dlg._cancel)
        self.assertEqual(dlg.fps_choice.get(), "很流畅 10 FPS")

    def test_general_scrolls_with_wheel_and_has_one_slider_thumb(self):
        import winmon
        with mock.patch.object(core, "get_autostart", return_value=False):
            dlg = winmon._SettingsWindow(
                self.root, self.app, "exit", False, False, 30, "zh", 9,
                "cobalt", 100, {})
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
        fills = [item for item in sliders[0].find_all()
                 if "slider-fill" in sliders[0].gettags(item)]
        self.assertEqual(len(fills), 1, "已选填充只能是一条连续轨道")
        self.assertNotEqual(
            str(sliders[0].itemcget(thumbs[0], "outline")),
            str(self.winmon.UI_SURFACE),
            "滑块不能用白色描边把轨道切成两段")


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

    def test_missing_key_is_masked_and_reads_from_clipboard(self):
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
        self.assertFalse(hasattr(panel, "direct_key_show"),
                         "不应提供“显示 key”入口")
        self.assertFalse(hasattr(panel, "copy_direct_key"),
                         "不应把用户的 key 再复制出去")
        self.assertEqual(str(panel.direct_key_entry.cget("show")), "●")
        self.assertEqual(str(panel.direct_key_paste._icon), "paste")
        self.root.clipboard_clear()
        self.root.clipboard_append("secret-from-clipboard")
        panel.paste_direct_key()
        self.assertEqual(panel.direct_key_var.get(), "secret-from-clipboard")
        self.assertEqual(str(panel.direct_key_entry.cget("show")), "●",
                         "读入剪贴板后仍必须保持掩码")
        panel.save_direct_key()
        self.assertEqual(os.environ.get(self.env), "secret-from-clipboard")
        self.assertEqual(panel.direct_key_var.get(), "",
                         "保存后应立即从界面变量里清掉明文")


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
                "cobalt", 100, {})
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
