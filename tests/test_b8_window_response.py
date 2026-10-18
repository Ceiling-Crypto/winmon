# -*- coding: utf-8 -*-
"""
B8 —— 左栏列表的窗口响应（`test_b8`）。

需求（用户确认过的语义）：

    · **单击**一行 → 只负责选中（**刻意什么都不做**：第一版会把窗口抢到最前，
                      结果"点一行看看画面"也会把正在干活的窗口赶到后面去；
                      2026-10-16 复查补丁顺手删掉了那个空壳 `_on_list_click`）
    · **双击**一行 → 已最小化就唤醒还原，否则最小化它（"切换"）
    · **中键**一行 → 切到最前（和浏览器中键一样形成肌肉记忆）
    · 列表里按 **Enter** → 把选中的窗口切到最前

分三层写，和项目里其它测试同样的取舍：

  1. **纯逻辑**：`plan_window_click` 的决策表逐条核对。窗口操作真去抢前台、
     真去最小化的话，测试机会被搅得一团乱，而且结果依赖"当时谁在前台" ——
     所以"该做什么"与"怎么做"是分开的，这一层把前者钉死。
  2. **事件接线**：用一个只有几样属性的假 app 真的驱动 `_on_list_double_click` /
     `_on_list_middle_click`，核对它们**排进队列的动作**。这一层能真的报红：
     比如把双击的判定改成"第二次按下时才取状态"，下面就会失败
     （那正是"双击永远只会最小化"的那个 bug）。
  3. **真实窗口**：真开一个别的进程的 Tk 窗口，真的最小化 / 还原，并**回读系统状态**
     （`IsIconic` / `GetForegroundWindow`）核对，再加一段**循环稳定性**测试。

关于 skip：最小化 / 还原**不依赖前台锁**，所以这两条是硬断言（做不到就是 bug）；
只有"能不能真的抢到前台"取决于会话 —— 本环境（非交互式 shell）可能被 Windows 拒绝，
那种情况 skip，但**"声称成功却并没有前台"一律判失败**（假成功是最坏的一种错）。
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import _helpers as h                                          # noqa: E402
import winmon_core as core                                    # noqa: E402
import winmon                                                 # noqa: E402

#: 靶子窗口：一个普通的 Tk 窗口，可以被最小化 / 还原。
TARGET_WINDOW_SCRIPT = r'''
# -*- coding: utf-8 -*-
"""靶子：普通 Tk 窗口，用来验证"最小化 / 唤醒到最前"。"""
import sys
import tkinter as tk

title = sys.argv[1] if len(sys.argv) > 1 else "WinMon-WinOp-Target"
root = tk.Tk()
root.title(title)
root.geometry("420x300+220+200")
tk.Label(root, text="window state target").pack(pady=40)
root.update()
root.mainloop()
'''

#: 稳定性循环的次数。每次都是"最小化 → 唤醒还原"一个来回。
STABILITY_ROUNDS = 10


class TestPlanWindowClick(unittest.TestCase):
    """手势决策表 —— 这一层不需要窗口，任何环境都必须跑。"""

    def test_single_click_does_nothing(self):
        """
        **单击刻意不碰任何窗口**（用户 2026-10-11 定的）。

        以前单击会把窗口抢到最前，结果你只是想点一行看看画面，
        也会把正在干活的窗口赶到后面去。所以单击根本不是一个"手势"。
        """
        for iconic in (True, False):
            with self.subTest(iconic=iconic):
                self.assertEqual(winmon.plan_window_click("single", iconic),
                                 winmon.CLICK_NONE)

    def test_foreground_gesture_activates(self):
        """Enter / 中键 / 右键菜单都走这一支：切到最前。"""
        for iconic in (True, False):
            with self.subTest(iconic=iconic):
                self.assertEqual(winmon.plan_window_click("foreground", iconic),
                                 winmon.CLICK_ACTIVATE)

    def test_double_click_on_a_visible_window_minimizes_it(self):
        """**这条是"双击只会唤醒"那个 bug 的回归判据**：双击必须能最小化。"""
        self.assertEqual(winmon.plan_window_click("double", False),
                         winmon.CLICK_MINIMIZE)

    def test_double_click_on_a_minimized_window_restores_it(self):
        """已最小化的窗口双击是"唤醒还原"，不是"再最小化一次"。"""
        self.assertEqual(winmon.plan_window_click("double", True),
                         winmon.CLICK_ACTIVATE)

    def test_double_click_toggles(self):
        """来回两次必须落到两个不同动作（否则就是"只会一边"）。"""
        first = winmon.plan_window_click("double", False)
        second = winmon.plan_window_click("double", True)
        self.assertEqual({first, second},
                         {winmon.CLICK_MINIMIZE, winmon.CLICK_ACTIVATE})

    def test_decision_does_not_depend_on_foreground_state(self):
        """
        决策**只**由"窗口自己是否最小化"决定。

        第一版还带一个 `already_active` 参数（"按下之前它是不是最前面那个"），
        结果是：人已经在 WinMon 里了，双击哪一行它都不是前台 → 那一支永远走不到，
        用户看到的就是"双击只会唤醒、从来不会最小化"。这里把签名钉住。
        """
        import inspect
        params = list(inspect.signature(winmon.plan_window_click).parameters)
        self.assertEqual(params, ["gesture", "iconic"],
                         "决策函数不该再依赖「谁在前台」这类外部状态")

    def test_unknown_gesture_does_nothing(self):
        for gesture in ("", "middle", "triple"):
            with self.subTest(gesture=gesture):
                self.assertEqual(winmon.plan_window_click(gesture, False),
                                 winmon.CLICK_NONE)

    def test_actions_are_distinct_constants(self):
        self.assertEqual(len({winmon.CLICK_ACTIVATE, winmon.CLICK_MINIMIZE,
                              winmon.CLICK_NONE}), 3)


class _FakeTree:
    """只实现被用到的两个方法。"""

    def __init__(self, item: str, region: str = "cell"):
        self._item = item
        self._region = region

    def identify_region(self, _x, _y):
        return self._region

    def identify_row(self, _y):
        return self._item


class _FakeWindow:
    """`plan_window_click` 只认句柄；`iconic` 由 `_window_is_iconic` 这个接缝决定。"""

    def __init__(self, hwnd: int, iconic: bool = False):
        self.hwnd = hwnd
        self.iconic = iconic          # 故意留着：产品代码**不该**再读这个缓存值
        self.title = f"fake-{hwnd}"
        self.pid = 4242


class _ClickHarness(winmon.WinMonApp):
    """
    只装"列表点击"需要的几样东西，真的去跑 `WinMonApp` 的那两个事件处理函数。

    刻意**继承** `WinMonApp` 而不是复制一份逻辑：这样测的就是产品代码本身
    （复制一份的话，产品代码改了测试还在测旧逻辑）。
    用 `__new__` 跳过 `__init__`（它要开 Tk 窗口）。
    """

    def __init__(self, row_kind: str, hwnd: int, iconic: bool = False,
                 region: str = "cell", cached_iconic: bool = None):
        item = "row1"
        #: 列表里的**缓存快照**：故意允许和"实时状态"不一致，
        #: 用来验证产品代码读的是实时状态而不是这个快照
        cached = iconic if cached_iconic is None else cached_iconic
        self.win_by_hwnd = {hwnd: _FakeWindow(hwnd, cached)}
        self.row_info = {item: (row_kind, 4242, hwnd if row_kind == "window" else 0)}
        self.tree = _FakeTree(item, region)
        self._live_iconic = iconic
        self.ops: list[tuple[str, int]] = []
        self.status: list[str] = []

    # ---- 把有副作用的方法换成记录器 ----
    def _window_is_iconic(self, hwnd: int) -> bool:
        """替掉"实时问系统"那一层，让判定可控（真实实现由真窗口测试覆盖）。"""
        return bool(self._live_iconic)

    def _queue_window_op(self, action: str, hwnd: int) -> None:
        self.ops.append((action, hwnd))

    def _set_status(self, text: str) -> None:
        self.status.append(text)

    # ---- 驱动 ----
    def click(self, item: str = "row1", event=None):
        """
        单击 —— 产品里**连处理函数都没有**（`_on_list_click` 从第一版起就是个空实现、
        也从来没被 `bind` 过，2026-10-16 复查补丁把它删了）。

        所以这里只按 Tk 自己的行为理解单击：让那行被选中，**不排任何窗口操作**。
        """
        return self.ops

    def double_click(self, event=None):
        ev = event or _FakeEvent()
        self._on_list_double_click(ev)
        return self.ops

    def middle_click(self, event=None):
        ev = event or _FakeEvent()
        self._on_list_middle_click(ev)
        return self.ops


class _FakeEvent:
    """`identify_region/identify_row` 在假 tree 里不看坐标，随便给两个数即可。"""

    x = 10
    y = 10


class TestClickWiring(unittest.TestCase):
    """事件处理函数真的把正确的动作排进队列（这一层能报红）。"""

    HWND = 0x1234

    def test_single_click_touches_nothing(self):
        """
        单击只负责选中（Tk 默认行为），一个窗口操作都不该被排进队列。

        2026-10-16 复查补丁删掉了那个空实现 `_on_list_click`，所以这条测试现在钉的是
        两件事：**那个处理函数不许再被加回来** + 单击之后队列一定是空的。
        "接线"那一层由 `test_bindings_are_actually_registered` 守着 ——
        它直接断言列表上没有 `<Button-1>` 绑定（单击根本不进 WinMon 的代码）。
        """
        self.assertFalse(
            hasattr(winmon.WinMonApp, "_on_list_click"),
            "空的 _on_list_click 又被加回来了：单击不该有任何处理函数"
            "（以前它会抢前台，用户点一行看看画面也会把干活的窗口赶到后面）")
        app = _ClickHarness("window", self.HWND)
        self.assertEqual(app.click(), [])

    def test_middle_click_activates(self):
        app = _ClickHarness("window", self.HWND)
        self.assertEqual(app.middle_click(), [(winmon.CLICK_ACTIVATE, self.HWND)])

    def test_double_click_on_a_visible_window_minimizes(self):
        """本次 bug 的回归：可见的窗口被双击必须**最小化**，不能只是又唤醒一次。"""
        app = _ClickHarness("window", self.HWND, iconic=False)
        ops = app.double_click()
        self.assertIn((winmon.CLICK_MINIMIZE, self.HWND), ops,
                      f"双击可见窗口没有最小化：{ops}")
        self.assertEqual(ops[-1], (winmon.CLICK_MINIMIZE, self.HWND))

    def test_double_click_on_a_minimized_window_restores(self):
        app = _ClickHarness("window", self.HWND, iconic=True)
        ops = app.double_click()
        self.assertEqual(ops[-1], (winmon.CLICK_ACTIVATE, self.HWND))

    def test_double_click_toggles_back_and_forth(self):
        """连做两次双击：先最小化、再还原 —— 这是"切换"该有的样子。"""
        app = _ClickHarness("window", self.HWND, iconic=False)
        app.double_click()
        self.assertEqual(app.ops[-1], (winmon.CLICK_MINIMIZE, self.HWND),
                         "第一次双击可见窗口应当最小化")
        app._live_iconic = True                  # 上一步已经把它最小化了
        app.double_click()
        self.assertEqual(app.ops[-1], (winmon.CLICK_ACTIVATE, self.HWND),
                         "第二次双击应当还原，而不是再最小化一次")

    def test_decision_reads_live_state_not_the_list_snapshot(self):
        """
        判定必须问**实时状态**，不能读列表里那份最多 2 秒前的快照。

        这里故意让两者矛盾（快照说"可见"、实时说"已最小化"）：
        正确实现应当按实时状态走 → 还原。用快照就会**再最小化一次**，
        表现就是"双击切换失灵，怎么点都是最小化"。
        """
        app = _ClickHarness("window", self.HWND, iconic=True, cached_iconic=False)
        ops = app.double_click()
        self.assertEqual(ops[-1], (winmon.CLICK_ACTIVATE, self.HWND),
                         "读了列表快照而不是实时状态")

    def test_group_row_is_ignored(self):
        """分组行（按应用分组时的父行）没有窗口可切，必须原样返回。"""
        app = _ClickHarness("group", self.HWND)
        self.assertEqual(app.double_click(), [])
        self.assertEqual(app.click(), [])

    def test_nowin_row_is_ignored(self):
        app = _ClickHarness("nowin", self.HWND)
        self.assertEqual(app.double_click(), [])

    def test_heading_region_is_ignored(self):
        """点表头（分隔线 / 列标题）不该切任何窗口。"""
        app = _ClickHarness("window", self.HWND, region="heading")
        self.assertEqual(app.click(), [])
        self.assertEqual(app.double_click(), [])

    def test_unknown_hwnd_row_is_ignored(self):
        """行还在但窗口已经没了（列表还没刷新）→ 不能崩，也不能乱排队。"""
        app = _ClickHarness("window", self.HWND)
        app.win_by_hwnd = {}
        self.assertEqual(app.click(), [])
        self.assertEqual(app.double_click(), [])


class TestWindowOpQueue(unittest.TestCase):
    """
    后台队列这一段接线（真起线程、真调 `core`、结论真回到状态栏）。

    为什么要后台：激活要"附加输入队列 + 等待 + 回读 + 可能重试"，几百毫秒，
    放 Tk 主线程会让每次点击都顿一下。
    结果**必须经队列交回主线程**（不能在工作线程里直接碰 tkinter）。
    """

    def _app(self):
        import queue as queue_mod

        app = winmon.WinMonApp.__new__(winmon.WinMonApp)
        app._win_op_q = queue_mod.Queue()
        app._win_op_msg = queue_mod.Queue()
        app._win_op_thread = None
        app.status = []
        app._set_status = app.status.append
        return app

    def _wait_for(self, cond, timeout: float = 3.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if cond():
                return True
            time.sleep(0.02)
        return False

    def _drain(self, app, times: int = 6) -> None:
        """模拟主线程的泵：反复把结果队列抽干。"""
        for _ in range(times):
            app._drain_window_op_msgs()
            if app.status:
                return
            time.sleep(0.05)

    def test_activate_request_reaches_core_and_reports(self):
        app = self._app()
        with mock.patch.object(core, "activate_window",
                               return_value=(True, "已切换到前台：X")) as m:
            app._queue_window_op(winmon.CLICK_ACTIVATE, 0x1234)
            self.assertTrue(self._wait_for(lambda: m.called), "请求没到 core")
            self._drain(app)
            m.assert_called_once_with(0x1234)
        self.assertIn("已切换到前台", app.status[-1])

    def test_minimize_request_goes_to_minimize(self):
        app = self._app()
        with mock.patch.object(core, "minimize_window",
                               return_value=(True, "已最小化：X")) as m, \
                mock.patch.object(core, "activate_window") as m2:
            app._queue_window_op(winmon.CLICK_MINIMIZE, 0x2222)
            self.assertTrue(self._wait_for(lambda: m.called))
            self._drain(app)
            m.assert_called_once_with(0x2222)
        self.assertFalse(m2.called, "最小化请求不该去调激活")

    def test_failure_is_reported_verbatim(self):
        """失败要如实回报，不能吞掉 —— 静默失败会让人以为"点了没反应"是正常的。"""
        app = self._app()
        with mock.patch.object(core, "activate_window",
                               return_value=(False, "前台锁定拒绝了")):
            app._queue_window_op(winmon.CLICK_ACTIVATE, 0x3333)
            self.assertTrue(self._wait_for(lambda: app._win_op_msg.qsize() > 0))
            self._drain(app)
        self.assertIn("前台锁定", app.status[-1])

    def test_exception_in_core_does_not_escape_the_worker(self):
        app = self._app()
        with mock.patch.object(core, "activate_window", side_effect=OSError("boom")):
            app._queue_window_op(winmon.CLICK_ACTIVATE, 0x4444)
            self.assertTrue(self._wait_for(lambda: app._win_op_msg.qsize() > 0))
            self._drain(app)
        self.assertIn("boom", app.status[-1])

    def test_worker_never_touches_tk(self):
        """
        工作线程里**不许**碰 tkinter。

        第一版是 `self.root.after(0, _apply)`，有两个问题：
        ① 从非主线程调 tkinter 本身不安全；② 那次调用一旦抛异常就 `return`，
        把排在后面的窗口操作**整队丢掉**（表现是"点了没反应"，且无法察觉）。
        """
        app = self._app()
        app.root = None                       # 只要工作线程敢碰 root 就会 AttributeError
        with mock.patch.object(core, "activate_window",
                               return_value=(True, "已切换到前台：X")):
            app._queue_window_op(winmon.CLICK_ACTIVATE, 0x5555)
            self.assertTrue(self._wait_for(lambda: app._win_op_msg.qsize() > 0),
                            "工作线程没能完成操作（可能去碰了 tkinter）")
        self.assertEqual(app.status, [], "工作线程不该直接写状态栏")

    def test_queue_keeps_only_the_newest_request(self):
        """连点几下列表时，中间那些请求已经没有意义了。"""
        app = self._app()
        seen = []
        with mock.patch.object(core, "activate_window",
                               side_effect=lambda hwnd: (seen.append(hwnd), (True, "ok"))[1]):
            for hwnd in range(0x5000, 0x5008):
                app._queue_window_op(winmon.CLICK_ACTIVATE, hwnd)
            # 等**最新的那个**真的被执行 —— 只等"有动静"会撞上竞态
            # （线程可能刚取走第一个请求，后面的合并还没发生）。
            self.assertTrue(self._wait_for(lambda: seen and seen[-1] == 0x5007),
                            f"最新的请求没有被执行：{seen}")
        self.assertLess(len(seen), 8, f"队列没有合并，执行了 {len(seen)} 次")

    def test_a_failed_op_does_not_drop_the_next_one(self):
        """
        一次失败不能把后面的请求带走 —— 这正是第一版 `return` 造成的缺陷。
        """
        app = self._app()
        calls = []
        with mock.patch.object(core, "activate_window",
                               side_effect=lambda hwnd: (calls.append(hwnd), (False, "拒绝了"))[1]):
            app._queue_window_op(winmon.CLICK_ACTIVATE, 0x6001)
            self.assertTrue(self._wait_for(lambda: 0x6001 in calls))
            app._queue_window_op(winmon.CLICK_ACTIVATE, 0x6002)
            self.assertTrue(self._wait_for(lambda: 0x6002 in calls),
                            f"第二个请求被丢掉了：{calls}")


@h.needs_tk
class TestRowWindowAtRealTree(unittest.TestCase):
    """
    `_row_window_at` 对着**真的 ttk.Treeview** 跑一遍。

    为什么要单独写：`identify_region()` 返回的字符串（"tree" / "cell" / "heading" /
    "nothing"）在不同 Tk 版本、不同主题下不完全一样，只靠假 tree 是测不出来的 ——
    真出现"点表头也去切窗口"或"点行没反应"，就出在这里。
    """

    def setUp(self):
        import tkinter as tk
        from tkinter import ttk

        self.root = tk.Tk()
        self.root.geometry("420x260+80+80")
        self.addCleanup(self.root.destroy)
        self.tv = ttk.Treeview(self.root, columns=("a",), show="tree headings", height=6)
        self.tv.heading("#0", text="窗口")
        self.tv.pack(fill="both", expand=True)
        self.tv.insert("", "end", iid="grp", text="分组行")
        self.tv.insert("grp", "end", iid="win", text="窗口行")
        self.tv.insert("", "end", iid="nowin", text="无窗口进程行")
        self.tv.item("grp", open=True)     # 分组默认是收起的，展开才有可见区域
        self.root.update()

    def _app(self) -> winmon.WinMonApp:
        app = winmon.WinMonApp.__new__(winmon.WinMonApp)
        app.tree = self.tv
        app.row_info = {"grp": ("group", 11, 0),
                        "win": ("window", 22, 0x1234),
                        "nowin": ("nowin", 33, 0)}
        app.win_by_hwnd = {0x1234: _FakeWindow(0x1234)}
        return app

    def _event_at(self, item: str):
        box = self.tv.bbox(item)
        self.assertTrue(box, f"行 {item} 没有可见区域，测试环境不支持（bbox 为空）")
        return type("E", (), {"x": box[0] + max(1, box[2] // 2),
                              "y": box[1] + max(1, box[3] // 2)})()

    def test_window_row_resolves(self):
        item, wi = self._app()._row_window_at(self._event_at("win"))
        self.assertEqual(item, "win")
        self.assertIsNotNone(wi)
        self.assertEqual(wi.hwnd, 0x1234)

    def test_bindings_are_actually_registered(self):
        """
        光有处理函数不够 —— `WinMonApp` 里必须真的把它们绑到列表上。

        第一版的毛病是"决策不对"，但假如哪天有人漏了 `bind`，
        表现会是"点了完全没反应"，比现在更难查。这里对着真 Treeview 核对一次。
        """
        app = winmon.WinMonApp.__new__(winmon.WinMonApp)
        app.tree = self.tv
        app._on_list_double_click = lambda _e: None
        app._on_list_middle_click = lambda _e: None
        app._on_list_enter_key = lambda _e=None: None
        app._on_select = lambda _e=None: None
        app._on_right_click = lambda _e: None
        app.action_terminate = lambda: None
        app._on_tab_changed = lambda: None
        app.notebook = self.root                      # 只需要一个能 bind 的真控件
        winmon.WinMonApp._bind_events(app)        # 只跑绑定那一步
        self.assertFalse(self.tv.bind("<Button-1>"),
                         "单击不该绑任何处理函数（单击只负责选中，刻意不碰窗口）")
        self.assertTrue(self.tv.bind("<Double-1>"), "列表没有绑定双击")
        self.assertTrue(self.tv.bind("<Button-2>"), "列表没有绑定中键")
        self.assertTrue(self.tv.bind("<Return>"), "列表没有绑定回车（切到最前）")
        self.assertTrue(self.tv.bind("<<TreeviewSelect>>"), "列表没有绑定选中")

    def test_group_row_is_not_a_window_row(self):
        item, wi = self._app()._row_window_at(self._event_at("grp"))
        self.assertIsNone(item)
        self.assertIsNone(wi)

    def test_nowin_row_is_not_a_window_row(self):
        item, wi = self._app()._row_window_at(self._event_at("nowin"))
        self.assertIsNone(item)
        self.assertIsNone(wi)

    def test_heading_area_is_ignored(self):
        app = self._app()
        first = self.tv.bbox("grp")
        self.assertTrue(first)
        above = min(3, max(0, first[1] - 1))     # 表头/列标题所在的那条
        ev = type("E", (), {"x": 20, "y": above})()
        self.assertNotIn(self.tv.identify_region(ev.x, ev.y), ("tree", "cell"))
        self.assertEqual(app._row_window_at(ev), (None, None))


class _BatchHarness(winmon.WinMonApp):
    """
    只装批量操作需要的几样东西（`__new__` 跳过要开窗口的 `__init__`）。

    真去最小化一批窗口会把测试机的桌面搅乱，所以这里只核对**排进队列的动作**；
    "排进去的动作真的能最小化/还原"由 `TestWindowStateReal` 与端到端用例覆盖。
    """

    def __init__(self, rows: list[tuple]):
        """rows: `[(item_id, kind, hwnd, iconic), ...]`"""
        self.row_info = {}
        self.win_by_hwnd = {}
        self._live: dict[int, bool] = {}
        sel = []
        for item, kind, hwnd, ic in rows:
            self.row_info[item] = (kind, 1000 + len(sel), hwnd)
            if kind == "window" and hwnd:
                self.win_by_hwnd[hwnd] = _FakeWindow(hwnd, ic)
                self._live[hwnd] = bool(ic)
            sel.append(item)
        self.tree = _BatchHarness._Sel(sel)
        self.ops: list[tuple] = []
        self.status: list[str] = []

    class _Sel:
        def __init__(self, sel):
            self._sel = list(sel)

        def selection(self):
            return tuple(self._sel)

    def _window_is_iconic(self, hwnd: int) -> bool:
        return bool(self._live.get(hwnd))

    def _queue_window_op(self, action: str, hwnd: int, coalesce: bool = True) -> None:
        self.ops.append((action, hwnd, coalesce))

    def _set_status(self, text: str) -> None:
        self.status.append(text)


class TestBatchWindowAction(unittest.TestCase):
    """多选批量：一次处理多个窗口（"一键收拾桌面"）。"""

    def test_minimize_selected_covers_every_selected_window(self):
        app = _BatchHarness([("a", "window", 0x11, False),
                             ("b", "window", 0x22, False),
                             ("c", "window", 0x33, False)])
        app.action_minimize_selected()
        self.assertEqual([(a, h) for a, h, _c in app.ops],
                         [(winmon.CLICK_MINIMIZE, 0x11),
                          (winmon.CLICK_MINIMIZE, 0x22),
                          (winmon.CLICK_MINIMIZE, 0x33)])

    def test_batch_requests_are_not_coalesced(self):
        """
        **批量操作必须 `coalesce=False`。**

        队列默认"只保留最新一个请求"（连点列表时中间那些没意义），
        但批量操作要是也走合并，**"最小化选中的 5 个"会只处理最后一个** ——
        这是个很隐蔽的 bug：用户看到 4 个窗口纹丝不动，却没有任何报错。
        """
        app = _BatchHarness([("a", "window", 0x11, False),
                             ("b", "window", 0x22, False),
                             ("c", "window", 0x33, False)])
        app.action_minimize_selected()
        self.assertEqual([c for _a, _h, c in app.ops], [False, False, False],
                         "批量请求被合并了，会有窗口被漏掉")

    def test_restore_selected_activates_all(self):
        app = _BatchHarness([("a", "window", 0x11, True),
                             ("b", "window", 0x22, True)])
        app.action_restore_selected()
        self.assertEqual([a for a, _h, _c in app.ops],
                         [winmon.CLICK_ACTIVATE, winmon.CLICK_ACTIVATE])

    def test_toggle_decides_per_window(self):
        """`toggle` 要逐个判定：可见的最小化、已最小化的还原。"""
        app = _BatchHarness([("a", "window", 0x11, False),
                             ("b", "window", 0x22, True),
                             ("c", "window", 0x33, False)])
        app.action_toggle_selected()
        self.assertEqual([(a, h) for a, h, _c in app.ops],
                         [(winmon.CLICK_MINIMIZE, 0x11),
                          (winmon.CLICK_ACTIVATE, 0x22),
                          (winmon.CLICK_MINIMIZE, 0x33)])

    def test_non_window_rows_are_skipped(self):
        """分组行与"无窗口进程"行没有窗口可切，多选里必须被跳过。"""
        app = _BatchHarness([("g", "group", 0, False),
                             ("a", "window", 0x11, False),
                             ("n", "nowin", 0, False)])
        app.action_minimize_selected()
        self.assertEqual([(a, h) for a, h, _c in app.ops],
                         [(winmon.CLICK_MINIMIZE, 0x11)])

    def test_empty_selection_only_hints(self):
        app = _BatchHarness([])
        app.action_toggle_selected()
        self.assertEqual(app.ops, [])
        self.assertTrue(app.status, "没选中任何窗口时应当在状态栏说明")

    def test_selection_of_numbered_items_is_lenient(self):
        """Tk 的 iid 是字符串；这里用真实样子的 iid 再跑一遍。"""
        app = _BatchHarness([("I001", "window", 0x11, False),
                             ("I002", "window", 0x22, True)])
        app.action_toggle_selected()
        self.assertEqual([a for a, _h, _c in app.ops],
                         [winmon.CLICK_MINIMIZE, winmon.CLICK_ACTIVATE])


class TestActivityMarkers(unittest.TestCase):
    """「活动」列的状态标记（状态提示要一眼看得见，而不是只靠行颜色）。"""

    @staticmethod
    def _wi(iconic=False, top=False):
        wi = core.WinInfo(0x1234, 4242, "标题", "Cls", 800, 600, iconic, False, False)
        wi.always_on_top = top
        return wi

    def test_foreground_marker(self):
        self.assertEqual(winmon.act_text(self._wi(), True), "●")

    def test_minimized_marker(self):
        self.assertEqual(winmon.act_text(self._wi(iconic=True), False), "▁")

    def test_topmost_marker(self):
        self.assertEqual(winmon.act_text(self._wi(top=True), False), "顶")

    def test_plain_window_has_no_marker(self):
        self.assertEqual(winmon.act_text(self._wi(), False), "")

    def test_minimized_and_topmost_can_coexist(self):
        """
        这正是"只靠行颜色"做不到的事：`_insert_window_row` 的 tag 是 if/elif 互斥的，
        而"最小化 + 置顶"是真会同时出现的组合，只能靠这一列的多个符号表达。
        """
        self.assertEqual(winmon.act_text(self._wi(iconic=True, top=True), False), "▁顶")
        self.assertEqual(winmon.act_text(self._wi(iconic=True, top=True), True), "●▁顶")


@h.needs_tk
class TestRowTip(unittest.TestCase):
    """悬停提示：内容对不对、能不能正常收起、**绝不抢焦点**。"""

    def setUp(self):
        import tkinter as tk
        from tkinter import ttk

        self.root = tk.Tk()
        self.root.geometry("420x260+80+80")
        self.addCleanup(self.root.destroy)
        self.tv = ttk.Treeview(self.root, show="tree", height=6)
        self.tv.pack(fill="both", expand=True)
        self.tv.insert("", "end", iid="a", text="第一行")
        self.tv.insert("", "end", iid="b", text="第二行")
        self.root.update()

    def test_shows_the_given_text(self):
        tip = winmon._RowTip(self.tv, lambda row: f"提示：{row}")
        self.addCleanup(tip.destroy)
        tip._row = "a"
        tip._show("a")
        self.assertIsNotNone(tip.top)
        self.assertEqual(tip.label.cget("text"), "提示：a")
        self.assertTrue(tip.top.winfo_ismapped(), "提示条应当显示出来")

    def test_hide_withdraws_it(self):
        tip = winmon._RowTip(self.tv, lambda row: "X")
        self.addCleanup(tip.destroy)
        tip._row = "a"
        tip._show("a")
        tip.hide()
        self.assertFalse(tip.top.winfo_ismapped(), "收起之后不该还挂在屏幕上")

    def test_empty_text_means_no_tip(self):
        """分组行 / 无窗口行的 `text_for` 返回空串，就不该弹东西。"""
        tip = winmon._RowTip(self.tv, lambda row: "")
        self.addCleanup(tip.destroy)
        tip._row = "a"
        tip._show("a")
        self.assertTrue(tip.top is None or not tip.top.winfo_ismapped())

    def test_never_steals_focus(self):
        """
        提示条**绝不能**抢焦点：一抢，"双击最小化"这类手势就会被打断。

        判据：弹出前后前台窗口不变（本进程里用 `focus_get` 看 Tk 侧的焦点归属）。
        """
        tip = winmon._RowTip(self.tv, lambda row: "提示")
        self.addCleanup(tip.destroy)
        before = self.root.focus_get()
        tip._row = "a"
        tip._show("a")
        self.root.update()
        self.assertEqual(self.root.focus_get(), before, "提示条把焦点抢走了")

    def test_motion_binding_is_installed(self):
        tip = winmon._RowTip(self.tv, lambda row: "x")
        self.addCleanup(tip.destroy)
        self.assertTrue(self.tv.bind("<Motion>"), "没有绑定鼠标移动")
        self.assertTrue(self.tv.bind("<Leave>"), "没有绑定鼠标离开")

    def test_delayed_show_is_cancelled_on_hide(self):
        tip = winmon._RowTip(self.tv, lambda row: "x")
        self.addCleanup(tip.destroy)
        tip._after_id = self.tv.after(5000, lambda: None)
        tip.hide()
        self.assertIsNone(tip._after_id, "延迟弹出没有被取消")


class TestRowTipText(unittest.TestCase):
    """提示内容：窗口行才有，且要把"闲置计时"写出来。"""

    def _app(self, idle_enabled=False, idle_seconds=0.0):
        app = winmon.WinMonApp.__new__(winmon.WinMonApp)
        wi = core.WinInfo(0x11, 4242, "某窗口标题", "Cls", 800, 600, False, False, False)
        app.win_by_hwnd = {0x11: wi}
        app.row_info = {"win": ("window", 4242, 0x11),
                        "grp": ("group", 4242, 0)}
        app.idle_alert = _BoolVar(idle_enabled)
        app.idle_choice = _StrVar("30 分钟")
        app.idle_watch = core.IdleWatcher(core.IDLE_DEFAULT_SECONDS)
        app.idle_watch.idle[4242] = idle_seconds
        return app

    def test_window_row_tip_has_the_gesture_help(self):
        text = self._app()._row_tip_text("win")
        self.assertIn("某窗口标题", text)
        self.assertIn("双击", text)
        self.assertIn("Enter", text)

    def test_non_window_rows_have_no_tip(self):
        self.assertEqual(self._app()._row_tip_text("grp"), "")
        self.assertEqual(self._app()._row_tip_text("不存在"), "")

    def test_idle_time_is_shown_when_the_feature_is_on(self):
        app = self._app(idle_enabled=True, idle_seconds=25 * 60)
        self.assertIn("已闲置", app._row_tip_text("win"))

    def test_idle_line_is_absent_when_the_feature_is_off(self):
        app = self._app(idle_enabled=False, idle_seconds=25 * 60)
        self.assertNotIn("已闲置", app._row_tip_text("win"))

    def test_busy_program_says_it_is_being_counted_as_in_use(self):
        app = self._app(idle_enabled=True, idle_seconds=0.0)
        self.assertIn("在用", app._row_tip_text("win"))

    def test_unknown_window_is_tolerated(self):
        """行还在但窗口已经没了（列表还没刷新）→ 不能抛异常。"""
        app = self._app()
        app.win_by_hwnd = {}
        self.assertEqual(app._row_tip_text("win"), "")


class _BoolVar:
    def __init__(self, v):
        self._v = v

    def get(self):
        return self._v

    def set(self, v):
        self._v = v


class _StrVar:
    def __init__(self, v):
        self._v = v

    def get(self):
        return self._v

    def set(self, v):
        self._v = v


class TestOtherForegroundTrackingRemoved(unittest.TestCase):
    """第一版的"上一个前台窗口"那套状态已经彻底删掉了，别再有残留。"""

    def test_no_leftover_state_helpers(self):
        for name in ("_note_other_foreground", "_my_hwnd", "_on_list_click_activate",
                     "_on_list_click"):
            with self.subTest(name=name):
                self.assertFalse(hasattr(winmon.WinMonApp, name), f"{name} 还留着")

    def test_app_has_no_last_other_foreground_attribute(self):
        """构造一个 app 也不该出现 `_last_other_fg`（装机时最容易"顺手加回来"）。"""
        self.assertNotIn("_last_other_fg", dir(winmon.WinMonApp))


@h.needs_tk
class TestWindowIsIconicHelper(unittest.TestCase):
    """`core.window_is_minimized`：判定双击要用的是**实时**状态。"""

    def setUp(self):
        import tkinter as tk

        self.root = tk.Tk()
        self.addCleanup(self.root.destroy)

    def test_false_for_empty_and_dead_handles(self):
        for hwnd in (0, 0x7FFF0000):
            with self.subTest(hwnd=hwnd):
                self.assertFalse(core.window_is_minimized(hwnd))

    def test_matches_isisiconic_for_a_real_window(self):
        """真窗口上必须与 `IsIconic` 完全一致（这就是它的全部实现）。"""
        import tkinter as tk

        top = tk.Toplevel(self.root)
        self.addCleanup(top.destroy)
        top.geometry("320x200+120+120")
        top.update()
        wi = h.pick_window_for_pid(os.getpid())
        if wi is None:
            self.skipTest("本进程没有可见窗口可用")
        self.assertEqual(
            core.window_is_minimized(wi.hwnd),
            bool(core.user32.IsIconic(core.w.HWND(wi.hwnd))))
        self.assertFalse(core.window_is_minimized(wi.hwnd),
                         "刚建的窗口不可能是最小化的")
        core.minimize_window(wi.hwnd)
        self.assertTrue(core.window_is_minimized(wi.hwnd),
                        "最小化之后实时查询必须是 True（这正是双击切换的依据）")


class TestBadHandleSafety(unittest.TestCase):
    """非法句柄必须老老实实返回失败，不能抛异常也不能声称成功。"""

    DEAD = 0x7FFF0000

    def test_activate_rejects_empty_and_dead_handles(self):
        for hwnd in (0, self.DEAD):
            with self.subTest(hwnd=hwnd):
                ok, msg = core.activate_window(hwnd)
                self.assertFalse(ok)
                self.assertTrue(msg)

    def test_minimize_rejects_empty_and_dead_handles(self):
        for hwnd in (0, self.DEAD):
            with self.subTest(hwnd=hwnd):
                ok, msg = core.minimize_window(hwnd)
                self.assertFalse(ok)
                self.assertTrue(msg)

    def test_no_false_success_for_a_dead_handle(self):
        """最坏的一种错是"说切好了其实没有" —— 这里钉死它。"""
        ok, _msg = core.activate_window(self.DEAD)
        self.assertFalse(ok)


@h.needs_tk
class TestWindowStateReal(unittest.TestCase):
    """真开一个别的进程的窗口，真的最小化 / 还原，并回读系统状态核对。"""

    def setUp(self):
        self.proc, self.wi = self._spawn_target()
        if self.wi is None:
            self.skipTest("没能创建可见的靶子窗口（环境没有图形会话）")

    def _spawn_target(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = os.path.join(tmp.name, "target.py")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(TARGET_WINDOW_SCRIPT)
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        proc = subprocess.Popen([sys.executable, path, "WinMon-WinOp-Target"],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                creationflags=flags)
        h.spawn_cleanup(self, proc)
        wi = None
        for _ in range(60):
            if proc.poll() is not None:
                err = (proc.stderr.read() or b"").decode("utf-8", "replace")
                self.skipTest(f"靶子进程提前退出：{err[-300:]}")
            wi = h.pick_window_for_pid(proc.pid)
            if wi is not None and wi.rect_w >= 300:
                break
            wi = None
            time.sleep(0.2)
        return proc, wi

    # ---- 工具 ----

    @staticmethod
    def _iconic(hwnd: int) -> bool:
        return bool(core.user32.IsIconic(core.w.HWND(hwnd)))

    @staticmethod
    def _foreground() -> int:
        return core.get_foreground_hwnd()

    # ---- 断言 ----

    def test_minimize_really_minimizes(self):
        ok, msg = core.minimize_window(self.wi.hwnd)
        self.assertTrue(ok, msg)
        self.assertTrue(self._iconic(self.wi.hwnd),
                        "minimize_window 说成功，但系统说它没最小化")

    def test_minimize_is_idempotent(self):
        core.minimize_window(self.wi.hwnd)
        ok, msg = core.minimize_window(self.wi.hwnd)
        self.assertTrue(ok, msg)
        self.assertTrue(self._iconic(self.wi.hwnd))

    def test_activate_restores_a_minimized_window(self):
        """
        还原**不依赖前台锁**，所以这条是硬断言：
        哪怕这个会话抢不到前台，`SW_RESTORE` 也必须把窗口恢复过来。
        """
        core.minimize_window(self.wi.hwnd)
        self.assertTrue(self._iconic(self.wi.hwnd))
        core.activate_window(self.wi.hwnd)
        self.assertFalse(self._iconic(self.wi.hwnd),
                         "activate_window 没有把最小化的窗口还原")

    def test_success_is_never_faked(self):
        """声称切到前台了 → 系统必须也这么认为。"""
        core.minimize_window(self.wi.hwnd)
        ok, msg = core.activate_window(self.wi.hwnd)
        if not ok:
            self.skipTest(f"当时抢不到前台（Windows 前台锁）：{msg}")
        self.assertEqual(self._foreground(), self.wi.hwnd,
                         "activate_window 说成功，但前台窗口并不是它")

    def test_activate_on_an_already_visible_window(self):
        ok, msg = core.activate_window(self.wi.hwnd)
        if not ok:
            self.skipTest(f"当时抢不到前台（Windows 前台锁）：{msg}")
        self.assertEqual(self._foreground(), self.wi.hwnd)
        self.assertFalse(self._iconic(self.wi.hwnd))


@h.needs_tk
class TestWindowStateStability(unittest.TestCase):
    """
    稳定性：把"最小化 → 唤醒还原"连做 `STABILITY_ROUNDS` 个来回。

    最小化与还原是**不依赖前台锁**的（`ShowWindow`），所以这里要求 100% 成功；
    "能不能抢到前台"另算 —— 只要它**一次都没有谎报成功**就行。
    """

    def test_minimize_and_restore_loop_is_stable(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = os.path.join(tmp.name, "target.py")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(TARGET_WINDOW_SCRIPT)
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        proc = subprocess.Popen([sys.executable, path, "WinMon-WinOp-Target"],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                creationflags=flags)
        h.spawn_cleanup(self, proc)
        wi = None
        for _ in range(60):
            if proc.poll() is not None:
                self.skipTest("靶子进程提前退出")
            wi = h.pick_window_for_pid(proc.pid)
            if wi is not None and wi.rect_w >= 300:
                break
            wi = None
            time.sleep(0.2)
        if wi is None:
            self.skipTest("没能创建可见的靶子窗口（环境没有图形会话）")

        minimized = restored = foreground = faked = 0
        for i in range(STABILITY_ROUNDS):
            ok, msg = core.minimize_window(wi.hwnd)
            if not ok or not core.user32.IsIconic(core.w.HWND(wi.hwnd)):
                self.fail(f"第 {i + 1} 轮最小化失败：{msg}")
            minimized += 1

            ok, msg = core.activate_window(wi.hwnd)
            if core.user32.IsIconic(core.w.HWND(wi.hwnd)):
                self.fail(f"第 {i + 1} 轮没有还原：{msg}")
            restored += 1
            if ok:
                if core.get_foreground_hwnd() == wi.hwnd:
                    foreground += 1
                else:
                    faked += 1

        with self.subTest(stats=f"{minimized}/{restored}/{foreground}"):
            self.assertEqual(minimized, STABILITY_ROUNDS)
            self.assertEqual(restored, STABILITY_ROUNDS)
            self.assertEqual(faked, 0, "出现了「声称切到前台但并不是」的情况")
        print(f"\n  稳定性：最小化 {minimized}/{STABILITY_ROUNDS} · "
              f"还原 {restored}/{STABILITY_ROUNDS} · "
              f"真拿到前台 {foreground}/{STABILITY_ROUNDS}")


@h.needs_tk
class TestListClickEndToEnd(unittest.TestCase):
    """
    **真·端到端**：真的开一个 `WinMonApp`（含真 Treeview 与真事件循环），
    用真的行坐标驱动点击处理，核对目标窗口真的切到前台 / 真的被最小化。

    为什么这条不能省：其它几层用的都是假 tree 或直接调 `core`，
    整条链路（行定位 → 决策 → 后台队列 → core 窗口操作）只有这里能一起跑到。
    开发时正是它抓到了一个真 bug：`activate_window` 原来只把本线程挂到
    **目标窗口**的输入队列上，于是"本进程不是前台进程"时会被前台锁拒绝；
    改成同时挂到**当前前台窗口**的线程之后才真正生效
    （只跑单测/只跑 core 测试都发现不了，因为那些场景下前台锁本来就没生效）。

    同时它也是**稳定性**证据：连续跑这条用例，各步都应稳定通过。
    """

    SETTLE_MS = 900        # 两次独立点击之间必须超过系统双击间隔

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.settings = os.path.join(self.tmp.name, "winmon_settings.json")
        self.procs = []
        self.targets = {}
        self.results: list[tuple[str, bool, str]] = []

    # ---- 靶子 ----

    def _spawn(self, title: str):
        path = os.path.join(self.tmp.name, f"{title}.py")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(TARGET_WINDOW_SCRIPT)
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        proc = subprocess.Popen([sys.executable, path, title],
                                stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, creationflags=flags)
        h.spawn_cleanup(self, proc)
        self.procs.append(proc)
        for _ in range(60):
            if proc.poll() is not None:
                self.skipTest("靶子进程提前退出")
            hits = [w for w in core.enum_windows() if w.title == title]
            if hits:
                self.targets[title] = hits[0]
                return
            time.sleep(0.2)
        self.skipTest(f"没能创建靶子窗口 {title}（环境没有图形会话）")

    # ---- 工具 ----

    def _record(self, name: str, ok: bool, detail: str) -> None:
        self.results.append((name, bool(ok), detail))

    def test_real_gestures_select_minimize_and_activate(self):
        import tkinter as tk
        import winmon as winmon_mod

        a, b = "WinMon-E2E-A", "WinMon-E2E-B"
        self._spawn(a)
        self._spawn(b)

        old_env = os.environ.get("WINMON_SETTINGS")
        os.environ["WINMON_SETTINGS"] = self.settings
        try:
            root = tk.Tk()
            app = winmon_mod.WinMonApp(root)
        finally:
            if old_env is None:
                os.environ.pop("WINMON_SETTINGS", None)
            else:
                os.environ["WINMON_SETTINGS"] = old_env
        self.addCleanup(h.shutdown_app, app)
        app.follow_selection.set(False)          # 别让预览抢活儿

        def row_of(name: str) -> str:
            """每次重新查行号 —— 列表每 2 秒重建一次，iid 会变。"""
            item = app.row_of_hwnd.get(self.targets[name].hwnd)
            if item:
                app.tree.see(item)
                root.update_idletasks()
            return item

        hwnd_a, hwnd_b = self.targets[a].hwnd, self.targets[b].hwnd
        iconic = lambda h: bool(core.user32.IsIconic(core.w.HWND(h)))     # noqa: E731

        def why() -> str:
            """把状态栏那句话带上 —— 失败时一眼能看出是"前台锁拒绝"还是别的。"""
            return f"状态栏：{app.status_text.get()}"

        def wait_then(cond, then, timeout: float = 6.0) -> None:
            """
            轮询等待，**不能堵住事件循环**。

            必须用 `root.after` 而不是 `time.sleep` 死等：WinMon 的后台窗口操作是靠
            `root.after(0, ...)` 把结果送回主线程的，`_poll_foreground` 也靠事件循环。
            一旦在主线程里 sleep，这些回调全被卡住 —— 第一版就是这么写的，
            结果"最小化"那一步永远等不到（不是产品的问题，是测试把循环堵死了）。
            """
            end = time.time() + timeout

            def poll():
                if cond() or time.time() > end:
                    then()
                else:
                    root.after(100, poll)
            poll()

        def own_hwnd() -> int:
            """WinMon 自己的窗口句柄（模拟"用户点了 WinMon"要用）。"""
            return core.H(core.user32.GetAncestor(core.w.HWND(root.winfo_id()),
                                                  core.GA_ROOT))

        def click(name: str, how: str = "single") -> None:
            """
            按**真实使用方式**驱动一次手势。

            关键：先把 WinMon 自己的窗口切到前台 —— 用户是"坐在 WinMon 里"点列表的，
            上一版端到端漏了这一步（直接调处理函数，WinMon 并不是前台窗口），
            于是漏掉了"双击永远只会唤醒"那个 bug（见 plan_window_click 的说明）。
            """
            core.activate_window(own_hwnd())
            item = row_of(name)
            self.assertTrue(item, f"{name} 没出现在列表里")
            box = app.tree.bbox(item)
            self.assertTrue(box, f"{name} 的行不可见")
            ev = type("E", (), {"x": box[0] + box[2] // 2,
                                "y": box[1] + box[3] // 2})()
            resolved_item, wi = app._row_window_at(ev)
            self.assertIsNotNone(
                wi, f"行没被识别成窗口行：item={item!r} box={box} "
                    f"region={app.tree.identify_region(ev.x, ev.y)!r} "
                    f"identify_row={app.tree.identify_row(ev.y)!r} "
                    f"row_info={app.row_info.get(item)!r}")
            self.assertEqual(wi.hwnd, self.targets[name].hwnd)
            if how == "single":
                # 单击在 WinMon 里**没有处理函数**（`_on_list_click` 已随复查补丁删除，
                # 它本来就是空实现且没被 bind 过）。所以这里只做 Tk 自己会做的事：
                # 把那一行选中（`<<TreeviewSelect>>` 由 Tk 派发），
                # 然后由后面的断言核对"前台仍是 WinMon 自己" = 没有抢前台。
                app.tree.selection_set(item)
                root.update()
            elif how == "double":
                app._on_list_double_click(ev)
            elif how == "middle":
                app._on_list_middle_click(ev)
            elif how == "enter":
                # 回车作用于"选中的行"：先真的把它选中。
                # 必须 `update()` 而不是 `update_idletasks()` ——
                # `<<TreeviewSelect>>` 是虚拟事件，只有真正跑一轮事件循环才会派发，
                # 而 `selected_hwnd` 正是由那个事件的处理函数设置的。
                app.tree.selection_set(item)
                root.update()
                app.action_bring_front()
            else:
                raise AssertionError(how)

        hwnd_a, hwnd_b = self.targets[a].hwnd, self.targets[b].hwnd
        iconic = lambda h: bool(core.user32.IsIconic(core.w.HWND(h)))     # noqa: E731
        fg_is = lambda h: core.get_foreground_hwnd() == h                 # noqa: E731

        #: 每步 =（名字, 动作, 期望条件, 细节）
        checks = [
            # ① 回车 = 切到最前
            ("回车 → B 切到最前", lambda: click(b, "enter"),
             lambda: fg_is(hwnd_b), lambda: hex(core.get_foreground_hwnd())),
            # ② 单击只选中：**不会**把被点的那一行抢到前面
            #    （点完列表后前台仍是 WinMon 自己，就说明 A 没有被激活）
            ("单击 A 只选中、不抢前台", lambda: click(a, "single"),
             lambda: core.get_foreground_hwnd() == own_hwnd(),
             lambda: hex(core.get_foreground_hwnd())),
            # ③ 中键 = 切到最前
            ("中键 A → A 切到最前", lambda: click(a, "middle"),
             lambda: fg_is(hwnd_a), lambda: hex(core.get_foreground_hwnd())),
            # ④ 双击 = 最小化 ↔ 还原（"双击只会唤醒"那个 bug 的核心回归）
            ("双击 A（可见）→ 最小化", lambda: click(a, "double"),
             lambda: iconic(hwnd_a), lambda: f"IsIconic={iconic(hwnd_a)}"),
            ("再双击 A（已最小化）→ 唤醒还原", lambda: click(a, "double"),
             lambda: not iconic(hwnd_a), lambda: f"IsIconic={iconic(hwnd_a)}"),
            # ⑤ 后台窗口同样能最小化（不再有"是前台才最小化"的门槛）
            ("双击后台窗口 B（可见）→ 最小化", lambda: click(b, "double"),
             lambda: iconic(hwnd_b), lambda: f"IsIconic={iconic(hwnd_b)}"),
            ("再双击 B → 还原", lambda: click(b, "double"),
             lambda: not iconic(hwnd_b), lambda: f"IsIconic={iconic(hwnd_b)}"),
        ]

        finished = {"flag": False}

        def finish() -> None:
            if finished["flag"]:
                return
            finished["flag"] = True
            app.on_close()

        def run(i: int = 0) -> None:
            if i >= len(checks):
                root.after(0, finish)
                return
            name, action, cond, detail = checks[i]

            def go():
                try:
                    action()
                except AssertionError as exc:
                    self._record(name, False, f"驱动点击失败：{exc}")
                    root.after(self.SETTLE_MS, lambda: run(i + 1))
                    return
                wait_then(cond, lambda: (
                    self._record(name, cond(), f"{detail()} · {why()}"),
                    root.after(self.SETTLE_MS, lambda: run(i + 1))))

            root.after(0, go)

        def bootstrap() -> None:
            missing = [n for n in (a, b) if n not in self.targets]
            if missing:
                self.skipTest(f"靶子窗口没起来：{missing}")
            run(0)

        # 兜底：万一某一步永远等不到，也别把整轮测试挂死
        root.after(70000, lambda: (self._record("整体超时", False, "70 秒没走完"),
                                   finish()))
        root.after(2500, bootstrap)
        root.mainloop()

        self.assertTrue(self.results, "一步都没执行")
        failed = [f"{n}（{d}）" for n, ok, d in self.results if not ok]
        self.assertEqual(failed, [], f"端到端有步骤失败：{failed}")
        print("\n  端到端：" + " · ".join(f"{n} ✓" for n, _ok, _d in self.results))


class TestTreeKeepsUserExpansion(unittest.TestCase):
    """
    列表 / 进程树重建时**不能把用户展开的节点收回去**。

    2026-10-13 修掉的两个真 bug，病根是同一个：`tkiner` 重建时拿 **item id**
    当身份 —— Tk 不复用删掉的 id（实测 insert→I001、删掉再 insert→I003），
    于是"恢复展开"永远对不上：

      ① 左栏的分组行每次重建都写死 `open=False`，而列表**每 2 秒**重建一次：
         用户展开「某程序（2 个窗口）」想看子窗口，两秒内必定被折叠，子行点不到；
      ② 进程树的恢复逻辑存的是旧 item id，`tv.exists(iid)` 恒为假 ——
         那段代码从来没生效过，任何进程启动/退出都会把展开的节点收回去。

    修法：分组行按**程序名**、进程树按 **pid** 记展开状态，重建后按新 id 还原。
    """

    def setUp(self):
        import tkinter as tk
        try:
            self.root = tk.Tk()
        except Exception as exc:                              # pragma: no cover
            self.skipTest(f"没有图形会话：{exc}")
        self.addCleanup(lambda: self._safe_destroy(self.root))
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        old = os.environ.get("WINMON_SETTINGS")
        os.environ["WINMON_SETTINGS"] = os.path.join(self.tmp.name, "winmon_settings.json")
        try:
            self.app = winmon.WinMonApp(self.root)
        finally:
            if old is None:
                os.environ.pop("WINMON_SETTINGS", None)
            else:
                os.environ["WINMON_SETTINGS"] = old
        self.addCleanup(h.shutdown_app, self.app)

    @staticmethod
    def _safe_destroy(root) -> None:
        """清理时销毁 root；`h.shutdown_app` 可能已经销毁过了，别再抛出来。"""
        h.destroy_root(root)

    @staticmethod
    def _fake_window(hwnd: int, pid: int, title: str, proc: str) -> core.WinInfo:
        wi = core.WinInfo(hwnd, pid, title, "FakeClass", 300, 200, False, False, False)
        wi.proc = proc
        wi.path = r"C:\fake\%s" % proc
        return wi

    def _group_rows(self) -> list:
        return [iid for iid, info in self.app.row_info.items()
                if info and info[0] == "group"]

    def test_group_row_stays_expanded_across_the_2s_rebuild(self):
        proc = "wingroup.exe"
        self.app.windows = [self._fake_window(900001, 9001, "组窗口一", proc),
                            self._fake_window(900002, 9002, "组窗口二", proc)]
        self.app.proc_map = {
            9001: {"name": proc, "ppid": 0, "mem_kb": 10, "mem_private_kb": 5},
            9002: {"name": proc, "ppid": 0, "mem_kb": 20, "mem_private_kb": 8},
        }
        self.app._rebuild_tree(None)
        groups = self._group_rows()
        self.assertEqual(len(groups), 1, f"应当生成一个分组行，实际 {len(groups)} 个")

        self.app.tree.item(groups[0], open=True)      # 用户展开它
        self.app._rebuild_tree(None)                  # 相当于 2 秒后的那次刷新
        groups2 = self._group_rows()
        self.assertEqual(len(groups2), 1)
        self.assertTrue(self.app.tree.item(groups2[0], "open"),
                        "刷新一次就把用户展开的分组折叠了（子窗口行点不到）")

    def test_group_row_that_was_never_opened_stays_closed(self):
        """别矫枉过正：没展开过的分组，重建后仍然应当是收起的（默认收起是有意的）。"""
        proc = "wingroup2.exe"
        self.app.windows = [self._fake_window(900011, 9011, "组窗口一", proc),
                            self._fake_window(900012, 9012, "组窗口二", proc)]
        self.app.proc_map = {
            9011: {"name": proc, "ppid": 0, "mem_kb": 10, "mem_private_kb": 5},
            9012: {"name": proc, "ppid": 0, "mem_kb": 20, "mem_private_kb": 8},
        }
        self.app._rebuild_tree(None)
        self.app._rebuild_tree(None)
        groups = self._group_rows()
        self.assertEqual(len(groups), 1)
        self.assertFalse(self.app.tree.item(groups[0], "open"))

    def test_process_tree_keeps_expanded_nodes(self):
        """进程树：展开一个**深层**节点，重建后它必须还是展开的（按 pid 还原）。"""
        tv = self.app.proc_tree.tree
        root_pid, mid_pid, leaf_pid = 8101, 8102, 8103
        self.app.windows = [self._fake_window(900021, leaf_pid, "叶子窗口", "leaf.exe")]
        self.app.proc_map = {
            root_pid: {"name": "root.exe", "ppid": 0, "mem_kb": 30,
                       "mem_private_kb": 10},
            mid_pid: {"name": "mid.exe", "ppid": root_pid, "mem_kb": 20,
                      "mem_private_kb": 8},
            leaf_pid: {"name": "leaf.exe", "ppid": mid_pid, "mem_kb": 10,
                       "mem_private_kb": 4},
        }
        self.app.refresh_process_tree(force=True)
        mid = next((i for i, p in self.app._tree_pid_of.items() if p == mid_pid), "")
        self.assertTrue(mid, "中间那个进程应当在树上")
        self.assertFalse(tv.item(mid, "open"), "前提：深度 ≥1 的节点默认是收起的")
        tv.item(mid, open=True)                       # 用户展开它

        self.app.refresh_process_tree(force=True)     # 任何进程变动都会走到这里
        mid2 = next((i for i, p in self.app._tree_pid_of.items() if p == mid_pid), "")
        self.assertTrue(mid2, "重建后中间进程还在")
        self.assertTrue(tv.item(mid2, "open"),
                        "重建一次就把进程树上展开的节点收回去了")


if __name__ == "__main__":
    unittest.main(verbosity=2)
