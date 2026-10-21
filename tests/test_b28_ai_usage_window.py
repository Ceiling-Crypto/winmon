# -*- coding: utf-8 -*-
"""
B28 —— 设置里的「AI 用量」标签页（阶段 2.5 的本地恢复入口）。

这个标签页是**唯一**能暂停/恢复预算的地方；测试只验证接线与状态刷新，
不真的点系统菜单、不打开数据目录。2026-10-20 起它不再单独开窗，
而是「设置」三合一窗口的一部分。
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
            "_ai_sessions": {
                "primary": {"name": "智谱清言", "kind": "web", "confidence": 0.65,
                            "proc": "msedge.exe", "pid": 2676,
                            "source": "window_title", "pinned": False},
                "others": [{"name": "DeepSeek", "kind": "desktop", "score": 0.0}],
                "note": "测试",
            },
        })()

    def test_panel_refreshes_and_closes(self):
        import tkinter as tk
        host = tk.Frame(self.root)
        host.pack(fill="both", expand=True)
        win = self.winmon._AiUsagePanel(host, self.app)
        self.assertIn("当前", win.state_text.get())
        win.refresh()
        self.assertIn("预算", win.budget_text.get())
        self.assertIn("智谱清言", win.ai_name_text.get())
        labels = []

        def walk(widget):
            yield widget
            for child in widget.winfo_children():
                yield from walk(child)

        for widget in walk(win.frame):
            try:
                labels.append(str(widget.cget("text")))
            except Exception:
                pass
        for title in ("真实 API", "精确分词", "估算"):
            self.assertIn(title, labels)
        win.toggle_pause()
        self.assertTrue(win.budget.status().get("paused"))
        win.toggle_pause()
        self.assertFalse(win.budget.status().get("paused"))
        win.reset_today()
        win.raise_limit()
        self.assertEqual(win.budget.status()["daily_tokens"], 20_000_000)
        win.close()
        self.assertFalse(win._alive)

    def test_tray_settings_id_is_distinct_and_present(self):
        ids = {core.IDM_TRAY_SHOW, core.IDM_TRAY_EXIT, core.IDM_TRAY_SETTINGS}
        self.assertEqual(len(ids), 3)


if __name__ == "__main__":
    unittest.main(verbosity=2)
