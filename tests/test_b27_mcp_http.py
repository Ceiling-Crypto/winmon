# -*- coding: utf-8 -*-
"""
B27 —— 本地 HTTP MCP（阶段 4）。

只绑回环、Bearer token、Origin 白名单、请求体上限；网页端要"实时调用"必须
自己配隧道，WinMon 不捆绑。测试全部在本机回环上做，不联网。
"""
from __future__ import annotations

import json
import os
import sys
import threading
import unittest
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import winmon_mcp as mcp                 # noqa: E402


def post(port: int, body, token: str = "", origin: str = "",
         path: str = "/mcp"):
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=data,
                                 method="POST")
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    if origin:
        req.add_header("Origin", origin)
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            raw = resp.read()
            return resp.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            return exc.code, json.loads(raw) if raw else None
        except Exception:
            return exc.code, None


class TestHttpServer(unittest.TestCase):
    def setUp(self):
        self.state = mcp._fake_state()
        self.server, self.token = mcp.make_http_server(
            "127.0.0.1", 0, "test-token", ["http://allowed"], self.state)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self._stop)

    def _stop(self):
        try:
            self.server.shutdown()
            self.server.server_close()
        except Exception:
            pass
        self.thread.join(timeout=2)

    def test_binds_loopback_only(self):
        self.assertEqual(self.server.server_address[0], "127.0.0.1")

    def test_initialize_with_token(self):
        code, data = post(self.port, {
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-11-25", "capabilities": {}}},
            token=self.token)
        self.assertEqual(code, 200)
        self.assertEqual(data["result"]["protocolVersion"], "2025-11-25")

    def test_missing_token_is_401(self):
        code, _data = post(self.port, {"jsonrpc": "2.0", "id": 1, "method": "ping"})
        self.assertEqual(code, 401)

    def test_wrong_origin_is_403(self):
        code, _data = post(self.port, {"jsonrpc": "2.0", "id": 1, "method": "ping"},
                           token=self.token, origin="http://evil.example")
        self.assertEqual(code, 403)

    def test_allowed_origin_ok(self):
        code, _data = post(self.port, {"jsonrpc": "2.0", "id": 1, "method": "ping"},
                           token=self.token, origin="http://allowed")
        self.assertEqual(code, 200)

    def test_health_and_404(self):
        with urllib.request.urlopen(
                f"http://127.0.0.1:{self.port}/health", timeout=5) as resp:
            self.assertEqual(resp.status, 200)
        code, _data = post(self.port, {"jsonrpc": "2.0", "id": 1, "method": "ping"},
                           token=self.token, path="/nope")
        self.assertEqual(code, 404)

    def test_notification_returns_202(self):
        code, data = post(self.port, {"jsonrpc": "2.0",
                                      "method": "notifications/initialized"},
                          token=self.token)
        self.assertEqual(code, 202)
        self.assertIsNone(data)

    def test_tools_call_over_http(self):
        body = {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                "params": {"name": "winmon_playbooks", "arguments": {}}}
        code, data = post(self.port, body, token=self.token)
        self.assertEqual(code, 200)
        self.assertFalse(data["result"]["isError"])

    def test_batch_request(self):
        body = [{"jsonrpc": "2.0", "id": 1, "method": "ping"},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}]
        code, data = post(self.port, body, token=self.token)
        self.assertEqual(code, 200)
        self.assertEqual(len(data), 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
