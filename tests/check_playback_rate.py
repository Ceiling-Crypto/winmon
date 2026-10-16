# -*- coding: utf-8 -*-
"""
排查"视频被强制慢放"（例如 0.6×）——这才是音画不同步的真正原因线索。

为什么之前所有音频排查都无效：**慢放会让画面落后于音频**，
表现和"音频延迟"一模一样，但根因在播放器的播放速率上。
所以这类问题的第一步应该是"确认倍速是否正常"，而不是去测音频延迟。

本脚本查三处可能造成"强制非 1.0 倍速"的地方：
  1) 浏览器/站点保存的播放速率偏好（Local Storage / Preferences 文件）
  2) Edge 的媒体相关策略与实验开关
  3) 哔哩哔哩客户端的播放设置（同一账号同步的倍速偏好会跨端生效）

只读，不改任何配置。
"""
import json
import os
import re
import sys

sys.stdout.reconfigure(encoding="utf-8")

HOME = os.path.expanduser("~")
EDGE_USER = os.path.join(HOME, "AppData", "Local", "Microsoft", "Edge", "User Data")
BILI_ROAM = os.path.join(HOME, "AppData", "Roaming", "bilibili")

print("=" * 70)
print("1) Edge 里与『播放速率』相关的偏好（Local Storage / Preferences）")
print("=" * 70)
hits = 0
if os.path.isdir(EDGE_USER):
    for root, _dirs, files in os.walk(EDGE_USER):
        if "Local Storage" not in root and "Preferences" not in files:
            continue
        for f in files:
            if f not in ("Preferences",) and not f.endswith(".log"):
                continue
            path = os.path.join(root, f)
            try:
                if os.path.getsize(path) > 12 * 1024 * 1024:
                    continue
                text = open(path, "rb").read().decode("utf-8", "replace")
            except OSError:
                continue
            for m in re.finditer(r'"[^"]{0,40}(playback|speed|rate|ratechange)[^"]{0,40}"'
                                 r'\s*:\s*("?[\w.\-]+"?)', text, re.I):
                print(f"   {os.path.relpath(path, EDGE_USER)}: {m.group(0)[:110]}")
                hits += 1
                if hits > 25:
                    break
        if hits > 25:
            break
if hits == 0:
    print("   （没找到明显相关项；也可能是站点把倍速存在 IndexedDB 里，工具读不到）")

print()
print("=" * 70)
print("2) Edge 媒体相关策略 / 启动参数")
print("=" * 70)
import winreg
for path in (r"SOFTWARE\Policies\Microsoft\Edge",
             r"SOFTWARE\Microsoft\Edge"):
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, path) as k:
            n = winreg.QueryInfoKey(k)[1]
            print(f"   HKLM\\{path}: {n} 个值")
            for i in range(min(n, 15)):
                nm, v, _t = winreg.EnumValue(k, i)
                if any(t in nm.lower() for t in ("media", "play", "rate", "gpu",
                                                 "hardware", "accel")):
                    print(f"       {nm} = {v!r}")
    except OSError:
        print(f"   HKLM\\{path}: 不存在")

print()
print("=" * 70)
print("3) 哔哩哔哩客户端的播放设置（账号同步的倍速偏好会跨端生效）")
print("=" * 70)
if os.path.isdir(BILI_ROAM):
    found = 0
    for root, _dirs, files in os.walk(BILI_ROAM):
        for f in files:
            if not f.lower().endswith((".json", ".ini", ".cfg", ".conf")):
                continue
            path = os.path.join(root, f)
            try:
                if os.path.getsize(path) > 4 * 1024 * 1024:
                    continue
                text = open(path, "rb").read().decode("utf-8", "replace")
            except OSError:
                continue
            for m in re.finditer(r'[^,{}\[\]]{0,30}(rate|speed|倍速)[^,{}\[\]]{0,30}'
                                 r'\s*[:=]\s*("?[\w.]{1,12}"?)', text, re.I):
                print(f"   {os.path.relpath(path, BILI_ROAM)}: {m.group(0)[:110]}")
                found += 1
                if found > 20:
                    break
        if found > 20:
            break
    if found == 0:
        print("   （没找到倍速字段）")
else:
    print(f"   {BILI_ROAM} 不存在")

print()
print("=" * 70)
print("结论与建议")
print("=" * 70)
print("""  慢放是"音画不同步"的常见伪装：画面慢半拍、音频正常，
  听起来/看起来和"音频延迟"一样，但根因在播放速率。

  按这个顺序处理（改完**立刻**能听出来）：
    1) 视频播放器右下角齿轮 → 「倍速」→ 确认改成 **1.0x（正常）**；
       哔哩哔哩网页播放器在右下角有倍速按钮，快捷键 Shift+.（句号）
    2) 关掉「记忆播放速度」类插件；哔哩哔哩客户端的倍速偏好会同步到网页端
    3) Edge 设置 → Cookie 和网站权限 → 查看所有站点数据 → 删掉 bilibili.com
       （清掉站点保存的倍速偏好）
    4) 若每次打开都自动变 0.6x，检查是否装了倍速类扩展（油猴脚本/扩展程序）
""")
