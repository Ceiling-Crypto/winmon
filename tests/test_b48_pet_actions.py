# -*- coding: utf-8 -*-
"""
B48 —— 2026-10-22：桌宠动作、闭眼帧与独立设置面板。

用户这轮提了三件事：
  1. 去掉精灵背后的透明框架；
  2. 桌宠“没有动作”；
  3. 桌宠不能脱离主界面响应设置。

这里分别钉住：
  · 窗口只比精灵大一圈透明留白，DWM 装饰被清掉；
  · side_left 是真正的镜像（不是和 side 同一张图）；
  · 眨眼/睡觉/音乐泡泡/散步这些本地动作真的会发生；
  · 主窗口收进托盘时，独立桌宠面板和完整设置仍然可见。
"""
from __future__ import annotations

import json
import os
import sys
import time
import tkinter as tk
import unittest
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import _helpers as h                     # noqa: E402
import winmon_core as core               # noqa: E402
import winmon_pet as petmod              # noqa: E402


class TestPetAnimatedAssets(unittest.TestCase):
    def test_blink_frames_exist_for_every_size(self):
        for height in petmod.available_heights():
            for name in ("front_blink", "side_blink", "side_left_blink"):
                path = petmod.sprite_path(height, name)
                self.assertTrue(os.path.isfile(path), path)
                self.assertGreater(os.path.getsize(path), 1000, path)

    @h.needs_tk
    def test_side_left_is_a_real_mirror(self):
        """向左/向右走必须用两张镜像图；同一张图会“倒着走”。"""
        import tkinter as tk
        root = tk.Tk()
        root.withdraw()
        try:
            for height in petmod.available_heights():
                side = tk.PhotoImage(
                    master=root, file=petmod.sprite_path(height, "side"))
                left = tk.PhotoImage(
                    master=root, file=petmod.sprite_path(height, "side_left"))
                self.assertEqual((side.width(), side.height()),
                                 (left.width(), left.height()))
                checked = 0
                for y in range(9, side.height(), 23):
                    for x in range(7, side.width(), 17):
                        mirrored = side.width() - 1 - x
                        a = bool(side.transparency_get(x, y))
                        b = bool(left.transparency_get(mirrored, y))
                        self.assertEqual(a, b, (height, x, y))
                        if not a:
                            self.assertEqual(side.get(x, y),
                                             left.get(mirrored, y),
                                             (height, x, y))
                            checked += 1
                self.assertGreater(checked, 20, height)
        finally:
            h.destroy_root(root)

    @h.needs_tk
    def test_blink_frame_really_differs(self):
        import tkinter as tk
        root = tk.Tk()
        root.withdraw()
        try:
            normal = tk.PhotoImage(
                master=root, file=petmod.sprite_path(238, "front"))
            blink = tk.PhotoImage(
                master=root, file=petmod.sprite_path(238, "front_blink"))
            self.assertEqual((normal.width(), normal.height()),
                             (blink.width(), blink.height()))
            changed = 0
            for y in range(70, 140, 5):
                for x in range(35, 145, 5):
                    if normal.get(x, y) != blink.get(x, y):
                        changed += 1
            self.assertGreater(changed, 10, "闭眼帧必须在眼睛区域有变化")
        finally:
            h.destroy_root(root)


class TestMediaKind(unittest.TestCase):
    def test_music_video_and_unknown(self):
        self.assertEqual(core.media_kind("cloudmusic.exe", "晴天"), "music")
        self.assertEqual(core.media_kind("Spotify.exe", "某首歌"), "music")
        self.assertEqual(core.media_kind("PotPlayerMini64.exe", "电影"), "video")
        self.assertEqual(core.media_kind("哔哩哔哩.exe", "某个视频"), "video")
        self.assertEqual(core.media_kind("msedge.exe", "视频_哔哩哔哩_bilibili"),
                         "video")
        self.assertEqual(core.media_kind("msedge.exe", "YouTube Music"),
                         "music")
        self.assertEqual(core.media_kind("notepad.exe", "无标题"), "")


@h.needs_tk
class TestPetBehavior(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = tk.Tk()
        cls.root.withdraw()

    @classmethod
    def tearDownClass(cls):
        h.destroy_root(cls.root)

    def _pet(self, **kwargs):
        pet = petmod.DesktopPet(self.root, height=238, x=100, y=100, **kwargs)
        self.root.update()
        self.addCleanup(lambda: (pet.destroy(), self.root.update()))
        return pet

    def test_motion_and_window_fit(self):
        pet = self._pet(motion="安静")
        self.assertEqual(pet._motion, "安静")
        self.assertLess(pet._speed, 1.0)
        pet.set_motion("活泼")
        self.assertGreater(pet._speed, 1.0)
        # 窗口只保留摇摆/跳跃的透明留白，不再有旧的 +12 / +16 大边。
        self.assertEqual(pet._w, max(img.width() for img in pet._images.values())
                         + petmod.PAD_X * 2)
        self.assertEqual(pet._h, max(img.height() for img in pet._images.values())
                         + petmod.PAD_TOP)
        # 窗口边框位必须被清掉（透明框架的另一半来自这些扩展样式）。
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.windll.user32
        hwnd = user32.GetAncestor(wintypes.HWND(pet.top.winfo_id()), 2)
        style = int(user32.GetWindowLongPtrW(hwnd, -20) or 0)
        for bit, name in ((0x00000100, "WINDOWEDGE"),
                          (0x00000200, "CLIENTEDGE"),
                          (0x00020000, "STATICEDGE"),
                          (0x00040000, "DLGMODALFRAME")):
            self.assertFalse(style & bit, name)
        self.assertTrue(style & 0x00080000, "WS_EX_LAYERED")
        self.assertIsNotNone(pet._surface)
        self.assertIsNotNone(pet._paint_guard)
        self.assertLessEqual(petmod.TICK_MS, 16, "动作链应至少 60 FPS")

    def test_dwm_round_corner_is_disabled_after_show(self):
        """Win11 圆角会给分层窗口套一层半透明白卡，显示后必须再关一次。"""
        if sys.getwindowsversion().build < 22000:
            self.skipTest("DWM 圆角只存在于 Windows 11")
        pet = self._pet()
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.windll.user32
        user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
        user32.GetAncestor.restype = wintypes.HWND
        hwnd = user32.GetAncestor(wintypes.HWND(pet.top.winfo_id()), 2)
        dwm = ctypes.windll.dwmapi
        get_attr = dwm.DwmGetWindowAttribute
        get_attr.argtypes = [wintypes.HWND, wintypes.DWORD,
                             ctypes.POINTER(wintypes.DWORD),
                             wintypes.DWORD]
        get_attr.restype = ctypes.c_long

        def corner() -> int:
            value = wintypes.DWORD()
            hr = get_attr(wintypes.HWND(hwnd), 33, ctypes.byref(value),
                          ctypes.sizeof(value))
            self.assertEqual(hr, 0, "DwmGetWindowAttribute 失败")
            return int(value.value)

        self.assertEqual(corner(), 1, "窗口显示后必须保持 DONOTROUND")
        # 再走一次 FRAMECHANGED，防止 DWM 属性又被系统重置。
        pet._strip_window_frame(hwnd)
        self.assertEqual(corner(), 1, "FRAMECHANGED 之后必须重新关圆角")

    def test_actions_are_real_and_safe(self):
        pet = self._pet()
        before = pet._frame_count
        self.assertEqual(pet.perform("hop"), "hop")
        self.assertGreater(pet._hop_until, time.monotonic())
        self.assertGreater(pet._frame_count, before,
                           "perform 应立即提交一帧，不等下一个 tick")
        self.assertEqual(pet.perform("greet"), "greet")
        self.assertGreater(len(pet._particles), 0)
        self.assertEqual(pet.perform("spin"), "spin")
        self.assertGreater(pet._spin_until, time.monotonic())
        self.assertEqual(pet.perform("stretch"), "stretch")
        self.assertEqual(pet.perform("look"), "look")
        self.assertGreater(pet._look_until, time.monotonic())
        self.assertEqual(pet.perform("sleep"), "sleep")
        self.assertEqual(pet._mode, "sleep")
        self.assertEqual(pet._sprite_name(), "front_blink")
        self.assertEqual(pet.perform("wake"), "wake")
        self.assertEqual(pet._mode, "idle")
        self.assertEqual(pet.perform("bubble"), "bubble")
        self.assertGreater(len(pet._particles), 0)
        self.assertEqual(pet.perform("watch", 3), "watch")
        self.assertGreater(pet._watch_until, time.monotonic())
        self.assertEqual(pet.perform("不存在的动作"), "idle")

    def test_press_feedback_is_immediate(self):
        """按下就 Q 弹+爱心；不能等 240ms 双击判定后才动。"""
        pet = self._pet()
        pet._hop_until = 0.0
        before = pet._frame_count
        pet._on_press(SimpleNamespace(
            x_root=pet._x + pet._w // 2,
            y_root=pet._y + pet._h // 2))
        try:
            self.assertGreater(pet._hop_until, time.monotonic())
            self.assertGreater(pet._frame_count, before,
                               "按下应立即重绘，不等 240ms")
            self.assertGreater(len(pet._particles), 0)
        finally:
            pet._on_release(SimpleNamespace(x_root=0, y_root=0))

    def test_window_is_per_pixel_alpha(self):
        """不再用 SetWindowRgn：GDI+ 分层表面按 alpha 透明，角落无像素。"""
        pet = self._pet()
        self.root.update()
        self.assertIsNotNone(pet._surface, "应创建 GDI+ 分层表面")
        self.assertIsNotNone(pet._paint_guard, "Tk 子窗口重绘应被抑制")
        self.assertEqual(pet._surface.size, (pet._w, pet._h))
        self.assertEqual(pet._surface.sample_alpha(0, 0), 0)
        self.assertEqual(pet._surface.sample_alpha(pet._w - 1, 0), 0)
        self.assertGreater(pet._surface.sample_alpha(
            pet._w // 2, pet._h // 2), 0, "身体中心应不透明")
        self.assertEqual(pet._render_error, "")
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.windll.user32
        hwnd = user32.GetAncestor(wintypes.HWND(pet.top.winfo_id()), 2)
        gdi32 = ctypes.windll.gdi32
        gdi32.CreateRectRgn.argtypes = [ctypes.c_int, ctypes.c_int,
                                        ctypes.c_int, ctypes.c_int]
        gdi32.CreateRectRgn.restype = wintypes.HRGN
        region = gdi32.CreateRectRgn(0, 0, 0, 0)
        self.assertTrue(region, "CreateRectRgn 失败")
        user32.GetWindowRgn.argtypes = [wintypes.HWND, wintypes.HRGN]
        user32.GetWindowRgn.restype = ctypes.c_int
        try:
            self.assertEqual(user32.GetWindowRgn(hwnd, region), 0,
                             "不应再设置窗口区域")
        finally:
            try:
                ctypes.windll.gdi32.DeleteObject(region)
            except Exception:
                pass

    def test_walk_uses_correct_facing(self):
        pet = self._pet()
        pet._mode = "walk"
        pet._walk_target = (pet._x + 80, pet._y)
        pet._walk_remaining = 20
        pet._walk_tick(time.monotonic())
        self.assertEqual(pet._sprite, "side_left", "向右走应使用镜像的侧面")
        pet._mode = "walk"
        pet._walk_target = (pet._x - 80, pet._y)
        pet._walk_remaining = 20
        pet._walk_tick(time.monotonic())
        self.assertEqual(pet._sprite, "side", "向左走应使用原始侧面")

    def test_music_spawns_particles_without_walking(self):
        pet = self._pet(motion="活泼")
        pet.set_ambient("music")
        pet._music_next = 0.0
        self.root.after(220, self.root.quit)
        self.root.mainloop()
        self.assertEqual(pet._ambient, "music")
        self.assertEqual(pet._mode, "idle")
        self.assertGreater(len(pet._particles), 0)

    def test_video_keeps_front_and_wakes(self):
        pet = self._pet()
        pet.perform("sleep")
        pet.set_ambient("video")
        self.assertEqual(pet._mode, "idle")
        self.assertEqual(pet._ambient, "video")
        self.assertEqual(pet._sprite, "front")


@h.needs_tk
class TestPetIndependentSettings(unittest.TestCase):
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

    def tearDown(self):
        try:
            self.app._destroy_pet_action_drawer()
            self.app._destroy_pet_panel()
            self.app.apply_pet_settings_live(False, "中", "标准")
            self.root.deiconify()
            self.root.update()
        except Exception:
            pass

    def test_panel_stays_visible_when_main_window_hides(self):
        self.app.apply_pet_settings_live(True, "中", "活泼")
        self.root.update()
        self.root.withdraw()
        self.root.update()
        try:
            self.app.show_pet_panel()
            self.root.update()
            panel = self.app._pet_panel
            self.assertIsNotNone(panel)
            self.assertTrue(panel.top.winfo_viewable(),
                            "主窗口隐藏时桌宠面板仍必须可见")
            self.assertIsNotNone(self.app._pet)
            self.assertTrue(self.app._pet.top.winfo_viewable(),
                            "主窗口隐藏时桌宠本身不能被一起藏掉")
            self.assertFalse(panel.top.transient(),
                             "独立面板不能是主窗口的 transient")
            # 面板里关掉桌宠：面板自己不能跟着消失。
            panel.enabled.set(False)
            panel._apply()
            self.root.update()
            self.assertIsNone(self.app._pet)
            self.assertTrue(panel.top.winfo_viewable())
        finally:
            self.app._destroy_pet_panel()
            self.root.deiconify()
            self.root.update()

    def test_pet_menu_reopens_after_close_and_has_media_controls(self):
        """右键菜单是一次性 PopupMenu；第二次右键必须重建，不能直接 return。"""
        self.root.after(
            50, lambda: self.app._pet_menu._close()
            if getattr(self.app, "_pet_menu", None) else None)
        self.app.show_pet_menu(100, 100)
        first = getattr(self.app, "_pet_menu", None)
        self.assertIsNotNone(first)
        self.assertFalse(first.winfo_exists(), "菜单关闭后应已销毁")
        labels = [item.get("label") for item in first._items]
        for label in ("⏮ 上一首", "⏭ 下一首"):
            self.assertIn(label, labels)
        self.assertTrue(any("播放" in str(label) for label in labels),
                        "右键菜单应包含播放/暂停")
        self.root.after(
            50, lambda: self.app._pet_menu._close()
            if getattr(self.app, "_pet_menu", None) else None)
        self.app.show_pet_menu(100, 100)
        second = getattr(self.app, "_pet_menu", None)
        self.assertIsNotNone(second)
        self.assertIsNot(second, first, "第二次右键必须创建新菜单")

    def test_action_drawer_stays_visible_and_runs_action(self):
        self.app.apply_pet_settings_live(True, "中", "标准")
        self.root.update()
        self.root.withdraw()
        self.root.update()
        try:
            self.app.show_pet_action_drawer()
            self.root.update()
            drawer = self.app._pet_action_drawer
            self.assertIsNotNone(drawer)
            self.assertTrue(drawer.top.winfo_viewable(),
                            "主窗口隐藏时动作抽屉仍必须可见")
            self.assertFalse(drawer.top.transient())
            pet = self.app._pet
            self.assertIsNotNone(pet)
            before = pet._hop_until
            drawer._run("hop")
            self.root.update()
            self.assertGreater(pet._hop_until, before)
            self.assertIsNone(self.app._pet_action_drawer,
                              "执行动作后抽屉应收起")
        finally:
            self.app._destroy_pet_action_drawer()
            self.root.deiconify()
            self.root.update()

    def test_panel_size_and_motion_apply_live(self):
        self.app.apply_pet_settings_live(True, "中", "标准")
        self.root.update()
        self.app.show_pet_panel()
        self.root.update()
        panel = self.app._pet_panel
        self.assertIsNotNone(panel)
        panel.size.set("大")
        panel.motion.set("安静")
        panel._apply()
        self.root.update()
        self.assertEqual(self.app.pet_size_choice.get(), "大")
        self.assertEqual(self.app.pet_motion_choice.get(), "安静")
        self.assertIsNotNone(self.app._pet)
        self.assertEqual(self.app._pet._height, 306)
        self.assertEqual(self.app._pet._motion, "安静")
        with open(self.app._settings_path, encoding="utf-8") as fh:
            saved = json.load(fh)
        self.assertEqual(saved.get("pet_motion"), "安静")
        self.assertEqual(saved.get("pet_size"), "大")

    def test_detached_full_settings_visible_when_root_hidden(self):
        self.root.withdraw()
        self.root.update()
        dlg = None
        try:
            dlg = self.winmon._SettingsWindow(
                self.root, self.app, "tray", True, False, 30, "zh", 9,
                "cobalt", 100, {}, attach=False)
            self.root.update()
            self.assertFalse(dlg.attached)
            self.assertTrue(dlg.top.winfo_viewable(),
                            "主窗口在托盘里时，完整设置也必须看得见")
            self.assertFalse(dlg.top.transient())
        finally:
            if dlg is not None:
                try:
                    dlg._cancel()
                except Exception:
                    pass
            self.root.deiconify()
            self.root.update()

    def test_action_settings_picks_attach_by_root_visibility(self):
        seen = []

        class FakeSettings:
            def __init__(self, *args, **kwargs):
                seen.append(kwargs.get("attach"))

            def show(self, *args, **kwargs):
                return None

        # build_app 只建界面，不跑 __init__；补上 action_settings 读取的三个偏好。
        self.app.font_size = tk.StringVar(value=str(self.winmon.UI_FONT_SIZE))
        self.app.ui_opacity = tk.StringVar(value="100")
        self.app.theme_pref = self.winmon.THEME_DEFAULT
        with mock.patch.object(self.winmon, "_SettingsWindow", FakeSettings):
            self.root.withdraw()
            self.root.update()
            self.app.action_settings("general")
            self.root.deiconify()
            self.root.update()
            self.app.action_settings("general")
        self.assertEqual(seen, [False, True],
                         "托盘状态要独立显示，正常状态仍挂主窗口")


class TestPetSettingsPersist(unittest.TestCase):
    def test_motion_round_trips_as_json_value(self):
        # 纯值校验：写进设置文件的是三档中文标签之一，不是速度倍率。
        for label, _factor in petmod.MOTION_PRESETS:
            self.assertEqual(petmod.normalize_motion(label), label)
            self.assertIn(petmod.normalize_motion(label),
                          [name for name, _f in petmod.MOTION_PRESETS])
        self.assertEqual(petmod.normalize_motion("坏值"), petmod.DEFAULT_MOTION)

    def test_default_motion_is_standard(self):
        self.assertEqual(petmod.DEFAULT_MOTION, "标准")
        payload = {"pet_motion": "活泼"}
        self.assertEqual(json.loads(json.dumps(payload))["pet_motion"], "活泼")


if __name__ == "__main__":
    unittest.main(verbosity=2)
