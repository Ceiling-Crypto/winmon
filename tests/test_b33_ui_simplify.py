# -*- coding: utf-8 -*-
"""
B33 —— 界面去技术化：主界面只留动作和高亮数据，长解释进说明窗口。

用户 2026-10-19 的意见：
  · 主界面的「画面与刷新率只影响预览…」「从窗口读的」这类表述太硬核，
    应放进说明窗口；
  · 「接入与建议」文字堆积，说明应折叠，点开再看；
  · API key / MCP 接入要醒目；
  · 主界面识别到的 AI 和媒体要一眼能看到。

这里守住的是**可见层级**，不是像素级截图：
  · 主界面不再出现那几句常驻技术说明；
  · 识别对象用粗体，接入状态有底色；
  · 说明窗口可按标签页切换；
  · 接入窗口的 MCP / key 标题是高亮卡片，配置默认折叠。
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import _helpers as h                     # noqa: E402
import winmon_ai as ai                   # noqa: E402
import winmon_ai_provider as provider     # noqa: E402
import winmon_i18n as i18n               # noqa: E402


def walk(widget):
    yield widget
    for child in widget.winfo_children():
        yield from walk(child)


class TestAiSummary(unittest.TestCase):
    def test_summary_splits_identity_connection_and_usage(self):
        primary = {"name": "Claude Desktop", "kind": "desktop", "proc": "claude.exe"}
        status = {"provider_tokens": 12, "tokenizer_tokens": 3, "estimated_tokens": 4}
        info = {"enabled": True, "key_present": True, "model": "claude-3-5"}
        parts = ai.format_ai_summary(
            primary, status, info,
            connection={"kind": "mcp", "client": "Claude Desktop",
                        "ts": "2026-10-19T19:58:00"},
            mode="local")
        self.assertIn("Claude Desktop", parts["identity"])
        self.assertEqual(parts["status_kind"], "mcp")
        self.assertIn("MCP", parts["connection"])
        self.assertIn("本地", parts["mode"])
        self.assertIn("真实 12", parts["usage"])
        self.assertIn("精确 3", parts["usage"])
        self.assertIn("估算 4", parts["usage"])

    def test_bar_still_composes_the_same_kinds_of_facts(self):
        text = ai.format_ai_bar(
            {"name": "Claude Desktop", "kind": "desktop", "proc": "claude.exe"},
            {"provider_tokens": 12, "tokenizer_tokens": 0, "estimated_tokens": 1},
            {"enabled": True, "key_present": True, "model": "claude-3-5"},
            connection={"kind": "mcp", "client": "Claude Desktop"},
            mode="active")
        for piece in ("Claude Desktop", "已接入 MCP", "主动", "真实 12", "估算 1"):
            self.assertIn(piece, text)

    def test_manual_direct_is_marked(self):
        text = ai.format_ai_bar(
            {"name": "DeepSeek", "kind": "desktop", "proc": "deepseek.exe"},
            {"provider_tokens": 5, "tokenizer_tokens": 0, "estimated_tokens": 0},
            {"enabled": True, "key_present": True, "model": "glm-5.3"},
            connection={"kind": "direct", "model": "glm-5.3", "manual": True})
        self.assertIn("直连就绪（手动）", text)

    def test_provider_profile_lookup_and_key_env(self):
        with tempfile.TemporaryDirectory(prefix="winmon-b33-prof-") as tmp:
            path = os.path.join(tmp, "provider.json")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write('{"profiles":{"openai":{"kind":"openai",'
                         '"api_key_env":"WINMON_OPENAI_API_KEY","model":"gpt"}}}')
            cfg = provider.resolve_profile("openai", path)
            self.assertIsNotNone(cfg)
            self.assertEqual(cfg.model, "gpt")
            self.assertEqual(provider.profile_key_env("openai", path),
                             "WINMON_OPENAI_API_KEY")
            self.assertEqual(provider.profile_key_env("missing", path),
                             "WINMON_AI_API_KEY")


@h.needs_tk
class TestMainUiHierarchy(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import tkinter as tk
        import winmon
        import winmon_core as core
        cls.winmon = winmon
        cls.root = tk.Tk()
        cls.root.geometry("1120x720+0+0")
        app = cls.app = winmon.WinMonApp.__new__(winmon.WinMonApp)
        app.root = cls.root
        app._settings_path = os.path.join(tempfile.mkdtemp(prefix="winmon-b33-"),
                                          "settings.json")
        app._settings_after_id = None
        app.history = core.ActionHistory(log_path=os.path.join(
            tempfile.mkdtemp(prefix="winmon-b33-"), "history.log"))
        app.show_hidden = tk.BooleanVar(value=False)
        app.show_no_window = tk.BooleanVar(value=False)
        app.only_windowed = tk.BooleanVar(value=True)
        app.follow_selection = tk.BooleanVar(value=True)
        app.live_gallery = tk.BooleanVar(value=True)
        app.size_choice = tk.StringVar(value=winmon.PREVIEW_SIZES[1][0])
        app.fps_choice = tk.StringVar(value="很流畅 10 FPS")
        app.search_text = tk.StringVar()
        app.status_text = tk.StringVar(value="就绪")
        app.overview_text = tk.StringVar(value="")
        app._init_video_vars()
        app._build_ui()
        app.dpi_scale = 1.0
        app._guide_window = None
        cls.root.update_idletasks()

    @classmethod
    def tearDownClass(cls):
        h.destroy_root(cls.root)

    def test_long_technical_hints_are_not_in_the_main_window(self):
        texts = []
        for w in walk(self.root):
            try:
                texts.append(str(w.cget("text")))
            except Exception:
                pass
        joined = "\n".join(texts)
        self.assertNotIn("画面与刷新率只影响预览", joined)
        self.assertNotIn("从窗口标题读的", joined)
        self.assertNotIn("勾了才替你启动", joined)

    def test_ai_and_media_are_visually_emphasized(self):
        self.assertIn("bold", str(self.app.ai_identity_label.cget("font")))
        self.assertIn("bold", str(self.app.media_label.cget("font")))
        self.assertNotEqual(str(self.app.media_label.cget("bg")),
                            str(self.root.cget("background")))
        self.assertNotEqual(str(self.app.ai_state_label.cget("bg")),
                            str(self.root.cget("background")))

    def test_actions_live_in_the_left_rail(self):
        labels = [str(w.cget("text")) for w in walk(self.app.action_rail)
                  if w.winfo_class() in ("Button", "TButton")]
        for label in ("加入总览", "移除总览", "AI 助手…", "设置…", "说明…"):
            self.assertIn(label, labels)
        self.assertGreater(self.app.action_rail.winfo_reqwidth(), 50)
        # 顶部不再重复这些动作（否则又回到“一排白按钮”的老路）。
        top_texts = [str(w.cget("text")) for w in walk(self.root)
                     if w.winfo_class() in ("Button", "TButton")]
        self.assertEqual(top_texts.count("加入总览"), 1)

    def test_default_window_is_compact(self):
        self.assertEqual((self.winmon.APP_W, self.winmon.APP_H), (1160, 730))

    def test_ai_card_updates_identity_state_and_usage_separately(self):
        self.app._ai_status_snapshot = lambda: {
            "provider_tokens": 10, "tokenizer_tokens": 2, "estimated_tokens": 3}
        self.app._ai_provider_snapshot = lambda: {
            "enabled": True, "key_present": True, "model": "claude-3-5",
            "connection": {"kind": "mcp", "client": "Claude Desktop"}}
        self.app._update_ai_bar({"primary": {"name": "Claude Desktop",
                                             "kind": "desktop",
                                             "proc": "claude.exe"}})
        self.assertIn("Claude Desktop", self.app.ai_identity_text.get())
        self.assertIn("已接入 MCP", self.app.ai_state_text.get())
        self.assertIn("真实 10", self.app.ai_usage_text.get())
        self.assertEqual(str(self.app.ai_state_label.cget("bg")), "#e2f5e9")

    def test_guide_button_opens_the_separate_help_window(self):
        self.app.action_guide("main")
        win = self.app._guide_window
        self.assertIsNotNone(win)
        try:
            self.root.update()
            self.assertEqual(len(win.nb.tabs()), 3)
            text = win._texts[0].get("1.0", "end")
            self.assertIn("预览大小和刷新速度", text)
            self.assertIn("窗口标题", text)
        finally:
            win.close()
        self.assertIsNone(self.app._guide_window)

    def test_guide_follows_english(self):
        i18n.set_language("en")
        self.addCleanup(i18n.set_language, "zh")
        self.app.action_guide("ai")
        win = self.app._guide_window
        try:
            self.root.update()
            self.assertEqual(win.nb.tab(0, "text"), "Main window")
            self.assertIn("Preview size", win._texts[0].get("1.0", "end"))
            self.assertEqual(win.nb.index("current"), 1)
        finally:
            win.close()


@h.needs_tk
class TestConnectWindowHierarchy(unittest.TestCase):
    class FakeApp:
        dpi_scale = 1.0
        _ai_assist = {"mode": "local", "break_enabled": True, "workspace_dir": "",
                      "enabled": False, "auto_explain": False}
        _ai_connect_window = None

        def __init__(self):
            self.guide_calls = []
            self.saved = []

        def _ai_provider_snapshot(self):
            return {"api_key_env": "WINMON_AI_API_KEY", "key_present": False}

        def _ai_connection_snapshot(self):
            return {"kind": "clipboard", "ai": "DeepSeek"}

        def set_assist(self, **kw):
            self.saved.append(dict(kw))
            self._ai_assist.update(kw)
            return dict(self._ai_assist)

        def action_guide(self, section="main"):
            self.guide_calls.append(section)

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

    def test_mcp_and_key_are_highlighted_and_config_is_collapsed(self):
        win = self.winmon._AiConnectWindow(self.root, self.app)
        try:
            labels = {}
            for w in walk(win.top):
                try:
                    labels[str(w.cget("text"))] = w
                except Exception:
                    pass
            mcp = labels[i18n.t("connect.mcp")]
            key = labels[i18n.t("connect.key")]
            self.assertIn("bold", str(mcp.cget("font")))
            self.assertIn("bold", str(key.cget("font")))
            self.assertEqual(str(mcp.cget("fg")), "#1f6f9e")
            self.assertEqual(str(key.cget("fg")), "#a86b00")
            self.assertFalse(win.mcp_disclosure.open)
            self.assertEqual(win.mcp_disclosure.body.winfo_manager(), "")
            self.assertIn("winmon", win.config_text.get("1.0", "end"))
            win.mcp_disclosure.toggle()
            self.assertEqual(win.mcp_disclosure.body.winfo_manager(), "pack")
        finally:
            win.close()

    def test_key_warning_is_visible_without_expanding(self):
        win = self.winmon._AiConnectWindow(self.root, self.app)
        try:
            self.assertIn("不要写进仓库", i18n.t("connect.key_warning"))
            labels = [str(w.cget("text")) for w in walk(win.top)
                      if w.winfo_class() == "Label"]
            self.assertTrue(any("不要写进仓库" in text for text in labels))
        finally:
            win.close()

    def test_direct_target_is_user_choice_not_auto_deepseek(self):
        win = self.winmon._AiConnectWindow(self.root, self.app)
        try:
            self.assertEqual(win.key_provider_var.get(),
                             i18n.t("connect.key_target_follow"),
                             "默认应是跟随当前 AI，不能直接写死某个厂商的 key")
            with mock.patch.object(self.winmon.provider, "profile_key_env",
                                   return_value="WINMON_OPENAI_API_KEY"):
                win.key_provider_var.set("OpenAI")
                win.change_key_provider()
            self.assertEqual(self.app._ai_assist.get("direct_provider"), "openai")
            self.assertEqual(win.key_env, "WINMON_OPENAI_API_KEY")
            self.assertIn("WINMON_OPENAI_API_KEY", win.key_env_text.get())
            self.assertTrue(any(item.get("direct_provider") == "openai"
                                for item in self.app.saved))
        finally:
            win.close()

    def test_full_guide_button_uses_the_shared_window(self):
        win = self.winmon._AiConnectWindow(self.root, self.app)
        try:
            win.open_guide()
            self.assertEqual(self.app.guide_calls, ["ai"])
        finally:
            win.close()


@h.needs_tk
class TestFontSizeSetting(unittest.TestCase):
    def setUp(self):
        import tkinter as tk
        import winmon
        self.winmon = winmon
        self.old = winmon.UI_FONT_SIZE
        self.addCleanup(winmon.set_font_size, self.old)
        self.root = tk.Tk()
        self.root.withdraw()
        self.addCleanup(self.root.destroy)

    def test_read_font_size_from_settings(self):
        with tempfile.TemporaryDirectory(prefix="winmon-b33-font-") as tmp:
            path = os.path.join(tmp, "settings.json")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write('{"font_size": 12}')
            self.assertEqual(self.winmon.read_font_size(path), 12)
            with open(path, "w", encoding="utf-8") as fh:
                fh.write('{"font_size": 99}')
            self.assertEqual(self.winmon.read_font_size(path),
                             self.winmon.FONT_SIZE_DEFAULT)

    def test_live_resize_updates_existing_widgets(self):
        import tkinter as tk
        label = tk.Label(self.root, text="字号 123", font=self.winmon.UI_FONT)
        label.pack()
        self.root.update_idletasks()
        self.winmon.apply_font_size_live(self.root, 12)
        self.assertIn("12", str(label.cget("font")))
        bigger = label.winfo_reqwidth()
        self.winmon.apply_font_size_live(self.root, 9)
        self.assertIn(" 9", str(label.cget("font")))
        self.assertLess(label.winfo_reqwidth(), bigger)

    def test_settings_dialog_returns_font_size(self):
        with mock.patch.object(self.winmon.core, "get_autostart", return_value=False):
            dlg = self.winmon._SettingsDialog(self.root, "exit", False, False, 30,
                                              "zh", 10)
            dlg.font_size.set("大")
            dlg._ok()
        self.assertEqual(dlg.result["font_size"], 11)


if __name__ == "__main__":
    unittest.main()
