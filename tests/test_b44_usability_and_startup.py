# -*- coding: utf-8 -*-
"""
B44 —— 2026-10-22 第五轮：交互收口与启动提速。

这一轮用户点名的四件事：
  · 右上角「搜索程序」点不开（焦点绑定被应用层覆盖）；
  · 接入 AI 的「更多设置」展开后滚轮没反应；
  · 播放器 / 设置里的颜色、语言、字号下拉必须选了才能关；
  · 启动慢（媒体清单重复枚举整张进程表）。

这里只放**纯逻辑 / 可重复**的回归测试；真实窗口的焦点、点击和滚轮
在 b38 / b43 / b4 的窗口用例里覆盖。
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
    def __init__(self, value=""):
        self.value = value

    def get(self):
        return self.value

    def set(self, value):
        self.value = str(value)


class _Combo:
    def __init__(self):
        self.values = []

    def configure(self, **kw):
        if "values" in kw:
            self.values = list(kw["values"])


class TestStartupMediaScanIsNotRepeated(unittest.TestCase):
    """启动时媒体清单只取一份进程表；不能每个候选 exe 再枚举一次。"""

    def test_media_app_path_accepts_an_existing_snapshot(self):
        with mock.patch.object(core, "snapshot_processes",
                               side_effect=AssertionError("不应重新枚举进程表")):
            self.assertEqual(
                core.media_app_path("__winmon_no_such_program__.exe",
                                    processes={}), "")

    def test_reload_media_app_list_takes_one_snapshot(self):
        import winmon
        app = winmon.WinMonApp.__new__(winmon.WinMonApp)
        app.media_app_combo = _Combo()
        app.media_app_pin = _Var(winmon.MEDIA_APP_ANY)
        app._media_session_apps = []
        app._media_pin_dropped = []
        app._media_apps = {}
        snapshot = {
            10: {"name": "running.exe", "ppid": 1, "threads": 1,
                 "mem_kb": 0, "mem_private_kb": 0, "path": ""},
        }
        seen_processes = []
        snapshots = []

        def fake_snapshot():
            snapshots.append(1)
            return snapshot

        def fake_path(exe, processes=None):
            seen_processes.append(processes)
            return r"C:\Apps\%s" % exe

        with mock.patch.object(core, "snapshot_processes", side_effect=fake_snapshot), \
                mock.patch.object(core, "installed_media_apps", return_value=[]), \
                mock.patch.object(core, "media_app_path", side_effect=fake_path):
            app._reload_media_app_list()

        self.assertEqual(len(snapshots), 1,
                         "一次清单重建只该枚举一次进程表")
        self.assertTrue(seen_processes, "候选表里的 exe 仍应做可用性检查")
        self.assertTrue(all(p is snapshot for p in seen_processes),
                        "每次可用性检查都应复用同一份进程快照")
        self.assertTrue(app._media_app_list_ready)
        self.assertIn(winmon.MEDIA_APP_ANY, app.media_app_combo.values)


class TestConnectWheel(unittest.TestCase):
    """连接窗口的滚轮是显式处理的；不能只靠右侧滚动条拖。"""

    class _Canvas:
        def __init__(self, view=(0.0, 0.4)):
            self.view = view
            self.moves = []

        def winfo_exists(self):
            return True

        def yview(self):
            return self.view

        def yview_scroll(self, step, what):
            self.moves.append((step, what))

    def _fake_window(self, view=(0.0, 0.4)):
        canvas = self._Canvas(view)
        return SimpleNamespace(canvas=canvas), canvas

    def test_wheel_scrolls_when_content_overflows(self):
        import winmon
        win, canvas = self._fake_window()
        result = winmon._AiConnectWindow._on_connect_wheel(
            win, SimpleNamespace(delta=-120))
        self.assertEqual(canvas.moves, [(1, "units")])
        self.assertEqual(result, "break")

    def test_wheel_direction_and_large_delta(self):
        import winmon
        win, canvas = self._fake_window()
        winmon._AiConnectWindow._on_connect_wheel(
            win, SimpleNamespace(delta=240))
        self.assertEqual(canvas.moves, [(-2, "units")])

    def test_wheel_does_nothing_when_content_fits(self):
        import winmon
        win, canvas = self._fake_window(view=(0.0, 1.0))
        result = winmon._AiConnectWindow._on_connect_wheel(
            win, SimpleNamespace(delta=-120))
        self.assertEqual(canvas.moves, [])
        self.assertEqual(result, "")


@h.needs_tk
class TestMediaListIsDeferredPastFirstPaint(unittest.TestCase):
    """建界面时不再同步扫开始菜单；首帧之后再填下拉。"""

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

    def test_build_ui_leaves_media_list_for_later(self):
        self.assertFalse(self.app._media_app_list_ready)
        self.assertEqual(list(self.app.media_app_combo.cget("values")),
                         [self.winmon.MEDIA_APP_ANY],
                         "首帧前不应同步扫描媒体清单")

    def test_opening_picker_before_scan_loads_it_on_demand(self):
        self.app._media_app_list_ready = False
        calls = []
        with mock.patch.object(self.winmon.WinMonApp, "_reload_media_app_list",
                               lambda self: calls.append(1) or
                               setattr(self, "_media_app_list_ready", True)):
            self.app._edit_media_app_multi()
            self.root.update()
        try:
            self.assertEqual(calls, [1], "点开时应补一次清单")
        finally:
            closer = getattr(self.app, "_media_picker_close", None)
            if callable(closer):
                closer()
            self.root.update()


if __name__ == "__main__":
    unittest.main(verbosity=2)
