# -*- coding: utf-8 -*-
"""
A2 —— 核心层：窗口枚举、进程采集、句柄池。

覆盖 HANDOFF.md「三、踩过的坑」里的 Win32 条目：
  · 坑 4：64 位下 GDI/USER 句柄高 32 位符号扩展，必须用 H() 掩码
  · 坑 5：不能用 WaitForSingleObject 判活（缺 SYNCHRONIZE 权限会全判死），
          改用 GetExitCodeProcess + STILL_ACTIVE
  · 坑 6：ctypes.c_long 在 64 位 Windows 上是 8 字节，POINT/RECT 必须用 w.LONG
  · 坑 10：cloaked 隐藏窗口（UWP/输入法）不能算成可见程序
  · 坑 11：UWP 外壳窗口 ApplicationFrameWindow 与真实进程是两个进程
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as w
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import _helpers as h                     # noqa: E402
import winmon_core as core               # noqa: E402


# ===========================================================================
# 句柄归一化与句柄池
# ===========================================================================
class TestHandleNormalization(unittest.TestCase):
    """坑 4：句柄高 32 位是符号扩展的垃圾值，必须掩码。"""

    def test_masks_sign_extended_handle(self):
        self.assertEqual(core.H(0xFFFFFFFFA901105F), 0xA901105F)

    def test_zero_and_none(self):
        self.assertEqual(core.H(0), 0)
        self.assertEqual(core.H(None), 0)

    def test_accepts_c_void_p(self):
        self.assertEqual(core.H(ctypes.c_void_p(0xFFFFFFFFA901105F)), 0xA901105F)

    def test_masked_value_fits_handle_types(self):
        """掩码后的值必须能塞进 wintypes.HDC/HWND 等句柄类型。"""
        raw = 0xFFFFFFFFA901105F
        masked = core.H(raw)
        for typ in (w.HDC, w.HWND, w.HANDLE, w.HGDIOBJ):
            with self.subTest(type=typ.__name__):
                typ(masked)                      # 不抛异常即通过

    def test_mask_is_idempotent_and_32bit(self):
        """
        掩码后必须落在 32 位内，且再次掩码不变。

        判据用"落在 0..2^32-1"而不是"一定抛 OverflowError"：
        不同 ctypes 版本对 c_void_p 子类的越界行为不完全一致，
        真正要保证的不变量是"送进 Win32 的值永远是 32 位句柄值"。
        """
        for raw in (0xFFFFFFFFA901105F, 0x10000, 0, 0xFFFFFFFF, 0x1_0000_ABCD):
            with self.subTest(raw=hex(raw)):
                m = core.H(raw)
                self.assertGreaterEqual(m, 0)
                self.assertLessEqual(m, 0xFFFFFFFF)
                self.assertEqual(core.H(m), m, "H() 不是幂等的")


class TestProcessHandlePool(unittest.TestCase):
    """句柄池：缓存、复用、失效回收（HANDOFF 自检里有 stats 输出）。"""

    def test_acquire_current_process(self):
        pool = core.ProcessHandlePool()
        try:
            h, ct = pool.acquire(os.getpid())
            self.assertTrue(h, "无法打开自身进程句柄")
            self.assertTrue(ct, "拿不到创建时间，句柄不会入池")
            h2, ct2 = pool.acquire(os.getpid())
            self.assertEqual((h, ct), (h2, ct2), "第二次 acquire 应命中缓存")
            self.assertEqual(pool.stats()["cached"], 1)
        finally:
            pool.close_all()

    def test_acquire_invalid_pid_returns_zero(self):
        pool = core.ProcessHandlePool()
        try:
            self.assertEqual(pool.acquire(0), (0, 0))
            self.assertEqual(pool.acquire(-1), (0, 0))
        finally:
            pool.close_all()

    def test_dead_pid_is_dropped_and_counted(self):
        """
        进程退出后，池里该条必须失效重开而不是继续用旧句柄。

        这是真实存在的坑类：PID 会被复用（reuse_detected 就是为此计数）。
        """
        proc = h.spawn_cleanup(self, h.sleep_child())
        self.assertTrue(h.wait_alive(proc.pid), "测试子进程没起来")
        pool = core.ProcessHandlePool()
        try:
            h1, ct1 = pool.acquire(proc.pid)
            self.assertTrue(h1)
            proc.kill()
            proc.wait(timeout=5)
            self.assertTrue(h.wait_dead(proc.pid), "子进程没退出")
            self.assertGreaterEqual(pool.purge_dead(), 1, "purge_dead 没清掉已退出的进程")
            self.assertEqual(pool.stats()["cached"], 0)
        finally:
            pool.close_all()

    def test_release_temp_only_closes_temp(self):
        """release_temp 只关闭"不入池的临时句柄"，对池内句柄必须无动作。"""
        pool = core.ProcessHandlePool()
        try:
            hh, ct = pool.acquire(os.getpid())
            self.assertTrue(ct)
            core.ProcessHandlePool.release_temp(hh, ct)       # 不应关闭
            hh2, _ = pool.acquire(os.getpid())
            self.assertEqual(hh, hh2, "池内句柄被 release_temp 误关了")
        finally:
            pool.close_all()

    def test_shared_pool_is_reused_by_helpers(self):
        """query_process_detail / query_process_cpu_time 共用全局池。"""
        path, mem = core.query_process_detail(os.getpid())
        self.assertTrue(path, "取不到自身可执行文件路径")
        self.assertTrue(path.lower().endswith(("python.exe", "pythonw.exe", "py.exe")),
                        f"自身路径异常：{path}")
        self.assertGreater(mem, 0, "工作集内存应大于 0")
        cpu = core.query_process_cpu_time(os.getpid())
        self.assertIsNotNone(cpu)
        self.assertGreaterEqual(cpu, 0.0)

    def test_private_memory_is_available_and_smaller_than_working_set(self):
        """
        私有提交（任务管理器「提交大小」列的口径）必须真的读得到。

        为什么要单独钉这一条：WinMon 早期只显示工作集（**含与其它进程共享的
        DLL / 共享内存页**），拿它和任务管理器的数字一对，用户会以为"WinMon 不准"。
        实测本机 Edge 主进程：工作集 238MB、私有提交 111MB、私有工作集 63MB ——
        三个数差得远，必须把口径摆出来，而不是只给一个数字。

        **注意私有提交可以大于工作集**（实测 SystemSettings.exe：工作集 31MB、
        私有提交 126MB）：一个是"已提交的私有内存"，一个是"其中驻留在物理内存里的"，
        提交了但被换出/尚未触碰的页不计入工作集。所以这里**不**断言大小关系。
        """
        _path, wss, private = core.query_process_detail(os.getpid(), with_private=True)
        self.assertGreater(wss, 0, "工作集应大于 0")
        self.assertGreater(private, 0, "私有提交应大于 0（读不到就是结构体定义或 argtypes 错了）")

    def test_plain_detail_call_still_returns_two_values(self):
        """默认调用保持两元组 —— 别把老调用方（含测试）静默改坏。"""
        got = core.query_process_detail(os.getpid())
        self.assertEqual(len(got), 2)


# ===========================================================================
# 进程快照 / 存活判定
# ===========================================================================
class TestProcessSnapshot(unittest.TestCase):
    def test_snapshot_contains_self(self):
        procs = core.snapshot_processes()
        self.assertGreater(len(procs), 5, "进程快照数量异常地少")
        me = procs.get(os.getpid())
        self.assertIsNotNone(me, "快照里没有自己")
        self.assertTrue(me["name"], "进程名为空")
        self.assertTrue(me["name"].lower().endswith(".exe"),
                        f"进程名应带 .exe：{me['name']}")
        self.assertIsInstance(me["mem_kb"], int)

    def test_snapshot_ppid_matches_independent_enumeration(self):
        """
        用独立的 Toolhelp 实现交叉验证 ppid。

        刻意不复用 core 的实现：拿被测代码验证被测代码等于没验。
        """
        mine = core.snapshot_processes()
        theirs = h.independent_ppid_map()
        self.assertGreater(len(theirs), 5)
        checked = 0
        for pid, info in mine.items():
            if pid in theirs:
                self.assertEqual(info["ppid"], theirs[pid],
                                 f"PID {pid} 的父进程不一致")
                checked += 1
        self.assertGreater(checked, 5, "没比对上几个进程，交叉验证没起作用")

    def test_self_alive_and_dead_pid(self):
        """坑 5：判活必须用 GetExitCodeProcess，不能因缺权限把活进程判死。"""
        self.assertTrue(core.is_process_alive(os.getpid()),
                        "把自己判成已退出 —— 这正是坑 5 的症状")
        # 找一个几乎不可能存在的 PID
        self.assertFalse(core.is_process_alive(0xFFFF0000))

    def test_alive_after_terminate(self):
        proc = h.spawn_cleanup(self, h.sleep_child())
        self.assertTrue(h.wait_alive(proc.pid), "测试子进程没起来")
        self.assertTrue(core.is_process_alive(proc.pid))
        proc.kill()
        proc.wait(timeout=5)
        self.assertTrue(h.wait_dead(proc.pid))

    def test_ppid_of_child(self):
        proc = h.spawn_cleanup(self, h.sleep_child())
        self.assertTrue(h.wait_alive(proc.pid))
        self.assertEqual(core.query_process_ppid(proc.pid), os.getpid(),
                         "子进程的父 PID 应该是本测试进程")


class TestProcessTree(unittest.TestCase):
    def test_build_tree_has_no_cycles(self):
        procs = core.snapshot_processes()
        children = core.build_process_tree(procs)
        self.assertIn(os.getpid(), procs)
        # 从自己往上走，必须能在有限步内到达根（否则就是环）
        cur = os.getpid()
        seen = set()
        depth = 0
        while cur and cur not in seen and depth < 200:
            seen.add(cur)
            cur = procs.get(cur, {}).get("ppid", 0)
            depth += 1
        self.assertLess(depth, 200, "进程树里存在环")

    def test_descendants_of_child(self):
        """spawn 出来的子进程必须出现在自己的子孙清单里。"""
        import time
        proc = h.spawn_cleanup(self, h.sleep_child())
        self.assertTrue(h.wait_alive(proc.pid))
        for _ in range(20):                      # 父子关系建立可能略有延迟
            procs = core.snapshot_processes()
            desc = core.collect_descendants(os.getpid(), core.build_process_tree(procs))
            if proc.pid in desc:
                break
            time.sleep(0.1)
        self.assertIn(proc.pid, desc, "子进程没有出现在自己的子孙清单里")

    def test_process_depth_is_sane(self):
        procs = core.snapshot_processes()
        d_self = core._process_depth(os.getpid(), procs)
        self.assertGreaterEqual(d_self, 0)
        self.assertLess(d_self, 64)


# ===========================================================================
# 窗口枚举
# ===========================================================================
class TestWindowEnumeration(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.wins = core.enum_windows(include_cloaked=True)

    def test_windows_found(self):
        self.assertGreater(len(self.wins), 0, "一个窗口都没枚举到")

    def test_hwnd_unique(self):
        hwnds = [wi.hwnd for wi in self.wins]
        self.assertEqual(len(hwnds), len(set(hwnds)), "存在重复的 hwnd")

    def test_fields_are_sane(self):
        for wi in self.wins:
            with self.subTest(hwnd=hex(wi.hwnd)):
                self.assertIsInstance(wi.hwnd, int)
                self.assertGreater(wi.hwnd, 0)
                self.assertIsInstance(wi.title, str)
                self.assertNotIn("\x00", wi.title, "窗口标题里混进了 NUL")
                self.assertIsInstance(wi.cls, str)
                self.assertGreaterEqual(wi.rect_w, 0)
                self.assertGreaterEqual(wi.rect_h, 0)
                self.assertIsInstance(wi.iconic, bool)
                self.assertIsInstance(wi.cloaked, bool)
                self.assertTrue(wi.real_name, "real_name 为空")
                self.assertTrue(wi.state)

    def test_self_window_is_found(self):
        """
        本测试进程自己开的顶层窗口必须能被枚举到。

        这条把"枚举到的是别人的窗口"变成可控前提：窗口归属必须正确。
        """
        ok, why = h.tk_state()
        if not ok:
            self.skipTest(why)
        import tkinter as tk
        root = tk.Tk()
        try:
            root.title("WinMon-Test-Sentinel")
            root.geometry("260x120+40+40")
            root.update()
            own = core.H(core.user32.GetAncestor(core.w.HWND(root.winfo_id()),
                                                 core.GA_ROOT))
            found = [wi for wi in core.enum_windows(include_cloaked=True)
                     if wi.pid == os.getpid()]
            self.assertTrue(found, "没枚举到本进程自己的窗口")
            self.assertIn(own, [wi.hwnd for wi in found],
                          "本进程顶层窗口没出现在枚举结果里")
            sentinel = [wi for wi in found if wi.hwnd == own][0]
            self.assertEqual(sentinel.title, "WinMon-Test-Sentinel")
        finally:
            root.destroy()

    def test_include_cloaked_flag_is_not_smaller(self):
        """include_cloaked=False 应当是 True 的子集。"""
        all_w = core.enum_windows(include_cloaked=True)
        vis_w = core.enum_windows(include_cloaked=False)
        self.assertLessEqual(len(vis_w), len(all_w))
        all_hwnds = {wi.hwnd for wi in all_w}
        for wi in vis_w:
            self.assertIn(wi.hwnd, all_hwnds)

    def test_hidden_surface_is_consistent(self):
        """坑 10：cloaked 窗口必须被标成隐藏，且不能出现在"只列可见"的结果里。"""
        for wi in self.wins:
            with self.subTest(hwnd=hex(wi.hwnd)):
                if wi.cloaked:
                    self.assertTrue(wi.is_hidden_surface)
                    self.assertEqual(wi.state, "后台隐藏")
        visible = core.collect_desktop(include_cloaked=False, with_detail=False)
        self.assertTrue(all(not x.is_hidden_surface for x in visible),
                        "collect_desktop(include_cloaked=False) 里混进了隐藏窗口")

    def test_frame_host_uses_shell_process(self):
        """
        坑 11：ApplicationFrameWindow 是外壳窗口。

        `WinInfo.real_name` 对这类窗口要改用窗口标题（外壳进程名没意义）；
        「（应用外壳）」这个后缀由界面层拼，不在核心层。
        """
        hosts = [wi for wi in self.wins if wi.cls == "ApplicationFrameWindow"]
        for wi in hosts:
            with self.subTest(hwnd=hex(wi.hwnd)):
                self.assertTrue(wi.is_frame_host)
                self.assertEqual(wi.real_name, wi.title,
                                 "外壳窗口的 real_name 应该是窗口标题")
                self.assertNotIn("ApplicationFrameHost", wi.real_name)

    def test_real_name_never_looks_like_path(self):
        """坑 12：标题其实是文件路径的窗口，real_name 不能被当成软件名。"""
        for wi in self.wins:
            with self.subTest(hwnd=hex(wi.hwnd)):
                if core._looks_like_path_title(wi.title):
                    self.assertNotEqual(wi.real_name, wi.title.strip())

    def test_collect_desktop_summary(self):
        summary = core.get_system_summary()
        self.assertIn("visible_windows", summary)
        self.assertIn("hidden_windows", summary)
        self.assertIn("apps", summary)
        self.assertGreaterEqual(summary["visible_windows"], 0)
        self.assertGreaterEqual(summary["hidden_windows"], 0)
        self.assertIsInstance(summary["apps"], list)

    def test_detail_collection_fills_both_memory_metrics(self):
        """
        `collect_desktop(with_detail=True)` 必须把**两个内存口径**都填上。

        界面列表显示的是「工作集/私有提交」，进程树和悬停提示也都要这两个数 ——
        少填一个，用户看到的就是半句话（列表里只剩一个数字、或者显示成「—」）。
        """
        wins = core.collect_desktop(include_cloaked=True, with_detail=True)
        have = [wi for wi in wins if wi.mem_kb > 0]
        if not have:
            self.skipTest("当前没有能读到内存的窗口")
        missing = [wi.proc for wi in have if wi.mem_private_kb <= 0]
        self.assertEqual(missing[:5], [],
                         f"这些窗口只填了工作集、没填私有提交：{missing[:5]}")
        for wi in have[:20]:
            with self.subTest(app=wi.proc):
                self.assertIsInstance(wi.mem_private_kb, int)
                self.assertGreater(wi.mem_private_kb, 0)
                # **不**比较两者大小：私有提交可以大于工作集
                # （实测 SystemSettings.exe 工作集 31MB、私有提交 126MB），
                # 一个是"提交了多少"，一个是"其中驻留了多少"。


class TestPathLikeTitle(unittest.TestCase):
    """坑 12 的纯逻辑部分：哪些标题应当被识别成"其实是路径"。"""

    CASES_TRUE = [
        r"X:\Program Files\bilibili\哔哩哔哩.exe",
        r'"C:\Program Files\App\app.exe"',
        r"C:/Program Files/App/app.exe",
        r"X:\Tools\x.lnk",
        r"C:\a\b.cmd",
        r"X:\setup.msi",
        r"C:\x\y.BAT",
    ]
    CASES_FALSE = [
        "",
        "记事本",
        "ai",                       # 太短，不做判断
        "Visual Studio Code",
        "哔哩哔哩 (゜-゜)つロ 干杯~-bilibili",
        "设置",
        "C:\\",
    ]

    #: 已知会误判成路径的标题 —— 记录在案而不是假装不存在。
    #: 判据是"同时含 :/ 与 /"，"C:/ 磁盘"这种带盘符的口语化标题会命中；
    #: 真实软件标题不会长这样，所以暂不为它加复杂度（加了反而会漏掉真路径）。
    KNOWN_FALSE_POSITIVES = ["C:/ 磁盘"]

    def test_true_cases(self):
        for t in self.CASES_TRUE:
            with self.subTest(title=t):
                self.assertTrue(core._looks_like_path_title(t), f"应判为路径：{t}")

    def test_false_cases(self):
        for t in self.CASES_FALSE:
            with self.subTest(title=t):
                self.assertFalse(core._looks_like_path_title(t), f"不应判为路径：{t}")

    def test_known_false_positives_are_still_recognized(self):
        """把已知误判钉住：如果哪天行为变了，这里会提醒更新文档。"""
        for t in self.KNOWN_FALSE_POSITIVES:
            with self.subTest(title=t):
                self.assertTrue(core._looks_like_path_title(t),
                                f"{t!r} 的行为变了，请同步更新注释与文档")


if __name__ == "__main__":
    unittest.main(verbosity=2)
