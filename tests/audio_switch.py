# -*- coding: utf-8 -*-
"""
音频端点切换 + 配置查看工具（内置测试用，也是给 A/V 不同步做 A/B 验证的工具）。

用法：
    py tests/audio_switch.py --list                :: 列出输出端点与默认设备
    py tests/audio_switch.py --set "网易"           :: 把默认输出切到名字含"网易"的端点
    py tests/audio_switch.py --set "Realtek"       :: 切回 Realtek 扬声器

实现：Windows 没有公开的"设置默认音频设备"API，标准做法是 IPolicyConfig
（未公开的 COM 接口，通过 vtable 调用）。这里用纯 ctypes 实现，不引入第三方库。
IPolicyConfig 的 vtable（Win7 起稳定）：
    0-2 IUnknown, 3 GetMixFormat, 4 GetDeviceFormat, 5 ResetDeviceFormat,
    6 SetDeviceFormat, 7 GetProcessingPeriod, 8 SetProcessingPeriod,
    9 GetShareMode, 10 SetShareMode, 11 GetPropertyValue, 12 SetPropertyValue,
    13 SetDefaultEndpoint, 14 SetEndpointVisibility
"""
import ctypes
import ctypes.wintypes as w
import sys
import uuid

sys.stdout.reconfigure(encoding="utf-8")

ole32 = ctypes.WinDLL("ole32")


class GUID(ctypes.Structure):
    _fields_ = [("d1", w.DWORD), ("d2", w.WORD), ("d3", w.WORD),
                ("d4", ctypes.c_ubyte * 8)]

    def __init__(self, s=""):
        super().__init__()
        if s:
            u = uuid.UUID(s)
            self.d1, self.d2, self.d3 = u.time_low, u.time_mid, u.time_hi_version
            self.d4 = (ctypes.c_ubyte * 8)(*u.bytes[8:])

    def __str__(self):
        return (f"{{{self.d1:08X}-{self.d2:04X}-{self.d3:04X}-"
                f"{''.join(f'{b:02X}' for b in self.d4[:2])}-"
                f"{''.join(f'{b:02X}' for b in self.d4[2:])}}}")


CLSID_MMDeviceEnumerator = GUID("{BCDE0395-E52F-467C-8E3D-C4579291692E}")
IID_IMMDeviceEnumerator = GUID("{A95664D2-9614-4F35-A746-DE8DB63617E6}")
CLSID_CPolicyConfigClient = GUID("{870AF99C-171D-4F9E-AF0D-E63DF40C2BC9}")
IID_IPolicyConfig = GUID("{F8679F50-850A-41CF-9C72-430F290290C8}")

PKEY_FriendlyName = GUID("{A45C254E-DF1C-4EFD-8020-67D146A850E0}")
PID_FRIENDLY = 14

eRender = 0
DEVICE_STATE_ACTIVE = 0x1
HRESULT = ctypes.c_long


def vt(ptr, index, restype, *argtypes):
    vtbl = ctypes.cast(ptr, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    return ctypes.WINFUNCTYPE(restype, ctypes.c_void_p, *argtypes)(vtbl[index])


def friendly_name(dev) -> str:
    store = ctypes.c_void_p()
    vt(dev, 4, HRESULT, ctypes.c_void_p,
       ctypes.POINTER(ctypes.c_void_p))(dev, 0, ctypes.byref(store))
    if not store.value:
        return ""
    key = GUID()
    key.d1, key.d2, key.d3 = PKEY_FriendlyName.d1, PKEY_FriendlyName.d2, PKEY_FriendlyName.d3
    key.d4 = PKEY_FriendlyName.d4
    pkey = (ctypes.c_byte * 20)()
    ctypes.memmove(pkey, ctypes.byref(key), 16)
    ctypes.memmove(ctypes.addressof(pkey) + 16, ctypes.byref(w.DWORD(PID_FRIENDLY)), 4)
    pv = (ctypes.c_byte * 32)()
    ctypes.memset(pv, 0, 32)
    if vt(store, 5, HRESULT, ctypes.c_void_p, ctypes.c_void_p)(
            store, ctypes.byref(pkey), ctypes.byref(pv)) != 0:
        return ""
    if ctypes.c_ushort.from_address(ctypes.addressof(pv)).value == 31:
        p = ctypes.c_void_p.from_address(ctypes.addressof(pv) + 8).value
        return ctypes.c_wchar_p(p).value if p else ""
    return ""


def device_id(dev) -> str:
    p = ctypes.c_void_p()
    if vt(dev, 5, HRESULT, ctypes.POINTER(ctypes.c_void_p))(dev, ctypes.byref(p)) != 0:
        return ""
    if not p.value:
        return ""
    s = ctypes.c_wchar_p(p.value).value or ""
    ole32.CoTaskMemFree(p)
    return s


def endpoints(enum):
    coll = ctypes.c_void_p()
    vt(enum, 3, HRESULT, ctypes.c_int, ctypes.c_int,
       ctypes.POINTER(ctypes.c_void_p))(enum, eRender, DEVICE_STATE_ACTIVE,
                                        ctypes.byref(coll))
    out = []
    if not coll.value:
        return out
    count = ctypes.c_int(0)
    vt(coll, 3, HRESULT, ctypes.POINTER(ctypes.c_int))(coll, ctypes.byref(count))
    for i in range(count.value):
        dev = ctypes.c_void_p()
        vt(coll, 4, HRESULT, ctypes.c_int,
           ctypes.POINTER(ctypes.c_void_p))(coll, i, ctypes.byref(dev))
        if dev.value:
            out.append((friendly_name(dev), device_id(dev), dev))
    return out


def default_endpoint(enum, role=0):
    dev = ctypes.c_void_p()
    vt(enum, 4, HRESULT, ctypes.c_int, ctypes.c_int,
       ctypes.POINTER(ctypes.c_void_p))(enum, eRender, role, ctypes.byref(dev))
    return friendly_name(dev) if dev.value else ""


def make_policy():
    pol = ctypes.c_void_p()
    hr = ole32.CoCreateInstance(ctypes.byref(CLSID_CPolicyConfigClient), None, 1,
                                ctypes.byref(IID_IPolicyConfig), ctypes.byref(pol))
    return (pol, hr)


def set_default(dev_id: str) -> tuple[bool, str]:
    pol, hr = make_policy()
    if hr != 0 or not pol.value:
        return False, f"CoCreateInstance(CPolicyConfigClient) 失败 hr={hr}"
    ok = True
    msgs = []
    # 三个角色：0=eConsole, 1=eMultimedia, 2=eCommunications
    for role in (0, 1, 2):
        try:
            hr2 = vt(pol, 13, HRESULT, ctypes.c_wchar_p, ctypes.c_int)(
                pol, dev_id, role)
        except Exception as exc:
            return False, f"SetDefaultEndpoint 异常：{type(exc).__name__}: {exc}"
        if hr2 != 0:
            ok = False
        msgs.append(f"role{role}:hr={hr2}")
    return ok, " ".join(msgs)


def main():
    argv = sys.argv[1:]
    ole32.CoInitializeEx(None, 2)
    enum = ctypes.c_void_p()
    ole32.CoCreateInstance(ctypes.byref(CLSID_MMDeviceEnumerator), None, 1,
                           ctypes.byref(IID_IMMDeviceEnumerator), ctypes.byref(enum))
    if not enum.value:
        print("拿不到设备枚举器")
        return 1

    if "--set" in argv:
        key = argv[argv.index("--set") + 1]
        target = next((e for e in endpoints(enum) if key.lower() in e[0].lower()), None)
        if target is None:
            print(f"没找到名字含 {key!r} 的活动输出端点")
            print("可选：")
            for name, _id, _d in endpoints(enum):
                print(f"   {name}")
            return 2
        print(f"把默认输出切到：{target[0]}")
        ok, msg = set_default(target[1])
        print(("[OK] " if ok else "[失败] ") + msg)
        print(f"切换后默认（普通）: {default_endpoint(enum, 0)!r}")
        print(f"切换后默认（通信）: {default_endpoint(enum, 2)!r}")
        return 0 if ok else 3

    print(f"默认输出（普通）: {default_endpoint(enum, 0)!r}")
    print(f"默认输出（通信）: {default_endpoint(enum, 2)!r}")
    print("\n活动输出端点：")
    for name, did, _d in endpoints(enum):
        print(f"   {name}")
        print(f"      {did}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
