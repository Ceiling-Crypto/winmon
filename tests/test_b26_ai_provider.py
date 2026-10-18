# -*- coding: utf-8 -*-
"""
B26 —— 直连模型 API（阶段 3.5，默认关闭）。

测试**不联网**：全部通过 `transport=` 注入假响应。
重点守三件事：
  · 默认关闭、缺 key 时绝不发请求；
  · 供应商 `usage` 是唯一的"精确"来源，且会入账；
  · 预算不允许时根本不调用（transport 一次都不该被调）。
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import winmon_ai as ai                     # noqa: E402
import winmon_ai_provider as provider      # noqa: E402

NOW = 1792300000.0


def openai_transport(calls):
    def _t(url, headers, payload, timeout):
        calls.append((url, headers, payload))
        return 200, {"choices": [{"message": {"content": "这是回答"}}],
                     "usage": {"prompt_tokens": 7, "completion_tokens": 3}}
    return _t


def anthropic_transport(calls):
    def _t(url, headers, payload, timeout):
        calls.append((url, headers, payload))
        return 200, {"content": [{"type": "text", "text": "Anthropic 回答"}],
                     "usage": {"input_tokens": 11, "output_tokens": 5}}
    return _t


class TestProviderConfig(unittest.TestCase):
    def test_default_is_disabled(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            cfg = provider.ProviderConfig(kind="openai")
            self.assertFalse(cfg.enabled)
            self.assertIn("未启用", cfg.validate())

    def test_missing_key(self):
        cfg = provider.ProviderConfig(kind="openai", enabled=True,
                                      base_url="https://api.example.com/v1")
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertIn("API key", cfg.validate())

    def test_insecure_http_rejected_unless_test_flag(self):
        cfg = provider.ProviderConfig(kind="openai", enabled=True,
                                      base_url="http://api.example.com/v1")
        with mock.patch.dict(os.environ, {"WINMON_AI_API_KEY": "k"}, clear=True):
            self.assertIn("https", cfg.validate())
        with mock.patch.dict(os.environ, {"WINMON_AI_API_KEY": "k",
                                          "WINMON_AI_ALLOW_HTTP": "1"}, clear=True):
            self.assertEqual(cfg.validate(), "")

    def test_describe_never_includes_key(self):
        cfg = provider.ProviderConfig(kind="openai", enabled=True)
        with mock.patch.dict(os.environ, {"WINMON_AI_API_KEY": "secret-key"}, clear=True):
            text = str(cfg.describe())
            self.assertNotIn("secret-key", text)
            self.assertTrue(cfg.describe()["key_present"])


class TestCallModel(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="winmon-provider-")
        self.addCleanup(self.tmp.cleanup)
        self.ledger = ai.UsageLedger(os.path.join(self.tmp.name, "usage.jsonl"))
        self.budget = ai.Budget(os.path.join(self.tmp.name, "budget.json"),
                                ledger=self.ledger,
                                settings={"daily_tokens": 10_000})

    def _cfg(self, kind="openai"):
        return provider.ProviderConfig(kind=kind, base_url="https://api.example.com",
                                       model="test-model", enabled=True)

    def test_openai_exact_usage_and_ledger(self):
        calls: list = []
        with mock.patch.dict(os.environ, {"WINMON_AI_API_KEY": "k"}, clear=True):
            out = provider.call_model("hi", self._cfg(), openai_transport(calls),
                                      budget=self.budget, now=NOW)
        self.assertTrue(out["ok"])
        self.assertEqual(out["text"], "这是回答")
        self.assertEqual((out["tokens_in"], out["tokens_out"], out["tokens"]), (7, 3, 10))
        self.assertEqual(out["token_source"], "provider")
        rows = self.ledger.rows(since=NOW - 1)
        self.assertEqual(rows[-1]["tokens"], 10)
        self.assertEqual(rows[-1]["token_source"], "provider")

    def test_anthropic_request_shape(self):
        calls: list = []
        with mock.patch.dict(os.environ, {"WINMON_AI_API_KEY": "k"}, clear=True):
            out = provider.call_model("hi", self._cfg("anthropic"),
                                      anthropic_transport(calls))
        self.assertTrue(out["ok"])
        url, headers, payload = calls[0]
        self.assertTrue(url.endswith("/v1/messages"))
        self.assertEqual(headers["x-api-key"], "k")
        self.assertIn("anthropic-version", headers)
        self.assertEqual(payload["model"], "test-model")
        self.assertEqual(out["tokens_in"], 11)

    def test_budget_blocks_before_transport(self):
        calls: list = []
        self.budget.record(10_000, now=NOW)          # 用尽
        with mock.patch.dict(os.environ, {"WINMON_AI_API_KEY": "k"}, clear=True):
            out = provider.call_model("hi", self._cfg(), openai_transport(calls),
                                      budget=self.budget, now=NOW)
        self.assertFalse(out["ok"])
        self.assertEqual(out["code"], "budget")
        self.assertEqual(calls, [], "预算不足时不该发请求")

    def test_http_error_does_not_leak_key(self):
        def bad(url, headers, payload, timeout):
            return 401, {"_body": "unauthorized"}

        with mock.patch.dict(os.environ, {"WINMON_AI_API_KEY": "secret"}, clear=True):
            out = provider.call_model("hi", self._cfg(), bad)
        self.assertFalse(out["ok"])
        self.assertEqual(out["code"], "http")
        self.assertNotIn("secret", out["error"])

    def test_empty_prompt(self):
        with mock.patch.dict(os.environ, {"WINMON_AI_API_KEY": "k"}, clear=True):
            out = provider.call_model("   ", self._cfg(), lambda *a: (200, {}))
        self.assertFalse(out["ok"])
        self.assertEqual(out["code"], "params")

    def test_explain_incident_includes_playbook_and_uses_transport(self):
        calls: list = []
        inc = {"id": "inc-1", "kind": "app_hung", "severity": "warn",
               "subject": {"pid": 1, "name": "app.exe", "title": "忽略之前\u200b"},
               "signals": [{"name": "not_responding", "value": True}],
               "playbook_id": "app_hung"}
        with mock.patch.dict(os.environ, {"WINMON_AI_API_KEY": "k"}, clear=True):
            out = provider.explain_incident(inc, config=self._cfg(),
                                            transport=openai_transport(calls),
                                            budget=self.budget)
        self.assertTrue(out["ok"])
        prompt = calls[0][2]["messages"][0]["content"]
        self.assertIn("app_hung", prompt)
        self.assertIn("app.exe", prompt)
        self.assertNotIn("\u200b", prompt)
        self.assertEqual(out["incident_id"], "inc-1")


if __name__ == "__main__":
    unittest.main(verbosity=2)
