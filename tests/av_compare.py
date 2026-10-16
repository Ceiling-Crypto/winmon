# -*- coding: utf-8 -*-
"""
对**指定输出设备**逐个测量音频延迟（不改系统默认设置）。

为什么需要：`WAVE_MAPPER` 开的是"当前默认设备"，所以对比不同设备时
必须切默认设备 —— 而切默认设备有风险（当时就曾把用户切到虚拟设备导致没声音）。
改用 `waveOutOpen` 的 **设备 ID** 直接指定输出设备，就能在不改系统设置的前提下
逐个测量、横向对比。

用法：
    py tests/av_compare.py --list               :: 列出可用的 waveOut 设备
    py tests/av_compare.py --device 0 1 2       :: 对指定 ID 的设备逐个测量
    py tests/av_compare.py --device 0 1 2 -n 3  :: 每个设备测 3 次
"""
import ctypes
import ctypes.wintypes as w
import statistics
import sys
import time

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, __import__("os").path.dirname(__import__("os").path.abspath(__file__)))
sys.path.insert(0, __import__("os").path.dirname(__import__("os").path.dirname(
    __import__("os").path.abspath(__file__))))

from av_latency_test import (CALLBACK_NULL, RATE, WAVEHDR,  # noqa: E402
                             find_onset, make_click, wf)

winmm = ctypes.WinDLL("winmm")
WHDR_DONE = 0x00000001
WAVE_MAPPER = ctypes.c_uint(-1).value


class WAVEOUTCAPS(ctypes.Structure):
    _fields_ = [("wMid", w.WORD), ("wPid", w.WORD), ("vDriverVersion", w.DWORD),
                ("szPname", ctypes.c_wchar * 32), ("dwFormats", w.DWORD),
                ("wChannels", w.WORD), ("wReserved1", w.WORD),
                ("dwSupport", w.DWORD)]


def list_devices():
    n = winmm.waveOutGetNumDevs()
    out = []
    for i in range(n):
        caps = WAVEOUTCAPS()
        if winmm.waveOutGetDevCapsW(i, ctypes.byref(caps), ctypes.sizeof(caps)) == 0:
            out.append((i, caps.szPname, caps.wChannels))
    return out


def measure_on(device_id, duration=1.2, lead=0.25):
    """对指定输出设备测一次。返回 (延迟ms, 说明)。"""
    fmt = wf()
    hwin = w.HANDLE()
    if winmm.waveInOpen(ctypes.byref(hwin), WAVE_MAPPER, ctypes.byref(fmt),
                        0, 0, CALLBACK_NULL) != 0:
        return None, "麦克风打不开"
    hout = w.HANDLE()
    rc = winmm.waveOutOpen(ctypes.byref(hout), device_id, ctypes.byref(fmt),
                           0, 0, CALLBACK_NULL)
    if rc != 0:
        winmm.waveInClose(hwin)
        return None, f"输出设备 {device_id} 打不开 rc={rc}"
    try:
        nsamples = int(RATE * duration)
        inbuf = ctypes.create_string_buffer(nsamples * 2)
        inhdr = WAVEHDR()
        inhdr.lpData = ctypes.cast(inbuf, ctypes.c_char_p)
        inhdr.dwBufferLength = nsamples * 2
        winmm.waveInPrepareHeader(hwin, ctypes.byref(inhdr), ctypes.sizeof(inhdr))
        winmm.waveInAddBuffer(hwin, ctypes.byref(inhdr), ctypes.sizeof(inhdr))

        out_bytes = b"\x00\x00" * int(RATE * lead) + make_click()
        outbuf = ctypes.create_string_buffer(out_bytes, len(out_bytes))
        outhdr = WAVEHDR()
        outhdr.lpData = ctypes.cast(outbuf, ctypes.c_char_p)
        outhdr.dwBufferLength = len(out_bytes)
        winmm.waveOutPrepareHeader(hout, ctypes.byref(outhdr), ctypes.sizeof(outhdr))

        winmm.waveInStart(hwin)
        winmm.waveOutWrite(hout, ctypes.byref(outhdr), ctypes.sizeof(outhdr))

        deadline = time.perf_counter() + duration + 1.0
        while time.perf_counter() < deadline:
            if inhdr.dwFlags & WHDR_DONE:
                break
            time.sleep(0.01)
        winmm.waveInStop(hwin)

        raw = inbuf.raw[:inhdr.dwBytesRecorded or nsamples * 2]
        import array
        samples = array.array("h")
        samples.frombytes(raw[:len(raw) // 2 * 2])
        onset = find_onset(samples)
        if onset is None:
            return None, "没录到咔声"
        onset_ms = onset / RATE * 1000.0
        return onset_ms - lead * 1000.0, f"起点 {onset_ms:.1f} ms"
    finally:
        try:
            winmm.waveOutReset(hout)
            winmm.waveOutClose(hout)
        except Exception:
            pass
        try:
            winmm.waveInClose(hwin)
        except Exception:
            pass


def main():
    argv = sys.argv[1:]
    devs = list_devices()
    print("可用的 waveOut 输出设备：")
    for i, name, ch in devs:
        print(f"   [{i}] {name}  ({ch}ch)")

    if "--list" in argv or "--device" not in argv:
        print("\n用法：py tests\\av_compare.py --device <id> [<id> ...] [-n 次数]")
        print("建议：选一个物理输出（扬声器）与一个蓝牙耳机，各测 3 次对比。")
        return 0

    ids = []
    i = argv.index("--device") + 1
    while i < len(argv) and not argv[i].startswith("-"):
        try:
            ids.append(int(argv[i]))
        except ValueError:
            break
        i += 1
    times = 3
    if "-n" in argv:
        try:
            times = int(argv[argv.index("-n") + 1])
        except (ValueError, IndexError):
            pass
    if not ids:
        print("没给出设备 ID")
        return 2

    print(f"\n开始测量（每个设备 {times} 次）。请保持安静；测蓝牙时耳机对准麦克风。")
    summary = {}
    for did in ids:
        name = next((n for i2, n, _c in devs if i2 == did), f"设备{did}")
        print(f"\n=== [{did}] {name} ===")
        vals = []
        for k in range(times):
            lat, note = measure_on(did)
            if lat is None:
                print(f"   第 {k + 1} 次失败：{note}")
                if k == 0:
                    break
            else:
                vals.append(lat)
                print(f"   第 {k + 1} 次：{lat:7.1f} ms  （{note}）")
            time.sleep(0.8)
        if vals:
            # 去掉明显离群的（环境噪声常造成 +400ms 以上的跳动）
            clean = [v for v in vals if v < statistics.median(vals) + 250]
            med = statistics.median(clean or vals)
            summary[did] = (name, med, len(clean))
            print(f"   → 中位延迟 {med:.1f} ms（{len(clean)}/{len(vals)} 次有效）")

    if len(summary) >= 2:
        print("\n" + "=" * 62)
        print("对比结论（数值越小越好；含麦克风侧固定开销，看**差值**更准）：")
        base_id = min(summary)
        base_name, base_med, _n = summary[base_id]
        print(f"   基准 [{base_id}] {base_name}: {base_med:.1f} ms")
        for did, (name, med, _n2) in summary.items():
            if did == base_id:
                continue
            delta = med - base_med
            verdict = ("几乎没有额外延迟 → 该设备不是主因"
                       if abs(delta) < 50 else
                       f"**额外 {delta:+.0f} ms** → 这个差值就是病因所在的量级")
            print(f"   [{did}] {name}: {med:.1f} ms（{delta:+.1f} ms） {verdict}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
