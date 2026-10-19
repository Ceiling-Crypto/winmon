# -*- coding: utf-8 -*-
"""
在桌面创建 WinMon 快捷方式（用 pythonw 启动，不弹黑色控制台窗口）。

要点：
  · TargetPath 用 **pythonw.exe**（python.exe 会带一个控制台黑框）
  · Arguments 传 winmon.py 的**绝对路径**（避免依赖工作目录）
  · WorkingDirectory 设为项目目录（程序要在这里读写 settings/catalog）
  · IconLocation 用刚生成的 winmon.ico
  · 通过 WScript.Shell COM 创建 .lnk（Windows 标准做法，无需第三方库）

用法：
    py tests/make_shortcut.py            :: 在桌面创建/更新快捷方式
    py tests/make_shortcut.py --check    :: 只检查现有快捷方式
    py tests/make_shortcut.py --remove   :: 删除桌面快捷方式
"""
import os
import subprocess
import sys

sys.stdout.reconfigure(encoding="utf-8")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CNW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

APP_NAME = "WinMon"
DESKTOP_NAME = "WinMon 程序监控.lnk"
PY = sys.executable
PYW = os.path.join(os.path.dirname(PY), "pythonw.exe")
SCRIPT = os.path.join(ROOT, "winmon.py")
ICON = os.path.join(ROOT, "winmon.ico")


def desktop_dir() -> str:
    """取桌面路径（尊重 OneDrive 重定向）。"""
    out = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command",
         "[Environment]::GetFolderPath('Desktop')"],
        capture_output=True, timeout=30, creationflags=CNW)
    path = (out.stdout or b"").decode("utf-8", "replace").strip()
    return path or os.path.join(os.path.expanduser("~"), "Desktop")


def run_ps(script: str) -> tuple[int, str]:
    r = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command",
         "[Console]::OutputEncoding=[Text.Encoding]::UTF8; " + script],
        capture_output=True, timeout=60, creationflags=CNW)
    err = (r.stderr or b"").decode("utf-8", "replace").strip()
    return r.returncode, err


def main():
    argv = sys.argv[1:]
    desktop = desktop_dir()
    lnk = os.path.join(desktop, DESKTOP_NAME)
    print(f"桌面目录 : {desktop}")
    print(f"快捷方式 : {lnk}")
    print(f"pythonw  : {PYW}（存在={os.path.exists(PYW)}）")
    print(f"脚本     : {SCRIPT}（存在={os.path.exists(SCRIPT)}）")
    print(f"图标     : {ICON}（存在={os.path.exists(ICON)}）")

    if "--remove" in argv:
        if os.path.exists(lnk):
            os.remove(lnk)
            print("\n已删除桌面快捷方式")
        else:
            print("\n桌面没有该快捷方式")
        return 0

    if "--check" in argv:
        if not os.path.exists(lnk):
            print("\n[未找到] 桌面还没有该快捷方式")
            return 1
        code, _err = run_ps(
            "$s=(New-Object -ComObject WScript.Shell).CreateShortcut('" +
            lnk.replace("'", "''") + "'); Write-Output $s.TargetPath; "
            "Write-Output $s.Arguments; Write-Output $s.WorkingDirectory; "
            "Write-Output $s.IconLocation")
        print(f"\n现有快捷方式内容（ps 退出码 {code}）：")
        return 0

    if not os.path.exists(SCRIPT):
        print("\n[失败] 找不到 winmon.py")
        return 2
    target = PYW if os.path.exists(PYW) else PY
    if target == PY:
        print("\n[注意] 没有 pythonw.exe，会用 python.exe（会带控制台窗口）")

    # 用 PowerShell 的 WScript.Shell 创建 .lnk
    ps = (
        f"$ws = New-Object -ComObject WScript.Shell; "
        f"$sc = $ws.CreateShortcut('{lnk}'); "
        f"$sc.TargetPath = '{target}'; "
        f"$sc.Arguments = '\"{SCRIPT}\"'; "
        f"$sc.WorkingDirectory = '{ROOT}'; "
        f"$sc.IconLocation = '{ICON},0'; "
        f"$sc.Description = 'WinMon'; "
        f"$sc.WindowStyle = 1; "
        f"$sc.Save(); "
        f"Write-Output 'saved'"
    )
    code, err = run_ps(ps)
    if code != 0 or not os.path.exists(lnk):
        print(f"\n[失败] 创建快捷方式失败（ps 退出码 {code}）\n{err[-500:]}")
        return 3
    print("\n[成功] 已在桌面创建快捷方式")
    print(f"  目标   : {target}")
    print(f"  参数   : \"{SCRIPT}\"")
    print(f"  起始于 : {ROOT}")
    print(f"  图标   : {ICON},0")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
