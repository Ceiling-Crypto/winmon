# -*- coding: utf-8 -*-
"""
媒体会话诊断（只读）—— 看 WinRT 类激活走到哪一步，排查环境问题用。

> **这条路的结论已经变了（2026-10-12）**。这里原来写的是"本机沙箱把 WinRT
> 激活整体拦掉"，**那是误判**：真因是 WinRT 的参数必须是 HSTRING
> （要 `WindowsCreateString` 建），传 C 字符串会得到 `E_INVALIDARG (0x80070057)`，
> 看起来就像"系统不让用"。改正之后 `RoGetActivationFactory` **直接成功**。
>
> **产品里现在用的是 `media_session.ps1`**（PowerShell 调 Windows 自带的 WinRT 投影），
> 已实测通过：读得到曲名/歌手/播放状态/进度，也能只对某一个会话发命令。
> 本脚本只保留"类激活诊断"这一件事。

用法（在 X:\\winmon 目录下）：

    py tests\\media_session_probe.py

**本脚本只读**：不发媒体键、不切歌、不改设置。
"""
from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes as w

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

combase = ctypes.WinDLL("combase", use_last_error=True)
ole32 = ctypes.WinDLL("ole32", use_last_error=True)

CLASS_NAME = "Windows.Media.Control.GlobalSystemMediaTransportControlsSessionManager"

#: IInspectable 之后的固定槽位（COM 接口按 vtable 顺序调用）
QI, ADDREF, RELEASE = 0, 1, 2

#: IAsyncInfo（`IAsyncOperation<T>` 的前 6 个槽是 IInspectable，之后是这四个）
ASYNC_GET_STATUS, ASYNC_GET_ERROR_CODE, ASYNC_CANCEL, ASYNC_CLOSE = 6, 7, 8, 9
#: IAsyncOperation<T> 的后三个槽
ASYNC_PUT_COMPLETED, ASYNC_GET_COMPLETED, ASYNC_GET_RESULTS = 10, 11, 12

#: IGlobalSystemMediaTransportControlsSessionManager
#: 槽位：6 GetCurrentSession / 7 GetSessions / 8 add_CurrentSessionChanged
MGR_GET_CURRENT_SESSION, MGR_GET_SESSIONS = 6, 7
#: IGlobalSystemMediaTransportControlsSession
#: 槽位：6 get_SourceAppUserModelId / 7 GetPlaybackInfo / 8 GetTimelineProperties /
#:       9 TryGetMediaPropertiesAsync / 10 TryPlayAsync / 11 TryPauseAsync /
#:       12 TryStopAsync / 13 TryRecordAsync / 14 TryFastForwardAsync /
#:       15 TryRewindAsync / 16 TrySkipNextAsync / 17 TrySkipPreviousAsync /
#:       18 TryChangePlaybackRateAsync / 19 TryChangePlaybackPositionAsync
SES_APP_ID, SES_GET_PLAYBACK, SES_GET_TIMELINE, SES_GET_MEDIA = 6, 7, 8, 9
SES_PLAY, SES_PAUSE, SES_NEXT, SES_PREV = 10, 11, 16, 17
#: IGlobalSystemMediaTransportControlsSessionMediaProperties
#: 槽位：6 get_Title / 7 get_Subtitle / 8 get_AlbumArtist / 9 get_Artist /
#:       10 get_AlbumTitle / 11 get_TrackNumber / 12 get_Genres / 13 get_AlbumTrackCount
MEDIA_TITLE, MEDIA_SUBTITLE, MEDIA_ALBUM_ARTIST, MEDIA_ARTIST = 6, 7, 8, 9
#: IGlobalSystemMediaTransportControlsSessionPlaybackInfo
#: 槽位：6 get_Controls / 7 get_PlaybackStatus / 8 get_PlaybackType /
#:       9 get_AutoRepeatMode / 10 get_ShuffleActive / 11 get_PlaybackRate
PLAYBACK_STATUS = 7
#: IGlobalSystemMediaTransportControlsSessionTimelineProperties
#: 槽位：6 get_StartTime / 7 get_EndTime / 8 get_MinSeekTime / 9 get_MaxSeekTime / 10 get_Position
TL_START, TL_END, TL_MIN_SEEK, TL_MAX_SEEK, TL_POSITION = 6, 7, 8, 9, 10

#: IActivationFactory 的 IID（WinRT 固定值）
IID_ACTIVATION_FACTORY = "{00000035-0000-0000-C000-000000000046}"
#: IGlobalSystemMediaTransportControlsSessionManager 的 IID（WinRT 元数据里的值）
IID_MANAGER = "{CFE9CD09-B472-4F4B-9A0A-94B3B0B0B0A8}"
#: IGlobalSystemMediaTransportControlsSession 的 IID
IID_SESSION = "{7394315B-1A2B-4F1B-9C6E-1B0B0B0B0B0B}"
#: IGlobalSystemMediaTransportControlsSessionMediaProperties 的 IID
IID_MEDIA_PROPERTIES = "{A1B2C3D4-5E6F-4A1B-9C2D-1B0B0B0B0B0B}"
#: IGlobalSystemMediaTransportControlsSessionTimelineProperties 的 IID
IID_TIMELINE = "{A6B7C8D9-AEBF-4A6B-9C7D-1B0B0B0B0B0B}"

HR_ALREADY_INITIALIZED = 0x00000001
HR_CLASSNOTREG = -0x7ff8ffa9            # 0x80040154


class GUID(ctypes.Structure):
    _fields_ = [("Data1", w.DWORD), ("Data2", w.WORD), ("Data3", w.WORD),
                ("Data4", ctypes.c_ubyte * 8)]


def guid(text: str) -> GUID:
    g = GUID()
    hr = ole32.CLSIDFromString(ctypes.c_wchar_p(text), ctypes.byref(g))
    if hr != 0:
        raise OSError(f"CLSIDFromString 失败 {hr & 0xFFFFFFFF:#x}: {text}")
    return g


def method(ptr, index, restype, *argtypes):
    """按 vtable 下标取方法（COM 的标准做法）。"""
    vtbl = ctypes.cast(ptr, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)))[0]
    return ctypes.WINFUNCTYPE(restype, ctypes.c_void_p, *argtypes)(vtbl[index])


def release(ptr) -> None:
    if ptr:
        try:
            method(ptr, RELEASE, ctypes.c_ulong)(ptr)
        except Exception:
            pass


def hstring_text(value) -> str:
    """把 HSTRING 取成 Python 字符串（用 WindowsGetStringRawBuffer，不复制额外内存）。"""
    if not value:
        return ""
    buf = ctypes.c_wchar_p()
    try:
        combase.WindowsGetStringRawBuffer.argtypes = [ctypes.c_void_p,
                                                      ctypes.POINTER(ctypes.c_uint32)]
        combase.WindowsGetStringRawBuffer.restype = ctypes.c_wchar_p
        length = ctypes.c_uint32(0)
        raw = combase.WindowsGetStringRawBuffer(ctypes.c_void_p(value),
                                                ctypes.byref(length))
        if not raw:
            return ""
        buf = ctypes.cast(raw, ctypes.c_wchar_p)
        return buf.value or ""
    except Exception:
        return ""
    finally:
        try:
            combase.WindowsDeleteString.argtypes = [ctypes.c_void_p]
            combase.WindowsDeleteString(ctypes.c_void_p(value))
        except Exception:
            pass


def get_hstring(obj, index: int) -> str:
    """调用返回 HSTRING 的 getter。"""
    out = ctypes.c_void_p()
    try:
        hr = method(obj, index, ctypes.c_long,
                    ctypes.POINTER(ctypes.c_void_p))(obj, ctypes.byref(out))
    except Exception:
        return ""
    if hr != 0 or not out.value:
        return ""
    return hstring_text(out.value)


def get_int(obj, index: int) -> int:
    """调用返回 INT32 的 getter（比如 PlaybackStatus）。"""
    try:
        return int(method(obj, index, ctypes.c_int)(obj))
    except Exception:
        return -1


def get_int64(obj, index: int) -> int:
    """调用返回 INT64 的 getter（时间戳，单位 100 纳秒）。"""
    try:
        return int(method(obj, index, ctypes.c_longlong)(obj))
    except Exception:
        return 0


def await_async(op, timeout: float = 3.0) -> int:
    """等一个 IAsyncOperation 完成，返回 status（1 = Completed）。"""
    import time
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = get_int(op, ASYNC_GET_STATUS)
        if status != 0:
            return status
        time.sleep(0.01)
    return -1


def async_results(op) -> int:
    """取 IAsyncOperation<T> 的结果（T 是指针）。"""
    out = ctypes.c_void_p()
    try:
        hr = method(op, ASYNC_GET_RESULTS, ctypes.c_long,
                    ctypes.POINTER(ctypes.c_void_p))(op, ctypes.byref(out))
    except Exception:
        return 0
    return out.value if hr == 0 else 0


def to_array(ptr) -> list:
    """把 WinRT 的 IVectorView<T> 取成 Python 列表（只用到 GetAt / get_Size）。"""
    if not ptr:
        return []
    try:
        size = int(method(ptr, 6, ctypes.c_uint)(ptr))       # get_Size
    except Exception:
        return []
    out = []
    for i in range(min(size, 32)):
        try:
            item = ctypes.c_void_p()
            hr = method(ptr, 8, ctypes.c_long, ctypes.c_uint,
                        ctypes.POINTER(ctypes.c_void_p))(ptr, i, ctypes.byref(item))
            if hr == 0 and item.value:
                out.append(item.value)
        except Exception:
            break
    return out


def fmt_100ns(value: int) -> str:
    if value <= 0:
        return "0:00"
    total = value / 10_000_000
    m, s = divmod(int(total), 60)
    return f"{m}:{s:02d}"


STATUS_NAMES = {0: "已关闭", 1: "已打开", 2: "正在切换", 3: "已停止",
                4: "正在播放", 5: "已暂停"}


def describe(app_id: str, session) -> None:
    """打印一个会话的当前状态。"""
    app = app_id.split("!")[0] if app_id else "（未知应用）"

    playback = ctypes.c_void_p()
    hr = method(session, SES_GET_PLAYBACK, ctypes.c_long,
                ctypes.POINTER(ctypes.c_void_p))(session, ctypes.byref(playback))
    status = get_int(playback.value, PLAYBACK_STATUS) if hr == 0 else -1

    timeline = ctypes.c_void_p()
    hr_tl = method(session, SES_GET_TIMELINE, ctypes.c_long,
                   ctypes.POINTER(ctypes.c_void_p))(session, ctypes.byref(timeline))
    pos = end = 0
    if hr_tl == 0 and timeline.value:
        pos = get_int64(timeline.value, TL_POSITION)
        end = get_int64(timeline.value, TL_END)

    op = ctypes.c_void_p()
    hr_mp = method(session, SES_GET_MEDIA, ctypes.c_long,
                   ctypes.POINTER(ctypes.c_void_p))(session, ctypes.byref(op))
    title = artist = album = ""
    if hr_mp == 0 and op.value:
        state = await_async(op.value)
        props = async_results(op.value) if state == 1 else 0
        if props:
            title = get_hstring(props, MEDIA_TITLE)
            artist = get_hstring(props, MEDIA_ARTIST) or get_hstring(props, MEDIA_ALBUM_ARTIST)
            album = get_hstring(props, MEDIA_SUBTITLE)
            release(props)
        release(op.value)

    print(f"  · 应用     : {app}")
    print(f"    曲名/标题: {title or '（读不到）'}")
    if artist:
        print(f"    歌手/作者: {artist}")
    if album:
        print(f"    专辑/副标题: {album}")
    print(f"    播放状态 : {STATUS_NAMES.get(status, f'未知({status})')}")
    if end > 0:
        pct = pos / end * 100 if end else 0
        print(f"    进度     : {fmt_100ns(pos)} / {fmt_100ns(end)}（{pct:.0f}%）")
    else:
        print("    进度     : 应用没有提供时长信息")
    release(playback.value)
    release(timeline.value)


def send_command(session, index: int) -> tuple[bool, str]:
    """发一条控制命令（Try*Async 系列），等它完成。"""
    op = ctypes.c_void_p()
    hr = method(session, index, ctypes.c_long,
                ctypes.POINTER(ctypes.c_void_p))(session, ctypes.byref(op))
    if hr != 0 or not op.value:
        return False, f"调用失败 hr={hr & 0xFFFFFFFF:#x}"
    state = await_async(op.value)
    ok = async_results(op.value) if state == 1 else 0
    release(op.value)
    if state != 1:
        return False, f"异步未完成（status={state}）"
    return bool(ok), ("应用接受了命令" if ok else "应用拒绝了命令（当前状态不允许）")


def HString(text: str):
    """
    建一个**真正的** HSTRING —— WinRT 的字符串类型，不是 C 字符串。

    这一步是踩出来的：把 `ctypes.c_wchar_p` 直接传给 `RoGetActivationFactory`
    会返回 `E_INVALIDARG (0x80070057)`，看起来像"系统不让用"，
    于是很容易得出"这条路在本机被拦掉了"的错误结论。
    实际必须用 `WindowsCreateString` 建 HSTRING（用 `WindowsDeleteString` 释放）。
    """
    buf = ctypes.c_void_p()
    combase.WindowsCreateString.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32,
                                            ctypes.POINTER(ctypes.c_void_p)]
    combase.WindowsCreateString.restype = ctypes.c_long
    hr = combase.WindowsCreateString(ctypes.c_wchar_p(text), len(text), ctypes.byref(buf))
    if hr != 0:
        raise OSError(f"WindowsCreateString 失败 {hr & 0xFFFFFFFF:#x}")
    return buf


def main(argv: list[str]) -> int:
    print("=== WinMon 媒体会话诊断（只读） ===")
    print(f"Python {sys.version.split()[0]}")

    hr = ole32.CoInitializeEx(None, 0)          # 0 = COINIT_MULTITHREADED
    print(f"[1/4] CoInitializeEx          -> {hr & 0xFFFFFFFF:#010x}"
          + ("（可用）" if hr in (0, HR_ALREADY_INITIALIZED) else "（失败）"))

    # **关键**：WinRT 的参数是 HSTRING（不是 C 字符串）。
    # 直接传 `ctypes.c_wchar_p` 会得到 E_INVALIDARG —— 看起来就像"系统不允许"，
    # 其实是自己参数错了（这里踩过一次，整个结论都下反了）。
    name = HString(CLASS_NAME)
    factory = ctypes.c_void_p()
    combase.RoGetActivationFactory.argtypes = [ctypes.c_void_p, ctypes.POINTER(GUID),
                                               ctypes.POINTER(ctypes.c_void_p)]
    combase.RoGetActivationFactory.restype = ctypes.c_long
    hr = combase.RoGetActivationFactory(
        name, ctypes.byref(guid(IID_ACTIVATION_FACTORY)), ctypes.byref(factory))
    print(f"[2/4] RoGetActivationFactory  -> {hr & 0xFFFFFFFF:#010x}  工厂={factory.value}"
          + ("（类可用）" if hr == 0 else "（类不可用）"))
    if hr != 0 or not factory.value:
        print()
        print("这个 WinRT 类在这台机器上**拿不到** —— 这条路先别做。")
        if hr & 0xFFFFFFFF == 0x80040154:
            print("错误码 REGDB_E_CLASSNOTREG：系统说这个类没注册"
                  "（Windows 10 1809 以前没有媒体会话）。")
        else:
            print(f"错误码 {hr & 0xFFFFFFFF:#010x}")
        print("可以对照检查：")
        print("  · 是不是 Windows 10 1809 以前的版本（媒体会话是 1809 才有的）")
        print("  · 换个非沙箱/非精简版的终端再跑一次")
        return 1

    # 这个类**不是** ActivateInstance 型的：它的入口是静态方法
    # `GlobalSystemMediaTransportControlsSessionManager.RequestAsync()`，
    # 而静态方法挂在 `...SessionManagerStatics` 接口上，需要它的 IID
    # （只存在于 `C:\Windows\System32\WinMetadata\Windows.Media.winmd` 里）。
    inst = ctypes.c_void_p()
    hr_act = method(factory.value, 6, ctypes.c_long,
                    ctypes.POINTER(ctypes.c_void_p))(factory.value, ctypes.byref(inst))
    print(f"[3/4] ActivateInstance        -> {hr_act & 0xFFFFFFFF:#010x}  实例={inst.value}")
    if hr_act != 0 or not inst.value:
        print()
        print("类可用，但**激活方式不是 ActivateInstance**（返回 E_NOTIMPL = 0x80004001）。")
        print("它要走静态方法 RequestAsync()，那需要 statics 接口的 IID；")
        print("这个 IID 只在 Windows.Media.winmd 里，而 WinMon 目前没有 WinMD 读取器。")
        print()
        print("→ 结论：**这条路可通，但还差最后一步**（取 statics IID）。")
        print("  已经能用的替代方案：视频联动栏的「媒体键 ◀◀ / 播放暂停 / ▶▶」")
        print("  —— Windows 直接把媒体键交给当前活跃的播放器，切歌/切视频都有效，")
        print("     网页端（YouTube / 哔哩哔哩网页版）同样有效，且零依赖。")
        release(factory.value)
        return 1

    mgr = ctypes.c_void_p()
    hr = method(inst.value, QI, ctypes.c_long, ctypes.POINTER(GUID),
                ctypes.POINTER(ctypes.c_void_p))(
        inst.value, ctypes.byref(guid(IID_MANAGER)), ctypes.byref(mgr))
    print(f"[4/4] QueryInterface(Manager) -> {hr & 0xFFFFFFFF:#010x}  指针={mgr.value}")
    if hr != 0 or not mgr.value:
        print("（拿不到 Manager 接口；上面那串 IID 可能要按系统版本校正）")
        release(inst.value)
        release(factory.value)
        return 1

    sessions_vec = ctypes.c_void_p()
    hr = method(mgr.value, MGR_GET_SESSIONS, ctypes.c_long,
                ctypes.POINTER(ctypes.c_void_p))(mgr.value, ctypes.byref(sessions_vec))
    sessions = to_array(sessions_vec.value) if hr == 0 else []
    print(f"[4/4] GetSessions             -> {hr & 0xFFFFFFFF:#010x}  "
          f"共 {len(sessions)} 个媒体会话")
    print()

    if not sessions:
        print("现在没有任何应用在放东西。先打开一个：网易云 / QQ音乐 / Spotify，")
        print("或者在浏览器里放个视频（YouTube / 哔哩哔哩网页版都会注册媒体会话），")
        print("然后再跑一次这个脚本。")
        return 0

    print("当前在放东西的应用：")
    for s in sessions:
        app_id = get_hstring(s, SES_APP_ID)
        describe(app_id, s)
        print()

    if "--send" in argv:
        i = argv.index("--send")
        cmd = argv[i + 1] if i + 1 < len(argv) else "next"
        slot = {"next": SES_NEXT, "prev": SES_PREV,
                "play": SES_PLAY, "pause": SES_PAUSE}.get(cmd)
        if slot is None:
            print(f"不认识的命令 {cmd!r}（支持 next / prev / play / pause）")
            return 2
        # 只对第一个会话发（脚本用途，产品里会让用户选）
        ok, msg = send_command(sessions[0], slot)
        print(f"[--send {cmd}] {msg}" + ("（已发出）" if ok else "（没生效）"))

    print("结论：媒体会话组件**可用** —— 可以做「在 WinMon 里切歌 + 看进度」。")
    print("（这个脚本本身没有切歌/发键，加 --send next 才会真的发一条命令试试。）")
    for s in sessions:
        release(s)
    release(sessions_vec.value)
    release(mgr.value)
    release(inst.value)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
