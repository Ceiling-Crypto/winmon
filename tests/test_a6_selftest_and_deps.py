# -*- coding: utf-8 -*-
"""
A6 —— 自检链路与"零依赖"约定。

两件事：

1. **`--selftest` 必须还能整条跑通**（HANDOFF 里"自检 8/8"是出厂的硬指标）。
   前面几组测试是分别验证各环节的 API；这一组直接把 `selftest()` 跑一遍，
   确认 8 个环节确实都被走到了、且结论是成功（后 3 项是随附文件/自启动命令/
   桥接脚本版本的守门员，见 `SELFTEST_STAGES` 的注释）。
   为什么用子进程跑：自检会创建窗口/抓屏/DIB，在子进程里更干净，
   而且能顺带验证真正的命令行入口。

2. **零第三方依赖**是项目的核心卖点，必须有测试守着：
   把所有源文件的 `import` 解出来，逐个确认它们要么是标准库、要么是项目自己的模块。
   新增一个 `import requests` 就会被这条测试拦住。
"""

from __future__ import annotations

import ast
import os
import subprocess
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import _helpers as h                                # noqa: E402
from _helpers import PROJECT_DIR, SOURCE_FILES      # noqa: E402
import winmon_core as core                          # noqa: E402

#: 自检的 8 个环节（顺序与 winmon.py 里的输出一致）
#:
#: 前 5 项是链路（枚举→捕获→渲染→图标→终止）；后 3 项是 2026-10-16 复查补丁加的
#: 「随程序一起发出去的东西到底对不对」守门员（[6] 随附文件、[7] 自启动命令、
#: [8] 桥接脚本版本）—— 它们各自钉住一个**只在真机上才会暴露**的坑：
#: 文件缺失、自启动指向不存在的脚本、ps1 还是旧版（非原子写）。
SELFTEST_STAGES = [
    ("[1/8]", "窗口枚举"),
    ("[2/8]", "画面捕获"),
    ("[3/8]", "画面渲染"),
    ("[4/8]", "图标提取"),
    ("[5/8]", "终止能力"),
    ("[6/8]", "随附文件"),
    ("[7/8]", "自启动命令"),
    ("[8/8]", "桥接脚本"),
]


def run_python(args: list[str], timeout: float = 120.0) -> subprocess.CompletedProcess:
    """在项目目录下跑一次 python，强制 UTF-8 输出（否则中文会被本地代码页弄乱）。"""
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    kw = {}
    if hasattr(subprocess, "CREATE_NO_WINDOW"):
        kw["creationflags"] = subprocess.CREATE_NO_WINDOW
    return subprocess.run([sys.executable] + args, cwd=PROJECT_DIR,
                          capture_output=True, timeout=timeout, env=env, **kw)


def decode(proc: subprocess.CompletedProcess) -> tuple[str, str]:
    out = (proc.stdout or b"").decode("utf-8", "replace")
    err = (proc.stderr or b"").decode("utf-8", "replace")
    return out, err


class TestSelftestPipeline(unittest.TestCase):
    """直接跑 `python winmon.py --selftest`，确认 8 个环节都通过。"""

    @classmethod
    def setUpClass(cls):
        try:
            cls.proc = run_python(["winmon.py", "--selftest"])
        except subprocess.TimeoutExpired:                      # pragma: no cover
            raise unittest.SkipTest("自检超时（环境太慢？）")
        cls.out, cls.err = decode(cls.proc)

    def test_exit_code_is_zero(self):
        self.assertEqual(self.proc.returncode, 0,
                         f"自检退出码 {self.proc.returncode}\n"
                         f"--- stdout ---\n{self.out[-3000:]}\n--- stderr ---\n{self.err[-2000:]}")

    def test_all_eight_stages_ran(self):
        for marker, name in SELFTEST_STAGES:
            with self.subTest(stage=name):
                self.assertIn(marker, self.out, f"自检没有走到「{name}」这一步")

    def test_tail_stages_report_ok(self):
        """
        [6..8]/8 三项守门员必须**明确报 OK**，而不是"跑到了就算过"。

        为什么单列一条：这三项是复查补丁新加的，它们的价值全在"结论"上 ——
        随附文件缺失 / 自启动指向不存在的脚本 / ps1 还是旧版（非原子写），
        都会让自检照样 exit 0 跑完，只有这一行能看出来。
        """
        for marker, needle, name in (
                ("[6/8]", "随附文件   : OK", "随附文件"),
                ("[7/8]", "自启动命令 : OK", "自启动命令"),
                ("[8/8]", "OK（v2 原子写）", "桥接脚本 v2")):
            with self.subTest(stage=name):
                line = next((l for l in self.out.splitlines()
                             if marker in l), "")
                self.assertTrue(line, f"自检输出里没有 {name} 那一行")
                self.assertIn(needle, line, f"{name} 没报 OK：{line!r}")

    def test_no_failure_markers(self):
        """自检内部用 [!!] 标失败；输出里不能出现它。"""
        self.assertNotIn("[!!]", self.out, f"自检报出了失败：\n{self.out[-2000:]}")

    def test_capture_stage_is_not_black(self):
        """
        抓屏不能是"全黑"。

        注意判据的脆弱点：**单个窗口**的像素可能本来就很暗
        （实测哔哩哔哩深色播放页只有 75.9% 非零），那不代表抓屏坏了。
        所以这里看两件事：
          · 自检报告的采样里有像素（不是 0%）
          · 自检目标窗口的采样也不是全零（说明链路真的取到了内容）
        真正"抓屏坏了"的表现是全零 / 极低比例，而不是"内容偏暗"。
        """
        line = next((l for l in self.out.splitlines()
                     if "非零采样" in l), "")
        if not line:
            self.skipTest("环境里没有可抓取的窗口，自检跳过了捕获环节")
        pct = float(line.split("非零采样")[1].split("%")[0].strip())
        self.assertGreater(pct, 5.0,
                           f"抓屏几乎全黑（{pct}%），链路可能有问题：{line.strip()}")

    def test_capture_has_real_content(self):
        """
        用"多个候选窗口里至少有一个抓到实内容"来判定抓屏链路。

        这比盯死某一个窗口稳健：某个窗口天生全黑（隐藏窗口、DRM 播放器）
        不代表抓屏坏了。
        """
        import winmon_core as core
        best = 0.0
        for wi in h.desktop_windows(visible_only=True)[:6]:
            if wi.rect_w < 200 or wi.rect_h < 150:
                continue
            cap = core.WindowCapturer()
            try:
                raw, ow, oh = cap.grab_window(wi.hwnd, wi.rect_w, wi.rect_h, 320, 200)
            except Exception:
                continue
            finally:
                cap.release()
            # 非零比例 + 颜色种类（纯色画面说明没抓到内容）
            sample = raw[::101]
            nonzero = 100.0 * sum(1 for b in sample if b) / max(1, len(sample))
            distinct = len(set(sample))
            best = max(best, nonzero if distinct > 3 else 0.0)
            if best > 50.0:
                break
        if best == 0.0:
            self.skipTest("桌面上没有可抓取的窗口")
        self.assertGreater(best, 50.0, f"抓屏链路可疑：最好的窗口也只有 {best:.1f}% 非零")

    def test_render_stage_printed_cost(self):
        line = next((l for l in self.out.splitlines() if "画面渲染" in l), "")
        self.assertTrue(line, "自检没有输出画面渲染结果")
        self.assertIn("ms", line)

    def test_icon_stage_reports_png_size(self):
        line = next((l for l in self.out.splitlines() if "图标提取" in l), "")
        self.assertTrue(line)
        self.assertTrue("成功" in line or "没有可用图标" in line, line)

    def test_terminate_stage_killed_the_temp_process(self):
        line = next((l for l in self.out.splitlines() if "强制结束成功" in l), "")
        self.assertTrue(line, f"自检里没有确认终止成功：\n{self.out[-1500:]}")
        self.assertIn("[OK]", line)

    def test_selftest_writes_no_error_log(self):
        """
        自检正常结束时不应留下 winmon_error.log。

        注意**位置**：错误日志 2026-10-16 起写在运行时数据目录里
        （`core.data_file()`，默认 `%LOCALAPPDATA%\\WinMon`），不在程序目录里。
        """
        self.assertFalse(os.path.exists(core.data_file("winmon_error.log")),
                         "自检留下了错误日志")

    def test_source_still_has_all_eight_stages(self):
        """
        源码层面也要守住（避免有人删掉某个环节而测试恰好在没有窗口的环境里跳过）。
        """
        src = open(os.path.join(PROJECT_DIR, "winmon.py"), encoding="utf-8").read()
        for marker, name in SELFTEST_STAGES:
            with self.subTest(stage=name):
                self.assertIn(marker, src, f"winmon.py 里缺少自检环节 {marker} {name}")


class TestScanCli(unittest.TestCase):
    """命令行 `--scan` / `--json` 也要能真的跑起来（无界面）。"""

    def test_scan_text(self):
        proc = run_python(["winmon.py", "--scan"])
        out, err = decode(proc)
        self.assertEqual(proc.returncode, 0, err[-2000:])
        self.assertIn("桌面窗口", out)
        self.assertIn("系统进程", out)

    def test_scan_json_is_valid(self):
        import json
        proc = run_python(["winmon.py", "--json"])
        out, err = decode(proc)
        self.assertEqual(proc.returncode, 0, err[-2000:])
        data = json.loads(out)                       # 解析失败会直接抛异常
        self.assertIn("windows", data)
        self.assertIn("counts", data)
        for win in data["windows"]:
            with self.subTest(hwnd=win.get("hwnd")):
                self.assertIsInstance(win["pid"], int)
                self.assertIsInstance(win["title"], str)
                self.assertEqual(len(win["size"]), 2)


class TestNoThirdPartyDependencies(unittest.TestCase):
    """零第三方依赖是项目的核心约定，必须有测试守着。"""

    #: 明确允许的标准库模块（其余由 sys.stdlib_module_names 判定）
    EXTRA_ALLOWED = {"winmon_core", "winmon"}

    @staticmethod
    def _imports_of(path: str) -> set[str]:
        """把一个源文件里所有的顶层模块名解出来（含函数内的延迟 import）。"""
        tree = ast.parse(open(path, encoding="utf-8").read())
        found: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    found.add(alias.name.split(".")[0])
            elif isinstance(node, ast.ImportFrom):
                if node.level == 0 and node.module:
                    found.add(node.module.split(".")[0])
        return found

    def test_all_python_files_use_only_stdlib(self):
        stdlib = set(getattr(sys, "stdlib_module_names", ()))
        if not stdlib:                                       # pragma: no cover
            self.skipTest("本 Python 版本没有 stdlib_module_names，无法判定")
        tests_dir = os.path.join(PROJECT_DIR, "tests")
        #: 项目内部的模块名（主程序 + tests 目录下的辅助模块）都不算第三方。
        #: 注意别用"白名单硬编码"：新增一个辅助脚本（如 icon_realsize.py）
        #: 就会让这条测试误报 —— 实测踩过。这里改为"是不是本项目的文件"。
        own = {m[:-3] for m in os.listdir(tests_dir) if m.endswith(".py")}
        own |= {m[:-3] for m in os.listdir(PROJECT_DIR) if m.endswith(".py")}
        own |= self.EXTRA_ALLOWED
        files = [os.path.join(PROJECT_DIR, n) for n in SOURCE_FILES]
        files += [os.path.join(tests_dir, n)
                  for n in sorted(os.listdir(tests_dir)) if n.endswith(".py")]
        checked = 0
        for path in files:
            name = os.path.relpath(path, PROJECT_DIR)
            with self.subTest(file=name):
                mods = self._imports_of(path)
                bad = {m for m in mods
                       if m not in stdlib and m not in own
                       and not m.startswith("_") and m != "__future__"}
                self.assertEqual(bad, set(),
                                 f"{name} 引入了非标准库模块：{sorted(bad)}")
                checked += 1
        self.assertGreater(checked, 8, "检查的文件太少，测试可能没生效")

    def test_no_dependency_manifest(self):
        """项目里不应出现 requirements.txt / pyproject 依赖声明。"""
        for name in ("requirements.txt", "pyproject.toml", "Pipfile", "setup.py"):
            with self.subTest(file=name):
                self.assertFalse(os.path.exists(os.path.join(PROJECT_DIR, name)),
                                 f"出现了依赖清单 {name}，与零依赖约定冲突")

    def test_documented_imports_match_reality(self):
        """
        文档里教的用法必须真的能用：`import winmon_core` 后调用 README 里那几个 API。

        这条能防止"文档还在教已经改名/删掉的函数"。
        """
        code = (
            "import winmon_core as core\n"
            "wins = core.collect_desktop()\n"
            "print(core.get_system_summary()['visible_windows'])\n"
            "cap = core.WindowCapturer()\n"
            "cap.release()\n"
            "print(core._fmt_mem(2048))\n"
            "print(isinstance(core.IconCache(16).get(wins[0]), str) if wins else 'no-window')\n"
            "print(core.STILL_ACTIVE)\n"
        )
        proc = run_python(["-c", code])
        out, err = decode(proc)
        self.assertEqual(proc.returncode, 0, f"{err[-2000:]}\n{out[-1000:]}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
