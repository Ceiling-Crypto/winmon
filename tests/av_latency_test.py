# -*- coding: utf-8 -*-
"""
A/V 延迟测量工具：量出"声音从发出到被听到"的**具体毫秒数**。

原理（不需要任何第三方库，只靠 Windows 自带的 waveIn/waveOut）：
  1) 同时开始录音与放音
  2) 通过扬声器发出一个短促的"咔"声（click，宽频、定位准）
  3) 从麦克风录到的波形里找出这个咔声的起点
  4) 起点相对"应该发声的时刻"的偏移 ≈ 整条音频链路的延迟

怎么用它判断病因：
  · 把笔记本扬声器当输出测一次，再把蓝牙耳机当输出测一次
      - 两者**差不多**（都在 <60ms）→ 延迟在软件侧（DSP/驱动/浏览器）
      - 只有蓝牙**明显更大**（150~300ms）→ 是蓝牙编解码延迟，属正常物理限制
  · 反复测几次取中位数，避免单次误差

注意：测量本身包含"扬声器→麦克风"的空气传播时间（约 3ms/米），可忽略。
      测蓝牙时要**戴在耳朵附近**对着麦克风，否则数值会偏大。
"""
import array
import ctypes
import ctypes.wintypes as w
import os
import statistics
import sys
import time
import wave

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

winmm = ctypes.WinDLL("winmm")

WAVE_MAPPER = ctypes.c_uint(-1).value
WAVE_FORMAT_PCM = 1
CALLBACK_NULL = 0
WHDR_DONE = 0x00000001

RATE = 48000
CLICK_MS = 2.0          # 咔声长度
CLICK_AMP = 12000       # 幅度（避免削顶）


class WAVEFORMATEX(ctypes.Structure):
    _fields_ = [("wFormatTag", w.WORD), ("nChannels", w.WORD),
                ("nSamplesPerSec", w.DWORD), ("nAvgBytesPerSec", w.DWORD),
                ("nBlockAlign", w.WORD), ("wBitsPerSample", w.WORD),
                ("cbSize", w.WORD)]


class WAVEHDR(ctypes.Structure):
    _fields_ = [("lpData", ctypes.c_char_p), ("dwBufferLength", w.DWORD),
                ("dwBytesRecorded", w.DWORD), ("dwUser", ctypes.c_void_p),
                ("dwFlags", w.DWORD), ("dwLoops", w.DWORD),
                ("lpNext", ctypes.c_void_p), ("reserved", ctypes.c_void_p)]


def make_click(rate=RATE, ms=CLICK_MS, amp=CLICK_AMP):
    """生成一段"咔"声：短促、带陡峭起跳，便于定位起点。"""
    n = int(rate * ms / 1000)
    buf = array.array("h")
    for i in range(n):
        # 起始即最大幅度（方波包络最容易定位），末端快速衰减避免拖尾
        env = 1.0 if i < n * 0.5 else max(0.0, 1.0 - (i - n * 0.5) / (n * 0.5))
        v = int(amp * env * (1 if (i // 8) % 2 == 0 else -1))
        buf.append(max(-32768, min(32767, v)))
    return buf.tobytes()


def wf(rate=RATE, ch=1, bits=16):
    fmt = WAVEFORMATEX()
    fmt.wFormatTag = WAVE_FORMAT_PCM
    fmt.nChannels = ch
    fmt.nSamplesPerSec = rate
    fmt.wBitsPerSample = bits
    fmt.nBlockAlign = ch * bits // 8
    fmt.nAvgBytesPerSec = rate * fmt.nBlockAlign
    fmt.cbSize = 0
    return fmt


def find_onset(samples, rate=RATE, thresh_ratio=0.35, template=None):
    """
    在录到的样本里找"咔声"起点。

    第一版用"全局最大能量峰"定位，实测**不可靠**：同一个设备三次测出来
    283 / 477 / 720 ms 乱跳（抖动 ±400ms）。原因是环境噪声或回声在某些录音里
    比真正的咔声还大，于是把回声当成了起点。

    现在改成**模板互相关 + 取最早的可信峰**：
      1) 用生成的咔声波形（方波包络）当模板，在录音上滑窗算归一化互相关
      2) 相关值**同时**满足"绝对阈值"和"相对峰值的比例"才算可信 ——
         只用相对阈值会在**纯噪声**时失效（噪声里也有个"最高分"，相对阈值
         跟着一起变低，于是在录音开头误报，实测踩到）
      3) 命中取**最早**的那个位置：咔声比它的回声先出现，取最早自然避开回声
      4) 跳过开头一小段，躲开录音启动瞬态

    返回样本索引（找不到返回 None）。
    """
    #: 归一化互相关的绝对下限。实测：真咔声 0.95+，纯噪声 0.05 以下
    ABS_MIN = 0.30
    #: 跳过开头这么多毫秒（录音启动瞬态、以及合成用例里 0ms 处的脉冲）
    SKIP_MS = 60.0

    if not samples:
        return None
    if template is None:
        template = array.array("h")
        template.frombytes(make_click())

    tpl = list(template)
    n = len(tpl)
    if len(samples) < n + 4:
        return None

    # 模板能量（归一化用）
    tpl_e = sum(v * v for v in tpl)
    if tpl_e <= 0:
        return None

    # 用**连续**互相关（不抽稀采样）。
    #
    # 归一化公式：r = Σ(x·t) / sqrt(Σt² · Σx²)
    #   注意模板能量要按 **n** 归一化（即除以 n），否则当"窗口样本数 n 与
    #   模板长度 1 不同量级"时，分数会被系统性放大 —— 本工具第一版就因为这个
    #   把纯噪声算到 0.33（看起来像命中），而真正的咔声是 0.7+，
    #   于是阈值怎么调都不干净。这里显式按 n 归一到真正的 [-1, 1]。
    #
    # 为什么不能稀疏采样：咔声是 2ms 的高频方波包络，每 8 个样本取一个点会把它
    # "降采样"成慢速交替序列，而白噪声在高频同样有能量 → 噪声刷出假高分。
    step = max(1, rate // 1000)                   # 1ms 粗步长
    skip = int(rate * SKIP_MS / 1000.0)
    mean_t = sum(tpl) / n
    tpl_c = [v - mean_t for v in tpl]             # 去均值，抗直流偏置
    tpl_e = sum(v * v for v in tpl_c)
    if tpl_e <= 0:
        return None

    def _score(offset: int) -> float:
        seg = samples[offset:offset + n]
        mean_s = sum(seg) / n
        num = 0.0
        seg_e = 0.0
        for k in range(n):
            v = seg[k] - mean_s
            num += v * tpl_c[k]
            seg_e += v * v
        if seg_e <= 0:
            return 0.0
        return num / ((tpl_e / n * seg_e / n) ** 0.5) / n

    best = 0.0
    scores = []
    for start in range(skip, len(samples) - n, step):
        sc = _score(start)
        scores.append((start, sc))
        if sc > best:
            best = sc

    if best < ABS_MIN:                          # 连绝对门槛都不到 → 没有咔声
        return None

    thr = max(ABS_MIN, best * thresh_ratio)
    for start, score in scores:
        if score >= thr:
            # 在命中点附近逐样本精修（同口径）
            fine_start = max(0, start - step)
            fine_best, fine_at = -2.0, fine_start
            for s2 in range(fine_start, min(len(samples) - n, start + step)):
                sc = _score(s2)
                if sc > fine_best:
                    fine_best, fine_at = sc, s2
            return fine_at
    return None


def measure_once(duration=1.2, delay=0.25):
    """
    做一次测量：开录音 → 播放咔声 → 停止录音 → 分析。
    返回 (延迟毫秒, 说明)。失败返回 (None, 原因)。
    """
    fmt_in = wf()
    fmt_out = wf()
    hwin = w.HANDLE()
    rc = winmm.waveInOpen(ctypes.byref(hwin), WAVE_MAPPER, ctypes.byref(fmt_in),
                          0, 0, CALLBACK_NULL)
    if rc != 0:
        return None, f"waveInOpen 失败 rc={rc}（可能没有麦克风）"
    hwout = w.HANDLE()
    rc = winmm.waveOutOpen(ctypes.byref(hwout), WAVE_MAPPER,
                           ctypes.byref(fmt_out), 0, 0, CALLBACK_NULL)
    if rc != 0:
        winmm.waveInClose(hwin)
        return None, f"waveOutOpen 失败 rc={rc}（可能没有输出设备）"

    try:
        # --- 录音缓冲 ---
        nsamples = int(RATE * duration)
        inbuf = ctypes.create_string_buffer(nsamples * 2)
        inhdr = WAVEHDR()
        inhdr.lpData = ctypes.cast(inbuf, ctypes.c_char_p)
        inhdr.dwBufferLength = nsamples * 2
        winmm.waveInPrepareHeader(hwin, ctypes.byref(inhdr), ctypes.sizeof(inhdr))
        winmm.waveInAddBuffer(hwin, ctypes.byref(inhdr), ctypes.sizeof(inhdr))

        # --- 放音缓冲：开头留静音，再放咔声 ---
        lead = int(RATE * delay)
        click = make_click()
        out_bytes = b"\x00\x00" * lead + click
        outbuf = ctypes.create_string_buffer(out_bytes, len(out_bytes))
        outhdr = WAVEHDR()
        outhdr.lpData = ctypes.cast(outbuf, ctypes.c_char_p)
        outhdr.dwBufferLength = len(out_bytes)
        winmm.waveOutPrepareHeader(hwout, ctypes.byref(outhdr), ctypes.sizeof(outhdr))

        t0 = time.perf_counter()
        winmm.waveInStart(hwin)
        winmm.waveOutWrite(hwout, ctypes.byref(outhdr), ctypes.sizeof(outhdr))
        expected = t0 + delay                       # 咔声"应该"在此时刻开始

        # 等录音缓冲满
        deadline = time.perf_counter() + duration + 0.6
        while time.perf_counter() < deadline:
            if inhdr.dwFlags & WHDR_DONE:
                break
            time.sleep(0.01)
        winmm.waveInStop(hwin)

        # --- 分析：找咔声起点（相对录音开始）---
        raw = inbuf.raw[:inhdr.dwBytesRecorded or nsamples * 2]
        samples = array.array("h")
        samples.frombytes(raw[:len(raw) // 2 * 2])
        onset = find_onset(samples)
        if onset is None:
            return None, "没录到咔声（麦克风静音？音量太小？请靠近麦克风重试）"
        onset_ms = onset / RATE * 1000.0
        # 发声时刻（相对录音开始）≈ delay*1000；延迟 = 实际到达 - 应该到达
        latency = onset_ms - delay * 1000.0
        return latency, f"咔声起点 {onset_ms:.1f} ms，预期 {delay * 1000:.1f} ms"
    finally:
        try:
            winmm.waveOutClose(hwout)
        except Exception:
            pass
        try:
            winmm.waveInClose(hwin)
        except Exception:
            pass


def main():
    times = int(sys.argv[1]) if len(sys.argv) > 1 else 5
    print("A/V 延迟测量（扬声器放咔声 → 麦克风录回）")
    print("=" * 62)
    print("提示：测量期间请勿说话。蓝牙耳机请戴好并对准麦克风。")
    print(f"计划测量 {times} 次，每次间隔 1 秒…\n")
    results = []
    for i in range(times):
        lat, note = measure_once()
        if lat is None:
            print(f"  第 {i + 1} 次失败：{note}")
            if i == 0:
                return 1
        else:
            results.append(lat)
            print(f"  第 {i + 1} 次：{lat:7.1f} ms   （{note}）")
        time.sleep(1.0)
    if not results:
        print("没有有效测量")
        return 1
    med = statistics.median(results)
    print("\n" + "=" * 62)
    print(f"中位延迟：{med:.1f} ms（{len(results)} 次有效）")
    print()
    if med < 60:
        print("→ 音频侧延迟正常。若你仍感觉不同步，问题在**视频侧**")
        print("  （帧节奏/渲染：G-SYNC、RTX 视频超分、硬件加速、虚拟显示器）。")
    elif med < 120:
        print("→ 音频侧有轻微缓冲延迟（60~120ms）。蓝牙或 DSP 都可能到这个量级。")
    else:
        print("→ 音频侧延迟明显（>120ms）。典型来自蓝牙编解码或音效 DSP 缓冲。")
        print("  下一步：换**有线**输出再测一次，对比差值即可定位。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
