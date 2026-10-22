# -*- coding: utf-8 -*-
"""
B45 —— 2026-10-22 第六轮：闲置即时生效、清单空态、类别抽屉。

用户点名的四件事（B41 已单独守住右键菜单）：
  · 闲置提醒在设置里打开后应直接生效，不需要再点保存；
  · 「打开清单」在还没有到阈值的程序时也要能打开窗口；
  · 程序目录的「类别：全部」改成抽屉，点菜单外可以关；
  · 程序行右键不再被「刷新列表」占用（见 b41）。
"""
from __future__ import annotations

import os
import sys
import unittest
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import _helpers as h                     # noqa: E402
import winmon_core as core               # noqa: E402


class _Var:
    def __init__(self, value):
        self.value = value

    def get(self):
        return self.value

    def set(self, value):
        self.value = value


class TestIdleDialogAlwaysOpens(unittest.TestCase):
    def test_empty_list_still_opens_the_dialog(self):
        import winmon
        app = winmon.WinMonApp.__new__(winmon.WinMonApp)
        app.root = None
        app.idle_watch = core.IdleWatcher()
        app.idle_alert = _Var(True)
        app.idle_choice = _Var("30 分钟")
        app.idle_auto_close = _Var(False)
        app._idle_dialog_open = False
        app._set_status = lambda _text: None
        seen = {}

        class FakeDialog:
            auto_close_changed = False
            minimize_result = None

            def __init__(self, parent, rows, threshold, auto_close, enabled=True):
                seen.update(parent=parent, rows=list(rows),
                            threshold=threshold, enabled=enabled)

            def show(self):
                return []

        with mock.patch.object(winmon, "_IdleDialog", FakeDialog):
            app.action_idle_dialog()
        self.assertEqual(seen["rows"], [])
        self.assertTrue(seen["enabled"], "开关即时生效后，清单应按已开启打开")
        self.assertFalse(app._idle_dialog_open, "关闭后必须复位对话框标记")


@h.needs_tk
class TestIdleDialogEmptyWindow(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import tkinter as tk
        import winmon
        cls.winmon = winmon
        winmon.apply_theme("cobalt")
        cls.root = tk.Tk()
        cls.root.withdraw()

    @classmethod
    def tearDownClass(cls):
        h.destroy_root(cls.root)

    def test_empty_state_explains_why_there_are_no_rows(self):
        import tkinter as tk
        auto = tk.BooleanVar(value=False)
        dlg = self.winmon._IdleDialog(
            self.root, [], "30 分钟", auto, enabled=True)
        self.root.update()
        try:
            self.assertIn("正在计时", dlg.top.title())
            self.assertEqual(str(dlg.btn_close.cget("state")), "disabled")
            self.assertEqual(str(dlg.btn_minimize.cget("state")), "disabled")
        finally:
            dlg._cancel()

    def test_disabled_state_still_shows_a_window(self):
        import tkinter as tk
        auto = tk.BooleanVar(value=False)
        dlg = self.winmon._IdleDialog(
            self.root, [], "30 分钟", auto, enabled=False)
        self.root.update()
        try:
            self.assertIn("未开启", dlg.top.title())
        finally:
            dlg._cancel()


@h.needs_tk
class TestCategoryDrawer(unittest.TestCase):
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

    def test_drawer_lists_all_and_each_category(self):
        pane = self.app.catalog
        menu = pane._category_menu()
        labels = [menu.entrycget(i, "label")
                  for i in range((menu.index("end") or 0) + 1)]
        self.assertEqual(labels[0], "✓ 全部")
        for key in core.CATEGORY_ORDER:
            self.assertTrue(any(core.CATEGORY_LABELS[key] in label
                                for label in labels), key)
        self.assertIn("多选…", labels)

    def test_drawer_selection_applies_immediately(self):
        pane = self.app.catalog
        pane.selected_categories = set()
        menu = pane._category_menu()
        labels = [menu.entrycget(i, "label")
                  for i in range((menu.index("end") or 0) + 1)]
        index = next(i for i, label in enumerate(labels)
                     if label.strip() == "开发工具")
        menu._items[index]["command"]()
        self.root.update()
        self.assertEqual(pane.selected_categories, {"dev"})
        self.assertIn("开发工具", pane.btn_cats.cget("text"))

    def test_drawer_closes_on_outside_click(self):
        pane = self.app.catalog
        menu = pane._category_menu()
        x = pane.btn_cats.winfo_rootx()
        y = pane.btn_cats.winfo_rooty() + pane.btn_cats.winfo_height()
        self.root.after(
            150, lambda: menu._on_toplevel_click(
                SimpleNamespace(x_root=0, y_root=0)))
        menu.tk_popup(x, y)
        self.assertTrue(menu._closing, "点抽屉外必须收起")


@h.needs_tk
class TestIdleSettingsLive(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from shot import build_app
        import winmon
        cls.winmon = winmon
        winmon.apply_theme("cobalt")
        cls.root, cls.app = build_app(1160, 730)
        cls.app.idle_watch = core.IdleWatcher()
        cls.app._idle_ticks = 0
        cls.app.idle_auto_close = __import__("tkinter").BooleanVar(value=False)
        cls.root.update()

    @classmethod
    def tearDownClass(cls):
        h.destroy_root(cls.root)

    def _open(self):
        with mock.patch.object(core, "get_autostart", return_value=False):
            dlg = self.winmon._SettingsWindow(
                self.root, self.app, "tray", True, True, 30, "zh", 9,
                "cobalt", 100, {})
        self.addCleanup(lambda: dlg.top.winfo_exists() and dlg._cancel())
        self.root.update()
        return dlg

    def test_toggle_takes_effect_without_save(self):
        self.app.idle_alert.set(False)
        dlg = self._open()
        try:
            dlg.idle_alert.set(True)
            dlg._on_idle_settings_changed()
            self.assertTrue(self.app.idle_alert.get())
            self.assertEqual(self.app.idle_watch.threshold,
                             core.IDLE_DEFAULT_SECONDS)
        finally:
            dlg._cancel()
        self.assertFalse(self.app.idle_alert.get(),
                         "取消设置应还原运行态")

    def test_open_list_button_reaches_the_main_app(self):
        dlg = self._open()
        calls = []
        self.app.action_idle_dialog = lambda: calls.append(1)
        try:
            dlg._open_idle_list()
        finally:
            dlg._cancel()
        self.assertEqual(calls, [1])


if __name__ == "__main__":
    unittest.main(verbosity=2)
