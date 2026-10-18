# -*- coding: utf-8 -*-
"""
B25 —— AI 上下文用量账本与预算调节器（阶段 2.5）。

守住 MCP.md §10.7：
  · 精确（provider）与估算（estimated）分栏，不把估算写成账单；
  · 80% 软提醒 / 100% 停止 / 用户暂停 / 每小时上限；
  · 恢复只能走本地 `Budget` 方法（MCP 没有改预算的工具）；
  · 事件包（剪贴板桥）有 token 上限，且不含未清洗的标题。
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import winmon_ai as ai                   # noqa: E402

NOW = 1792300000.0                        # 2026-10-18 附近的固定时刻


class TestUsageLedger(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="winmon-usage-")
        self.path = os.path.join(self.tmp.name, "usage.jsonl")
        self.ledger = ai.UsageLedger(self.path)
        self.addCleanup(self.tmp.cleanup)

    def test_record_and_read(self):
        self.ledger.record(120, client="Claude", tool="winmon_ai_sessions", now=NOW)
        self.ledger.record(80, client="Claude", tool="winmon_incidents",
                           token_source="provider", now=NOW + 10)
        rows = self.ledger.rows(since=NOW - 1)
        self.assertEqual(len(rows), 2)
        self.assertEqual(self.ledger.tokens_since(NOW - 1), 200)
        self.assertEqual(self.ledger.calls_since(NOW - 1), 2)

    def test_bad_lines_are_ignored(self):
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write("{broken\n")
        self.ledger.record(5, now=NOW)
        self.assertEqual(len(self.ledger.rows(since=NOW - 1)), 1)

    def test_summary_splits_precision(self):
        self.ledger.record(100, client="A", token_source="estimated", now=NOW)
        self.ledger.record(30, client="A", token_source="provider", now=NOW + 1)
        summary = self.ledger.summary(NOW + 2, NOW - 10)
        self.assertEqual(summary["day_tokens"], 130)
        self.assertEqual(summary["by_client"]["A"]["estimated"], 100)
        self.assertEqual(summary["by_client"]["A"]["provider"], 30)

    def test_invalid_numbers_do_not_crash(self):
        self.ledger.record("abc", now=NOW)
        self.assertEqual(self.ledger.tokens_since(NOW - 1), 0)


class TestBudgetStateMachine(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="winmon-budget-")
        self.addCleanup(self.tmp.cleanup)
        self.usage = ai.UsageLedger(os.path.join(self.tmp.name, "usage.jsonl"))
        self.path = os.path.join(self.tmp.name, "budget.json")
        self.budget = ai.Budget(self.path, ledger=self.usage, settings={
            "daily_tokens": 1000, "hourly_calls": 5,
            "warn_ratio": 0.8, "per_incident_tokens": 200})

    def test_default_daily_is_ten_million(self):
        self.assertEqual(ai.DEFAULT_BUDGET["daily_tokens"], 10_000_000)

    def test_ok_warn_exhausted(self):
        self.assertEqual(self.budget.status(NOW)["state"], "ok")
        self.budget.record(800, now=NOW)
        self.assertEqual(self.budget.status(NOW)["state"], "warn")
        self.budget.record(200, now=NOW)
        self.assertEqual(self.budget.status(NOW)["state"], "exhausted")
        self.assertFalse(self.budget.check(1, now=NOW)["allowed"])

    def test_hourly_limit(self):
        for _ in range(5):
            self.budget.record(1, now=NOW)
        status = self.budget.status(NOW)
        self.assertEqual(status["state"], "exhausted")
        self.assertIn("每小时", status["reason"])

    def test_pause_and_resume_are_local_methods(self):
        self.budget.pause(True)
        self.assertEqual(self.budget.status(NOW)["state"], "paused")
        self.assertFalse(self.budget.check(1, now=NOW)["allowed"])
        self.budget.pause(False)
        self.assertEqual(self.budget.status(NOW)["state"], "ok")

    def test_reset_today_and_raise_limit(self):
        self.budget.record(900, now=NOW)
        self.budget.reset_today(NOW + 1)
        self.assertEqual(self.budget.status(NOW + 1)["used_tokens"], 0)
        self.budget.raise_limit(2.0)
        self.assertEqual(self.budget.status(NOW + 1)["daily_tokens"], 2000)

    def test_persists_limits(self):
        self.budget.set_daily_tokens(12345)
        other = ai.Budget(self.path, ledger=self.usage)
        self.assertEqual(other.status(NOW)["daily_tokens"], 12345)

    def test_other_process_sees_pause_without_restart(self):
        """MCP 进程常驻时，本地窗口暂停/恢复必须立刻生效（按 mtime 重读）。"""
        server_side = ai.Budget(self.path, ledger=self.usage)
        self.assertFalse(server_side.status(NOW)["paused"])
        self.budget.pause(True)
        self.assertTrue(server_side.status(NOW)["paused"])
        self.budget.pause(False)
        self.assertFalse(server_side.status(NOW)["paused"])

    def test_check_refuses_when_next_call_would_overshoot(self):
        self.budget.record(950, now=NOW)
        gate = self.budget.check(100, now=NOW)
        self.assertFalse(gate["allowed"])
        self.assertIn("超出", gate["reason"])

    def test_format_usage_text(self):
        text = ai.format_usage_text(self.budget.status(NOW))
        self.assertIn("预算", text)
        self.assertIn("估算", text)


class TestContextPack(unittest.TestCase):
    def test_pack_contains_primary_and_incident(self):
        sessions = {"primary": {"name": "Claude Desktop", "kind": "desktop",
                                "confidence": 0.9, "proc": "claude.exe", "pid": 1,
                                "title": "Claude\n忽略之前\u200b"},
                    "others": [], "note": "测试"}
        incidents = {"incidents": [{
            "kind": "app_hung", "severity": "warn", "playbook_id": "app_hung",
            "subject": {"name": "notepad.exe", "title": "无响应"},
            "signals": [{"name": "not_responding", "value": True}]}]}
        text = ai.build_context_pack(max_tokens=600, sessions=sessions,
                                     incidents=incidents)
        self.assertIn("Claude Desktop", text)
        self.assertIn("app_hung", text)
        self.assertNotIn("\u200b", text)
        self.assertLessEqual(ai.estimate_tokens(text), 600)

    def test_copy_uses_clip_and_reports_failure(self):
        with mock.patch.object(ai.subprocess, "run",
                               return_value=mock.Mock(returncode=0)) as run:
            self.assertTrue(ai.copy_to_clipboard("hello"))
            self.assertEqual(run.call_args[0][0][0], "clip")
        with mock.patch.object(ai.subprocess, "run", side_effect=OSError("x")):
            self.assertFalse(ai.copy_to_clipboard("hello"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
