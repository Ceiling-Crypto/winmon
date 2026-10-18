# -*- coding: utf-8 -*-
"""
B30 —— AI 助手"跟随用户正在用的 AI"（阶段 6）。

用户 2026-10-19 的原话：
  · 「winmon 的 AI 助手应该采取的是读取用户所使用的 AI，你直接写 GLM5.3 了，
     用户不用 GLM5.3 等同虚设」；
  · 「智谱本身的对话记录就得用户自己去调取，关了对话窗口就不知道 AI 用来干了啥」。

这里守住：
  · 识别到哪个 AI，就找哪个 profile；找不到只做剪贴板交接，绝不偷换模型；
  · 直连回答 / 剪贴板交接 / 用户手动保存的回复都落本地 `winmon_ai_chat.jsonl`；
  · 测试不联网（transport / count_tokens 全部注入假实现）。
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import _helpers as h                     # noqa: E402
import winmon_ai as ai                   # noqa: E402
import winmon_ai_provider as provider    # noqa: E402

NOW = 1792344000.0


class TestProfileMapping(unittest.TestCase):
    def test_detected_names_map_to_profiles(self):
        self.assertEqual(provider.profile_key_for_ai("智谱清言"), "zhipu")
        self.assertEqual(provider.profile_key_for_ai("DeepSeek Harness"), "deepseek")
        self.assertEqual(provider.profile_key_for_ai("Claude Desktop"), "anthropic")
        self.assertEqual(provider.profile_key_for_ai("ChatGPT"), "openai")
        self.assertEqual(provider.profile_key_for_ai("Kimi"), "moonshot")
        self.assertEqual(provider.profile_key_for_ai("记事本"), "")

    def test_profiles_load_and_follow(self):
        path = self._write({
            "follow_detected": True,
            "active": "zhipu",
            "profiles": {
                "zhipu": {"kind": "zhipu", "model": "glm-5.3", "enabled": True},
                "deepseek": {"kind": "openai", "base_url": "https://api.deepseek.com/v1",
                             "model": "deepseek-chat", "enabled": True,
                             "api_key_env": "WINMON_DEEPSEEK_API_KEY"},
            },
        })
        profiles, follow, active = provider.load_profiles(path)
        self.assertTrue(follow)
        self.assertEqual(active, "zhipu")
        self.assertEqual(set(profiles), {"zhipu", "deepseek"})
        deep = provider.resolve_for_ai("DeepSeek", path)
        self.assertIsNotNone(deep)
        self.assertEqual(deep.model, "deepseek-chat")
        self.assertIsNone(provider.resolve_for_ai("Kimi", path),
                          "没有 Kimi profile 时必须返回 None，不能拿 GLM 顶替")

    def test_follow_off_uses_active_profile(self):
        path = self._write({
            "follow_detected": False, "active": "zhipu",
            "profiles": {"zhipu": {"kind": "zhipu", "enabled": True}},
        })
        cfg = provider.resolve_for_ai("DeepSeek", path)
        self.assertIsNotNone(cfg)
        self.assertEqual(cfg.kind, "zhipu", "用户显式关掉跟随时，才允许用 active")

    def test_missing_file_has_no_profile(self):
        with tempfile.TemporaryDirectory(prefix="winmon-b30-") as tmp:
            path = os.path.join(tmp, "nope.json")
            self.assertEqual(provider.load_profiles(path)[0], {})
            self.assertIsNone(provider.resolve_for_ai("DeepSeek", path))
            self.assertFalse(provider.ProviderConfig.load(path).enabled)

    def test_legacy_flat_config_still_works(self):
        path = self._write({"kind": "zhipu", "model": "glm-5.3", "enabled": True})
        profiles, follow, active = provider.load_profiles(path)
        self.assertFalse(follow)
        self.assertEqual(active, "default")
        self.assertEqual(profiles["default"].model, "glm-5.3")

    def test_tokenizer_config_only_returns_zhipu(self):
        path = self._write({
            "follow_detected": True, "active": "deepseek",
            "profiles": {
                "deepseek": {"kind": "openai", "model": "deepseek-chat"},
                "zhipu": {"kind": "zhipu", "model": "glm-5.3", "exact_tokens": True},
            },
        })
        with mock.patch.dict(os.environ, {"WINMON_AI_EXACT": "1"}):
            cfg = provider.tokenizer_config(path)
        self.assertIsNotNone(cfg)
        self.assertEqual(cfg.kind, "zhipu")

    def _write(self, data: dict) -> str:
        tmp = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False,
                                          encoding="utf-8")
        with tmp:
            json.dump(data, tmp, ensure_ascii=False)
        self.addCleanup(lambda: os.path.exists(tmp.name) and os.remove(tmp.name))
        return tmp.name


class TestBarFollowsAi(unittest.TestCase):
    def test_bar_says_no_direct_config_instead_of_glm(self):
        text = ai.format_ai_bar(
            {"name": "DeepSeek", "kind": "desktop", "proc": "x.exe"},
            {"provider_tokens": 0, "tokenizer_tokens": 0, "estimated_tokens": 0},
            {"configured": False, "follow_ai": "DeepSeek"})
        self.assertIn("跟随 DeepSeek", text)
        self.assertIn("无直连", text)
        self.assertNotIn("glm-5.3", text)

    def test_bar_shows_followed_model(self):
        text = ai.format_ai_bar(
            {"name": "智谱清言", "kind": "web", "proc": "msedge.exe"},
            {"provider_tokens": 441, "tokenizer_tokens": 0, "estimated_tokens": 0},
            {"configured": True, "follow_ai": "智谱清言", "model": "glm-5.3",
             "enabled": True, "key_present": True})
        self.assertIn("跟随 智谱清言 → glm-5.3 就绪", text)
        self.assertIn("真实 441", text)

    def test_bar_shows_mcp_connection_and_mode(self):
        text = ai.format_ai_bar(
            {"name": "Claude Desktop", "kind": "desktop", "proc": "claude.exe"},
            {"provider_tokens": 20, "tokenizer_tokens": 30, "estimated_tokens": 0},
            {"configured": True, "follow_ai": "Claude Desktop",
             "model": "claude-3-5-haiku-latest", "enabled": True, "key_present": True},
            connection={"kind": "mcp", "client": "Claude Desktop",
                        "ts": "2026-10-19T01:33:00", "calls": 3},
            mode="local")
        self.assertIn("已接入 MCP（Claude Desktop）", text)
        self.assertIn("01:33", text)
        self.assertIn("本地提醒", text)


class TestWorkAndBreak(unittest.TestCase):
    def test_work_kind_and_summary(self):
        class W:
            def __init__(self, hwnd, proc, title, cpu=0):
                self.hwnd, self.proc, self.title = hwnd, proc, title
                self.real_name = proc
                self.cpu = cpu
                self.mem_private_kb = 1024
                self.is_hidden_surface = False
        wins = [W(1, "Code.exe", "winmon.py — Visual Studio Code", 5),
                W(2, "msedge.exe", "Python 文档", 1)]
        self.assertEqual(ai.work_kind_of("Code.exe", "x"), "编程")
        self.assertEqual(ai.work_kind_of("winword.exe", "文档"), "文档")
        text = ai.describe_work(wins, 1, work_minutes=37)
        self.assertIn("当前工作：编程", text)
        self.assertIn("Visual Studio Code", text)
        self.assertIn("37 分钟", text)
        self.assertIn("0 token", text)

    def test_time_slots_use_24h_clock(self):
        import time
        night = ai.time_slot(time.mktime((2026, 10, 19, 1, 54, 0, 0, 0, -1)))
        self.assertIsNotNone(night)
        self.assertEqual(night["label"], "深夜")
        self.assertEqual(night["clock"], "01:54")
        self.assertIn("01:54", night["message"])
        self.assertIn("休息", night["message"])
        morning = ai.time_slot(time.mktime((2026, 10, 19, 7, 30, 0, 0, 0, -1)))
        self.assertEqual(morning["label"], "早上")
        self.assertIn("07:30", morning["message"])
        self.assertIsNone(ai.time_slot(
            time.mktime((2026, 10, 19, 15, 0, 0, 0, 0, -1))),
            "15:00 不在提醒时段")

    def test_night_slot_crosses_midnight_once(self):
        import time
        before = ai.time_slot(time.mktime((2026, 10, 19, 23, 59, 0, 0, 0, -1)))
        after = ai.time_slot(time.mktime((2026, 10, 20, 0, 1, 0, 0, 0, -1)))
        self.assertEqual(before["key"], after["key"],
                         "23:59 与次日 00:01 必须算同一个深夜时段")

    def test_last_input_is_a_number(self):
        import winmon_core as core
        self.assertGreaterEqual(core.seconds_since_last_input(), 0.0)


class TestHandoffPrompt(unittest.TestCase):
    def test_prompt_names_the_current_ai_and_marks_data_untrusted(self):
        text = provider.handoff_prompt("# 现场\n前台：Edge", "为什么卡？",
                                       ai_name="DeepSeek", via_mcp=True)
        self.assertIn("你是 DeepSeek", text)
        self.assertIn("不可信数据", text)
        self.assertIn("winmon_ai_sessions", text)
        self.assertIn("为什么卡？", text)


class TestChatLog(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="winmon-chat-")
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "chat.jsonl")
        self.env = mock.patch.dict(os.environ, {"WINMON_AI_CHAT": self.path})
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_direct_handoff_and_reply_round_trip(self):
        ai.append_chat({"mode": "direct", "ai": "智谱清言", "model": "glm-5.3",
                        "question": "为什么卡", "answer": "因为 X", "tokens": 12,
                        "tokens_in": 8, "tokens_out": 4})
        ai.append_chat({"mode": "handoff", "ai": "DeepSeek", "question": "看现场"})
        ai.append_chat({"mode": "reply", "ai": "DeepSeek", "answer": "我看到了 Edge"})
        rows = ai.load_chat(limit=10)
        self.assertEqual([r["mode"] for r in rows], ["direct", "handoff", "reply"])
        self.assertIn("API 回执", ai.format_chat_record(rows[0]))
        self.assertIn("看不到", ai.format_chat_record(rows[1]))
        self.assertIn("手动保存", ai.format_chat_record(rows[2]))

    def test_bad_lines_are_ignored(self):
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write("{broken\n")
        ai.append_chat({"mode": "handoff", "ai": "X"})
        self.assertEqual(len(ai.load_chat()), 1)


class TestAssistantDoesNotFallback(unittest.TestCase):
    def test_no_profile_hands_off_and_never_calls_another_model(self):
        import winmon
        tmp = tempfile.TemporaryDirectory(prefix="winmon-b30h-")
        self.addCleanup(tmp.cleanup)
        env = mock.patch.dict(os.environ, {
            "WINMON_AI_PROVIDER_FILE": os.path.join(tmp.name, "none.json"),
            "WINMON_AI_CHAT": os.path.join(tmp.name, "chat.jsonl"),
            "WINMON_AI_EXACT": "0",
        })
        env.start()
        self.addCleanup(env.stop)
        app = winmon.WinMonApp.__new__(winmon.WinMonApp)
        app._ai_sessions = {"primary": {"name": "DeepSeek", "source": "window_title"}}
        app._ai_context = lambda: "# 现场\n前台：Edge"
        app._ai_log_lines = []
        app._ai_assistant_window = None
        app._ai_jobs = []
        app._ai_busy = False
        app._ai_last_handoff = {}
        app._set_status = lambda text: None
        copied = []
        with mock.patch.object(ai, "copy_to_clipboard",
                               side_effect=lambda text: copied.append(text) or True), \
                mock.patch.object(provider, "call_model",
                                  side_effect=AssertionError("不该调用别的模型")):
            ok = app.ai_assistant_request("看一下")
        self.assertTrue(ok)
        self.assertEqual(len(copied), 1)
        self.assertIn("你是 DeepSeek", copied[0])
        self.assertIn("看一下", copied[0])
        rows = ai.load_chat()
        self.assertEqual(rows[-1]["mode"], "handoff")
        self.assertEqual(rows[-1]["ai"], "DeepSeek")


class TestWellnessTick(unittest.TestCase):
    def _app(self, mode="local"):
        import winmon
        app = winmon.WinMonApp.__new__(winmon.WinMonApp)
        app._ai_assist = {"mode": mode, "break_enabled": True}
        app._ai_log_lines = []
        app._ai_assistant_window = None
        app._ai_notify_enabled = False
        app._last_time_slot = ""
        app._work_since = time.time()
        app._set_status = lambda text: None
        return app

    def test_clock_slots_fire_once_per_slot(self):
        import time
        import winmon
        app = self._app("local")
        night = time.mktime((2026, 10, 19, 1, 54, 0, 0, 0, -1))
        with mock.patch.object(winmon.core, "seconds_since_last_input",
                               return_value=0.0):
            app._ai_wellness_tick(night)
        self.assertTrue(any("时间提醒 01:54" in line for line in app._ai_log_lines),
                        app._ai_log_lines)
        self.assertTrue(any("深夜" in line for line in app._ai_log_lines))
        count = len(app._ai_log_lines)
        with mock.patch.object(winmon.core, "seconds_since_last_input",
                               return_value=0.0):
            app._ai_wellness_tick(night + 60)
        self.assertEqual(len(app._ai_log_lines), count,
                         "同一时段每天只提醒一次")
        morning = time.mktime((2026, 10, 19, 7, 30, 0, 0, 0, -1))
        with mock.patch.object(winmon.core, "seconds_since_last_input",
                               return_value=0.0):
            app._ai_wellness_tick(morning)
        self.assertEqual(len(app._ai_log_lines), count + 1)
        self.assertTrue(any("早上" in line for line in app._ai_log_lines))

    def test_idle_and_passive_do_not_remind(self):
        import time
        import winmon
        app = self._app("local")
        night = time.mktime((2026, 10, 19, 1, 54, 0, 0, 0, -1))
        with mock.patch.object(winmon.core, "seconds_since_last_input",
                               return_value=400.0):
            app._ai_wellness_tick(night)
        self.assertEqual(app._ai_log_lines, [], "离开电脑时不打扰")
        passive = self._app("passive")
        with mock.patch.object(winmon.core, "seconds_since_last_input",
                               return_value=0.0):
            passive._ai_wellness_tick(night)
        self.assertEqual(passive._ai_log_lines, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
