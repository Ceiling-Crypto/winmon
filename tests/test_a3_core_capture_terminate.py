# -*- coding: utf-8 -*-
"""
A3 —— 核心层：画面捕获、图标 PNG、进程终止。

覆盖 HANDOFF.md 里这些结论：
  · 自检 5 项中的「480×300 捕获 100% 非零 / 画面渲染 11ms / 图标 PNG / 终止进程」
  · GDI StretchBlt 在 C 层完成缩放（Python 侧只处理裸像素）
  · 图标是自己写的 PNG 编码器（不引 Pillow），所以必须校验 PNG 结构合法
  · 终止进程用 TerminateProcess（不依赖 WaitForSingleObject）

抓屏测试优先用**自己开的窗口**当目标：这样尺寸、内容都可控，
不依赖桌面上恰好有什么程序（那种测试会随环境时好时坏）。
"""

from __future__ import annotations

import base64
import ctypes
import os
import sys
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import _helpers as h                     # noqa: E402
import winmon_core as core               # noqa: E402


# ===========================================================================
# 画面捕获
# ===========================================================================
class TestWindowCapturer(unittest.TestCase):
    """直接用自己开的 Tk 窗口当目标，保证有窗口可抓。"""

    @classmethod
    def setUpClass(cls):
        ok, why = h.tk_state()
        cls.tk_why = why
        cls.root = None
        if not ok:
            return
        import tkinter as tk
        root = tk.Tk()
        root.title("WinMon-Capture-Target")
        root.geometry("420x280+60+60")
        cv = tk.Canvas(root, width=420, height=280, bg="#ffffff",
                       highlightthickness=0)
        cv.pack(fill="both", expand=True)
        cv.create_rectangle(20, 20, 200, 150, fill="#d32f2f", outline="")
        cv.create_rectangle(220, 120, 400, 260, fill="#1565c0", outline="")
        cv.create_text(210, 30, text="WinMon", fill="#000000")
        root.update()
        root.update_idletasks()
        time.sleep(0.4)
        cls.root = root
        cls.hwnd = core.H(core.user32.GetAncestor(core.w.HWND(root.winfo_id()),
                                                  core.GA_ROOT))
        rc = core.w.RECT()
        core.user32.GetWindowRect(core.w.HWND(cls.hwnd), ctypes.byref(rc))
        cls.win_w = rc.right - rc.left
        cls.win_h = rc.bottom - rc.top
        cls.cap = core.WindowCapturer()

    @classmethod
    def tearDownClass(cls):
        if getattr(cls, "cap", None):
            cls.cap.release()
        if getattr(cls, "root", None):
            try:
                cls.root.destroy()
            except Exception:
                pass

    def setUp(self):
        if self.root is None:
            self.skipTest(self.tk_why)

    def test_own_window_is_capturable(self):
        raw, ow, oh = self.cap.grab_window(self.hwnd, self.win_w, self.win_h, 480, 300)
        # 输出尺寸会被夹到"请求尺寸"与"源尺寸"的较小者（窗口可能没到 480 宽）
        self.assertEqual(ow, min(480, self.win_w))
        self.assertEqual(oh, min(300, self.win_h))
        self.assertEqual(len(raw), ow * oh * 3, "BGR 裸像素长度应为 宽*高*3")
        # 阈值取 90%：窗口里画了一块黑色文字，蓝色分量为 0 的像素天然存在；
        # 真正"抓黑了"的失败长这样：非零比例 < 5%。
        self.assertGreater(h.nonzero_ratio(raw), 90.0,
                           "自己画的彩色窗口抓出来却是大面积黑 —— 抓屏链路有问题")

    def test_output_size_clamped_to_source(self):
        """输出尺寸会被裁到不超过源尺寸（避免放大出无意义的大图）。"""
        raw, ow, oh = self.cap.grab_window(self.hwnd, 100, 80, 9999, 9999)
        self.assertLessEqual(ow, 100)
        self.assertLessEqual(oh, 80)
        self.assertEqual(len(raw), ow * oh * 3)

    def test_tiny_size_has_floor(self):
        raw, ow, oh = self.cap.grab_window(self.hwnd, self.win_w, self.win_h, 1, 1)
        self.assertGreaterEqual(ow, 16)
        self.assertGreaterEqual(oh, 16)
        self.assertEqual(len(raw), ow * oh * 3)

    def test_content_matches_drawn_colors(self):
        """
        抓到的画面必须真的来自那个窗口。

        做法：左上 1/4 区域应该以红色为主，右下区域应该以蓝色为主。
        这比"非零比例"强得多 —— 它能发现"抓的是别的窗口/整屏"这类错误。
        """
        raw, ow, oh = self.cap.grab_window(self.hwnd, self.win_w, self.win_h, 240, 160)

        def avg(px0, py0, px1, py1):
            r = g = b = n = 0
            for y in range(py0, py1):
                row = y * ow * 3
                for x in range(px0, px1):
                    i = row + x * 3
                    b += raw[i]
                    g += raw[i + 1]
                    r += raw[i + 2]
                    n += 1
            return r / n, g / n, b / n

        r1, g1, b1 = avg(30, 30, 100, 70)          # 左上：红色块
        self.assertGreater(r1, g1 + 30, f"左上区域不是红色调：{(r1, g1, b1)}")
        self.assertGreater(r1, b1 + 30, f"左上区域不是红色调：{(r1, g1, b1)}")

        r2, g2, b2 = avg(150, 100, 220, 150)       # 右下：蓝色块
        self.assertGreater(b2, r2 + 30, f"右下区域不是蓝色调：{(r2, g2, b2)}")
        self.assertGreater(b2, g2 + 30, f"右下区域不是蓝色调：{(r2, g2, b2)}")

    def test_screen_capture(self):
        pw, ph = core.get_physical_screen_size()
        self.assertGreater(pw, 0)
        self.assertGreater(ph, 0)
        raw, ow, oh = self.cap.grab_screen(pw, ph, 320, 200)
        self.assertEqual((ow, oh), (320, 200))
        self.assertEqual(len(raw), ow * oh * 3)
        self.assertGreater(h.nonzero_ratio(raw), 5.0, "整屏抓取几乎全黑")

    def test_release_is_idempotent(self):
        cap = core.WindowCapturer()
        raw, _, _ = cap.grab_window(self.hwnd, self.win_w, self.win_h, 160, 120)
        self.assertTrue(raw)
        cap.release()
        cap.release()                               # 重复释放不应抛异常


class TestCapturePerformance(unittest.TestCase):
    """自检里的"画面渲染 11ms"：Python 侧像素生成必须够快。"""

    def test_bgr_to_hex_data_throughput(self):
        w_, h_ = 240, 150                            # 480×300 半尺寸抽样后的规格
        raw = bytes((i * 7) % 256 for i in range(w_ * h_ * 3))
        t0 = time.perf_counter()
        data = core.bgr_to_tk_hex_data(raw, w_ * 2, h_ * 2, 2)
        dt_ms = (time.perf_counter() - t0) * 1000
        # 输出格式：每行 "{#rrggbb #rrggbb ...}"，行间以空格分隔
        self.assertTrue(data.startswith("{#"), f"输出格式不对：{data[:20]!r}")
        self.assertEqual(data.count("{"), h_, "行数应等于抽样后的高度")
        self.assertLess(dt_ms, 120.0,
                        f"单帧像素串生成耗时 {dt_ms:.1f}ms，超过 120ms（自检基准约 11ms）")

    def test_sample_scale_reduces_pixels(self):
        w_, h_ = 40, 30
        raw = bytes([7]) * (w_ * h_ * 3)
        full = core.bgr_to_tk_hex_data(raw, w_, h_, 1)
        half = core.bgr_to_tk_hex_data(raw, w_, h_, 2)
        self.assertEqual(full.count("#"), w_ * h_)
        self.assertEqual(half.count("#"), ((w_ + 1) // 2) * ((h_ + 1) // 2))
        self.assertAlmostEqual(len(half) / len(full), 0.25, delta=0.05,
                               msg="scale=2 应把生成量降到约 1/4")


# ===========================================================================
# 图标（自己写的 PNG 编码器）
# ===========================================================================
class TestPngEncoding(unittest.TestCase):
    def test_encoder_output_is_valid_png(self):
        w_, h_ = 8, 6
        rgba = bytearray()
        for y in range(h_):
            for x in range(w_):
                rgba += bytes([x * 30 % 256, y * 40 % 256, 128, 255])
        png = core.encode_png_rgba(bytes(rgba), w_, h_)
        info = h.parse_png(png)
        self.assertEqual((info["width"], info["height"]), (w_, h_))
        self.assertEqual(info["bit_depth"], 8)
        self.assertEqual(info["color_type"], 6, "应为 RGBA(6)")
        self.assertEqual(info["chunks"][0], "IHDR")
        self.assertEqual(info["chunks"][-1], "IEND")
        self.assertIn("IDAT", info["chunks"])

    def test_corrupt_png_is_detected(self):
        """反向确认：解析器真的在校验 CRC（否则上面的测试就成了摆设）。"""
        png = bytearray(core.encode_png_rgba(bytes(4 * 4 * 4), 4, 4))
        png[20] ^= 0xFF                              # 破坏 IHDR 内容
        with self.assertRaises(ValueError):
            h.parse_png(bytes(png))


class TestIconExtraction(unittest.TestCase):
    def test_icon_from_existing_file(self):
        png_b64 = core.icon_from_file(sys.executable, 32)
        self.assertTrue(png_b64, f"从 {sys.executable} 取不到图标")
        png = base64.b64decode(png_b64)
        info = h.parse_png(png)
        self.assertEqual((info["width"], info["height"]), (32, 32))

    def test_icon_missing_file_returns_empty(self):
        self.assertEqual(core.icon_from_file(r"C:\definitely\missing\x.exe", 32), "")

    def test_hicon_zero_returns_empty(self):
        self.assertEqual(core.hicon_to_base64_png(0, 32), "")

    def test_icon_cache_is_stable(self):
        """同一个窗口取两次必须完全一致（走缓存），且是合法 PNG。"""
        target = h.pick_capture_target()
        if target is None:
            self.skipTest("桌面上没有合适的窗口")
        cache = core.IconCache(24)
        first = cache.get(target)
        second = cache.get(target)
        self.assertEqual(first, second)
        if first:
            self.assertEqual(h.parse_png(base64.b64decode(first))["width"], 24)
        cache.forget_hwnd(target.hwnd)
        self.assertEqual(cache.get(target), first, "忘记后重取应当得到同样的图标")

    def test_icon_cache_clear(self):
        cache = core.IconCache(24)
        cache.clear()                                # 不应抛异常


# ===========================================================================
# 终止进程
# ===========================================================================
class TestTerminateProcess(unittest.TestCase):
    def test_terminate_sleep_child(self):
        proc = h.spawn_cleanup(self, h.sleep_child())
        self.assertTrue(h.wait_alive(proc.pid), "测试子进程没起来")
        ok, msg, err = core.terminate_process(proc.pid)
        self.assertTrue(ok, f"结束失败：{msg}（错误码 {err}）")
        self.assertEqual(err, 0)
        self.assertTrue(h.wait_dead(proc.pid), "进程还在")
        proc.wait(timeout=5)

    def test_invalid_pid(self):
        ok, msg, err = core.terminate_process(0)
        self.assertFalse(ok)
        self.assertEqual(err, 87)
        ok2, _, _ = core.terminate_process(-5)
        self.assertFalse(ok2)

    def test_nonexistent_pid(self):
        ok, msg, err = core.terminate_process(0xFFFF0000)
        self.assertFalse(ok, "结束一个不存在的进程不应报告成功")

    def test_already_dead_pid(self):
        proc = h.spawn_cleanup(self, h.sleep_child(seconds=5))
        self.assertTrue(h.wait_alive(proc.pid))
        proc.kill()
        proc.wait(timeout=5)
        self.assertTrue(h.wait_dead(proc.pid))
        ok, _msg, _err = core.terminate_process(proc.pid)
        self.assertFalse(ok, "已退出的进程不应报告结束成功")

    def test_critical_process_list_is_not_empty(self):
        """安全保护用的黑名单必须有内容（界面层依赖它）。"""
        for name in ("csrss.exe", "winlogon.exe", "lsass.exe", "services.exe",
                     "svchost.exe", "dwm.exe"):
            self.assertIn(name, core.CRITICAL_PROCESSES)
        self.assertIn("explorer.exe", core.RISKY_PROCESSES)


class TestProcessTreeTermination(unittest.TestCase):
    def test_terminate_tree_kills_children(self):
        """
        结束进程树：父进程与孙进程都必须消失。

        构造两代子进程（python -> python），验证 core 的 BFS 确实覆盖到孙进程。
        """
        code = ("import subprocess, sys, time;"
                "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)']);"
                "time.sleep(120)")
        import subprocess
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        parent = h.spawn_cleanup(self, subprocess.Popen(
            [sys.executable, "-c", code], stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, creationflags=flags))
        self.assertTrue(h.wait_alive(parent.pid), "父测试进程没起来")

        # 找出它的子进程（轮询，等孙进程真的被创建）
        procs = {}
        children = []
        for _ in range(40):
            procs = core.snapshot_processes()
            children = [p for p, info in procs.items() if info["ppid"] == parent.pid]
            if children:
                break
            time.sleep(0.1)
        self.assertTrue(children, "没找到父进程的子进程，测试前提不成立")

        ok_count, failures = core.terminate_process_tree(parent.pid, procs)
        self.assertGreaterEqual(ok_count, 1, f"一个都没结束：{failures}")
        for pid in [parent.pid] + children:
            self.assertTrue(h.wait_dead(pid), f"PID {pid} 没被结束")
        parent.wait(timeout=5)

    def test_access_denied_is_reported_not_swallowed(self):
        """
        「拒绝访问」也必须进失败清单（2026-10-13 修的真 bug）。

        以前那句 `elif "拒绝访问" not in msg` 会把它滤掉：界面只显示"成功 1/4"、
        操作历史也只记这个数，用户完全不知道为什么少了三个 ——
        而"权限不够"恰恰是**有行动价值**的提示（用管理员身份再试）。
        """
        procs = {1: {"name": "protected.exe", "ppid": 0},
                 2: {"name": "ok.exe", "ppid": 1}}
        messages = {1: (False, "拒绝访问：目标进程权限更高，请以管理员身份运行本程序后重试。", 5),
                    2: (True, "已强制结束", 0)}

        def fake_terminate(pid):
            return messages[pid]

        with mock.patch.object(core, "terminate_process", side_effect=fake_terminate):
            ok_count, failures = core.terminate_process_tree_multi([1, 2], procs)
        self.assertEqual(ok_count, 1)
        self.assertEqual(len(failures), 1, f"被拒绝的那个必须报出来：{failures}")
        self.assertIn("protected.exe", failures[0])
        self.assertIn("拒绝访问", failures[0])


# ===========================================================================
# 音频会话枚举：**已删除**（2026-10-13，定稿决定）
# ===========================================================================
#
# 原 `core.enum_audio_sessions()` 用 MMDevice/IAudioSessionManager2 枚举音频会话，
# 想拿"某进程是否在出声"当判定信号。实测**在本机恒返回 0 个会话**
# （连自己造的出声源正在放时也是 0），等于一条永远不成立的证据 ——
# 留着只会误导（开发中真的被它带偏过一次）。所以整块删掉了，
# 连带的 `VideoActivityDetector.audio_peak()` 与 `idle_samples` 的 `audio` 字段一起去掉。
# 明确：**以后要重写再单独决议**。这里保留一条用例防止它被"顺手加回来"：


class TestAudioSessionsRemoved(unittest.TestCase):
    def test_enum_audio_sessions_is_gone(self):
        self.assertFalse(hasattr(core, "enum_audio_sessions"),
                         "这个函数已被删除；要重写请先和用户确认（它会误导判定）")

    def test_video_detector_no_longer_reports_audio_peak(self):
        self.assertFalse(hasattr(core.VideoActivityDetector, "audio_peak"))

    def test_idle_samples_has_no_audio_field(self):
        import inspect
        src = inspect.getsource(core.idle_samples)
        self.assertNotIn('"audio"', src,
                         "闲置判定的样本里不该再有 audio 字段")


if __name__ == "__main__":
    unittest.main(verbosity=2)
