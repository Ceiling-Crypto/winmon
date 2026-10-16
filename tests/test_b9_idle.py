# -*- coding: utf-8 -*-
"""
B9 —— 闲置程序（"长期开着、只耗电不使用"）的识别与处理（`test_b9`）。

用户的需求：某个程序长期开着又没人用，希望"建议关闭，或超过指定时间帮忙关闭"；
功能是**勾选项、默认不开启**，时长默认 30 分钟，另有 1 小时 / 2 小时 / 4 小时。

分四层写：

  1. **纯逻辑**：`IdleWatcher` 的计时与判据（三个"在干活"的信号：前台 / CPU / 声音）。
     这一层不碰 Win32，任何环境都必须跑。
  2. **采样与过滤**：`idle_samples()` 的作用范围与排除项
     （只含有可见窗口的程序；关键系统进程、外壳、输入法、以及 **WinMon 自己**都不参与）。
  3. **真窗口**：真开一个别的进程的窗口，真的"优雅关闭"它并核对窗口消失；
     以及真窗口上的默认值/开关行为。
  4. **界面接线**：用假 app 驱动 `_check_idle()`，核对"默认不开启""关掉功能就清零"
     "只在越线时提醒一次""自动关只有显式打开才会走"。

安全性是这套功能里最要紧的东西，所以测试里专门钉了几条：
**绝不强杀**（只发 `WM_CLOSE`）、**保护进程根本进不来**、**默认全关**。
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import _helpers as h                                          # noqa: E402
import winmon_core as core                                    # noqa: E402
import winmon                                                 # noqa: E402

#: 靶子窗口：一个普通的 Tk 窗口，用来验证"优雅关闭"与"闲置采样"。
IDLE_TARGET_SCRIPT = r'''
# -*- coding: utf-8 -*-
"""靶子：普通 Tk 窗口。"""
import sys
import tkinter as tk

title = sys.argv[1] if len(sys.argv) > 1 else "IdleProbe-Target"
root = tk.Tk()
root.title(title)
root.geometry("420x300+240+200")
tk.Label(root, text="idle target").pack(pady=40)
root.update()
root.mainloop()
'''

#: 关键系统进程与外壳类，必须永远进不了候选
MUST_BE_PROTECTED = ("explorer.exe", "dwm.exe", "csrss.exe", "winlogon.exe",
                     "textinputhost.exe", "sihost.exe", "ctfmon.exe")


#: 「无视关闭请求」的靶子。
#:
#: 它会在**注册好 WM_DELETE_WINDOW 处理器之后**原子地写一个就绪文件；
#: 父进程必须等这个文件出现才能开始关它。为什么必须这样：
#: Tk 在 `tk.Tk()` 之后窗口就可以被枚举到了，但"忽略关闭请求"是下一行才注册的 ——
#: 父进程若在这几十微秒里发了 WM_CLOSE，窗口会**正常关掉**，
#: 于是"绝不强杀"这条用例就会随机假失败（实测全量跑时撞到过一次）。
STUBBORN_TARGET_SCRIPT = r'''
# -*- coding: utf-8 -*-
"""靶子：无视关闭请求的窗口（会先写一个就绪文件）。"""
import os
import sys
import tkinter as tk

title, ready = sys.argv[1], sys.argv[2]
root = tk.Tk()
root.title(title)
root.geometry("420x300+240+200")
tk.Label(root, text="stubborn").pack(pady=40)
root.protocol("WM_DELETE_WINDOW", lambda: None)      # 无视关闭请求
root.update()
tmp = ready + ".tmp"
with open(tmp, "w", encoding="utf-8") as fh:
    fh.write("ready")
os.replace(tmp, ready)                               # 原子落盘：能看到它就说明协议已注册
root.mainloop()
'''


def samp(fg=False, cpu=0.0, name="app.exe", hwnd=1, iconic=False):
    """
    造一条观测值（`IdleWatcher.observe` 的入参格式）。

    注意：**没有 `audio` 字段了** —— 2026-10-13 删掉了"在放声音"那条信号
    （它依赖的 `enum_audio_sessions()` 在本机恒返回 0，等于永远不成立）。
    判"在用"只剩：在前台 / CPU 超过阈值。
    """
    return {"foreground": fg, "cpu": cpu, "name": name,
            "title": "T", "hwnd": hwnd, "iconic": iconic}


class TestIdleWatcher(unittest.TestCase):
    """计时与判据（纯逻辑）。"""

    def test_accumulates_until_threshold_then_fires_once(self):
        w = core.IdleWatcher(threshold=10.0)
        fired = []
        for _ in range(4):                       # 4 × 3s = 12s
            fired += w.observe(3.0, {100: samp()})
        self.assertEqual(fired, [100], "越过阈值时应当只报一次")
        self.assertAlmostEqual(w.idle_seconds(100), 12.0)
        fired += w.observe(3.0, {100: samp()})   # 再闲一拍
        self.assertEqual(fired, [100], "已经提醒过的不该反复报")
        self.assertAlmostEqual(w.idle_seconds(100), 15.0)

    def test_not_fired_before_threshold(self):
        w = core.IdleWatcher(threshold=30.0)
        self.assertEqual(w.observe(29.0, {7: samp()}), [])
        self.assertEqual(w.candidates(), [], "没到阈值不该出现在候选里")

    def test_foreground_resets_the_clock(self):
        w = core.IdleWatcher(threshold=10.0)
        w.observe(9.0, {1: samp()})
        w.observe(1.0, {1: samp(fg=True)})       # 你正在用它
        self.assertEqual(w.idle_seconds(1), 0.0)
        self.assertEqual(w.observe(9.0, {1: samp()}), [])

    def test_cpu_activity_resets_the_clock(self):
        """CPU 在动就是"在干活" —— 后台编译 / 索引不该被当成闲置。"""
        w = core.IdleWatcher(threshold=10.0, busy_cpu=1.0)
        w.observe(9.0, {1: samp(cpu=0.0)})
        w.observe(1.0, {1: samp(cpu=8.0)})
        self.assertEqual(w.idle_seconds(1), 0.0)

    def test_audio_signal_is_gone_and_documented(self):
        """
        「在放声音就不算闲置」这条**已被删除**（2026-10-13，定稿决定）。

        为什么删：它依赖 `enum_audio_sessions()`，而那个 API 在本机**恒返回 0 个会话**
        （连自己造的出声源正在放时也是 0）—— 一条永远不成立的证据留着只会误导。
        这条用例把它固定住：以后要恢复这块能力，必须先和用户确认、
        并且**换一个在本机真的能拿到数据的实现**（比如重写 MMDevice 那条路）。
        """
        self.assertFalse(hasattr(core, "enum_audio_sessions"),
                         "要恢复音频信号请先与用户确认（旧实现恒为 0，会误导判定）")
        # 闲置判定仍然只看"前台 / CPU"两个信号
        w = core.IdleWatcher(threshold=10.0)
        w.observe(9.0, {1: samp()})
        w.observe(1.0, {1: samp(cpu=8.0)})          # CPU 起来了就算在用
        self.assertEqual(w.idle_seconds(1), 0.0)

    def test_just_below_busy_cpu_threshold_still_counts_as_idle(self):
        w = core.IdleWatcher(threshold=10.0, busy_cpu=1.0)
        w.observe(10.0, {1: samp(cpu=0.99)})
        self.assertEqual(w.idle_seconds(1), 10.0, "低于阈值应当仍算闲置")

    def test_forget_drops_dead_processes(self):
        w = core.IdleWatcher(threshold=10.0)
        w.observe(5.0, {1: samp(), 2: samp()})
        w.observe(1.0, {1: samp()})              # 2 消失了
        self.assertEqual(w.idle_seconds(2), 0.0)
        self.assertNotIn(2, [r["pid"] for r in w.candidates()])
        self.assertGreater(w.idle_seconds(1), 0.0)

    def test_reset_zeroes_the_clock(self):
        """「稍后再说」就是 reset：清零后重新计时、到点会再提醒。"""
        w = core.IdleWatcher(threshold=10.0)
        w.observe(12.0, {1: samp()})
        self.assertTrue(w.candidates())
        w.reset([1])
        self.assertEqual(w.candidates(), [])
        self.assertEqual(w.observe(11.0, {1: samp()}), [1], "reset 之后应当能再提醒一次")

    def test_candidates_sorted_by_idle_desc(self):
        w = core.IdleWatcher(threshold=5.0)
        for _ in range(3):
            w.observe(4.0, {1: samp(), 2: samp(), 3: samp()})
        w.observe(4.0, {1: samp(), 2: samp()})   # 3 提前一拍消失，1/2 更久
        rows = w.candidates()
        self.assertTrue(rows)
        self.assertEqual([r["idle"] for r in rows],
                         sorted([r["idle"] for r in rows], reverse=True))

    def test_set_threshold_keeps_accumulated_time_but_rearms(self):
        """
        改时长**不该把已累计的闲置时间清掉**（否则用户会想"我明明等了半小时"），
        但要清掉"已提醒"的标记，好让立刻满足新阈值的那批重新被提示。
        """
        w = core.IdleWatcher(threshold=100.0)
        w.observe(60.0, {1: samp()})
        self.assertEqual(w.candidates(), [])
        w.set_threshold(30.0)
        self.assertAlmostEqual(w.idle_seconds(1), 60.0, msg="累计时间被清掉了")
        self.assertEqual([r["pid"] for r in w.candidates()], [1])
        self.assertEqual(w.observe(1.0, {1: samp()}), [1], "降低阈值后应当能立刻提醒")

    def test_zero_or_negative_dt_is_ignored(self):
        w = core.IdleWatcher(threshold=10.0)
        self.assertEqual(w.observe(0.0, {1: samp()}), [])
        self.assertEqual(w.idle_seconds(1), 0.0)
        self.assertEqual(w.observe(-5.0, {1: samp()}), [])

    def test_meta_is_kept_for_the_dialog(self):
        w = core.IdleWatcher(threshold=5.0)
        w.observe(6.0, {9: samp(name="cloudmusic.exe")})
        row = w.candidates()[0]
        self.assertEqual(row["name"], "cloudmusic.exe")
        self.assertIn("idle", row)
        self.assertIn("hwnd", row)


class TestIdleFormatting(unittest.TestCase):
    """给人看的时长文字与下拉框标签。"""

    def test_format_idle(self):
        self.assertEqual(core.format_idle(45), "45 秒")
        self.assertEqual(core.format_idle(120), "2 分钟")
        self.assertEqual(core.format_idle(3600), "1 小时")
        self.assertEqual(core.format_idle(3600 + 5 * 60), "1 小时 5 分钟")

    def test_threshold_choices_are_what_the_user_asked(self):
        labels = [label for label, _s in core.IDLE_CHECK_CHOICES]
        self.assertEqual(labels[0], "30 分钟", "默认必须是 30 分钟")
        self.assertIn("1 小时", labels)
        self.assertIn("2 小时", labels)
        self.assertEqual(core.IDLE_DEFAULT_SECONDS, 30 * 60)

    def test_label_round_trip(self):
        for label, seconds in core.IDLE_CHECK_CHOICES:
            with self.subTest(label=label):
                self.assertEqual(core.idle_threshold_label(seconds), label)
                import winmon as w
                self.assertEqual(w._idle_seconds_of(label), seconds)

    def test_unknown_values_fall_back(self):
        self.assertEqual(core.idle_threshold_label(99999), "1666 分钟")
        import winmon as w
        self.assertEqual(w._idle_seconds_of("胡说八道"), core.IDLE_DEFAULT_SECONDS)


class TestProtectedProcesses(unittest.TestCase):
    """保护名单：这些进程**根本不该**产生观测值。"""

    def test_shell_and_critical_are_protected(self):
        for name in MUST_BE_PROTECTED:
            with self.subTest(name=name):
                self.assertIn(name, core.IDLE_PROTECTED_PROCESSES)

    def test_protected_is_a_superset_of_critical(self):
        self.assertTrue(core.CRITICAL_PROCESSES <= core.IDLE_PROTECTED_PROCESSES)

    def test_winmon_title_prefix_is_defined(self):
        """WinMon 自己的窗口按标题前缀排除（只比 pid 排不掉"别的实例"）。"""
        self.assertTrue(core.WINMON_TITLE_PREFIX)


class TestIdleSamples(unittest.TestCase):
    """采样与过滤（真窗口）。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def _spawn(self, title: str):
        path = os.path.join(self.tmp.name, f"{abs(hash(title))}.py")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(IDLE_TARGET_SCRIPT)
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        proc = subprocess.Popen([sys.executable, path, title],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                creationflags=flags)
        h.spawn_cleanup(self, proc)
        for _ in range(60):
            if proc.poll() is not None:
                self.skipTest("靶子进程提前退出")
            hits = [w for w in core.enum_windows() if w.title == title]
            if hits:
                return proc, hits[0]
            time.sleep(0.2)
        self.skipTest(f"没能创建靶子窗口 {title}（环境没有图形会话）")
        return proc, None

    def test_winmon_own_window_is_excluded(self):
        """
        **WinMon 自己绝不能被当成"闲置程序"**。

        只比 pid 是不够的：本机可能同时开着别的 WinMon 实例（比如界面在跑、
        命令行又在判定），那些实例的 pid 当然不等于本进程 —— 所以按标题前缀再排一次。
        这条是真踩到的：`--idle` 第一版把自己列进了候选。
        """
        proc, wi = self._spawn(core.WINMON_TITLE_PREFIX + " 测试窗口（不该被当成闲置）")
        try:
            samples = core.idle_samples({})
            self.assertNotIn(proc.pid, samples,
                             "WinMon 自己的窗口出现在了闲置候选里")
        finally:
            try:
                core.close_window_and_verify(wi.hwnd, timeout=2.0)
            except Exception:
                pass

    def test_a_normal_window_is_included(self):
        proc, _wi = self._spawn("IdleProbe-Normal")
        samples = core.idle_samples({})
        self.assertIn(proc.pid, samples, "普通窗口应当参与闲置判定")
        self.assertIn("name", samples[proc.pid])

    def test_foreground_marking_follows_the_given_handle(self):
        """前台判定要认传入的句柄（这样测试与界面都能明确指定"谁在前台"）。"""
        proc, wi = self._spawn("IdleProbe-Fg")
        self.assertIn(proc.pid, core.idle_samples({}, fg_hwnd=wi.hwnd))
        self.assertTrue(core.idle_samples({}, fg_hwnd=wi.hwnd)[proc.pid]["foreground"])
        self.assertFalse(core.idle_samples({}, fg_hwnd=0x7FFF0000)[proc.pid]["foreground"])

    def test_cpu_map_is_passed_through(self):
        proc, _wi = self._spawn("IdleProbe-Cpu")
        samples = core.idle_samples({proc.pid: 12.5})
        self.assertAlmostEqual(samples[proc.pid]["cpu"], 12.5)


@h.needs_tk
class TestGracefulClose(unittest.TestCase):
    """优雅关闭：真开一个别的进程的窗口，真的关掉它，并核对窗口消失。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def _spawn(self, title: str = "IdleProbe-Close"):
        path = os.path.join(self.tmp.name, "target.py")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(IDLE_TARGET_SCRIPT)
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        proc = subprocess.Popen([sys.executable, path, title],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                creationflags=flags)
        h.spawn_cleanup(self, proc)
        for _ in range(60):
            if proc.poll() is not None:
                self.skipTest("靶子进程提前退出")
            hits = [w for w in core.enum_windows() if w.title == title]
            if hits:
                return proc, hits[0]
            time.sleep(0.2)
        self.skipTest("没能创建靶子窗口（环境没有图形会话）")
        return proc, None

    def test_bad_handles_are_rejected(self):
        for hwnd in (0, 0x7FFF0000):
            with self.subTest(hwnd=hwnd):
                ok, msg = core.close_idle_window(hwnd)
                self.assertFalse(ok)
                self.assertTrue(msg)

    def test_really_closes_the_window(self):
        _proc, wi = self._spawn()
        ok, msg = core.close_idle_window(wi.hwnd, timeout=5.0)
        self.assertTrue(ok, msg)
        self.assertFalse(core.user32.IsWindow(core.w.HWND(wi.hwnd)),
                         "说关好了，但窗口还在")

    def test_only_sends_wm_close_never_kills(self):
        """
        **绝不强杀**：把靶子换成"无视关闭请求"的窗口，函数必须如实报"仍在"，
        而且进程还活着（否则就变成偷偷强杀了，那是会丢数据的）。
        """
        path = os.path.join(self.tmp.name, "stubborn.py")
        ready = os.path.join(self.tmp.name, "stubborn.ready")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(STUBBORN_TARGET_SCRIPT)
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        proc = subprocess.Popen([sys.executable, path, "IdleProbe-Stubborn", ready],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                creationflags=flags)
        h.spawn_cleanup(self, proc)
        wi = None
        for _ in range(60):
            if proc.poll() is not None:
                self.skipTest("靶子进程提前退出")
            # 先等"协议已注册"的就绪文件，再找窗口 —— 否则会撞上初始化竞态
            if not os.path.exists(ready):
                time.sleep(0.1)
                continue
            # **按 PID 找窗口**，不要按标题找：标题可能撞上别的窗口
            # （或别的进程遗留下来的同名窗口），那就会去关一个无关的窗口。
            wi = h.pick_window_for_pid(proc.pid)
            if wi is not None:
                break
            time.sleep(0.2)
        if wi is None:
            self.skipTest("没能创建靶子窗口")
        self.assertEqual(wi.title, "IdleProbe-Stubborn", "找到的不是靶子窗口")
        ok, msg = core.close_idle_window(wi.hwnd, timeout=1.5)
        self.assertFalse(ok, f"无视关闭请求的窗口不该被报成'已关闭'：{msg}")
        self.assertTrue(core.is_process_alive(proc.pid),
                        "对方没响应关闭请求，不该被强杀（会丢数据）")
        self.assertTrue(core.user32.IsWindow(core.w.HWND(wi.hwnd)))


class TestIdleUiDefaults(unittest.TestCase):
    """界面变量的默认值 —— 约定"勾选项、默认不开启"。"""

    def setUp(self):
        import tkinter as tk

        try:
            self.root = tk.Tk()
            self.root.withdraw()
        except Exception as exc:                              # pragma: no cover
            self.skipTest(f"没有图形会话：{exc}")
        self.addCleanup(self.root.destroy)

    def _vars(self):
        app = winmon.WinMonApp.__new__(winmon.WinMonApp)
        winmon.WinMonApp._init_video_vars(app)     # 界面变量统一在这里创建
        return app

    def test_defaults_are_off(self):
        app = self._vars()
        self.assertFalse(app.idle_alert.get(), "闲置提醒必须默认关闭")
        self.assertFalse(app.idle_auto_close.get(), "自动关闭必须默认关闭")
        self.assertEqual(app.idle_choice.get(), "30 分钟", "默认时长是 30 分钟")

    def test_threshold_choices_are_offered(self):
        app = self._vars()
        self.assertIn(app.idle_choice.get(),
                      [label for label, _s in core.IDLE_CHECK_CHOICES])

    def test_watcher_starts_empty(self):
        app = self._vars()
        self.assertEqual(app.idle_watch.candidates(), [])
        self.assertEqual(app.idle_watch.threshold, core.IDLE_DEFAULT_SECONDS)

    def test_toggle_resets_the_clock(self):
        app = self._vars()
        app.idle_watch.observe(120.0, {1: samp()})
        self.assertGreater(app.idle_watch.idle_seconds(1), 0)
        app._set_status = lambda _t: None
        app._schedule_save_settings = lambda *a: None
        app._on_idle_toggle()
        self.assertEqual(app.idle_watch.idle_seconds(1), 0.0,
                         "切换开关应当重新开始计时")


class _IdleHarness(winmon.WinMonApp):
    """只装 `_check_idle` 需要的几样东西（用 `__new__` 跳过要开窗口的 `__init__`）。"""

    def __init__(self, enabled: bool, auto_close: bool = False,
                 snooze: float = 3600.0):
        self.idle_alert = _Var(enabled)
        self.idle_auto_close = _Var(auto_close)
        self.idle_choice = _Var("30 分钟")
        self.idle_snooze_choice = _Var(core.idle_snooze_label(snooze))
        self.idle_watch = core.IdleWatcher(core.IDLE_DEFAULT_SECONDS, snooze=snooze)
        self._idle_ticks = 0
        self._idle_dialog_open = False
        self._cpu_pct: dict = {}
        #: `_check_idle` 现在把"本拍已经枚举过的进程/窗口"传给 `core.idle_samples`
        #: （P2 优化：同一拍别枚举两遍），所以替身 app 也得有这两样。
        self.proc_map: dict = {}
        self.windows: list = []
        self.status: list[str] = []
        self.dialogs = 0
        self.auto_closed: list = []

    def _set_status(self, text: str) -> None:
        self.status.append(text)

    def action_idle_dialog(self) -> None:
        self.dialogs += 1

    def _auto_close_idle(self, pids) -> None:
        self.auto_closed.append(list(pids))


class _Var:
    """最小的 tk 变量替身（只需要 get/set）。"""

    def __init__(self, value):
        self._v = value

    def get(self):
        return self._v

    def set(self, value):
        self._v = value


class TestCheckIdleWiring(unittest.TestCase):
    """`_check_idle` 的接线：默认不动手、关掉就清零、只在越线时提醒一次。"""

    def _samples(self, pid=4242):
        return {pid: samp(name="idle.exe")}

    def test_disabled_feature_never_accumulates(self):
        app = _IdleHarness(enabled=False)
        with mock.patch.object(core, "idle_samples", return_value=self._samples()):
            for _ in range(5):
                app._check_idle(100.0)
        self.assertEqual(app.idle_watch.idle_seconds(4242), 0.0,
                         "没勾选就不该开始计时")
        self.assertEqual(app.dialogs, 0)

    def test_enabled_accumulates_and_opens_the_dialog_once(self):
        app = _IdleHarness(enabled=True)
        with mock.patch.object(core, "idle_samples", return_value=self._samples()):
            for _ in range(3):                    # 3 × 20 分钟 = 1 小时
                app._check_idle(20 * 60)
        self.assertGreaterEqual(app.idle_watch.idle_seconds(4242), 1800)
        self.assertEqual(app.dialogs, 1, "越线时应当只弹一次清单")
        self.assertTrue(app.status, "应当在状态栏说明")

    def test_auto_close_only_when_explicitly_enabled(self):
        """到点自动关**只有显式打开那个开关**才会走。"""
        app = _IdleHarness(enabled=True, auto_close=True)
        with mock.patch.object(core, "idle_samples", return_value=self._samples()):
            app._check_idle(31 * 60)
        self.assertEqual(app.auto_closed, [[4242]])
        self.assertEqual(app.dialogs, 0, "自动关闭模式下不该再弹确认清单")

    def test_turning_the_feature_off_clears_the_clock(self):
        """关掉功能要清零 —— 否则再打开时"凭空"已经闲置很久，会立刻炸出一堆提醒。"""
        app = _IdleHarness(enabled=True)
        with mock.patch.object(core, "idle_samples", return_value=self._samples()):
            app._check_idle(20 * 60)
        self.assertGreater(app.idle_watch.idle_seconds(4242), 0)
        app.idle_alert.set(False)
        app._check_idle(20 * 60)
        self.assertEqual(app.idle_watch.idle_seconds(4242), 0.0)

    def test_changing_the_choice_does_not_reset_the_clock(self):
        """
        改时长不该把已累计的闲置时间清掉（用户会想"我明明等了半小时"）。
        这里把时长从 30 分钟改成 2 小时，累计时间必须原样保留。
        """
        app = _IdleHarness(enabled=True)
        with mock.patch.object(core, "idle_samples", return_value=self._samples()):
            app._check_idle(10 * 60)
            app.idle_choice.set("2 小时")
            app._check_idle(60.0)
        self.assertAlmostEqual(app.idle_watch.idle_seconds(4242), 660.0, delta=1.0)
        self.assertEqual(int(app.idle_watch.threshold), 2 * 3600)

    def test_disabled_when_candidates_appear_means_no_dialog(self):
        app = _IdleHarness(enabled=False)
        with mock.patch.object(core, "idle_samples", return_value=self._samples()):
            app._check_idle(99 * 60)
        self.assertEqual(app.dialogs, 0)
        self.assertEqual(app.auto_closed, [])


class TestIdleSnooze(unittest.TestCase):
    """
    提醒节流（纯逻辑）。

    为什么需要：多个程序会在不同时刻陆续越过阈值，不节流的话你刚关掉一个清单、
    过几分钟又被另一个弹一次；一个长期挂着的程序在「稍后再说」之后也会周期性再弹。
    """

    def test_fresh_watcher_may_always_notify(self):
        """**还从来没提醒过**时，任何时候都该允许 —— 别把哨兵值当时间戳。"""
        w = core.IdleWatcher()
        for now in (0.0, 1.0, 100.0, 1e9):
            with self.subTest(now=now):
                self.assertTrue(w.may_notify(now))

    def test_silent_until_the_interval_elapses(self):
        w = core.IdleWatcher(snooze=3600.0)
        w.note_notified(1000.0)
        self.assertFalse(w.may_notify(1000.0))
        self.assertFalse(w.may_notify(4599.0))
        self.assertTrue(w.may_notify(4600.0))

    def test_never_again_mode(self):
        """"本次运行只提醒一次"：弹过就不再自动弹（手动清单仍然可用）。"""
        w = core.IdleWatcher(snooze=-1.0)
        self.assertTrue(w.may_notify(5.0))
        w.note_notified(5.0)
        for now in (6.0, 1e6, 1e12):
            with self.subTest(now=now):
                self.assertFalse(w.may_notify(now))

    def test_set_snooze_takes_effect(self):
        w = core.IdleWatcher(snooze=3600.0)
        w.note_notified(100.0)
        self.assertFalse(w.may_notify(200.0))
        w.set_snooze(60.0)
        self.assertTrue(w.may_notify(200.0))

    def test_default_and_choices(self):
        labels = [label for label, _s in core.IDLE_SNOOZE_CHOICES]
        self.assertIn("1 小时", labels)
        self.assertIn("本次运行只提醒一次", labels)
        self.assertIn("每天只提醒一次", labels)
        self.assertEqual(core.IDLE_SNOOZE_DEFAULT, 3600.0)

    def test_daily_choice_uses_its_own_sentinel(self):
        """
        「每天只提醒一次」必须是**独立哨兵值**（0.0），不能借"86400 秒"表示。

        借秒数就变成了"满 24 小时"：早上 8 点提醒过之后，第二天 8 点前什么都不说，
        第二天整天也可能只提醒一次 —— 与用户说的"每天一次"（日历上的每一天）不符。
        """
        self.assertEqual(core.IDLE_SNOOZE_DAILY, 0.0)
        self.assertEqual(core.IDLE_SNOOZE_RUN_ONCE, -1.0)
        self.assertEqual(core.idle_snooze_seconds_of("每天只提醒一次"), core.IDLE_SNOOZE_DAILY)
        self.assertNotEqual(core.IDLE_SNOOZE_DAILY, core.IDLE_SNOOZE_RUN_ONCE)

    def test_label_round_trip(self):
        for label, secs in core.IDLE_SNOOZE_CHOICES:
            with self.subTest(label=label):
                self.assertEqual(core.idle_snooze_label(secs), label)
                self.assertEqual(core.idle_snooze_seconds_of(label), float(secs))

    def test_unknown_label_falls_back_to_default(self):
        self.assertEqual(core.idle_snooze_seconds_of("胡说"), core.IDLE_SNOOZE_DEFAULT)
        self.assertEqual(core.idle_snooze_label(123.0), "1 小时")

    def test_hint_wording_matches_the_mode(self):
        """
        状态栏那句说明必须按档位给不同说法 —— 两种新档位不是"间隔"，
        拼成「自动提醒间隔 每天只提醒一次」读着别扭，也没说清下次什么时候提醒。
        """
        self.assertIn("自动提醒间隔", core.idle_snooze_hint(1800.0))
        self.assertIn("明天", core.idle_snooze_hint(core.IDLE_SNOOZE_DAILY))
        self.assertIn("本次运行", core.idle_snooze_hint(core.IDLE_SNOOZE_RUN_ONCE))
        self.assertNotIn("间隔", core.idle_snooze_hint(core.IDLE_SNOOZE_DAILY))
        self.assertNotIn("间隔", core.idle_snooze_hint(core.IDLE_SNOOZE_RUN_ONCE))

    def test_snooze_does_not_affect_the_idle_accounting(self):
        """节流只管"要不要弹窗"，不该影响"闲置了多久"的判定。"""
        w = core.IdleWatcher(threshold=10.0, snooze=99999.0)
        w.note_notified(0.5)
        self.assertEqual(w.observe(11.0, {1: samp()}), [1])


class TestIdleDailySnooze(unittest.TestCase):
    """
    「每天只提醒一次」的判定（纯逻辑，`today` 由调用方给，所以不用等真的过夜）。

    为什么要有这一档：按小时的节流在"一整个白天挂着不用"的场景下还是会弹好几次；
    用户想要的是"今天说过了，明天再说"。
    """

    DAY1, DAY2 = "2026-10-11", "2026-10-12"

    def test_first_popup_is_always_allowed(self):
        """
        从来没弹过时，第一拍就该放行 —— 否则这一档会让功能整个哑掉。
        这条是"用哨兵值（`last_popup_day`）当日期"最容易踩的坑。
        """
        w = core.IdleWatcher(snooze=core.IDLE_SNOOZE_DAILY)
        self.assertTrue(w.may_notify(0.0, self.DAY1))

    def test_same_day_is_throttled(self):
        w = core.IdleWatcher(snooze=core.IDLE_SNOOZE_DAILY)
        w.note_popup(self.DAY1)
        w.note_notified(10.0, self.DAY1)
        for now in (10.0, 3600.0, 23 * 3600.0):
            with self.subTest(now=now):
                self.assertFalse(w.may_notify(now, self.DAY1),
                                 "同一天内不该再自动弹窗")

    def test_a_new_day_allows_it_again(self):
        w = core.IdleWatcher(snooze=core.IDLE_SNOOZE_DAILY)
        w.note_popup(self.DAY1)
        w.note_notified(10.0, self.DAY1)
        self.assertTrue(w.may_notify(11.0, self.DAY2),
                        "只过了 1 秒，但日期翻篇了 —— 这一档看的是日期，不是秒数")

    def test_a_status_only_day_does_not_suppress_tomorrow(self):
        """
        今天只提示过、没弹过窗（比如跨零点时才越线，被前一天的状态提示挡住了）——
        明天**照常**该有一次弹窗机会。

        这一条守的是"别把结构式节流做成永久静音"：`last_notice_day` 只是"今天交代过了"，
        它不该顺手把明天也一起关掉。
        """
        w = core.IdleWatcher(snooze=core.IDLE_SNOOZE_DAILY)
        w.note_notified(10.0, self.DAY1)         # 只有状态栏提示（被节流的那几拍）
        self.assertTrue(w.may_notify(11.0, self.DAY2),
                        "没弹过窗的那天不该把第二天也一起挡掉")
        self.assertFalse(w.may_notify(12.0, self.DAY1),
                         "但同一天内仍然不能再弹")
        # 第二天真的弹过之后，第二天自己就不该再弹了
        w.note_popup(self.DAY2)
        w.note_notified(13.0, self.DAY2)
        self.assertFalse(w.may_notify(14.0, self.DAY2))

    def test_tomorrow_is_not_blocked_by_todays_popup(self):
        """今天弹过 → 明天照常放行（`last_popup_day != today` 就是为这一条）。"""
        w = core.IdleWatcher(snooze=core.IDLE_SNOOZE_DAILY)
        w.note_popup(self.DAY1)
        w.note_notified(10.0, self.DAY1)
        self.assertTrue(w.may_notify(11.0, self.DAY2))

    def test_missing_day_never_pops_up(self):
        """没给日期时**一律不放行** —— 宁可少弹一次，也不要"以为拦住了其实没拦"。"""
        w = core.IdleWatcher(snooze=core.IDLE_SNOOZE_DAILY)
        w.note_popup(self.DAY1)
        w.note_notified(10.0, self.DAY1)
        self.assertFalse(w.may_notify(1e9, ""))

    def test_open_dialog_never_gets_a_second_popup(self):
        w = core.IdleWatcher(snooze=3600.0)
        self.assertFalse(w.may_notify(0.0, "", dialog_open=True),
                         "清单已经开着时不该再叠一个")
        daily = core.IdleWatcher(snooze=core.IDLE_SNOOZE_DAILY)
        daily.note_popup(self.DAY1)
        self.assertFalse(daily.may_notify(0.0, self.DAY2, dialog_open=True))

    def test_calling_set_snooze_every_tick_keeps_the_throttle(self):
        """
        界面**每 2 秒**就会把下拉框当前值推一次给 `set_snooze`。

        若它"每次都清空日期记录"，每日档的节流就整个失效 —— 第二拍又会弹一次窗
        （当时实测到的现象：同一天里弹了两次）。
        """
        w = core.IdleWatcher(snooze=core.IDLE_SNOOZE_DAILY)
        w.note_popup(self.DAY1)
        w.note_notified(10.0, self.DAY1)
        for _ in range(5):                       # 模拟后续每一拍
            w.set_snooze(core.IDLE_SNOOZE_DAILY)
        self.assertFalse(w.may_notify(20.0, self.DAY1),
                         "同一档位重复推送不该把当天的节流取消")

    def test_switching_back_to_an_interval_takes_effect_at_once(self):
        """
        从「每天只提醒一次」换回「30 分钟」时，不该被当天残留的日期记录挡住 ——
        用户会看到"我刚改成 30 分钟，它却说今天不再弹"。
        """
        w = core.IdleWatcher(snooze=core.IDLE_SNOOZE_DAILY)
        w.note_popup(self.DAY1)
        w.note_notified(100.0, self.DAY1)
        self.assertFalse(w.may_notify(200.0, self.DAY1))
        w.set_snooze(1800.0)
        self.assertEqual(w.last_popup_day, "", "换档时要清掉「今天弹过」的记录")
        self.assertEqual(w.last_notice_day, "", "换档时连「今天交代过」的记录也要清掉")
        self.assertFalse(w.may_notify(200.0, self.DAY1))
        # 单调时钟那份时间戳保留着：换档后按新间隔算，而不是"立刻再弹一次"
        self.assertFalse(w.may_notify(100.0 + 1799.0, self.DAY1),
                         "换档后仍然按新间隔算（没满 30 分钟）")
        self.assertTrue(w.may_notify(100.0 + 1800.0, self.DAY1))


class TestIdleSnoozeWiring(unittest.TestCase):
    """`_check_idle` 里的节流接线。"""

    def _samples(self, pid=777):
        return {pid: samp(name="idle.exe")}

    def test_second_batch_within_the_window_does_not_pop_up_again(self):
        """
        第一波越线 → 弹清单；节流窗口内第二波越线 → **只更新状态栏**。
        这正是要解决的烦人场景。
        """
        app = _IdleHarness(enabled=True)
        app.idle_snooze_choice = _Var("1 小时")
        with mock.patch.object(core, "idle_samples", return_value=self._samples(1)), \
                mock.patch.object(winmon.time, "monotonic", return_value=1000.0):
            app._check_idle(31 * 60)
        self.assertEqual(app.dialogs, 1)
        # 第二波：节流窗口内
        with mock.patch.object(core, "idle_samples", return_value=self._samples(2)), \
                mock.patch.object(winmon.time, "monotonic", return_value=1100.0):
            app._check_idle(31 * 60)
        self.assertEqual(app.dialogs, 1, "节流窗口内又弹了一次")
        self.assertTrue(any("闲置清单" in s for s in app.status),
                        "被节流时应当在状态栏说明去哪儿看")

    def test_pops_up_again_after_the_window(self):
        app = _IdleHarness(enabled=True)
        app.idle_snooze_choice = _Var("1 小时")
        with mock.patch.object(core, "idle_samples", return_value=self._samples(1)), \
                mock.patch.object(winmon.time, "monotonic", return_value=1000.0):
            app._check_idle(31 * 60)
        with mock.patch.object(core, "idle_samples", return_value=self._samples(2)), \
                mock.patch.object(winmon.time, "monotonic", return_value=5000.0):
            app._check_idle(31 * 60)
        self.assertEqual(app.dialogs, 2, "过了节流窗口应当再提醒一次")

    def test_auto_close_mode_is_not_throttled(self):
        """自动关闭是"到点就办"，不该被提醒节流挡住。"""
        app = _IdleHarness(enabled=True, auto_close=True)
        app.idle_snooze_choice = _Var("4 小时")
        with mock.patch.object(core, "idle_samples", return_value=self._samples(1)), \
                mock.patch.object(winmon.time, "monotonic", return_value=1000.0):
            app._check_idle(31 * 60)
        with mock.patch.object(core, "idle_samples", return_value=self._samples(2)), \
                mock.patch.object(winmon.time, "monotonic", return_value=1010.0):
            app._check_idle(31 * 60)
        self.assertEqual(app.auto_closed, [[1], [2]], "自动关闭被节流挡住了")

    def test_auto_close_mode_does_not_re_close_every_tick(self):
        """
        自动关闭只处理**这一拍刚越线**的那批，不该每一拍都把清单里的人再关一遍。

        为什么值得单独钉：`observe()` 只在"刚越过阈值"时返回 pid，
        而"要不要动手"必须绑定在这上面 —— 改成"清单非空就动手"的话，
        对方还没退出（比如正在弹"要不要保存"）就会被每 2 秒重发一次 WM_CLOSE。
        """
        app = _IdleHarness(enabled=True, auto_close=True)
        with mock.patch.object(core, "idle_samples", return_value=self._samples(1)), \
                mock.patch.object(winmon.time, "monotonic", return_value=1000.0):
            app._check_idle(31 * 60)
        self.assertEqual(app.auto_closed, [[1]])
        for _ in range(3):                      # 后续几拍：它还在清单里，但不再越线
            with mock.patch.object(core, "idle_samples", return_value=self._samples(1)), \
                    mock.patch.object(winmon.time, "monotonic", return_value=1010.0):
                app._check_idle(2.0)
        self.assertEqual(app.auto_closed, [[1]], "每一拍都在重复关闭同一个程序")

    def test_the_dialog_still_shows_up_while_the_clock_is_throttled(self):
        """
        被节流的那几拍**只更新状态栏**，但清单本身仍然要看得到。

        为什么同时钉两条：这一档改的是"要不要打扰你"，不是"还算不算闲置" ——
        若顺手把 `candidates()` 也拦掉，用户点「闲置清单」会得到"现在没有闲置到阈值的程序"，
        与状态栏刚说的"有 N 个程序闲置超过…"自相矛盾。
        """
        app = _IdleHarness(enabled=True, snooze=3600.0)
        with mock.patch.object(core, "idle_samples", return_value=self._samples(1)), \
                mock.patch.object(winmon.time, "monotonic", return_value=1000.0):
            app._check_idle(31 * 60)
        self.assertEqual(app.dialogs, 1)
        # 第二波越线（被节流）：只提示、不弹窗，但清单里必须有它
        with mock.patch.object(core, "idle_samples", return_value=self._samples(2)), \
                mock.patch.object(winmon.time, "monotonic", return_value=1100.0):
            app._check_idle(31 * 60)
        self.assertEqual(app.dialogs, 1)
        pids = [r["pid"] for r in app.idle_watch.candidates()]
        self.assertIn(2, pids, f"被节流时清单里少了刚越线的程序：{pids}")

    def test_interval_mode_can_fire_again_with_realistic_ticks(self):
        """
        **真 bug 的回归判据**（2026-10-13 抓到并修掉）：真实节奏（每 2 秒一拍）下，
        间隔档必须还能等到下一轮。

        以前 `_check_idle` **每一拍**都把 `note_notified(now)` 推一次，
        而那个时刻正是间隔档比较的对象 —— 于是"1 小时"永远凑不满：
        第一波越线弹一次之后再也不会自动弹（除非清单整整一小时都是空的）。
        上面那条 `test_pops_up_again_after_the_window` 之所以没抓到，
        是因为它只调用两次、中间直接把时钟从 1000 跳到 5000（看起来过了 4000 秒）；
        生产里中间那一千多拍会把时刻一路刷新到当下。
        """
        app = _IdleHarness(enabled=True, snooze=3600.0)
        clock = {"now": 1000.0}
        with mock.patch.object(core, "idle_samples", return_value=self._samples(1)), \
                mock.patch.object(winmon.time, "monotonic",
                                  side_effect=lambda: clock["now"]):
            app._check_idle(31 * 60)              # 第一波越线 → 弹一次
            self.assertEqual(app.dialogs, 1)
            for _ in range(1800):                 # 1 小时 = 1800 拍 × 2 秒
                clock["now"] += 2.0
                app._check_idle(2.0)
        self.assertEqual(app.dialogs, 2,
                         "过了整个节流窗口都没再提醒 —— '上次提醒时刻'被每一拍刷新了")

    def test_throttled_ticks_do_not_push_the_clock_forward(self):
        """
        被节流的那几拍**只能更新状态栏**，不许动节流用的时刻。

        这是上一条 bug 的最小形态：拍数越多，时刻被推得越远。
        """
        app = _IdleHarness(enabled=True, snooze=3600.0)
        with mock.patch.object(core, "idle_samples", return_value=self._samples(1)), \
                mock.patch.object(winmon.time, "monotonic", return_value=1000.0):
            app._check_idle(31 * 60)
        stamp = app.idle_watch.last_notice
        for t in (1100.0, 1200.0, 1300.0):
            with mock.patch.object(core, "idle_samples", return_value=self._samples(1)), \
                    mock.patch.object(winmon.time, "monotonic", return_value=t):
                app._check_idle(2.0)
        self.assertEqual(app.idle_watch.last_notice, stamp,
                         "被节流的拍子不该推进 last_notice")
        self.assertTrue(any("闲置清单" in s for s in app.status[-1:]),
                        "被节流时仍然要在状态栏说明")

    # ---- 「每天只提醒一次」的接线（日期由 `winmon._local_day()` 给，测试里直接换掉它） ----

    def _daily_app(self, day=None):
        """配置成每日档的假 app，外加一个"当前是哪一天"的可改状态。"""
        state = {"day": day or "2026-10-11"}
        app = _IdleHarness(enabled=True, snooze=core.IDLE_SNOOZE_DAILY)
        app.day = lambda: state["day"]
        return app, state

    def test_daily_mode_pops_up_once_a_day(self):
        """
        同一天只弹一次；跨过零点、又有新程序越线时才再弹。

        「过了一整天」在测试里表现为"日期字符串换了"，不需要真的等 24 小时 ——
        这也是把日期交给调用方传进来的原因（`IdleWatcher` 本身不读墙上时钟）。
        """
        app, state = self._daily_app()
        with mock.patch.object(core, "idle_samples", return_value=self._samples(1)), \
                mock.patch.object(winmon, "_local_day", app.day), \
                mock.patch.object(winmon.time, "monotonic", return_value=1000.0):
            app._check_idle(31 * 60)
        self.assertEqual(app.dialogs, 1, "第一波越线应当弹清单")

        # 同一天的第二波：只提示，不弹
        with mock.patch.object(core, "idle_samples", return_value=self._samples(2)), \
                mock.patch.object(winmon, "_local_day", app.day), \
                mock.patch.object(winmon.time, "monotonic", return_value=2000.0):
            app._check_idle(31 * 60)
        self.assertEqual(app.dialogs, 1, "同一天内不该再弹")
        self.assertTrue(any("明天" in s for s in app.status),
                        f"应当说明今天不再弹：{app.status[-1:]}")

        # 第二天：新程序越线 → 再弹一次
        state["day"] = "2026-10-12"
        with mock.patch.object(core, "idle_samples", return_value=self._samples(3)), \
                mock.patch.object(winmon, "_local_day", app.day), \
                mock.patch.object(winmon.time, "monotonic", return_value=3000.0):
            app._check_idle(31 * 60)
        self.assertEqual(app.dialogs, 2, "新的一天应当再提醒一次")

    def test_daily_mode_does_not_rely_on_elapsed_seconds(self):
        """
        每日档看的是**日期翻篇**，不是"满了多少秒"：只过 1 秒但日期变了就该放行。

        这是它与「4 小时」这种固定间隔的本质区别（后者 1 秒内绝不会再弹）。
        """
        app, state = self._daily_app()
        with mock.patch.object(core, "idle_samples", return_value=self._samples(1)), \
                mock.patch.object(winmon, "_local_day", app.day), \
                mock.patch.object(winmon.time, "monotonic", return_value=1000.0):
            app._check_idle(31 * 60)
        state["day"] = "2026-10-12"
        with mock.patch.object(core, "idle_samples", return_value=self._samples(2)), \
                mock.patch.object(winmon, "_local_day", app.day), \
                mock.patch.object(winmon.time, "monotonic", return_value=1001.0):
            app._check_idle(31 * 60)
        self.assertEqual(app.dialogs, 2)


class TestLocalDay(unittest.TestCase):
    """`winmon._local_day()`：本地日历日（每日档的判据来源）。"""

    def test_format_is_a_calendar_day(self):
        day = winmon._local_day()
        self.assertRegex(day, r"^\d{4}-\d{2}-\d{2}$")
        self.assertEqual(day, time.strftime("%Y-%m-%d"))

    def test_explicit_timestamp_is_used(self):
        """给了时间戳就用它 —— 否则跨零点那条用例没法确定性地测。"""
        ts = time.mktime((2026, 3, 4, 12, 0, 0, 0, 0, -1))
        self.assertEqual(winmon._local_day(ts), "2026-03-04")


class TestIdleCli(unittest.TestCase):
    """命令行 `--idle`：只看不动手，退出码 0。"""

    def _run(self, args, timeout=60.0):
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "utf-8"
        return subprocess.run(
            [sys.executable, os.path.join(h.PROJECT_DIR, "winmon.py")] + args,
            cwd=h.PROJECT_DIR, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            timeout=timeout, env=env)

    def test_lists_and_never_touches_anything(self):
        got = self._run(["--idle", "--idle-minutes", "1"])
        text = got.stdout.decode("utf-8", "replace")
        self.assertEqual(got.returncode, 0, text)
        self.assertIn("只看不动手", text)
        self.assertIn("有可见窗口的程序", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
