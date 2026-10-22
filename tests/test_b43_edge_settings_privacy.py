# -*- coding: utf-8 -*-
"""
B43 —— 2026-10-22 第三轮：AI/媒体归位、Edge 式设置与 key 隐私。

这一轮用户点名的事实（含 2026-10-22 第四轮修订）：
  · AI 工作区回到最后一栏，标题行不再写 WinMon；
  · 顶部左侧是实时概览，右侧是搜索/设置，设置不再孤立；
  · 媒体播放器 + 媒体键在曲名上方，媒体键是无边框裸按钮；
  · 设置改成 Edge 的左侧导航 + 中性卡片；
  · 所有开关都是 46×26 的 Edge 蓝，红色不再表示“打开”；
  · key 只从剪贴板读入，始终掩码，没有“显示/复制 key”；
  · 透明度滑杆的已选填充必须连续，不能看起来断成两截。
  · 列表装得下时，右侧不能留下滚动条空白竖条。
"""
from __future__ import annotations

import os
import sys
import time
import unittest
from types import SimpleNamespace
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
        winmon.apply_theme("cobalt")
        cls.root, cls.app = build_app(1160, 730)
        cls.root.update()

    @classmethod
    def tearDownClass(cls):
        h.destroy_root(cls.root)

    def _open(self):
        with mock.patch.object(core, "get_autostart", return_value=False):
            dlg = self.winmon._SettingsWindow(
                self.root, self.app, "tray", True, True, 30, "zh", 9,
                "cobalt", 80, {})
        self.addCleanup(dlg._cancel)
        self.root.update()
        return dlg

    def test_sidebar_and_cards_replace_the_old_capsule_tabs(self):
        dlg = self._open()
        self.assertEqual(list(dlg._nav_items),
                         ["general", "ai", "guide", "about"])
        self.assertTrue(dlg.settings_search.winfo_exists())
        self.assertFalse(hasattr(dlg, "tab_strip"),
                         "Edge 式设置不再使用顶部胶囊标签")
        self.assertEqual([dlg.nb.tab(i, "text")
                          for i in range(len(dlg.nb.tabs()))],
                         ["常规", "AI 用量", "使用说明", "关于"])
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
        for section in ("general", "ai", "guide", "about"):
            self.assertEqual(dlg._nav_items[section]["frame"].winfo_manager(),
                             "pack")

    def test_about_page_declares_license_contact_and_pet_credit(self):
        dlg = self._open()
        dlg.select("about")
        self.root.update()
        self.assertEqual(dlg.nb.index("current"),
                         dlg._section_tabs["about"])
        joined = "\n".join(texts(dlg.about.frame))
        self.assertIn("关于 winmon1.0", joined)
        self.assertIn("MIT", joined)
        self.assertIn("3688068026", joined)
        self.assertIn("3688068026@qq.com", joined)
        self.assertIn("非商业", joined)
        self.assertIn("DeepSeek", joined)
        self.assertIn("志同道合", joined)
        self.assertIn("不一定要会写代码", joined)
        self.assertEqual(dlg.about.links["dsh_pet"],
                         "https://github.com/PC2005-cloud/dsh-pet")
        self.assertEqual(dlg.about.links["issues"],
                         "https://github.com/Ceiling-Crypto/winmon/issues")
        self.assertEqual(dlg.about.links["repo"],
                         "https://github.com/Ceiling-Crypto/winmon")

    def test_all_toggles_are_large_edge_blue_switches(self):
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
            return str(toggle.itemcget(items[0], "fill")).lower()

        expected = self.winmon.SWITCH_ON_LIGHT.lower()
        self.assertTrue(toggles, "设置页没有任何 ToggleSwitch")
        for toggle in toggles:
            self.assertGreaterEqual(int(toggle.cget("width")), 46)
            self.assertGreaterEqual(int(toggle.cget("height")), 26)
            self.assertEqual(str(toggle.cget("bg")).lower(),
                             self.winmon.UI_SURFACE.lower())
            if toggle.get():
                self.assertEqual(track_fill(toggle), expected,
                                 "打开状态必须统一为 Edge 蓝，不能再用红/黑")
            else:
                self.assertNotEqual(track_fill(toggle), expected,
                                    "关闭状态不能是蓝色开启态")
        # 关掉的普通开关仍是胶囊，不是又退回圆点。
        off = next(t for t in toggles
                   if t._variant == "normal" and not t.get())
        self.assertEqual(off._state, "normal")
        self.assertTrue([i for i in off.find_all()
                         if "toggle-knob" in off.gettags(i)])

    def test_toggle_slides_instead_of_jumping(self):
        dlg = self._open()
        toggles = [w for w in walk(dlg._general_canvas)
                   if isinstance(w, self.winmon.ToggleSwitch)
                   and w._state == "normal"]
        toggle = next(t for t in toggles if not t.get())
        toggle.set(True)
        self.assertIsNotNone(toggle._anim_after, "打开时应启动 120ms 滑动")
        deadline = time.time() + 0.5
        while time.time() < deadline:
            self.root.update()
            if toggle._anim_after is None:
                break
            time.sleep(0.01)
        self.assertIsNone(toggle._anim_after)
        self.assertAlmostEqual(toggle._pos, 1.0, places=3)
        self.assertLessEqual(self.winmon.ToggleSwitch.ANIM_MS, 160,
                            "滑动应短促，不能拖成慢动画")

    def test_capsule_combo_opens_and_selects(self):
        dlg = self._open()
        combos = [w for w in walk(dlg.top)
                  if isinstance(w, self.winmon.CapsuleCombo)]
        self.assertTrue(combos, "设置里应使用自绘胶囊下拉")
        theme = next(c for c in combos
                     if "钴蓝" in list(c.cget("values")))
        theme._open_popup()
        self.root.update()
        self.assertIsNotNone(theme._popup, "点击后应出现胶囊选项浮层")
        self.assertTrue(theme._popup.winfo_exists())
        drawn = [str(theme._pop_canvas.itemcget(i, "text"))
                 for i in theme._pop_canvas.find_all()
                 if "combo-option" in theme._pop_canvas.gettags(i)]
        self.assertIn("钴蓝", drawn, "选项文字不能全被裁成省略号")
        self.assertNotIn("…", drawn)
        names = list(theme.cget("values"))
        theme._choose_row(names.index("山雪"))
        self.root.update()
        self.assertEqual(dlg.theme.get(), "山雪")
        self.assertIsNone(theme._popup, "选完后浮层应销毁")

    def test_capsule_combo_closes_on_outside_click_without_choosing(self):
        """下拉不是霸王条款：点外面就收起，并且保留原值。"""
        dlg = self._open()
        combos = [w for w in walk(dlg.top)
                  if isinstance(w, self.winmon.CapsuleCombo)]
        combo = next(c for c in combos if "钴蓝" in list(c.cget("values")))
        before = combo._var.get()
        combo._open_popup()
        self.root.update()
        self.assertTrue(combo._open)
        self.winmon.CapsuleCombo._dismiss_open_click(
            SimpleNamespace(widget=dlg.top, x_root=1, y_root=1))
        self.root.update()
        self.assertFalse(combo._open, "点下拉外应能关闭")
        self.assertIsNone(combo._popup)
        self.assertEqual(combo._var.get(), before, "关闭不等于替用户选一项")

    def test_selected_nav_is_a_rounded_canvas_pill(self):
        dlg = self._open()
        item = dlg._nav_items["general"]
        self.assertEqual(item["frame"].winfo_class(), "Canvas")
        items = [i for i in item["frame"].find_all()
                 if "nav-bg" in item["frame"].gettags(i)]
        self.assertTrue(items)
        fill = str(item["frame"].itemcget(items[0], "fill"))
        self.assertEqual(fill.lower(), self.winmon.UI_SELECT_BG.lower())

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
        winmon.apply_theme("cobalt")
        cls.root, cls.app = build_app(1160, 730)
        cls.root.update()

    @classmethod
    def tearDownClass(cls):
        h.destroy_root(cls.root)

    def test_overview_is_the_left_side_of_the_top_toolbar(self):
        label = self.app.overview_label
        self.assertEqual(label.pack_info()["side"], "left")
        self.assertIs(label.master, self.app.search_field.master)
        self.assertLess(label.winfo_rootx(),
                        self.app.search_field.winfo_rootx(),
                        "左侧概览必须在搜索框左边，不能继续留空")
        self.assertIn("窗口", self.app.overview_text.get())
        self.assertNotIn("WinMon", self.app.overview_text.get())

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

    def test_media_controls_above_full_width_title(self):
        #: “媒体”两个字和曲名同排、同一条基线，不再悬在控件行左侧。
        self.assertIs(self.app.media_badge.master,
                      self.app.media_label.master)
        self.assertLessEqual(abs(self.app.media_badge.winfo_rooty()
                                 - self.app.media_label.winfo_rooty()), 4)
        self.assertLess(self.app.media_badge.winfo_rootx(),
                        self.app.media_label.winfo_rootx())
        keys = self.app.media_play_btn.master
        self.assertIs(keys.master,
                      self.app.media_app_combo.master.master,
                      "媒体键与播放器应在同一排")
        self.assertLess(keys.winfo_rootx(),
                        self.app.media_app_combo.winfo_rootx(),
                        "媒体键应在播放器左边")
        self.assertLess(self.app.media_app_combo.winfo_rooty(),
                        self.app.media_label.winfo_rooty(),
                        "播放器应在曲名上方")
        self.assertLess(self.app.media_play_btn.winfo_rooty(),
                        self.app.media_label.winfo_rooty(),
                        "媒体键应在曲名上方")
        for button, icon in (
                (self.app.media_prev_btn, "media_prev"),
                (self.app.media_play_btn, "media_play"),
                (self.app.media_next_btn, "media_next")):
            self.assertEqual(button._icon, icon)
            self.assertTrue(button._bare)
            self.assertTrue([i for i in button.find_all()
                             if "jelly-icon" in button.gettags(i)])

    def test_media_and_ai_share_one_two_column_status_band(self):
        self.assertTrue(self.app.status_band.winfo_exists())
        self.assertIs(self.app.media_pane.master, self.app.status_band)
        self.assertIs(self.app.ai_pane.master, self.app.status_band)
        self.assertLess(self.app.media_pane.winfo_rootx(),
                        self.app.ai_pane.winfo_rootx(),
                        "媒体在左、AI 在右")
        self.assertEqual(self.app._status_divider.winfo_manager(), "grid")
        # 两栏高度由同一条 band 的行拉伸，不应该再是上下两条整宽横条。
        self.assertLess(abs(self.app.media_pane.winfo_height()
                            - self.app.ai_pane.winfo_height()), 3)

    def test_icon_only_controls_have_tooltips(self):
        for button in (self.app.settings_button, self.app.media_prev_btn,
                       self.app.media_next_btn):
            self.assertIsNotNone(button._tip, "图标按钮必须有 tooltip")
            self.assertTrue(button._tip._resolve())
        play = self.app.media_play_btn
        self.assertIsNotNone(play._tip)
        old = bool(self.app._media_playing)
        try:
            self.app._media_playing = False
            self.assertIn(play._tip._resolve(), ("播放", "Play"))
            self.app._media_playing = True
            self.assertIn(play._tip._resolve(), ("暂停", "Pause"))
        finally:
            self.app._media_playing = old

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
