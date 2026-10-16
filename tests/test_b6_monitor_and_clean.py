# -*- coding: utf-8 -*-
"""
B6 —— 「监视当前窗口」与「清理程序文件」。

覆盖本次新增的两块：

1. **视频联动不再只认某个 App**：选中哪个窗口就判断哪个窗口是不是在放视频
   （`probe_window_as_video`），并用两个选项收窄范围 ——
   「仅监视视频类 App」与「指定 App」。测试要点：
     · 过滤在**跑检测之前**就生效（不是视频类 App 连画面都不用抓）；
     · 真开一个"在动的窗口"也能被判成视频（不依赖任何特定 App）；
     · 勾上「仅视频类 App」后，同一个会动的窗口（靶子是 python.exe，属"其他程序"）
       立刻不再被响应 —— 这就是"仅针对 app 响应"。

2. **「清理程序文件」**：只清程序自己产生的文件；
   `winmon_settings.json`（收藏/分类/最近使用）是**用户数据**，默认不勾，
   而且 `clean_files()` 只删清单里解析出来的路径，不可能删到别的东西。
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import _helpers as h                       # noqa: E402
import winmon_core as core                 # noqa: E402
from test_b5_video_link import ANIMATED_WINDOW_SCRIPT   # noqa: E402


class FakeWi:
    """假窗口：过滤分支在跑检测之前就返回，所以这里只需要几个字段。"""

    def __init__(self, proc: str, pid: int = 1, title: str = "",
                 path: str = "", hwnd: int = 0) -> None:
        self.proc = proc
        self.pid = pid
        self.title = title
        self.path = path
        self.hwnd = hwnd
        self.rect_w = 800
        self.rect_h = 600
        self.iconic = False


# ===========================================================================
# 1. 两个筛选选项（纯逻辑：不碰窗口就返回）
# ===========================================================================
class TestProbeFilters(unittest.TestCase):
    def test_only_app_filter_rejects_other_processes(self):
        probe = core.probe_window_as_video(FakeWi("notepad.exe"), require_video_app=False,
                                           only_app="msedge.exe")
        self.assertFalse(probe["is_video"])
        self.assertIn("指定的 App", probe["reason"])
        self.assertIn("msedge.exe", probe["reason"])

    def test_only_app_filter_accepts_the_named_process(self):
        """指定 msedge.exe 时，msedge 自己的窗口不会被这一条挡掉。"""
        probe = core.probe_window_as_video(FakeWi("msedge.exe", hwnd=0), only_app="msedge.exe")
        self.assertNotIn("指定的 App", probe["reason"])   # 会继续往下走（后面因无窗口而失败）

    def test_require_video_app_rejects_unknown_programs(self):
        probe = core.probe_window_as_video(FakeWi("notepad.exe"), require_video_app=True)
        self.assertFalse(probe["is_video"])
        self.assertIn("仅视频类 App", probe["reason"])

    def test_require_video_app_lets_known_apps_through(self):
        for proc in ("msedge.exe", "哔哩哔哩.exe", "vlc.exe"):
            probe = core.probe_window_as_video(FakeWi(proc), require_video_app=True)
            self.assertNotIn("仅视频类 App", probe["reason"], proc)

    def test_no_filters_means_any_window_is_considered(self):
        probe = core.probe_window_as_video(FakeWi("notepad.exe"))
        self.assertNotIn("仅视频类 App", probe["reason"])
        self.assertNotIn("指定的 App", probe["reason"])


# ===========================================================================
# 2. 真窗口：能判成视频；勾上"仅视频类 App"后立刻不响应
# ===========================================================================
@h.needs_tk
class TestProbeRealWindow(unittest.TestCase):
    """真开一个"画面在动"的窗口 —— 它不属任何视频类 App，正好检验两个选项。"""

    def _spawn(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = os.path.join(tmp.name, "target.py")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(ANIMATED_WINDOW_SCRIPT)
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        proc = subprocess.Popen([sys.executable, path, "WinMon-Probe-Target"],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                creationflags=flags)
        h.spawn_cleanup(self, proc)
        for _ in range(60):
            wi = h.pick_window_for_pid(proc.pid)
            if wi is not None and wi.rect_w >= 300:
                return proc, wi
            time.sleep(0.2)
        self.skipTest("没能创建靶子窗口（环境没有图形会话）")

    def test_moving_window_is_detected_as_video(self):
        _proc, wi = self._spawn()
        probe = core.probe_window_as_video(wi, probe_seconds=2.0)
        if not probe["detail"]["detector"].get("series"):
            self.skipTest("这个环境抓不到该窗口的画面变化")
        self.assertTrue(probe["is_video"], f"没判成视频：{probe['reason']}")
        self.assertIn("判定为视频", probe["reason"])
        self.assertTrue(probe["detail"]["evidence"], "没有留下判定依据")

    def test_require_video_app_skips_the_same_window(self):
        """同一个会动的窗口：不勾选项 → 判成视频；勾上「仅视频类 App」→ 不响应。"""
        _proc, wi = self._spawn()
        free = core.probe_window_as_video(wi, probe_seconds=2.0)
        if not free["detail"]["detector"].get("series"):
            self.skipTest("这个环境抓不到该窗口的画面变化")
        strict = core.probe_window_as_video(wi, probe_seconds=2.0, require_video_app=True)
        self.assertTrue(free["is_video"], "不勾选项时应当判成视频")
        self.assertFalse(strict["is_video"], "勾上「仅视频类 App」后不该再响应")
        self.assertIn("仅视频类 App", strict["reason"])


# ===========================================================================
# 3. 唤起最小化的窗口
# ===========================================================================
@h.needs_tk
class TestRestoreWindow(unittest.TestCase):
    def test_restore_minimized_window(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = os.path.join(tmp.name, "target.py")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(ANIMATED_WINDOW_SCRIPT)
        proc = subprocess.Popen(
            [sys.executable, path, "WinMon-Restore-Target"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        h.spawn_cleanup(self, proc)
        wi = None
        for _ in range(60):
            wi = h.pick_window_for_pid(proc.pid)
            if wi is not None and wi.rect_w >= 300:
                break
            wi = None
            time.sleep(0.2)
        if wi is None:
            self.skipTest("没能创建靶子窗口（环境没有图形会话）")

        # 用系统调用把它最小化（模拟"用户在用的 App 被最小化了"）
        hwnd = core.w.HWND(wi.hwnd)
        core.user32.ShowWindow(hwnd, 6)          # SW_MINIMIZE
        time.sleep(0.6)
        self.assertTrue(bool(core.user32.IsIconic(hwnd)), "靶子窗口没有被最小化")

        ok, msg = core.restore_window(wi)
        self.assertTrue(ok, msg)
        time.sleep(0.6)
        self.assertFalse(bool(core.user32.IsIconic(hwnd)), "调用后窗口仍是 minimized")
        self.assertTrue(bool(core.user32.IsWindowVisible(hwnd)), "调用后窗口不可见")

    def test_restore_bogus_window_is_honest(self):
        ok, msg = core.restore_window(FakeWi("notepad.exe", hwnd=0xFFFF0000))
        self.assertFalse(ok)
        self.assertIn("已经不在了", msg)


# ===========================================================================
# 4. 清理程序文件
# ===========================================================================
def _make_fake_project() -> tuple[str, tempfile.TemporaryDirectory]:
    """搭一个"假项目目录"：缓存 / 日志 / 字节码 / 设置都有内容。"""
    tmp = tempfile.TemporaryDirectory()
    root = tmp.name
    for rel, body in (("winmon_catalog.json", "{}"),
                      ("winmon_error.log", "boom\n"),
                      ("winmon_settings.json", '{"favorites": 1}'),
                      (os.path.join("__pycache__", "winmon_core.cpython-314.pyc"), "x" * 50),
                      (os.path.join("tests", "__pycache__", "test_a1.cpython-314.pyc"), "y" * 30)):
        full = os.path.join(root, rel)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w", encoding="utf-8") as fh:
            fh.write(body)
    return root, tmp


class TestCleanableFiles(unittest.TestCase):
    def test_listing_reports_counts_sizes_and_defaults(self):
        root, tmp = _make_fake_project()
        self.addCleanup(tmp.cleanup)
        items = {d["key"]: d for d in core.list_cleanable_files(root=root)}
        self.assertEqual(items["catalog_cache"]["count"], 1)
        self.assertTrue(items["catalog_cache"]["default"])
        self.assertEqual(items["pycache"]["count"], 1)
        self.assertEqual(items["pycache"]["bytes"], 50)
        self.assertEqual(items["tests_pycache"]["count"], 1)
        # **用户数据默认不勾选** —— 这是"清理"最容易出事的地方
        self.assertFalse(items["settings"]["default"], "设置文件不该默认勾选")
        self.assertTrue(items["settings"]["exists"])
        self.assertIn("用户数据", items["settings"]["note"])

    def test_listing_never_looks_outside_the_project(self):
        root, tmp = _make_fake_project()
        self.addCleanup(tmp.cleanup)
        outside = os.path.join(tmp.name, "..", "not-mine.txt")
        with open(os.path.abspath(outside), "w", encoding="utf-8") as fh:
            fh.write("keep me")
        self.addCleanup(lambda: os.path.exists(outside) and os.remove(outside))
        listed = [p for d in core.list_cleanable_files(root=root) for p in d["paths"]]
        self.assertNotIn(os.path.abspath(outside), listed)

    def test_clean_defaults_removes_cache_but_keeps_settings(self):
        root, tmp = _make_fake_project()
        self.addCleanup(tmp.cleanup)
        keys = [d["key"] for d in core.list_cleanable_files(root=root) if d["default"]]
        removed, freed, failures = core.clean_files(keys, root=root)
        self.assertEqual(failures, [])
        self.assertGreaterEqual(removed, 3)
        self.assertGreater(freed, 0)
        self.assertFalse(os.path.exists(os.path.join(root, "winmon_catalog.json")))
        self.assertFalse(os.path.exists(os.path.join(root, "__pycache__")))
        self.assertTrue(os.path.exists(os.path.join(root, "winmon_settings.json")),
                        "默认清理把用户设置删掉了！")

    def test_clean_settings_only_when_asked(self):
        root, tmp = _make_fake_project()
        self.addCleanup(tmp.cleanup)
        removed, _freed, failures = core.clean_files(["settings"], root=root)
        self.assertEqual(failures, [])
        self.assertEqual(removed, 1)
        self.assertFalse(os.path.exists(os.path.join(root, "winmon_settings.json")))

    def test_unknown_key_is_reported_not_deleted(self):
        root, tmp = _make_fake_project()
        self.addCleanup(tmp.cleanup)
        removed, freed, failures = core.clean_files(["../../etc/passwd"], root=root)
        self.assertEqual((removed, freed), (0, 0))
        self.assertTrue(failures, "未知清理项必须报出来")
        self.assertTrue(os.path.exists(os.path.join(root, "winmon_catalog.json")))

    def test_second_clean_finds_nothing_to_do(self):
        root, tmp = _make_fake_project()
        self.addCleanup(tmp.cleanup)
        keys = [d["key"] for d in core.list_cleanable_files(root=root) if d["default"]]
        core.clean_files(keys, root=root)
        removed, _freed, failures = core.clean_files(keys, root=root)
        self.assertEqual(removed, 0)
        self.assertEqual(failures, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
