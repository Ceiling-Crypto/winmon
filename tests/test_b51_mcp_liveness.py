# -*- coding: utf-8 -*-
"""
B51 —— 2026-10-24：MCP「已接入」必须看进程还活着，不能只看时间戳。

用户实测：删掉客户端的 MCP 配置、重启客户端后，WinMon 仍然显示
「已接入 · MCP」，因为它当时只判断「最近 15 分钟有没有调用过」。
这次改成：

  · MCP 服务器启动时写一份存活登记（pid + 客户端名），退出时删掉；
  · 界面只有 **pid 还活着** 才说「已接入」；
  · 时间戳只用来解释「上次是哪会儿」→「未接入 · 上次 MCP（03:11）」。

这一组同时钉住：登记的读写/清理、死 pid 不算接入、判定函数的三态、
以及「已接入」文案不会再被 stale 记录顶替。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import winmon                              # noqa: E402
import winmon_ai as ai                     # noqa: E402
import winmon_mcp as mcp                   # noqa: E402


def use_temp_data_dir(case):
    """
    给某个用例换一个临时数据目录（别碰用户真实的 WinMon 数据）。

    为什么写成函数而不是公共基类：lint 的「可能死代码」只认直接继承
    `unittest.TestCase` 的类，绕一层基类会被误报成没人用。
    """
    tmp = tempfile.TemporaryDirectory(prefix="winmon-b51-")
    case.addCleanup(tmp.cleanup)
    case.data_dir = tmp.name
    old = os.environ.get("WINMON_DATA_DIR")
    os.environ["WINMON_DATA_DIR"] = tmp.name

    def restore():
        if old is None:
            os.environ.pop("WINMON_DATA_DIR", None)
        else:
            os.environ["WINMON_DATA_DIR"] = old

    case.addCleanup(restore)


class TestLiveMarker(unittest.TestCase):
    def setUp(self):
        use_temp_data_dir(self)

    def test_no_marker_means_not_connected(self):
        self.assertEqual(mcp.live_marker(), {})

    def test_roundtrip_and_clear(self):
        mcp.write_live_marker("豆包")
        data = mcp.live_marker()
        self.assertEqual(data.get("pid"), os.getpid())
        self.assertEqual(data.get("client"), "豆包")
        mcp.clear_live_marker()
        self.assertEqual(mcp.live_marker(), {})

    def test_dead_pid_is_not_connected(self):
        """进程已经不在了：登记文件还在，也不能算接入。"""
        path = mcp.live_marker_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"pid": 999999, "t": time.time(), "client": "豆包"}, fh)
        self.assertTrue(os.path.exists(path))
        self.assertEqual(mcp.live_marker(), {},
                         "pid 不存在的登记必须当作未接入")

    def test_broken_file_is_ignored(self):
        path = mcp.live_marker_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("{not json")
        self.assertEqual(mcp.live_marker(), {})

    def test_stdio_server_registers_and_cleans_up(self):
        """
        真起一个 `--mcp-stdio`：启动后登记存在，stdin 关闭后登记被删。

        这是用户那次踩坑的完整复现路径——客户端没了，登记也必须马上消失。
        """
        script = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                              "winmon.py")
        env = dict(os.environ)
        env["WINMON_DATA_DIR"] = self.data_dir
        env["PYTHONUTF8"] = "1"
        proc = subprocess.Popen([sys.executable, script, "--mcp-stdio"],
                                stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, env=env)
        try:
            deadline = time.time() + 15
            data = {}
            while time.time() < deadline:
                data = mcp.live_marker()
                if data:
                    break
                time.sleep(0.1)
            self.assertEqual(data.get("pid"), proc.pid,
                             "stdio 服务器启动后必须登记自己的 pid")
            proc.stdin.close()
            proc.wait(timeout=15)
            self.assertFalse(os.path.exists(mcp.live_marker_path()),
                             "服务器退出后必须删掉存活登记")
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=10)


class TestClassifyMcp(unittest.TestCase):
    def test_live_beats_timestamp(self):
        state = ai.classify_mcp({"t": time.time() - 600, "client": "豆包"},
                                {"pid": 1, "client": "豆包"})
        self.assertEqual(state["state"], "live")

    def test_recent_but_dead_is_stale(self):
        state = ai.classify_mcp({"t": time.time() - 60, "client": "豆包",
                                 "ts": "2026-10-24T03:11:00"}, {})
        self.assertEqual(state["state"], "stale")
        self.assertEqual(state["ts"], "2026-10-24T03:11:00")

    def test_old_record_is_none(self):
        state = ai.classify_mcp({"t": time.time() - ai.MCP_FRESH_SECONDS - 5}, {})
        self.assertEqual(state["state"], "none")

    def test_empty_is_none(self):
        self.assertEqual(ai.classify_mcp({}, {})["state"], "none")
        self.assertEqual(ai.classify_mcp(None, None)["state"], "none")


class TestStaleWording(unittest.TestCase):
    def test_stale_never_claims_connected(self):
        parts = ai.format_ai_summary(
            None, None, connection={"kind": "mcp", "stale": True, "client": "豆包",
                                    "ts": "2026-10-24T03:11:00"}, lang="zh")
        self.assertIn("上次", parts["connection"])
        self.assertNotIn("已接入", parts["connection"])
        self.assertEqual(parts["status_kind"], "local")

    def test_live_still_says_connected(self):
        parts = ai.format_ai_summary(
            None, None, connection={"kind": "mcp", "client": "豆包"}, lang="zh")
        self.assertIn("已接入", parts["connection"])
        self.assertEqual(parts["status_kind"], "mcp")


class TestSnapshotUsesLiveness(unittest.TestCase):
    """GUI 的 `_ai_connection_snapshot` 必须反映「进程在不在」。"""

    def setUp(self):
        use_temp_data_dir(self)

    class _App:
        _ai_assist = {}

        def __init__(self, last_mcp):
            self._last = last_mcp

        def _ai_status_snapshot(self):
            return {"last_mcp": self._last, "mcp_calls": 4}

        def _ai_direct_target(self):
            return (None, "")

        _ai_connection_snapshot = winmon.WinMonApp._ai_connection_snapshot
        _ai_recent_mcp_client = winmon.WinMonApp._ai_recent_mcp_client

    def test_recent_record_without_process_is_stale(self):
        app = self._App({"t": time.time() - 60, "client": "豆包",
                         "ts": "2026-10-24T03:11:00"})
        conn = app._ai_connection_snapshot()
        self.assertEqual(conn["kind"], "mcp")
        self.assertTrue(conn.get("stale"), "没有存活进程时必须标成 stale")
        self.assertEqual(app._ai_recent_mcp_client(), "",
                         "stale 不能对外宣称「已通过 MCP 接入」")

    def test_live_marker_makes_it_connected(self):
        mcp.write_live_marker("豆包")
        self.addCleanup(mcp.clear_live_marker)
        app = self._App({"t": time.time() - 60, "client": "豆包",
                         "ts": "2026-10-24T03:11:00"})
        conn = app._ai_connection_snapshot()
        self.assertEqual(conn["kind"], "mcp")
        self.assertFalse(conn.get("stale"))
        self.assertEqual(app._ai_recent_mcp_client(), "豆包")


if __name__ == "__main__":
    unittest.main()
