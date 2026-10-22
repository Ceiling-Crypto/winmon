# -*- coding: utf-8 -*-
"""
B46 —— 2026-10-22 第七轮：可选桌面宠物（大肥鱼）。

守住四件事：
  · 精灵素材齐全，尺寸档位能互相对上；
  · 位置夹取不会把桌宠丢到屏幕外；
  · 点一下会回调（气泡由主程序打开），拖动会回报位置；
  · 设置里开关/尺寸即时生效，点「取消」会还原。
"""
from __future__ import annotations

import os
import sys
import unittest
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import _helpers as h                     # noqa: E402
import winmon_pet as petmod              # noqa: E402


class TestPetAssets(unittest.TestCase):
    def test_every_size_has_all_views(self):
        heights = petmod.available_heights()
        self.assertEqual(set(heights), {187, 238, 306})
        for height in heights:
            for name in ("front", "side", "side_left", "back"):
                path = petmod.sprite_path(height, name)
                self.assertTrue(os.path.isfile(path), path)
                self.assertGreater(os.path.getsize(path), 1000, path)

    def test_size_choice_round_trip(self):
        for label, height in petmod.SIZE_PRESETS:
            self.assertEqual(petmod.height_for_choice(label), height)
            self.assertEqual(petmod.choice_for_height(height), label)
        self.assertEqual(petmod.height_for_choice("不存在"), 238)
        self.assertEqual(petmod.choice_for_height("坏值"), "中")


class TestPetGeometry(unittest.TestCase):
    def test_clamp_keeps_window_inside(self):
        self.assertEqual(petmod.clamp_position(5000, 5000, 200, 300,
                                               0, 0, 1920, 1080),
                         (1720, 780))
        self.assertEqual(petmod.clamp_position(-50, -20, 200, 300,
                                               0, 0, 1920, 1080),
                         (0, 0))

    def test_default_position_sits_at_bottom_right(self):
        x, y = petmod.default_position(200, 300, (0, 0, 1920, 1040))
        self.assertEqual(x, 1920 - 200 - petmod.EDGE_MARGIN)
        self.assertEqual(y, 1040 - 300 - petmod.EDGE_MARGIN)


@h.needs_tk
class TestDesktopPetWindow(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import tkinter as tk
        cls.root = tk.Tk()
        cls.root.withdraw()

    @classmethod
    def tearDownClass(cls):
        h.destroy_root(cls.root)

    def test_create_show_move_and_destroy(self):
        clicks = []
        pet = petmod.DesktopPet(
            self.root, height=238, x=100, y=100,
            on_click=lambda _p: clicks.append("click"))
        self.root.update()
        try:
            self.assertEqual(pet.bounds()[2:], (190, 254))
            self.assertEqual(pet.bounds()[:2], (100, 100))
            import ctypes
            from ctypes import wintypes
            user32 = ctypes.windll.user32
            user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
            user32.GetAncestor.restype = wintypes.HWND
            user32.GetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int]
            user32.GetWindowLongPtrW.restype = ctypes.c_longlong
            hwnd = user32.GetAncestor(wintypes.HWND(pet.top.winfo_id()), 2)
            style = int(user32.GetWindowLongPtrW(hwnd, -20) or 0)
            self.assertTrue(style & 0x08000000, "桌宠不应抢焦点")
            self.assertTrue(style & 0x00000080, "桌宠不应出现在 Alt+Tab")
            self.assertTrue(style & 0x00080000, "桌宠应是分层窗口（透明生效）")
            key, flags = wintypes.DWORD(), wintypes.DWORD()
            user32.GetLayeredWindowAttributes.argtypes = [
                wintypes.HWND, ctypes.POINTER(wintypes.DWORD),
                ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(wintypes.DWORD)]
            self.assertTrue(user32.GetLayeredWindowAttributes(
                hwnd, ctypes.byref(key), ctypes.byref(flags),
                ctypes.byref(wintypes.DWORD())))
            self.assertEqual(key.value & 0xFFFFFF, 0xFE00FF,
                             "颜色键应为精灵透明底色")
            pet.move_to(40, 40)
            self.root.update()
            self.assertEqual(pet.bounds()[:2], (40, 40))
            # 二维漫游：目标同时改 x/y 时，两个轴都要动（不是只沿任务栏横走）。
            pet._walk_target = (70, 65)
            pet._walk_remaining = 40
            self.root.after(500, self.root.quit)
            self.root.mainloop()
            wx, wy, _ww, _hh = pet.bounds()
            self.assertGreater(wx, 40)
            self.assertGreater(wy, 40)
            pet.set_height(306)
            self.root.update()
            self.assertEqual(pet.bounds()[2:], (241, 322))
            self.root.after(80, lambda: pet.canvas.event_generate(
                "<ButtonPress-1>", x=30, y=30))
            self.root.after(100, lambda: pet.canvas.event_generate(
                "<ButtonRelease-1>", x=30, y=30))
            self.root.after(420, self.root.quit)
            self.root.mainloop()
            self.assertEqual(clicks, ["click"], "单击应回调一次")
        finally:
            pet.destroy()
            self.root.update()

    def test_double_click_cancels_single_click(self):
        clicks, doubles = [], []
        pet = petmod.DesktopPet(
            self.root, height=187, x=100, y=100,
            on_click=lambda _p: clicks.append("click"),
            on_double=lambda _p: doubles.append("double"))
        self.root.update()
        try:
            pet.canvas.event_generate("<ButtonPress-1>", x=20, y=20)
            pet.canvas.event_generate("<ButtonRelease-1>", x=20, y=20)
            pet._on_double_click(SimpleNamespace(x=20, y=20))
            self.root.after(350, self.root.quit)
            self.root.mainloop()
            self.assertEqual(doubles, ["double"])
            self.assertEqual(clicks, [], "双击不应再冒单击气泡")
        finally:
            pet.destroy()
            self.root.update()


@h.needs_tk
class TestPetSettingsLive(unittest.TestCase):
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

    def test_toggle_creates_and_cancel_restores(self):
        self.app.pet_enabled.set(False)
        with mock.patch.object(self.winmon.core, "get_autostart",
                               return_value=False):
            dlg = self.winmon._SettingsWindow(
                self.root, self.app, "tray", True, True, 30, "zh", 9,
                "cobalt", 100, {})
        self.addCleanup(lambda: dlg.top.winfo_exists() and dlg._cancel())
        self.root.update()
        try:
            dlg.pet_enabled.set(True)
            dlg._on_pet_settings_changed()
            self.root.update()
            self.assertIsNotNone(self.app._pet, "打开后应立即出现")
            self.assertTrue(self.app.pet_enabled.get())
        finally:
            dlg._cancel()
            self.root.update()
        self.assertIsNone(self.app._pet, "取消设置应把桌宠收掉")
        self.assertFalse(self.app.pet_enabled.get())

    def test_click_bubble_opens_and_reports_media(self):
        app = self.app
        app._destroy_pet()
        app.apply_pet_settings_live(True, "中")
        app.media_text.set("网易云音乐：晴天 · 周杰伦")
        self.root.update()
        app.show_pet_bubble()
        self.root.update()
        try:
            self.assertIsNotNone(app._pet_bubble)
            self.assertTrue(app._pet_bubble.top.winfo_exists())
            texts = []

            def walk(widget):
                for child in widget.winfo_children():
                    try:
                        texts.append(str(child.cget("text")))
                    except Exception:
                        pass
                    walk(child)

            walk(app._pet_bubble.top)
            joined = "\n".join(texts)
            self.assertIn("大肥鱼 · 桌面陪看", joined)
            self.assertIn("晴天", joined)
        finally:
            app._close_pet_bubble()
            app.apply_pet_settings_live(False, "中")
            self.root.update()

    def test_summary_uses_windowed_processes_only(self):
        app = self.app
        app.media_text.set("网易云音乐：晴天 · 周杰伦")
        app.windows = [
            SimpleNamespace(pid=101, proc="cloudmusic.exe",
                            mem_kb=100 * 1024),
            SimpleNamespace(pid=102, proc="chrome.exe",
                            mem_kb=50 * 1024),
        ]
        app.proc_map = {
            101: {"name": "cloudmusic.exe", "mem_kb": 100 * 1024},
            102: {"name": "chrome.exe", "mem_kb": 50 * 1024},
            999: {"name": "System", "mem_kb": 999 * 1024},
        }
        app._cpu_pct = {101: 3.2, 102: 0.0, 999: 50.0}
        media, top, scale = app._pet_summary()
        joined = "\n".join(top)
        self.assertIn("晴天", media)
        self.assertIn("cloudmusic.exe", joined)
        self.assertNotIn("System", joined, "系统进程不该占桌宠气泡")
        self.assertIn("2 个窗口", scale)


if __name__ == "__main__":
    unittest.main(verbosity=2)
