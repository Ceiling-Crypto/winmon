# -*- coding: utf-8 -*-
"""
B32 —— AI 自由发挥：素材包 / 人设 / 手动“搭子说一句”（阶段 8.1）。

用户 2026-10-19 的决定：
  · 固定句式只做兜底，措辞交给 AI 自由发挥；
  · 默认“关心型”；
  · 媒体/游戏先只读**应用名**，网页端读**标题**；
  · B（手动“搭子说一句”）可以先在 D:\\test 跑一次。

这里守住：素材包只含事实、不含曲名/歌手/非浏览器标题；人设卡可复制；
本地兜底按检测到的应用名说，不硬编码本机媒体。
"""
from __future__ import annotations

import io
import os
import sys
import time
import unittest
from contextlib import redirect_stdout
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import _helpers as h                     # noqa: E402
import winmon_ai as ai                   # noqa: E402
import winmon_ai_provider as provider    # noqa: E402
import winmon_core as core               # noqa: E402
import winmon_i18n as i18n               # noqa: E402


class W:
    """最小窗口替身（只给 foreground_facts 用到的字段）。"""

    def __init__(self, hwnd, proc, title, name):
        self.hwnd = hwnd
        self.proc = proc
        self.title = title
        self.real_name = name
        self.is_hidden_surface = False


def _night():
    return time.mktime((2026, 10, 19, 1, 54, 0, 0, 0, -1))


class TestMaterialFacts(unittest.TestCase):
    def setUp(self):
        i18n.set_language("zh")
        self.addCleanup(i18n.set_language, "zh")

    def test_media_facts_keeps_app_and_playing_only(self):
        picked = {"app": "cloudmusic.exe", "title": "某首歌", "artist": "某歌手",
                  "status": 4}
        out = ai.media_facts(picked)
        self.assertIsNotNone(out)
        self.assertEqual(out["playing"], True)
        self.assertNotIn("title", out, "媒体先只读应用名，不传曲名")
        self.assertNotIn("artist", out)
        # 不硬编码本机应用：显示名走现有 media_app_display_name
        self.assertEqual(out["app"], core.media_app_display_name("cloudmusic.exe")
                         or "cloudmusic.exe")

    def test_foreground_browser_title_but_not_coding_title(self):
        browser = W(1, "msedge.exe", "Python 文档 — Microsoft Edge", "Microsoft Edge")
        got = ai.foreground_facts([browser], 1, read_web_title=True)
        self.assertEqual(got["kind"], "查资料")
        self.assertIn("Python", got["title"])
        hidden = ai.foreground_facts([browser], 1, read_web_title=False)
        self.assertEqual(hidden["title"], "")
        code = W(2, "Code.exe", "secret_project.py — Visual Studio Code",
                 "Visual Studio Code")
        got = ai.foreground_facts([code], 2, read_web_title=True)
        self.assertEqual(got["kind"], "编程")
        self.assertEqual(got["title"], "", "非浏览器不读标题")

    def test_game_facts_only_app_name(self):
        game = W(3, "steam.exe", "Steam", "Steam")
        fg = ai.foreground_facts([game], 3)
        self.assertEqual(fg["kind"], "游戏")
        self.assertEqual(fg["title"], "")
        comp = ai.companion_payload(_night(), foreground=fg, read_game=True)
        self.assertEqual(comp["facts"]["game"]["app"], "Steam")
        self.assertNotIn("title", comp["facts"]["game"])
        off = ai.companion_payload(_night(), foreground=fg, read_game=False)
        self.assertIsNone(off["facts"]["game"])
        self.assertNotEqual(off["facts"]["foreground"]["kind"], "游戏",
                            "关掉游戏读取后不能还从 kind 里泄露")


class TestPersona(unittest.TestCase):
    def setUp(self):
        i18n.set_language("zh")
        self.addCleanup(i18n.set_language, "zh")

    def test_default_is_care_and_persona_card_has_boundaries(self):
        self.assertEqual(ai.DEFAULT_PERSONA, "care")
        self.assertEqual(ai.persona_label("care"), "关心型")
        card = ai.persona_card("care")
        self.assertEqual(card["id"], "care")
        self.assertTrue(any("不评价" in r for r in card["rules"]))
        self.assertTrue(any("不编造" in r for r in card["rules"]))

    def test_persona_prompt_is_reusable_and_does_not_fix_wording(self):
        text = ai.persona_prompt("tease", "zh")
        self.assertIn("人设：损友型", text)
        self.assertIn("不要逐字复制 fallback", text)
        self.assertNotIn("现在 01:54", text, "人设卡里不能写死某句提醒")
        comp = ai.companion_payload(_night(), persona="tease", lang="zh")
        prompt = provider.quip_prompt(comp, "zh")
        self.assertIn("只回一句", prompt)
        self.assertIn("不要逐字复制", prompt)

    def test_companion_carries_persona_and_free_wording_instruction(self):
        comp = ai.companion_payload(_night(), persona="tease", lang="zh")
        self.assertEqual(comp["persona"]["id"], "tease")
        self.assertIn("逐字", comp["instruction"])
        self.assertTrue(comp["speak"])
        self.assertIn("01:54", comp["fallback"], "固定句只留在 fallback 里")


class TestLocalQuip(unittest.TestCase):
    def setUp(self):
        i18n.set_language("zh")
        self.addCleanup(i18n.set_language, "zh")

    def _comp(self, tone):
        fg = ai.foreground_facts([W(1, "Code.exe", "x.py — Code", "VS Code")], 1)
        return ai.companion_payload(
            time.mktime((2026, 10, 19, 15, 0, 0, 0, 0, -1)),
            foreground=fg,
            media={"app": "SomePlayer", "playing": True},
            persona=tone, intent="quip")

    def test_local_quip_uses_detected_app_not_hardcoded(self):
        care = ai.local_quip(self._comp("care"), "zh")
        tease = ai.local_quip(self._comp("tease"), "zh")
        self.assertIn("SomePlayer", care)
        self.assertIn("SomePlayer", tease)
        self.assertNotIn("网易云", care + tease, "不能把本机环境写死")
        self.assertNotEqual(care, tease, "不同人设应给出不同说法")
        paused = ai.companion_payload(
            time.mktime((2026, 10, 19, 15, 0, 0, 0, 0, -1)),
            foreground=ai.foreground_facts(
                [W(1, "Code.exe", "x.py — Code", "VS Code")], 1),
            media={"app": "SomePlayer", "playing": False},
            persona="care", intent="quip")
        line = ai.local_quip(paused, "zh")
        self.assertIn("开着", line)
        self.assertNotIn("在放", line, "暂停的媒体不能说成正在播放")

    def test_ai_quip_cli_returns_local_line(self):
        import winmon
        default = ai.load_assist()
        default.update({"read_media": True, "read_game": True,
                        "read_web_title": True, "persona_tone": "care"})
        buf = io.StringIO()
        with mock.patch.object(ai, "load_assist", return_value=default), \
                mock.patch.object(ai, "foreground_facts", return_value=None), \
                mock.patch.object(ai, "media_facts", return_value=None), \
                redirect_stdout(buf):
            code = winmon.ai_quip_cli()
        self.assertEqual(code, 0)
        out = buf.getvalue()
        self.assertIn("[素材]", out)
        self.assertIn("[人设]", out)
        self.assertTrue(out.strip())


if __name__ == "__main__":
    unittest.main(verbosity=2)
