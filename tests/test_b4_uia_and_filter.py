# -*- coding: utf-8 -*-
"""
B4 —— 非程序条目过滤 + UI Automation 读取视频卡片。

这两块都是本轮的实测结论固化：
  · 注册表来源会把 .txt/.chm/.html 带进"程序目录"，必须有过滤规则
  · UIA 能读到哔哩哔哩的视频卡片（无需 --force-renderer-accessibility），
    但**不能**替应用点击 —— 所以只测"读得对"，不测"点得开"
"""

from __future__ import annotations

import os
import queue
import subprocess
import sys
import tempfile
import time
import tkinter as tk
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import _helpers as h                     # noqa: E402
import winmon_core as core               # noqa: E402
import winmon                            # noqa: E402


def pump_media(app, root, cond, timeout: float = 3.0) -> bool:
    """
    把"媒体命令"那条**异步**链路跑完（P1-1 起它是后台线程 + 主线程队列）。

    为什么不能只 `root.update()`：`media_command` 先用 `root.after(1, …)` 把活儿交给
    下一拍（Tk 的 after 要事件循环派发），`_media_command_async` 再起后台线程，
    线程的结论经 `_ui_call` 进队列 —— 队列必须由主线程的泵 `_drain_ui_msgs()` 取走。
    所以"跑完" = `root.update()` 与 `_drain_ui_msgs()` 轮流来几拍，而不是 sleep 死等。
    （顺带一条：断言必须在 `mock.patch` 的 with 里等，否则后台线程读到的是真函数。）
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        root.update()
        app._drain_ui_msgs()
        if cond():
            return True
        time.sleep(0.02)
    root.update()
    app._drain_ui_msgs()
    return cond()


class _FakeVar:
    """最小的 tk 变量替身（只需要 get/set）—— 免得为了一个字符串开 Tk root。"""

    def __init__(self, value=""):
        self._v = value

    def get(self):
        return self._v

    def set(self, value):
        self._v = value


class TestRunnableTarget(unittest.TestCase):
    """`is_runnable_target`：哪些目标算"用户真的能启动的程序"。"""

    RUNNABLE = [
        r"C:\WINDOWS\system32\cmd.exe",
        r"X:\Program Files\bilibili\哔哩哔哩.exe",
        r"C:\WINDOWS\system32\compmgmt.msc",       # 管理控制台文档，双击能开工具
        r"C:\Start Menu\某程序.lnk",
        r"X:\tools\x.bat",
        r"X:\tools\x.cmd",
        r"X:\tools\x.com",
        r"X:\app\run.py",
        r"X:\app\run.pyw",
        r"X:\app\a.jar",
        r"https://example.com".replace("https://example.com", r"X:\a\b.url"),
    ]

    NOT_RUNNABLE = [
        r"C:\Program Files (x86)\Dev-Cpp\COPYING.txt",     # 实测混进来的 License
        r"C:\Windows\7-Zip\7-zip.chm",                     # 实测 7-Zip Help
        r"C:\Python\Doc\html\index.html",                  # 实测 Python Manuals
        r"C:\a\b.pdf",
        r"C:\a\b.docx",
        r"C:\a\b.png",
        r"C:\a\b.zip",
        r"C:\a\b.msi",
        r"C:\a\b.log",
        r"C:\a\no_extension",
        "",
    ]

    def test_runnable(self):
        for p in self.RUNNABLE:
            with self.subTest(path=p):
                self.assertTrue(core.is_runnable_target(p), f"应算可启动：{p}")

    def test_not_runnable(self):
        for p in self.NOT_RUNNABLE:
            with self.subTest(path=p):
                self.assertFalse(core.is_runnable_target(p), f"不应算可启动：{p}")

    def test_extension_lists_do_not_overlap(self):
        """
        "可启动扩展名"与"明确不是程序的扩展名"两份清单不能有交集。

        为什么这份非程序清单**内联在测试里**（2026-10-16 复查补丁落地时改的）：
        核心层那个 `CATALOG_NON_PROGRAM_EXTS` 常量被复查判定为死代码删掉了
        （真正干活的过滤走 `CATALOG_TARGET_EXTS` 白名单 + 下面那一组用例），
        但"两份清单别冲突"这条不变式仍然值得钉住 —— 所以清单搬进测试，
        以后往白名单里加 `.pdf` 这种就会当场报红。
        """
        not_programs = (
            ".txt", ".md", ".rtf", ".chm", ".hlp", ".html", ".htm", ".mht",
            ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
            ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".ico", ".svg",
            ".zip", ".rar", ".7z", ".cab", ".msi", ".msp", ".log", ".ini",
        )
        overlap = set(core.CATALOG_TARGET_EXTS) & set(not_programs)
        self.assertEqual(overlap, set(), f"白名单里混进了非程序扩展名：{overlap}")

    def test_case_insensitive(self):
        self.assertTrue(core.is_runnable_target(r"C:\A\B.EXE"))
        self.assertFalse(core.is_runnable_target(r"C:\A\B.TXT"))


class TestFilterNonPrograms(unittest.TestCase):
    def _items(self):
        return [
            core.ProgramItem("Steam", r"X:\steam\steam.exe"),
            core.ProgramItem("License", r"C:\Dev-Cpp\COPYING.txt"),
            core.ProgramItem("7-Zip Help", r"C:\Windows\7-Zip\7-zip.chm"),
            core.ProgramItem("Computer Management", r"C:\WINDOWS\system32\compmgmt.msc"),
            core.ProgramItem("无路径项", ""),
        ]

    def test_drops_documents_keeps_programs(self):
        kept, dropped = core.filter_non_programs(self._items())
        names = [it.name for it in kept]
        self.assertEqual(names, ["Steam", "Computer Management", "无路径项"])
        self.assertEqual(len(dropped), 2)
        self.assertTrue(any("License" in d and ".txt" in d for d in dropped))
        self.assertTrue(any("7-Zip Help" in d and ".chm" in d for d in dropped))

    def test_keeps_items_without_target(self):
        """没有目标的条目（UWP 之类）不该被这条规则删掉。"""
        kept, dropped = core.filter_non_programs([core.ProgramItem("设置", "")])
        self.assertEqual(len(kept), 1)
        self.assertEqual(dropped, [])

    def test_idempotent(self):
        kept, _ = core.filter_non_programs(self._items())
        kept2, dropped2 = core.filter_non_programs(kept)
        self.assertEqual([it.name for it in kept2], [it.name for it in kept])
        self.assertEqual(dropped2, [])

    def test_empty(self):
        self.assertEqual(core.filter_non_programs([]), ([], []))


class TestRealCatalogIsClean(unittest.TestCase):
    """真实目录里不能再出现非程序条目（用临时缓存，不污染用户缓存）。"""

    def test_no_non_program_targets(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            cache = os.path.join(tmp, "catalog.json")
            items = core.build_program_catalog(use_cache=True, cache_path=cache)
        self.assertGreater(len(items), 10, "目录条目太少，测试前提不成立")
        bad = [(it.name, it.target) for it in items
               if it.target and not core.is_runnable_target(it.target)]
        self.assertEqual(bad, [], f"目录里仍有非程序条目：{bad[:5]}")

    def test_cache_round_trip_also_filters(self):
        """从缓存加载的路径同样要过滤（旧缓存可能含有非程序条目）。"""
        import json
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            cache = os.path.join(tmp, "catalog.json")
            items = core.build_program_catalog(use_cache=True, cache_path=cache)
            # 往缓存里塞一条非程序条目，再重新加载，它必须被过滤掉
            with open(cache, encoding="utf-8") as fh:
                raw = json.load(fh)
            raw["items"].append(core.ProgramItem(
                "假程序", r"C:\x\README.txt").to_dict())
            with open(cache, "w", encoding="utf-8") as fh:
                json.dump(raw, fh, ensure_ascii=False)
            again = core.build_program_catalog(use_cache=True, cache_path=cache)
        names = [it.name for it in again]
        self.assertNotIn("假程序", names, "缓存加载路径没有过滤非程序条目")
        self.assertLessEqual(len(again), len(items))
class TestUiaClient(unittest.TestCase):
    """
    UIA 客户端：只验证"能初始化、能安全降级、能读到真实卡片"。

    刻意**不**验证"能点开视频"：实测 InvokePattern 返回 S_OK 但页面不变，
    那是应用侧的行为，不是本项目能保证的。
    """

    def test_client_initializes_or_reports_error(self):
        client = core.UiaClient()
        self.assertIsInstance(client.available, bool)
        if not client.available:
            self.assertTrue(client.error, "不可用时应当有原因说明")

    def test_bogus_hwnd_returns_empty_without_raising(self):
        client = core.UiaClient()
        if not client.available:
            self.skipTest(f"本机 UIA 不可用：{client.error}")
        self.assertEqual(client.elements(0), [])
        self.assertEqual(client.names(0), [])

    def test_try_invoke_without_match_reports_reason(self):
        client = core.UiaClient()
        if not client.available:
            self.skipTest(f"本机 UIA 不可用：{client.error}")
        ok, msg = client.try_invoke(0xFFFF0000, "绝对不存在的标题XYZ")
        self.assertFalse(ok)
        self.assertTrue(msg)

    def test_try_invoke_prefers_the_card_container(self):
        """
        "优先匹配卡片容器"那条规则以前**永远不可达**（2026-10-13 修的真 bug）：

        原写法是
            if card_pattern.match(name) and fallback is None:  fallback = ...; break
            if fallback is None:                               fallback = ...
        第一个匹配到的元素就已经把 fallback 填上了，于是卡片那条分支的条件
        （`fallback is None`）从此恒为假，`break` 也永远不执行 ——
        结果总是取**最先出现**（往往是最短的标题文本）那个元素，与文档说的相反。

        用一个替身客户端喂两个元素（先短标题、后卡片容器）即可判红：
        正确的实现必须选**卡片**那个。
        """
        names = {
            "SHORT": "某视频标题",
            "CARD": "某视频标题 12.3万 456 10:20",
        }
        client = core.UiaClient.__new__(core.UiaClient)
        client.available = True
        client.error = ""
        client.elements = lambda _hwnd, limit=1500: ["SHORT", "CARD"]
        client._prop_str = lambda el, _pid: names.get(el, "")
        picked: dict = {}

        def fake_vt(obj, index, restype, *argtypes):
            def call(*_a):
                picked["el"] = obj
                return 1          # 让"不支持 Invoke 模式"这条分支返回，避免真去调 COM
            return call

        client._vt = fake_vt
        client.try_invoke(0, "某视频标题")
        self.assertEqual(picked.get("el"), "CARD",
                         "应当优先选卡片容器，而不是最先出现的短标题元素")
class TestMessageInputPath(unittest.TestCase):
    """
    消息通路（PostMessage）相关的辅助方法。

    背景（当时实测）：SendInput/keybd_event 注入在受限会话里到不了其他进程，
    而 PostMessage 的**键盘消息**能到达（实测能让 Chromium 滚动页面）。
    这里只验证这些方法"存在、安全、不崩"——它们实际是否被应用接受，
    取决于目标应用（哔哩哔哩的卡片激活就只认真实指针输入）。
    """

    def test_render_widget_found_for_real_window(self):
        wi = core.find_video_window("bilibili")
        if wi is None:
            self.skipTest("哔哩哔哩没有运行")
        sender = core.WindowKeySender(wi.hwnd)
        rw = sender.render_widget()
        self.assertTrue(rw, "render_widget 必须返回一个有效 hwnd")
        # 要么找到渲染子窗口，要么退回主窗口，两者都必须是窗口
        self.assertTrue(core.user32.IsWindow(core.w.HWND(rw)))

    def test_render_widget_falls_back_for_bogus_hwnd(self):
        sender = core.WindowKeySender(0xFFFF0000)
        self.assertEqual(sender.render_widget(), 0xFFFF0000,
                         "找不到渲染子窗口时应退回主窗口参数")

    def test_key_message_does_not_raise(self):
        try:
            core.WindowKeySender(0xFFFF0000).key_message(0x0D)
            core.WindowKeySender(0xFFFF0000).type_message("x")
        except Exception as exc:                             # pragma: no cover
            self.fail(f"对无效窗口发消息不应抛异常：{type(exc).__name__}: {exc}")
class TestMediaKeys(unittest.TestCase):
    """
    媒体键（"在 WinMon 里切歌"）。

    为什么用媒体键：Windows 把 `VK_MEDIA_*` 直接交给**当前活跃的媒体会话**，
    网易云 / QQ音乐 / Spotify / 浏览器里的网页播放器都会注册 —— 所以它
    "不挑 App"，也不需要知道界面长什么样（像素定位那套一改版就废）。

    这里**不真的发键**（发了会去切用户正在听的歌），只钉住规则：
    白名单、VK 值、如实报告的措辞。
    """

    def test_vk_codes_match_the_win32_values(self):
        """值必须是系统约定的那几个，写错一个就是"按了没反应"。"""
        self.assertEqual(core.MEDIA_KEY_VK["prev"], 0xB1)
        self.assertEqual(core.MEDIA_KEY_VK["play_pause"], 0xB3)
        self.assertEqual(core.MEDIA_KEY_VK["next"], 0xB0)
        self.assertEqual(set(core.MEDIA_KEY_VK),
                         {slug for slug, _l, _v in core.MEDIA_KEYS})

    def test_whitelist_rejects_anything_else(self):
        """
        只允许切歌这三个键：传别的 VK 一律拒绝。

        做成白名单而不是"传什么发什么"，是因为这个函数只该干"切歌"这一件事 ——
        给它任意 VK 的能力，就等于给了一个能往别的程序乱发按键的后门。
        """
        for bad in ("", None, "  ", "volume_up", "0x41", "A", 13, "playpause"):
            with self.subTest(slug=bad):
                ok, msg = core.send_media_key(bad)
                self.assertFalse(ok, f"{bad!r} 不该被接受")
                self.assertIn("不认识", msg)

    def test_labels_are_chinese_and_nonempty(self):
        for slug, label, _vk in core.MEDIA_KEYS:
            with self.subTest(slug=slug):
                self.assertTrue(label.strip())
                self.assertEqual(core.MEDIA_KEY_LABEL[slug], label)

    def test_success_message_says_sent_not_switched(self):
        """
        措辞必须如实：**只能确认"键发出去了"**，不能声称"歌已经切了"。

        系统不回执"目标应用是否真的响应"，所以照着"已切换"写就是假报成功
        —— 这正是铁律 2 要避免的。这里用替身拦下真实的 keybd_event。

        2026-10-12 起 `send_media_key()` 只是**兜底**（首选是能回读核对的
        `smart_media_command()`），所以文案不再承诺"作用于当前活跃的播放器"，
        只如实说"已发送"。
        """
        calls = []
        with mock.patch.object(core.user32, "keybd_event",
                               side_effect=lambda *a, **k: calls.append(a)):
            ok, msg = core.send_media_key("next")
        self.assertTrue(ok, msg)
        self.assertIn("已发送", msg)
        self.assertNotIn("已切换", msg)
        self.assertIn("下一首", msg)
        self.assertEqual(len(calls), 2, "应当按下+抬起各一次")
        self.assertEqual(calls[0][0], 0xB0)
        self.assertEqual(calls[1][2], core.KEYEVENTF_KEYUP, "第二次必须是抬起")

    def test_invalid_slug_never_touches_the_keyboard(self):
        calls = []
        with mock.patch.object(core.user32, "keybd_event",
                               side_effect=lambda *a, **k: calls.append(a)):
            core.send_media_key("definitely-not-a-media-key")
        self.assertEqual(calls, [], "被拒绝的键不该真的发出去")


class TestMediaSessionLogic(unittest.TestCase):
    """
    媒体会话（"正在放什么"）的**纯逻辑**部分：挑哪一个会话、怎么显示进度。

    真实的读取要起 PowerShell（`media_session.ps1`，实测约 290ms），
    在 `TestMediaSessionLive` 里单独测；这里保证"给一串会话 → 显示什么"是确定的。
    """

    def _s(self, app="MSEdge", title="歌名", artist="", status=4,
           pos=61000, dur=373000):
        return {"app": app, "title": title, "artist": artist, "album": "",
                "status": status, "position": pos, "duration": dur}

    def test_no_sessions(self):
        info = core.media_session_summary([])
        self.assertFalse(info["found"])
        self.assertIn("没有", info["text"])
        self.assertEqual(info["count"], 0)

    def test_prefers_the_specified_app(self):
        """指定了应用就挑它 —— 这是"按应用控制"的前提。"""
        rows = [self._s(app="MSEdge", title="网页上的", status=4),
                self._s(app="cloudmusic.exe", title="网易云的", status=5)]
        info = core.media_session_summary(rows, "cloudmusic.exe")
        self.assertEqual(info["picked"]["title"], "网易云的")
        self.assertNotIn("不是指定的应用", info["text"])

    def test_falls_back_to_playing_then_first(self):
        rows = [self._s(app="A", title="暂停的", status=5),
                self._s(app="B", title="在放的", status=4)]
        self.assertEqual(core.media_session_summary(rows)["picked"]["title"], "在放的")
        paused = [self._s(app="A", title="暂停的", status=5)]
        self.assertEqual(core.media_session_summary(paused)["picked"]["title"], "暂停的")

    def test_warns_when_the_pick_is_not_the_requested_app(self):
        """
        指定了应用、但**它没有会话**时必须**说出来**。

        否则用户看着 WinMon 显示的歌、以为控的是自己选的播放器 —— 那正是
        "静默地做错事"，铁律 2 不允许。
        """
        rows = [self._s(app="MSEdge", title="网页上的")]
        info = core.media_session_summary(rows, "cloudmusic.exe")
        self.assertIn("不是指定的应用", info["text"])
        self.assertIn("MSEdge", info["text"])

    def test_text_contains_title_artist_and_status(self):
        info = core.media_session_summary([self._s(title="七里香", artist="周杰伦",
                                                   status=5)])
        self.assertIn("七里香", info["text"])
        self.assertIn("周杰伦", info["text"])
        self.assertIn("已暂停", info["text"])

    def test_missing_title_is_explained(self):
        info = core.media_session_summary([self._s(title="", status=4)])
        self.assertIn("没有提供曲名", info["text"])
    # ---- 媒体自动唤醒（用户点名要测的那一项）----
    #
    # 2026-10-16 复查补丁删掉了 `media_launch()`：查过界面接线，**没有任何按钮或
    # 快捷键调它**（发命令走的是 `media_command`），唤醒那三步现在长在 `media_command`
    # 里。所以这一组改成驱动 `media_command` —— 断言还是原来那些"决策"
    # （该不该替你启动它），只是发命令那一步挪到了后台线程（P1-1），
    # 断言前要把那条链路跑完（见 `pump_media`）。

    def _wake_app(self, auto_wake: bool, already_running: bool,
                  launch_ok: bool = True, picked: bool = True):
        """
        搭一个"手工 app"，只测媒体命令里的**唤醒分支**，不碰真进程。

        `snapshot_processes` / `launch_media_app` / `media_command` 都被替身接管，
        所以断言的是**决策**："这一步该不该去启动它"、命令有没有发出去。
        """
        import tkinter as tk
        root = tk.Tk()
        root.withdraw()
        self.addCleanup(root.destroy)
        app = winmon.WinMonApp.__new__(winmon.WinMonApp)
        app.root = root
        app._init_video_vars()
        app._ui_msg = queue.Queue()          # `_ui_call` 的队列（真 __init__ 会建）
        app._media_busy = False
        app.media_auto_wake.set(auto_wake)
        app.status_text = tk.StringVar()
        app._set_status = lambda t: app.status_text.set(t)
        app.log_action = lambda *a, **k: None
        app._refresh_media = lambda: None
        app.media_text = tk.StringVar()
        if picked:
            fake = core.MediaApp("测试播放器", "testplayer.exe", False)
            app._picked_media_app = lambda: fake
            app._media_apps = {fake.title: fake}
            app.media_app_pin.set(fake.title)
        procs = ({1: {"name": "testplayer.exe"}} if already_running else {})
        launched = []
        sent = []

        def fake_launch(a):
            launched.append(a)
            return (launch_ok, f"已唤醒 {a.label}" if launch_ok else "启动失败：没有这个程序")

        # 注意：这几条 patch 必须**罩住整条异步链路**（后台线程也要读到替身），
        # 所以 pump 放在 with 里面 —— 出了 with 再 pump 就是在调真 PowerShell 了。
        with mock.patch.object(core, "snapshot_processes", return_value=procs), \
                mock.patch.object(core, "launch_media_app", side_effect=fake_launch), \
                mock.patch.object(
                    core, "media_command",
                    side_effect=lambda slug, key="": (
                        sent.append((slug, key)), (True, "已发送"))[1]):
            app.media_command("play_pause")
            pump_media(app, root, lambda: bool(app.status_text.get()))
        return app, launched, sent

    def test_already_running_is_not_launched_again(self):
        app, launched, sent = self._wake_app(auto_wake=True, already_running=True)
        self.assertEqual(launched, [], "已经在运行的程序不该被再启动一次")
        self.assertEqual([slug for slug, _k in sent], ["play_pause"],
                         "已经在运行 → 直接发命令（这才是有用的那一步）")

    def test_auto_wake_off_really_does_not_launch(self):
        """
        **修掉的 bug**：「启动/唤醒」以前完全不看「自动唤醒」那个勾，
        永远是"能拉就拉" —— 设置里那个勾等于摆设（用户实测发现）。
        """
        app, launched, sent = self._wake_app(auto_wake=False, already_running=False)
        self.assertEqual(launched, [], "没勾「自动唤醒」就不该替你启动程序")
        self.assertEqual(sent, [], "程序没在跑又没勾唤醒 → 连命令都不该发（发了没人收）")
        self.assertIn("没在运行", app.status_text.get())
        self.assertIn("自动唤醒", app.status_text.get())

    def test_auto_wake_on_launches_it(self):
        app, launched, _sent = self._wake_app(auto_wake=True, already_running=False)
        self.assertEqual(len(launched), 1, "勾了「自动唤醒」就该真的去启动")
        self.assertIn("已唤醒", app.status_text.get())

    def test_failed_launch_is_reported_not_pretended(self):
        app, launched, _sent = self._wake_app(auto_wake=True, already_running=False,
                                              launch_ok=False)
        self.assertEqual(len(launched), 1)
        self.assertIn("失败", app.status_text.get())

    def test_no_picked_app_sends_a_global_command_without_launching(self):
        """
        没选应用时：一个程序都不启动，命令**照发**（走"没有指定应用"的全局那条路）。

        这一条替换了原来的 `test_launch_without_a_picked_app_says_so`：
        以前那句"请先选一个媒体应用"长在 `media_launch()` 里，而现在已经没有
        "单独唤醒"这个入口了 —— 从「播放/暂停」按钮进来时，"没选应用"本来就该
        当作"操作当前在放东西的那个程序"，不该拦着不发。
        """
        app, launched, sent = self._wake_app(auto_wake=True, already_running=False,
                                             picked=False)
        self.assertEqual(launched, [], "没选应用就不知道该唤醒谁，不能乱启动")
        self.assertEqual(sent, [("play_pause", "")],
                         "没选应用时应当发一条不带应用标识的全局命令")

    def test_per_app_command_falls_back_to_media_keys(self):
        """
        选中的应用**没有媒体会话**时（网易云就是这种：进程在跑但不发布会话），
        按应用发命令发不出去 —— 这时应当**自动改用全局媒体键**，
        而不是只报一句失败（用户真正想要的还是"继续放歌"）。
        """
        import tkinter as tk
        root = tk.Tk()
        root.withdraw()
        self.addCleanup(root.destroy)
        app = winmon.WinMonApp.__new__(winmon.WinMonApp)
        app.root = root
        app._init_video_vars()
        app._media_busy = False
        app._media_found = True
        app._media_playing = False
        app._media_last_pick = {"app": "MSEdge"}        # 当前会话属于 Edge
        app.status_text = tk.StringVar()
        app._set_status = lambda t: app.status_text.set(t)
        app.log_action = lambda *a, **k: None
        app.media_text = tk.StringVar()
        app._refresh_media = lambda: None
        fake = core.MediaApp("网易云音乐", "cloudmusic.exe", False)
        app._picked_media_app = lambda: fake
        # A.2 起 `_app_key_for_media()` 走的是"标签 → exe"（支持多选），所以这里也要喂它
        app._media_apps = {fake.title: fake}
        app.media_app_pin.set(fake.title)
        app.media_auto_wake.set(False)                  # 别真去启动它
        app._ui_msg = queue.Queue()                     # `_ui_call` 的队列
        app._media_busy = False
        sent = []
        with mock.patch.object(core, "snapshot_processes",
                               return_value={1: {"name": "cloudmusic.exe"}}), \
                mock.patch.object(core, "media_command",
                                  return_value=(False, "no matching session")), \
                mock.patch.object(core, "send_media_key",
                                  side_effect=lambda k: (sent.append(k), (True, "已发送"))[1]):
            app.media_command("play")
            # P1-1 起这条链路是异步的（after → 后台线程 → `_ui_call` 队列），
            # 断言必须等它走完，而且要在 patch 还罩着的时候等。
            pump_media(app, root, lambda: bool(app.status_text.get()))
        self.assertEqual(sent, ["play_pause"], "没有会话时应当退回全局媒体键")
        self.assertIn("媒体键", app.status_text.get())

    # ---- 用户第 2 条："明确哪个程序它是啥、运行了啥" ----

    def test_workspace_log_lists_the_actual_programs(self):
        """
        恢复工作区的记录必须写出**具体程序 + 窗口标题**，
        不能只写一句"已还原 3 个"（用户 2026-10-12 的原话）。
        """
        rows = [{"name": "chrome.exe", "title": "GitHub"},
                {"name": "Code.exe", "title": "winmon.py"},
                {"name": "notepad.exe", "title": ""}]
        text = winmon.WinMonApp._describe_windows(rows)
        self.assertIn("chrome.exe｜GitHub", text)
        self.assertIn("Code.exe｜winmon.py", text)
        self.assertIn("notepad.exe", text)

    def test_workspace_log_truncates_long_lists(self):
        rows = [{"name": f"p{i}.exe", "title": f"t{i}"} for i in range(9)]
        text = winmon.WinMonApp._describe_windows(rows, limit=5)
        self.assertIn("等 4 个", text)
        self.assertNotIn("p8.exe", text)

    def test_workspace_log_handles_empty_and_broken_rows(self):
        self.assertEqual(winmon.WinMonApp._describe_windows([]), "")
        # 脏数据不能让它抛异常（设置文件是用户能改的）
        self.assertEqual(winmon.WinMonApp._describe_windows([None, "x", {}]), "")

    # ---- 用户第 3 条：媒体那一行要写清"谁在放什么"，没在放时也别含糊 ----

    def _media_app_for_display(self):
        import tkinter as tk
        root = tk.Tk()
        root.withdraw()
        self.addCleanup(root.destroy)
        app = winmon.WinMonApp.__new__(winmon.WinMonApp)
        app.root = root
        app._init_video_vars()
        app.media_text = tk.StringVar()
        app._media_busy = False
        app.status_text = tk.StringVar()
        app._set_status = lambda t: app.status_text.set(t)
        return app

    def test_media_line_names_the_playing_program(self):
        app = self._media_app_for_display()
        app._app_key_for_media = lambda: ""
        app._picked_media_app = lambda: None
        app._apply_media_result({"ok": True, "sessions": [
            {"app": "MSEdge", "title": "某首歌", "artist": "某人", "status": 4}]})
        text = app.media_text.get()
        self.assertIn("Microsoft Edge", text, f"应当写清是哪个程序在放：{text!r}")
        self.assertIn("某首歌", text)

    # ---- 网易云这类客户端不发布媒体会话 → 标题兜底（2026-10-12 实测后加的）----

    def test_browser_titles_are_never_used_as_track_names(self):
        """
        浏览器标题里是"网易云音乐 和另外 2 个页面 - 个人 - Microsoft Edge"，
        拿它当歌名就是编造 —— 必须拒绝。
        """
        title, why = core.title_track_hint("msedge.exe")
        self.assertEqual(title, "")
        self.assertIn("浏览器", why)

    def test_title_hint_needs_an_app(self):
        self.assertEqual(core.title_track_hint("")[0], "")
        self.assertIn("没有指定", core.title_track_hint("")[1])

    def test_title_hint_reports_when_window_is_missing(self):
        title, why = core.title_track_hint("绝对不存在的播放器.exe")
        self.assertEqual(title, "")
        self.assertTrue(why)

    def test_media_line_falls_back_to_the_window_title(self):
        """
        会话里没有它、但它的窗口标题就是歌名时，那一行要显示歌名并**标注来源**
        （网易云音乐客户端实测就是这样：在放歌，却不发布会话）。
        """
        app = self._media_app_for_display()
        app.media_app_pin.set(core.MediaApp("网易云音乐", "cloudmusic.exe", False).title)
        app._media_apps = {app.media_app_pin.get():
                           core.MediaApp("网易云音乐", "cloudmusic.exe", False)}
        app._app_key_for_media = lambda: "cloudmusic.exe"
        app._picked_media_app = lambda: core.MediaApp("网易云音乐", "cloudmusic.exe", False)
        with mock.patch.object(core, "title_track_hint",
                              return_value=("某首歌 - 某歌手", "窗口标题")):
            app._apply_media_result({"ok": True, "sessions": []})
        text = app.media_text.get()
        self.assertIn("网易云音乐", text)
        self.assertIn("某首歌 - 某歌手", text)
        self.assertIn("窗口标题", text, "从标题读来的必须标注来源，不能冒充系统会话")

    def test_media_line_says_nothing_is_playing_when_title_is_empty(self):
        app = self._media_app_for_display()
        app._picked_media_app = lambda: core.MediaApp("网易云音乐", "cloudmusic.exe", False)
        app._app_key_for_media = lambda: "cloudmusic.exe"
        with mock.patch.object(core, "title_track_hint", return_value=("", "没找到窗口")):
            app._apply_media_result({"ok": True, "sessions": []})
        self.assertIn("没有在播放", app.media_text.get())

    def test_media_line_is_clear_when_nothing_plays(self):
        app = self._media_app_for_display()
        app._app_key_for_media = lambda: ""
        app._picked_media_app = lambda: None
        # 把"标题兜底"按住，专门测"真的一无所有"（否则这台机器上用户正开着网易云，
        # 兜底会读到歌名，这条用例就不成立了）
        with mock.patch.object(core, "title_track_hint", return_value=("", "测试假装的")):
            app._apply_media_result({"ok": True, "sessions": []})
        text = app.media_text.get()
        self.assertIn("没有", text)
        # 原来是"现在没有程序在放东西"——用户说看不懂，不该再出现
        self.assertNotIn("没有程序在放东西", text)

    def test_media_line_names_which_app_is_silent(self):
        app = self._media_app_for_display()
        app._picked_media_app = lambda: core.MediaApp("网易云音乐", "cloudmusic.exe", False)
        app._app_key_for_media = lambda: "cloudmusic.exe"
        app._apply_media_result({"ok": True, "sessions": []})
        self.assertIn("网易云音乐", app.media_text.get())

    def test_failed_media_command_is_not_logged(self):
        """
        用户第 3 条：什么都**没发生**的时候不要写操作历史
        （否则日志会被"其实什么也没做"的记录塞满）。
        """
        app = self._media_app_for_display()
        logged = []
        app.log_action = lambda *a, **k: logged.append(a)
        app._media_found = True
        app._media_last_pick = {"app": "MSEdge"}
        app._media_playing = False
        app._app_key_for_media = lambda: ""
        app._picked_media_app = lambda: None
        app._refresh_media = lambda: None
        app._ui_msg = queue.Queue()              # `_ui_call` 的队列
        app._media_busy = False
        with mock.patch.object(core, "media_command",
                              return_value=(False, "没人在放东西")):
            app.media_command("play")
            pump_media(app, app.root, lambda: bool(app.status_text.get()))
        self.assertEqual(logged, [], f"什么都没做却写了日志：{logged}")
        self.assertIn("没人在放东西", app.status_text.get())

    def test_app_has_session_matches_family_ids(self):
        """会话里的 app 常是**家族名**（Edge 报 MSEdge），匹配要认这个。"""
        import tkinter as tk
        root = tk.Tk()
        root.withdraw()
        self.addCleanup(root.destroy)
        app = winmon.WinMonApp.__new__(winmon.WinMonApp)
        app.root = root
        app._media_found = True
        app._media_last_pick = {"app": "MSEdge"}
        self.assertTrue(app._app_has_session("msedge.exe"))
        self.assertFalse(app._app_has_session("cloudmusic.exe"))

    def test_candidates_are_a_name_map_not_a_gate(self):
        """
        候选表的职责变了（2026-10-16）：**只做显示名映射 + 唤醒补充**，
        不再是"能不能被识别"的门槛 —— 覆盖范围改由系统媒体会话决定
        （任何 app id 都能变成一条可选条目，完整矩阵见 `test_b12_media_discovery`）。

        所以这里只钉两件事：
          · 常见的播放器 / 浏览器仍然在表里（明确要"能指定 Edge 这类浏览器"）；
          · **表里没有**的应用照样能被认出来 —— 这才是"基于所有可识别的媒体"。
        """
        exes = {exe.lower() for _l, exe, _b in core.MEDIA_APP_CANDIDATES}
        self.assertIn("cloudmusic.exe", exes)
        self.assertIn("qqmusic.exe", exes)
        self.assertIn("msedge.exe", exes)
        browsers = [exe for _l, exe, b in core.MEDIA_APP_CANDIDATES if b]
        self.assertTrue(browsers, "浏览器要能在下拉框里选到（网页端切歌就靠它）")
        # 表里没有的应用：会话里一出现就得认出来（这条按老实现必红）
        unknown = "SomePlayer_1a2b3c!App"
        self.assertNotIn(unknown.lower(), exes)
        app = core.media_app_from_session(unknown, set())
        self.assertIsNotNone(app)
        self.assertEqual(app.label, unknown)

    def test_media_app_key_is_the_exe_name(self):
        app = core.MediaApp("网易云音乐", "cloudmusic.exe")
        self.assertEqual(app.key, "cloudmusic.exe")
        self.assertEqual(app.exe, "cloudmusic.exe")
        self.assertIn("播放器", app.title)
        browser = core.MediaApp("Microsoft Edge", "msedge.exe", True)
        self.assertIn("浏览器", browser.title)
        self.assertTrue(browser.browser)

    def test_launch_reports_missing_app_instead_of_pretending(self):
        """
        找不到可执行文件时**如实说**，不能假装"已唤醒"。

        这条对应铁律 2：有副作用的动作要么真做成、要么如实报失败。
        """
        app = core.MediaApp("根本不存在的播放器", "winmon-no-such-player-xyz.exe")
        ok, msg = core.launch_media_app(app)
        self.assertFalse(ok)
        self.assertIn("没找到", msg)
        self.assertIn("手动打开", msg, "要给出下一步，而不是只说失败")

    def test_media_command_rejects_unknown_slug(self):
        for bad in ("", None, "nuke", "playnow", "play_pause"):
            with self.subTest(slug=bad):
                ok, msg = core.media_command(bad)
                self.assertFalse(ok, f"{bad!r} 不该被接受")
                self.assertIn("不认识", msg)

    def test_media_command_accepts_the_working_slugs(self):
        """
        四个合法命令要认（顺序无关大小写）—— 不能因为白名单写太严把 "PLAY" 挡掉。
        这里**不真的发命令**（`collect_media_sessions` 会去起 PowerShell，替身掉）。
        """
        for slug in ("play", "PAUSE", "Next", "previous"):
            with self.subTest(slug=slug):
                with mock.patch.object(core, "collect_media_sessions",
                                       return_value={"ok": True, "sessions": []}):
                    ok, msg = core.media_command(slug)
                self.assertFalse("不认识的媒体命令" in msg, msg)

    # ---- 当时实测的真 bug：PowerShell 管道输出是 ANSI 代码页，不是 UTF-8 ----

    def test_helper_output_decodes_gbk_titles(self):
        """
        中文 Windows 上 PowerShell 5.1 往管道写的是 **GBK**（系统 ANSI 代码页），
        以前 WinMon 硬按 UTF-8 解 → 网页端的中文曲名全是 `'������'`。
        实测原始字节就是 `d3 d0 ce d2 c4 d8`（=《有我呢》）。
        """
        payload = '{"ok":true,"sessions":[{"title":"有我呢","artist":"郭一凡"}]}'
        try:
            raw = payload.encode("gbk")
        except UnicodeEncodeError:                               # pragma: no cover
            self.skipTest("本机 ANSI 代码页装不下这些字")
        text = core._decode_helper_output(raw)
        self.assertIn("有我呢", text)
        self.assertIn("郭一凡", text)
        self.assertNotIn("�", text)

    def test_helper_output_prefers_utf8_when_it_is_valid_utf8(self):
        payload = '{"ok":true,"sessions":[{"title":"有我呢"}]}'
        self.assertIn("有我呢", core._decode_helper_output(payload.encode("utf-8")))

    def test_helper_output_survives_garbage(self):
        """完全不是 JSON 的字节也不能抛异常（宁可返回个问号）。"""
        self.assertIsInstance(core._decode_helper_output(b"\xff\xfe\x00garbage"), str)
        self.assertEqual(core._decode_helper_output(b""), "")

    def test_collect_media_sessions_reads_chinese_from_gbk_bytes(self):
        """
        端到端一小步：把桥接脚本的字节流换成 GBK 的中文，读回来必须是对的。

        注意要把**快照路径指到一个不存在的地方**：助手常驻时
        `collect_media_sessions()` 会先读快照（那会绕开本用例要测的解码路径），
        这台机器上真有会话时就会读到真数据而不是这里准备的字节。
        """
        payload = ('{"ok":true,"error":"","sessions":[{"app":"MSEdge",'
                   '"title":"有我呢","artist":"郭一凡","album":"","status":4}]}')
        old = os.environ.get(core.MEDIA_SNAPSHOT_ENV)
        os.environ[core.MEDIA_SNAPSHOT_ENV] = os.path.join(
            tempfile.gettempdir(), "winmon-没有这个快照.jsonl")
        self.addCleanup(lambda: os.environ.pop(core.MEDIA_SNAPSHOT_ENV, None)
                        if old is None
                        else os.environ.update({core.MEDIA_SNAPSHOT_ENV: old}))
        with mock.patch.object(core, "_run_media_helper",
                               return_value=(payload.encode("gbk"), b"", 0)):
            got = core.collect_media_sessions()
        self.assertTrue(got["ok"])
        self.assertEqual(got["sessions"][0]["title"], "有我呢")
        self.assertEqual(got["sessions"][0]["artist"], "郭一凡")

    def test_media_command_survives_gbk_output(self):
        payload = '{"ok":true,"command_ok":true,"command_msg":"好的"}'
        with mock.patch.object(core, "_run_media_helper",
                               return_value=(payload.encode("gbk"), b"", 0)):
            ok, msg = core.media_command("play")
        self.assertTrue(ok, msg)

    # ---- 2026-10-12 实测：键盘媒体键"没人理"，改成会话命令为主 + 回读核对 ----

    def test_play_pause_is_a_toggle_not_a_fixed_play(self):
        """
        「播放/暂停」是**切换**：正在放 → 应发 pause；已暂停 → 应发 play。
        写成固定的 "play" 会变成空操作（当时实测踩到，核对直接判成"没生效"）。
        """
        for status, expect in ((4, "pause"), (5, "play"), (2, "play")):
            sent = []

            def fake_cmd(cmd, app_id="", timeout=8.0, _s=sent):
                _s.append(cmd)
                return True, "应用接受了命令"

            # 第一次读返回"当前状态"，之后的读返回"变过之后的状态"
            seq = [{"app": "MSEdge", "title": "t", "status": status},
                   {"app": "MSEdge", "title": "t",
                    "status": 5 if status == 4 else 4}]
            with mock.patch.object(core, "media_command", side_effect=fake_cmd), \
                    mock.patch.object(core, "media_session_app_ids",
                                      return_value=["MSEdge"]), \
                    mock.patch.object(core, "media_session_snapshot",
                                      side_effect=lambda: seq.pop(0)), \
                    mock.patch.object(core.time, "sleep", lambda _s: None):
                ok, msg = core.smart_media_command("play_pause", verify_seconds=0.1)
            self.assertEqual(sent, [expect], f"状态 {status} 时该发 {expect}")
            self.assertTrue(ok, msg)

    def test_it_verifies_and_reports_honestly_when_nothing_changes(self):
        """
        命令被接受、但状态**一点没动** → 不能报成功；要退回按键并说明
        "这一路无法核对"，还得点出常见原因（比如播放列表里没有下一首）。
        """
        same = {"app": "MSEdge", "title": "同一首", "status": 4}
        with mock.patch.object(core, "media_command",
                              return_value=(True, "应用接受了命令")), \
                mock.patch.object(core, "media_session_app_ids",
                                  return_value=["MSEdge"]), \
                mock.patch.object(core, "media_session_snapshot",
                                  side_effect=lambda: dict(same)), \
                mock.patch.object(core, "send_media_key",
                                  return_value=(True, "已发送媒体键")), \
                mock.patch.object(core.time, "sleep", lambda _s: None):
            ok, msg = core.smart_media_command("next", verify_seconds=0.1)
        self.assertFalse(ok)
        self.assertIn("没变化", msg)
        self.assertIn("没有下一首", msg)
        self.assertIn("无法核对", msg)

    def test_next_tries_the_other_players_when_the_first_cannot_skip(self):
        """
        A.3：当前会话"没有下一首"时，**换下一个有会话的播放器试**，
        而不是直接报一句"没变化"（用户点名要的改进）。
        """
        seen = []
        same = {"app": "MSEdge", "title": "第一首", "status": 4}
        changed = {"app": "cloudmusic", "title": "换了一首", "status": 4}
        # 读快照的行为**跟轮询次数无关**：还没发过任何命令时是"没变"，
        # 一旦试过 cloudmusic 就变 —— 这样用例不会因为"核对循环跑几轮"而脆。
        state = {"reads": 0, "tried": set()}

        def snap():
            state["reads"] += 1
            if "cloudmusic" in state["tried"]:
                return dict(changed)
            if "MSEdge" in state["tried"] and state["reads"] > 2:
                return dict(same)          # 第一个会话：怎么读都不变
            return dict(same)

        def fake_cmd(cmd, app_id="", timeout=8.0):
            seen.append(app_id)
            state["tried"].add(app_id)
            return True, "应用接受了命令"

        with mock.patch.object(core, "media_command", side_effect=fake_cmd), \
                mock.patch.object(core, "media_session_app_ids",
                                  return_value=["MSEdge", "cloudmusic"]), \
                mock.patch.object(core, "media_session_snapshot", side_effect=snap), \
                mock.patch.object(core, "send_media_key") as keys, \
                mock.patch.object(core.time, "sleep", lambda _s: None):
            ok, msg = core.smart_media_command("next", verify_seconds=1.0)
        self.assertTrue(ok, msg)
        self.assertEqual(seen, ["MSEdge", "cloudmusic"], "应当换下一个会话再试")
        self.assertIn("cloudmusic", msg)
        self.assertIn("试了 2 个会话", msg)
        keys.assert_not_called()

    def test_no_session_at_all_is_reported_not_pretended(self):
        """一个会话都没有时，按键也几乎肯定没人理 —— 要如实说，不能报成功。"""
        with mock.patch.object(core, "media_session_snapshot",
                              return_value={"app": "", "title": "", "status": -1}), \
                mock.patch.object(core, "send_media_key",
                                  return_value=(True, "已发送媒体键")):
            ok, msg = core.smart_media_command("play_pause")
        self.assertFalse(ok)
        self.assertIn("没有任何程序发布媒体会话", msg)

    def test_unknown_key_is_rejected(self):
        ok, msg = core.smart_media_command("随便什么")
        self.assertFalse(ok)
        self.assertIn("不认识", msg)

    def test_keyboard_injection_is_still_available_as_fallback(self):
        """`send_media_key()` 作为兜底保留，且仍然如实说"已发送"而不是"已切换"。"""
        with mock.patch.object(core.user32, "keybd_event", return_value=None):
            ok, msg = core.send_media_key("play_pause")
        self.assertTrue(ok)
        self.assertIn("已发送", msg)
        self.assertNotIn("已切换", msg)

    def test_status_names_cover_the_winrt_enum(self):
        """WinRT 的 PlaybackStatus 是 0..5，别漏条目（漏了会显示"状态 4"）。"""
        self.assertEqual(core.MEDIA_STATUS_NAMES[4], "正在播放")
        self.assertEqual(core.MEDIA_STATUS_NAMES[5], "已暂停")
        self.assertEqual(set(core.MEDIA_STATUS_NAMES), {0, 1, 2, 3, 4, 5})

    def test_helper_script_exists_and_is_ascii_safe(self):
        """
        桥接脚本必须存在，且**不能含非 ASCII**（除非带 BOM）。

        为什么钉这条：Windows PowerShell 5.1 会把没有 BOM 的 .ps1 当 ANSI 读，
        文件里只要有一句中文注释，解析器就可能整个崩掉（本次真踩过：
        "The string is missing the terminator"）。
        """
        path = core.media_session_helper_path()
        self.assertTrue(os.path.exists(path), f"缺少 {path}")
        raw = open(path, "rb").read()
        if raw[:3] == b"\xef\xbb\xbf":
            return                      # 有 BOM 就允许非 ASCII
        try:
            raw.decode("ascii")
        except UnicodeDecodeError as exc:
            self.fail(f"{os.path.basename(path)} 没有 BOM 却含非 ASCII 字节：{exc}")


class TestAutostart(unittest.TestCase):
    """
    开机自启动：写 `HKCU\\...\\Run`，**写完回读核对**。

    铁律 2 管着这条：这是"有副作用的动作"，必须真的生效再报成功；
    测试**先备份、后恢复**注册表里原来的值（不能污染用户设置）。
    """

    def setUp(self):
        self._backup = core.get_autostart()

    def tearDown(self):
        if self._backup:
            import winreg
            with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, core.AUTOSTART_KEY,
                                    0, winreg.KEY_SET_VALUE) as k:
                winreg.SetValueEx(k, core.AUTOSTART_NAME, 0, winreg.REG_SZ,
                                  self._backup)
        else:
            core.set_autostart(False)

    def test_command_targets_pythonw_and_the_script(self):
        """
        命令行必须用 **pythonw.exe**（否则开机时会弹一个黑控制台窗口），
        并且带着 winmon.py 的完整路径、路径都加引号。
        """
        cmd = core.autostart_command()
        self.assertIn("winmon.py", cmd)
        self.assertIn('"', cmd, "路径要加引号，含空格时才不会被拆开")
        if not getattr(sys, "frozen", False):
            self.assertIn("python", cmd.lower())

    def test_on_off_roundtrip_is_verified(self):
        ok, msg = core.set_autostart(True)
        self.assertTrue(ok, msg)
        self.assertEqual(core.get_autostart(), core.autostart_command(),
                         "打开后回读到的应当是刚写进去的那条命令")
        ok2, msg2 = core.set_autostart(False)
        self.assertTrue(ok2, msg2)
        self.assertEqual(core.get_autostart(), "", "关闭后注册表里不该还留着")

    def test_turning_off_when_already_off_is_fine(self):
        core.set_autostart(False)
        ok, msg = core.set_autostart(False)
        self.assertTrue(ok, msg)

    def test_registry_location_is_per_user(self):
        """必须写 HKCU（不需要管理员），不能偷偷写 HKLM。"""
        self.assertTrue(core.AUTOSTART_KEY.startswith("Software\\Microsoft\\Windows"))
        self.assertIn("Run", core.AUTOSTART_KEY)
        self.assertEqual(core.AUTOSTART_NAME, "WinMon")


class TestTrayIcon(unittest.TestCase):
    """
    托盘：`Shell_NotifyIcon` + **子类化 Tk 主窗口的窗口过程**。

    为什么钉这一组：托盘只有真加一次才知道行不行；而"点 × 隐藏到托盘"是用户
    点名要的功能 —— 加不上图标就等于把窗口弄丢了（所以失败必须放回窗口）。
    """

    def setUp(self):
        try:
            self.root = tk.Tk()
            self.root.withdraw()
        except Exception as exc:                              # pragma: no cover
            self.skipTest(f"没有图形会话：{exc}")
        self.addCleanup(self.root.destroy)
        self.hwnd = int(core.user32.GetAncestor(
            core.w.HWND(self.root.winfo_id()), core.GA_ROOT) or self.root.winfo_id())

    def test_install_and_uninstall_subclass(self):
        host = core.TrayMessageWindow(self.hwnd)
        ok, msg = host.install()
        self.assertTrue(ok, msg)
        self.assertTrue(host.installed)
        ok2, _msg2 = host.install()          # 重复接管不该报错
        self.assertTrue(ok2)
        host.uninstall()
        self.assertFalse(host.installed)

    def test_add_and_remove_icon(self):
        tray = core.TrayIcon(self.hwnd, os.path.join(core.APP_DIR, "winmon.ico"),
                             "WinMon")
        ok, msg = tray.add()
        self.assertTrue(ok, msg)
        self.assertTrue(tray.ok)
        tray.remove()
        self.assertFalse(tray.ok)

    def test_commands_are_recorded_and_drained(self):
        """
        托盘菜单选择必须能被取走 —— **只取一次**（第二次为空）。

        为什么不走 PostMessage+Peek：实测那条自定义消息会被 Tk 的消息泵吃掉，
        所以窗口过程直接记进列表；这条钉住"取走即清空"的语义。
        """
        host = core.TrayMessageWindow(self.hwnd)
        host.install()
        self.addCleanup(host.uninstall)
        host.commands.append(core.IDM_TRAY_SHOW)
        self.assertEqual(host.pump(), [core.IDM_TRAY_SHOW])
        self.assertEqual(host.pump(), [], "取过一次之后要清空")

    def test_window_procedure_survives_foreign_messages(self):
        """收到不认识的消息时不能崩（而且必须转给原来的窗口过程）。"""
        host = core.TrayMessageWindow(self.hwnd)
        host.install()
        self.addCleanup(host.uninstall)
        try:
            host._proc(self.hwnd, 0x0113, 0, 0)              # WM_TIMER
            host._proc(self.hwnd, core.WM_TRAY, 0, 0x7FFF)   # 未知鼠标消息
        except Exception as exc:
            self.fail(f"窗口过程不该抛异常：{type(exc).__name__}: {exc}")

    def test_tray_menu_ids_are_distinct(self):
        self.assertNotEqual(core.IDM_TRAY_SHOW, core.IDM_TRAY_EXIT)
        self.assertTrue(core.IDM_TRAY_SHOW and core.IDM_TRAY_EXIT)

    def test_setup_tray_makes_the_icon_ready_before_any_close(self):
        """
        **启动就绪**：不再等用户第一次点 × 才装图标。

        用户的要求是"点 × 隐藏到托盘"，如果图标要等到点 × 那一刻才创建，
        第一次点的时候会有延迟、失败了还会把窗口弄丢。这里钉住
        `_setup_tray()` 一调就能就绪，而且 `_hide_to_tray()` 直接复用。
        """
        app = winmon.WinMonApp.__new__(winmon.WinMonApp)
        app.root = self.root
        app._tray = None
        app._tray_window = None
        app._tray_polling = False
        app._tray_ok = False
        app._tray_error = ""
        app._set_status = lambda _t: None
        # `restore_from_tray` 现在会顺手补一帧预览（P2：隐藏期间不取帧）——
        # 真 `__init__` 一定会把 `preview_key` 设成 None，替身 app 也得给一个。
        app.preview_key = None
        for name in ("_tray_available", "_tray_hwnd", "_poll_tray"):
            if not hasattr(app, name):
                continue
        ok = winmon.WinMonApp._setup_tray(app)
        self.assertTrue(ok, getattr(app, "_tray_error", "托盘没起来"))
        self.assertTrue(app._tray_ok)
        self.assertIsNotNone(app._tray)
        self.assertIsNotNone(app._tray_window)
        # 隐藏到托盘应当直接成功（不重新创建）
        ok2, msg = winmon.WinMonApp._hide_to_tray(app)
        self.assertTrue(ok2, msg)
        self.assertFalse(self.root.winfo_ismapped(), "隐藏后主窗口不该还显示着")
        winmon.WinMonApp.restore_from_tray(app)
        self.root.update()
        self.assertTrue(self.root.winfo_ismapped(), "恢复后主窗口要显示出来")
        self.assertTrue(app._tray_ok, "恢复窗口不该把托盘图标摘掉（它现在是常驻的）")
        winmon.WinMonApp._teardown_tray(app)
        self.assertFalse(app._tray_ok)


class TestCloseBehavior(unittest.TestCase):
    """设置里的「关闭行为」：默认结束进程；选托盘时点 × 只是收起来。"""

    def test_default_is_exit(self):
        """原始需求："默认为后者"（后者 = 结束进程）。"""
        self.assertEqual(winmon.CLOSE_MODE_DEFAULT, winmon.CLOSE_MODE_EXIT)
        slugs = [slug for slug, _l in winmon.CLOSE_MODE_CHOICES]
        self.assertIn(winmon.CLOSE_MODE_TRAY, slugs)
        self.assertIn(winmon.CLOSE_MODE_EXIT, slugs)

    def test_close_mode_is_persisted_and_invalid_values_fall_back(self):
        import json
        import shutil
        import tempfile
        try:
            root = tk.Tk()
            root.withdraw()
        except Exception as exc:                              # pragma: no cover
            self.skipTest(f"没有图形会话：{exc}")
        self.addCleanup(root.destroy)
        tmp = tempfile.mkdtemp(prefix="winmon-close-")
        self.addCleanup(lambda: shutil.rmtree(tmp, ignore_errors=True))
        settings = os.path.join(tmp, "winmon_settings.json")
        with open(settings, "w", encoding="utf-8") as fh:
            fh.write(json.dumps({"close_mode": "胡说"}))

        app = winmon.WinMonApp.__new__(winmon.WinMonApp)
        app.root = root
        app._settings_path = settings
        app._settings_after_id = None
        app.show_hidden = tk.BooleanVar(value=False)
        app.show_no_window = tk.BooleanVar(value=False)
        app.only_windowed = tk.BooleanVar(value=True)
        app.follow_selection = tk.BooleanVar(value=True)
        app.live_gallery = tk.BooleanVar(value=True)
        app.history_visible = tk.BooleanVar(value=False)
        app.size_choice = tk.StringVar(value=winmon.PREVIEW_SIZES[1][0])
        app.fps_choice = tk.StringVar(value="很流畅 10 FPS")
        app.search_text = tk.StringVar()
        app.status_text = tk.StringVar()
        app.overview_text = tk.StringVar()
        app._init_video_vars()          # 关闭行为 / 媒体 / 保留天数等都在这一处创建
        app.video_close_mode.set("smart")
        app.history_days.set(7)
        # `save_settings_now` 还要读 `app.catalog` 的两个偏好（纯逻辑替身就够）
        app.catalog = mock.Mock(selected_categories=set(),
                                only_favorites=tk.BooleanVar(value=False),
                                only_recent=tk.BooleanVar(value=False))
        app.load_settings()
        self.assertEqual(app.close_mode.get(), winmon.CLOSE_MODE_DEFAULT,
                         "非法的 close_mode 应当退回默认（结束进程）")

        app.close_mode.set(winmon.CLOSE_MODE_TRAY)
        app.start_minimized.set(True)
        app.save_settings_now()
        with open(settings, encoding="utf-8") as fh:
            saved = json.load(fh)
        self.assertEqual(saved["close_mode"], winmon.CLOSE_MODE_TRAY)
        self.assertIs(saved["start_minimized"], True)

        # 再读回来：两项都要还在（设置界面之外没人能改它）
        app2 = winmon.WinMonApp.__new__(winmon.WinMonApp)
        app2.root = root
        app2._settings_path = settings
        app2._settings_after_id = None
        for name, var in (("show_hidden", tk.BooleanVar(value=False)),
                          ("show_no_window", tk.BooleanVar(value=False)),
                          ("only_windowed", tk.BooleanVar(value=True)),
                          ("follow_selection", tk.BooleanVar(value=True)),
                          ("live_gallery", tk.BooleanVar(value=True)),
                          ("history_visible", tk.BooleanVar(value=False))):
            setattr(app2, name, var)
        app2.size_choice = tk.StringVar(value=winmon.PREVIEW_SIZES[1][0])
        app2.fps_choice = tk.StringVar(value="很流畅 10 FPS")
        app2.search_text = tk.StringVar()
        app2.status_text = tk.StringVar()
        app2.overview_text = tk.StringVar()
        app2._init_video_vars()
        app2.video_close_mode.set("smart")
        app2.catalog = mock.Mock(selected_categories=set(),
                                 only_favorites=tk.BooleanVar(value=False),
                                 only_recent=tk.BooleanVar(value=False))
        app2.load_settings()
        self.assertEqual(app2.close_mode.get(), winmon.CLOSE_MODE_TRAY)
        self.assertTrue(app2.start_minimized.get())
        self.assertEqual(app2.history_days.get(), 7, "日志保留天数也要往返")


class TestHistoryLog(unittest.TestCase):
    """
    操作历史的两条线：**内存列表** + **磁盘日志**（用户要的"独立日志记录放在
    D 盘的 winmon 里"）。

    用 `WINMON_HISTORY_LOG` 指到临时目录 —— 测试**绝不能**往用户真实的历史
    日志里写东西（那本身就是一份审计留痕）。
    """

    def setUp(self):
        import shutil
        import tempfile
        self.tmp = tempfile.mkdtemp(prefix="winmon-hist-")
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        self.log = os.path.join(self.tmp, core.HISTORY_LOG_NAME)
        self._old_env = os.environ.get("WINMON_HISTORY_LOG")
        os.environ["WINMON_HISTORY_LOG"] = self.log
        self.addCleanup(self._restore_env)

    def _restore_env(self):
        if self._old_env is None:
            os.environ.pop("WINMON_HISTORY_LOG", None)
        else:
            os.environ["WINMON_HISTORY_LOG"] = self._old_env

    def test_log_path_defaults_into_the_data_dir(self):
        """
        默认位置 = **运行时数据目录**（不再写在程序目录里），可用环境变量覆盖。

        2026-10-16 改：程序目录只留源与文档 —— 别人 clone 下来跑一次，
        `git status` 也该是干净的。便携模式（`WINMON_DATA_DIR`）另说。
        """
        os.environ.pop("WINMON_HISTORY_LOG", None)
        self.assertEqual(core.history_log_path(),
                         core.data_file(core.HISTORY_LOG_NAME))
        if not os.environ.get(core.DATA_DIR_ENV) and (
                os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")):
            self.assertNotEqual(os.path.dirname(core.history_log_path()), core.APP_DIR,
                                "日志不该再写回程序目录（那样 clone 下来跑一次就脏了）")
        os.environ["WINMON_HISTORY_LOG"] = self.log
        self.assertEqual(core.history_log_path(), self.log)

    def test_every_action_is_appended_to_disk(self):
        h = core.ActionHistory(log_path=self.log)
        h.add("关闭窗口", "记事本 - 未命名", "已关闭（回读核对：窗口已消失）")
        h.add("结束进程", "notepad.exe [1234]", "强制结束成功（退出码 1）", "3 个窗口")
        text = open(self.log, encoding="utf-8").read()
        self.assertIn("关闭窗口", text)
        self.assertIn("notepad.exe [1234]", text)
        self.assertIn("3 个窗口", text)
        self.assertEqual(len(text.rstrip().split("\n")), 2, "两条记录应当正好两行")
        self.assertEqual(h.log_error, "")

    def test_log_survives_a_restart(self):
        """跨会话保留：新开一个 ActionHistory 也能读到上次的记录。"""
        core.ActionHistory(log_path=self.log).add("关闭窗口", "A", "已关闭")
        rows = core.ActionHistory(log_path=self.log).load_from_log()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["action"], "关闭窗口")
        self.assertEqual(rows[0]["target"], "A")

    def test_newlines_and_tabs_are_flattened(self):
        """记录里带换行/制表符时必须压平，否则一条记录会占两行、读回来就错位。"""
        h = core.ActionHistory(log_path=self.log)
        h.add("测试", "含\t制表符\n与换行", "结果")
        lines = open(self.log, encoding="utf-8").read().rstrip().split("\n")
        self.assertEqual(len(lines), 1, f"被拆成了多行：{lines}")
        rows = h.load_from_log()
        self.assertEqual(rows[0]["target"], "含 制表符 与换行")

    def test_bad_lines_are_skipped_not_fatal(self):
        """用户可能用记事本改过这个文件：坏行要跳过，不能崩。"""
        with open(self.log, "w", encoding="utf-8") as fh:
            fh.write("# 注释行\n")
            fh.write("这不是一条合法记录\n")
            fh.write("2026-10-12 10:00:00\t关闭窗口\tA\t已关闭\n")
            fh.write("\n")
        rows = core.ActionHistory(log_path=self.log).load_from_log()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["target"], "A")
        self.assertEqual(rows[0]["time"], "2026-10-12 10:00:00")

    def test_clear_list_does_not_touch_the_log(self):
        """「清空列表」只清内存 —— 日志是留痕，不该被顺手删掉。"""
        h = core.ActionHistory(log_path=self.log)
        h.add("关闭窗口", "A", "已关闭")
        h.clear()
        self.assertEqual(len(h), 0)
        self.assertTrue(os.path.exists(self.log), "清空列表把日志文件也删了")
        self.assertEqual(len(h.load_from_log()), 1)

    def test_clear_log_file_removes_it(self):
        h = core.ActionHistory(log_path=self.log)
        h.add("关闭窗口", "A", "已关闭")
        ok, msg = h.clear_log_file()
        self.assertTrue(ok, msg)
        self.assertFalse(os.path.exists(self.log))

    def test_log_failure_is_reported_not_raised(self):
        """
        写日志失败**不能打断主流程**（关窗口才是正事），但错误要留痕给界面。

        这里用一个"指向目录"的路径制造写入失败。
        """
        h = core.ActionHistory(log_path=self.tmp)
        h.add("关闭窗口", "A", "已关闭")          # 不该抛异常
        self.assertEqual(len(h), 1, "写日志失败不该影响内存里的记录")
        self.assertTrue(h.log_error, "失败原因要留下来给界面显示")

    def test_rotation_keeps_two_files(self):
        """日志超过上限要轮转（改名成 `.1`），不至于无限膨胀。"""
        h = core.ActionHistory(log_path=self.log)
        h.add("关闭窗口", "A", "已关闭")
        old = core.HISTORY_LOG_MAX_BYTES
        core.HISTORY_LOG_MAX_BYTES = 10
        self.addCleanup(lambda: setattr(core, "HISTORY_LOG_MAX_BYTES", old))
        h.add("结束进程", "B", "已结束")
        self.assertTrue(os.path.exists(self.log))
        self.assertTrue(os.path.exists(self.log + ".1"), "轮转出来的备份不见了")

    def test_export_writes_readable_text(self):
        h = core.ActionHistory(log_path=self.log)
        h.add("关闭窗口", "记事本", "已关闭")
        out = os.path.join(self.tmp, "export.txt")
        h.export(out)
        text = open(out, encoding="utf-8").read()
        self.assertIn("# WinMon 操作记录", text)
        self.assertIn("记事本", text)

    # ---- 用户反馈："缺乏实时操作时间" → 到分钟（2026-10-12：他要求不要再精确到秒）----

    def test_timestamp_is_minute_precision(self):
        h = core.ActionHistory(log_path=self.log)
        h.add("关闭窗口", "A", "已关闭")
        stamp = h.items[0]["time"]
        self.assertRegex(stamp, r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}$",
                         f"时间戳应当只到分钟：{stamp!r}")

    def test_old_second_and_millisecond_stamps_still_parse(self):
        """换过两次格式（带毫秒 → 到秒 → 到分钟），老日志必须照样能读出时间。"""
        cases = {
            "2026-10-12 13:40": 1,          # 现在这种
            "2026-10-12 13:40:08": 1,       # 曾经到秒
            "2026-10-12 13:40:08.123": 1,   # 曾经带毫秒
            "看不懂的时间": 0,
        }
        for text, expect in cases.items():
            got = core.ActionHistory.parse_time(text)
            self.assertEqual(bool(got), bool(expect), f"{text!r} -> {got}")

    def test_millisecond_stamps_keep_their_order(self):
        """老格式（带毫秒）读回来时，同一秒内的先后仍然分得出来。"""
        h = core.ActionHistory(log_path=self.log)
        with open(self.log, "w", encoding="utf-8") as fh:
            fh.write("2026-10-12 13:40:08.100\t第一件\tA\tok\n")
            fh.write("2026-10-12 13:40:08.900\t第二件\tB\tok\n")
        rows = h.load_from_log()          # 最新在前
        self.assertEqual(rows[0]["action"], "第二件")
        self.assertGreater(core.ActionHistory.parse_time(rows[0]["time"]),
                           core.ActionHistory.parse_time(rows[1]["time"]))

    def test_open_time_is_written_and_read_back_as_its_own_column(self):
        """
        用户反馈：「没法明确哪个程序打开哪个程序关闭了」→
        对象改成"程序名.exe [PID]｜窗口标题"，并单列一列"打开时间"。
        """
        h = core.ActionHistory(log_path=self.log)
        h.add("关闭窗口", "notepad.exe [1234]｜未命名 - 记事本", "已关闭（回读核对）",
              open_time="09:15:02")
        line = open(self.log, encoding="utf-8").read().rstrip()
        self.assertEqual(line.count("\t"), 5, f"应当是 6 列：{line!r}")
        row = h.load_from_log()[0]
        self.assertEqual(row["target"], "notepad.exe [1234]｜未命名 - 记事本")
        self.assertEqual(row["open_time"], "09:15:02")

    def test_old_five_column_logs_still_read(self):
        """老日志（没有"打开时间"那列）必须照样读得回来。"""
        with open(self.log, "w", encoding="utf-8") as fh:
            fh.write("2026-10-12 10:00:00\t关闭窗口\tA\t已关闭\t备注\n")
        row = core.ActionHistory(log_path=self.log).load_from_log()[0]
        self.assertEqual(row["extra"], "备注")
        self.assertEqual(row["open_time"], "")

    # ---- 保留天数（约定：默认 30 天，另设 7 / 90）----

    def _write_old_line(self, days_ago: float, action: str = "结束进程") -> None:
        stamp = time.strftime("%Y-%m-%d %H:%M:%S",
                              time.localtime(time.time() - days_ago * 86400))
        with open(self.log, "a", encoding="utf-8") as fh:
            fh.write(f"{stamp}\t{action}\told.exe\t已结束\n")

    def test_prune_keeps_recent_and_drops_old(self):
        h = core.ActionHistory(log_path=self.log)
        h.add("关闭窗口", "今天.exe", "已关闭")
        self._write_old_line(10)
        self._write_old_line(40)
        dropped, note = h.prune_log(30)
        self.assertEqual(dropped, 1, note)
        targets = [r["target"] for r in h.load_from_log()]
        self.assertIn("今天.exe", targets)
        self.assertIn("old.exe", targets, "10 天前的记录不该被 30 天档删掉")

    def test_prune_writes_a_backup_before_deleting(self):
        h = core.ActionHistory(log_path=self.log)
        h.add("关闭窗口", "今天.exe", "已关闭")
        self._write_old_line(40)
        h.prune_log(30)
        self.assertTrue(os.path.exists(self.log + ".bak"),
                        "删记录前应当留一份 .bak，删错了还能找回来")

    def test_prune_seven_days_is_stricter_than_ninety(self):
        h = core.ActionHistory(log_path=self.log)
        self._write_old_line(20)
        self.assertEqual(h.prune_log(90)[0], 0, "20 天前的记录在 90 天档里应当留下")
        self.assertEqual(h.prune_log(7)[0], 1, "20 天前的记录在 7 天档里应当删掉")

    def test_prune_zero_means_keep_everything(self):
        h = core.ActionHistory(log_path=self.log)
        self._write_old_line(999)
        dropped, note = h.prune_log(0)
        self.assertEqual(dropped, 0)
        self.assertIn("未清理", note)

    def test_prune_keeps_unparsable_lines(self):
        """时间认不出来的行**保留**（宁可留着，也不因为格式怪就删记录）。"""
        h = core.ActionHistory(log_path=self.log)
        # 先写"认不出来的行"，再用 add() 追加一条真正带时间戳的旧记录 ——
        # 用 add() 写是为了拿到本程序自己的格式（手写字符串容易写错列数）。
        with open(self.log, "w", encoding="utf-8") as fh:
            fh.write("# 我手写的注释\n")
            fh.write("这不是一条记录\n")
        stamp = time.strftime("%Y-%m-%d %H:%M:%S",
                              time.localtime(time.time() - 40 * 86400))
        with open(self.log, "a", encoding="utf-8") as fh:
            fh.write(f"{stamp}\t结束进程\told.exe\t已结束\n")
        dropped, note = h.prune_log(30)
        self.assertEqual(dropped, 1)
        text = open(self.log, encoding="utf-8").read()
        self.assertIn("# 我手写的注释", text)
        self.assertIn("这不是一条记录", text)

    def test_prune_on_a_missing_file_is_not_an_error(self):
        h = core.ActionHistory(log_path=os.path.join(self.tmp, "没有这个.log"))
        dropped, note = h.prune_log(30)
        self.assertEqual(dropped, 0)
        self.assertIn("不存在", note)


class TestHistoryWindow(unittest.TestCase):
    """独立的「操作历史」窗口：真开一次、真读日志、真关掉。"""

    def setUp(self):
        import shutil
        import tempfile
        try:
            self.root = tk.Tk()
            self.root.withdraw()
        except Exception as exc:                              # pragma: no cover
            self.skipTest(f"没有图形会话：{exc}")
        self.addCleanup(self.root.destroy)
        self.tmp = tempfile.mkdtemp(prefix="winmon-histwin-")
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        self.log = os.path.join(self.tmp, core.HISTORY_LOG_NAME)
        core.ActionHistory(log_path=self.log).add("关闭窗口", "记事本", "已关闭（回读核对）")

    def _app(self):
        app = winmon.WinMonApp.__new__(winmon.WinMonApp)
        app.root = self.root
        app.history = core.ActionHistory(log_path=self.log)
        app._history_window = None
        app.status_text = tk.StringVar()
        app._set_status = lambda t: app.status_text.set(t)
        app._copy = lambda t: None
        return app

    def test_window_shows_the_log_rows(self):
        app = self._app()
        app.action_history()
        win = app._history_window
        self.assertIsNotNone(win)
        self.addCleanup(win.close)
        self.root.update()
        rows = win.tree.get_children()
        self.assertEqual(len(rows), 1, "日志里的记录应当显示出来")
        values = win.tree.item(rows[0], "values")
        self.assertIn("关闭窗口", values)
        self.assertIn("记事本", values)

    def test_open_time_column_is_shown(self):
        """「打开时间」列要有，而且没有值的时候显示占位符而不是空白。"""
        app = self._app()
        app.history.add("关闭窗口", "notepad.exe [1]｜未命名", "已关闭", open_time="09:15")
        app.history.add("关闭窗口", "x.exe [2]｜另一个", "已关闭")
        app.action_history()
        win = app._history_window
        self.addCleanup(win.close)
        self.root.update()
        heads = [win.tree.heading(c)["text"] for c in win.tree["columns"]]
        self.assertIn("程序打开时间", heads)
        values = [win.tree.item(r, "values") for r in win.tree.get_children()]
        open_times = [v[1] for v in values]
        self.assertIn("09:15", open_times)
        self.assertIn("—", open_times, "没读到打开时间时应当显示占位符")

    def test_status_line_reports_the_retention_window(self):
        app = self._app()
        app.history_days = tk.IntVar(value=90)
        app.action_history()
        win = app._history_window
        self.addCleanup(win.close)
        self.root.update()
        self.assertIn("90 天", win.status.get())

    def test_filter_by_action_and_keyword(self):
        app = self._app()
        app.history.add("结束进程", "chrome.exe [9]", "强制结束成功")
        app.action_history()
        win = app._history_window
        self.addCleanup(win.close)
        self.root.update()
        self.assertEqual(len(win.tree.get_children()), 2)

        win.kind.set("结束进程")
        win.reload()
        self.assertEqual(len(win.tree.get_children()), 1, "按动作筛选没生效")

        win.kind.set("（全部）")
        win.query.set("chrome")
        win.reload()
        self.assertEqual(len(win.tree.get_children()), 1, "关键词筛选没生效")

        win.query.set("根本不存在的东西")
        win.reload()
        self.assertEqual(len(win.tree.get_children()), 0)

    def test_reopen_after_close(self):
        """关掉之后还能再打开（引用要清干净，否则按钮变成没反应）。"""
        app = self._app()
        app.action_history()
        app._history_window.close()
        self.assertIsNone(app._history_window)
        app.action_history()
        self.assertIsNotNone(app._history_window, "关掉之后应当还能再打开")
        app._history_window.close()

    def test_log_action_refreshes_an_open_window(self):
        """开着历史窗口时发生的操作要立刻出现在里面（不用手动点刷新）。"""
        app = self._app()
        app.action_history()
        win = app._history_window
        self.addCleanup(win.close)
        self.root.update()
        before = len(win.tree.get_children())
        app.log_action("结束进程", "x.exe [1]", "强制结束成功")
        self.root.update()
        self.assertEqual(len(win.tree.get_children()), before + 1)

    # ---- 2026-10-13 修掉的两个真 bug（都属"按钮/导出看着能用、实际不对"）----

    def test_clear_list_really_empties_the_window(self):
        """
        「清空列表」必须**真的把窗口清空**，且日志文件不动（README 的约定）。

        以前它只清内存就 `reload()`，而 reload 又去读日志文件 ——
        于是列表原样回来、状态栏却说"已清空"（复现出来的真 bug）。
        """
        app = self._app()
        app.history.add("结束进程", "chrome.exe [9]", "强制结束成功")
        app.action_history()
        win = app._history_window
        self.addCleanup(win.close)
        self.root.update()
        self.assertEqual(len(win.tree.get_children()), 2)

        win.clear_list()
        self.root.update()
        self.assertEqual(win.tree.get_children(), (),
                         "点了「清空列表」列表却还在")
        self.assertTrue(os.path.exists(self.log), "日志文件不能被这一步删掉")
        self.assertEqual(len(app.history.load_from_log()), 2,
                         "磁盘上的留痕必须原样保留")
        self.assertIn("日志文件未动", win.status.get())

    def test_records_after_a_clear_still_show_up(self):
        """清空只影响"已经显示过的"，之后新产生的记录要照常出现（不能变成永久空窗口）。"""
        app = self._app()
        app.action_history()
        win = app._history_window
        self.addCleanup(win.close)
        self.root.update()
        win.clear_list()
        self.assertEqual(win.tree.get_children(), ())

        app.log_action("结束进程", "afterclear.exe [7]", "强制结束成功")
        self.root.update()
        rows = win.tree.get_children()
        self.assertEqual(len(rows), 1, f"清空之后的新记录没显示：{len(rows)} 条")
        values = [str(x) for x in win.tree.item(rows[0], "values")]
        self.assertTrue(any("afterclear.exe" in v for v in values), values)

    def test_export_follows_the_filter_even_when_it_matches_nothing(self):
        """
        筛到 0 条时导出必须是"没有可导出的"，**不能把全部记录倒出来**。

        以前写的是 `getattr(self, "_kept", None) or self._rows` ——
        `_kept == []` 是假值，于是退回未筛选的 `_rows`：
        界面显示"0 条"、导出文件里却是全部（真 bug）。
        """
        app = self._app()
        app.history.add("结束进程", "chrome.exe [9]", "强制结束成功")
        app.action_history()
        win = app._history_window
        self.addCleanup(win.close)
        self.root.update()
        win.query.set("根本不存在的东西")
        win.reload()
        self.assertEqual(win.tree.get_children(), ())
        self.assertEqual(win._kept, [])

        calls: list[str] = []
        win.app.history.export = lambda path="", rows=None: calls.append("txt")
        win.app.history.export_csv = lambda path="", rows=None: calls.append("csv")
        win.export_csv()
        win.export()
        self.assertEqual(calls, [], f"筛到 0 条却仍然导出了：{calls}")
        self.assertIn("没有可导出", win.status.get())


class TestMediaSessionLive(unittest.TestCase):
    """
    **只观察、不造环境**：从系统媒体会话里读一份真实数据，核对字段是否可信。

    为什么改成这样（2026-10-16，原始需求"脱离个人环境"）：以前这个类会
    **自己拉起本机的 Edge/Chrome** 去放一段在线音频来"造"一个会话 ——
    等于把测试绑死在"这台机器装了哪个浏览器 + 能联网 + 能出声"上，
    换台机器就完全测不出东西。现在把两件事分家：

      · **这里**只做观察：本机此刻真有"正在播放"的会话，就核对它；
        没有就 `skipTest` 并说明原因 —— **绝不为了测试去改这台机器的状态**；
      · "自己造一个会话"那套流程移到**手动探针**
        `tests/media_session_probe.py --play-audio`（开发时想验真实链路就跑它，
        全量测试不再依赖它，也就不再依赖任何本机装着的东西）。

    链路本身照样被覆盖：WinRT 投影 / PowerShell 版本 / 曲名编码 / 异步等待
    在 `test_b4` 其余用例里用**替身字节流**逐环钉住（GBK 解码、快照回退…），
    那些用例一个外部依赖都没有；`test_b12` 则用**替身会话行**覆盖任意 app id。
    """

    def _sessions(self):
        return core.collect_media_sessions()

    def test_reads_a_real_media_session(self):
        got = self._sessions()
        if not got.get("ok"):
            # 拿不到就如实跳过（多为沙箱/策略限制），并说明原因
            self.skipTest(f"读不到媒体会话：{got.get('error')}")

        rows = got.get("sessions") or []
        playing = [s for s in rows if int(s.get("status") or 0) == 4]
        if not playing:
            self.skipTest("现在没有正在播放的媒体会话（本用例只观察，不替你造一个）")
        # 曲名必须读得到（这一条就是用户说的"只能看到标题"的解药）
        with_title = [s for s in playing if str(s.get("title") or "").strip()]
        self.assertTrue(with_title, f"会话里没有曲名：{rows}")
        for s in with_title[:3]:
            with self.subTest(app=s.get("app")):
                self.assertTrue(str(s.get("app") or "").strip(), "应用 id 不能为空")
                self.assertIn(int(s.get("status") or 0), (0, 1, 2, 3, 4, 5))
                self.assertGreaterEqual(int(s.get("position") or 0), 0)
                self.assertGreaterEqual(int(s.get("duration") or 0), 0)

    def test_every_real_session_becomes_a_selectable_media_app(self):
        """
        真实会话 → 下拉框清单：**每一个**会话里的 app id 都要能变成一条可选条目。

        这是"基于所有可识别的媒体"落在**真数据**上的那一步：不再要求它出现在
        写死的候选表里，也不再要求它就装在本机 —— 只要能发布会话就该认得出来。
        """
        got = self._sessions()
        if not got.get("ok"):
            self.skipTest(f"读不到媒体会话：{got.get('error')}")
        rows = got.get("sessions") or []
        ids = core.session_app_ids(rows)
        if not ids:
            self.skipTest("现在没有任何媒体会话")

        labels, mapping = winmon.build_media_app_entries(
            running={str(i.get("name") or "").lower()
                     for i in core.snapshot_processes().values()},
            available=lambda exe: bool(core.media_app_path(exe)),
            found=[], sessions=ids)
        self.assertEqual(labels[0], winmon.MEDIA_APP_ANY)
        for app_id in ids:
            with self.subTest(app=app_id):
                hit = [lbl for lbl in labels if mapping.get(lbl) is not None
                       and core.media_app_stems_match(
                           mapping[lbl].exe or mapping[lbl].label, app_id)]
                self.assertTrue(hit, f"{app_id} 没进清单：{labels}")
                self.assertTrue(mapping[hit[0]].exe,
                                "清单里的条目必须带得上 exe（空 exe 会让命令发错人）")

    def test_summary_picks_the_playing_one(self):
        got = self._sessions()
        if not got.get("ok"):
            self.skipTest(f"读不到媒体会话：{got.get('error')}")
        rows = got.get("sessions") or []
        if not any(int(s.get("status") or 0) == 4 for s in rows):
            self.skipTest("现在没有正在播放的媒体会话")
        info = core.media_session_summary(rows)
        self.assertTrue(info["found"])
        self.assertTrue(info["text"].strip())
        self.assertGreaterEqual(info["playing"], 1)


class TestBrowserNotMistakenForPlayer(unittest.TestCase):
    """
    `find_video_window` 不能把浏览器窗口当成播放器。

    实测踩到：用 Edge 打开视频页后，Edge 的标题是
    「2026年荣获世界第一的巴厘岛…_哔哩哔哩_bilibili - 个人 - Microsoft Edge」，
    里面含 "bilibili"，于是旧实现会返回**浏览器窗口**，
    之后所有键盘消息都发给了浏览器（白费功夫，还看不出错）。
    """

    def test_browser_processes_are_excluded(self):
        self.assertIn("msedge.exe", core.BROWSER_PROCESSES)
        self.assertIn("chrome.exe", core.BROWSER_PROCESSES)
        wi = core.find_video_window("bilibili")
        if wi is None:
            self.skipTest("没有找到播放器窗口")
        procs = core.snapshot_processes()
        name = (procs.get(wi.pid, {}).get("name") or "").lower()
        if not name:
            self.skipTest("拿不到该窗口的进程名，无法判定")
        self.assertNotIn(name, core.BROWSER_PROCESSES,
                         f"返回了浏览器窗口：{name}")

    def test_all_windows_with_bilibili_title_are_checked(self):
        """若同时存在浏览器与客户端，必须选中客户端。"""
        cands = [wi for wi in core.enum_windows(include_cloaked=True)
                 if "bilibili" in (wi.title or "").lower()
                 or "哔哩" in (wi.title or "")]
        if len(cands) < 2:
            self.skipTest("当前没有同时存在浏览器与客户端窗口")
        procs = core.snapshot_processes()
        chosen = core.find_video_window("bilibili")
        self.assertIsNotNone(chosen)
        name = (procs.get(chosen.pid, {}).get("name") or "").lower()
        self.assertNotIn(name, core.BROWSER_PROCESSES)


class TestRoundTwoFeatures(unittest.TestCase):
    """
    2026-10-12 第二批：A.1 快照 / A.2 多会话 / B.1 CSV 与按程序筛 /
    B.4 参数启动 / C.1 单实例 / C.2 旧配置迁移。
    """

    # ---- A.1：常驻快照（把"每次起一个 PowerShell（~290ms）"换成"读文件（~0.05ms）"）----

    def test_snapshot_read_is_used_when_fresh(self):
        import json
        import tempfile
        tmp = tempfile.mkdtemp(prefix="wm-snap-read-")
        self.addCleanup(lambda: __import__("shutil").rmtree(tmp, ignore_errors=True))
        path = os.path.join(tmp, "snap.jsonl")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write('{"ok":true,"error":"","sessions":[],"ts":1}\n')
            fh.write(json.dumps({"ok": True, "error": "",
                                 "sessions": [{"app": "MSEdge", "title": "最后一行的才是最新",
                                               "status": 4}]}) + "\n")
        old = os.environ.get(core.MEDIA_SNAPSHOT_ENV)
        os.environ[core.MEDIA_SNAPSHOT_ENV] = path
        self.addCleanup(lambda: os.environ.pop(core.MEDIA_SNAPSHOT_ENV, None)
                        if old is None else os.environ.update({core.MEDIA_SNAPSHOT_ENV: old}))
        got = core.read_media_snapshot()
        self.assertIsNotNone(got)
        self.assertEqual(got["sessions"][0]["title"], "最后一行的才是最新",
                         "必须取最后一行（最新那次写进去的）")

    def test_stale_or_broken_snapshot_falls_back(self):
        import tempfile
        tmp = tempfile.mkdtemp(prefix="wm-snap-old-")
        self.addCleanup(lambda: __import__("shutil").rmtree(tmp, ignore_errors=True))
        path = os.path.join(tmp, "snap.jsonl")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("{坏 JSON\n")
        old = os.environ.get(core.MEDIA_SNAPSHOT_ENV)
        os.environ[core.MEDIA_SNAPSHOT_ENV] = path
        self.addCleanup(lambda: os.environ.pop(core.MEDIA_SNAPSHOT_ENV, None)
                        if old is None else os.environ.update({core.MEDIA_SNAPSHOT_ENV: old}))
        self.assertIsNone(core.read_media_snapshot(), "坏内容应当当作没有快照")
        old_time = time.time() - 999
        os.utime(path, (old_time, old_time))
        with open(path, "w", encoding="utf-8") as fh:
            fh.write('{"ok":true,"sessions":[]}\n')
        os.utime(path, (old_time, old_time))
        self.assertIsNone(core.read_media_snapshot(), "过期快照应当当作没有快照")

    def test_missing_snapshot_is_not_an_error(self):
        old = os.environ.get(core.MEDIA_SNAPSHOT_ENV)
        os.environ[core.MEDIA_SNAPSHOT_ENV] = r"X:\绝对没有这个目录\nope.jsonl"
        self.addCleanup(lambda: os.environ.pop(core.MEDIA_SNAPSHOT_ENV, None)
                        if old is None else os.environ.update({core.MEDIA_SNAPSHOT_ENV: old}))
        self.assertIsNone(core.read_media_snapshot())

    # ---- A.2：多会话 / 多选 ----

    def test_multi_key_matches_several_sessions(self):
        rows = [{"app": "MSEdge", "title": "网页那首", "status": 4},
                {"app": "cloudmusic.exe", "title": "客户端那首", "status": 5}]
        info = core.media_session_summary(rows, "msedge.exe,cloudmusic.exe")
        self.assertTrue(info["found"])
        self.assertEqual(info["picked"]["app"], "MSEdge")
        self.assertEqual(info["count"], 2, "应当知道一共有几个会话")
        self.assertEqual(len(info["others"]), 1, "应当给出'其余在放什么'")
        self.assertIn("客户端那首", info["others"][0])

    def test_single_key_still_works(self):
        rows = [{"app": "MSEdge", "title": "a", "status": 4},
                {"app": "cloudmusic.exe", "title": "b", "status": 4}]
        info = core.media_session_summary(rows, "cloudmusic.exe")
        self.assertEqual(info["picked"]["app"], "cloudmusic.exe")
        self.assertIn("⚠" not in info["text"], [True])

    # ---- B.1：按程序筛 + CSV 导出 ----

    def test_history_program_extraction(self):
        app = winmon.WinMonApp.__new__(winmon.WinMonApp)
        win = winmon._HistoryWindow
        self.assertEqual(win._program_of({"target": "notepad.exe [12]｜未命名"}),
                         "notepad.exe")
        self.assertEqual(win._program_of({"target": "chrome.exe [999]｜某网页"}),
                         "chrome.exe")
        # 没有 exe 的记录（比如恢复工作区）也要能归类
        self.assertEqual(win._program_of({"target": "工作区「写代码」"}), "工作区「写代码」")
        self.assertEqual(win._program_of({}), "")
        del app

    def test_history_csv_export_has_bom_and_headers(self):
        import tempfile
        tmp = tempfile.mkdtemp(prefix="wm-csv-")
        self.addCleanup(lambda: __import__("shutil").rmtree(tmp, ignore_errors=True))
        log = os.path.join(tmp, "h.log")
        h = core.ActionHistory(log_path=log)
        h.add("关闭窗口", "notepad.exe [1]｜含,逗号\"引号", "已关闭", open_time="09:00")
        out = os.path.join(tmp, "out.csv")
        h.export_csv(out)
        raw = open(out, "rb").read()
        self.assertTrue(raw.startswith(b"\xef\xbb\xbf"), "CSV 要带 BOM，Excel 打开才不乱码")
        text = raw.decode("utf-8-sig")
        self.assertIn("时间,程序打开时间,动作,对象,结果,备注", text)
        self.assertIn("notepad.exe", text)
        self.assertIn("09:00", text)

    def test_history_txt_export_can_limit_to_filtered_rows(self):
        import tempfile
        tmp = tempfile.mkdtemp(prefix="wm-txt-")
        self.addCleanup(lambda: __import__("shutil").rmtree(tmp, ignore_errors=True))
        log = os.path.join(tmp, "h.log")
        h = core.ActionHistory(log_path=log)
        h.add("关闭窗口", "只想要这条.exe", "已关闭")
        h.add("结束进程", "不想要这条.exe", "已结束")
        out = os.path.join(tmp, "out.txt")
        h.export(out, rows=[{"time": "2026-01-01 00:00", "action": "关闭窗口",
                             "target": "只想要这条.exe", "result": "已关闭",
                             "extra": "", "open_time": ""}])
        text = open(out, encoding="utf-8").read()
        self.assertIn("只想要这条.exe", text)
        self.assertNotIn("不想要这条.exe", text, "筛选后导出不该带上别的记录")

    # ---- B.4：参数启动（launch_item 早就支持，这里钉住界面用的那个入口）----

    def test_launch_item_accepts_extra_args(self):
        import inspect
        params = inspect.signature(core.launch_item).parameters
        self.assertIn("extra_args", params)

    def test_explorer_fallback_refuses_to_drop_arguments(self):
        """
        explorer 兜底**送不进参数**（2026-10-13 实测：
        `explorer.exe "<脚本>" 参数1 参数2` 什么都不启动，只给路径才启动）。
        所以带参数时必须如实拒绝 —— 否则就是"启动了但没带参数"却报成功，
        用户会以为参数生效了（铁律 2 不允许）。

        先用一个真会留下痕迹的 .cmd 证明"不带参数时这条路是通的"，
        再证明"带参数时既不启动、也不假报成功"。
        """
        import shutil
        tmp = tempfile.mkdtemp(prefix="wm-shellargs-")
        self.addCleanup(lambda: shutil.rmtree(tmp, ignore_errors=True))
        marker = os.path.join(tmp, "ran.txt")
        script = os.path.join(tmp, "bark.cmd")
        with open(script, "w", encoding="ascii") as fh:
            fh.write("@echo off\r\necho ran > \"%s\"\r\n" % marker)

        ok, msg = core.start_via_shell(script)
        if ok:
            deadline = time.time() + 6
            while time.time() < deadline and not os.path.exists(marker):
                time.sleep(0.1)
        if not os.path.exists(marker):
            self.skipTest("这台机器上 explorer 没有启动它，无法验证这条通路")
        os.remove(marker)

        ok2, msg2 = core.start_via_shell(script, "--some-arg")
        self.assertFalse(ok2, "带参数时必须如实拒绝，不能偷偷启动一个没带参数的程序")
        self.assertIn("参数", msg2)
        time.sleep(1.0)
        self.assertFalse(os.path.exists(marker),
                         "带参数时竟然还是启动了（参数被丢掉却报成功）")

    def test_launch_item_with_args_does_not_claim_the_shell_fallback(self):
        """`launch_item(..., always_shell=True)` + 参数：只能如实说做不到。"""
        item = core.ProgramItem(name="参数测试", target=sys.executable)
        ok, msg = core.launch_item(item, extra_args="--x", always_shell=True)
        self.assertFalse(ok, f"不该假报成功：{msg}")
        self.assertIn("参数", msg)

    # ---- C.1：单实例 ----

    def test_single_instance_blocks_the_second_claim(self):
        name = "Local\\WinMon-test-" + str(os.getpid())
        ok1, _m1 = core.claim_single_instance(name)
        self.assertTrue(ok1, "第一次应当抢到")
        other = (
            "import sys; sys.path.insert(0, r'%s'); import winmon_core as core;"
            "print(core.claim_single_instance(r'%s'))" % (h.PROJECT_DIR, name)
        )
        import subprocess
        got = subprocess.run([sys.executable, "-c", other], capture_output=True,
                             timeout=30)
        text = (got.stdout or b"").decode("utf-8", "replace")
        self.assertIn("False", text, f"另一个进程不该抢到：{text!r}")
        core.release_single_instance()
        got2 = subprocess.run([sys.executable, "-c", other], capture_output=True,
                              timeout=30)
        self.assertIn("True", (got2.stdout or b"").decode("utf-8", "replace"),
                      "释放之后应当能抢到")

    # ---- C.2：旧配置迁移 ----

    def test_migration_removes_dead_keys_and_normalises_values(self):
        old = {"video_close_mode": "暂停并返回", "video_tabs": 3, "video_only_apps": True,
               "history_days": "很久", "close_mode": 123, "workspaces": "不是列表",
               "gallery_order": None, "keep_me": 7}
        new, changes = core.migrate_settings(old)
        self.assertNotIn("video_tabs", new)
        self.assertNotIn("video_only_apps", new)
        self.assertEqual(new["video_close_mode"], "smart")
        self.assertEqual(new["history_days"], 30)
        self.assertEqual(new["close_mode"], "exit")
        self.assertEqual(new["workspaces"], [])
        self.assertEqual(new["gallery_order"], [])
        self.assertEqual(new["keep_me"], 7, "不认识的键要原样留着，别乱删")
        self.assertEqual(new["settings_version"], core.SETTINGS_VERSION)
        self.assertGreaterEqual(len(changes), 4)
        self.assertEqual(old["video_close_mode"], "暂停并返回", "不能改调用方的字典")

    def test_migration_is_idempotent(self):
        new, _c = core.migrate_settings({"video_close_mode": "smart"})
        again, changes2 = core.migrate_settings(new)
        self.assertEqual(changes2, [], "迁移过的设置再迁一次应当没有任何改动")
        self.assertEqual(again, new)

    def test_migration_survives_garbage(self):
        new, changes = core.migrate_settings("根本不是字典")
        self.assertEqual(new, {})
        self.assertTrue(changes)


class TestCliEntryPoints(unittest.TestCase):
    """
    命令行入口的边界（当时实测抓到的真 bug）：

      · `--help` / `-h` / `/?` **根本没实现** → 以前会掉进"起界面"，看着像卡死；
      · `--unknown-flag` 这种认不出来的参数**静默起界面**（实测跑一次参数测试
        就多出两个 WinMon 实例）；
      · `--category` / `--idle-minutes` 这类"修饰性"选项**单独用不生效**
        （条件写的是 `if "--catalog" in args`，只给 `--category` 就掉进界面了）；
      · `--idle-minutes -5` 的 `-5` 被当成未知开关。

    这些都用**真子进程**跑一遍（`--help` 之类只看行为，不依赖界面）。
    """

    def _run(self, args, timeout=60):
        import subprocess
        import winmon
        env = dict(os.environ)
        # 让子进程按 UTF-8 输出 —— 否则中文在 Windows 控制台（GBK）上会变成乱码，
        # 而"用法/类别名"这些恰恰全是中文（当时实测踩到）。
        env["PYTHONIOENCODING"] = "utf-8"
        proc = subprocess.run(
            [sys.executable, os.path.join(h.PROJECT_DIR, "winmon.py")] + list(args),
            capture_output=True, timeout=timeout,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            cwd=h.PROJECT_DIR, env=env)
        out = (proc.stdout or b"").decode("utf-8", "replace")
        err = (proc.stderr or b"").decode("utf-8", "replace")
        return proc.returncode, out, err, winmon

    def test_help_prints_usage_and_exits(self):
        for flag in ("--help", "-h", "/?"):
            code, out, _err, _w = self._run([flag], timeout=30)
            self.assertEqual(code, 0, f"{flag} 应当正常退出")
            self.assertIn("用法", out)
            self.assertIn("--json", out)

    def test_usage_text_lists_every_supported_flag(self):
        """用法里必须把实际支持的选项都写上（否则用户只能猜）。"""
        code, out, _err, winmon = self._run(["--help"], timeout=30)
        self.assertEqual(code, 0)
        for flag in ("--json", "--all", "--catalog", "--find", "--open", "--clean",
                     "--idle", "--selftest", "--smoke", "--version"):
            self.assertIn(flag, out, f"用法里漏了 {flag}")
        # 用法里提到的选项必须真的在 main() 的已知集合里（避免文档与实现脱节）
        for flag in ("--json", "--clean", "--idle"):
            self.assertIn(flag, winmon.USAGE_TEXT)

    def test_unknown_flag_is_rejected_instead_of_starting_the_gui(self):
        code, _out, err, _w = self._run(["--unknown-flag"], timeout=30)
        self.assertEqual(code, 2, "认不出来的参数应当报错退出，不能静默起界面")
        self.assertIn("--unknown-flag", err)
        self.assertIn("--help", err, "要告诉用户去哪儿看说明")

    def test_negative_number_is_a_value_not_a_flag(self):
        """`--idle-minutes -5` 里的 `-5` 是值，不是未知开关。"""
        code, out, err, _w = self._run(["--idle-minutes", "-5"], timeout=60)
        self.assertNotEqual(code, 2, f"负数被当成未知参数了：{err!r}")
        self.assertIn("作用范围", out)

    def test_modifier_options_work_on_their_own(self):
        """`--category` / `--idle-minutes` 这类修饰选项单独用也要生效。"""
        code, out, _err, _w = self._run(["--category", "影音播放"], timeout=60)
        self.assertEqual(code, 0, out)
        self.assertIn("影音播放", out)

    # ---- 漏值：必须报错，不能静默按默认值跑（2026-10-13 修的真 bug）----

    def test_category_without_a_value_is_an_error(self):
        """
        `--category` 后面忘了给值：以前会**打印整个目录并退出 0**（看起来像成功了）。

        与"认不出来的参数要报错"同一条道理：不能替用户把意图改掉还不说。
        """
        code, out, err, _w = self._run(["--category"], timeout=60)
        self.assertEqual(code, 2, f"漏值应当报错退出，实际 {code}；out={out[:200]!r}")
        self.assertIn("--category", err)
        self.assertIn("--help", err)

    def test_category_followed_by_another_flag_is_an_error(self):
        code, _out, err, _w = self._run(["--category", "--admin"], timeout=60)
        self.assertEqual(code, 2, f"`--category --admin` 等于漏值，应当报错：{err!r}")

    def test_equals_form_reaches_its_branch(self):
        """
        `--flag=值` 是 `_arg_value()` 一直支持的写法，但**分支判断**只查裸开关 ——
        于是 `--category=影音播放` 既不走目录分支、也不算未知参数，一路掉到"起界面"。
        这里钉住两种写法等价。
        """
        code, out, err, _w = self._run(["--category=影音播放"], timeout=60)
        self.assertEqual(code, 0, f"`--category=…` 没走目录分支（退出码 {code}）：{err!r}")
        self.assertIn("影音播放", out)

    def test_equals_form_of_idle_minutes_works(self):
        code, out, err, _w = self._run(["--idle-minutes=1"], timeout=90)
        self.assertEqual(code, 0, f"`--idle-minutes=1` 没走闲置分支：{err!r}")
        self.assertIn("作用范围", out)

    def test_non_numeric_idle_minutes_is_an_error(self):
        """`--idle-minutes abc` 以前按 30 分钟静默执行 —— 那是替用户改了阈值。"""
        code, out, err, _w = self._run(["--idle", "--idle-minutes", "abc"], timeout=60)
        self.assertEqual(code, 2, f"非数字应当报错退出，实际 {code}；out={out[:200]!r}")
        self.assertIn("--idle-minutes", err)

    def test_empty_equals_value_is_an_error(self):
        code, _out, err, _w = self._run(["--category="], timeout=60)
        self.assertEqual(code, 2, f"`--category=` 等于漏值：{err!r}")


class TestBrokenSettingsFile(unittest.TestCase):
    """
    设置文件坏了的时候：**不能静默用默认值跑完、再默默覆盖掉它**。

    2026-10-13 修掉的真 bug：`load_settings()` 整个被一个裸 `except: pass` 包着，
    于是一份手改漏了逗号的 `winmon_settings.json` 会让**所有**设置
    （收藏 / 最近使用 / 工作区快照 / 所有勾选项）悄悄回到默认值 ——
    而退出时的自动保存又把这些默认值写回同一个文件，用户数据就这么没了，
    全程没有任何提示（同一个文件里"保存失败"那条路却是会报的，自相矛盾）。
    """

    def _app(self, path: str):
        """只装 `load_settings` 在这个场景里真正会用到的东西。"""
        app = winmon.WinMonApp.__new__(winmon.WinMonApp)
        app._settings_path = path
        app._settings_load_error = ""
        app._settings_backup = ""
        return app

    def test_broken_json_is_backed_up_and_reported(self):
        tmp = tempfile.mkdtemp(prefix="wm-badsettings-")
        self.addCleanup(lambda: __import__("shutil").rmtree(tmp, ignore_errors=True))
        path = os.path.join(tmp, "winmon_settings.json")
        original = '{"show_hidden": true, "program_prefs": {  <- 少了一个大括号\n'
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(original)

        app = self._app(path)
        app.load_settings()                       # 绝不能抛（设置坏了也要能启动）

        self.assertTrue(app._settings_load_error, "读不出来必须记下原因，不许静默")
        backup = path + ".bad"
        self.assertTrue(os.path.exists(backup),
                        "覆盖之前必须先把原文件备份一份，否则用户数据永久丢失")
        with open(backup, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), original, "备份必须是原文件的内容")
        text = app._startup_status()
        self.assertIn("读不出来", text)
        self.assertIn(os.path.basename(backup), text, "要告诉用户备份在哪")

    def test_backup_does_not_clobber_an_earlier_one(self):
        """已经有一份 `.bad` 时不许覆盖它（那可能是更早、更完整的一份）。"""
        tmp = tempfile.mkdtemp(prefix="wm-badsettings2-")
        self.addCleanup(lambda: __import__("shutil").rmtree(tmp, ignore_errors=True))
        path = os.path.join(tmp, "winmon_settings.json")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("{坏")
        backup = path + ".bad"
        with open(backup, "w", encoding="utf-8") as fh:
            fh.write("更早的那份")
        app = self._app(path)
        app.load_settings()
        with open(backup, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "更早的那份")

    def test_missing_file_is_not_an_error(self):
        """文件根本不存在是**正常情况**（第一次运行），不该报错也不该建备份。"""
        tmp = tempfile.mkdtemp(prefix="wm-nosettings-")
        self.addCleanup(lambda: __import__("shutil").rmtree(tmp, ignore_errors=True))
        path = os.path.join(tmp, "never_written.json")
        app = self._app(path)
        app.load_settings()
        self.assertEqual(app._settings_load_error, "")
        self.assertFalse(os.path.exists(path + ".bad"))
        self.assertNotIn("读不出来", app._startup_status())


class TestMediaLineFits(unittest.TestCase):
    """
    「正在播放」那一行：**装不下要用 `…` 明说**，不能静默切断。

    2026-10-13 修的真问题：那个标签是定宽的（不定宽就会把右边的按钮挤坏），
    实测 300px 的宽度会把 `网易云音乐：…（从窗口标题读的）` 的**来源标注**切掉、
    把多会话那句"还有 N 个在放（…）"整句吞掉 —— 用户以为后面没有内容。
    """

    def test_short_text_is_untouched(self):
        self.assertEqual(winmon.fit_media_text("短标题", 64), "短标题")
        self.assertEqual(winmon.fit_media_text("", 64), "")

    def test_chinese_counts_as_two_units(self):
        """中文按 2 个单位算 —— 与定宽标签的算法一致，否则仍会溢出。"""
        # 预算 8：给 `…` 留一位 → 只能装 3 个汉字（6 单位）+ `…`
        got = winmon.fit_media_text("一二三四五", 8)
        self.assertTrue(got.endswith("…"), got)
        self.assertEqual(got, "一二三…")

    def test_no_ellipsis_when_it_fits(self):
        text = "Microsoft Edge：有我呢 · 郭一凡 · 正在播放"
        self.assertEqual(winmon.fit_media_text(text, 64), text)

    def test_long_text_keeps_the_multi_session_hint(self):
        """
        真实长文案：开头是曲名、**"还有 N 个在放"在中间** ——
        裁完必须还看得见"还有…个在放"（这正是以前被整句吞掉的信息）。
        """
        text = ("Microsoft Edge：有我呢 · 郭一凡 · 正在播放"
                "　｜　还有 2 个在放（网易云音乐：us feat. ゆきな - 澤田空海理）")
        got = winmon.fit_media_text(text, 64)
        self.assertTrue(got.endswith("…"))
        self.assertIn("还有 2 个在放", got)

    def test_wide_text_stays_within_the_budget(self):
        """裁完的宽度（中文=2、西文=1）不许超过预算 —— 否则定宽标签还是会切。"""
        text = "网易云音乐：us feat. ゆきな - 澤田空海理（从窗口标题读的）" * 3
        got = winmon.fit_media_text(text, 40)
        used = sum(2 if ord(ch) > 0x2E80 else 1 for ch in got)
        self.assertLessEqual(used, 40, f"{got!r} 超宽了")

    def test_window_uses_it_when_writing_the_line(self):
        """界面写这一行时必须过这道裁切（否则等于没修）。"""
        app = winmon.WinMonApp.__new__(winmon.WinMonApp)
        app.media_text = _FakeVar()
        app.MEDIA_TEXT_WIDTH = 20
        app._set_media_line("网易云音乐：一二三四五六七八九十" * 3)
        self.assertTrue(app.media_text.get().endswith("…"))


class TestWorkerNeverTouchesTk(unittest.TestCase):
    """
    工作线程把界面动作交回主线程时**不许直接调 tkinter**。

    2026-10-13 修的真缺陷：目录构建 / 视频探测 / 播放检测三个后台线程原来直接
    `root.after(0, …)`，主线程不在 mainloop 里派发时会抛
    `RuntimeError: main thread is not in main loop` —— 异常在工作线程里，
    只会把它静默弄死（实测：程序目录面板永远停在"正在扫描程序目录…"，
    没有任何提示）。现在统一走 `app._ui_call()` 队列，由主线程的定时泵取走。
    """

    def _app(self):
        import queue
        app = winmon.WinMonApp.__new__(winmon.WinMonApp)
        app._ui_msg = queue.Queue()
        app.status_text = _FakeVar()
        app._set_status = lambda t: app.status_text.set(t)

        class _Root:
            """替身 root：**任何 after 调用都判失败**（工作线程不许碰 tkinter）。"""
            def after(self, *_a, **_k):
                raise AssertionError("工作线程直接调用了 tkinter 的 after()")

            def after_cancel(self, *_a):
                pass
        app.root = _Root()
        return app

    def test_call_from_a_worker_is_queued_not_run_inline(self):
        app = self._app()
        import threading
        done = threading.Event()

        def worker():
            app._ui_call(lambda: app.status_text.set("来自工作线程"))
            done.set()
        threading.Thread(target=worker, daemon=True).start()
        self.assertTrue(done.wait(3), "工作线程没跑起来")
        self.assertEqual(app._ui_msg.qsize(), 1, "动作应当排队等主线程")
        self.assertEqual(app.status_text.get(), "", "工作线程不许直接改界面")

        app._drain_ui_msgs()                       # 主线程的定时泵做的事
        self.assertEqual(app.status_text.get(), "来自工作线程")
        self.assertEqual(app._ui_msg.qsize(), 0)

    def test_call_from_the_main_thread_runs_inline(self):
        """主线程里调用就直接执行（不必绕一圈队列，少一拍延迟）。"""
        app = self._app()
        app._ui_call(lambda: app.status_text.set("主线程"))
        self.assertEqual(app.status_text.get(), "主线程")
        self.assertEqual(app._ui_msg.qsize(), 0)

    def test_a_failing_callback_does_not_kill_the_pump(self):
        """一个回调抛异常不能把定时泵带走（项目里为队列定的同一条规矩）。"""
        app = self._app()
        # 直接入队 = 来自工作线程的那条路（主线程里 `_ui_call` 是直接执行的，
        # 那种情况下异常就该让调用者看见）
        app._ui_msg.put(lambda: (_ for _ in ()).throw(RuntimeError("坏回调")))
        app._ui_msg.put(lambda: app.status_text.set("后面的还在"))
        app._drain_ui_msgs()
        self.assertEqual(app.status_text.get(), "后面的还在")

    def test_the_three_workers_no_longer_call_root_after(self):
        """源码层面钉住：那几个后台线程里不许再出现 `root.after(0, …)`。"""
        import ast
        path = os.path.join(h.PROJECT_DIR, "winmon.py")
        tree = ast.parse(open(path, encoding="utf-8").read())
        offenders = []
        for fn in ast.walk(tree):
            if not isinstance(fn, ast.FunctionDef) or fn.name != "worker":
                continue
            for node in ast.walk(fn):
                if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                        and node.func.attr == "after"):
                    offenders.append(node.lineno)
        self.assertEqual(offenders, [],
                         f"这些 worker 还在直接调 tkinter 的 after()：{offenders}")


class TestNoThirdPartyStaysTrue(unittest.TestCase):
    """本轮新增的 UIA 代码不能引入第三方依赖。"""

    def test_uia_uses_system_dll_only(self):
        src = open(os.path.join(h.PROJECT_DIR, "winmon_core.py"),
                   encoding="utf-8").read()
        self.assertIn('WinDLL("uiautomationcore")', src,
                      "UIA 应当走系统自带的 uiautomationcore.dll")

    def test_no_third_party_imports_in_uia_code(self):
        """
        用 AST 检查**真正的 import 语句**，而不是搜字符串。

        踩过的坑：第一版直接 `assertNotIn("pywinauto", src)`，
        结果被注释里那句"不引入 comtypes/pywinauto/uiautomation 等第三方库"绊倒 ——
        文档里提到某个库名，不代表代码 import 了它。
        """
        import ast
        path = os.path.join(h.PROJECT_DIR, "winmon_core.py")
        tree = ast.parse(open(path, encoding="utf-8").read())
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    imported.add(alias.name.split(".")[0])
            elif isinstance(node, ast.ImportFrom):
                if node.level == 0 and node.module:
                    imported.add(node.module.split(".")[0])
        banned = {"comtypes", "pywinauto", "uiautomation", "pyautogui",
                  "pywin32", "win32com"}
        self.assertEqual(imported & banned, set(),
                         f"核心层 import 了第三方库：{imported & banned}")


class TestMediaAppPin(unittest.TestCase):
    """
    「媒体应用」下拉框：**同一个程序只列一条** + **用户选的那个不会自己变回「自动」**。

    这两条都是 2026-10-13 复现出来的真 bug（都属 HANDOFF 坑 67 那一类：
    "设置项/界面看着有，实际没接进代码路径"）：

      ① 标签里带着**会变的后缀**（「（运行中）」取决于此刻在不在跑），
         而界面原来拿 `pin not in labels` 直接比字符串 → 用户选的 Edge
         （选的时候在跑，存的是带「（运行中）」的那条）在下次启动时对不上，
         **静默退回「自动」**，"按应用发命令"跟着失效；
      ② 候选表与开始菜单会撞车（实测 Edge 列了两次），而去重用的是
         `MediaApp.key`（exe 名）与菜单项的显示名 —— 两种东西不同源，永远去不掉。
    """

    def _menu_app(self, label, browser=False):
        """开始菜单条目：只有 .lnk，`exe` 是空串（与 `installed_media_apps` 一致）。"""
        return core.MediaApp(label, "", browser, r"C:\开始菜单\%s.lnk" % label)

    # ---- ① 选择必须活过"运行状态变了" ----

    def test_choice_survives_the_running_suffix_disappearing(self):
        """选的时候 Edge 在跑、下次启动时没跑 —— 选择必须保住（真 bug 的回归判据）。"""
        saved = "Microsoft Edge（浏览器）（运行中）"
        labels = [winmon.MEDIA_APP_ANY, "Microsoft Edge（浏览器）（菜单）",
                  "网易云音乐（播放器）（菜单）"]
        resolved, dropped = core.resolve_media_pin(saved, labels, winmon.MEDIA_APP_ANY)
        self.assertEqual(resolved, "Microsoft Edge（浏览器）（菜单）",
                         "应当换回当前清单里的那条**真实标签**，而不是退回「自动」")
        self.assertEqual(dropped, [])

    def test_choice_survives_the_running_suffix_appearing(self):
        """反向也一样：选的时候没跑、下次启动时在跑。"""
        saved = "Microsoft Edge（浏览器）（菜单）"
        labels = [winmon.MEDIA_APP_ANY, "Microsoft Edge（浏览器）（运行中）"]
        resolved, _dropped = core.resolve_media_pin(saved, labels, winmon.MEDIA_APP_ANY)
        self.assertEqual(resolved, "Microsoft Edge（浏览器）（运行中）")

    def test_resolved_label_is_one_that_the_lookup_can_find(self):
        """
        换回来的必须是**清单里真实存在的标签** —— `_picked_media_app()` /
        `_app_key_for_media()` 是拿标签查 `self._media_apps` 字典的，
        写成别的形式反而查不到（那就等于没选中）。
        """
        saved = "网易云音乐（播放器）（运行中）"
        labels, mapping = winmon.build_media_app_entries(
            running={"cloudmusic.exe"},
            available=lambda exe: exe == "cloudmusic.exe",
            found=[self._menu_app("网易云音乐")])
        resolved, _d = core.resolve_media_pin(saved, labels, winmon.MEDIA_APP_ANY)
        self.assertIn(resolved, labels)
        self.assertIsNotNone(mapping.get(resolved), "解析出来的标签要能查到 MediaApp")
        self.assertEqual(mapping[resolved].exe, "cloudmusic.exe")

    def test_multi_select_keeps_what_is_still_there(self):
        """多选：丢掉真的没有的那个，**保住还在的那个**（不连坐）。"""
        saved = "网易云音乐（播放器）（菜单）、早就卸了的播放器（播放器）（菜单）"
        labels = [winmon.MEDIA_APP_ANY, "网易云音乐（播放器）（菜单）"]
        resolved, dropped = core.resolve_media_pin(saved, labels, winmon.MEDIA_APP_ANY)
        self.assertEqual(resolved, "网易云音乐（播放器）（菜单）")
        self.assertEqual(dropped, ["早就卸了的播放器（播放器）（菜单）"])

    def test_unknown_app_still_falls_back_to_auto(self):
        """真找不到（程序卸了）才退回「自动」—— 与以前的行为一致。"""
        resolved, dropped = core.resolve_media_pin(
            "早就卸了的播放器（播放器）（菜单）",
            [winmon.MEDIA_APP_ANY, "PotPlayer 64 bit（播放器）（菜单）"],
            winmon.MEDIA_APP_ANY)
        self.assertEqual(resolved, winmon.MEDIA_APP_ANY)
        self.assertEqual(len(dropped), 1)

    def test_auto_label_is_kept_as_is(self):
        resolved, dropped = core.resolve_media_pin(winmon.MEDIA_APP_ANY,
                                                   [winmon.MEDIA_APP_ANY],
                                                   winmon.MEDIA_APP_ANY)
        self.assertEqual(resolved, winmon.MEDIA_APP_ANY)
        self.assertEqual(dropped, [])

    # ---- 主干（去掉会变的后缀）本身 ----

    def test_stem_only_strips_known_suffixes(self):
        self.assertEqual(core.media_app_stem("Microsoft Edge（浏览器）（运行中）"),
                         "microsoft edge")
        self.assertEqual(core.media_app_stem("哔哩哔哩（播放器）（菜单）"), "哔哩哔哩")
        self.assertEqual(core.media_app_stem("PotPlayer 64 bit（播放器）"),
                         "potplayer 64 bit")
        # 名字里"恰好含"这些词的不许被误伤（只吃结尾的已知后缀）
        self.assertEqual(core.media_app_stem("我的浏览器"), "我的浏览器")
        self.assertEqual(core.media_app_stem("（播放器）"), "")

    # ---- ② 清单：去重 + 给菜单条目补 exe ----

    def test_same_app_is_listed_once_and_the_exe_bearing_one_wins(self):
        """候选表与开始菜单撞车时只留一条，且留**带 exe** 的那条。"""
        labels, mapping = winmon.build_media_app_entries(
            running={"msedge.exe"},
            available=lambda exe: exe == "msedge.exe",
            found=[self._menu_app("Microsoft Edge", browser=True)])
        edge = [l for l in labels if "Edge" in l]
        self.assertEqual(len(edge), 1, f"同一个程序列了多条：{edge}")
        self.assertEqual(mapping[edge[0]].exe, "msedge.exe",
                         "留下的必须是带 exe 的那条（否则按应用发命令发不出去）")

    def test_menu_entry_inherits_the_exe_name_from_the_candidate_table(self):
        """
        开始菜单条目自己没有 exe —— 名字能对上候选表就补上。

        不补的话 `_app_key_for_media()` 是空串，命令会发给"第一个会话"：
        用户看到的是"我选了 A、控的却是 B"（项目里最忌讳的假成功）。
        """
        labels, mapping = winmon.build_media_app_entries(
            running=set(), available=lambda exe: False,
            found=[self._menu_app("哔哩哔哩")])
        hit = [l for l in labels if "哔哩哔哩" in l]
        self.assertEqual(len(hit), 1)
        self.assertEqual(mapping[hit[0]].exe, "哔哩哔哩.exe")

    def test_unknown_menu_app_is_kept_without_an_exe(self):
        """候选表里没有的程序照样要能选（只是拿不到 exe，如实留空）。"""
        labels, mapping = winmon.build_media_app_entries(
            running=set(), available=lambda exe: False,
            found=[self._menu_app("某冷门播放器")])
        hit = [l for l in labels if "某冷门播放器" in l]
        self.assertEqual(len(hit), 1)
        self.assertEqual(mapping[hit[0]].exe, "")

    def test_not_installed_and_not_running_candidates_are_hidden(self):
        labels, _mapping = winmon.build_media_app_entries(
            running=set(), available=lambda exe: False, found=[])
        self.assertEqual(labels, [winmon.MEDIA_APP_ANY])

    def test_no_two_entries_share_a_stem(self):
        """
        整个清单里**任何两条的主干都不能相同** —— 这是"重复条目"的通用判据，
        以后加候选/加来源也不会再冒出一个重复项。
        """
        labels, _mapping = winmon.build_media_app_entries(
            running={"msedge.exe", "cloudmusic.exe"},
            available=lambda exe: exe in ("msedge.exe", "cloudmusic.exe"),
            found=[self._menu_app("Microsoft Edge", browser=True),
                   self._menu_app("网易云音乐"),
                   self._menu_app("某冷门播放器")])
        stems = [core.media_app_stem(l) for l in labels
                 if l != winmon.MEDIA_APP_ANY]
        self.assertEqual(len(stems), len(set(stems)), f"有重复条目：{labels}")

    # ---- 启动那句状态：选择被丢掉时要**说出来**（不静默）----

    def test_startup_status_reports_a_dropped_choice(self):
        app = winmon.WinMonApp.__new__(winmon.WinMonApp)
        app._media_pin_dropped = ["早就卸了的播放器（播放器）（菜单）"]
        text = app._startup_status()
        self.assertIn("早就卸了的播放器", text)
        self.assertIn("自动", text)

    def test_startup_status_is_plain_when_nothing_was_dropped(self):
        app = winmon.WinMonApp.__new__(winmon.WinMonApp)
        app._media_pin_dropped = []
        self.assertNotIn("没找到", app._startup_status())


if __name__ == "__main__":
    unittest.main(verbosity=2)
