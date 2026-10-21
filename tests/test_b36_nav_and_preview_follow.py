# -*- coding: utf-8 -*-
"""
B36 —— 导航收口、AI 工作区、预览跟随与关闭超时升级。

用户 2026-10-20 的要求：
  · 主界面左侧按「监控 / 工作台 / AI / 设置」四种心智模型组织；
  · AI 助手 / 接入 / 用量收进一个 AI 工作区；
  · 在实时预览里结束当前窗口后，预览不能停在旧画面，要清掉并跟随下一个窗口；
  · 请求关闭超时后，底栏给出「改为强制结束」。
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import _helpers as h                     # noqa: E402


def walk(widget):
    yield widget
    for child in widget.winfo_children():
        yield from walk(child)


@h.needs_tk
class TestNavigationAndAiWorkspace(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import tkinter as tk
        import winmon
        import winmon_core as core
        cls.winmon = winmon
        winmon.apply_theme("light")
        cls.root = tk.Tk()
        cls.root.geometry("1160x730+0+0")
        app = cls.app = winmon.WinMonApp.__new__(winmon.WinMonApp)
        app.root = cls.root
        app._settings_path = os.path.join(tempfile.mkdtemp(prefix="winmon-b36-"),
                                          "settings.json")
        app._settings_after_id = None
        app.history = core.ActionHistory(log_path=os.path.join(
            tempfile.mkdtemp(prefix="winmon-b36-"), "history.log"))
        app.show_hidden = tk.BooleanVar(value=False)
        app.show_no_window = tk.BooleanVar(value=False)
        app.only_windowed = tk.BooleanVar(value=True)
        app.follow_selection = tk.BooleanVar(value=True)
        app.live_gallery = tk.BooleanVar(value=True)
        app.fps_choice = tk.StringVar(value="很流畅 10 FPS")
        app.search_text = tk.StringVar()
        app.status_text = tk.StringVar(value="就绪")
        app.overview_text = tk.StringVar(value="")
        app._init_video_vars()
        app._build_ui()
        app.dpi_scale = 1.0
        cls.root.update()

    @classmethod
    def tearDownClass(cls):
        h.destroy_root(cls.root)
        cls.winmon.apply_theme("light")

    def test_navigation_is_consolidated_into_top_right_and_ai_card(self):
        """2026-10-21：左栏撤掉；设置=齿轮，搜索=放大镜，AI 工作区在 AI 卡里。"""
        self.assertFalse(hasattr(self.app, "action_rail"))
        self.assertEqual(str(self.app.settings_button.cget("text")), "⚙")
        self.assertEqual(self.app.ai_workspace_btn.cget("text"), "AI 工作区")
        self.assertEqual(str(self.app.gallery.remove_btn.cget("text")),
                         "移除总览")
        self.assertTrue(self.app.search_entry.winfo_exists())

    def test_ai_workspace_is_a_tab_and_contains_the_three_entries(self):
        nb = self.app.notebook
        self.assertEqual(len(nb.tabs()), 6)
        self.assertEqual(nb.tab(5, "text"), "AI 工作区")
        texts = [str(w.cget("text")) for w in walk(self.app.ai_workspace.frame)
                 if w.winfo_class() in ("Label", "TButton", "Button")]
        for label in ("AI 助手", "接入 AI", "AI 用量与预算",
                      "打开助手…", "打开接入…", "打开用量…"):
            self.assertIn(label, texts)
        self.app.action_show_ai_workspace()
        self.root.update()
        self.assertEqual(nb.index("current"), 5)


class FakePreview:
    def __init__(self):
        self.calls = []

    def clear(self, text=""):
        self.calls.append(("clear", text))

    def set_title(self, text):
        self.calls.append(("title", text))

    def set_meta(self, text):
        self.calls.append(("meta", text))

    def set_stats(self, text):
        self.calls.append(("stats", text))

    def set_hint(self, text):
        self.calls.append(("hint", text))


class FakeWorker:
    def __init__(self):
        self.forgot = []

    def forget(self, key):
        self.forgot.append(key)


class FakeTree:
    def __init__(self):
        self.selected = None

    def selection_set(self, item):
        self.selected = item

    def see(self, item):
        pass


class FakeWindow:
    def __init__(self, hwnd=2, pid=20):
        self.hwnd = hwnd
        self.pid = pid
        self.iconic = False
        self.is_hidden_surface = False
        self.real_name = "Notepad"
        self.title = "Untitled - Notepad"


@h.needs_tk
class TestPreviewFollowsGoneWindow(unittest.TestCase):
    def setUp(self):
        import tkinter as tk
        import winmon
        self.winmon = winmon
        self.root = tk.Tk()
        self.root.withdraw()
        self.addCleanup(self.root.destroy)
        app = self.app = winmon.WinMonApp.__new__(winmon.WinMonApp)
        app.root = self.root
        app.preview = FakePreview()
        app.worker = FakeWorker()
        app.win_by_hwnd = {}
        app.windows = [FakeWindow(2, 20)]
        app.row_of_hwnd = {2: "row2"}
        app.tree = FakeTree()
        app.follow_selection = tk.BooleanVar(value=True)
        app.paused = False
        app.preview_key = 1
        app.selected_hwnd = 1
        app.selected_pid = 10
        app._fg_hwnd = 0
        app._initial_preview_done = True
        app._last_preview_req = ("old",)
        app._statuses = []
        app._set_status = lambda text: app._statuses.append(text)
        app.start_preview = mock.Mock()

    def test_clears_old_frame_and_follows_next_window(self):
        self.app._ensure_preview_alive()
        self.assertEqual(self.app.preview_key, None)
        self.assertEqual(self.app.worker.forgot, [("win", 1)])
        self.assertEqual(self.app.tree.selected, "row2")
        self.app.start_preview.assert_called_once_with(2)
        self.assertEqual(self.app.selected_hwnd, 2)
        self.assertIn("已关闭", self.app.preview.calls[0][1])
        self.assertEqual(self.app.preview.calls[1], ("title", "（未选择程序）"))

    def test_follow_off_only_clears(self):
        self.app.follow_selection.set(False)
        self.app._ensure_preview_alive()
        self.app.start_preview.assert_not_called()
        self.assertIsNone(self.app.preview_key)
        self.assertIn("未自动切换", self.app._statuses[-1])

    def test_no_next_window_shows_empty_state(self):
        self.app.windows = []
        self.app._ensure_preview_alive()
        self.app.start_preview.assert_not_called()
        self.assertIn("没有可切换", self.app._statuses[-1])


@h.needs_tk
class TestCloseEscalation(unittest.TestCase):
    def test_timeout_shows_force_end_button_and_clear_hides_it(self):
        import tkinter as tk
        import winmon

        class Btn:
            def __init__(self):
                self.manager = ""
                self.text = ""

            def configure(self, **kw):
                self.text = kw.get("text", self.text)

            def winfo_manager(self):
                return self.manager

            def pack(self, **kw):
                self.manager = "pack"

            def pack_forget(self):
                self.manager = ""

        root = tk.Tk()
        root.withdraw()
        self.addCleanup(root.destroy)
        app = winmon.WinMonApp.__new__(winmon.WinMonApp)
        app.root = root
        app.escalate_btn = Btn()
        app._escalate_target = None
        app._set_status = lambda _t: None
        app._show_escalation(11, 22, "demo.exe", "Demo")
        self.assertEqual(app._escalate_target, (11, 22, "demo.exe", "Demo"))
        self.assertEqual(app.escalate_btn.manager, "pack")
        self.assertEqual(app.escalate_btn.text, "改为强制结束")
        app._clear_escalation()
        self.assertIsNone(app._escalate_target)
        self.assertEqual(app.escalate_btn.manager, "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
