# -*- coding: utf-8 -*-
"""
B43 —— 2026-10-22 第三轮：AI/媒体归位、Edge 式设置与 key 隐私。

这一轮用户点名的事实：
  · AI 工作区回到最后一栏，标题行不再写 WinMon；
  · 媒体键紧挨标题，播放器在下一行，不重叠；
  · 设置改成 Edge 的左侧导航 + 右侧卡片；
  · 启动/关闭的勾选是黑色，其余勾选是红色；
  · key 只从剪贴板读入，始终掩码，没有“显示/复制 key”；
  · 透明度滑杆的已选填充必须连续，不能看起来断成两截。
  · 列表装得下时，右侧不能留下滚动条空白竖条。
"""
from __future__ import annotations

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import _helpers as h                     # noqa: E402
import winmon_core as core               # noqa: E402


def walk(widget):
    yield widget
    for child in widget.winfo_children():
        yield from walk(child)


def texts(widget):
    out = []
    for w in walk(widget):
        try:
            out.append(str(w.cget("text")))
        except Exception:
            pass
    return out


@h.needs_tk
class TestEdgeSettings(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from shot import build_app
        import winmon
        cls.winmon = winmon
        winmon.apply_theme("sunlit")
        cls.root, cls.app = build_app(1160, 730)
        cls.root.update()

    @classmethod
    def tearDownClass(cls):
        h.destroy_root(cls.root)

    def _open(self):
        with mock.patch.object(core, "get_autostart", return_value=False):
            dlg = self.winmon._SettingsWindow(
                self.root, self.app, "tray", True, True, 30, "zh", 9,
                "sunlit", 80, {})
        self.addCleanup(dlg._cancel)
        self.root.update()
        return dlg

    def test_sidebar_and_cards_replace_the_old_capsule_tabs(self):
        dlg = self._open()
        self.assertEqual(list(dlg._nav_items), ["general", "ai", "guide"])
        self.assertTrue(dlg.settings_search.winfo_exists())
        self.assertFalse(hasattr(dlg, "tab_strip"),
                         "Edge 式设置不再使用顶部胶囊标签")
        self.assertEqual([dlg.nb.tab(i, "text")
                          for i in range(len(dlg.nb.tabs()))],
                         ["常规", "AI 用量", "使用说明"])
        # 常规页是卡片流：有多个分组标题和可滚动 canvas。
        labels = texts(dlg._general_canvas)
        for section in ("外观", "窗口列表", "主界面内容", "启动与关闭", "维护"):
            self.assertIn(section, labels)
        self.assertTrue(dlg._hint_sources)
        self.assertEqual(str(dlg._general_canvas.cget("bg")), self.winmon.UI_BG)

    def test_search_filters_the_left_navigation(self):
        dlg = self._open()
        dlg._settings_search_text.set("预算")
        dlg._filter_nav()
        self.root.update()
        self.assertEqual(dlg._nav_items["ai"]["frame"].winfo_manager(), "pack")
        self.assertEqual(dlg._nav_items["general"]["frame"].winfo_manager(), "")
        self.assertEqual(dlg._nav_items["guide"]["frame"].winfo_manager(), "")
        self.assertEqual(dlg.nb.index("current"), dlg._section_tabs["ai"])
        dlg._settings_search_text.set("")
        dlg._filter_nav()
        self.root.update()
        for section in ("general", "ai", "guide"):
            self.assertEqual(dlg._nav_items[section]["frame"].winfo_manager(),
                             "pack")

    def test_startup_toggles_are_black_and_others_are_red(self):
        dlg = self._open()
        toggles = [w for w in walk(dlg._general_canvas)
                   if isinstance(w, self.winmon.ToggleSwitch)]
        self.assertGreaterEqual(len(toggles), 7)
        self.assertEqual(sum(1 for t in toggles
                             if t._variant == "startup"), 3)

        def track_fill(toggle):
            items = [i for i in toggle.find_all()
                     if "toggle-track" in toggle.gettags(i)]
            self.assertTrue(items, "开关没有画出胶囊轨道")
            return str(toggle.itemcget(items[0], "fill"))

        normal = next(t for t in toggles
                      if t._variant == "normal" and t.get())
        startup = next(t for t in toggles
                       if t._variant == "startup" and t.get())
        self.assertEqual(track_fill(normal), "#D13438")
        self.assertEqual(track_fill(startup), "#101010")
        # 关掉的普通开关仍是胶囊，不是又退回圆点。
        off = next(t for t in toggles
                   if t._variant == "normal" and not t.get())
        self.assertEqual(off._state, "normal")

    def test_old_migration_hints_are_not_in_settings(self):
        dlg = self._open()
        joined = "\n".join(texts(dlg.top))
        for gone in ("主窗口不再显示", "以前常驻", "这些选项以前",
                     "AI 用量和使用说明也在这里", "主界面只留一个入口"):
            self.assertNotIn(gone, joined)
        # 必要说明仍在：托盘不可用/最低透明度/不自动关程序。
        self.assertIn("最低 45%", joined)
        self.assertIn("不会自动结束程序", joined)


@h.needs_tk
class TestRound3MainPlacement(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from shot import build_app
        import winmon
        cls.winmon = winmon
        winmon.apply_theme("sunlit")
        cls.root, cls.app = build_app(1160, 730)
        cls.root.update()

    @classmethod
    def tearDownClass(cls):
        h.destroy_root(cls.root)

    def test_ai_tab_is_last_and_title_row_has_no_winmon(self):
        nb = self.app.notebook
        self.assertEqual(nb.tab(len(nb.tabs()) - 1, "text"), "AI 工作区")
        self.assertEqual(nb.tab(0, "text"), "实时预览")
        self.assertFalse(hasattr(self.app, "titlebar_label"))
        self.assertEqual(self.app.search_field.pack_info()["side"], "right")
        self.assertEqual(self.app.settings_button.pack_info()["side"], "right")
        self.assertLess(self.app.search_field.winfo_rootx(),
                        self.app.settings_button.winfo_rootx(),
                        "搜索框仍应在齿轮左侧，位置没有互换")

    def test_media_keys_sit_beside_title_and_player_below(self):
        keys = self.app.media_play_btn.master
        self.assertIs(keys.master, self.app.media_label.master)
        self.assertIsNot(keys.master, self.app.media_app_combo.master)
        self.assertGreater(self.app.media_app_combo.winfo_rooty(),
                           self.app.media_label.winfo_rooty())
        self.assertGreater(self.app.media_app_combo.winfo_rooty(),
                           self.app.media_play_btn.winfo_rooty())
        for button, icon in (
                (self.app.media_prev_btn, "media_prev"),
                (self.app.media_play_btn, "media_play"),
                (self.app.media_next_btn, "media_next")):
            self.assertEqual(button._icon, icon)
            self.assertTrue([i for i in button.find_all()
                             if "jelly-icon" in button.gettags(i)])

    def test_window_scrollbar_disappears_when_the_list_fits(self):
        """用户圈出的右侧空白竖条：列表装得下时不能占位。"""
        self.assertEqual(self.app.window_scroll.winfo_manager(), "",
                         "空列表不应留下滚动条空白竖条")
        try:
            for i in range(40):
                self.app.tree.insert("", "end", iid=f"b43-{i}", text="",
                                     values=(f"app{i}", f"title{i}", "运行中"))
            self.root.update()
            self.assertEqual(self.app.window_scroll.winfo_manager(), "grid",
                             "列表超出高度后应出现细滚动条")
        finally:
            for i in range(40):
                try:
                    self.app.tree.delete(f"b43-{i}")
                except Exception:
                    pass
            self.root.update()
        self.assertEqual(self.app.window_scroll.winfo_manager(), "",
                         "列表恢复后滚动条应再次收起")


if __name__ == "__main__":
    unittest.main(verbosity=2)
