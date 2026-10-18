# -*- coding: utf-8 -*-
"""
B28 —— 「AI 用量」窗口（阶段 2.5 的本地恢复入口）。

这个窗口是**唯一**能暂停/恢复预算的地方；测试只验证接线与状态刷新，
不真的点系统菜单、不打开数据目录。
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import _helpers as h                     # noqa: E402
import winmon_core as core               # noqa: E402


@h.needs_tk
class TestAiUsageWindow(unittest.TestCase):
    def setUp(self):
        import tkinter as tk
        import winmon
        self.winmon = winmon
        self.tmp = tempfile.TemporaryDirectory(prefix="winmon-ui-")
        self.addCleanup(self.tmp.cleanup)
        self.env = mock.patch.dict(os.environ, {
            "WINMON_AI_BUDGET": os.path.join(self.tmp.name, "budget.json"),
            "WINMON_AI_USAGE": os.path.join(self.tmp.name, "usage.jsonl"),
            "WINMON_AI_NOTIFY": "0",
        })
        self.env.start()
        self.addCleanup(self.env.stop)
        self.root = tk.Tk()
        self.root.withdraw()
        self.addCleanup(self.root.destroy)
        self.app = type("FakeApp", (), {
            "root": self.root,
            "_set_status": lambda self_, text: None,
            "_ai_usage_window": None,
        })()

    def test_window_opens_refreshes_and_closes(self):
        win = self.winmon._AiUsageWindow(self.root, self.app)
        self.app._ai_usage_window = win
        self.assertIn("当前", win.state_text.get())
        win.refresh()
        text = win.body.get("1.0", "end")
        self.assertIn("预算", text)
        win.toggle_pause()
        self.assertTrue(win.budget.status().get("paused"))
        win.toggle_pause()
        self.assertFalse(win.budget.status().get("paused"))
        win.reset_today()
        win.raise_limit()
        self.assertEqual(win.budget.status()["daily_tokens"], 20_000_000)
        win.close()
        self.assertIsNone(self.app._ai_usage_window)

    def test_tray_menu_id_is_distinct_and_present(self):
        ids = {core.IDM_TRAY_SHOW, core.IDM_TRAY_EXIT, core.IDM_TRAY_AI_USAGE}
        self.assertEqual(len(ids), 3)


if __name__ == "__main__":
    unittest.main(verbosity=2)
