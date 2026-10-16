# -*- coding: utf-8 -*-
"""
A8 —— 音频/A/V 测量工具的**自校验**。

背景：当时为了排查"音画不同步"，写了个用麦克风测音频延迟的工具。
第一版**测不准**（同一设备三次结果 283/477/720ms，抖动 ±400ms），
根因有两个，都是代码问题而不是环境问题：
  1) 用"全局最大能量峰"定位咔声起点 → 会被回声骗到
  2) 归一化公式少除以 n → 相关系数被放大 √96 倍 → 纯噪声也能刷出"命中"分

修好之后先用**合成信号**把检测器标定准（真值已知），再去测真实设备。
这组测试就是那套标定，防止以后改坏。
"""

from __future__ import annotations

import array
import os
import random
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from av_latency_test import (CLICK_AMP, CLICK_MS, RATE,  # noqa: E402
                             find_onset, make_click)


def synth(true_ms=None, echo_ms=None, echo_gain=1.0, noise=0.0,
          duration_ms=1200, seed=7):
    """
    合成一段录音。true_ms=None 表示"不放咔声"（纯噪声）。
    seed 固定，保证可重复。
    """
    rnd = random.Random(seed)
    n = int(RATE * duration_ms / 1000)
    buf = [int(rnd.gauss(0, noise)) if noise else 0 for _ in range(n)]
    click = array.array("h", make_click())
    if true_ms is not None:
        at = int(RATE * true_ms / 1000)
        for k, v in enumerate(click):
            if at + k < n:
                buf[at + k] += v
    if echo_ms is not None:
        at2 = int(RATE * echo_ms / 1000)
        for k, v in enumerate(click):
            if at2 + k < n:
                buf[at2 + k] += int(v * echo_gain * 0.6)
    out = array.array("h")
    for v in buf:
        out.append(max(-32768, min(32767, v)))
    return out


def detected_ms(sig):
    idx = find_onset(sig)
    return None if idx is None else idx / RATE * 1000.0


class TestOnsetDetector(unittest.TestCase):
    """咔声起点检测：真值已知的合成信号。"""

    def test_clean_signal(self):
        self.assertAlmostEqual(detected_ms(synth(true_ms=300)), 300.0, delta=8.0)

    def test_survives_echo_louder_than_click(self):
        """
        回声比原声还大时也要定位到**原声**。

        第一版就是被这个骗到：它取全局最大峰，回声更大就报到回声上。
        现在取"最早的可信峰"，天然避开回声。
        """
        got = detected_ms(synth(true_ms=300, echo_ms=700, echo_gain=1.5))
        self.assertAlmostEqual(got, 300.0, delta=8.0)

    def test_survives_noise(self):
        for sigma in (400, 800, 1600, 3000):
            with self.subTest(sigma=sigma):
                got = detected_ms(synth(true_ms=350, noise=sigma))
                self.assertAlmostEqual(got, 350.0, delta=8.0)

    def test_survives_noise_and_echo(self):
        got = detected_ms(synth(true_ms=280, echo_ms=800, echo_gain=1.2, noise=600))
        self.assertAlmostEqual(got, 280.0, delta=8.0)

    def test_early_click_near_skip_window(self):
        """咔声出现得比较早（接近跳过窗口）时也要抓住。"""
        got = detected_ms(synth(true_ms=120, noise=400, echo_ms=700))
        self.assertAlmostEqual(got, 120.0, delta=10.0)

    def test_pure_noise_returns_none(self):
        """
        没有咔声时必须返回 None，不能瞎报一个位置。

        这条是"阈值标定"的核心：归一化算对之前，纯噪声能刷到 0.33 分，
        看起来就像命中（实测误报到 17ms / 678ms / 109ms）。
        """
        for sigma in (200, 400, 800, 1600, 3000):
            with self.subTest(sigma=sigma):
                self.assertIsNone(detected_ms(synth(true_ms=None, noise=sigma)),
                                  f"σ={sigma} 的纯噪声被误判成咔声")

    def test_empty_and_tiny_input(self):
        self.assertIsNone(find_onset(array.array("h")))
        self.assertIsNone(find_onset(array.array("h", [0] * 10)))


class TestClickTemplate(unittest.TestCase):
    """咔声模板本身的形状要适合定位（陡峭起跳、快速收尾）。"""

    def test_template_is_short_and_loud_at_start(self):
        click = array.array("h", make_click())
        self.assertGreater(len(click), 10)
        # 长度应当就是 CLICK_MS（长了既拖慢相关、也不利于定位"起点"）
        self.assertAlmostEqual(len(click) / RATE * 1000, CLICK_MS, delta=0.1)
        self.assertLess(CLICK_MS, 10.0, "模板过长会拖慢相关计算")
        # 起点幅度 = CLICK_AMP（**刻意不做满幅**，避免扬声器/麦克风削顶导致失真）
        head = max(abs(v) for v in click[:max(1, len(click) // 8)])
        self.assertAlmostEqual(head, CLICK_AMP, delta=CLICK_AMP * 0.1)
        self.assertLess(CLICK_AMP, 32767, "不要用满幅，留出头room")

    def test_template_is_deterministic(self):
        self.assertEqual(bytes(make_click()), bytes(make_click()))


if __name__ == "__main__":
    unittest.main(verbosity=2)
