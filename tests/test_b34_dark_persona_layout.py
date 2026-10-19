# -*- coding: utf-8 -*-
"""
B34 —— 纯黑主题、预览主体、字号与社区人设库。

用户 2026-10-19 的七条要求里，这一组守住其中可程序化的部分：
  · 字号设置默认 9，标签叫「字号大小」，所有窗口字体从同一基准派生；
  · 主界面纯黑底白字，但 AI / 媒体高亮卡片保持原色；
  · 勾选框选中时是整格填充，不再是「×」；
  · 实时预览是主体，窗口列表是右侧紧凑侧栏；
  · AI 助手不再重复显示「当前工作」；
  · 人设下拉有「更多…」，社区角色卡可读取、命名、保存、删除；
  · MCP 配置使用当前解释器，提示里不写死本机盘符。
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
import winmon_i18n as i18n               # noqa: E402


def walk(widget):
    yield widget
    for child in widget.winfo_children():
        yield from walk(child)


class TestFontDefaults(unittest.TestCase):
    def tearDown(self):
        i18n.set_language("zh")

    def test_default_is_nine_and_labels_are_numbers(self):
        import winmon
        old = winmon.UI_FONT_SIZE
        self.addCleanup(winmon.set_font_size, old)
        self.assertEqual(winmon.FONT_SIZE_DEFAULT, 9)
        self.assertEqual([label for _v, label in winmon.FONT_SIZE_CHOICES],
                         ["9", "10", "11", "12"])
        winmon.set_font_size(12)
        self.assertIn("12", str(winmon.UI_FONT))
        self.assertIn("14", str(winmon.TITLE_FONT))
        self.assertIn("11", str(winmon.SMALL_BOLD_FONT))
        winmon.set_font_size(9)

    def test_invalid_saved_font_falls_back_to_nine(self):
        import winmon
        with tempfile.TemporaryDirectory(prefix="winmon-b34-font-") as tmp:
            path = os.path.join(tmp, "settings.json")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write('{"font_size": 99}')
            self.assertEqual(winmon.read_font_size(path), 9)


class TestPersonaDocument(unittest.TestCase):
    def test_community_character_card_v2_reads_name_and_body(self):
        data = {
            "spec": "chara_card_v2",
            "data": {
                "name": "星野",
                "description": "你是星野，语气温和。",
                "personality": "慢热但不冷淡。",
                "scenario": "用户在写代码。",
                "system_prompt": "不要打断用户。",
            },
        }
        with tempfile.TemporaryDirectory(prefix="winmon-b34-persona-") as tmp:
            path = os.path.join(tmp, "card.json")
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(data, fh, ensure_ascii=False)
            doc = ai.load_persona_document(path)
        self.assertEqual(doc["error"], "")
        self.assertEqual(doc["name"], "星野")
        self.assertIn("语气温和", doc["text"])
        self.assertIn("慢热", doc["text"])
        self.assertIn("不要打断", doc["text"])

    def test_markdown_front_matter_and_heading_guess_name(self):
        text = ("---\nname: 老同事\n---\n\n# 老同事\n\n说话直一点，别绕弯。\n")
        with tempfile.TemporaryDirectory(prefix="winmon-b34-persona-") as tmp:
            path = os.path.join(tmp, "role.md")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(text)
            doc = ai.load_persona_document(path)
        self.assertEqual(doc["error"], "")
        self.assertEqual(doc["name"], "老同事")
        self.assertNotIn("name: 老同事", doc["text"])
        self.assertIn("说话直一点", doc["text"])

    def test_library_upsert_rename_and_remove(self):
        lib, entry = ai.upsert_persona([], "同事", "像老同事。")
        self.assertEqual(len(lib), 1)
        self.assertEqual(entry["name"], "同事")
        lib2, entry2 = ai.upsert_persona(lib, "老同事", "像老同事。",
                                         entry["id"])
        self.assertEqual(len(lib2), 1, "同一段文字不能堆出重复条目")
        self.assertEqual(entry2["name"], "老同事")
        lib3 = ai.remove_persona(lib2, entry["id"])
        self.assertEqual(lib3, [])

    def test_custom_name_reaches_prompt_and_companion(self):
        prompt = ai.persona_prompt("custom", "zh", "像老同事。", "老周")
        self.assertIn("人设：老周", prompt)
        comp = ai.companion_payload(persona="custom", persona_custom="像老同事。",
                                    persona_name="老周", lang="zh")
        self.assertEqual(comp["persona"]["label"], "老周")
        self.assertIn("老周", comp["persona_text"])


class TestEnvironmentIndependence(unittest.TestCase):
    def test_mcp_snippet_uses_current_interpreter(self):
        import winmon
        snippet = winmon._AiConnectWindow._config_snippet(None)
        data = json.loads(snippet)
        command = data["mcpServers"]["winmon"]["command"]
        expected = sys.executable or "python"
        if expected.lower().endswith("pythonw.exe"):
            expected = expected[:-11] + "python.exe"
        self.assertEqual(command, expected,
                         "MCP 配置必须指向当前解释器，不能假设 PATH 里有 python")

    def test_workspace_advice_has_no_machine_specific_drive(self):
        self.assertNotIn("D:\\work", ai.workspace_advice("", "zh"))
        self.assertNotIn("D:\\work", ai.workspace_advice("", "en"))


@h.needs_tk
class TestDarkThemeAndPreviewFirst(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import tkinter as tk
        import winmon
        import winmon_core as core
        cls.winmon = winmon
        cls.root = tk.Tk()
        cls.root.geometry("1160x730+0+0")
        app = cls.app = winmon.WinMonApp.__new__(winmon.WinMonApp)
        app.root = cls.root
        app._settings_path = os.path.join(tempfile.mkdtemp(prefix="winmon-b34-"),
                                          "settings.json")
        app._settings_after_id = None
        app.history = core.ActionHistory(log_path=os.path.join(
            tempfile.mkdtemp(prefix="winmon-b34-"), "history.log"))
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
        cls.root.update()

    @classmethod
    def tearDownClass(cls):
        h.destroy_root(cls.root)

    def test_pure_black_theme_but_highlight_cards_stay_colored(self):
        self.assertEqual(str(self.root.cget("background")), self.winmon.UI_BG)
        self.assertEqual(self.winmon.UI_BG, "#000000")
        self.assertNotEqual(str(self.app.media_label.cget("bg")),
                            str(self.root.cget("background")),
                            "媒体高亮卡片不能被黑色主题吃掉")
        self.assertNotEqual(str(self.app.ai_state_label.cget("bg")),
                            str(self.root.cget("background")),
                            "AI 高亮卡片不能被黑色主题吃掉")
        from tkinter import ttk
        style = ttk.Style(self.root)
        self.assertNotEqual(style.lookup("TButton", "background"), "#ffffff")

    def test_checkbox_indicator_is_a_filled_square_not_cross(self):
        from tkinter import ttk
        style = ttk.Style(self.root)
        self.assertIn("WinMon.Checkbutton.indicator", style.element_names())
        layout = str(style.layout("TCheckbutton"))
        self.assertIn("WinMon.Checkbutton.indicator", layout)
        self.assertTrue(getattr(self.root, "_winmon_check_images", None),
                        "勾选图片必须挂在 root 上，避免跨 Tk root 失效")

    def test_preview_is_the_main_pane_and_list_is_a_compact_sidebar(self):
        self.root.update()
        panes = self.app._body.panes()
        self.assertGreaterEqual(len(panes), 2)
        main = self.app.notebook.master
        self.assertEqual(str(panes[0]), str(main),
                         "实时预览必须是 Panedwindow 的第一块（主体）")
        self.assertGreater(self.app.notebook.winfo_width(),
                           self.app.tree.winfo_width() * 1.5)
        shown = tuple(str(c) for c in self.app.tree["displaycolumns"])
        self.assertEqual(shown, ("app", "title", "state"),
                         "侧栏只显示选窗口必需的三列，明细留给悬停/预览")
        self.assertEqual(self.app.notebook.index("current"), 0)


@h.needs_tk
class TestFontAppliesToEveryWindow(unittest.TestCase):
    def test_settings_label_and_toplevel_scale_live(self):
        import tkinter as tk
        import winmon
        old = winmon.UI_FONT_SIZE
        self.addCleanup(winmon.set_font_size, old)
        self.addCleanup(i18n.set_language, "zh")
        root = tk.Tk()
        root.withdraw()
        self.addCleanup(root.destroy)
        top = tk.Toplevel(root)
        top.withdraw()
        label = tk.Label(top, text="窗口文字", font=winmon.UI_FONT)
        label.pack()
        winmon.set_font_size(9)
        winmon.apply_font_size_live(root, 12)
        self.assertEqual(winmon.UI_FONT_SIZE, 12)
        self.assertIn("12", str(label.cget("font")))

        with mock.patch.object(winmon.core, "get_autostart", return_value=False):
            dlg = winmon._SettingsDialog(root, "exit", False, False, 30, "zh", 9)
        try:
            texts = [str(w.cget("text")) for w in walk(dlg.top)
                     if w.winfo_class() == "Label"]
            self.assertIn("字号大小", texts)
        finally:
            dlg.top.destroy()
        winmon.set_font_size(9)


@h.needs_tk
class TestAssistantHasNoCurrentWorkLine(unittest.TestCase):
    class FakeApp:
        dpi_scale = 1.0
        _ai_assist = {"mode": "local"}
        _ai_log_lines = []
        _ai_assistant_window = None

        def _ai_header_text(self):
            return "接入：测试"

        def _ai_time_status(self):
            return "现在 12:00"

        def action_ai_usage(self):
            pass

    def test_window_does_not_repeat_current_work(self):
        import tkinter as tk
        import winmon
        i18n.set_language("zh")
        self.addCleanup(i18n.set_language, "zh")
        root = tk.Tk()
        root.withdraw()
        self.addCleanup(root.destroy)
        app = self.FakeApp()
        win = winmon._AiAssistantWindow(root, app)
        try:
            self.assertFalse(hasattr(win, "work_text"))
            self.assertFalse(hasattr(win, "set_work"))
            texts = []
            for w in walk(win.top):
                try:
                    texts.append(str(w.cget("text")))
                except Exception:
                    pass
            self.assertNotIn("当前工作", "\n".join(texts))
            self.assertEqual(win.time_text.get(), "现在 12:00")
        finally:
            win.close()


@h.needs_tk
class TestPersonaLibraryWindow(unittest.TestCase):
    class FakeApp:
        dpi_scale = 1.0
        _ai_assist = {"mode": "local", "persona_tone": "care",
                      "persona_custom": "", "persona_id": "",
                      "persona_name": "", "persona_library": []}

        def set_assist(self, **kw):
            for key, value in kw.items():
                self._ai_assist[key] = value
            return dict(self._ai_assist)

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
        self.app._ai_assist = dict(self.FakeApp._ai_assist)

    def test_import_rename_and_delete(self):
        win = self.winmon._PersonaManager(self.root, self.app)
        try:
            with tempfile.TemporaryDirectory(prefix="winmon-b34-persona-") as tmp:
                path = os.path.join(tmp, "card.json")
                with open(path, "w", encoding="utf-8") as fh:
                    json.dump({"data": {"name": "星野",
                                        "description": "语气温和。"}},
                              fh, ensure_ascii=False)
                with mock.patch.object(self.winmon.filedialog, "askopenfilename",
                                       return_value=path):
                    win.import_file()
            self.assertEqual(self.app._ai_assist.get("persona_name"), "星野")
            self.assertEqual(self.app._ai_assist.get("persona_tone"), "custom")
            self.assertEqual(len(self.app._ai_assist.get("persona_library") or []), 1)

            win.listbox.selection_clear(0, "end")
            win.listbox.selection_set(0)
            with mock.patch.object(self.winmon.simpledialog, "askstring",
                                   return_value="小星"):
                win.rename()
            self.assertEqual(self.app._ai_assist.get("persona_name"), "小星")
            self.assertEqual(self.app._ai_assist["persona_library"][0]["name"], "小星")

            with mock.patch.object(self.winmon.messagebox, "askyesno",
                                   return_value=True):
                win.delete()
            self.assertEqual(self.app._ai_assist.get("persona_library"), [])
            self.assertEqual(self.app._ai_assist.get("persona_tone"), "care")
            self.assertEqual(self.app._ai_assist.get("persona_custom"), "")
        finally:
            win.close()

    def test_connect_dropdown_offers_more_and_saved_name(self):
        app = self.app
        app._ai_assist.update({
            "persona_tone": "custom", "persona_custom": "像老同事。",
            "persona_id": "abc123", "persona_name": "老同事",
            "persona_library": [{"id": "abc123", "name": "老同事",
                                 "text": "像老同事。"}],
        })
        win = self.winmon._AiConnectWindow(self.root, app)
        try:
            values = list(win.persona_combo.cget("values"))
            self.assertIn(i18n.t("connect.persona_more"), values)
            self.assertIn("老同事", values)
            self.assertEqual(win.persona_var.get(), "老同事")
            win.open_persona_manager()
            self.assertIsNotNone(getattr(win, "_persona_manager", None))
        finally:
            if getattr(win, "_persona_manager", None) is not None:
                win._persona_manager.close()
            win.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
