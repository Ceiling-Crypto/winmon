# -*- coding: utf-8 -*-
"""
B39 —— 果冻按钮、整窗透明度与媒体栏拟光。

用户 2026-10-21 的要求：
  · 按键按下有阻尼感，松手果冻回弹，视觉上有 3D 错觉；
  · 界面透明度可调，默认 100%，最低只淡到还能看见窗口边缘；
    透明度要影响二级菜单；
  · 媒体真正播放后，媒体高亮卡出现由淡到深的拟光，并同样受透明度影响。
"""
from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import _helpers as h                     # noqa: E402


class TestOpacityAndColorMath(unittest.TestCase):
    def tearDown(self):
        import winmon
        winmon.set_ui_opacity(winmon.UI_OPACITY_DEFAULT)

    def test_opacity_is_clamped_and_never_invisible(self):
        import winmon
        self.assertEqual(winmon.UI_OPACITY_DEFAULT, 100)
        self.assertEqual(winmon.UI_OPACITY_MIN, 45)
        self.assertEqual(winmon.normalize_opacity(100), 100)
        self.assertEqual(winmon.normalize_opacity(0), 45)
        self.assertEqual(winmon.normalize_opacity(-20), 45)
        self.assertEqual(winmon.normalize_opacity(999), 100)
        self.assertEqual(winmon.normalize_opacity("bad"), 100)
        self.assertAlmostEqual(winmon.opacity_to_alpha(45), 0.45)
        self.assertAlmostEqual(winmon.opacity_to_alpha(100), 1.0)

    def test_read_opacity_from_settings(self):
        import winmon
        with tempfile.TemporaryDirectory(prefix="winmon-b39-opacity-") as tmp:
            path = os.path.join(tmp, "settings.json")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write('{"ui_opacity": 72}')
            self.assertEqual(winmon.read_ui_opacity(path), 72)
            with open(path, "w", encoding="utf-8") as fh:
                fh.write('{"opacity": 30}')
            self.assertEqual(winmon.read_ui_opacity(path), 45,
                             "旧键也认，但必须夹到可见下限")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("{broken")
            self.assertEqual(winmon.read_ui_opacity(path), 100)

    def test_color_mixing_for_3d_and_glow(self):
        import winmon
        self.assertEqual(winmon.mix_color("#000000", "#ffffff", 0), "#000000")
        self.assertEqual(winmon.mix_color("#000000", "#ffffff", 1), "#ffffff")
        self.assertEqual(winmon.mix_color("#000000", "#ffffff", 0.5), "#808080")
        self.assertEqual(winmon.mix_color("not-a-color", "#ffffff", 0.5),
                         "not-a-color")

    def test_guide_documents_opacity_and_feel(self):
        import winmon
        zh = dict(winmon._GUIDE_CONTENT["zh"])["主界面"]
        main = "\n".join(body for _kind, body in zh)
        for piece in ("界面透明度", "45%", "右键菜单", "阻尼", "拟光",
                      "由淡到深", "暂停"):
            self.assertIn(piece, main)
        en = dict(winmon._GUIDE_CONTENT["en"])["Main window"]
        en_main = "\n".join(body for _kind, body in en)
        for piece in ("Window opacity", "45%", "context menus", "ambient"):
            self.assertIn(piece, en_main)


@h.needs_tk
class TestOpacityRuntime(unittest.TestCase):
    def setUp(self):
        import tkinter as tk
        import winmon
        self.winmon = winmon
        winmon.set_ui_opacity(100)
        self.root = tk.Tk()
        self.root.withdraw()
        self.addCleanup(self.root.destroy)
        self.addCleanup(winmon.set_ui_opacity, 100)

    def test_root_and_secondary_window_follow_opacity(self):
        import tkinter as tk
        second = tk.Toplevel(self.root)
        second.withdraw()
        self.addCleanup(second.destroy)
        self.winmon.apply_opacity_to_windows(self.root, 65)
        self.assertAlmostEqual(float(self.root.attributes("-alpha")), 0.65)
        self.assertAlmostEqual(float(second.attributes("-alpha")), 0.65)
        self.assertEqual(self.winmon.UI_OPACITY, 65)
        self.winmon.apply_opacity_to_windows(self.root, 100)
        self.assertAlmostEqual(float(self.root.attributes("-alpha")), 1.0)
        self.assertAlmostEqual(float(second.attributes("-alpha")), 1.0)


@h.needs_tk
class TestJellyButtonAndAmbientGlow(unittest.TestCase):
    def setUp(self):
        import tkinter as tk
        import winmon
        self.winmon = winmon
        winmon.apply_theme("light")
        self.root = tk.Tk()
        self.root.withdraw()
        self.addCleanup(self.root.destroy)
        self.addCleanup(winmon.apply_theme, "light")

    def test_button_draws_3d_layers_and_animates(self):
        btn = self.winmon.RoundedButton(
            self.root, text="执行", kind="accent", width=120, height=32)
        btn.pack()
        self.root.update_idletasks()
        tags = set()
        for item in btn.find_all():
            tags.update(btn.gettags(item))
        self.assertIn("jelly-shadow", tags, "必须有底部投影")
        self.assertIn("jelly-body", tags)
        self.assertIn("jelly-highlight", tags, "必须有顶部高光")
        self.assertEqual(btn._squash, 0.0)

        btn._on_press()
        self.root.update()
        time.sleep(0.09)
        self.root.update()
        self.assertGreater(btn._squash, 0.1, "按下应压扁，而不是瞬间换色")

        btn._on_release()
        self.assertIsNotNone(btn._anim_after, "松手后要启动回弹动画")
        deadline = time.time() + 0.9
        while time.time() < deadline:
            self.root.update()
            time.sleep(0.02)
            if btn._anim_after is None:
                break
        self.assertIsNone(btn._anim_after)
        self.assertLess(abs(btn._squash), 0.02, "回弹最终要收住")

    def test_ambient_glow_fades_from_base_to_accent(self):
        media = self.winmon.UI_CARDS["media"]
        glow = self.winmon.AmbientGlowFrame(self.root, media)
        glow.pack(fill="x")
        self.root.update_idletasks()
        base = media["bg"]
        self.assertEqual(str(glow.cget("bg")), base)
        self.assertEqual(glow.intensity, 0.0)

        glow.set_level(1.0, animate=False)
        self.root.update_idletasks()
        self.assertEqual(glow.intensity, 1.0)
        outer = str(glow.cget("bg"))
        self.assertNotEqual(outer, base, "播放时高亮卡边缘必须亮起")
        self.assertEqual(outer, self.winmon.mix_color(
            base, media["tag_bg"], 0.43))

        glow.set_level(0.0, animate=False)
        self.root.update_idletasks()
        self.assertEqual(str(glow.cget("bg")), base)

    def test_destroy_with_pending_animation_is_clean(self):
        """
        定时器还挂着时销毁控件不能抛 “can't delete Tcl command”。

        这是真踩到的坑：Python 3.14 里 after 回调执行后命令已被 Tcl 删除，
        `Misc._tclCommands` 却可能还留着名字；root.destroy 再删一次就中断，
        整个应用关不掉（全量 test_b8 卡住）。控件自己的 destroy() 要先清账。
        """
        btn = self.winmon.RoundedButton(
            self.root, text="回弹", kind="accent", width=90, height=30)
        btn.pack()
        btn._animate_release()
        self.assertIsNotNone(btn._anim_after)
        btn.destroy()

        media = self.winmon.UI_CARDS["media"]
        glow = self.winmon.AmbientGlowFrame(self.root, media)
        glow.pack()
        glow.set_level(1.0)
        self.assertIsNotNone(glow._anim_after)
        glow.destroy()

    def test_media_ambience_tracks_session_and_title_fallback(self):
        media = self.winmon.UI_CARDS["media"]
        glow = self.winmon.AmbientGlowFrame(self.root, media)
        glow.pack()
        self.root.update_idletasks()

        class Stub:
            pass

        app = Stub()
        app.media_glow = glow
        app._media_found = True
        app._media_playing = True
        app._media_source = "session"
        self.winmon.WinMonApp._sync_media_ambience(app, animate=False)
        self.assertEqual(glow.intensity, 1.0)

        app._media_found = False
        app._media_playing = False
        app._media_source = "title"
        self.winmon.WinMonApp._sync_media_ambience(app, animate=False)
        self.assertEqual(glow.intensity, 0.5,
                         "标题兜底只给半档光，不能装作完整会话")

        app._media_source = ""
        self.winmon.WinMonApp._sync_media_ambience(app, animate=False)
        self.assertEqual(glow.intensity, 0.0)


@h.needs_tk
class TestSettingsOpacityAndPopupMenu(unittest.TestCase):
    class FakeApp:
        dpi_scale = 1.0
        _ai_sessions = None

        def __init__(self, root):
            self.root = root

        def _ai_provider_snapshot(self):
            return {}

        def _set_status(self, _text):
            pass

        def action_ai_assistant(self):
            pass

    def setUp(self):
        import tkinter as tk
        import winmon
        import winmon_i18n as i18n
        self.winmon = winmon
        winmon.apply_theme("light")
        i18n.set_language("zh")
        self.root = tk.Tk()
        self.root.withdraw()
        self.addCleanup(self.root.destroy)
        self.addCleanup(winmon.apply_theme, "light")
        self.addCleanup(i18n.set_language, "zh")
        self.addCleanup(winmon.apply_opacity_to_windows, self.root, 100)
        self.app = self.FakeApp(self.root)

    def _open(self, opacity=100):
        with mock.patch.object(self.winmon.core, "get_autostart",
                               return_value=False):
            dlg = self.winmon._SettingsWindow(
                self.root, self.app, "exit", False, False, 30,
                "zh", 9, "light", opacity)
        self.addCleanup(dlg._cancel)
        return dlg

    def test_slider_previews_and_cancel_restores(self):
        dlg = self._open(100)
        self.assertEqual(dlg.opacity.get(), 100)
        dlg.opacity.set(65)
        dlg._on_opacity_change()
        self.assertAlmostEqual(float(self.root.attributes("-alpha")), 0.65)
        self.assertEqual(dlg.opacity_text.get(), "65%")

        dlg.opacity.set(45)
        dlg._on_opacity_change()
        self.assertAlmostEqual(float(self.root.attributes("-alpha")), 0.45)

        dlg._cancel()
        self.assertAlmostEqual(float(self.root.attributes("-alpha")), 1.0,
                               msg="取消设置必须回到打开前的透明度")
        self.assertIsNone(dlg.result)

    def test_save_returns_opacity_value(self):
        dlg = self._open(100)
        dlg.opacity.set(58)
        dlg._on_opacity_change()
        with mock.patch.object(self.winmon.core, "get_autostart",
                               return_value=False):
            dlg._ok()
        self.assertEqual(dlg.result["ui_opacity"], 58)

    def test_popup_menu_is_transparent_and_matches_menu_api(self):
        self.winmon.set_ui_opacity(70)
        menu = self.winmon.PopupMenu(self.root, font=self.winmon.UI_FONT)
        menu.add_command(label="第一项", command=lambda: None)
        menu.add_separator()
        menu.add_command(label="第二项", command=lambda: None)
        self.assertEqual(menu.index("end"), 2)
        self.assertEqual(menu.type(1), "separator")
        self.assertEqual(menu.entrycget(0, "label"), "第一项")
        menu.entryconfigure(0, label="改名")
        self.assertEqual(menu.entrycget(0, "label"), "改名")

        seen = {}

        def close():
            seen["alpha"] = float(menu.attributes("-alpha"))
            menu._close()

        self.root.after(50, close)
        menu.tk_popup(40, 40)
        # 菜单窗口关闭后就不能再查属性了，所以关闭前记下来。
        self.assertAlmostEqual(seen["alpha"], 0.70,
                               msg="自绘二级菜单必须跟随界面透明度")


if __name__ == "__main__":
    unittest.main(verbosity=2)
