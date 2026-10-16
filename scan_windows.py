# -*- coding: utf-8 -*-
"""扫描当前屏幕上的窗口清单（命令行，不开界面）

用法：
    python scan_windows.py            # 只列出屏幕上真实可见的窗口
    python scan_windows.py --all      # 连同屏幕上看不到的隐藏窗口一起列出
    python scan_windows.py --json     # 以 JSON 输出（便于脚本二次处理）
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import winmon_core as core

if __name__ == "__main__":
    args = sys.argv[1:]
    sys.exit(core.scan_windows_cli(
        as_json=("--json" in args),
        show_hidden=("--all" in args or "-a" in args),
    ))
