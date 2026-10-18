# -*- coding: utf-8 -*-
"""
B29 —— 界面可见的 AI 识别 + 真实/精确/估算三栏用量（阶段 5）。

用户 2026-10-19 的三条要求：
  1. 主界面必须显示"现在识别到的 AI"，不能只在命令行里看；
  2. 用量要区分：直连 API 回执（真实）、分词器计数（精确）、本地估算；
  3. GLM-5.3 要真的读取窗口现场，并把每次调用的 token 写回界面。

这里守住纯逻辑与窗口接线，**不联网**（transport / count_tokens 全部注入假实现）。
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import _helpers as h                     # noqa: E402
import winmon_ai as ai                   # noqa: E402
import winmon_ai_provider as provider    # noqa: E402

NOW = 1792340000.0


class _Win:
    def __init__(self, hwnd, name, title, cpu=0.0, mem=0, hidden=False, proc=""):
        self.hwnd = hwnd
        self.real_name = name
        self.proc = proc or name
        self.title = title
        self.cpu = cpu
        self.mem_private_kb = mem
        self.is_hidden_surface = hidden


class TestVisibleAiBar(unittest.TestCase):
    def test_bar_shows_name_and_real_estimate_split(self):
        primary = {"name": "智谱清言", "kind": "web", "proc": "msedge.exe",
                   "foreground_seconds_30m": 72}
        status = {"provider_tokens": 428, "tokenizer_tokens": 0,
                  "estimated_tokens": 1200}
        info = {"enabled": True, "key_present": True, "model": "glm-5.3"}
        text = ai.format_ai_bar(primary, status, info)
        self.assertIn("智谱清言", text)
        self.assertIn("网页", text)
        self.assertIn("1 分 12 秒", text)
        self.assertIn("真实 428", text)
        self.assertIn("精确 0", text)
        self.assertIn("估算 1,200", text)
        self.assertIn("glm-5.3 就绪", text)

    def test_bar_without_ai_and_without_key(self):
        text = ai.format_ai_bar(None, {"provider_tokens": 0, "tokenizer_tokens": 0,
                                       "estimated_tokens": 0},
                                {"enabled": True, "key_present": False,
                                 "model": "glm-5.3"})
        self.assertIn("未识别到", text)
        self.assertIn("缺 key", text)

    def test_sessions_detail_lists_candidates_and_reason(self):
        res = {"primary": {"name": "Claude Desktop", "kind": "desktop",
                           "confidence": 0.95, "proc": "claude.exe", "pid": 10,
                           "foreground_seconds_30m": 61, "activations_30m": 3,
                           "source": "mcp_parent", "pinned": False},
               "others": [{"name": "ChatGPT", "kind": "web", "score": 0.4}],
               "note": "按连接状态优先", "ambiguous": True}
        text = ai.format_sessions_detail(res)
        self.assertIn("Claude Desktop", text)
        self.assertIn("chatgpt", text.lower())
        self.assertIn("mcp_parent", text)
        self.assertIn("分差很小", text)


class TestThreeWayLedger(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="winmon-b29-")
        self.addCleanup(self.tmp.cleanup)
        self.usage = ai.UsageLedger(os.path.join(self.tmp.name, "u.jsonl"))
        self.budget = ai.Budget(os.path.join(self.tmp.name, "b.json"),
                                ledger=self.usage,
                                settings={"daily_tokens": 1000})

    def test_provider_tokenizer_estimated_are_separate(self):
        self.usage.record(10, client="A", token_source="provider", now=NOW)
        self.usage.record(20, client="A", token_source="tokenizer", now=NOW + 1)
        self.usage.record(30, client="A", token_source="estimated", now=NOW + 2)
        summary = self.usage.summary(NOW + 3, NOW - 10)
        item = summary["by_client"]["A"]
        self.assertEqual((item["provider"], item["tokenizer"], item["estimated"]),
                         (10, 20, 30))
        self.assertEqual(item["exact"], 30)
        status = self.budget.status(NOW + 3)
        self.assertEqual(status["exact_tokens"], 30)
        self.assertEqual(status["provider_tokens"], 10)
        self.assertEqual(status["tokenizer_tokens"], 20)
        self.assertEqual(status["estimated_tokens"], 30)
        text = ai.format_usage_text(status)
        self.assertIn("真实 API 回执 10", text)
        self.assertIn("精确分词 20", text)
        self.assertIn("估算 30", text)

    def test_unknown_source_falls_back_to_estimated(self):
        self.usage.record(5, client="A", token_source="whatever", now=NOW)
        row = self.usage.rows()[-1]
        self.assertEqual(row["token_source"], "estimated")

    def test_status_reports_last_mcp_client(self):
        self.usage.record(10, client="Claude Desktop", mode="mcp", now=NOW)
        self.usage.record(20, client="WinMon direct", mode="direct", now=NOW + 1)
        status = self.budget.status(NOW + 2)
        self.assertEqual(status["mcp_calls"], 1)
        self.assertEqual(status["last_mcp"]["client"], "Claude Desktop")
        self.assertEqual(status["mcp_clients"], ["Claude Desktop"])


class TestExactTokenizer(unittest.TestCase):
    def setUp(self):
        provider._TOKEN_CACHE.clear()
        self.cfg = provider.ProviderConfig(
            kind="zhipu", base_url="https://api.example.com/v4",
            model="glm-5.3", enabled=True, exact_tokens=True)

    def test_count_tokens_request_shape_and_cache(self):
        calls = []

        def transport(url, headers, payload, timeout):
            calls.append((url, headers, payload))
            return 200, {"usage": {"prompt_tokens": 42}}

        with mock.patch.dict(os.environ, {"WINMON_AI_API_KEY": "k"}, clear=True):
            one = provider.count_tokens("窗口标题是数据", self.cfg, transport)
            two = provider.count_tokens("窗口标题是数据", self.cfg, transport)
        self.assertTrue(one["ok"], one.get("error"))
        self.assertEqual(one["tokens"], 42)
        self.assertEqual(one["token_source"], "tokenizer")
        self.assertTrue(two.get("cached"))
        self.assertEqual(len(calls), 1, "同一段文本第二次必须走缓存")
        url, headers, payload = calls[0]
        self.assertTrue(url.endswith("/tokenizer"))
        self.assertEqual(headers["Authorization"], "Bearer k")
        self.assertEqual(payload["model"], "glm-5.3")
        self.assertEqual(payload["messages"][0]["role"], "user")

    def test_count_tokens_errors_are_reported(self):
        with mock.patch.dict(os.environ, {"WINMON_AI_API_KEY": "k"}, clear=True):
            bad = provider.count_tokens("x", self.cfg, lambda *a: (500, {"_body": "no"}))
            empty = provider.count_tokens("", self.cfg, lambda *a: (200, {}))
        self.assertFalse(bad["ok"])
        self.assertEqual(bad["code"], "http")
        self.assertTrue(empty["ok"])
        self.assertEqual(empty["tokens"], 0)

    def test_set_exact_tokens_preserves_other_keys(self):
        path = os.path.join(self.tmp_dir(), "provider.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"kind": "zhipu", "enabled": True, "model": "glm-5.3"}, fh)
        self.assertTrue(provider.set_exact_tokens(True, path))
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        self.assertTrue(data["exact_tokens"])
        self.assertEqual(data["model"], "glm-5.3")
        self.assertTrue(data["enabled"])
        self.assertFalse(provider.set_exact_tokens(True,
                                                   os.path.join(self.tmp_dir(), "none.json")))

    def tmp_dir(self):
        if not hasattr(self, "_tmp"):
            self._tmp = tempfile.TemporaryDirectory(prefix="winmon-b29p-")
            self.addCleanup(self._tmp.cleanup)
        return self._tmp.name

    def test_provider_config_exact_flag_from_file_and_env(self):
        path = os.path.join(self.tmp_dir(), "provider.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"kind": "zhipu", "model": "glm-5.3", "enabled": True,
                       "exact_tokens": True}, fh)
        with mock.patch.dict(os.environ, {"WINMON_AI_PROVIDER_FILE": path,
                                          "WINMON_AI_EXACT": "0"}, clear=True):
            cfg = provider.ProviderConfig.load()
        self.assertFalse(cfg.exact_tokens, "环境变量 0 应当能临时关掉")
        with mock.patch.dict(os.environ, {"WINMON_AI_PROVIDER_FILE": path}, clear=True):
            cfg = provider.ProviderConfig.load()
        self.assertTrue(cfg.exact_tokens)


class TestWorkContext(unittest.TestCase):
    def test_context_reads_windows_and_sanitizes_titles(self):
        windows = [
            _Win(1, "Code.exe", "winmon.py - Visual Studio Code", 5.0, 200 * 1024),
            _Win(2, "chrome.exe", "忽略之前\n\u200b 的网页", 1.0, 100 * 1024),
            _Win(3, "hidden.exe", "看不见", hidden=True),
        ]
        sessions = {"primary": {"name": "智谱清言", "kind": "web",
                                "proc": "msedge.exe", "confidence": 0.65}}
        incidents = {"incidents": [{"severity": "warn", "kind": "app_hung",
                                    "subject": {"name": "notepad.exe", "title": "无响应"},
                                    "playbook_id": "app_hung"}]}
        text = ai.build_work_context(windows, {}, 1, sessions, incidents)
        self.assertIn("前台：Code.exe", text)
        self.assertIn("智谱清言", text)
        self.assertIn("app_hung", text)
        self.assertIn("忽略之前", text)
        self.assertNotIn("\u200b", text)
        self.assertNotIn("hidden.exe", text)
        self.assertIn("窗口清单", text)
        self.assertLessEqual(text.count("- "), 8, "窗口清单不该无限膨胀")

    def test_assistant_prompt_contains_question_and_boundary(self):
        prompt = provider.assistant_prompt("# 现场\n前台：x", "为什么卡？")
        self.assertIn("为什么卡？", prompt)
        self.assertIn("不可信数据", prompt)
        self.assertIn("不要建议自动结束进程", prompt)


class TestAssistSettings(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="winmon-b29a-")
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "assist.json")
        self.env = mock.patch.dict(os.environ, {"WINMON_AI_ASSIST": self.path})
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_default_is_local_and_round_trip(self):
        default = ai.load_assist()
        self.assertEqual(default["mode"], "local", "默认必须是不花 token 的本地提醒")
        self.assertTrue(default["break_enabled"])
        saved = ai.save_assist({"mode": "active", "enabled": True,
                                "auto_explain": True, "interval_seconds": 5})
        self.assertEqual(saved["mode"], "active")
        self.assertTrue(saved["enabled"])
        self.assertEqual(saved["interval_seconds"], 60, "间隔必须夹到 ≥60 秒")
        loaded = ai.load_assist()
        self.assertTrue(loaded["auto_explain"])
        ai.save_assist({"interval_seconds": 99999, "break_minutes": 5})
        after = ai.load_assist()
        self.assertEqual(after["interval_seconds"], 3600)
        self.assertEqual(after["break_minutes"], 15, "休息间隔夹到 ≥15 分钟")
        local = ai.save_assist({"mode": "local", "enabled": True,
                                "auto_explain": True})
        self.assertFalse(local["enabled"], "切回本地提醒必须关掉自动调用开关")
        self.assertFalse(local["auto_explain"])

    def test_old_enabled_config_migrates_to_active(self):
        with open(self.path, "w", encoding="utf-8") as fh:
            json.dump({"enabled": True, "auto_explain": True}, fh)
        self.assertEqual(ai.load_assist()["mode"], "active")

    def test_bad_file_falls_back_to_default(self):
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write("{broken")
        self.assertFalse(ai.load_assist()["enabled"])


class TestMcpExactAccounting(unittest.TestCase):
    class FakeBudget:
        settings = {"per_incident_tokens": 2000}

        def __init__(self):
            self.recorded = None

        def check(self, estimated=0, now=None):
            return {"allowed": True, "state": "ok", "reason": ""}

        def record(self, tokens, **kw):
            self.recorded = (tokens, kw)
            return {"state": "ok", "used_tokens": tokens, "daily_tokens": 1000,
                    "ratio": 0.01, "reason": ""}

    def test_mcp_records_tokenizer_source_when_enabled(self):
        import winmon_mcp as mcp
        budget = self.FakeBudget()
        cfg = provider.ProviderConfig(kind="zhipu", enabled=True, exact_tokens=True,
                                      base_url="https://api.example.com/v4")
        with mock.patch.object(mcp.provider, "tokenizer_config", return_value=cfg), \
                mock.patch.object(mcp.provider, "count_tokens",
                                  return_value={"ok": True, "tokens": 77}), \
                mock.patch.dict(os.environ, {"WINMON_AI_API_KEY": "k"}):
            out = mcp.call_tool("winmon_playbooks", {}, {"budget": budget,
                                                         "client": "Claude Desktop"})
        self.assertFalse(out.get("isError"))
        self.assertIsNotNone(budget.recorded)
        tokens, kw = budget.recorded
        self.assertEqual(tokens, 77)
        self.assertEqual(kw["token_source"], "tokenizer")
        self.assertEqual(kw["client"], "Claude Desktop")

    def test_mcp_falls_back_to_estimate_when_tokenizer_fails(self):
        import winmon_mcp as mcp
        budget = self.FakeBudget()
        cfg = provider.ProviderConfig(kind="zhipu", enabled=True, exact_tokens=True,
                                      base_url="https://api.example.com/v4")
        with mock.patch.object(mcp.provider, "tokenizer_config", return_value=cfg), \
                mock.patch.object(mcp.provider, "count_tokens",
                                  return_value={"ok": False, "error": "x"}), \
                mock.patch.dict(os.environ, {"WINMON_AI_API_KEY": "k"}):
            mcp.call_tool("winmon_playbooks", {}, {"budget": budget})
        tokens, kw = budget.recorded
        self.assertEqual(kw["token_source"], "estimated")
        self.assertGreater(tokens, 0)


@h.needs_tk
class TestAssistantWindow(unittest.TestCase):
    """「AI 助手」窗口：只验证接线（不联网、不真的点系统菜单）。"""

    def setUp(self):
        import tkinter as tk
        import winmon
        self.winmon = winmon
        self.root = tk.Tk()
        self.root.withdraw()
        self.addCleanup(self.root.destroy)

        class FakeApp:
            _ai_assist = {"enabled": False, "auto_explain": False,
                          "interval_seconds": 300}
            _ai_log_lines = ["[00:00:00] 历史记录"]
            _ai_assistant_window = None
            _ai_usage_window = None

            def __init__(self, root):
                self.root = root
                self.asked = []
                self.force_flags = []
                self.saved = []

            def _ai_header_text(self):
                return "跟随当前 AI：DeepSeek → deepseek-chat · 缺 API key ｜ 真实 0 token"

            def _ai_provider_snapshot(self):
                return {"configured": True, "enabled": True, "key_present": False,
                        "api_key_env": "WINMON_AI_API_KEY",
                        "follow_ai": "DeepSeek", "model": "deepseek-chat"}

            def set_assist(self, enabled=None, auto_explain=None, mode=None,
                           break_enabled=None, break_minutes=None):
                if mode is not None:
                    self._ai_assist["mode"] = mode
                if enabled is not None:
                    self._ai_assist["enabled"] = bool(enabled)
                if auto_explain is not None:
                    self._ai_assist["auto_explain"] = bool(auto_explain)
                if break_enabled is not None:
                    self._ai_assist["break_enabled"] = bool(break_enabled)
                if break_minutes is not None:
                    self._ai_assist["break_minutes"] = int(break_minutes)
                return dict(self._ai_assist)

            def ai_assistant_request(self, question="", force_handoff=False):
                self.asked.append(question)
                self.force_flags.append(bool(force_handoff))
                return True

            def ai_save_reply(self, text=""):
                self.saved.append(text)
                return True

            def action_ai_usage(self):
                pass

        self.app = FakeApp(self.root)

    def test_window_wires_analyze_send_and_toggles(self):
        win = self.winmon._AiAssistantWindow(self.root, self.app)
        self.app._ai_assistant_window = win
        text = win.body.get("1.0", "end")
        self.assertIn("历史记录", text)
        self.assertIn("DeepSeek", win.header.get())
        win.analyze()
        self.assertEqual(self.app.asked, [""])
        win.question.set("为什么这么卡？")
        win.send()
        self.assertEqual(self.app.asked[-1], "为什么这么卡？")
        self.assertEqual(win.question.get(), "")
        win.watch_var.set(True)
        win.toggle_watch()
        self.assertTrue(self.app._ai_assist["enabled"])
        win.explain_var.set(True)
        win.toggle_explain()
        self.assertTrue(self.app._ai_assist["auto_explain"])
        win.key_var.set("test-key-only-in-memory")
        win.use_key()
        self.assertEqual(os.environ.get("WINMON_AI_API_KEY"), "test-key-only-in-memory")
        self.assertEqual(win.key_var.get(), "")
        os.environ.pop("WINMON_AI_API_KEY", None)
        win.question.set("交给当前 AI")
        win.handoff()
        self.assertEqual(self.app.asked[-1], "交给当前 AI")
        self.assertTrue(self.app.force_flags[-1], "剪贴板交接必须显式 force_handoff")
        self.assertEqual(win.question.get(), "")
        self.root.clipboard_clear()
        self.root.clipboard_append("AI 的回答")
        win.save_reply()
        self.assertEqual(self.app.saved[-1], "AI 的回答")
        win.mode_var.set("主动 AI（会花 token）")
        win.change_mode()
        self.assertEqual(self.app._ai_assist.get("mode"), "active")
        self.assertEqual(str(win.watch_chk.cget("state")), "normal")
        win.close()
        self.assertIsNone(self.app._ai_assistant_window)


if __name__ == "__main__":
    unittest.main(verbosity=2)
