# -*- coding: utf-8 -*-
"""
A5 —— 主窗口：界面尺寸、控件不溢出、安全保护。

对应 HANDOFF.md：
  · 界面固定 1280×800（最小 1080×660），"93 个控件无一横向超出"是程序化校验的结论，
    这里把它变成可重复的测试
  · 坑 13：Treeview 行高必须大于图标高度，否则图标被裁成一条竖条
  · 安全保护：关键系统进程拒绝结束、禁止结束自己（必须真的拦住，不能只写在文档里）
"""

from __future__ import annotations

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import _helpers as h                     # noqa: E402
import winmon_core as core               # noqa: E402

APP_W, APP_H = 1280, 800
MIN_W, MIN_H = 1080, 660

#: 已知可以超出窗口右边缘的控件：Treeview 里的列会横向滚动，
#: 属于设计如此（有水平滚动条），不是布局错误。
KNOWN_OVERFLOW_TOLERANT = ("Treeview", "TScrollbar", "Scrollbar", "Text")


def walk(widget):
    yield widget
    for child in widget.winfo_children():
        yield from walk(child)


@h.needs_tk
class TestMainWindowLayout(unittest.TestCase):
    """
    只构建界面（`_build_ui`），不启动定时器和后台采集线程。

    为什么不用 `WinMonApp(root)`：它会启动 CaptureWorker 线程与一串 after 定时器，
    测试结束时不容易干净收尾（而且会真的去抓屏，干扰其它用例）。
    """

    @classmethod
    def setUpClass(cls):
        import tkinter as tk
        import winmon
        cls.winmon = winmon
        cls.root = tk.Tk()
        cls.root.geometry(f"{APP_W}x{APP_H}+0+0")
        cls.app = winmon.WinMonApp.__new__(winmon.WinMonApp)
        cls.app.root = cls.root
        cls.app._settings_path = os.path.join(
            os.path.dirname(os.path.abspath(winmon.__file__)), "winmon_settings.json")
        cls.app._settings_after_id = None
        # _build_ui 需要的最小状态
        app = cls.app
        app.history = core.ActionHistory()
        app.show_hidden = tk.BooleanVar(value=False)
        app.show_no_window = tk.BooleanVar(value=False)
        app.only_windowed = tk.BooleanVar(value=True)
        app.follow_selection = tk.BooleanVar(value=True)
        app.live_gallery = tk.BooleanVar(value=True)
        app.history_visible = tk.BooleanVar(value=False)
        app.size_choice = tk.StringVar(value=winmon.PREVIEW_SIZES[1][0])
        app.fps_choice = tk.StringVar(value="很流畅 10 FPS")
        app.search_text = tk.StringVar()
        app.status_text = tk.StringVar(value="测试")
        app.overview_text = tk.StringVar(value="")
        # 视频联动 / 媒体 / 闲置 / 关闭行为的所有界面变量统一由它创建
        # （加变量只需改那一处；当时为此踩过两次漏变量的坑）。
        app._init_video_vars()
        app.video_close_mode.set("smart")
        app._build_ui()
        cls.root.update_idletasks()

    @classmethod
    def tearDownClass(cls):
        try:
            cls.root.destroy()
        except Exception:
            pass

    # -- 尺寸 --
    def test_window_geometry(self):
        self.root.update_idletasks()
        self.assertEqual(self.root.winfo_width(), APP_W)
        # 高度可能被窗口管理器微调（任务栏/DPI），给一点余量
        self.assertAlmostEqual(self.root.winfo_height(), APP_H, delta=40)
        self.root.minsize(MIN_W, MIN_H)
        self.assertEqual(self.root.minsize(), (MIN_W, MIN_H))

    def test_widget_count_is_substantial(self):
        """控件数量级不能塌（防止误删整块界面）。"""
        n = len(list(walk(self.root)))
        self.assertGreater(n, 60, f"控件只有 {n} 个，界面可能被破坏")

    # -- 不溢出 --
    #: 允许被压窄的控件类。
    #: · Treeview/Text/Canvas/Scrollbar：有滚动条或按设计填满父容器
    #: · Frame/Labelframe/Panedwindow/Notebook：容器，收缩是布局的正常行为
    #: · 状态栏那个 fill="x" 的标签：它就是用来占满剩余空间的
    SHRINK_OK_CLASSES = ("Treeview", "Text", "Canvas", "TScrollbar", "Scrollbar",
                         "TFrame", "Frame", "TLabelframe", "Labelframe",
                         "TPanedwindow", "Panedwindow", "TNotebook", "Notebook",
                         "TSeparator", "Separator", "Toplevel", "Tk")

    def _squeezed(self, min_deficit=5):
        """
        找出"实际宽度明显小于自身需要宽度"的**交互控件**。

        为什么这条判据是对的：Tk 在窗口变窄时不会把控件推出屏幕，而是压缩它们。
        一个按钮被压到只有请求宽度的一半，显示出来就是「移除」（原为「移除选中总览」）；
        压得更狠（reqwidth=73 却只有 1px）时按钮直接看不见 ——
        而它的右边缘仍在窗口内，所以"越界"类判据完全测不出来。

        实测在 1280 宽下，顶部工具条正是这种情况：
        「移除选中总览」被压成 38px、「加入总览」被压到 1px 而彻底消失。
        """
        self.root.geometry(f"{APP_W}x{APP_H}")
        self.root.update_idletasks()
        self.root.update()
        bad = []
        for w in walk(self.root):
            if not w.winfo_ismapped():
                continue
            try:
                cls = w.winfo_class()
            except Exception:
                continue
            if cls in self.SHRINK_OK_CLASSES:
                continue
            need, got = w.winfo_reqwidth(), w.winfo_width()
            if not need or not got:
                continue
            if need - got >= min_deficit:
                text = ""
                try:
                    text = str(w.cget("text"))[:20]
                except Exception:
                    pass
                bad.append(f"{cls}『{text}』被压窄：需要 {need}px 只有 {got}px")
        return bad

    def _overflow(self):
        return self._squeezed()

    def test_toolbars_do_not_overflow(self):
        """
        顶部两条工具条 + 视频联动两条 + 状态栏，任何控件都不能被挤坏。

        这是 HANDOFF 里"93 个控件无一横向超出（已程序化校验）"的固化版本。
        """
        bad = self._overflow()
        self.assertEqual(bad[:5], [], f"有控件在 {APP_W} 宽度下被挤坏：{bad[:5]}")

    def test_all_tabs_do_not_overflow(self):
        """逐个标签页检查（隐藏的标签页不会创建真实控件树，必须切过去）。"""
        nb = self.app.notebook
        for i in range(len(nb.tabs())):
            with self.subTest(tab=i):
                nb.select(i)
                self.root.update()
                bad = self._overflow()
                self.assertEqual(bad[:3], [],
                                 f"第 {i} 个标签页（{nb.tab(i, 'text')}）有控件被挤坏：{bad[:3]}")

    def test_gallery_drag_is_wired_on_the_whole_tile(self):
        """
        总览墙的拖拽排序：**按下、移动、松手必须绑在同一批控件上**。

        踩过的坑（2026-10-13，真 bug）：`<Button-1>` 绑到了标题/画面/说明行，
        而 `<B1-Motion>` / `<ButtonRelease-1>` 只绑在格子上。Tk 会给"按下"的那个
        控件一个**隐式 grab**，后续移动/松手只发给它 —— 于是从标题、画面或说明行
        按下时拖拽永远不会触发（它们盖满整格，只剩 1~2px 的边框能用，功能等于没有）。
        """
        pane = self.app.gallery
        pane.add(9001, "拖拽测试标题", "说明行")
        try:
            tile = pane.tiles[9001]
            widgets = [tile["cell"]]
            for child in tile["cell"].winfo_children():     # 标题 / 画面 / 说明行
                widgets.append(child)
                widgets.extend(child.winfo_children())      # 画面里那个 Label
            for w in widgets:
                bound = w.bind()
                for seq in ("<Button-1>", "<B1-Motion>", "<ButtonRelease-1>"):
                    self.assertIn(
                        seq, bound,
                        f"{w.winfo_class()} 上没有绑 {seq}（拖拽在它上面就断了）")
        finally:
            pane.remove(9001)          # 别把格子留给同一批里的其它检查

    def test_long_media_title_does_not_squeeze_the_row(self):
        """
        「正在播放」写进**真实长度的曲名**（含多会话那句）之后，这一行也不许压坏别的控件。

        为什么单独测：媒体那一行的标签以前定宽 42 字符、且直接 `set()` 原文，
        长文案会被 Tk 静默切断（2026-10-13 修）；现在走 `_set_media_line()`
        先裁成定宽 + `…`。这条盯住"换成长文案之后布局仍然安全"。
        """
        line = ("Microsoft Edge：有我呢 · 郭一凡 · 正在播放"
                "　｜　还有 2 个在放（网易云音乐：us feat. ゆきな - 澤田空海理）")
        self.app._set_media_line(line)
        self.app.root.update_idletasks()
        bad = self._overflow()
        self.assertEqual(bad[:3], [], f"长曲名把这一行挤坏了：{bad[:3]}")
        shown = self.app.media_text.get()
        self.assertTrue(shown.endswith("…"), f"装不下应当用 … 收尾：{shown!r}")
        self.assertIn("还有 2 个在放", shown)

    def test_window_scales_with_dpi(self):
        """
        **窗口必须跟着 DPI 缩放**（2026-10-13 修的真 bug）。

        本进程是 DPI 感知的（`enable_dpi_awareness()`），150% 缩放下字体与控件
        都比设计稿宽 1.5 倍；窗口若还按 1280×800 物理像素开，就真有控件被挤没 ——
        实测「加入总览」需要 109px 却只剩 17px（媒体那行的说明只剩 14px），
        而 `test_a5` 以前的检查**看不到**：测试进程没开 DPI 感知，字体没被放大。
        这里把"按 DPI 放大窗口"的计算本身钉住（UI 是同一套版式，比例对了就不会挤）。
        """
        self.assertEqual(self.winmon.dpi_scale_from(96), 1.0)
        self.assertAlmostEqual(self.winmon.dpi_scale_from(144), 1.5, places=3)
        self.assertAlmostEqual(self.winmon.dpi_scale_from(120), 1.25, places=3)
        # 读不到 / 明显不合理时按 1.0，绝不把窗口撑到屏幕外
        for bad in (0, -1, None, "x", float("nan")):
            self.assertEqual(self.winmon.dpi_scale_from(bad), 1.0)
        self.assertEqual(self.winmon.dpi_scale_from(10_000), 3.0)

        self.assertEqual(self.winmon.scaled_window_size(1.0), (1280, 800))
        self.assertEqual(self.winmon.scaled_window_size(1.5), (1920, 1200))
        # 屏幕装不下缩放后的尺寸时夹到屏幕内，但**不小于设计稿** ——
        # 比设计稿还小才是真的挤坏，那属于小屏硬约束
        # （与 README「比 1280×800 小的屏幕会被裁」一致）。
        w_small, h_small = self.winmon.scaled_window_size(1.5, screen=(1366, 768))
        self.assertGreaterEqual(w_small, 1280)
        self.assertGreaterEqual(h_small, 800)
        self.assertLessEqual(w_small, 1366)
        self.assertLessEqual(h_small, 800)          # 768 屏放不下设计稿，取设计稿
        small = self.winmon.scaled_window_size(2.0, screen=(2560, 1600))
        self.assertLessEqual(small[1], int(1600 * 0.92))

    def test_history_window_is_separate_and_main_layout_stays_intact(self):
        """
        操作历史现在是**独立窗口**（约定），主界面不再有折叠面板。

        为什么这条仍然要留着检查：把面板从主窗口搬走，正是为了让主窗口的纵向
        空间不再被它挤占 —— 所以搬完之后主界面必须**没有控件被压窄**，
        而且独立窗口自己也要能开起来、能关掉（关掉后主界面不受影响）。
        """
        self.assertFalse(hasattr(self.app, "history_box"),
                         "主界面不该再有「操作历史」折叠面板")
        self.assertFalse(hasattr(self.app, "history_text"))
        bad = self._overflow()
        self.assertEqual(bad[:3], [], f"去掉折叠面板后主界面反而被挤坏：{bad[:3]}")

        # 打开独立历史窗口
        self.app._history_window = None
        self.app._set_status = lambda _t: None
        self.app.action_history()
        win = self.app._history_window
        self.assertIsNotNone(win, "「操作历史…」应当打开独立窗口")
        self.root.update()
        try:
            self.assertTrue(win.top.winfo_exists())
            # 独立窗口自己也不该有被压窄的控件
            win_h, win_w = win.top.winfo_height(), win.top.winfo_width()
            self.assertGreater(win_w, 700, f"历史窗口太窄：{win_w}")
            self.assertGreater(win_h, 300, f"历史窗口太矮：{win_h}")
        finally:
            win.close()
            self.root.update()
        self.assertIsNone(self.app._history_window, "关窗后引用要清掉，否则再点打不开")

    # -- 坑 13：行高 vs 图标 --
    def test_treeview_rowheight_exceeds_icon_size(self):
        """
        坑 13：Treeview 行高小于图标高度会把图标裁成一条竖条。

        直接读实际生效的 style，而不是读源码里的常量。
        """
        from tkinter import ttk
        rowheight = int(ttk.Style().lookup("Treeview", "rowheight"))
        self.assertGreater(rowheight, self.winmon.ICON_SIZE,
                           f"行高 {rowheight} 必须大于图标 {self.winmon.ICON_SIZE}，否则图标被裁")

    def test_icon_columns_are_wide_enough(self):
        """
        图标列宽必须容得下图标本身。

        实测踩过：把图标从 24 提到 32 之后，`#0` 列宽仍是 34 —— 只差 2px，
        看着像"图标没显示"。列宽必须 ≥ ICON_SIZE，且留一点余量。
        """
        tv = self.app.tree
        width = int(tv.column("#0", "width"))
        self.assertGreaterEqual(width, self.winmon.ICON_SIZE,
                                f"窗口列表的图标列宽 {width} < 图标 {self.winmon.ICON_SIZE}")
        cat_w = int(self.app.catalog.tree.column("#0", "width"))
        self.assertGreaterEqual(cat_w, self.winmon.ICON_SIZE,
                                f"程序目录的图标列宽 {cat_w} < 图标 {self.winmon.ICON_SIZE}")

    def test_icon_size_is_reasonable(self):
        self.assertGreaterEqual(self.winmon.ICON_SIZE, 16)
        self.assertLessEqual(self.winmon.ICON_SIZE, 64)

    def test_rowheight_and_icon_size_are_consistent(self):
        """行高与图标尺寸一起决定"一屏能看几行"，两者不能脱节。"""
        from tkinter import ttk
        rowheight = int(ttk.Style().lookup("Treeview", "rowheight"))
        self.assertLessEqual(rowheight - self.winmon.ICON_SIZE, 10,
                             f"行高 {rowheight} 比图标 {self.winmon.ICON_SIZE} 大太多，浪费垂直空间")

    def test_notebook_has_all_tabs_in_order(self):
        """
        标签页的名称与顺序。

        顺序不是随便定的：`_on_tab_changed` 用**下标**分发，
        所以这个断言同时守着"下标与页面必须一一对应"这件事 ——
        插一个页面忘了改 `_on_tab_changed`，就会去刷新错的页面。
        """
        nb = self.app.notebook
        texts = [nb.tab(i, "text") for i in range(len(nb.tabs()))]
        self.assertEqual(texts, ["实时预览", "实时总览", "进程树", "程序目录", "工作区"])

    def test_idle_snooze_choices_fit_in_their_combobox(self):
        """
        下拉框的 `width` 必须容得下**最长的那条**选项，否则显示出来是截断的。

        为什么单独钉这一条：`width` 是"字符数"，而中文一个字符占两个字符宽 ——
        实测 `Microsoft YaHei UI 9` 下 width=12 只有 107px，
        而最长条目「本次运行只提醒一次」要 108px，**差 1px 就会被截**。
        以后往 `IDLE_SNOOZE_CHOICES` 里加更长的选项时，这条会立刻报红。
        """
        from tkinter import font as tkfont
        want = [label for label, _s in core.IDLE_SNOOZE_CHOICES]
        box = None
        for w in walk(self.root):
            if w.winfo_class() != "TCombobox":
                continue
            try:
                if list(w.cget("values")) == want:
                    box = w
                    break
            except Exception:
                continue
        self.assertIsNotNone(box, "没找到闲置「提醒间隔」下拉框")
        f = tkfont.Font(font=self.winmon.UI_FONT)
        widest = max(want, key=f.measure)
        need, got = f.measure(widest), box.winfo_reqwidth()
        self.assertLess(need, got,
                        f"「{widest}」要 {need}px，下拉框只有 {got}px，会被截断")


@h.needs_tk
class TestTerminateSafety(unittest.TestCase):
    """
    安全保护必须真的拦住 —— 用真实的 PID 走一遍 `_terminate_selected`，
    靠"没有真的结束进程"来判定，而不是看它有没有弹窗。
    """

    @classmethod
    def setUpClass(cls):
        import tkinter as tk
        import winmon
        cls.winmon = winmon
        cls.root = tk.Tk()
        cls.root.withdraw()
        cls.app = winmon.WinMonApp.__new__(winmon.WinMonApp)
        cls.app.root = cls.root
        cls.app.selected_pid = None
        cls.app.selected_hwnd = None
        cls.app.win_by_hwnd = {}
        cls.app.proc_map = {}
        cls.app.history = core.ActionHistory()
        cls.app.status_text = tk.StringVar()
        cls.app._set_status = lambda text: cls.app.status_text.set(text)
        cls.app.log_action = lambda *a, **kw: None

    @classmethod
    def tearDownClass(cls):
        try:
            cls.root.destroy()
        except Exception:
            pass

    def test_critical_process_is_blocked(self):
        """
        找一个真的在运行的关键进程，尝试结束后它必须还在。

        选目标时优先挑"能被 OpenProcess 打开"的（例如 csrss.exe）：
        像 "Secure System" 这类由内核代管的伪进程打不开句柄，
        用 is_process_alive 判定会得到"看起来还活着"的无效结论。
        """
        procs = core.snapshot_processes()
        victim = None
        for pid, info in procs.items():
            if info["name"].lower() in core.CRITICAL_PROCESSES and pid > 4:
                hh = core.kernel32.OpenProcess(core.PROCESS_QUERY_LIMITED_INFORMATION,
                                              False, pid)
                if hh:
                    core.kernel32.CloseHandle(hh)
                    victim = (pid, info["name"])
                    break
        if not victim:
            self.skipTest("当前没有找到可测试的关键系统进程")
        pid, name = victim
        self.app.selected_pid = pid
        self.app.proc_map = procs
        with mock.patch.object(self.winmon.messagebox, "showerror") as err, \
                mock.patch.object(self.winmon.messagebox, "showwarning") as warn:
            self.app._terminate_selected(tree=False)
        self.assertGreaterEqual(err.call_count + warn.call_count, 1,
                                "结束关键系统进程时必须给出提示并阻止")
        self.assertIn("阻止", self.app.status_text.get())
        if name.lower() == "csrss.exe":
            self.assertTrue(h.wait_alive(pid, 1.0),
                            f"关键系统进程 {name}({pid}) 居然被结束了")

    def test_cannot_terminate_self(self):
        me = os.getpid()
        self.app.selected_pid = me
        self.app.proc_map = {me: {"name": "winmon-test.exe", "ppid": 0}}
        with mock.patch.object(self.winmon.messagebox, "showwarning") as warn:
            self.app._terminate_selected(tree=False)
        self.assertEqual(warn.call_count, 1, "结束自己时必须弹警告并阻止")
        self.assertTrue(core.is_process_alive(me), "测试进程把自己结束了？")

    def test_no_selection_is_handled(self):
        self.app.selected_pid = None
        self.app._terminate_selected(tree=False)
        self.assertIn("请选择", self.app.status_text.get())

    def test_confirm_dialog_declined_does_not_kill(self):
        """用户在确认框里选"否"，进程必须活着。"""
        proc = h.spawn_cleanup(self, h.sleep_child())
        self.assertTrue(h.wait_alive(proc.pid))
        self.app.selected_pid = proc.pid
        self.app.proc_map = core.snapshot_processes()
        with mock.patch.object(self.winmon.messagebox, "askyesno",
                               return_value=False) as ask:
            self.app._terminate_selected(tree=False)
        self.assertEqual(ask.call_count, 1, "结束普通进程前必须弹确认框")
        self.assertTrue(core.is_process_alive(proc.pid), "用户选了「否」却把进程结束了")

    def test_confirmation_text_shows_identity(self):
        """确认框里必须写清是哪个程序（避免误杀）。"""
        proc = h.spawn_cleanup(self, h.sleep_child())
        self.assertTrue(h.wait_alive(proc.pid))
        self.app.selected_pid = proc.pid
        self.app.proc_map = {proc.pid: {"name": "very-unique-test-name.exe",
                                        "ppid": 0}}
        with mock.patch.object(self.winmon.messagebox, "askyesno",
                               return_value=False) as ask:
            self.app._terminate_selected(tree=False)
        msg = ask.call_args[0][1]
        self.assertIn("very-unique-test-name.exe", msg)
        self.assertIn(str(proc.pid), msg)
        self.assertIn("未保存", msg, "确认框应当提示数据会丢失")

    def test_tree_confirmation_mentions_scope(self):
        proc = h.spawn_cleanup(self, h.sleep_child())
        self.assertTrue(h.wait_alive(proc.pid))
        self.app.selected_pid = proc.pid
        self.app.proc_map = {proc.pid: {"name": "x.exe", "ppid": 0}}
        with mock.patch.object(self.winmon.messagebox, "askyesno",
                               return_value=False) as ask:
            self.app._terminate_selected(tree=True)
        self.assertIn("全部子进程", ask.call_args[0][1])


if __name__ == "__main__":
    unittest.main(verbosity=2)
