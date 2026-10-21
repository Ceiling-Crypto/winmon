# -*- coding: utf-8 -*-
"""
B11 —— 外部复查报告 76 条修改的**回归钉**（`test_b11`）。

这一批测什么
------------
2026-10-16 把一份外部复查报告的 76 条修改落进了源项目
（`winmon_core.py` / `winmon.py` / `media_session.ps1`）。这个文件专门钉住其中
**最容易"改着改着又滑回去"**的那些，按报告编号组织（P0-2 / P1-1 …）：

  · **P0-2**：主窗口必须绑 `WM_DELETE_WINDOW`，否则点 × 走 Tk 默认 destroy，
    `on_close`/`quit_app` 永不执行 —— 托盘图标残留、常驻 PowerShell 助手成孤儿、
    退出前的设置改动丢失。所以既要**源码级**钉"绑定那一行真的在 `__init__` 里"，
    也要**行为级**钉"点 × 真的走到退出流程"。
  · **P1-1 / P1-2 / P1-3**：媒体命令、停止视频、批量启动以前都在 Tk 主线程里跑
    阻塞调用（`subprocess.run(timeout=8)`、最长 4 秒轮询、`launch_item` 的
    `_wait_running`），界面上表现为"点一下卡好几秒"。这几条统一用
    **"执行 core 的线程 id ≠ 调用者线程 id"** 来钉 —— 这是唯一能证明
    "真的挪到后台线程了"的判据；只看"函数返回了"是钉不住的（阻塞调用也会返回）。
  · **P1-4**：`media_session.ps1` 升到 v2（原子写快照）。除了标记与 `File.Replace`，
    还钉**纯 ASCII**：PowerShell 5.1 读**无 BOM 的非 ASCII** .ps1 会按 ANSI 解码，
    中文注释当场把语法搞坏（这条踩过）。
  · **P1-5**：常驻媒体助手的看门狗。助手 -Watch 到期/崩溃后快照会过期，
    `_periodic_refresh` 里要按 30 秒节流把它拉起来；读得到新鲜快照时**不能**重复启动
    （否则堆一串 PowerShell）。
  · **P2**：一批纯逻辑修复（`media_app_stems_match` 精确优先且包含需 ≥5 字符、
    `media_session_summary` 认 `app_key`、`idle_samples` 不再二次枚举、
    `resolve_shortcuts` 对 `null` 输出退化、`filter_non_programs` 仍滤文档、
    托盘隐藏时不再取帧、私有内存分档查询）。
  · **P3**：死代码删除的守门员（防止"顺手加回来"），以及 `action_clean_files`
    区分「取消」与「全不勾」。

为什么这么测（本项目的取舍）
----------------------------
1. **能不开窗口就不开窗口**。绝大多数用例用
   `WinMonApp.__new__(WinMonApp)` 跳过 `__init__`，再手工把要用的属性装齐；
   假 `root` 只记录 `after` 回调、由测试**手动派发**，这样"后台化"这件事可以在
   毫秒级、无图形会话的环境里被真验证。
2. **不真起程序、不真关窗口、不真结束进程**。核心层调用一律
   `mock.patch.object(core, ...)` 拦下来并**记录线程 id** —— 测试要证明的是
   "调用发生在哪个线程"，不是"那个调用真的干了什么"。
3. **每条断言都指向一个能复现的失效模式**（线程 id 相等 / 助手被重复启动 /
   脚本里出现非 ASCII / 少了 `WM_DELETE_WINDOW` 绑定 …），
   而不是"函数返回了 True"。本项目最恨假成功，所以没有 `assertTrue(True)` 之类的填充。
4. 少数几条（`ConfirmDialog` 的倒计时残留）**必须**真 Tk 才能构造实例，
   按项目约定加 `@h.needs_tk` 并在 `setUp` 里建根窗口 + `addCleanup` 收干净。
"""
from __future__ import annotations

import inspect
import json
import os
import queue
import re
import sys
import threading
import time
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import _helpers as h                                          # noqa: E402
import winmon_core as core                                    # noqa: E402
import winmon                                                 # noqa: E402

# ---------------------------------------------------------------------------
# 小替身（整个文件共用）
# ---------------------------------------------------------------------------

#: 非程序（文档 / 图片 / 压缩包 …）扩展名清单。
#:
#: 为什么**内联在这里**而不是读 `core.CATALOG_NON_PROGRAM_EXTS`：
#: 那个常量在 2026-10-16 的复查里被判定为死代码（没有任何地方读它）并删掉了。
#: 但"两份清单不能冲突"这条不变式本身仍然值钱 —— 一旦有人把 `.txt`/`.png`
#: 加进 `CATALOG_TARGET_EXTS`（等于把文档当程序列进程序目录），下面就会红。
NON_PROGRAM_EXTS = (".txt", ".pdf", ".chm", ".png", ".jpg", ".jpeg",
                    ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
                    ".zip", ".rar", ".7z", ".mp3", ".mp4", ".log")


class _Var:
    """最小化的 Tk 变量替身：被测代码只用到 `get()` / `set()`。"""

    def __init__(self, value):
        self._value = value

    def get(self):
        return self._value

    def set(self, value):
        self._value = value


class _Root:
    """
    假 Tk 根窗口：`after` **只记录不执行**（由测试手动派发那一拍）。

    这样"主线程里登记了一个回调、真正的阻塞调用在别的线程"就能被逐拍核对，
    而不是靠 `time.sleep` 猜。也不需要图形会话。
    """

    def __init__(self, state: str = "normal"):
        self._state = state
        self.after_calls: list[tuple] = []
        self._seq = 0

    def after(self, delay, fn=None, *args):
        self.after_calls.append((delay, fn, args))
        self._seq += 1
        return f"after#{self._seq}"

    def state(self):
        return self._state

    # 托盘恢复要用的三个（真 Tk 里都有，这里什么都不做）
    def deiconify(self):
        pass

    def lift(self):
        pass

    def focus_force(self):
        pass


class _StubWin:
    """窗口替身：`idle_samples` / `refresh_processes` / 总览只按属性读取。"""

    def __init__(self, hwnd=0, pid=0, title="", proc="", rect_w=640, rect_h=480,
                 iconic=False, is_hidden_surface=False, cls="", cloaked=False):
        self.hwnd = hwnd
        self.pid = pid
        self.title = title
        self.proc = proc
        self.rect_w = rect_w
        self.rect_h = rect_h
        self.iconic = iconic
        self.is_hidden_surface = is_hidden_surface
        self.cls = cls
        self.cloaked = cloaked


def _wait_for(cond, timeout: float = 3.0) -> bool:
    """轮询等条件成立（最多 3 秒）。绝不用固定 sleep 死等。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return True
        time.sleep(0.01)
    return False


def _pump_until(app, cond, timeout: float = 3.0) -> bool:
    """
    模拟 Tk 主线程的帧泵：反复 `_drain_ui_msgs()` 直到条件成立。

    为什么必须**反复**抽而不是抽一次：后台线程是"先放 A、再放 B"两条消息，
    抽一次可能只赶上第一条。这里每次抽完再判条件，条件成立才返回。
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app._drain_ui_msgs()
        if cond():
            return True
        time.sleep(0.01)
    return False


def _media_app() -> "winmon.WinMonApp":
    """
    装一个只够跑媒体命令的假 app（**不指定媒体应用** → 走"直接发命令"那条路）。

    刻意让 `media_app_pin` 是「自动」：这样 `_picked_media_app()` 返回 None，
    `media_command` 不会去 `core.snapshot_processes()` 探活、也不会真启动播放器 ——
    测试只关心"发命令这一步是不是挪到后台线程了"。
    """
    app = winmon.WinMonApp.__new__(winmon.WinMonApp)
    app.root = _Root()
    app._ui_msg = queue.Queue()
    app.media_app_pin = _Var(winmon.MEDIA_APP_ANY)
    app._media_apps = {}
    app._media_busy = False
    app.status = []
    app._set_status = app.status.append
    return app


# ===========================================================================
# 1. P0-2：点 × 真的走退出流程
# ===========================================================================
class TestP02CloseWindowBinding(unittest.TestCase):
    """`WM_DELETE_WINDOW` 这一绑定是"点 × 有没有走退出流程"的唯一开关。"""

    def test_source_binds_wm_delete_window_inside_init(self):
        """
        源码级：绑定必须出现在 `WinMonApp.__init__` 里。

        为什么非要限定在 `__init__`：以前有人把绑定挪进"托盘装好之后"的回调里，
        托盘不可用时那一支根本不执行 —— 表现就是"点了 × 窗口没了，但托盘图标
        和后台助手都还在"。绑定必须在**建窗口的那一段**、无条件执行。
        """
        init_src = inspect.getsource(winmon.WinMonApp.__init__)
        self.assertIn('root.protocol("WM_DELETE_WINDOW", self.on_close)', init_src,
                      "WinMonApp.__init__ 里没有把 WM_DELETE_WINDOW 绑到 on_close")

    def test_source_has_exactly_one_such_binding_in_winmon(self):
        """整个 winmon.py 里只有这一处 `WM_DELETE_WINDOW` 绑定（防重复/防挪走）。"""
        src = open(winmon.__file__, encoding="utf-8").read()
        hits = re.findall(r'protocol\(\s*"WM_DELETE_WINDOW"\s*,\s*self\.on_close\s*\)', src)
        self.assertEqual(len(hits), 1, f"self.on_close 的绑定出现了 {len(hits)} 次")

    def test_exit_mode_really_reaches_quit_app(self):
        """
        行为级：`close_mode` = 「结束进程」时调 `on_close` 必须走到 `quit_app`。

        这才是那条 bug 的真判据 —— 只断言"源码里有 protocol(...)"是钉不住的：
        改成 `root.protocol("WM_DELETE_WINDOW", lambda: None)` 一样能骗过源码断言，
        但点 × 会变成"什么都不发生"。
        """
        app = winmon.WinMonApp.__new__(winmon.WinMonApp)
        app.close_mode = _Var(winmon.CLOSE_MODE_EXIT)
        quit_calls = []
        app.quit_app = lambda: quit_calls.append(threading.get_ident())
        winmon.WinMonApp.on_close(app)
        self.assertEqual(len(quit_calls), 1, "点 × 没有走到退出流程（托盘/助手会成孤儿）")

    def test_tray_mode_falls_back_to_exit_when_tray_unavailable(self):
        """
        「隐藏到托盘」在托盘不可用时必须**退回直接退出**，并把原因写出来。

        不能因为"用户选了托盘"就卡在半路：窗口藏起来了、图标又没装，用户会以为
        程序已经退出，实际后台还在跑（这是比崩溃更难查的一种状态）。
        """
        app = winmon.WinMonApp.__new__(winmon.WinMonApp)
        app.close_mode = _Var(winmon.CLOSE_MODE_TRAY)
        status = []
        app._set_status = status.append
        app._hide_to_tray = lambda: (False, "托盘不可用")
        quit_calls = []
        app.quit_app = lambda: quit_calls.append(True)
        winmon.WinMonApp.on_close(app)
        self.assertEqual(len(quit_calls), 1, "托盘不可用时没有退回「结束进程」")
        self.assertIn("已改为直接退出", status[-1])


# ===========================================================================
# 2 + 3. P1-1：媒体命令不再占主线程
# ===========================================================================
class TestP11MediaCommandsAreBackground(unittest.TestCase):
    """
    `core.media_command` / `core.smart_media_command` 里都有 8 秒超时的
    `subprocess.run`。放在 Tk 回调里 = 点一次按钮冻 8 秒。

    **判据只有线程 id**：不管同步还是异步，函数都会"返回"；只有比对
    "谁调用了 core"才能区分两者。所以每条都用 `threading.get_ident()` 记录。
    """

    def test_media_command_runs_core_on_a_worker_thread(self):
        app = _media_app()
        main_tid = threading.get_ident()
        seen: dict = {}
        done = threading.Event()

        def fake_media_command(slug, app_id="", timeout=8.0):
            seen["tid"] = threading.get_ident()
            seen["slug"] = slug
            seen["app_id"] = app_id
            done.set()
            return (True, "已发送")

        applied: dict = {}
        app._after_media_command = (
            lambda ok, msg, slug, key, label:
            applied.update(tid=threading.get_ident(), ok=ok))

        with mock.patch.object(core, "media_command", side_effect=fake_media_command):
            app.media_command("play_pause")
            # 主线程这一段必须**只登记、不执行**：一个 after 回调，零阻塞调用
            self.assertTrue(app._media_busy, "主线程没有进入「忙碌」状态")
            self.assertEqual(len(app.root.after_calls), 1,
                             "主线程应该只登记一个 after 回调")
            delay, fn, _args = app.root.after_calls[0]
            self.assertGreaterEqual(delay, 1)
            fn()                                  # 模拟 Tk 主循环派发那一拍
            self.assertTrue(done.wait(3.0), "后台线程始终没调 core.media_command")

        self.assertNotEqual(seen["tid"], main_tid,
                            "core.media_command 仍在主线程跑（界面会冻 8 秒）")
        self.assertEqual(seen["slug"], "play_pause")
        self.assertEqual(seen["app_id"], "", "没指定应用时不该编造 app_key")

        # 结果必须**经队列回到主线程**（工作线程不许直接碰 tkinter）
        self.assertTrue(_pump_until(app, lambda: "tid" in applied),
                        "后台结果没有回到主线程")
        self.assertEqual(applied["tid"], main_tid)
        self.assertTrue(applied["ok"])

    def test_media_prev_next_writes_status_now_and_runs_core_in_background(self):
        """
        `media_prev_next`（◀◀ / ▶▶）：状态栏那句必须**在调用线程里立刻写**，
        真正发命令的 `core.smart_media_command` 必须换线程。

        为什么状态要立刻写：`smart_media_command` 会逐个会话试命令 + 回读核对
        （最坏几十秒）。要是连"正在发送…"都等后台回来才写，用户点完按钮就是
        "毫无反应"，会连点好几次。
        """
        app = _media_app()
        main_tid = threading.get_ident()
        status: list[tuple] = []
        app._set_status = lambda text: status.append((text, threading.get_ident()))
        app.log_action = lambda *a, **k: None
        app._refresh_media = lambda: None

        seen: dict = {}
        done = threading.Event()

        def fake_smart(*args, **kwargs):
            seen["tid"] = threading.get_ident()
            seen["args"] = args
            seen["kwargs"] = kwargs
            done.set()
            return (True, "已切到上一首")

        with mock.patch.object(core, "smart_media_command", side_effect=fake_smart):
            app.media_prev_next("previous")
            # 调用还没返回时状态就该写好了 —— 而且写在**主线程**
            self.assertEqual(len(status), 1, "「正在发送…」没有在调用线程里同步写出来")
            self.assertIn("正在发送", status[0][0])
            self.assertIn("上一首", status[0][0])
            self.assertEqual(status[0][1], main_tid, "状态栏被工作线程写了（tkinter 不安全）")
            self.assertTrue(done.wait(3.0), "后台线程始终没调 core.smart_media_command")

        self.assertNotEqual(seen["tid"], main_tid,
                            "core.smart_media_command 仍在主线程跑（界面会冻几十秒）")
        self.assertEqual(seen["args"][:1], ("prev",),
                         "「上一首」应该发 prev 而不是 next")

        self.assertTrue(_pump_until(app, lambda: app._media_busy is False),
                        "后台结果没有回到主线程（_media_busy 一直卡在 True）")
        self.assertIn("已切到上一首", status[-1][0])


# ===========================================================================
# 4. P1-2：停止视频不再占主线程
# ===========================================================================
class TestP11StopVideoIsBackground(unittest.TestCase):
    """`stop_video_in_window` 要轮询最长 4 秒、还可能升级为结束整棵进程树。"""

    def test_source_action_stop_video_delegates_to_the_background_helper(self):
        """
        源码级：`action_stop_video` 必须只做"挑窗口 + 调后台入口"。

        钉住"不再直接调 `core.stop_video_in_window`"：那一句只要回来，
        点「停止视频」就又会冻住界面。
        """
        src = inspect.getsource(winmon.WinMonApp.action_stop_video)
        self.assertIn("self._stop_video_in_background(", src,
                      "action_stop_video 没有走后台入口")
        self.assertNotIn("core.stop_video_in_window", src,
                         "action_stop_video 又在主线程里直接调核心层阻塞函数了")

    def test_stop_video_in_background_does_not_use_the_caller_thread(self):
        app = winmon.WinMonApp.__new__(winmon.WinMonApp)
        app._ui_msg = queue.Queue()
        status: list[tuple] = []
        app._set_status = lambda text: status.append((text, threading.get_ident()))
        logged = []
        app.log_action = lambda *a, **k: logged.append((a, k))
        app._log_target = winmon.WinMonApp._log_target.__get__(app, winmon.WinMonApp)

        wi = types.SimpleNamespace(real_name="播放器窗口", pid=4242,
                                   proc="chrome.exe", title="某个视频")
        main_tid = threading.get_ident()
        seen: dict = {}
        done = threading.Event()

        def fake_stop(wi_arg, mode="smart"):
            seen["tid"] = threading.get_ident()
            seen["mode"] = mode
            seen["name"] = wi_arg.real_name
            done.set()
            return (True, "已关闭窗口")

        with mock.patch.object(core, "stop_video_in_window", side_effect=fake_stop), \
                mock.patch.object(core, "format_open_time", return_value="2026-01-01 20:00"):
            winmon.WinMonApp._stop_video_in_background(app, wi, "window")
            self.assertEqual(status[0][1], main_tid,
                             "「正在执行…」应该由调用线程立刻写")
            self.assertIn("正在执行", status[0][0])
            self.assertIn("播放器窗口", status[0][0])
            self.assertTrue(done.wait(3.0), "后台线程始终没调 core.stop_video_in_window")

        self.assertNotEqual(seen["tid"], main_tid,
                            "core.stop_video_in_window 仍在主线程跑（界面会冻几秒）")
        self.assertEqual(seen["mode"], "window")
        self.assertEqual(seen["name"], "播放器窗口")

        self.assertTrue(_pump_until(app, lambda: bool(logged)),
                        "关闭结果没有回到主线程写操作历史")
        self.assertIn("已关闭窗口", status[-1][0])


# ===========================================================================
# 5. P1-3：批量启动后台化 + 收尾回主线程
# ===========================================================================
class TestP13LaunchSelected(unittest.TestCase):
    """
    `launch_item` 内含 `_wait_running` 轮询（默认 2s + explorer 兜底 4s），
    多选时线性叠加。以前全在主线程 —— 点一次「打开」界面能冻好几秒。
    """

    def test_source_launches_in_a_worker_and_finishes_via_ui_call(self):
        """
        源码级两条：
          ① 工作真的丢进 `threading.Thread`；
          ② 收尾走 `_ui_call(self._after_launch_all)` —— 收尾里有 `root.after`，
             必须在**主线程**执行（`_ui_call` 会按线程判断是直调还是入队）。
        """
        src = inspect.getsource(winmon.ProgramCatalogPane.launch_selected)
        self.assertIn("threading.Thread(", src, "launch_selected 没有起工作线程")
        self.assertIn("self.app._ui_call(self._after_launch_all)", src,
                      "批量启动的收尾没有经 _ui_call 交回主线程")
        self.assertNotIn("root.after", src,
                         "worker 里出现 root.after —— 工作线程碰 tkinter 是不安全的")
        self.assertTrue(hasattr(winmon.ProgramCatalogPane, "_after_launch_all"),
                        "收尾方法 _after_launch_all 不见了")
        after_src = inspect.getsource(winmon.ProgramCatalogPane._after_launch_all)
        self.assertIn("self.app.root.after(1500, self.apply_filter)", after_src)

    def test_launch_selected_records_use_and_logs_on_the_main_thread(self):
        """
        行为级：跑完 worker、泵一轮主线程后，必须**既**记了「最近使用」**也**写了日志。

        只测"`core.launch_item` 被调过"是不够的：这两个收尾动作分别在 `done()`
        和 `_after_launch_all()` 里，任何一处漏掉都是用户可见的功能缺失
        （"最近使用"列表不更新 / 操作历史里没有这次打开）。
        """
        app = winmon.WinMonApp.__new__(winmon.WinMonApp)
        app.root = _Root()
        app._ui_msg = queue.Queue()
        app._set_status = lambda text: None
        app.hwnd_of_pid = lambda pid: 0
        main_tid = threading.get_ident()

        logged: list[tuple] = []
        app.log_action = lambda action, target, result: logged.append(
            (action, target, result, threading.get_ident()))
        used: list[tuple] = []
        app.program_prefs = types.SimpleNamespace(
            record_use=lambda target, name: used.append((target, name)))
        saved: list[int] = []
        app._save_settings_soon = lambda: saved.append(threading.get_ident())

        pane = winmon.ProgramCatalogPane.__new__(winmon.ProgramCatalogPane)
        pane.app = app
        item = core.ProgramItem("测试记事本", r"C:\tools\notepad.exe", pid=0)
        pane._selected_items = lambda: [item]
        pane.apply_filter = lambda: None

        # 收尾方法的线程必须可核对：包一层记录线程 id，再调真身
        original_after = winmon.ProgramCatalogPane._after_launch_all
        after_tids: list[int] = []

        def spy_after(self_):
            after_tids.append(threading.get_ident())
            original_after(self_)

        pane._after_launch_all = spy_after.__get__(pane)

        with mock.patch.object(core, "launch_item",
                               return_value=(True, "已启动")) as m_launch:
            pane.launch_selected()
            self.assertTrue(_pump_until(app, lambda: bool(used) and bool(after_tids)),
                            "批量启动的收尾没有回到主线程执行")

        m_launch.assert_called_once()
        self.assertEqual(used, [(r"C:\tools\notepad.exe", "测试记事本")],
                         "成功启动后没有记「最近使用」")
        self.assertTrue(logged, "成功启动后没有写操作历史")
        self.assertEqual(logged[0][0], "打开程序")
        self.assertEqual(logged[0][2], "成功")
        self.assertEqual(logged[0][3], main_tid, "操作历史被工作线程写了")
        self.assertEqual(after_tids, [main_tid], "_after_launch_all 不在主线程跑")
        self.assertEqual(saved, [main_tid], "收尾没有立刻保存设置")


# ===========================================================================
# 6. P1-4：media_session.ps1 是 v2 且必须纯 ASCII
# ===========================================================================
class TestP14MediaHelperV2(unittest.TestCase):
    """桥接脚本的版本标记 / 原子写 / 编码 —— 三条都是"踩过坑"的判据。"""

    def setUp(self):
        self.path = core.media_session_helper_path()
        self.assertTrue(os.path.exists(self.path),
                        f"媒体桥接脚本不在：{self.path}")
        with open(self.path, "rb") as fh:
            self.raw = fh.read()
        self.text = self.raw.decode("utf-8")

    def test_marker_constant_matches_the_script(self):
        """
        标记常量存在、等于约定值，并且**真的写在脚本里**。

        这个标记的用途是 `--selftest` 区分「旧版脚本（该升级）」与
        「用户手工改过的脚本（绝不能动）」。两边一旦对不上，
        升级逻辑就会把用户改过的脚本当旧版覆盖掉。
        """
        self.assertTrue(hasattr(core, "_MEDIA_HELPER_MARKER"),
                        "core._MEDIA_HELPER_MARKER 不见了（升级判据没了）")
        self.assertEqual(core._MEDIA_HELPER_MARKER, "WinMon media helper v2")
        self.assertIn(core._MEDIA_HELPER_MARKER, self.text,
                      "脚本里没有版本标记，--selftest 会把它当成旧版")

    def test_command_uses_real_winrt_session_objects(self):
        """
        命令模式必须拿**真的 WinRT session 对象**去调 TryPlayAsync 等方法。

        2026-10-20 实测：`Read-Sessions` 只把 `[pscustomobject]` 数据行放进
        `$sessions`，命令循环却对数据行调 `TryPauseAsync()`，PowerShell 直接报
        “PSCustomObject 找不到方法”。表现就是：上一首/下一首还能靠媒体键兜底，
        但播放/暂停在有会话时也不生效。`$res.objects` 专门保存 COM 对象，
        绝不能混进要序列化的 `sessions`。
        """
        self.assertIn("$res.objects += $s", self.text)
        self.assertIn("foreach ($s in $objects)", self.text)
        self.assertNotIn("foreach ($s in $sessions)", self.text)
        self.assertIn("[int]$sessions[$i].status -eq 4", self.text,
                      "没有指定应用时应优先选正在播放的会话")
        self.assertIn("split '[,|]'", self.text,
                      "多选的应用键（逗号/竖线）要拆开逐个匹配")

    def test_snapshot_writes_are_atomic(self):
        """
        v2 的核心改动：先写 `$path.tmp` 再用 `File.Replace` 顶过去。

        为什么必须原子：Python 侧每 4 秒读一次"最后一行 JSON"，直接
        `WriteAllText` 时读到半个文件就会 JSON 解析失败 —— 表现是媒体行
        周期性闪回「没有程序在放东西」。
        """
        self.assertIn("[System.IO.File]::Replace($tmp, $path", self.text,
                      "快照写入不再是原子的（读端会看到半个文件）")

    def test_script_is_pure_ascii_without_bom(self):
        """
        **必须是纯 ASCII、且不带 BOM。**

        坑：PowerShell 5.1 读**没有 BOM**的 .ps1 时按系统 ANSI(GBK) 解码。
        脚本里只要有一个中文注释，那几个字节就会被解成乱码，严重时直接语法报错
        —— 而 Python 这边只会看到"助手起不来、快照永远过期"。
        所以这个脚本里的注释只能写英文。
        """
        bad = [i for i, byte in enumerate(self.raw) if byte > 127]
        self.assertEqual(
            bad, [],
            f"media_session.ps1 里有 {len(bad)} 个非 ASCII 字节"
            f"（首个在第 {bad[0] if bad else -1} 字节）——PS 5.1 会解析失败")
        self.assertNotEqual(self.raw[:3], b"\xef\xbb\xbf",
                            "脚本被加上了 UTF-8 BOM（项目约定不带 BOM）")


# ===========================================================================
# 7. P1-5：常驻媒体助手的看门狗
# ===========================================================================
class TestP15MediaHelperWatchdog(unittest.TestCase):
    """
    `_periodic_refresh` 里的看门狗：快照读不到 → 把常驻助手拉起来；
    但必须 30 秒才查一次、而且助手还活着时不许重复启动。
    """

    def _app(self):
        """装一个能跑 `_periodic_refresh` 的假 app：刷新动作全部换成空操作。"""
        app = winmon.WinMonApp.__new__(winmon.WinMonApp)
        app.root = _Root()
        app.notebook = types.SimpleNamespace(index=lambda which: 0)
        for name in ("refresh_processes", "refresh_windows", "_refresh_preview_header",
                     "_request_preview_frame", "_check_idle", "_refresh_media"):
            setattr(app, name, lambda *a, **k: None)
        app._media_last = time.monotonic()      # 别触发媒体轮询（已换成空操作）
        app._media_helper_check = 0.0           # "很久没查过"
        app.status = []
        app._set_status = app.status.append
        return app

    def _patches(self, snapshot, started):
        return (
            mock.patch.object(core, "read_media_snapshot", return_value=snapshot),
            mock.patch.object(core, "start_media_snapshot_helper",
                              side_effect=lambda *a, **k: started.append(True)),
        )

    def test_stale_snapshot_restarts_the_helper(self):
        """
        助手 -Watch 到期/崩溃后快照会过期（`read_media_snapshot` 返回 None）。
        这时不拉起它，媒体那一行就永远是「没有程序在放东西」。
        """
        app = self._app()
        started: list = []
        p_snap, p_start = self._patches(None, started)
        before = time.time()
        with p_snap, p_start:
            app._periodic_refresh()
        self.assertEqual(len(started), 1,
                         "快照过期时没有把常驻助手拉起来（P1-5 看门狗失效）")
        self.assertGreaterEqual(app._media_helper_check, before,
                                "看门狗时间戳没有推进 —— 会每 2 秒重查一遍")

    def test_throttle_and_live_helper_prevent_repeated_starts(self):
        """两种"不该启动"的情形：① 30 秒节流窗口内；② 助手还活着（快照新鲜）。"""
        app = self._app()
        started: list = []

        # ① 刚检查过：连快照文件都不该去读（每 2 秒读一次文件是白费）
        app._media_helper_check = time.time()
        p_snap, p_start = self._patches(None, started)
        with p_snap as m_read, p_start:
            app._periodic_refresh()
        self.assertEqual(started, [], "30 秒节流没生效：每个刷新周期都在起助手")
        self.assertFalse(m_read.called, "节流窗口内不该去读快照文件")

        # ② 时间到了，但读得到新鲜快照 = 助手活着 → 也不重启
        app._media_helper_check = 0.0
        p_snap, p_start = self._patches({"sessions": []}, started)
        with p_snap, p_start:
            app._periodic_refresh()
        self.assertEqual(started, [],
                         "助手还活着却重复启动（会堆一串 PowerShell 进程）")


# ===========================================================================
# 8. P2：media_app_stems_match
# ===========================================================================
class TestP2MediaAppStemsMatch(unittest.TestCase):
    """统一的应用匹配入口：精确优先，包含匹配要求两边都 ≥5 字符。"""

    def test_exact_match_wins_and_containment_needs_five_chars(self):
        """
        `("QQ","QQMusic")` 必须是 False。

        这是"选了 A、控的却是 B"的真判据：媒体会话里的家族名常常只有 2 个字符
        （QQ），裸包含匹配会让它命中 QQMusic 的会话，用户点播放结果动了另一个程序。
        """
        for a, b in (("QQMusic", "qqmusic.exe"), ("qqmusic.exe", "QQMusic"),
                     ("MSEdge", "msedge"), ("msedge.exe", "MSEdge"),
                     ("msedge", "msedgewebview2")):
            with self.subTest(a=a, b=b):
                self.assertTrue(core.media_app_stems_match(a, b),
                                f"{a!r} 与 {b!r} 应该算同一个程序")

        for a, b in (("QQ", "QQMusic"), ("QQMusic", "QQ"),
                     ("edge", "msedge"), ("QQ", "QQMusic.exe")):
            with self.subTest(a=a, b=b):
                self.assertFalse(core.media_app_stems_match(a, b),
                                 f"{a!r} 与 {b!r} 不该算同一个程序（短名包含会误命中）")

    def test_empty_side_never_matches(self):
        """空串（会话没报 app）不能匹配任何东西，也不能匹配另一个空串。"""
        for a, b in (("", ""), ("", "QQMusic"), ("QQMusic", ""), (None, "QQMusic")):
            with self.subTest(a=a, b=b):
                self.assertFalse(core.media_app_stems_match(a, b))


# ===========================================================================
# 9. P2：media_session_summary 认 app_key
# ===========================================================================
class TestP2MediaSessionSummaryAppKey(unittest.TestCase):
    """`app_key` 指定应用时，不能把**别的**应用的会话算成命中。"""

    def test_qq_key_does_not_steal_the_qqmusic_session(self):
        """
        会话顺序刻意把 QQMusic 放前面且状态是"正在播放"(4)。

        旧实现（裸包含）会把 QQMusic 当成"用户指定的 QQ"直接挑中；
        新实现挑中真正的 QQ 会话、且**不会**加那句「⚠ 不是指定的应用」。
        """
        sessions = [
            {"app": "QQMusic", "title": "正在放歌", "artist": "某人", "status": 4},
            {"app": "QQ", "title": "聊天窗口", "artist": "", "status": 1},
        ]
        summ = core.media_session_summary(sessions, app_key="QQ")
        self.assertTrue(summ["found"])
        self.assertIsNotNone(summ["picked"])
        self.assertEqual(summ["picked"]["app"], "QQ",
                         "选了 QQ 却挑中了 QQMusic 的会话（P2 匹配修复被回退了）")
        self.assertNotIn("⚠", summ["text"],
                         "挑中的就是指定应用，不该出现「不是指定的应用」提示")

    def test_missing_session_for_the_key_is_reported_honestly(self):
        """
        指定的应用**没有会话**时必须如实提示（⚠），而不是"看起来匹配上了"。

        旧实现里这一条会给出一句毫无破绽的正常文案，用户根本看不出
        "你选的 QQ 其实没有会话、这行显示的是 QQMusic"。
        """
        only_music = [{"app": "QQMusic", "title": "正在放歌", "status": 4}]
        summ = core.media_session_summary(only_music, app_key="QQ")
        self.assertIn("⚠", summ["text"],
                      "指定应用没有会话却没有提示（用户会被误导）")

    def test_the_matching_key_still_works(self):
        """反向对照：`app_key` 正好命中时必须是它，且没有 ⚠。"""
        sessions = [
            {"app": "QQMusic", "title": "正在放歌", "status": 4},
            {"app": "QQ", "title": "聊天窗口", "status": 1},
        ]
        summ = core.media_session_summary(sessions, app_key="QQMusic")
        self.assertEqual(summ["picked"]["app"], "QQMusic")
        self.assertNotIn("⚠", summ["text"])


# ===========================================================================
# 10. P2：idle_samples 不再二次枚举
# ===========================================================================
class TestP2IdleSamplesNoReenumeration(unittest.TestCase):
    """
    界面刷新那一拍刚枚举过进程/窗口，`idle_samples` 不该再枚举一遍。

    判据用"被调用就抛 AssertionError"而不是"调用次数少"：
    `snapshot_processes` 每 2 秒一次是几百次 OpenProcess，省掉的收益必须**归零**，
    "少几次"说明还有路径在偷偷枚举。
    """

    def test_provided_procs_and_windows_are_used_as_is(self):
        procs = {111: {"name": "alpha.exe"}, 222: {"name": "beta.exe"}}
        wins = [_StubWin(hwnd=0x10, pid=111, title="甲", proc="alpha.exe"),
                _StubWin(hwnd=0x20, pid=222, title="乙", proc="beta.exe")]

        with mock.patch.object(core, "snapshot_processes",
                               side_effect=AssertionError("传了 procs 就不该再枚举进程")), \
                mock.patch.object(core, "enum_windows",
                                  side_effect=AssertionError("传了 windows 就不该再枚举窗口")):
            out = core.idle_samples({111: 3.5}, fg_hwnd=0x20,
                                    procs=procs, windows=wins)

        self.assertEqual(set(out), {111, 222})
        self.assertEqual(out[111]["name"], "alpha.exe")
        self.assertEqual(out[222]["name"], "beta.exe")
        self.assertFalse(out[111]["foreground"])
        self.assertTrue(out[222]["foreground"], "前台窗口没被认出来")
        self.assertEqual(out[111]["cpu"], 3.5)

    def test_without_arguments_it_still_enumerates_itself(self):
        """
        命令行入口 `idle_cli` 仍然走"自己枚举"那条路 —— 不能把参数做成必填。
        """
        called = []
        with mock.patch.object(core, "snapshot_processes",
                               side_effect=lambda: (called.append("procs"), {})[1]), \
                mock.patch.object(core, "enum_windows",
                                  side_effect=lambda include_cloaked=True:
                                  (called.append("wins"), [])[1]):
            out = core.idle_samples({}, fg_hwnd=1)
        self.assertEqual(out, {})
        self.assertEqual(called, ["procs", "wins"],
                         "不传参数时应该自己枚举一次（顺序：进程在前、窗口在后）")


# ===========================================================================
# 11. P2：resolve_shortcuts 对异常输出要退化
# ===========================================================================
class TestP2ResolveShortcutsBadOutput(unittest.TestCase):
    """
    批量解析 .lnk 靠一段 PowerShell 输出 JSON。某些 PowerShell 版本空结果输出
    `null` —— 以前会走到 `for it in data` 上抛 TypeError，把整个程序目录扫描弄挂。
    """

    def _run(self, payload: bytes):
        proc = types.SimpleNamespace(stdout=payload, returncode=0)
        with mock.patch("subprocess.run", return_value=proc) as m:
            out = core.resolve_shortcuts([r"C:\tmp\a.lnk"], timeout=1.0)
        return out, m

    def test_null_output_degrades_to_empty_dict(self):
        """
        `null` / 标量 / 坏 JSON / 空输出 —— 一律返回 {}，绝不抛异常。

        这几条对应复查 P2 明确写的那一种（顶层不是 list/dict）。
        """
        for payload in (b"null", b"123", b'"a string"', b"true", b"",
                        b"{ not json", b"[]"):
            with self.subTest(payload=payload):
                out, m = self._run(payload)
                self.assertTrue(m.called, "不该跳过 PowerShell 调用（这是被测路径）")
                self.assertIsInstance(out, dict)
                self.assertEqual(out, {})

    def test_non_dict_elements_are_skipped_not_fatal(self):
        """
        顶层是数组、但**元素不是对象**时也不能崩。

        这条是本批落补丁时补上的（原复查只写"顶层不是 list/dict"这一种）：
        `for it in data: it.get("p")` 碰上 `[1,2,3]` 会抛 AttributeError，
        而调用方（程序目录扫描）没有兜底 —— 一次畸形输出就能把整个目录扫没。
        规矩与上面那条一致：**读不出来就当没有**，绝不抛给调用方。
        """
        for payload in (b"[1,2,3]", b'["a","b"]', b'[null, {"p": ""}]',
                        json.dumps([1, {"p": r"C:\tmp\a.lnk",
                                        "t": r"C:\x.exe"}]).encode("utf-8")):
            with self.subTest(payload=payload):
                out, _ = self._run(payload)
                self.assertIsInstance(out, dict)
                self.assertNotIn(1, out)
        # 混着坏元素时，好的那条仍然要解析出来（别整批丢掉）
        payload = json.dumps([1, {"p": r"C:\tmp\a.lnk", "t": r"C:\x.exe"}]).encode("utf-8")
        out, _ = self._run(payload)
        self.assertEqual(out[r"C:\tmp\a.lnk"]["target"], r"C:\x.exe")

    def test_normal_list_output_is_still_mapped(self):
        """反向对照：正常输出必须照样解析成 {lnk: {...}}（别把功能删掉了）。"""
        payload = json.dumps([{"p": r"C:\tmp\a.lnk", "t": r"C:\x.exe",
                               "a": "--fast", "w": r"C:\x"}]).encode("utf-8")
        out, _ = self._run(payload)
        self.assertEqual(out[r"C:\tmp\a.lnk"]["target"], r"C:\x.exe")
        self.assertEqual(out[r"C:\tmp\a.lnk"]["args"], "--fast")
        self.assertEqual(out[r"C:\tmp\a.lnk"]["workdir"], r"C:\x")

    def test_single_dict_output_is_wrapped(self):
        """只有一个结果时 PowerShell 会输出**一个对象**而不是数组。"""
        payload = json.dumps({"p": r"C:\tmp\b.lnk", "t": r"C:\y.exe"}).encode("utf-8")
        out, _ = self._run(payload)
        self.assertEqual(out[r"C:\tmp\b.lnk"]["target"], r"C:\y.exe")


# ===========================================================================
# 12. P2：filter_non_programs 仍滤文档 / 图片
# ===========================================================================
class TestP2FilterNonPrograms(unittest.TestCase):
    """`CATALOG_NON_PROGRAM_EXTS` 被删了，但"文档不是程序"这条判定不能跟着丢。"""

    def test_documents_and_images_are_still_dropped(self):
        """
        判定链是 `is_runnable_target` → `CATALOG_TARGET_EXTS`（白名单）。

        所以这条其实是"白名单里别混进文档扩展名"的回归：一旦 `.txt` 被加进去，
        「程序目录」里就会冒出几百个"新建文本文档"，而且点它会去 `os.startfile`。
        """
        items = [core.ProgramItem("说明", r"C:\docs\readme.txt"),
                 core.ProgramItem("帮助", r"C:\app\help.chm"),
                 core.ProgramItem("截图", r"C:\pics\shot.png"),
                 core.ProgramItem("真程序", r"C:\tools\app.exe")]
        kept, dropped = core.filter_non_programs(items)
        self.assertEqual([it.name for it in kept], ["真程序"])
        self.assertEqual(len(dropped), 3)
        for ext in (".txt", ".chm", ".png"):
            self.assertTrue(any(ext in note for note in dropped),
                            f"{ext} 的条目没有出现在「被剔除」说明里：{dropped}")

    def test_the_two_extension_lists_do_not_overlap(self):
        """
        内联的"非程序扩展名"清单与 `CATALOG_TARGET_EXTS` 不能有交集。

        这就是原来 `CATALOG_NON_PROGRAM_EXTS` 想守的那条不变式；常量删掉之后
        把它搬进测试里 —— 守门员不该跟着死代码一起被删。
        """
        overlap = sorted(set(NON_PROGRAM_EXTS) & set(core.CATALOG_TARGET_EXTS))
        self.assertEqual(overlap, [],
                         f"这些扩展名同时出现在两份清单里（会被当程序列出来）：{overlap}")

    def test_entries_without_a_target_are_kept(self):
        """没有目标的条目（UWP 之类）保留 —— 界面另有提示路径，不能顺手滤掉。"""
        items = [core.ProgramItem("商店应用", "")]
        kept, dropped = core.filter_non_programs(items)
        self.assertEqual([it.name for it in kept], ["商店应用"])
        self.assertEqual(dropped, [])


# ===========================================================================
# 13. P3：死代码删除的守门员
# ===========================================================================
class TestP3DeadCodeRemoved(unittest.TestCase):
    """
    源码级一次性断言，防止"顺手加回来"。

    为什么值得单独守着：这些名字对应的都是被复查判定为"没人调用 / 概念已废弃"
    的东西（旧的手势入口、倒计时确认框、旧的总览排序、旧的媒体键出口）。
    加回来不会有测试报红，但会把一整套旧行为一起带回来。
    """

    #: winmon.py 里不该再出现的 `def`（用正则要求后面紧跟 `(`，
    #: 这样 `def _tick` 不会误伤 `def _tick_close_watch`）。
    DEAD_DEFS = ("_on_list_click", "media_launch", "copy_history",
                 "export_history", "_gallery_restore_order",
                 "send_media_key", "_tick")

    def test_winmon_py_no_longer_defines_the_dead_methods(self):
        with open(winmon.__file__, encoding="utf-8") as fh:
            src = fh.read()
        for name in self.DEAD_DEFS:
            with self.subTest(name=name):
                hit = re.search(rf"^\s*def {re.escape(name)}\(", src, re.M)
                self.assertIsNone(
                    hit, f"winmon.py 里又出现了 def {name}(（死代码被加回来了）")

    def test_core_no_longer_carries_the_dead_symbols(self):
        with open(core.__file__, encoding="utf-8") as fh:
            src = fh.read()
        self.assertFalse(hasattr(core, "CATALOG_NON_PROGRAM_EXTS"),
                         "core.CATALOG_NON_PROGRAM_EXTS 又回来了")
        self.assertIsNone(re.search(r"^\s*CATALOG_NON_PROGRAM_EXTS", src, re.M))
        # TrayIcon.seen 是旧的"图标装没装过"标志，现在判据是 self.ok
        self.assertNotIn("self.seen", inspect.getsource(core.TrayIcon),
                         "TrayIcon 上又出现了 seen 标志（旧状态，会和 ok 打架）")
        self.assertFalse(hasattr(core.TrayIcon, "seen"))


@h.needs_tk
class TestP3ConfirmDialogHasNoCountdown(unittest.TestCase):
    """
    确认框以前有"N 秒后自动确认"的倒计时（`seconds` 形参 + `countdown` /
    `_remaining` / `_timer_id`）。那套被删掉了：无人值守下"自动点确认"会**真的
    关掉用户的视频**，属于危险默认值。

    只有构造真实例才能拿 `hasattr` 证明"实例上确实没有这些状态" ——
    所以这一条按项目约定开真 Tk 根窗口，并登记清理。
    """

    def setUp(self):
        import tkinter as tk

        self.root = tk.Tk()
        self.root.withdraw()
        self.addCleanup(self.root.destroy)

    def test_signature_and_instance_have_no_countdown_state(self):
        params = list(inspect.signature(winmon.ConfirmDialog.__init__).parameters)
        self.assertNotIn("seconds", params,
                         "倒计时参数又回来了（会恢复「自动确认」这个危险默认值）")

        dlg = winmon.ConfirmDialog(self.root, "测试", "要关闭吗？")

        def _cleanup_dialog():
            try:
                dlg._close()
            except Exception:
                pass

        self.addCleanup(_cleanup_dialog)
        for attr in ("countdown", "_remaining", "_timer_id"):
            with self.subTest(attr=attr):
                self.assertFalse(hasattr(dlg, attr),
                                 f"ConfirmDialog 实例上又出现了 {attr}")
        self.assertFalse(hasattr(winmon.ConfirmDialog, "countdown"))
        # 反向对照：真正在用的两个状态必须在，免得"删过头"也一路绿灯
        self.assertTrue(hasattr(dlg, "result"))
        self.assertTrue(hasattr(dlg, "_closed"))


# ===========================================================================
# 14. P3：action_clean_files 区分「取消」与「全不勾」
# ===========================================================================
class TestP3CleanFilesCancelVsEmpty(unittest.TestCase):
    """
    `_CleanupDialog.show()` 返回值有三态：`None`（取消）/ `{}`（全不勾）/ 非空（要删）。

    以前两种情况都当成"没勾"混成一句话，用户分不清"我点了取消"和
    "我确实什么都没勾" —— 而且"取消"被当成"确认了一个空选择"写日志。
    """

    def _app(self):
        app = winmon.WinMonApp.__new__(winmon.WinMonApp)
        app.root = None                       # 对话框被替换掉了，不需要真窗口
        app.status = []
        app._set_status = app.status.append
        app.log_action = lambda *a, **k: None
        return app

    @staticmethod
    def _items():
        return [{"key": "history_log", "label": "操作历史日志", "note": "",
                 "default": True, "paths": [r"X:\winmon\winmon_history.log"],
                 "count": 1, "bytes": 12, "exists": True}]

    def _run(self, show_result, clean_result=(0, 0, [])):
        app = self._app()
        dlg = types.SimpleNamespace(show=lambda: show_result)
        with mock.patch.object(core, "list_cleanable_files", return_value=self._items()), \
                mock.patch.object(winmon, "_CleanupDialog", return_value=dlg), \
                mock.patch.object(core, "clean_files",
                                  return_value=clean_result) as m_clean:
            app.action_clean_files()
        return app, m_clean

    def test_cancel_says_cancelled_and_never_deletes(self):
        app, m_clean = self._run(None)
        self.assertFalse(m_clean.called, "「取消」竟然真的去删文件了")
        self.assertEqual(len(app.status), 1)
        self.assertIn("取消", app.status[0])

    def test_empty_selection_says_nothing_ticked_and_never_deletes(self):
        app, m_clean = self._run({})
        self.assertFalse(m_clean.called, "「全不勾」竟然真的去删文件了")
        self.assertEqual(len(app.status), 1)
        self.assertIn("没有勾选", app.status[0])

    def test_the_two_situations_read_differently(self):
        """两种情况的文案必须不同 —— 否则用户分不清自己到底做了什么。"""
        cancel_msg = self._run(None)[0].status[0]
        empty_msg = self._run({})[0].status[0]
        self.assertNotEqual(cancel_msg, empty_msg,
                            "「取消」与「全不勾」给出了同一句话")

    def test_a_real_selection_still_deletes(self):
        """反向对照：非空选择必须真的走到 `core.clean_files`。"""
        app, m_clean = self._run({"history_log"})
        m_clean.assert_called_once()
        self.assertIn("已清理", app.status[0])


# ===========================================================================
# 15. P2：托盘隐藏时总览不取帧 / 恢复时补一帧
# ===========================================================================
class TestP2TrayGalleryAndRestore(unittest.TestCase):
    """隐藏到托盘时总览没人看 —— 还在取帧就是纯烧 CPU。"""

    def test_withdrawn_window_forgets_tiles_and_stops_requesting(self):
        app = winmon.WinMonApp.__new__(winmon.WinMonApp)
        app.root = _Root(state="withdrawn")
        forgotten: list = []
        requested: list = []
        app.worker = types.SimpleNamespace(forget=lambda key: forgotten.append(key),
                                           request=lambda *a: requested.append(a))
        app.gallery_keys = {0x11: "tile", 0x22: "tile", 0x33: "tile"}

        winmon.WinMonApp._sync_gallery_requests(app)

        self.assertEqual(sorted(forgotten),
                         [("tile", 0x11), ("tile", 0x22), ("tile", 0x33)],
                         "隐藏到托盘后没有把每个格子的取帧请求撤掉")
        self.assertEqual(requested, [],
                         "窗口已隐藏到托盘，还在请求取帧（白烧 CPU）")

    def test_visible_gallery_tab_still_requests_frames(self):
        """反向对照：正常显示 + 在总览页时必须照常取帧（别把功能一刀切了）。"""
        app = winmon.WinMonApp.__new__(winmon.WinMonApp)
        app.root = _Root(state="normal")
        app.notebook = types.SimpleNamespace(index=lambda which: 1)
        app.live_gallery = _Var(True)
        app.paused = False
        requested: list = []
        app.worker = types.SimpleNamespace(forget=lambda key: None,
                                           request=lambda *a: requested.append(a))
        app.gallery_keys = {0x11: "tile"}
        app.win_by_hwnd = {0x11: _StubWin(hwnd=0x11, rect_w=800, rect_h=600)}

        winmon.WinMonApp._sync_gallery_requests(app)

        self.assertEqual(len(requested), 1, "正常状态下总览不取帧了")
        self.assertEqual(requested[0][0], ("tile", 0x11))

    def test_preview_stops_when_user_is_on_another_tab(self):
        """不在实时预览页时不该继续抓屏；切回来要自动恢复。"""
        app = winmon.WinMonApp.__new__(winmon.WinMonApp)
        app.root = _Root(state="normal")
        app.preview_key = 0x123
        app.paused = False
        app.fps_choice = _Var("很流畅 10 FPS")
        app._last_preview_req = None
        app.preview = types.SimpleNamespace(canvas_size=lambda: (640, 400))
        app.win_by_hwnd = {0x123: _StubWin(hwnd=0x123, rect_w=800, rect_h=600)}
        forgotten: list = []
        requested: list = []
        app.worker = types.SimpleNamespace(forget=lambda key: forgotten.append(key),
                                           request=lambda *a, **k: requested.append(a))
        tab = {"index": 5}
        app.notebook = types.SimpleNamespace(index=lambda _which: tab["index"])

        winmon.WinMonApp._request_preview_frame(app)
        self.assertEqual(requested, [], "不在预览页还在抓屏")
        self.assertIn(("win", 0x123), forgotten)

        tab["index"] = 0
        winmon.WinMonApp._request_preview_frame(app)
        self.assertEqual(len(requested), 1, "切回预览页没有恢复取帧")

    def test_restore_from_tray_forces_one_frame_and_tolerates_no_preview(self):
        """
        恢复显示后必须**补一帧**：隐藏期间 `_request_preview_frame` 直接 return，
        预览会一直停在最后一张旧画面上，看起来像"卡死了"。

        末尾再跑一次 `preview_key = None`（还没选中任何窗口）的恢复：
        这条路径不能因为"没有目标"就抛异常把托盘消息处理打断。
        """
        app = winmon.WinMonApp.__new__(winmon.WinMonApp)
        app.root = _Root(state="normal")
        app._set_status = lambda text: None
        requested: list = []
        app.worker = types.SimpleNamespace(request=lambda *a, **k: requested.append(a),
                                           forget=lambda key: None)
        app.win_by_hwnd = {0x123: _StubWin(hwnd=0x123, rect_w=800, rect_h=600)}
        app.preview_key = 0x123
        app.paused = False
        app.fps_choice = _Var("")
        app._last_preview_req = None          # 真 __init__ 会设成 None，替身要补齐

        winmon.WinMonApp._request_preview_frame(app)
        self.assertEqual(len(requested), 1, "正常状态下没登记预览请求")

        app.root._state = "withdrawn"
        winmon.WinMonApp._request_preview_frame(app)
        self.assertEqual(len(requested), 1, "隐藏到托盘期间仍在登记取帧")

        app.root._state = "normal"
        winmon.WinMonApp.restore_from_tray(app)
        self.assertEqual(len(requested), 2, "从托盘恢复后没有补那一帧")
        self.assertEqual(requested[1], requested[0],
                         "补的那一帧参数应该和隐藏前一致")

        # 没有预览目标时也不能抛（托盘消息处理里是裸调用）
        app.preview_key = None
        app.root._state = "withdrawn"
        winmon.WinMonApp.restore_from_tray(app)


# ===========================================================================
# 16. P2：私有内存分档查询
# ===========================================================================
class TestP2PrivateMemoryTiering(unittest.TestCase):
    """
    「私有提交」以前对 `proc_map` 里的**每一个**进程都查（OpenProcess +
    GetProcessMemoryInfo）。每 2 秒几百次，纯浪费 —— 那些数只在
    「显示无窗口进程 / 进程树」视图里才有人看。

    分档：与有窗口进程同名的（Edge/Chrome 的后台兄弟进程）总要查；
    其余只有 `show_no_window` 勾了、或当前在进程树页时才查。
    """

    def test_source_has_the_need_full_tiering(self):
        src = inspect.getsource(winmon.WinMonApp.refresh_processes)
        self.assertRegex(
            src, r"need_full\s*=\s*self\.show_no_window\.get\(\)\s*or\s*\(",
            "refresh_processes 里的 need_full 分档判断不见了")
        self.assertIn("if not need_full", src,
                      "没有「不需要完整数据就跳过」的那一句，分档等于没做")

    def _app(self, show_no_window: bool):
        app = winmon.WinMonApp.__new__(winmon.WinMonApp)
        app.show_no_window = _Var(show_no_window)
        app.windows = [_StubWin(hwnd=0x10, pid=100, title="甲", proc="alpha.exe")]
        proc_map = {100: {"name": "alpha.exe", "mem_kb": 0}}
        for i in range(50):
            proc_map[1000 + i] = {"name": f"bg{i}.exe", "mem_kb": 0}
        app.proc_map = {}
        app._cpu_prev = {}
        app._cpu_pct = {}
        app._cpu_prev_t = 0.0
        app._path_cache = {}
        return app, proc_map

    def _run(self, app, proc_map, queried):
        with mock.patch.object(core, "snapshot_processes", return_value=proc_map), \
                mock.patch.object(core, "query_process_cpu_time", return_value=None), \
                mock.patch.object(
                    core, "query_process_detail",
                    side_effect=lambda pid, with_private=False:
                    (queried.append(pid), ("", 0, 0))[1]):
            app.refresh_processes(force_cpu=True)

    def test_overview_tab_skips_the_background_processes(self):
        """
        总览页（`show_no_window` 未勾）时，51 个进程里只有 1 个该被查。

        这条如果红了，说明"每 2 秒几百次 OpenProcess"又回来了 ——
        表现是空闲时 CPU 占用明显上升。
        """
        app, proc_map = self._app(show_no_window=False)
        self.assertFalse(hasattr(app, "notebook"),
                         "本用例刻意不装 notebook：总览页 = 不在进程树页")
        queried: list = []
        self._run(app, proc_map, queried)
        self.assertEqual(queried, [100],
                         f"总览页查了 {len(queried)} 个进程的私有内存（分档失效）")

    def test_show_no_window_queries_everything(self):
        """反向对照：勾了「显示无窗口进程」就必须查全部 —— 那些数字正是要看的。"""
        app, proc_map = self._app(show_no_window=True)
        queried: list = []
        self._run(app, proc_map, queried)
        self.assertEqual(sorted(queried), sorted(proc_map),
                         "勾了「显示无窗口进程」却没有查全部进程的私有内存")


if __name__ == "__main__":
    unittest.main(verbosity=2)
