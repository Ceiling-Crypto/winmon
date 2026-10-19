# -*- coding: utf-8 -*-
"""
B5 —— 视频联动：**标题/来源判定 + 关闭视频窗口**（含真实端到端）。

> 本文件在 2026-10-12 被裁剪过一次：明确放弃了"通过图像变化自动识别并打开
> 视频"这条需求，于是 `plan_video_open()`（打开决策表）、`detect_video_sources()`
> （来源打分）、视频来源弹窗、`--videos` 命令行**连同它们的用例一起删掉了**。
> 现在视频联动只剩下「监视当前窗口 / 停止监视 / 直接关闭视频」，对应：
>   · `probe_window_as_video()`   → 判断选中的窗口是不是在放视频
>   · `stop_video_in_window()`    → 发 WM_CLOSE 并**回读核对**（绝不强杀）
> 这里保留的用例仍然守着这些能力，以及它们的判定依据（标题/进程类型）。
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import _helpers as h                       # noqa: E402
import winmon_core as core                 # noqa: E402


#: 靶子窗口脚本：`python target.py <标题> <模式>`
#:   normal = 普通窗口，收到 WM_CLOSE 就走（验证"关窗真的关掉了"）
#:   tray   = 最小化到托盘：收到 WM_CLOSE 只藏窗口、进程继续活着
#:            （验证 WinMon 会**升级为结束进程**，否则视频还在后台出声）
#:   ignore = 弹一个"确认退出"框、忽略 WM_CLOSE
#:            （验证 WinMon **不越权**替用户强杀）
#:   animate= 画面持续变化（用色块循环代替真实影片），给播放判定当靶子
CHILD_WINDOW_SCRIPT = r'''
import sys
import tkinter as tk

title = sys.argv[1] if len(sys.argv) > 1 else "target"
mode = sys.argv[2] if len(sys.argv) > 2 else "normal"

root = tk.Tk()
root.title(title)
root.geometry("640x420+60+60")
label = tk.Label(root, text=title, font=("Microsoft YaHei UI", 12))
label.pack(expand=True, fill="both")


def on_close():
    if mode == "ignore":
        # 故意不关：模拟"弹出确认框、等用户点"，WinMon 不该替他杀进程
        label.configure(text=title + "：确认退出？")
        return
    if mode == "tray":
        # 藏起来但进程不退：句柄还在、不可见 —— WinMon 必须升级为结束进程
        root.withdraw()
        return
    root.destroy()


root.protocol("WM_DELETE_WINDOW", on_close)
root.mainloop()
'''

#: 画面持续变化的靶子（给 `VideoActivityDetector` 用）
ANIMATED_WINDOW_SCRIPT = CHILD_WINDOW_SCRIPT.replace(
    "root.mainloop()", """colors = ["#101820", "#e74c3c", "#2ecc71", "#3498db"]
state = {"i": 0}


def tick():
    state["i"] = (state["i"] + 1) % len(colors)
    label.configure(bg=colors[state["i"]], text=title + " " + "\\u25a0" * (state["i"] + 1))
    root.after(120, tick)


tick()
root.mainloop()""")
class TestBrowserVideoTitle(unittest.TestCase):
    """
    浏览器把应用名缀在标题后面，必须剥掉再判。

    实测踩到：Edge 打开视频页后标题是
    「…_哔哩哔哩_bilibili - 个人 - Microsoft Edge」——
    既不满足 `_VIDEO_TITLE_SUFFIXES`（结尾是 Microsoft Edge），
    又因为含 "bilibili" 被通用兜底排除，于是"浏览器里正在放视频"判不出来。
    """

    def test_browser_wrapped_video_titles_are_detected(self):
        for t in ("2026年荣获世界第一的巴厘岛 3000一晚_哔哩哔哩_bilibili"
                  " - 个人 - Microsoft Edge",
                  "某个视频_哔哩哔哩 - Google Chrome",
                  "某视频 BV1xx411c7mD - Microsoft Edge",
                  "How to write a parser_bilibili - Microsoft Edge"):
            with self.subTest(title=t):
                self.assertTrue(core.looks_like_video_title(t), f"应判为视频：{t}")

    def test_browser_plain_pages_are_not_videos(self):
        """浏览器里的普通网页不能算视频 —— 否则会无缘无故弹"是否关闭"。"""
        for t in ("百度一下，你就知道 - Microsoft Edge",
                  "新标签页 - 个人 - Microsoft Edge",
                  "哔哩哔哩 (゜-゜)つロ 干杯~-bilibili - 个人 - Microsoft Edge",
                  "GitHub - Microsoft Edge"):
            with self.subTest(title=t):
                self.assertFalse(core.looks_like_video_title(t), f"不应判为视频：{t}")

    def test_split_app_tail(self):
        inner, browser = core._split_app_tail("某视频_哔哩哔哩 - 个人 - Microsoft Edge")
        self.assertEqual(inner, "某视频_哔哩哔哩")
        self.assertTrue(browser)
        inner2, browser2 = core._split_app_tail("无标题 - 记事本")
        self.assertEqual(inner2, "无标题 - 记事本")      # 记事本不是"应用名尾巴"
        self.assertFalse(browser2)

    def test_zero_width_space_in_title_does_not_break_it(self):
        """
        真环境实测：Edge 的标题里夹了**零宽空格**（U+200B）。

        抓到的原文（本机，多标签 + 哔哩哔哩视频页）：
        `'…_游戏热门视频 和另外 2 个页面 - 个人 - Microsoft\\u200b Edge'`
        不剥掉它，"Microsoft Edge" 就匹配不上 → 被当成"非浏览器窗口" →
        于是浏览器里的普通网页会走"长中文标题"那条启发式，被误判成在放视频。
        """
        title = ("【血狼/抽卡】惊天三连金四连歪！血狼给双切片man抽卡猛歪，"
                 "甚至歪的人都一样？_游戏热门视频 和另外 2 个页面"
                 " - 个人 - Microsoft\u200b Edge")
        self.assertIn("\u200b", title)
        inner, browser = core._split_app_tail(title)
        self.assertTrue(browser, "带零宽空格的 Edge 标题没被认成浏览器窗口")
        self.assertEqual(inner, "【血狼/抽卡】惊天三连金四连歪！血狼给双切片man抽卡猛歪，"
                                "甚至歪的人都一样？_游戏热门视频 和另外 2 个页面")
        # 剥掉尾巴后确认是浏览器 → 标题启发式不再适用 → 不判成视频
        self.assertFalse(core.looks_like_video_title(title))
        self.assertEqual(core.video_title_strength(title), "none")


# ===========================================================================
# 2. 来源归类：按可执行文件路径认，不看窗口类名
# ===========================================================================


# ===========================================================================
# 2. 来源归类：按可执行文件路径认，不看窗口类名
# ===========================================================================
class TestClassifyVideoApp(unittest.TestCase):
    def test_by_executable_path(self):
        cases = [
            (r"X:\Program Files\bilibili\哔哩哔哩.exe", "client"),
            (r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe", "browser"),
            (r"X:\PotPlayer\PotPlayerMini64.exe", "local"),
            (r"C:\Windows\System32\notepad.exe", "unknown"),
        ]
        for path, want in cases:
            with self.subTest(path=path):
                _app, kind = core.classify_video_app(os.path.basename(path), path)
                self.assertEqual(kind, want)

    def test_by_process_name_when_path_missing(self):
        """中文进程名（哔哩哔哩.exe）在个别接口上取不到路径，只能靠名字兜。"""
        for proc, want in (("哔哩哔哩", "client"), ("msedge", "browser"),
                           ("VLC", "local"), ("explorer", "unknown")):
            with self.subTest(proc=proc):
                _app, kind = core.classify_video_app(proc, "")
                self.assertEqual(kind, want)

    def test_window_class_is_never_used(self):
        """
        为什么必须看路径：哔哩哔哩客户端与某个 AI 客户端都是 Electron、
        窗口类名都是 `Chrome_WidgetWin_1`（HANDOFF 坑 39）。
        归类函数**根本不接受**类名参数，所以不可能靠它认错。
        """
        import inspect
        params = list(inspect.signature(core.classify_video_app).parameters)
        self.assertEqual(params, ["proc", "path"])
class TestTitleStrength(unittest.TestCase):
    """
    "像播放页"必须分**铁证**（播放页后缀 / BV 号）与**启发式**（长中文标题）。

    实测踩到的坑（当时，很危险）：`--videos` 把「继续开发 winmon 监控工具
    — 某个 AI 客户端」判成了播放页 —— 它只有 8 个汉字，命中了那条通用启发式；
    于是它成了**得分最高的视频来源**，弹窗还写着"当前标题已是播放页"。
    如果用户顺手点「是，关闭窗口」，关掉的就是正在干活的编辑器。
    """

    def test_strong_evidence(self):
        self.assertEqual(core.video_title_strength("【某视频】第一讲_哔哩哔哩"), "strong")
        self.assertEqual(core.video_title_strength("某视频_哔哩哔哩_bilibili"), "strong")
        self.assertEqual(core.video_title_strength("随便看看 BV1xx411c7mD"), "strong")

    def test_weak_heuristic_only(self):
        self.assertEqual(
            core.video_title_strength("继续开发 winmon 监控工具 — 某个 AI 客户端"), "weak")
        self.assertEqual(
            core.video_title_strength("WinMon 桌面监控工具"), "weak")
        self.assertEqual(core.video_title_strength("深度学习入门到放弃的第一课"), "weak")

    def test_not_video(self):
        for t in ("", "无标题 - 记事本", "百度一下，你就知道 - Microsoft Edge",
                  "哔哩哔哩 (゜-゜)つロ 干杯~-bilibili", "bilibili"):
            with self.subTest(title=t):
                self.assertEqual(core.video_title_strength(t), "none")

    def test_looks_like_video_title_still_accepts_weak(self):
        """`looks_like_video_title` 的语义不变（weak 也算 True），强度另取。"""
        self.assertTrue(core.looks_like_video_title("深度学习入门到放弃的第一课"))
        self.assertFalse(core.looks_like_video_title("无标题 - 记事本"))


@h.needs_tk


class TestCloseModeDefaults(unittest.TestCase):
    """关闭方式的下拉框映射（界面显示中文，设置里存 slug）。"""

    def test_default_is_close_window(self):
        import winmon
        self.assertEqual(winmon.DEFAULT_VIDEO_CLOSE_MODE, "window")
        self.assertEqual(winmon.VIDEO_CLOSE_MODES[0][0], "window",
                         "默认项必须是列表第一项（下拉框的初始显示取它）")
        self.assertEqual(winmon._close_mode_label("window"), "关闭当前窗口")

    def test_slug_accepts_label_and_legacy_slug(self):
        import winmon
        self.assertEqual(winmon._close_mode_slug("关闭当前窗口"), "window")
        self.assertEqual(winmon._close_mode_slug("window"), "window")
        self.assertEqual(winmon._close_mode_slug("暂停并返回"), "smart")
        self.assertEqual(winmon._close_mode_slug("smart"), "smart")   # 老设置
        self.assertEqual(winmon._close_mode_slug("完全看不懂"), "window")

    def test_dialog_buttons_say_what_will_happen(self):
        """按钮文字必须点明动作 —— 原来只写"关闭"，实际可能只是暂停。"""
        import winmon
        ask, yes = winmon._video_dialog_text("window")
        self.assertIn("窗口", ask)
        self.assertIn("关闭窗口", yes)
        self.assertIn("Space", yes)                     # 快捷键说明不能丢
        self.assertIn("标签页", winmon._video_dialog_text("close")[1])
        self.assertIn("暂停", winmon._video_dialog_text("pause")[1])
        self.assertIn("返回", winmon._video_dialog_text("back")[1])


@h.needs_tk
class TestCloseWindowEndToEnd(unittest.TestCase):
    """
    **真实端到端**：真开一个别的进程的窗口 → `stop_video_in_window(mode="window")`
    → 核对窗口真的消失了。

    为什么值得单独写：这是"完整测试"里最硬的一环，而且它证明了一件关键的事 ——
    关闭窗口走的是 `WM_CLOSE`，**不需要窗口在前台、也不需要键盘注入**，
    所以在这台"注入不进去"的机器上它依然可用（而 Ctrl+W 那条路不行）。
    """

    def _spawn_window(self, title: str, script: str = CHILD_WINDOW_SCRIPT,
                      mode: str = "normal"):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = os.path.join(tmp.name, "target.py")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(script)
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        proc = subprocess.Popen([sys.executable, path, title, mode],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                creationflags=flags)
        h.spawn_cleanup(self, proc)
        wi = None
        for _ in range(60):
            if proc.poll() is not None:
                err = (proc.stderr.read() or b"").decode("utf-8", "replace")
                self.skipTest(f"靶子进程提前退出：{err[-300:]}")
            wi = h.pick_window_for_pid(proc.pid)
            if wi is not None and wi.rect_w >= 300:
                break
            wi = None
            time.sleep(0.2)
        if wi is None:
            self.skipTest("没能创建可见的靶子窗口（环境没有图形会话）")
        return proc, wi

    def test_close_window_mode_really_closes_it(self):
        proc, wi = self._spawn_window("WinMon-Close-Target")
        ok, msg = core.stop_video_in_window(wi, mode="window")
        self.assertTrue(ok, f"关闭窗口失败：{msg}")
        self.assertIn("已关闭", msg)
        # 双重核对：窗口句柄没了，而且进程也退出了
        self.assertFalse(bool(core.user32.IsWindow(core.w.HWND(wi.hwnd))),
                         "返回成功，但窗口句柄仍然有效")
        self.assertTrue(h.wait_dead(proc.pid, 5.0),
                        "窗口关了但进程没退出（Tk 主循环应当随之结束）")

    def test_close_mode_helper_verifies_vanished_window(self):
        """窗口本来就不在时，必须如实说"已经不在了"，而不是假装关成功。"""
        ok, msg = core.close_window_and_verify(0xFFFF0000)
        self.assertTrue(ok)
        self.assertIn("已经不在了", msg)

    def test_unknown_mode_falls_back_to_closing_window(self):
        """设置里存了认不出来的值时，按最稳妥的"关窗口"处理。"""
        proc, wi = self._spawn_window("WinMon-Close-Target-Fallback")
        ok, msg = core.stop_video_in_window(wi, mode="whatever")
        self.assertTrue(ok, msg)
        self.assertIn("已关闭", msg)
        self.assertTrue(h.wait_dead(proc.pid, 5.0))

    def test_tray_app_is_escalated_to_process_kill(self):
        """
        **最小化到托盘的视频客户端**：WM_CLOSE 只把窗口藏起来，进程还活着。

        真环境实测（哔哩哔哩客户端）：发完 `WM_CLOSE` 后
        `IsWindowVisible=False`、句柄仍在、6 个进程全都活着 ——
        也就是说视频很可能**还在后台出声**，"关闭窗口"没达到目的。
        所以这里必须升级为结束进程，否则就是假成功。
        """
        proc, wi = self._spawn_window("WinMon-Tray-Target", mode="tray")
        ok, msg = core.stop_video_in_window(wi, mode="window")
        self.assertTrue(ok, f"托盘程序没有被处理：{msg}")
        self.assertIn("托盘", msg)
        self.assertIn("结束进程", msg)
        self.assertTrue(h.wait_dead(proc.pid, 6.0), "升级结束进程后进程仍在")

    def test_app_that_ignores_close_is_not_killed(self):
        """
        窗口仍然可见（多半弹了"确认退出"）→ **不擅自结束进程**，如实报告。

        这条守住的是"不越权"：用户还没确认，程序不该替他杀掉一个可见的程序。
        """
        proc, wi = self._spawn_window("WinMon-Ignore-Target", mode="ignore")
        ok, msg = core.stop_video_in_window(wi, mode="window")
        self.assertFalse(ok, f"窗口还在却报成功：{msg}")
        self.assertIn("仍在", msg)
        self.assertTrue(core.is_process_alive(proc.pid),
                        "不该把一个'还在显示确认框'的程序直接杀掉")
        self.assertTrue(bool(core.user32.IsWindowVisible(core.w.HWND(wi.hwnd))))

    def test_detect_playing_then_close_window_end_to_end(self):
        """
        **真·端到端**：一个真的在动的窗口 → 判定"播放中" → 关闭当前窗口 → 核对没了。

        这是本次要补的"完整测试"里最关键的一条：以前只有零散的单元测试，
        从没把「识别播放 → 关掉它」整条串起来真跑一遍。
        唯一的替身是"视频"本身（用颜色循环代替真实影片，真视频要联网开客户端），
        检测逻辑（`VideoActivityDetector`）与关窗动作（`WM_CLOSE` + 核对）都是真代码。
        """
        proc, wi = self._spawn_window("WinMon-Playing-Target", script=ANIMATED_WINDOW_SCRIPT)
        det = core.VideoActivityDetector(wi)
        try:
            playing, info = det.is_playing(2.0)
        finally:
            det.close()
        if not info["series"] or max(info["series"] or [0]) == 0:
            self.skipTest("这个环境抓不到该窗口的画面变化，无法验证播放判定"
                          f"（series={info['series'][:4]}）")
        self.assertTrue(playing, f"画面在持续变化的窗口没被判成播放中：{info['reason']}")
        self.assertGreater(info["avg_diff"], 0.0)

        ok, msg = core.stop_video_in_window(wi, mode="window")
        self.assertTrue(ok, f"关闭窗口失败：{msg}")
        self.assertIn("已关闭", msg)
        self.assertTrue(h.wait_dead(proc.pid, 5.0), "关窗后进程没有退出")