# -*- coding: utf-8 -*-
"""
B31 —— AI 角色记忆 / 接入建议窗口 / 中英双语（阶段 8）。

用户 2026-10-19 的纠正：
  · 休息提醒和指导建议应当**建立在接入 AI 之后**，由 AI 读角色说明自己说，
    不是 WinMon 按时间弹窗；
  · 接入建议（尤其是“新项目单独建工作区”）要有独立入口；
  · AI 助手窗口太杂，API key / 接入建议应当单独一个窗口；
  · 设置里要有语言选择（中文 / English）。

这里守住：
  · companion 数据只产出、不弹窗；MCP instructions 带角色；
  · `_AiConnectWindow` 独立存在，主助手窗口不再塞 key/值守开关；
  · 语言表可中英往返，设置能持久化。
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
import winmon_i18n as i18n               # noqa: E402
import winmon_mcp as mcp                 # noqa: E402


def _night():
    return time.mktime((2026, 10, 19, 1, 54, 0, 0, 0, -1))


class TestBilingualBase(unittest.TestCase):
    def setUp(self):
        i18n.set_language("zh")
        self.addCleanup(i18n.set_language, "zh")

    def test_mode_and_time_labels_switch_language(self):
        self.assertEqual(ai.assist_mode_label("local", "en"), "Local (0 token)")
        self.assertEqual(ai.assist_mode_label("local", "zh"), "本地（0 token）")
        slot = ai.time_slot(_night(), lang="en")
        self.assertEqual(slot["label"], "deep night")
        self.assertIn("deep night", slot["message"])
        zh = ai.time_slot(_night(), lang="zh")
        self.assertEqual(zh["label"], "深夜")

    def test_translate_text_round_trip(self):
        i18n.set_language("en")
        self.assertEqual(i18n.translate_text("设置…"), "Settings…")
        self.assertEqual(i18n.translate_text("实时预览"), "Live preview")
        i18n.set_language("zh")
        self.assertEqual(i18n.translate_text("Settings…"), "设置…")
        self.assertEqual(i18n.translate_text("不是界面文案"), "不是界面文案")

    def test_t_falls_back_safely(self):
        title = i18n.t("app.title", ver="1.0")
        self.assertEqual(title, "winmon1.0")
        self.assertNotIn("WinMon", title)
        self.assertEqual(i18n.t("no.such.key"), "no.such.key")
        self.assertEqual(i18n.pick("中文", "English"), "中文")

    def test_load_language_from_settings(self):
        with tempfile.TemporaryDirectory(prefix="winmon-i18n-") as tmp:
            path = os.path.join(tmp, "settings.json")
            with open(path, "w", encoding="utf-8") as fh:
                json.dump({"language": "en"}, fh)
            self.assertEqual(i18n.load_from_settings(path), "en")
            self.assertEqual(i18n.get_language(), "en")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("{broken")
            self.assertEqual(i18n.load_from_settings(path), "zh")

    def test_load_assist_keeps_workspace(self):
        with tempfile.TemporaryDirectory(prefix="winmon-b31a-") as tmp:
            path = os.path.join(tmp, "assist.json")
            with mock.patch.dict(os.environ, {"WINMON_AI_ASSIST": path}):
                saved = ai.save_assist({"workspace_dir": "D:\\work\\demo"})
                self.assertEqual(saved["workspace_dir"], "D:\\work\\demo")
                self.assertEqual(ai.load_assist()["workspace_dir"], "D:\\work\\demo")


class TestPromptsCarryRole(unittest.TestCase):
    def setUp(self):
        i18n.set_language("zh")
        self.addCleanup(i18n.set_language, "zh")

    def test_handoff_prompt_includes_role_reminder_and_workspace(self):
        comp = ai.companion_payload(_night(), mode="local",
                                    workspace="D:\\work\\demo", lang="zh")
        text = provider.handoff_prompt("# 现场\n前台：Edge", "为什么卡？",
                                       ai_name="DeepSeek", via_mcp=True,
                                       companion=comp, lang="zh")
        self.assertIn("你的角色", text)
        self.assertIn("winmon_companion", text)
        self.assertIn("01:54", text)
        self.assertIn("once_key", text)
        self.assertIn("D:\\work\\demo", text)
        self.assertIn("为什么卡？", text)

    def test_handoff_prompt_english(self):
        comp = ai.companion_payload(_night(), mode="local",
                                    workspace="D:\\work\\demo", lang="en")
        text = provider.handoff_prompt("# context", "why?", ai_name="DeepSeek",
                                       companion=comp, lang="en")
        self.assertIn("Your role", text)
        self.assertIn("Fallback cue", text)
        self.assertIn("Workspace advice", text)

    def test_assistant_prompt_still_marks_untrusted_data(self):
        text = provider.assistant_prompt("# 现场", "看看")
        self.assertIn("不可信数据", text)
        self.assertIn("不要建议自动结束进程", text)


class TestMcpRoleMemory(unittest.TestCase):
    def test_initialize_instructions_explain_role(self):
        resp = mcp.handle_request({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                   "params": {"protocolVersion": "2025-11-25",
                                              "capabilities": {}}})
        text = resp["result"]["instructions"]
        self.assertIn("winmon_companion", text)
        self.assertIn("休息", text)
        self.assertIn("工作区", text)
        self.assertIn("不可信数据", text)
        self.assertIn("companion", text)

    def test_sessions_tool_returns_companion(self):
        comp = ai.companion_payload(_night(), mode="local", lang="zh")
        state = {
            "sessions": {"primary": {"name": "Claude Desktop", "kind": "desktop",
                                     "confidence": 0.9, "proc": "claude.exe"},
                         "others": [], "note": ""},
            "companion": comp,
        }
        out = mcp.call_tool("winmon_ai_sessions", {}, state)
        self.assertFalse(out.get("isError"))
        self.assertEqual(out["structuredContent"]["companion"]["slot"], "night")
        self.assertNotIn("role_text", out["structuredContent"]["companion"],
                         "角色说明已在 instructions，别每次工具调用都重复塞")
        self.assertIn("伴侣素材", out["content"][0]["text"])
        self.assertIn("01:54", out["content"][0]["text"])


@h.needs_tk
class TestLanguageWidgets(unittest.TestCase):
    def setUp(self):
        import tkinter as tk
        from tkinter import ttk
        i18n.set_language("zh")
        self.addCleanup(i18n.set_language, "zh")
        self.tk = tk
        self.ttk = ttk
        self.root = tk.Tk()
        self.root.withdraw()
        self.addCleanup(self.root.destroy)

    def test_translate_widgets_round_trip(self):
        label = self.ttk.Label(self.root, text="设置…")
        label.pack()
        button = self.ttk.Button(self.root, text="刷新列表")
        button.pack()
        nb = self.ttk.Notebook(self.root)
        nb.add(self.ttk.Frame(nb), text="实时预览")
        nb.pack()
        tree = self.ttk.Treeview(self.root, columns=("state",), show="tree headings")
        tree.heading("#0", text="图标")
        tree.heading("state", text="状态")
        tree.pack()
        i18n.set_language("en")
        changed = i18n.translate_widgets(self.root)
        self.assertGreaterEqual(changed, 5)
        self.assertEqual(label.cget("text"), "Settings…")
        self.assertEqual(button.cget("text"), "Refresh")
        self.assertEqual(nb.tab(0, "text"), "Live preview")
        self.assertEqual(tree.heading("#0").get("text"), "Img")
        self.assertEqual(tree.heading("state").get("text"), "State")
        i18n.set_language("zh")
        i18n.translate_widgets(self.root)
        self.assertEqual(label.cget("text"), "设置…")
        self.assertEqual(nb.tab(0, "text"), "实时预览")

    def test_settings_dialog_has_language_choice(self):
        import winmon

        class FakeApp:
            dpi_scale = 1.0
            _ai_sessions = None

            def _ai_provider_snapshot(self):
                return {}

            def _set_status(self, _text):
                pass

            def action_ai_assistant(self):
                pass

        i18n.set_language("zh")
        with mock.patch.object(winmon.core, "get_autostart", return_value=False):
            dlg = winmon._SettingsWindow(self.root, FakeApp(), "exit", False,
                                         False, 30, "zh")
            self.assertEqual(dlg.language.get(), "中文")
            dlg.language.set("English")
            dlg._ok()
        self.assertEqual(dlg.result.get("language"), "en")


@h.needs_tk
class TestConnectWindow(unittest.TestCase):
    class FakeApp:
        dpi_scale = 1.0
        _ai_assist = {"mode": "local", "break_enabled": True,
                      "workspace_dir": "", "enabled": False, "auto_explain": False}
        _ai_connect_window = None

        def __init__(self):
            self.saved = []

        def _ai_provider_snapshot(self):
            return {"api_key_env": "WINMON_AI_API_KEY"}

        def _ai_connection_snapshot(self):
            return {"kind": "clipboard", "ai": "DeepSeek"}

        def set_assist(self, **kw):
            self.saved.append(kw)
            for key, val in kw.items():
                self._ai_assist[key] = val
            return dict(self._ai_assist)

        def action_ai_connect(self):
            pass

    def setUp(self):
        import tkinter as tk
        import winmon
        i18n.set_language("zh")
        self.addCleanup(i18n.set_language, "zh")
        self.root = tk.Tk()
        self.root.withdraw()
        self.addCleanup(self.root.destroy)
        self.winmon = winmon
        self.app = self.FakeApp()

    def test_window_holds_setup_not_assistant(self):
        win = self.winmon._AiConnectWindow(self.root, self.app)
        self.app._ai_connect_window = win
        self.assertIn("未接入 · 纯本地", win.status_text.get())
        self.assertIn("winmon", win.config_text.get("1.0", "end"))
        win.break_var.set(False)
        win.toggle_break()
        self.assertIn({"break_enabled": False}, self.app.saved)
        win.persona_var.set("损友型")
        win.change_persona()
        self.assertTrue(any(item.get("persona_tone") == "tease"
                            for item in self.app.saved))
        win.copy_persona()
        self.assertIn("人设：损友型", self.root.clipboard_get())
        with tempfile.TemporaryDirectory(prefix="winmon-persona-") as tmp:
            path = os.path.join(tmp, "persona.txt")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("你是用户的桌面搭子，语气像老同事。")
            with mock.patch.object(self.winmon.filedialog, "askopenfilename",
                                   return_value=path):
                win.import_persona()
        self.assertEqual(self.app._ai_assist.get("persona_tone"), "custom")
        self.assertIn("老同事", self.app._ai_assist.get("persona_custom"))
        self.assertIn("persona", win.persona_var.get().lower(),
                      "导入后应使用从文件名/角色卡读到的名称")
        self.assertEqual(self.app._ai_assist.get("persona_name"), "persona")
        self.assertEqual(len(self.app._ai_assist.get("persona_library") or []), 1)
        win.read_media_var.set(False)
        win.toggle_facts()
        self.assertTrue(any(item.get("read_media") is False for item in self.app.saved))
        self.assertFalse(win.proactive_var.get(), "C 默认必须关")
        self.assertFalse(win.allow_focus_var.get())
        self.assertEqual(str(win.allow_focus_chk.cget("state")), "disabled")
        win.proactive_var.set(True)
        win.toggle_proactive()
        self.assertTrue(any(item.get("proactive_enabled") is True
                            for item in self.app.saved))
        self.assertEqual(str(win.allow_focus_chk.cget("state")), "normal")
        win.allow_focus_var.set(True)
        win.toggle_proactive_gates()
        self.assertTrue(any(item.get("allow_focus") is True for item in self.app.saved))
        self.app._ai_assist["workspace_dir"] = "D:\\work\\demo"
        win.copy_advice()
        copied = self.root.clipboard_get()
        self.assertIn("D:\\work\\demo", copied)
        self.assertIn("工作区", copied)
        win.close()
        self.assertIsNone(self.app._ai_connect_window)

    def test_key_stays_in_process_env(self):
        win = self.winmon._AiConnectWindow(self.root, self.app)
        # 新交互：先选直连对象（默认是占位项，不隐式绑定某个厂商）。
        win.key_provider_var.set(i18n.t("connect.key_target_follow"))
        win.change_key_provider()
        win.key_var.set("test-key-b31")
        win.use_key()
        self.assertEqual(os.environ.get("WINMON_AI_API_KEY"), "test-key-b31")
        os.environ.pop("WINMON_AI_API_KEY", None)
