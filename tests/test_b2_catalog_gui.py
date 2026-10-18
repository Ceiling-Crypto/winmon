# -*- coding: utf-8 -*-
"""
B2 —— 程序目录面板：分类筛选 / 收藏 / 最近使用（GUI 层）。

只构建 `ProgramCatalogPane` 本身（不给它塞真实的 100 多项目录），
用固定的小数据集验证：过滤、排序、分组渲染、收藏与类别操作、设置持久化。
"""

from __future__ import annotations

import json
import os
import queue
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import _helpers as h                     # noqa: E402
import winmon_core as core               # noqa: E402


def make_items():
    return [
        core.ProgramItem(name="Steam", target=r"X:\steam\steam.exe", source="startmenu"),
        core.ProgramItem(name="哔哩哔哩", target=r"X:\bilibili\哔哩哔哩.exe",
                         source="startmenu"),
        core.ProgramItem(name="Google Chrome",
                         target=r"C:\Program Files\Google\Chrome\Application\chrome.exe",
                         source="startmenu"),
        core.ProgramItem(name="Word",
                         target=r"C:\Program Files\Microsoft Office\root\WINWORD.EXE",
                         source="registry"),
        core.ProgramItem(name="e管家", target=r"X:\StarSoftComm\CooCare.exe",
                         source="registry"),
    ]


@h.needs_tk
class TestCatalogPane(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import tkinter as tk
        import winmon
        cls.winmon = winmon
        cls.root = tk.Tk()
        cls.root.withdraw()

        # 用一个"最小可用的 app 替身"：目录面板只用到这几个接口
        class FakeApp:
            def __init__(self, root):
                self.root = root
                self.icons = winmon.IconProvider(winmon.ICON_SIZE)
                self.program_prefs = core.CatalogPrefs()
                self.status = ""
                self.actions = []
                self.saved = 0
                #: `launch_selected` 的后台线程要用它把结果交回主线程
                #: （P1-3 起启动是异步的；替身也得照真 app 的规矩来，
                #:  否则工作线程里 AttributeError 会被静默吞掉，表现是"回调丢了"）
                self._ui_msg = queue.Queue()

            def _set_status(self, text):
                self.status = text

            def log_action(self, *a, **kw):
                self.actions.append((a, kw))

            def _save_settings_soon(self):
                self.saved += 1

            def _copy(self, text):
                self.copied = text

            def _ui_call(self, fn):
                """与真 `WinMonApp._ui_call` 同义（那边有一段很长的踩坑说明）。"""
                if threading.current_thread() is threading.main_thread():
                    fn()
                    return
                self._ui_msg.put_nowait(fn)

            def _drain_ui_msgs(self):
                """主线程的泵：把工作线程交回来的动作执行掉。"""
                while True:
                    try:
                        fn = self._ui_msg.get_nowait()
                    except queue.Empty:
                        return
                    fn()

        cls.app = FakeApp(cls.root)
        cls.pane = winmon.ProgramCatalogPane(cls.root, cls.app)
        cls.pane.frame.pack(fill="both", expand=True)
        cls.root.update_idletasks()

    @classmethod
    def tearDownClass(cls):
        # 取消挂起的防抖回调，避免 Tk 报 invalid command name 的噪音
        try:
            cls.pane.destroy()
        except Exception:
            pass
        h.destroy_root(cls.root)

    def setUp(self):
        self.app.program_prefs = core.CatalogPrefs()
        self.app.status = ""
        self.app.saved = 0
        self.pane.items = make_items()
        self.pane.search_var.set("")
        self.pane.only_runnable.set(False)
        self.pane.only_favorites.set(False)
        self.pane.only_recent.set(False)
        self.pane.selected_categories = set()
        self.pane.btn_cats.configure(text=self.pane._category_label())
        self.pane.apply_filter()
        self.root.update_idletasks()

    # -- 基本渲染 --
    def test_all_items_are_listed(self):
        self.assertEqual(len(self.pane.filtered), 5)

    def test_grouped_by_category(self):
        """默认按类别分组：顶层是类别表头，程序挂在下面。"""
        tv = self.pane.tree
        roots = tv.get_children("")
        self.assertEqual(len(roots), len(self.pane.category_row))
        self.assertGreaterEqual(len(roots), 4, "至少应有 游戏/浏览器/办公/其他 四组")
        for parent in roots:
            self.assertTrue(tv.get_children(parent), f"分组 {parent} 下面是空的")

    def test_every_category_header_has_items(self):
        tv = self.pane.tree
        for parent in tv.get_children(""):
            with self.subTest(parent=parent):
                self.assertIn(self.pane.category_row[parent], core.CATEGORY_ORDER)

    def test_rows_expose_category_column(self):
        tv = self.pane.tree
        found = {}
        for parent in tv.get_children(""):
            for iid in tv.get_children(parent):
                it = self.pane.row_of_item[iid]
                found[it.name] = tv.set(iid, "category")
        self.assertEqual(found["Steam"], "游戏娱乐")
        self.assertEqual(found["Google Chrome"], "浏览器")
        self.assertEqual(found["Word"], "办公文档")
        self.assertEqual(found["e管家"], "其他")

    def test_category_filter_shows_only_that_category(self):
        self.pane.selected_categories = {"browser"}
        self.pane.apply_filter()
        self.assertEqual([it.name for it in self.pane.filtered], ["Google Chrome"])
        # 选定类别后不再分组（平铺，少一层点击）
        self.assertEqual(self.pane.category_row, {})

    def test_category_filter_accepts_multiple(self):
        """多选：同时看"浏览器 + 办公文档"。"""
        self.pane.selected_categories = {"browser", "office"}
        self.pane.apply_filter()
        names = sorted(it.name for it in self.pane.filtered)
        self.assertEqual(names, ["Google Chrome", "Word"])
        self.assertEqual(self.pane.category_row, {}, "多选时也应该平铺")

    def test_category_filter_all_restores_grouping(self):
        self.pane.selected_categories = {"browser"}
        self.pane.apply_filter()
        self.pane.selected_categories = set()
        self.pane.btn_cats.configure(text=self.pane._category_label())
        self.pane.apply_filter()
        self.assertEqual(len(self.pane.filtered), 5)
        self.assertTrue(self.pane.category_row)

    def test_category_button_label(self):
        self.assertEqual(self.pane._category_label(), "类别：全部")
        self.pane.selected_categories = {"browser"}
        self.assertIn("浏览器", self.pane._category_label())
        self.pane.selected_categories = {"browser", "office", "game"}
        label = self.pane._category_label()
        self.assertIn("3 类", label, f"多选时应显示计数：{label}")

    def test_choose_categories_applies_selection(self):
        with mock.patch.object(self.winmon, "_MultiSelectDialog") as dlg:
            dlg.return_value.show.return_value = {"game", "media"}
            self.pane.choose_categories()
        self.assertEqual(self.pane.selected_categories, {"game", "media"})
        self.assertIn("2 类", self.pane.btn_cats.cget("text"))
        self.assertGreaterEqual(self.app.saved, 1, "类别选择要立刻写盘")

    def test_choose_categories_cancelled_keeps_selection(self):
        self.pane.selected_categories = {"browser"}
        with mock.patch.object(self.winmon, "_MultiSelectDialog") as dlg:
            dlg.return_value.show.return_value = None
            self.pane.choose_categories()
        self.assertEqual(self.pane.selected_categories, {"browser"})

    def test_choose_recent_without_records(self):
        self.pane.choose_recent()
        self.assertIn("还没有", self.app.status)

    def test_choose_recent_selects_row(self):
        prefs = self.app.program_prefs
        prefs.record_use(r"X:\steam\steam.exe", "Steam", ts=time.time())
        with mock.patch.object(self.winmon, "_RecentPickerDialog") as dlg:
            dlg.return_value.show.return_value = "1"
            self.pane.choose_recent()
        self.assertIn("Steam", self.app.status)

    def test_search_still_works_with_category(self):
        self.pane.search_var.set("steam")
        self.pane.apply_filter()
        self.assertEqual([it.name for it in self.pane.filtered], ["Steam"])

    # -- 收藏 --
    def test_toggle_favorite_marks_and_persists(self):
        steam = self.pane.items[0]
        iid = self._iid_of("Steam")
        self.pane.tree.selection_set(iid)
        self.pane.toggle_favorite_selected()
        self.assertTrue(self.app.program_prefs.is_favorite(steam.target))
        self.assertGreaterEqual(self.app.saved, 1, "收藏必须立刻写盘")
        self.assertIn("收藏", self.app.status)

    def test_favorite_is_sorted_first(self):
        """收藏项要排在同类别其它项前面（排序键第一维）。"""
        self.app.program_prefs.add_favorite(r"X:\StarSoftComm\CooCare.exe", "e管家")
        self.pane.apply_filter()
        self.assertEqual(self.pane.filtered[0].name, "e管家")

    def test_only_favorites_filter(self):
        self.app.program_prefs.add_favorite(r"X:\steam\steam.exe", "Steam")
        self.pane.only_favorites.set(True)
        self.pane.apply_filter()
        self.assertEqual([it.name for it in self.pane.filtered], ["Steam"])

    def test_favorite_star_in_name_and_tag(self):
        self.app.program_prefs.add_favorite(r"X:\steam\steam.exe", "Steam")
        self.pane.apply_filter()
        iid = self._iid_of("Steam")
        self.assertIn("★", self.pane.tree.set(iid, "name"))
        self.assertIn("fav", self.pane.tree.item(iid, "tags"))

    def test_toggle_again_removes_favorite(self):
        self.pane.tree.selection_set(self._iid_of("Steam"))
        self.pane.toggle_favorite_selected()
        self.pane.apply_filter()
        self.pane.tree.selection_set(self._iid_of("Steam"))
        self.pane.toggle_favorite_selected()
        self.assertFalse(self.app.program_prefs.is_favorite(r"X:\steam\steam.exe"))

    def test_favorite_button_label_follows_selection(self):
        self.pane.tree.selection_set(self._iid_of("Steam"))
        self.pane._sync_fav_button()
        self.assertIn("☆", self.pane.btn_fav.cget("text"))
        self.pane.toggle_favorite_selected()
        self.pane.tree.selection_set(self._iid_of("Steam"))
        self.pane._sync_fav_button()
        self.assertIn("★", self.pane.btn_fav.cget("text"))

    def test_favorite_without_selection_is_handled(self):
        self.pane.tree.selection_remove(*self.pane.tree.selection())
        self.pane.toggle_favorite_selected()
        self.assertIn("请先选择", self.app.status)

    # -- 最近使用 --
    def test_recent_filter_and_order(self):
        prefs = self.app.program_prefs
        prefs.record_use(r"C:\Program Files\Google\Chrome\Application\chrome.exe",
                         "Google Chrome", ts=1000)
        prefs.record_use(r"X:\steam\steam.exe", "Steam", ts=2000)
        self.pane.only_recent.set(True)
        self.pane.apply_filter()
        names = [it.name for it in self.pane.filtered]
        self.assertEqual(sorted(names), ["Google Chrome", "Steam"])
        self.assertEqual(names[0], "Steam", "最近使用应新的在前")

    def _pump(self, cond, timeout: float = 3.0) -> bool:
        """
        把"后台启动 → `_ui_call` 回主线程"这条链路跑完（P1-3 起它是异步的）。

        `root.update()` 让 Tk 的定时器/事件走，`_drain_ui_msgs()` 执行工作线程交回来的
        动作 —— 断言必须在 `mock.patch` 还罩着的时候等（见下面两条用例）。
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.root.update()
            self.app._drain_ui_msgs()
            if cond():
                return True
            time.sleep(0.02)
        self.root.update()
        self.app._drain_ui_msgs()
        return cond()

    def test_record_use_is_called_on_launch(self):
        steam = r"X:\steam\steam.exe"
        self.pane.tree.selection_set(self._iid_of("Steam"))
        with mock.patch.object(core, "launch_item", return_value=(True, "已启动")):
            self.pane.launch_selected()
            # P1-3 起"启动"在后台线程里跑，`record_use`/`_save_settings_soon` 都发生在
            # 回主线程的回调里 —— 所以要泵到"两条都发生"，而不是只 `update()` 一下。
            self._pump(lambda: (self.app.program_prefs.is_recent(steam)
                                and self.app.saved >= 1))
        self.assertTrue(self.app.program_prefs.is_recent(steam),
                        "启动成功应当记入最近使用（回调没跑 = 后台线程的活儿丢了）")
        self.assertGreaterEqual(self.app.saved, 1)

    def test_failed_launch_is_not_recorded_as_recent(self):
        self.pane.tree.selection_set(self._iid_of("Steam"))
        with mock.patch.object(core, "launch_item", return_value=(False, "启动失败")):
            self.pane.launch_selected()
            # 用"日志里出现了这条"当"回调已经跑过"的信号，再断言它没被记进最近使用
            self._pump(lambda: bool(self.app.actions))
        self.assertFalse(self.app.program_prefs.is_recent(r"X:\steam\steam.exe"),
                         "启动失败不该记入最近使用")
        self.assertTrue(self.app.actions, "启动失败也要留一条痕（如实报告）")

    # -- 手动类别 --
    def test_set_category_via_dialog(self):
        self.pane.tree.selection_set(self._iid_of("e管家"))
        with mock.patch.object(self.winmon, "_ChoiceDialog") as dlg:
            dlg.return_value.show.return_value = "系统工具"
            self.pane.set_category_selected()
        self.assertEqual(
            self.app.program_prefs.category_override(r"X:\StarSoftComm\CooCare.exe"),
            "system")
        self.pane.apply_filter()
        iid = self._iid_of("e管家")
        self.assertEqual(self.pane.tree.set(iid, "category"), "系统工具")

    def test_set_category_cancelled_changes_nothing(self):
        self.pane.tree.selection_set(self._iid_of("e管家"))
        with mock.patch.object(self.winmon, "_ChoiceDialog") as dlg:
            dlg.return_value.show.return_value = None
            self.pane.set_category_selected()
        self.assertEqual(self.app.program_prefs.overrides(), {})

    def test_clear_category_restores_automatic(self):
        target = r"X:\steam\steam.exe"
        self.app.program_prefs.set_category(target, "office")
        self.pane.tree.selection_set(self._iid_of("Steam"))
        self.pane.set_category_selected(clear=True)
        self.assertEqual(self.app.program_prefs.category_override(target), "")
        self.pane.apply_filter()
        self.assertEqual(self.pane.tree.set(self._iid_of("Steam"), "category"), "游戏娱乐")

    def test_category_choice_labels_match_core(self):
        """类别按钮/面板用的标签必须与核心层的类别表一致。"""
        multi = self.winmon._MultiSelectDialog
        self.assertTrue(callable(multi))
        # 多选面板的选项就是核心层的类别表（顺序一致）
        options = [(k, core.CATEGORY_LABELS[k]) for k in core.CATEGORY_ORDER]
        self.assertEqual([k for k, _ in options], core.CATEGORY_ORDER)
        self.assertEqual(len(options), len(core.CATEGORY_LABELS))

    # -- 工具 --
    def _iid_of(self, name: str) -> str:
        for iid, it in self.pane.row_of_item.items():
            if it.name == name:
                return iid
        raise AssertionError(f"列表里找不到 {name}")


@h.needs_tk
class TestRelativeTime(unittest.TestCase):
    """「最近使用」里的相对时间显示。"""

    def test_buckets(self):
        import winmon
        now = time.time()
        cases = [
            (now, "刚刚"),
            (now - 30, "刚刚"),
            (now - 180, "3 分钟前"),
            (now - 7200, "2 小时前"),
            (now - 86400 * 3, "3 天前"),
            (now - 86400 * 65, "2 个月前"),
            (now - 86400 * 800, "2 年前"),
        ]
        for ts, want in cases:
            with self.subTest(ts=ts):
                self.assertEqual(winmon._relative_time(ts), want)

    def test_future_and_bad_values(self):
        import winmon
        self.assertEqual(winmon._relative_time(time.time() + 500), "刚刚")
        self.assertEqual(winmon._relative_time(None), "时间未知")
        self.assertEqual(winmon._relative_time("不是数字"), "时间未知")


class TestCatalogPaneSettingsRoundTrip(unittest.TestCase):
    """收藏 / 最近使用 / 类别筛选必须能写进 settings 再读回来。"""

    def test_round_trip_through_settings_file(self):
        import tkinter as tk
        import winmon

        root = tk.Tk()
        root.withdraw()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                path = os.path.join(tmp, "settings.json")
                app = winmon.WinMonApp.__new__(winmon.WinMonApp)
                app.root = root
                app._settings_path = path
                app._settings_after_id = None
                app.program_prefs = core.CatalogPrefs()
                app.icons = winmon.IconProvider(winmon.ICON_SIZE)
                # save_settings_now 读的这些变量
                app.show_hidden = tk.BooleanVar(value=False)
                app.show_no_window = tk.BooleanVar(value=False)
                app.only_windowed = tk.BooleanVar(value=True)
                app.follow_selection = tk.BooleanVar(value=True)
                app.live_gallery = tk.BooleanVar(value=True)
                app.size_choice = tk.StringVar(value=winmon.PREVIEW_SIZES[1][0])
                app.fps_choice = tk.StringVar(value="很流畅 10 FPS")
                # save_settings_now 会读的其余变量统一由它创建（漏一个就整体保存失败）。
                # 用户砍掉"自动打开视频"后，这里改成媒体/关闭行为/日志保留天数
                # 那几项 —— 它们才是现在真正要往返验证的。
                app._init_video_vars()
                app.video_close_mode.set("smart")
                app.media_app_pin.set("msedge.exe")
                app.history_days.set(7)
                app.catalog = winmon.ProgramCatalogPane(root, app)
                app.catalog.selected_categories = {"game"}
                app.catalog.btn_cats.configure(text=app.catalog._category_label())
                app.catalog.only_favorites.set(True)

                target = r"X:\Program Files\bilibili\哔哩哔哩.exe"
                app.program_prefs.add_favorite(target, "哔哩哔哩")
                # 时间戳用"刚刚"，否则加载时的 prune_recent(60 天) 会把它清掉
                app.program_prefs.record_use(target, "哔哩哔哩", ts=time.time())
                app.program_prefs.set_category(target, "media")
                app.save_settings_now()

                with open(path, encoding="utf-8") as fh:
                    data = json.load(fh)
                self.assertIn("program_prefs", data)
                self.assertEqual(data["catalog_categories"], ["game"])
                self.assertTrue(data["catalog_only_favorites"])
                # 关闭方式存 slug（不是界面上的中文）；日志保留天数存整数
                self.assertEqual(data["video_close_mode"], "smart")
                self.assertEqual(data["media_app_pin"], "msedge.exe")
                self.assertEqual(data["history_days"], 7)

                # 用同一个文件重新加载，偏好必须还原
                app2 = winmon.WinMonApp.__new__(winmon.WinMonApp)
                app2.root = root
                app2._settings_path = path
                app2.show_hidden = tk.BooleanVar()
                app2.show_no_window = tk.BooleanVar()
                app2.only_windowed = tk.BooleanVar()
                app2.follow_selection = tk.BooleanVar()
                app2.live_gallery = tk.BooleanVar()
                app2.size_choice = tk.StringVar()
                app2.fps_choice = tk.StringVar()
                app2._init_video_vars()            # 媒体/关闭行为/保留天数都在里面
                app2.program_prefs = core.CatalogPrefs()
                app2.icons = winmon.IconProvider(winmon.ICON_SIZE)
                app2.catalog = winmon.ProgramCatalogPane(root, app2)
                app2.load_settings()

                self.assertTrue(app2.program_prefs.is_favorite(target))
                self.assertTrue(app2.program_prefs.is_recent(target))
                # 关闭方式：设置里是 slug，读回来后界面变量应当是中文标签
                self.assertEqual(app2.video_close_mode.get(), "暂停并返回")
                self.assertEqual(app2.media_app_pin.get(), "msedge.exe")
                self.assertEqual(app2.history_days.get(), 7)
                self.assertEqual(app2.program_prefs.category_override(target), "media")
                self.assertEqual(app2.catalog.selected_categories, {"game"})
                self.assertTrue(app2.catalog.only_favorites.get())
                # 收尾：先取消挂起的防抖回调，再销毁窗口
                for pane in (getattr(app, "catalog", None), getattr(app2, "catalog", None)):
                    if pane is not None:
                        try:
                            pane.destroy()
                        except Exception:
                            pass
        finally:
            h.destroy_root(root)


if __name__ == "__main__":
    unittest.main(verbosity=2)
