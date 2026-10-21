# -*- coding: utf-8 -*-
"""
B24 —— MCP 服务器（阶段 1.5）：协议 + 三个只读工具。

不接真 AI 客户端也能测：
  · `handle_request` 是纯逻辑（注入 state，毫秒级）；
  · 最后一个用例才真起 `--mcp-stdio` 子进程，验证分帧与 EOF 退出。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import _helpers as h                     # noqa: E402
import winmon_ai as ai                   # noqa: E402
import winmon_mcp as mcp                 # noqa: E402

MODERN = {"io.modelcontextprotocol/protocolVersion": mcp.MODERN_VERSION,
          "io.modelcontextprotocol/clientCapabilities": {}}


def fake_state():
    return {
        "sessions": {
            "primary": {"kind": "desktop", "name": "Claude Desktop",
                        "proc": "claude.exe", "pid": 1, "hwnd": 2,
                        "confidence": 0.98, "source": "mcp_parent",
                        "cpu_share": 0.1, "mem_share": 0.2, "score": 9.0,
                        "pinned": False},
            "others": [{"name": "Cursor", "score": 1.0}],
            "ambiguous": False, "pinned": False, "note": "假数据",
        },
        "incidents": {"incidents": [], "count": 0, "source": "fake"},
        "playbooks": {"playbooks": ai.get_playbooks()},
        "budget_status": {
            "state": "ok", "reason": "", "enabled": True, "paused": False,
            "daily_tokens": ai.DEFAULT_DAILY_TOKEN_BUDGET, "used_tokens": 0,
            "ratio": 0.0, "hourly_calls": 0, "hourly_limit": 60,
            "per_incident_tokens": 2000, "warn_ratio": 0.8,
            "reset_at": "2026-10-18T04:00:00", "day_calls": 0,
            "by_client": {}, "recent": [], "note": "假数据",
        },
    }


def req(req_id, method, params=None):
    return {"jsonrpc": "2.0", "id": req_id, "method": method,
            "params": {} if params is None else params}


class FakeBudget:
    """测试用预算替身：只关心"闸门 + 记账"是否被调用。"""

    def __init__(self, allowed: bool = True):
        self.allowed = allowed
        self.recorded: list = []
        self.settings = {"per_incident_tokens": 2000}

    def check(self, estimated: int = 0, now=None):
        status = {"state": "ok" if self.allowed else "exhausted",
                  "used_tokens": 0, "daily_tokens": 100, "ratio": 0.0,
                  "reason": "" if self.allowed else "今日预算已用尽"}
        return {"allowed": self.allowed, "state": status["state"],
                "reason": status["reason"], "status": status}

    def record(self, tokens: int, **kw):
        self.recorded.append((tokens, kw))
        return {"state": "ok", "used_tokens": tokens, "daily_tokens": 100,
                "ratio": round(tokens / 100, 3), "reason": ""}


class TestProtocol(unittest.TestCase):
    def test_legacy_initialize(self):
        resp = mcp.handle_request(req(1, "initialize", {
            "protocolVersion": "2025-11-25", "capabilities": {},
            "clientInfo": {"name": "unit-test"}}))
        res = resp["result"]
        self.assertEqual(res["protocolVersion"], "2025-11-25")
        self.assertIn("tools", res["capabilities"])
        self.assertEqual(res["serverInfo"]["name"], "winmon")
        self.assertNotIn("resultType", res, "旧时代不该发 resultType")

    def test_notification_has_no_response(self):
        self.assertIsNone(mcp.handle_request(
            {"jsonrpc": "2.0", "method": "notifications/initialized"}))

    def test_tools_list_is_stable_and_valid(self):
        resp = mcp.handle_request(req(2, "tools/list"))
        tools = resp["result"]["tools"]
        self.assertEqual(tuple(t["name"] for t in tools), mcp.TOOL_NAMES)
        for t in tools:
            with self.subTest(tool=t["name"]):
                self.assertTrue(t["description"])
                self.assertEqual(t["inputSchema"]["type"], "object")
                self.assertRegex(t["name"], r"^[A-Za-z0-9_.-]{1,128}$")

    def test_modern_tools_list_has_cache_fields(self):
        resp = mcp.handle_request(req(3, "tools/list", {"_meta": dict(MODERN)}))
        res = resp["result"]
        self.assertEqual(res["resultType"], "complete")
        self.assertEqual(res["ttlMs"], 60000)
        self.assertEqual(res["cacheScope"], "private")

    def test_server_discover(self):
        resp = mcp.handle_request(req(4, "server/discover", {"_meta": dict(MODERN)}))
        res = resp["result"]
        self.assertIn(mcp.MODERN_VERSION, res["supportedVersions"])
        self.assertIn("tools", res["capabilities"])

    def test_unsupported_version(self):
        bad = {"io.modelcontextprotocol/protocolVersion": "1900-01-01",
               "io.modelcontextprotocol/clientCapabilities": {}}
        resp = mcp.handle_request(req(5, "tools/list", {"_meta": bad}))
        self.assertEqual(resp["error"]["code"], -32022)
        self.assertIn(mcp.MODERN_VERSION, resp["error"]["data"]["supported"])

    def test_modern_requires_capabilities(self):
        resp = mcp.handle_request(req(6, "tools/list", {"_meta": {
            "io.modelcontextprotocol/protocolVersion": mcp.MODERN_VERSION}}))
        self.assertEqual(resp["error"]["code"], -32602)

    def test_unknown_method(self):
        resp = mcp.handle_request(req(7, "no/such"))
        self.assertEqual(resp["error"]["code"], -32601)

    def test_unknown_tool(self):
        resp = mcp.handle_request(req(8, "tools/call",
                                      {"name": "winmon_nope", "arguments": {}}))
        self.assertEqual(resp["error"]["code"], -32602)

    def test_invalid_arguments_are_protocol_errors(self):
        resp = mcp.handle_request(req(9, "tools/call", {
            "name": "winmon_incidents", "arguments": {"limit": "十"}}))
        self.assertEqual(resp["error"]["code"], -32602)

    def test_parse_error(self):
        line = mcp.handle_line("{oops")
        data = json.loads(line)
        self.assertEqual(data["error"]["code"], -32700)
        self.assertIsNone(data["id"])

    def test_framing_is_one_ascii_line(self):
        line = mcp.handle_line(json.dumps(req(10, "ping")))
        self.assertTrue(line)
        self.assertNotIn("\r", line)
        self.assertNotIn("\n", line)
        self.assertFalse(line.startswith("\ufeff"))
        line.encode("ascii")                 # 必须能纯 ASCII 编码


class TestTools(unittest.TestCase):
    def test_ai_sessions(self):
        resp = mcp.handle_request(req(1, "tools/call", {
            "name": "winmon_ai_sessions", "arguments": {}}), fake_state())
        res = resp["result"]
        self.assertFalse(res["isError"])
        self.assertIn("Claude Desktop", res["content"][0]["text"])
        self.assertEqual(res["structuredContent"]["primary"]["name"], "Claude Desktop")

    def test_incidents_empty_is_ok(self):
        resp = mcp.handle_request(req(2, "tools/call", {
            "name": "winmon_incidents", "arguments": {}}), fake_state())
        self.assertFalse(resp["result"]["isError"])
        self.assertEqual(resp["result"]["structuredContent"]["count"], 0)

    def test_playbooks(self):
        resp = mcp.handle_request(req(3, "tools/call", {
            "name": "winmon_playbooks", "arguments": {}}), fake_state())
        names = [b["id"] for b in resp["result"]["structuredContent"]["playbooks"]]
        self.assertIn("app_hung", names)
        self.assertIn("disk_low", names)

    def test_unknown_playbook_is_tool_error(self):
        resp = mcp.handle_request(req(4, "tools/call", {
            "name": "winmon_playbooks", "arguments": {"id": "nope"}}), fake_state())
        self.assertTrue(resp["result"]["isError"])

    def test_live_tools_do_not_crash(self):
        """不注入 state 时走真实采集；环境再差也必须返回合法结构。"""
        for name in mcp.TOOL_NAMES:
            with self.subTest(tool=name):
                resp = mcp.handle_request(req(5, "tools/call",
                                              {"name": name, "arguments": {}}))
                self.assertIn("result", resp)
                self.assertIn("content", resp["result"])

    def test_budget_status_tool(self):
        resp = mcp.handle_request(req(6, "tools/call", {
            "name": "winmon_budget_status", "arguments": {}}), fake_state())
        self.assertFalse(resp["result"]["isError"])
        budget = resp["result"]["structuredContent"]["budget"]
        self.assertEqual(budget["daily_tokens"], ai.DEFAULT_DAILY_TOKEN_BUDGET)

    def test_budget_gate_degrades_without_data(self):
        state = dict(fake_state())
        state["budget"] = FakeBudget(False)
        resp = mcp.handle_request(req(7, "tools/call", {
            "name": "winmon_ai_sessions", "arguments": {}}), state)
        result = resp["result"]
        self.assertFalse(result["isError"])
        self.assertTrue(result["structuredContent"]["degraded"])
        self.assertIn("预算", result["content"][0]["text"])
        self.assertNotIn("Claude Desktop", result["content"][0]["text"])

    def test_budget_records_estimated_usage(self):
        state = dict(fake_state())
        budget = FakeBudget(True)
        state["budget"] = budget
        resp = mcp.handle_request(req(8, "tools/call", {
            "name": "winmon_ai_sessions", "arguments": {}}), state)
        self.assertFalse(resp["result"]["isError"])
        self.assertTrue(budget.recorded, "成功返回后必须记一条用量")
        self.assertIn("budget", resp["result"]["structuredContent"])

    def test_no_tool_can_change_budget(self):
        names = " ".join(mcp.TOOL_NAMES).lower()
        for word in ("pause", "resume", "reset", "raise", "set_budget"):
            with self.subTest(word=word):
                self.assertNotIn(word, names, "预算钥匙不能交给 MCP")


class TestStdioProcess(unittest.TestCase):
    def test_real_stdio_round_trip_and_eof_exit(self):
        proc = subprocess.Popen(
            [sys.executable, "winmon.py", "--mcp-stdio"],
            cwd=h.PROJECT_DIR, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        try:
            def send(obj):
                proc.stdin.write((json.dumps(obj) + "\n").encode("ascii"))
                proc.stdin.flush()

            def recv():
                line = proc.stdout.readline()
                if not line:
                    # 先杀子进程再读 stderr：否则 stderr 管道还开着，read() 会永久阻塞
                    proc.kill()
                    err = (proc.stderr.read() or b"").decode("utf-8", "replace")
                    self.fail(f"服务器没有响应：{err[-800:]}")
                return json.loads(line.decode("utf-8"))

            send(req(1, "initialize", {"protocolVersion": "2025-11-25",
                                       "capabilities": {}}))
            self.assertEqual(recv()["result"]["protocolVersion"], "2025-11-25")
            send(req(2, "tools/list"))
            names = [t["name"] for t in recv()["result"]["tools"]]
            self.assertEqual(tuple(names), mcp.TOOL_NAMES)
            send(req(3, "tools/call", {"name": "winmon_playbooks",
                                       "arguments": {}}))
            self.assertFalse(recv()["result"]["isError"])
            proc.stdin.close()
            code = proc.wait(timeout=15)
            self.assertEqual(code, 0, "关掉 stdin 后应当正常退出（EOF）")
        finally:
            if proc.poll() is None:
                proc.kill()
            for stream in (proc.stdin, proc.stdout, proc.stderr):
                try:
                    stream.close()
                except Exception:
                    pass


if __name__ == "__main__":
    unittest.main(verbosity=2)
