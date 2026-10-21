# -*- coding: utf-8 -*-
"""
B34 —— 纯白主题、预览主体/工具栏、字号与社区人设库。

用户 2026-10-19 / 10-20 的要求里，这一组守住其中可程序化的部分：
  · 字号默认 9，最小 9，上限不超过界面能容纳的 12；
  · 主界面纯白底黑字，但 AI / 媒体高亮卡片保持原色；
  · 勾选框选中时是整格填充，不再是「×」；
  · 实时预览是主体，窗口列表是右侧紧凑侧栏；预览会自动填满画布；
  · 预览下方只有「请求关闭 / 强制结束」两个操作，向上弹出、选中项填充；
  · AI 助手不再重复显示「当前工作」；
  · 人设下拉有「更多…」，社区角色卡可读取、命名、保存、删除；
  · MCP 配置使用当前解释器，提示里不写死本机盘符。
"""
from __future__ import annotations

import base64
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
        self.assertEqual(winmon.FONT_SIZE_MIN, 9)
        self.assertGreaterEqual(winmon.FONT_SIZE_MAX, 12)
        self.assertEqual([label for _v, label in winmon.FONT_SIZE_CHOICES],
                         [str(n) for n in range(winmon.FONT_SIZE_MIN,
                                                winmon.FONT_SIZE_MAX + 1)])
        winmon.set_font_size(winmon.FONT_SIZE_MAX)
        self.assertIn(str(winmon.FONT_SIZE_MAX), str(winmon.UI_FONT))
        self.assertIn(str(winmon.FONT_SIZE_MAX + 2), str(winmon.TITLE_FONT))
        self.assertIn(str(winmon.FONT_SIZE_MAX - 1), str(winmon.SMALL_BOLD_FONT))
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
class TestLightThemeAndPreviewFirst(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import tkinter as tk
        import winmon
        import winmon_core as core
        cls.winmon = winmon
        cls.core = core
        winmon.apply_theme("light")
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
        cls.winmon.apply_theme("light")

    def test_klein_glass_theme_but_highlight_cards_stay_colored(self):
        self.assertEqual(str(self.root.cget("background")), self.winmon.UI_BG)
        self.assertEqual(self.winmon.UI_BG, "#04122f")
        self.assertNotEqual(self.winmon.UI_BG, "#ffffff",
                            "默认主题是克莱因蓝玻璃，不再保留纯白页")
        self.assertGreater(self.winmon.color_luminance(self.winmon.UI_INK), 0.5,
                           "深蓝底必须配浅色字")
        self.assertNotEqual(str(self.app.media_label.cget("bg")),
                            str(self.root.cget("background")),
                            "媒体高亮卡片不能被背景吃掉")
        self.assertNotEqual(str(self.app.ai_state_label.cget("bg")),
                            str(self.root.cget("background")),
                            "AI 高亮卡片不能被背景吃掉")
        from tkinter import ttk
        style = ttk.Style(self.root)
        button_bg = str(style.lookup("TButton", "background"))
        self.assertNotIn(button_bg, ("#000000", self.winmon.UI_BG),
                         "按钮要和深蓝页面区分开")

    def test_checkbox_indicator_is_a_filled_circle(self):
        from tkinter import ttk
        style = ttk.Style(self.root)
        names = [str(n) for n in style.element_names()
                 if str(n).startswith("WinMon.CheckIndicator")]
        self.assertTrue(names, "圆形勾选图片元素不存在")
        layout = str(style.layout("TCheckbutton"))
        self.assertIn(names[0], layout)
        images = getattr(self.root, "_winmon_check_images", None)
        self.assertTrue(images, "勾选图片必须挂在 root 上，避免跨 Tk root 失效")
        # 圆心有色、四角透明：这才是圆填充，而不是整块方块。
        selected = images["selected"]
        self.assertNotEqual(tuple(selected.get(7, 7)), (0, 0, 0),
                            "选中圆应有可见填充")
        self.assertEqual(tuple(selected.get(0, 0)), (0, 0, 0),
                         "圆的四角必须透明，不能退化成方块")

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
        widths = sum(int(self.app.tree.column(c, "width"))
                     for c in ("#0",) + shown)
        self.assertLessEqual(
            widths, self.app.tree.winfo_width(),
            f"侧栏显示列合计 {widths}px 超过 Treeview 宽度 "
            f"{self.app.tree.winfo_width()}px，状态列会被裁掉")
        self.assertEqual(self.app.notebook.index("current"), 0)

    def test_preview_toolbar_has_two_actions_and_fill_state(self):
        pane = self.app.preview
        self.addCleanup(pane.close_actions)
        self.assertEqual([key for key, _label in self.winmon.PREVIEW_ACTIONS],
                         ["close", "terminate"])
        self.assertEqual(pane.action, "close")
        pane.open_actions()
        self.assertIsNotNone(pane._action_popup)
        row, _box, _txt, _detail, _col = pane._action_rows["close"]
        other, _box2, _txt2, _detail2, _col2 = pane._action_rows["terminate"]
        self.assertEqual(str(row.cget("bg")), self.winmon.UI_ACCENT,
                         "当前操作必须整行填充，用户一眼知道选了哪个")
        self.assertEqual(str(other.cget("bg")), self.winmon.UI_BG,
                         "未选中的操作保持白底")
        pane.choose_action("terminate")
        self.assertEqual(pane.action, "terminate")
        self.assertIn("强制结束", str(pane.action_button.cget("text")))
        self.assertIsNone(pane._action_popup)
        texts = [str(w.cget("text")) for w in walk(pane.frame)
                 if w.winfo_class() in ("TButton", "Button")]
        self.assertNotIn("结束整树", texts,
                         "预览工具条只留两个不可逆动作，整树在右键菜单里")

        calls = []
        with mock.patch.object(self.app, "action_close_window",
                               side_effect=lambda: calls.append("close")), \
                mock.patch.object(self.app, "action_terminate",
                                  side_effect=lambda: calls.append("terminate")):
            pane.run_action()
        self.assertEqual(calls, ["terminate"])

    def test_preview_capture_size_follows_the_canvas(self):
        self.root.update()
        cw, ch = self.app.preview.canvas_size()
        self.assertGreater(cw, 300)
        self.assertGreater(ch, 200)
        ow, oh = self.app._current_out_size()
        self.assertEqual(ow, max(320, min(2200, cw)))
        self.assertEqual(oh, max(200, min(1400, ch)))
        self.assertFalse(hasattr(self.winmon, "PREVIEW_SIZES"),
                         "误导性的固定预览尺寸已经删除")

    def test_small_frame_is_enlarged_instead_of_left_tiny(self):
        pane = self.app.preview
        self.addCleanup(pane.clear)
        cw, _ch = pane.canvas_size()
        png = self.core.bgr_to_png(bytes(100 * 50 * 3), 100, 50)
        pane.set_png(base64.b64encode(png))
        pane._place_image()
        self.assertGreater(pane.photo_w, 100,
                           "小图必须按画布放大，不能缩在角落")
        self.assertLessEqual(pane.photo_w, cw * 2,
                             "放大不能超过预览区，避免把画面裁掉")

    def test_preview_accepts_raw_png_without_base64(self):
        """CPU 优化：上屏链路直接传原始 PNG 字节，仍要兼容旧 base64 调用。"""
        pane = self.app.preview
        self.addCleanup(pane.clear)
        png = self.core.bgr_to_png(bytes(100 * 50 * 3), 100, 50)
        pane.set_png(png)
        self.assertIsNotNone(pane.base_photo)
        self.assertEqual((pane.base_w, pane.base_h), (100, 50))

    def test_png_encoder_preserves_bgr_channels(self):
        """蓝底 BGR 必须解成 RGB 蓝色；编码错通道会让整个预览变黑。"""
        import tkinter as tk
        png = self.core.bgr_to_png(bytes([0, 0, 255]) * 100 * 50, 100, 50)
        self.assertTrue(png.startswith(b"\x89PNG\r\n\x1a\n"))
        photo = tk.PhotoImage(master=self.root, data=base64.b64encode(png))
        self.assertEqual(tuple(photo.get(5, 5)), (255, 0, 0))


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

        class FakeApp:
            dpi_scale = 1.0
            _ai_sessions = None

            def _ai_provider_snapshot(self):
                return {}

            def _set_status(self, _text):
                pass

            def action_ai_assistant(self):
                pass

        with mock.patch.object(winmon.core, "get_autostart", return_value=False):
            dlg = winmon._SettingsWindow(root, FakeApp(), "exit", False, False,
                                         30, "zh", 9, "light")
        try:
            texts = [str(w.cget("text")) for w in walk(dlg.top)
                     if w.winfo_class() == "Label"]
            self.assertIn("字号大小", texts)
        finally:
            dlg._cancel()
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


class TestInitialPreviewChoice(unittest.TestCase):
    class Win:
        def __init__(self, hwnd, pid=10, iconic=False, hidden=False):
            self.hwnd = hwnd
            self.pid = pid
            self.iconic = iconic
            self.is_hidden_surface = hidden

    def test_prefers_foreground_visible_window_and_skips_self(self):
        import winmon
        wins = [self.Win(1, pid=99), self.Win(2, pid=10),
                self.Win(3, pid=11, iconic=True), self.Win(4, pid=12, hidden=True)]
        self.assertEqual(winmon.choose_initial_preview(
            wins, fg_hwnd=2, self_pid=99).hwnd, 2)
        self.assertEqual(winmon.choose_initial_preview(
            wins, fg_hwnd=0, self_pid=99).hwnd, 2)
        self.assertIsNone(winmon.choose_initial_preview(
            [self.Win(1, pid=99)], self_pid=99))


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
