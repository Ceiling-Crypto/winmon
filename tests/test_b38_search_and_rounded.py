# -*- coding: utf-8 -*-
"""
B38 —— 顶部搜索统一窗口/程序目录，以及主界面的圆角按钮。

用户 2026-10-21 的要求：
  · 顶部搜索不能只搜已运行窗口，也不能要求窗口标题一字不差；
  · 运行中的程序落到右侧窗口列表，未运行的切到程序目录并高亮；
  · 主界面按钮用现代圆角，勾选标记从方块改成圆填充；
  · 媒体键按“上一首 / 播放暂停 / 下一首”排，播放用三角符号。
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import _helpers as h                     # noqa: E402
import winmon_core as core               # noqa: E402


class TestProgramSearchNormalization(unittest.TestCase):
    def test_chinese_and_exe_names_find_the_same_program(self):
        item = core.ProgramItem(
            "网易云音乐",
            r"C:\Program Files\Netease\CloudMusic\cloudmusic.exe")
        for query in ("网易云音乐", "cloud music", "CloudMusic", "cloudmusic.exe"):
            self.assertTrue(core.program_matches_query(item, query),
                            f"{query!r} 应该命中同一条目录项")
        self.assertFalse(core.program_matches_query(item, "QQ音乐"))

    def test_search_ranking_prefers_exact_name(self):
        items = [
            core.ProgramItem("Cloud Music Helper", r"C:\A\helper.exe"),
            core.ProgramItem("网易云音乐", r"C:\B\cloudmusic.exe"),
        ]
        found = core.search_program_items(items, "网易云音乐")
        self.assertEqual([x.name for x in found], ["网易云音乐"])

    def test_search_also_matches_publisher_and_install_path(self):
        item = core.ProgramItem("某工具", r"C:\Tools\app.exe",
                                publisher="Example Studio",
                                install_location=r"C:\Tools")
        self.assertTrue(core.program_matches_query(item, "example studio"))
        self.assertTrue(core.program_matches_query(item, "C:\\Tools"))

    def test_guide_documents_ai_states_and_media_keys(self):
        import winmon
        zh = dict(winmon._GUIDE_CONTENT["zh"])
        main = "\n".join(body for _kind, body in zh["主界面"])
        setup = "\n".join(body for _kind, body in zh["AI 接入"])
        for piece in ("顶部搜索", "已接入 · MCP", "已接入 · API",
                      "未接入 · 纯本地", "上一首", "下一首", "播放/暂停",
                      "不调音量", "不读进度"):
            self.assertIn(piece, main)
        for piece in ("MCP", "API", "纯本地", "不联网"):
            self.assertIn(piece, setup)
        self.assertNotIn("可复制现场", main)
        en = dict(winmon._GUIDE_CONTENT["en"])
        en_main = "\n".join(body for _kind, body in en["Main window"])
        self.assertIn("Media keys", en_main)


@h.needs_tk
class TestUnifiedSearchUi(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import tkinter as tk
        import winmon
        cls.winmon = winmon
        winmon.apply_theme("light")
        cls.root = tk.Tk()
        cls.root.geometry("1120x720+0+0")
        app = cls.app = winmon.WinMonApp.__new__(winmon.WinMonApp)
        app.root = cls.root
        app._settings_path = os.path.join(tempfile.mkdtemp(prefix="winmon-b38-"),
                                          "settings.json")
        app._settings_after_id = None
        app.history = core.ActionHistory(log_path=os.path.join(
            tempfile.mkdtemp(prefix="winmon-b38-"), "history.log"))
        app.icons = winmon.IconProvider(winmon.ICON_SIZE, master=cls.root)
        app.program_prefs = core.CatalogPrefs()
        app.show_hidden = tk.BooleanVar(value=False)
        app.show_no_window = tk.BooleanVar(value=False)
        app.only_windowed = tk.BooleanVar(value=True)
        app.follow_selection = tk.BooleanVar(value=False)
        app.live_gallery = tk.BooleanVar(value=True)
        app.fps_choice = tk.StringVar(value="很流畅 10 FPS")
        app.search_text = tk.StringVar()
        app.status_text = tk.StringVar(value="就绪")
        app.overview_text = tk.StringVar(value="")
        app.dpi_scale = 1.0
        app.windows = []
        app.proc_map = {}
        app.win_by_hwnd = {}
        app.row_of_hwnd = {}
        app.row_info = {}
        app._group_app_of = {}
        app._cpu_pct = {}
        app._path_cache = {}
        app.selected_hwnd = None
        app.selected_pid = None
        app._fg_hwnd = 0
        app._media_playing = False
        app._init_video_vars()
        app._build_ui()
        cls.root.update_idletasks()

    @classmethod
    def tearDownClass(cls):
        h.destroy_root(cls.root)
        cls.winmon.apply_theme("light")

    def setUp(self):
        self.app.search_text.set("")
        self.app.catalog.items = []
        self.app.catalog.apply_filter()
        self.app.windows = []
        self.app.win_by_hwnd = {}
        self.app.row_of_hwnd = {}
        self.app._path_cache = {}
        self.app.selected_hwnd = None
        self.app.selected_pid = None
        self.app._search_results_current = []
        self.app._rebuild_tree(None)

    @staticmethod
    def _win(hwnd=42, pid=4242, title="Cloud Music", proc="cloudmusic.exe",
             path=r"C:\Apps\cloudmusic.exe"):
        wi = core.WinInfo(hwnd, pid, title, "OrpheusBrowserHost", 1000, 700,
                          False, False, False)
        wi.proc = proc
        wi.path = path
        return wi

    def _set_windows(self, windows):
        self.app.windows = list(windows)
        self.app.win_by_hwnd = {w.hwnd: w for w in self.app.windows}
        self.app._path_cache = {w.pid: w.path for w in self.app.windows if w.path}
        self.app._rebuild_tree(None)

    def test_running_program_is_selected_in_window_list(self):
        item = core.ProgramItem("网易云音乐", r"C:\Apps\cloudmusic.exe", pid=4242)
        self.app.catalog.items = [item]
        wi = self._win()
        self._set_windows([wi])
        self.app.search_text.set("网易云音乐")

        results = self.app._search_results_for_query("网易云音乐")
        self.assertEqual(results[0][0], "window",
                         "识别到 pid 时应优先落到运行中的窗口")
        self.assertEqual(results[0][1].hwnd, 42)

        self.app._search_results_current = results
        self.app._activate_search_result(0)
        self.assertEqual(self.app.selected_hwnd, 42)
        self.assertEqual(self.app.selected_pid, 4242)
        self.assertEqual(self.app.notebook.index("current"),
                         self.winmon.TAB_PREVIEW)

    def test_not_running_program_switches_to_catalog_and_highlights(self):
        item = core.ProgramItem("未运行测试程序", r"C:\NoSuch\never-running.exe")
        self.app.catalog.items = [item]
        self.app.search_text.set("未运行测试程序")
        results = self.app._search_results_for_query("未运行测试程序")
        self.assertEqual(results[0][0], "catalog")

        self.app._search_results_current = results
        self.app._activate_search_result(0)
        self.assertEqual(self.app.notebook.index("current"),
                         self.winmon.TAB_CATALOG,
                         "未运行时应切到程序目录标签页")
        selected = self.app.catalog.tree.selection()
        self.assertTrue(selected, "目录行必须被选中高亮")
        self.assertEqual(self.app.catalog.row_of_item[selected[0]].name,
                         "未运行测试程序")
        self.assertFalse(item.pid, "定位不应伪造“运行中”")

    def test_window_list_filter_uses_catalog_alias(self):
        item = core.ProgramItem("网易云音乐", r"C:\Apps\cloudmusic.exe", pid=4242)
        self.app.catalog.items = [item]
        self._set_windows([self._win()])
        self.app.search_text.set("网易云音乐")
        self.app._rebuild_tree(None)
        self.assertTrue(self.app.row_of_hwnd, "中文名应通过目录别名命中英文 exe 窗口")

    def test_recycled_pid_does_not_match_an_unrelated_window(self):
        item = core.ProgramItem("网易云音乐", r"C:\Apps\cloudmusic.exe", pid=4242)
        self.app.catalog.items = [item]
        wrong = self._win(title="记事本", proc="notepad.exe",
                          path=r"C:\Windows\notepad.exe")
        self._set_windows([wrong])
        self.app.search_text.set("网易云音乐")
        results = self.app._search_results_for_query("网易云音乐")
        self.assertTrue(results)
        self.assertEqual(results[0][0], "catalog",
                         "PID 被系统回收后不能把别的程序当成网易云")

    def test_text_variable_alone_triggers_search_debounce(self):
        """不依赖 KeyRelease：中文输入法/粘贴/预设文本也要触发搜索。"""
        self.app.search_text.set("winmon")
        self.assertIsNotNone(self.app._search_after,
                             "StringVar 变化后应安排一次搜索")
        self.app._cancel_search_debounce()
        self.assertIsNone(self.app._search_after)

    def test_search_entry_gets_focus_and_shows_focus_ring(self):
        self.app.search_entry.focus_force()
        self.root.update()
        self.assertIs(self.root.focus_get(), self.app.search_entry)
        #: FocusIn 事件在部分无窗口管理器的测试会话里不会自动派发，
        #: 这里直接走画布自己的焦点回调，只验证描边确实会变成强调色。
        self.app.search_field._on_focus()
        self.assertTrue(self.app.search_field._focused)
        outline = [str(self.app.search_field.itemcget(i, "outline")).lower()
                   for i in self.app.search_field.find_all()
                   if "polygon" in str(self.app.search_field.type(i))]
        self.assertIn(self.winmon.UI_ACCENT.lower(), outline)
        self.app.search_field._on_blur()

    def test_media_keys_order_and_symbols(self):
        self.assertEqual(self.app.media_prev_btn.cget("text"), "⏮")
        self.assertEqual(self.app.media_play_btn.cget("text"), "▶")
        self.assertEqual(self.app.media_next_btn.cget("text"), "⏭")
        labels = [w.cget("text") for w in self.app.media_play_btn.master.pack_slaves()
                  if hasattr(w, "cget") and w.winfo_class() == "Button"]
        self.assertEqual(labels, ["⏮", "▶", "⏭"],
                         "媒体键顺序必须是上一首 → 播放/暂停 → 下一首")
        self.app._media_playing = False
        self.app._sync_media_play_button()
        self.assertEqual(self.app.media_play_btn.cget("text"), "▶")
        self.app._media_playing = True
        self.app._sync_media_play_button()
        self.assertEqual(self.app.media_play_btn.cget("text"), "⏸")

    def test_rounded_buttons_keep_button_semantics(self):
        btn = self.app.media_play_btn
        self.assertEqual(btn.winfo_class(), "Button")
        #: 2026-10-22 第四轮起媒体键是裸图标键，不再用深色 Media.TButton。
        self.assertIsInstance(btn, self.winmon.RoundedButton)
        self.assertTrue(btn._bare)
        self.assertEqual(str(btn.cget("width")), "30")
        self.assertGreaterEqual(btn.winfo_reqheight(), 28)


if __name__ == "__main__":
    unittest.main(verbosity=2)
