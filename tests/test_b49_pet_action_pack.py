# -*- coding: utf-8 -*-
"""
B49 —— 2026-10-23：21 段动作接入、右键动作菜单、气泡点空白关闭。

用户这轮的要求：
  1. 点桌宠弹出的信息气泡，点空白处也能关；
  2. 动作放进右键菜单，但**不要**再用全屏遮罩/覆盖层，避免右键失灵；
  3. 接上前一轮选的 21 段动作。

这里分别钉住：
  · 动作包 21 段、6 组，条带文件真实存在；
  · `perform_action()` 能切到条带动作，一次性播完回 idle；
  · 右键菜单「动作 ▸」原地换项，不关闭、不换窗口；
  · 气泡只轮询全局鼠标键，不 grab、不铺全屏，点外面会关、点里面不关。
"""
from __future__ import annotations

import os
import sys
import tkinter as tk
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import _helpers as h                     # noqa: E402
import winmon_pet as petmod              # noqa: E402


class TestActionPack(unittest.TestCase):
    def test_manifest_has_21_clips_and_six_groups(self):
        library = petmod.ActionLibrary(petmod.action_dir())
        self.assertTrue(library.available(), "动作包 manifest 没加载")
        self.assertEqual(len(library._clips), 21)
        groups = library.groups()
        self.assertEqual(len(groups), 6)
        self.assertEqual(sum(len(group["clips"]) for group in groups), 21)
        for clip_id in library._clips:
            meta = library.meta(clip_id)
            self.assertTrue(meta["strips"], clip_id)
            for rel in meta["strips"]:
                path = os.path.join(library.root, rel.replace("/", os.sep))
                self.assertTrue(os.path.isfile(path), path)
        self.assertGreaterEqual(library.max_width(306), 240)

    def test_every_clip_has_a_distinct_label(self):
        library = petmod.ActionLibrary(petmod.action_dir())
        labels = [library.label(clip_id) for clip_id in library._clips]
        self.assertEqual(len(labels), len(set(labels)))


@h.needs_tk
class TestActionPlayer(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = tk.Tk()
        cls.root.withdraw()

    @classmethod
    def tearDownClass(cls):
        h.destroy_root(cls.root)

    def _pet(self):
        pet = petmod.DesktopPet(self.root, height=238, x=100, y=100)
        self.root.update()
        self.addCleanup(lambda: (pet.destroy(), self.root.update()))
        return pet

    def test_play_action_and_fall_back_to_idle(self):
        pet = self._pet()
        self.assertEqual(pet._clip_id, "idle")
        self.assertEqual(pet.perform_action("wave"), "wave")
        self.assertEqual(pet._clip_id, "wave")
        self.assertIsNotNone(pet._clip_frame())
        # 一次性动作播完必须回 idle，不能停在最后一帧。
        pet._clip_elapsed = pet._actions.duration("wave") + 0.1
        pet._advance_clip(0.016)
        self.assertEqual(pet._clip_id, "idle")

    def test_sleep_and_wake_use_their_own_clips(self):
        pet = self._pet()
        self.assertEqual(pet.perform_action("sleep"), "sleep")
        self.assertEqual(pet._mode, "sleep")
        self.assertEqual(pet._clip_id, "sleep")
        self.assertEqual(pet.perform_action("wake"), "wake")
        self.assertEqual(pet._mode, "idle")
        self.assertEqual(pet._clip_id, "wake")
        pet._clip_elapsed = pet._actions.duration("wake") + 0.1
        pet._advance_clip(0.016)
        self.assertEqual(pet._clip_id, "idle")

    def test_window_is_wide_enough_for_the_widest_clip(self):
        pet = self._pet()
        widest = pet._actions.max_width(pet._height)
        self.assertGreaterEqual(pet._w, widest + petmod.PAD_X * 2 - 1)


@h.needs_tk
class TestPetMenuAndBubble(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from shot import build_app
        cls.root, cls.app = build_app(1160, 730)
        cls.root.update()

    @classmethod
    def tearDownClass(cls):
        h.destroy_root(cls.root)

    def setUp(self):
        self.app.apply_pet_settings_live(True, "中", "标准")
        self.root.update()

    def tearDown(self):
        self.app._destroy_pet()
        self.root.update()

    def test_right_click_menu_drills_into_actions_without_closing(self):
        seen = {}

        def drive():
            menu = self.app._pet_menu
            seen["main"] = [item.get("label") for item in menu._items]
            index = next(i for i, item in enumerate(menu._items)
                         if "动作" in str(item.get("label")))
            menu._choose(index)
            seen["root"] = [item.get("label") for item in menu._items]
            index = next(i for i, item in enumerate(menu._items)
                         if "日常" in str(item.get("label")))
            menu._choose(index)
            seen["group"] = [item.get("label") for item in menu._items]
            index = next(i for i, item in enumerate(menu._items)
                         if item.get("label") == "撸猫")
            menu._choose(index)

        self.root.after(100, drive)
        self.app.show_pet_menu(100, 100)
        self.root.update()
        self.assertTrue(any("动作" in str(label)
                            for label in seen.get("main", [])))
        self.assertFalse(any("动作抽屉" in str(label)
                             for label in seen.get("main", [])),
                         "动作不该再走旧抽屉")
        self.assertTrue(any("待机" in str(label)
                            for label in seen.get("root", [])))
        self.assertTrue(any("撸猫" in str(label)
                            for label in seen.get("group", [])))
        self.assertEqual(self.app._pet._clip_id, "cat")
        self.assertFalse(self.app._pet_menu.winfo_exists())

    def test_bubble_outside_click_closes_without_grab_or_overlay(self):
        self.app.show_pet_bubble()
        self.root.update()
        bubble = self.app._pet_bubble
        self.assertIsNotNone(bubble)
        self.assertIsNone(bubble.top.grab_current(), "气泡不能 grab")
        self.assertLess(bubble.top.winfo_width(),
                        bubble.top.winfo_screenwidth(),
                        "气泡不能铺满屏幕")
        bubble._ignore_until = 0.0
        bubble._left_down = False
        bubble._button_down = lambda vk: vk == 0x01
        bubble._cursor_pos = lambda: (0, 0)
        bubble._poll_outside()
        self.root.update()
        self.assertIsNone(self.app._pet_bubble)
        self.assertFalse(bubble.top.winfo_exists())

    def test_bubble_inside_click_does_not_close(self):
        self.app.show_pet_bubble()
        self.root.update()
        bubble = self.app._pet_bubble
        x = bubble.top.winfo_rootx() + 8
        y = bubble.top.winfo_rooty() + 8
        bubble._ignore_until = 0.0
        bubble._left_down = False
        bubble._button_down = lambda vk: vk == 0x01
        bubble._cursor_pos = lambda: (x, y)
        bubble._poll_outside()
        self.root.update()
        self.assertIs(self.app._pet_bubble, bubble)
        self.assertFalse(bubble._closed)
        bubble.close()
        self.root.update()
        self.assertIsNone(self.app._pet_bubble)


if __name__ == "__main__":
    unittest.main()
