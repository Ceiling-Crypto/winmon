# -*- coding: utf-8 -*-
"""
优雅关闭当前屏幕上所有可见窗口，但保留：
  1) 运行本脚本的进程自身
  2) 与「当前窗口」同一个可执行文件的窗口（默认取前台窗口所属程序）
  3) 通过 --keep / --keep-path / 环境变量额外指定的对象

按「可执行文件路径」保留比按 PID 保留更安全：即使 PID 变化，也不会误关当前程序。

用法：
    python close_others.py                # 关闭其他所有可见窗口（保留本程序）
    python close_others.py --dry-run      # 只列出将要关闭的窗口，不执行
    python close_others.py --keep 1234    # 额外保留指定 PID
    python close_others.py --keep-path "X:\\xxx\\yyy.exe"
    python close_others.py --self-path "X:\\xxx\\zzz.exe"   # 指定"当前窗口"
    python close_others.py --force        # 优雅关闭超时后改为强制结束
    python close_others.py --wait 10      # 等待优雅退出的秒数
"""
import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import winmon_core as core

# 由启动脚本通过环境变量传入需要保留的 PID（逗号分隔）与可执行文件路径
ENV_KEEP = "WINMON_KEEP_PIDS"
ENV_SELF = "WINMON_SELF_EXE"


def _foreground_exe() -> str:
    """取前台窗口所属进程的可执行文件路径，作为"当前窗口"的默认判定依据。"""
    try:
        hwnd = core.get_foreground_hwnd()
        if not hwnd:
            return ""
        pid = core.w.DWORD(0)
        core.user32.GetWindowThreadProcessId(core.w.HWND(hwnd), core.ctypes.byref(pid))
        path, _ = core.query_process_detail(pid.value)
        return path or ""
    except Exception:
        return ""


def main() -> int:
    ap = argparse.ArgumentParser(add_help=True)
    ap.add_argument("--keep", type=int, action="append", default=[],
                    help="额外保留的 PID（可重复）")
    ap.add_argument("--keep-path", action="append", default=[],
                    help="额外保留的可执行文件路径（可重复）")
    ap.add_argument("--self-path", default=None,
                    help="当前窗口的可执行文件路径；不指定则自动取前台窗口")
    ap.add_argument("--force", action="store_true", help="优雅关闭超时后强制结束")
    ap.add_argument("--dry-run", action="store_true", help="只预览，不执行")
    ap.add_argument("--wait", type=float, default=8.0, help="等待优雅退出的秒数")
    ns = ap.parse_args()

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    # ---- 计算保留集合 ----
    keep_pids = set(ns.keep)
    keep_pids.add(os.getpid())
    keep_paths = {p.lower() for p in ns.keep_path if p}
    if ns.self_path:
        keep_paths.add(ns.self_path.lower())
    if os.environ.get(ENV_SELF):
        keep_paths.add(os.environ[ENV_SELF].lower())
    if not keep_paths:
        fexe = _foreground_exe()
        if fexe:
            keep_paths.add(fexe.lower())
    for token in os.environ.get(ENV_KEEP, "").replace(";", ",").split(","):
        token = token.strip()
        if token.isdigit():
            keep_pids.add(int(token))

    # 只取"屏幕上真实可见"的窗口：排除 cloaked 隐藏窗口与 UWP 外壳窗口
    wins = core.collect_desktop(include_cloaked=False)

    def is_self(wi) -> bool:
        if wi.pid in keep_pids:
            return True
        return bool(wi.path) and wi.path.lower() in keep_paths

    cands = [wi for wi in wins if not is_self(wi) and not wi.is_frame_host]
    kept = [wi for wi in wins if is_self(wi)]

    print("保留（不会关闭）：")
    for wi in kept:
        print(f"  · {wi.real_name} [{wi.pid}] {wi.title[:50]}")
    for p in sorted(keep_paths):
        print(f"  · 路径匹配：{p}")
    print(f"  · 额外保留 PID：{sorted(keep_pids)}")
    print()
    print(f"屏幕上可见窗口 {len(wins)} 个 → 待关闭 {len(cands)} 个")
    print()

    if not cands:
        print("没有需要关闭的窗口。")
        return 0

    print(f"{'软件':<28} {'PID':>7}  {'状态':<8} 窗口标题")
    print("-" * 100)
    for wi in cands:
        title = wi.title if len(wi.title) <= 40 else wi.title[:39] + "…"
        print(f"{wi.real_name:<28} {wi.pid:>7}  {wi.state:<8} {title}")
    print("-" * 100)

    if ns.dry_run:
        print("\n[--dry-run] 未执行任何操作。")
        return 0

    history = core.ActionHistory()
    print("\n正在发送关闭请求……")
    for wi in cands:
        if wi.proc.lower() in core.CRITICAL_PROCESSES:
            print(f"  [跳过] {wi.real_name} 是系统关键进程")
            history.add("关闭窗口", f"{wi.real_name} [{wi.pid}]", "已跳过（关键系统进程）")
            continue
        ok, msg = core.graceful_close_window(wi.hwnd)
        print(f"  [{'已发送' if ok else '失败  '}] {wi.real_name} [{wi.pid}] {msg}")
        history.add("关闭窗口", f"{wi.real_name} [{wi.pid}]", "已请求" if ok else f"失败：{msg}")

    deadline = time.time() + ns.wait
    while time.time() < deadline:
        if not any(core.is_process_alive(wi.pid) for wi in cands):
            break
        time.sleep(0.3)

    still = [wi for wi in cands
             if core.is_process_alive(wi.pid)
             and wi.proc.lower() not in core.CRITICAL_PROCESSES]
    gone = [wi for wi in cands if wi not in still]

    print()
    print(f"结果：已退出 {len(gone)} 个，仍在运行 {len(still)} 个")
    for wi in gone:
        print(f"  [√] {wi.real_name} [{wi.pid}] 已关闭")
        history.add("关闭窗口", f"{wi.real_name} [{wi.pid}]", "已退出（优雅）")
    for wi in still:
        print(f"  [!] {wi.real_name} [{wi.pid}] 仍在运行（可能在等待保存确认，或未响应关闭消息）")
        history.add("关闭窗口", f"{wi.real_name} [{wi.pid}]", "未响应（超时）")

    if still and ns.force:
        print("\n--force 已启用，强制结束剩余进程……")
        for wi in still:
            ok, msg, _ = core.terminate_process(wi.pid)
            print(f"  [{'强制结束' if ok else '失败  '}] {wi.real_name} [{wi.pid}] {msg}")
            history.add("结束进程", f"{wi.real_name} [{wi.pid}]",
                        "成功（强制）" if ok else f"失败：{msg}")

    try:
        path = history.export()
        print(f"\n操作记录已写入：{path}")
    except Exception:
        pass
    return 0 if not still else 1


if __name__ == "__main__":
    sys.exit(main())
