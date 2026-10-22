# -*- coding: utf-8 -*-
"""
B47 —— 2026-10-22：直连 API 的「全部厂商」目录。

用户的原话：接入 AI 那里的直连 API key「没有罗列出所有 ai 软件」，
想法是「全写出来用户自己选」。这里守住三件事：
  · 目录覆盖常见厂商，每家都有可改的默认地址/模型/环境变量；
  · 界面的下拉框真的列出全部，不是只改了后端常量；
  · 保存一个厂商的配置时只写地址/模型/环境变量名，**绝不写 key 本身**。
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import _helpers as h                       # noqa: E402
import winmon_ai_provider as provider       # noqa: E402


class TestProviderCatalog(unittest.TestCase):
    def test_catalog_has_all_major_providers(self):
        expected = {
            "openai", "anthropic", "google", "xai", "deepseek", "zhipu",
            "moonshot", "dashscope", "qianfan", "doubao", "hunyuan",
            "mistral", "groq", "openrouter", "siliconflow", "perplexity",
            "ollama", "lmstudio", "custom",
        }
        self.assertEqual(set(provider.PROFILE_ORDER), expected)
        self.assertEqual(len(provider.PROFILE_ORDER), len(expected))
        for key, spec in provider.PROVIDER_PRESETS.items():
            with self.subTest(provider=key):
                self.assertTrue(spec.get("label"), key)
                self.assertIn(spec.get("kind"), provider.PROVIDER_KINDS)
                self.assertTrue(spec.get("api_key_env"), key)
                if key != "custom":
                    self.assertTrue(str(spec.get("base_url", "")).startswith("http"),
                                    key)
                    self.assertTrue(spec.get("model"), key)

    def test_known_ai_names_map_to_the_new_profiles(self):
        cases = {
            "Kimi": "moonshot", "Claude Desktop": "anthropic",
            "Gemini": "google", "Grok": "xai", "通义千问": "dashscope",
            "文心一言": "qianfan", "豆包": "doubao", "腾讯元宝": "hunyuan",
            "Mistral": "mistral", "Groq": "groq", "OpenRouter": "openrouter",
            "硅基流动": "siliconflow", "Perplexity": "perplexity",
            "Ollama": "ollama", "LM Studio": "lmstudio",
        }
        for name, profile in cases.items():
            with self.subTest(name=name):
                self.assertEqual(provider.profile_key_for_ai(name), profile)


class TestSaveProfile(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="winmon-b47-")
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp,
                                                            ignore_errors=True))
        self.path = os.path.join(self.tmp, "winmon_ai_provider.json")

    def test_save_profile_preserves_others_and_never_stores_the_key(self):
        with open(self.path, "w", encoding="utf-8") as fh:
            json.dump({
                "follow_detected": False,
                "active": "deepseek",
                "profiles": {
                    "deepseek": {"kind": "openai", "model": "deepseek-chat",
                                 "enabled": True},
                },
            }, fh)
        ok, msg = provider.save_profile(
            "moonshot", base_url="https://api.moonshot.cn/v1",
            model="kimi-k2-0905-preview",
            api_key_env="WINMON_MOONSHOT_API_KEY", path=self.path)
        self.assertTrue(ok, msg)
        with open(self.path, encoding="utf-8") as fh:
            raw = fh.read()
        self.assertNotIn("sk-secret", raw)
        data = json.loads(raw)
        self.assertFalse(data["follow_detected"], "不能把跟随开关改掉")
        self.assertEqual(set(data["profiles"]), {"deepseek", "moonshot"})
        self.assertEqual(data["active"], "moonshot")
        cfg = provider.resolve_profile("moonshot", self.path)
        self.assertIsNotNone(cfg)
        self.assertEqual(cfg.model, "kimi-k2-0905-preview")
        self.assertEqual(cfg.api_key_env, "WINMON_MOONSHOT_API_KEY")
        self.assertTrue(cfg.enabled)

    def test_custom_profile_requires_address_and_model(self):
        ok, msg = provider.save_profile("custom", path=self.path)
        self.assertFalse(ok)
        self.assertIn("接口地址", msg)
        ok, msg = provider.save_profile(
            "custom", base_url="https://example.invalid/v1", model="",
            api_key_env="WINMON_AI_API_KEY", path=self.path)
        self.assertFalse(ok)
        self.assertIn("模型", msg)

    def test_local_http_is_allowed_but_remote_http_is_not(self):
        local = provider.ProviderConfig(
            kind="openai", base_url="http://127.0.0.1:11434/v1",
            model="llama3.1", api_key_env="WINMON_OLLAMA_API_KEY",
            enabled=True)
        remote = provider.ProviderConfig(
            kind="openai", base_url="http://example.com/v1", model="x",
            api_key_env="WINMON_AI_API_KEY", enabled=True)
        with mock.patch.dict(os.environ, {"WINMON_OLLAMA_API_KEY": "local-key",
                                          "WINMON_AI_API_KEY": "remote-key"},
                             clear=False):
            self.assertEqual(local.validate(), "")
            self.assertIn("https", remote.validate())


@h.needs_tk
class TestProviderPickerUi(unittest.TestCase):
    class FakeApp:
        dpi_scale = 1.0
        _ai_assist = {"mode": "local", "break_enabled": True, "workspace_dir": "",
                      "enabled": False, "auto_explain": False}
        _ai_connect_window = None

        def __init__(self):
            self.saved = []

        def _ai_provider_snapshot(self):
            return {"api_key_env": "WINMON_AI_API_KEY", "key_present": False}

        def _ai_connection_snapshot(self):
            return {"kind": "local"}

        def set_assist(self, **kw):
            self.saved.append(dict(kw))
            self._ai_assist.update(kw)
            return dict(self._ai_assist)

    def setUp(self):
        import tkinter as tk
        import winmon
        self.root = tk.Tk()
        self.root.withdraw()
        self.addCleanup(h.destroy_root, self.root)
        self.winmon = winmon
        self.app = self.FakeApp()

    def test_dropdown_lists_every_provider_and_prefills_fields(self):
        with mock.patch.object(provider, "resolve_profile", return_value=None):
            win = self.winmon._AiConnectWindow(self.root, self.app)
            try:
                values = list(win.key_provider_combo.cget("values"))
                for key in provider.PROFILE_ORDER:
                    self.assertIn(provider.profile_label(key), values)
                self.assertIn(win._key_target_placeholder, values)
                win.key_provider_var.set(provider.profile_label("moonshot"))
                win.change_key_provider()
                self.assertEqual(win.key_env, "WINMON_MOONSHOT_API_KEY")
                self.assertEqual(win.key_model_var.get(),
                                 "kimi-k2-0905-preview")
                self.assertIn("api.moonshot.cn", win.key_base_var.get())
            finally:
                win.close()

    def test_save_key_creates_profile_for_the_selected_provider(self):
        tmp = tempfile.mkdtemp(prefix="winmon-b47-ui-")
        self.addCleanup(lambda: __import__("shutil").rmtree(tmp, ignore_errors=True))
        target = os.path.join(tmp, "provider.json")
        try:
            with mock.patch.object(provider, "provider_path",
                                   return_value=target):
                win = self.winmon._AiConnectWindow(self.root, self.app)
                try:
                    win.key_provider_var.set(provider.profile_label("google"))
                    win.change_key_provider()
                    win.key_var.set("test-key-not-persisted")
                    win.use_key()
                finally:
                    win.close()
            self.assertFalse(os.path.exists(target + ".tmp"))
            with open(target, encoding="utf-8") as fh:
                raw = fh.read()
            data = json.loads(raw)
            self.assertIn("google", data["profiles"])
            self.assertEqual(data["profiles"]["google"]["model"],
                             "gemini-2.5-flash")
            self.assertNotIn("test-key-not-persisted", raw)
            self.assertEqual(os.environ.get("WINMON_GEMINI_API_KEY"),
                             "test-key-not-persisted")
        finally:
            os.environ.pop("WINMON_GEMINI_API_KEY", None)


if __name__ == "__main__":
    unittest.main(verbosity=2)
