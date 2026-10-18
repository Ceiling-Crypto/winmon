# -*- coding: utf-8 -*-
"""
B10 —— 工作区快照（`test_b10`）。

用户要的是："写代码 / 看视频 / 打游戏"各存一套窗口布局，一键切回去。

分三层写：

  1. **纯逻辑**：`capture_workspace` / `plan_workspace_apply` / `normalize_workspaces`
     / `clamp_rect_to_screen`。这一层不碰 Win32，任何环境都必须跑；也是这个功能
     最容易出错的地方 —— 匹配错了就会把窗口挪到莫名其妙的位置。
  2. **真窗口**：真开一个别的进程的窗口，真的把它挪到位、真的最小化、真的还原；
     以及"矩形在屏幕外时会被夹回来"（快照可能是插着外接显示器时存的）。
  3. **界面接线**：用假 app 驱动 `WorkspacePane`（真 Tk 控件），
     核对保存/更新/删除/应用四条路径与列表刷新。

安全约定：恢复**只对当前开着的窗口生效**，对不上的如实报告，**不替你启动程序**；
`minimize_windows_except` 跳过保护进程与 WinMon 自己。
"""
from __future__ import annotations

import os
import json
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

#: 靶子：一个位置固定的普通 Tk 窗口（用来验证"真的挪动了"）。
WS_TARGET_SCRIPT = r'''
# -*- coding: utf-8 -*-
"""靶子：普通 Tk 窗口。"""
import sys
import tkinter as tk

title = sys.argv[1] if len(sys.argv) > 1 else "WsProbe"
root = tk.Tk()
root.title(title)
root.geometry("380x260+320+240")
tk.Label(root, text="workspace target").pack(pady=30)
root.update()
root.mainloop()
'''


def win(hwnd, name, title="", rect=None, iconic=False):
    """造一条"窗口"记录（`plan_workspace_apply` 两种入参形式都要吃）。"""
    return {"hwnd": hwnd, "name": name, "title": title,
            "rect": rect or [0, 0, 400, 300], "iconic": iconic}


class TestCaptureWorkspace(unittest.TestCase):
    """拍快照：只记可信的窗口，尺寸可疑的丢掉。"""

    def test_captures_name_title_rect_iconic(self):
        snap = core.capture_workspace("写代码", [win(1, "Code.exe", "a - Code",
                                                     [10, 20, 800, 600], False)],
                                      now=123.0)
        self.assertEqual(snap["name"], "写代码")
        self.assertEqual(snap["saved_at"], 123.0)
        e = snap["entries"][0]
        self.assertEqual((e["name"], e["title"], e["rect"], e["iconic"]),
                         ("Code.exe", "a - Code", [10, 20, 800, 600], False))

    def test_moves_dict_and_object_entries_equally(self):
        """两种入参形式（dict / 带属性的对象）都要能拍。"""
        import types
        obj = types.SimpleNamespace(name="Code.exe", title="标题",
                                    rect=[1, 2, 500, 400], iconic=True)
        snap = core.capture_workspace("x", [obj])
        self.assertEqual(snap["entries"][0]["rect"], [1, 2, 500, 400])
        self.assertTrue(snap["entries"][0]["iconic"])

    def test_too_small_rects_are_dropped(self):
        """
        尺寸可疑的窗口不记：正在做动画、或者被折叠成一条的窗口，
        记下来恢复时只会把它挪到奇怪的位置。
        """
        snap = core.capture_workspace("x", [
            win(1, "a.exe", rect=[0, 0, 50, 50]),          # 太小
            win(2, "b.exe", rect=[0, 0, 800, 20]),         # 太扁
            win(3, "c.exe", rect=[0, 0, 800, 600]),        # 正常
            {"hwnd": 4, "name": "d.exe", "title": "没有 rect"},
        ])
        self.assertEqual([e["name"] for e in snap["entries"]], ["c.exe"])

    def test_entry_count_is_capped(self):
        many = [win(i, f"a{i}.exe", rect=[0, 0, 400, 300])
                for i in range(core.WS_MAX_ENTRIES + 25)]
        self.assertEqual(len(core.capture_workspace("x", many)["entries"]),
                         core.WS_MAX_ENTRIES)

    def test_name_is_trimmed(self):
        self.assertEqual(core.capture_workspace("  写代码  ", [])["name"], "写代码")


class TestWindowRecordShapeMismatch(unittest.TestCase):
    """
    **回归判据：生产路径传进来的是 `WinInfo` 对象，不是 dict。**

    第一版在两种形态上各写了一套取值（`wi.get("name")` / `getattr(wi, "name")`），
    而 `WinInfo` 的字段叫 `proc`、根本没有 `name` —— 于是走真实路径时每条记录
    的可执行文件名都取成空串，**全部落进"这个程序没开"**：
    用户点「应用」毫无反应，而且不报任何错。
    更糟的是当时所有用例都只在 dict 上传参，所以整个测试套件是绿的。

    这一组用例专门用**真的 `WinInfo` 对象**跑一遍，把那个盲区钉死。
    """

    @staticmethod
    def _wi(hwnd, proc, title):
        wi = core.WinInfo(hwnd, 4242, title, "Cls", 800, 600, False, False, False)
        wi.proc = proc
        return wi

    def test_plan_matches_real_wininfo_objects(self):
        snap = {"name": "w", "entries": [
            {"name": "Code.exe", "title": "a - Code", "rect": [1, 2, 800, 600],
             "iconic": False}]}
        plan = core.plan_workspace_apply(
            snap, [self._wi(11, "Code.exe", "a - Code")])
        self.assertEqual(plan["missing"], [],
                         "真 WinInfo 没被匹配上 —— 字段名很可能又写错了")
        self.assertEqual([i["hwnd"] for i in plan["restore"]], [11])

    def test_plan_falls_back_to_name_on_real_objects(self):
        snap = {"name": "w", "entries": [
            {"name": "msedge.exe", "title": "旧标题", "rect": [1, 2, 800, 600],
             "iconic": True}]}
        plan = core.plan_workspace_apply(
            snap, [self._wi(22, "msedge.exe", "换了个标题")])
        self.assertEqual([i["hwnd"] for i in plan["minimize"]], [22])

    def test_empty_names_still_match_by_title(self):
        """
        名字**彻底取不到**时（这里用一个什么字段都没有的裸对象模拟），
        靠标题还是能对上一条 —— 比"全部报成没开"更有用。

        注意真 `WinInfo` 不会走到这一步：它没有 `proc` 时 `real_name` 会兜底
        （见下一个用例），所以这里必须用一个真正字段全空的对象。
        """
        import types
        bare = types.SimpleNamespace(hwnd=1, pid=1, title="?", iconic=False)
        snap = {"name": "w", "entries": [
            {"name": "", "title": "?", "rect": [0, 0, 400, 300], "iconic": False}]}
        plan = core.plan_workspace_apply(snap, [bare])
        self.assertEqual(plan["missing"], [])
        self.assertEqual([i["hwnd"] for i in plan["restore"]], [1])

    def test_capture_asks_windows_for_the_rect_when_missing(self):
        """`WinInfo` 没有 rect —— 有句柄时应当**当场问系统要一次**，而不是丢掉这条。"""
        wi = self._wi(core.user32.GetForegroundWindow(), "Code.exe", "前台窗口")
        snap = core.capture_workspace("x", [wi])
        self.assertEqual(len(snap["entries"]), 1, "带句柄的记录被丢掉了")
        self.assertEqual(len(snap["entries"][0]["rect"]), 4)

    def test_window_record_normalizes_both_shapes(self):
        got = core.window_record({"hwnd": 5, "name": "a.exe", "title": "T"})
        self.assertEqual((got["hwnd"], got["name"], got["title"]), (5, "a.exe", "T"))
        got = core.window_record(self._wi(6, "b.exe", "U"))
        self.assertEqual((got["hwnd"], got["name"], got["title"]), (6, "b.exe", "U"))

    def test_wininfo_without_proc_gives_a_usable_name(self):
        """`proc` 还没填上时退回 `real_name`，别直接给个空串。"""
        wi = core.WinInfo(7, 4242, "标题", "Cls", 800, 600, False, False, False)
        self.assertTrue(core.window_record(wi)["name"])


class TestPlanWorkspaceApply(unittest.TestCase):
    """匹配：先按标题精确配、再按可执行文件名补、配不上的如实报告。"""

    def _snapshot(self):
        return {"name": "写代码", "entries": [
            {"name": "Code.exe", "title": "main.py - Code",
             "rect": [10, 10, 800, 600], "iconic": False},
            {"name": "msedge.exe", "title": "文档 - Edge",
             "rect": [900, 10, 700, 500], "iconic": True},
            {"name": "notepad.exe", "title": "笔记",
             "rect": [0, 0, 400, 300], "iconic": False},
        ]}

    def test_title_match_wins(self):
        plan = core.plan_workspace_apply(self._snapshot(), [
            win(11, "Code.exe", "main.py - Code"),
            win(12, "Code.exe", "别的文件 - Code"),      # 同名进程的另一个窗口
        ])
        self.assertEqual(plan["restore"][0]["hwnd"], 11)
        self.assertEqual(plan["restore"][0]["matched_by"], "title")

    def test_falls_back_to_process_name_when_title_changed(self):
        """
        浏览器那种标题一直在变的窗口，只能靠可执行文件名对上 ——
        这也是为什么**不能**只按标题匹配。
        """
        plan = core.plan_workspace_apply(self._snapshot(), [
            win(22, "msedge.exe", "完全换了一个页面标题"),
        ])
        self.assertEqual(plan["minimize"][0]["hwnd"], 22)
        self.assertEqual(plan["minimize"][0]["matched_by"], "name")

    def test_saved_minimized_goes_to_minimize(self):
        plan = core.plan_workspace_apply(self._snapshot(), [win(22, "msedge.exe", "x")])
        self.assertEqual(len(plan["minimize"]), 1)
        self.assertEqual(plan["restore"], [])

    def test_missing_programs_are_reported_not_launched(self):
        """对不上的程序只报告 —— **绝不替你启动**（那是另一个功能，而且很危险）。"""
        plan = core.plan_workspace_apply(self._snapshot(), [win(11, "Code.exe", "main.py - Code")])
        self.assertEqual([m["name"] for m in plan["missing"]], ["msedge.exe", "notepad.exe"])

    def test_each_window_is_used_at_most_once(self):
        """
        快照里同一个程序有两个窗口时，不能把**同一个**当前窗口分配给两条记录 ——
        否则第二个窗口永远恢复不了，而第一个会被反复挪动。
        """
        snap = {"name": "双开", "entries": [
            {"name": "Code.exe", "title": "A", "rect": [0, 0, 800, 600], "iconic": False},
            {"name": "Code.exe", "title": "B", "rect": [800, 0, 800, 600], "iconic": False},
        ]}
        plan = core.plan_workspace_apply(snap, [
            win(1, "Code.exe", "A"), win(2, "Code.exe", "B")])
        self.assertEqual(sorted(i["hwnd"] for i in plan["restore"]), [1, 2])

    def test_third_window_of_same_app_is_left_alone(self):
        snap = {"name": "x", "entries": [
            {"name": "Code.exe", "title": "A", "rect": [0, 0, 800, 600], "iconic": False}]}
        plan = core.plan_workspace_apply(snap, [
            win(1, "Code.exe", "A"), win(2, "Code.exe", "B")])
        self.assertEqual([i["hwnd"] for i in plan["restore"]], [1])

    def test_matching_is_case_insensitive_on_the_exe_name(self):
        snap = {"name": "x", "entries": [
            {"name": "CODE.EXE", "title": "", "rect": [0, 0, 800, 600], "iconic": False}]}
        plan = core.plan_workspace_apply(snap, [win(1, "code.exe", "t")])
        self.assertEqual([i["hwnd"] for i in plan["restore"]], [1])

    def test_empty_snapshot_is_harmless(self):
        plan = core.plan_workspace_apply({"name": "空", "entries": []}, [win(1, "a.exe")])
        self.assertEqual((plan["restore"], plan["minimize"], plan["missing"]), ([], [], []))

    def test_snapshot_with_no_windows_running(self):
        plan = core.plan_workspace_apply(self._snapshot(), [])
        self.assertEqual(len(plan["missing"]), 3)
        self.assertEqual(plan["restore"], [])


class TestNormalizeWorkspaces(unittest.TestCase):
    """设置文件是用户可编辑的文本 —— 脏数据只丢弃，不许让它把界面炸掉。"""

    def test_garbage_input_yields_empty_list(self):
        for bad in (None, "x", 42, {}, [None, "x", 3]):
            with self.subTest(bad=bad):
                self.assertEqual(core.normalize_workspaces(bad), [])

    def test_bad_entries_are_dropped_entry_by_entry(self):
        got = core.normalize_workspaces([{
            "name": "w", "entries": [
                {"name": "a.exe", "rect": [1, 2, 3, 4]},
                {"name": "b.exe", "rect": "不是矩形"},
                {"name": "c.exe", "rect": [1, 2, 3]},
                {"name": "d.exe", "rect": [1, 2, 3, 4, 5]},
                "不是字典",
            ]}])
        self.assertEqual([e["name"] for e in got[0]["entries"]], ["a.exe"])

    def test_workspace_without_a_name_is_dropped(self):
        self.assertEqual(core.normalize_workspaces([{"name": "   ", "entries": []}]), [])

    def test_missing_fields_get_defaults(self):
        got = core.normalize_workspaces([{"name": "w", "entries": [{"rect": [1, 2, 3, 4]}]}])
        self.assertEqual(got[0]["saved_at"], 0.0)
        self.assertEqual(got[0]["entries"][0]["title"], "")
        self.assertFalse(got[0]["entries"][0]["iconic"])

    def test_count_is_capped(self):
        raw = [{"name": f"w{i}", "entries": []} for i in range(core.WS_MAX_COUNT + 5)]
        self.assertEqual(len(core.normalize_workspaces(raw)), core.WS_MAX_COUNT)

    def test_round_trip_through_normalize_preserves_valid_data(self):
        snap = core.capture_workspace("写代码", [win(1, "Code.exe", "t", [1, 2, 800, 600], True)],
                                      now=7.0)
        again = core.normalize_workspaces([snap])[0]
        self.assertEqual(again["entries"][0]["rect"], [1, 2, 800, 600])
        self.assertTrue(again["entries"][0]["iconic"])


class TestClampRect(unittest.TestCase):
    """矩形必须夹回屏幕内 —— 否则"拔掉外接显示器后恢复布局"会把窗口弄丢。"""

    def test_offscreen_rect_is_pulled_back(self):
        vx, vy, vw, vh = core.virtual_screen_rect()
        x, y, cw, ch = core.clamp_rect_to_screen([-99999, -99999, 800, 600])
        self.assertGreaterEqual(x, vx)
        self.assertGreaterEqual(y, vy)
        self.assertLessEqual(x + cw, vx + vw)
        self.assertLessEqual(y + ch, vy + vh)

    def test_rect_larger_than_screen_is_shrunk(self):
        vx, vy, vw, vh = core.virtual_screen_rect()
        _x, _y, cw, ch = core.clamp_rect_to_screen([0, 0, 99999, 99999])
        self.assertLessEqual(cw, vw)
        self.assertLessEqual(ch, vh)

    def test_normal_rect_is_untouched(self):
        vx, vy, vw, vh = core.virtual_screen_rect()
        if vw < 1200 or vh < 900:
            self.skipTest("屏幕太小，放不下测试用的正常矩形")
        self.assertEqual(core.clamp_rect_to_screen([100, 80, 800, 600]), [100, 80, 800, 600])

    def test_tiny_rect_is_grown_to_a_usable_size(self):
        """快照里那种"一条缝"的窗口也要撑到能看见、能点。"""
        _x, _y, cw, ch = core.clamp_rect_to_screen([10, 10, 1, 1])
        self.assertGreaterEqual(cw, core.WS_MIN_W)
        self.assertGreaterEqual(ch, core.WS_MIN_H)


class TestGetWindowRect(unittest.TestCase):
    def test_bad_handles_return_none(self):
        for hwnd in (0, 0x7FFF0000):
            with self.subTest(hwnd=hwnd):
                self.assertIsNone(core.get_window_rect(hwnd))


@h.needs_tk
class TestWorkspaceOnRealWindows(unittest.TestCase):
    """真窗口：真挪、真最小化、真还原。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def _spawn(self, title="WsProbe-Move"):
        path = os.path.join(self.tmp.name, "ws.py")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(WS_TARGET_SCRIPT)
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        proc = subprocess.Popen([sys.executable, path, title],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                creationflags=flags)
        h.spawn_cleanup(self, proc)
        for _ in range(60):
            if proc.poll() is not None:
                self.skipTest("靶子进程提前退出")
            wi = h.pick_window_for_pid(proc.pid)
            if wi is not None:
                time.sleep(0.3)          # 等 geometry 落定
                return proc, wi.hwnd
            time.sleep(0.2)
        self.skipTest("没能创建靶子窗口")
        return proc, 0

    def test_really_moves_a_window(self):
        _proc, hwnd = self._spawn()
        ok, msg = core.move_window(hwnd, [40, 60, 500, 400])
        self.assertTrue(ok, msg)
        time.sleep(0.3)
        rect = core.get_window_rect(hwnd)
        self.assertIsNotNone(rect)
        x, y, cw, ch = rect
        # 窗口边框/DPI 会带来几像素偏差，给一点余量
        self.assertLess(abs(x - 40), 24, f"横坐标没挪到位：{rect}")
        self.assertLess(abs(y - 60), 24, f"纵坐标没挪到位：{rect}")
        self.assertLess(abs(cw - 500), 40, f"宽度不对：{rect}")

    def test_move_bad_handle_reports_failure(self):
        ok, msg = core.move_window(0, [0, 0, 400, 300])
        self.assertFalse(ok)
        self.assertTrue(msg)

    def test_apply_plan_restores_and_minimizes(self):
        """
        整条链路：一条记录该"还原并挪位"、另一条该"最小化"，
        而且做完之后**状态真的变了**（回读核对，不是只看返回值）。
        """
        _p1, hwnd_a = self._spawn("WsProbe-Restore")
        _p2, hwnd_b = self._spawn("WsProbe-Minimize")
        plan = {
            "restore": [{"hwnd": hwnd_a, "rect": [60, 90, 520, 420], "name": "a", "title": ""}],
            "minimize": [{"hwnd": hwnd_b, "rect": [0, 0, 400, 300], "name": "b", "title": ""}],
            "missing": [],
        }
        result = core.apply_workspace_plan(plan)
        self.assertEqual(result["done"], 1)
        self.assertEqual(result["minimized"], 1)
        self.assertEqual(result["failed"], [])
        time.sleep(0.4)
        self.assertTrue(core.window_is_minimized(hwnd_b), "说最小化了，但窗口还立着")
        self.assertFalse(core.window_is_minimized(hwnd_a))
        rect = core.get_window_rect(hwnd_a)
        self.assertLess(abs(rect[0] - 60), 24, f"没有挪到位：{rect}")

    def test_apply_plan_restores_a_minimized_window(self):
        _proc, hwnd = self._spawn("WsProbe-Unminimize")
        core.minimize_window(hwnd)
        time.sleep(0.4)
        self.assertTrue(core.window_is_minimized(hwnd))
        core.apply_workspace_plan({"restore": [{"hwnd": hwnd, "rect": [80, 80, 480, 380],
                                                "name": "a", "title": ""}],
                                   "minimize": [], "missing": []})
        time.sleep(0.4)
        self.assertFalse(core.window_is_minimized(hwnd), "该还原的没有还原")

    def test_apply_plan_reports_dead_windows(self):
        result = core.apply_workspace_plan({
            "restore": [{"hwnd": 0x7FFF0000, "rect": [0, 0, 400, 300],
                         "name": "ghost.exe", "title": ""}],
            "minimize": [], "missing": []})
        self.assertEqual(result["done"], 0)
        self.assertEqual(len(result["failed"]), 1)

    def test_capture_then_apply_round_trip_on_a_real_window(self):
        """
        端到端的一条真链路：拍下当前布局 → 把窗口挪走 → 按快照恢复 → 核对回到原位。

        这条才是这个功能真正要保证的事。
        """
        _proc, hwnd = self._spawn("WsProbe-RoundTrip")
        original = core.get_window_rect(hwnd)
        snap = core.capture_workspace("测试", [
            {"name": "python.exe", "title": "WsProbe-RoundTrip",
             "rect": list(original), "iconic": False}])
        self.assertEqual(len(snap["entries"]), 1)
        core.move_window(hwnd, [200, 200, 600, 420])
        time.sleep(0.3)
        self.assertGreater(abs(core.get_window_rect(hwnd)[0] - original[0]), 24,
                           "第一步就没挪动，后面测不出来")
        plan = core.plan_workspace_apply(snap, [
            {"hwnd": hwnd, "name": "python.exe", "title": "WsProbe-RoundTrip"}])
        self.assertEqual(len(plan["restore"]), 1, f"没匹配上：{plan}")
        core.apply_workspace_plan(plan)
        time.sleep(0.4)
        back = core.get_window_rect(hwnd)
        self.assertLess(abs(back[0] - original[0]), 24, f"没回到原位：{back} vs {original}")

    def test_minimize_except_skips_protected_and_keep_set(self):
        """
        "顺带收起其它窗口"只动该动的：`keep` 里的不动，
        保护名单里的（外壳、输入法）与 WinMon 自己也不动。
        """
        _proc, hwnd = self._spawn("WsProbe-KeepMe")
        fake = [
            core.WinInfo(hwnd, os.getpid() + 1, "要保留的", "Cls", 800, 600, False, False, False),
            core.WinInfo(0x7FFF0000, os.getpid() + 2, "保护进程", "Cls", 800, 600,
                         False, False, False),
            core.WinInfo(0x7FFF0001, os.getpid(), "WinMon 自己", "Cls", 800, 600,
                         False, False, False),
        ]
        fake[1].proc = "explorer.exe"
        fake[2].proc = "pythonw.exe"
        n = core.minimize_windows_except([hwnd], fake)
        self.assertEqual(n, 0, "保护对象被最小化了")
        self.assertFalse(core.window_is_minimized(hwnd))


@h.needs_tk
class TestWorkspacePane(unittest.TestCase):
    """界面接线：保存 / 更新 / 删除 / 应用四条路径（真 Tk 控件 + 假 app）。"""

    def setUp(self):
        import tkinter as tk

        try:
            self.root = tk.Tk()
            self.root.withdraw()
        except Exception as exc:                              # pragma: no cover
            self.skipTest(f"没有图形会话：{exc}")
        self.addCleanup(self.root.destroy)
        self.applied: list[str] = []
        self.saved_settings = 0
        self.captured = [
            {"name": "Code.exe", "title": "a - Code", "rect": [10, 10, 800, 600],
             "iconic": False},
        ]
        outer = self

        class _App:
            pass

        app = _App()
        app.root = self.root
        app.workspaces: list = []
        app.ws_minimize_others = tk.BooleanVar(value=False)

        def capture_current_layout():
            return list(outer.captured)

        def apply_workspace(ws):
            outer.applied.append(ws.get("name", ""))

        def schedule():
            outer.saved_settings += 1

        app.capture_current_layout = capture_current_layout
        app.apply_workspace = apply_workspace
        app._schedule_save_settings = schedule
        self.app = app
        self.pane = winmon.WorkspacePane(self.root, app)
        self.addCleanup(self.root.update_idletasks)
        self.root.update()

    def _save_as(self, name):
        """走一遍"保存当前布局"（把输入框换成固定返回值的桩）。"""
        stub = mock.MagicMock()
        stub.return_value.show.return_value = name
        with mock.patch.object(winmon, "_TextInputDialog", stub):
            self.pane.action_save()
        return stub

    def test_starts_empty(self):
        self.assertEqual(self.pane.tree.get_children(""), ())
        self.assertIn("还没有保存过工作区", self.pane.info.cget("text"))

    def test_save_adds_it_to_the_list(self):
        self._save_as("写代码")
        rows = self.pane.tree.get_children("")
        self.assertEqual(len(rows), 1)
        self.assertEqual(self.pane.tree.item(rows[0], "text"), "写代码")
        self.assertEqual(self.pane.tree.item(rows[0], "values")[0], "1")
        self.assertEqual(len(self.app.workspaces), 1)
        self.assertEqual(self.saved_settings, 1, "存完没有安排写设置文件")

    def test_cancelling_the_dialog_saves_nothing(self):
        stub = mock.MagicMock()
        stub.return_value.show.return_value = None
        with mock.patch.object(winmon, "_TextInputDialog", stub):
            self.pane.action_save()
        self.assertEqual(self.app.workspaces, [])
        self.assertEqual(self.saved_settings, 0)

    def test_saving_the_same_name_replaces_it(self):
        self._save_as("写代码")
        self.captured.append({"name": "Edge.exe", "title": "t",
                              "rect": [0, 0, 700, 500], "iconic": True})
        self._save_as("写代码")
        self.assertEqual(len(self.app.workspaces), 1, "同名应当覆盖，不是新增")
        self.assertEqual(len(self.app.workspaces[0]["entries"]), 2)

    def test_nothing_to_save_is_reported(self):
        self.captured = []
        self._save_as("空的")
        self.assertEqual(self.app.workspaces, [])
        self.assertIn("没有可保存的窗口", self.pane.info.cget("text"))

    def test_update_overwrites_with_the_current_layout(self):
        self._save_as("写代码")
        self.captured = [{"name": "New.exe", "title": "n",
                          "rect": [0, 0, 500, 400], "iconic": False}]
        self.pane.action_update()
        self.assertEqual([e["name"] for e in self.app.workspaces[0]["entries"]], ["New.exe"])

    def test_update_without_selection_only_hints(self):
        self.pane.action_update()
        self.assertEqual(self.app.workspaces, [])
        self.assertIn("选中", self.pane.info.cget("text"))

    def test_apply_hands_the_snapshot_to_the_app(self):
        self._save_as("写代码")
        self.pane.tree.selection_set(self.pane.tree.get_children("")[0])
        self.pane.action_apply()
        self.assertEqual(self.applied, ["写代码"])

    def test_apply_without_selection_only_hints(self):
        self.pane.action_apply()
        self.assertEqual(self.applied, [])

    def test_delete_removes_it_after_confirmation(self):
        self._save_as("写代码")
        self.pane.tree.selection_set(self.pane.tree.get_children("")[0])
        with mock.patch.object(winmon.messagebox, "askyesno", return_value=True):
            self.pane.action_delete()
        self.assertEqual(self.app.workspaces, [])
        self.assertEqual(self.pane.tree.get_children(""), ())

    def test_delete_is_cancellable(self):
        """用户点"否"就什么都别删 —— 这里是不可撤销的操作，必须问清楚。"""
        self._save_as("写代码")
        self.pane.tree.selection_set(self.pane.tree.get_children("")[0])
        with mock.patch.object(winmon.messagebox, "askyesno", return_value=False):
            self.pane.action_delete()
        self.assertEqual(len(self.app.workspaces), 1)

    def test_detail_lists_the_windows(self):
        self._save_as("写代码")
        self.pane.tree.selection_set(self.pane.tree.get_children("")[0])
        self.pane._show_detail()
        self.assertIn("Code.exe", self.pane.info.cget("text"))
        self.assertIn("共 1 个窗口", self.pane.info.cget("text"))

    def test_refresh_keeps_the_selection(self):
        self._save_as("写代码")
        self.pane.tree.selection_set(self.pane.tree.get_children("")[0])
        self.pane.refresh()
        sel = self.pane.tree.selection()
        self.assertTrue(sel)
        self.assertEqual(self.pane.tree.item(sel[0], "text"), "写代码")

    def test_loaded_workspaces_show_up_on_refresh(self):
        """设置文件里已经有工作区时，进页面就该看到（不是只有刚存的才显示）。"""
        self.app.workspaces = core.normalize_workspaces([
            {"name": "看视频", "saved_at": 1.0,
             "entries": [{"name": "vlc.exe", "title": "t", "rect": [0, 0, 800, 600]}]}])
        self.pane.refresh()
        self.assertEqual(self.pane.tree.item(self.pane.tree.get_children("")[0], "text"),
                         "看视频")


@h.needs_tk
class TestWorkspaceOnRealApp(unittest.TestCase):
    """
    真 `WinMonApp` 的端到端：拍当前布局 → 存成工作区 → 写进设置文件 → 重启后还在 →
    把窗口挪走 → 应用工作区 → 窗口真的回到原位。

    `TestWorkspacePane` 用的是假 app，只验证界面接线；这里补上"真 app 真的把活干了"
    那一段 —— 也就是 `capture_current_layout` 取的是真窗口、`apply_workspace`
    真的挪动了真窗口、以及设置文件的往返。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.settings = os.path.join(self.tmp.name, "winmon_settings.json")
        self.targets: list = []

    def _make_app(self):
        import tkinter as tk
        root = tk.Tk()
        root.withdraw()
        old = os.environ.get("WINMON_SETTINGS")
        os.environ["WINMON_SETTINGS"] = self.settings
        try:
            app = winmon.WinMonApp(root)
        finally:
            if old is None:
                os.environ.pop("WINMON_SETTINGS", None)
            else:
                os.environ["WINMON_SETTINGS"] = old
        self.addCleanup(h.shutdown_app, app)
        return root, app

    def _spawn(self, title):
        path = os.path.join(self.tmp.name, f"{title}.py")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(WS_TARGET_SCRIPT)
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        proc = subprocess.Popen([sys.executable, path, title],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                creationflags=flags)
        h.spawn_cleanup(self, proc)
        for _ in range(60):
            if proc.poll() is not None:
                self.skipTest("靶子进程提前退出")
            wi = h.pick_window_for_pid(proc.pid)
            if wi is not None:
                time.sleep(0.3)
                return proc, wi.hwnd
            time.sleep(0.2)
        self.skipTest("没能创建靶子窗口")
        return proc, 0

    def _wait_until(self, cond, timeout=8.0):
        """等一个条件成立（**用 after 泵事件循环**，不能 sleep 死等）。"""
        root = getattr(self, "_root", None)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if root is not None:
                try:
                    root.update()
                except Exception:
                    pass
            if cond():
                return True
            time.sleep(0.05)
        return False

    def test_capture_uses_real_windows_and_rects(self):
        root, app = self._make_app()
        self._root = root
        _proc, _hwnd = self._spawn("WsReal-Capture")
        app.refresh_all()
        items = app.capture_current_layout()
        self.assertTrue(items, "一个窗口都没拍到")
        names = {it["name"].lower() for it in items}
        self.assertIn("python.exe", names)
        for it in items:
            with self.subTest(name=it["name"]):
                self.assertEqual(len(it["rect"]), 4)
                self.assertGreaterEqual(it["rect"][2], core.WS_MIN_W)

    def test_winmon_itself_is_not_captured(self):
        """拍布局不能把自己拍进去 —— 否则"恢复布局"会把 WinMon 自己的窗口挪走。"""
        root, app = self._make_app()
        self._root = root
        app.refresh_all()
        items = app.capture_current_layout()
        titles = [it.get("title") or "" for it in items]
        self.assertFalse([t for t in titles if t.startswith(core.WINMON_TITLE_PREFIX)],
                         f"WinMon 自己被抓进了快照：{titles}")

    def test_saved_workspace_is_written_to_disk(self):
        root, app = self._make_app()
        self._root = root
        app.workspaces.append(core.capture_workspace(
            "写代码", [{"name": "Code.exe", "title": "t", "rect": [10, 20, 800, 600],
                        "iconic": False}], now=5.0))
        app.save_settings_now()
        raw = open(self.settings, encoding="utf-8").read()
        self.assertIn("写代码", raw, "工作区没有写进设置文件")

    def test_workspaces_are_loaded_on_startup(self):
        """
        反过来的一半：设置文件里已经有工作区时，启动后要能看到。

        注意**不能在同一个进程里开两个 Tk root**（第一个 root 的图标 `PhotoImage`
        会让第二个 root 里的控件报 `image "pyimageN" doesn't exist`）——
        那是测试环境的限制，不是产品缺陷（产品一次只跑一个界面）。
        所以这里先把设置写好、再只创建一个 app。
        """
        with open(self.settings, "w", encoding="utf-8") as fh:
            fh.write(json.dumps({"workspaces": [
                {"name": "看视频", "saved_at": 3.0,
                 "entries": [{"name": "vlc.exe", "title": "t",
                              "rect": [1, 2, 700, 500], "iconic": True}]}]}))
        _root, app = self._make_app()
        self.assertEqual([w["name"] for w in app.workspaces], ["看视频"])
        self.assertEqual(app.workspaces[0]["entries"][0]["rect"], [1, 2, 700, 500])

    def test_corrupt_workspaces_in_settings_do_not_break_startup(self):
        """设置文件被手工改坏时，程序要能照常起来（丢掉坏数据而不是崩）。"""
        with open(self.settings, "w", encoding="utf-8") as fh:
            fh.write('{"workspaces": "这不是列表", "idle_snooze": 42}')
        _root, app = self._make_app()
        self.assertEqual(app.workspaces, [])
        self.assertEqual(app.idle_snooze_choice.get(), "1 小时")

    def test_applying_a_workspace_really_moves_the_window(self):
        """真·端到端：把窗口挪走 → 应用工作区 → **回读核对它真的回来了**。"""
        root, app = self._make_app()
        self._root = root
        _proc, hwnd = self._spawn("WsReal-Apply")
        app.refresh_all()
        target = [it for it in app.capture_current_layout()
                  if "WsReal-Apply" in (it.get("title") or "")]
        if not target:
            self.skipTest("靶子窗口没进列表（可能没有图形会话）")
        snap = core.capture_workspace("回归", [
            {"name": target[0]["name"], "title": target[0]["title"],
             "rect": [120, 140, 460, 360], "iconic": False}])
        app.workspaces = [snap]
        app.apply_workspace(snap)
        self.assertTrue(
            self._wait_until(lambda: abs((core.get_window_rect(hwnd) or [9999])[0] - 120) < 24),
            f"应用工作区之后窗口没挪到位：rect={core.get_window_rect(hwnd)}"
            f"　状态栏={app.status_text.get()!r}")

    def test_apply_with_nothing_running_says_so_and_touches_nothing(self):
        root, app = self._make_app()
        self._root = root
        app.refresh_all()
        snap = core.capture_workspace("做梦", [
            {"name": "definitely-not-running-xyz.exe", "title": "x",
             "rect": [0, 0, 800, 600], "iconic": False}])
        app.apply_workspace(snap)
        self.assertIn("一个都没开", app.status_text.get())


def own_hwnd(app) -> int:
    """当前 WinMon 主窗口的句柄。"""
    try:
        return int(app.root.winfo_id())
    except Exception:
        return 0

class TestWorkspaceDefaults(unittest.TestCase):
    """默认值：不该有任何东西在用户没点过的情况下动窗口。"""

    def setUp(self):
        import tkinter as tk

        try:
            self.root = tk.Tk()
            self.root.withdraw()
        except Exception as exc:                              # pragma: no cover
            self.skipTest(f"没有图形会话：{exc}")
        self.addCleanup(self.root.destroy)

    def test_defaults(self):
        app = winmon.WinMonApp.__new__(winmon.WinMonApp)
        winmon.WinMonApp._init_video_vars(app)
        self.assertEqual(app.workspaces, [], "不该预置任何工作区")
        self.assertFalse(app.ws_minimize_others.get(),
                         "「顺带最小化其它窗口」必须默认关闭")


if __name__ == "__main__":
    unittest.main(verbosity=2)
