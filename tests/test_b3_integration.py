# -*- coding: utf-8 -*-
"""
B3 —— 端到端集成：命令行入口 + 完整启动的界面。

这一组故意"少用替身、多用真东西"：

  · CLI：真的调用 `winmon.py --catalog …`，在**临时设置文件**上验证
    收藏 / 最近使用 / 类别筛选的完整链路（读 / 写 / 再读）。
    用环境变量 `WINMON_SETTINGS` 指向临时文件，所以不会碰用户真实偏好。
  · GUI：用 `WinMonApp(root)` 做一次**真正的完整初始化**（含后台捕获线程与定时器），
    验证目录面板能挂上真实偏好、收藏能落盘、关了再开能读回来。
    `_build_ui` + 替身对象的做法（见 test_a5/B2）用来测布局，
    但"初始化路径本身有没有写错属性名"只有完整启动才测得出来。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import _helpers as h                     # noqa: E402
import _marks as marks                   # noqa: E402
import winmon_core as core               # noqa: E402

PROJECT_DIR = h.PROJECT_DIR


def run_cli(args: list[str], settings_path: str, timeout: float = 120.0):
    """在项目目录下跑一次 winmon.py，设置文件指向临时路径，强制 UTF-8 输出。"""
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    env["WINMON_SETTINGS"] = settings_path
    kw = {}
    if hasattr(subprocess, "CREATE_NO_WINDOW"):
        kw["creationflags"] = subprocess.CREATE_NO_WINDOW
    proc = subprocess.run([sys.executable, "winmon.py"] + args, cwd=PROJECT_DIR,
                          capture_output=True, timeout=timeout, env=env, **kw)
    out = (proc.stdout or b"").decode("utf-8", "replace")
    err = (proc.stderr or b"").decode("utf-8", "replace")
    return proc.returncode, out, err


def write_settings(path: str, prefs: core.CatalogPrefs) -> None:
    data = {"show_hidden": False, "program_prefs": prefs.to_dict()}
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)


class TestCatalogCliAgainstTempSettings(unittest.TestCase):
    """`--catalog --favorites / --recent / --category` 在真实子进程里跑通。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.settings = os.path.join(cls.tmp.name, "winmon_settings.json")
        # 目录里真实存在的一项（用缓存构建出来的目录里找哔哩哔哩；
        # 找不到就退化成"目录里第一项"，避免依赖具体机器装了什么）
        items = core.build_program_catalog(use_cache=True)
        if not items:
            raise unittest.SkipTest("程序目录是空的，无法测试")
        fav = next((it for it in items if "哔哩哔哩" in it.name), None) or items[0]
        cls.fav = fav
        prefs = core.CatalogPrefs()
        prefs.add_favorite(fav.target, fav.name)
        prefs.record_use(fav.target, fav.name)
        write_settings(cls.settings, prefs)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_favorites_filter(self):
        code, out, err = run_cli(["--catalog", "--favorites"], self.settings)
        self.assertEqual(code, 0, f"{err[-1500:]}\n{out[-1500:]}")
        self.assertIn("仅收藏", out)
        self.assertIn(self.fav.name, out, f"收藏项没出现在输出里：\n{out[:1500]}")

    def test_recent_filter(self):
        code, out, err = run_cli(["--catalog", "--recent"], self.settings)
        self.assertEqual(code, 0, f"{err[-1500:]}")
        self.assertIn("仅最近使用", out)
        self.assertIn(self.fav.name, out)

    def test_category_filter_matches_core_classification(self):
        want = core.category_of_item(self.fav)
        label = core.CATEGORY_LABELS[want]
        code, out, err = run_cli(["--catalog", "--category", label], self.settings)
        self.assertEqual(code, 0, f"{err[-1500:]}")
        self.assertIn(f"类别：{label}", out)
        self.assertIn(self.fav.name, out)

    def test_short_category_name_works(self):
        """`--category 游戏` 这种简写也要能解析（前缀匹配）。"""
        code, out, err = run_cli(["--catalog", "--category", "游戏"], self.settings)
        self.assertEqual(code, 0, f"{err[-1500:]}")
        self.assertIn("游戏娱乐", out)

    def test_unknown_category_is_rejected(self):
        code, out, err = run_cli(["--catalog", "--category", "并不存在的类别"],
                                 self.settings)
        self.assertEqual(code, 2, "未知类别应返回退出码 2")
        self.assertIn("未知类别", out)
        self.assertIn("游戏娱乐", out, "应提示可用的类别清单")

    def test_grouped_output_has_category_headers(self):
        code, out, err = run_cli(["--catalog"], self.settings)
        self.assertEqual(code, 0, f"{err[-1500:]}")
        self.assertIn("==", out, "默认应当按类别分组输出")
        self.assertIn(core.CATEGORY_LABELS[core.category_of_item(self.fav)], out)

    def test_cli_reads_prefs_without_modifying_them(self):
        """只读命令不能改设置文件（避免"看一眼就写坏了用户偏好"）。"""
        before = open(self.settings, encoding="utf-8").read()
        run_cli(["--catalog", "--favorites"], self.settings)
        after = open(self.settings, encoding="utf-8").read()
        self.assertEqual(before, after, "只读的过滤命令不应修改设置文件")


@h.needs_tk
@marks.slow("完整 WinMonApp 启动（含后台线程）")
class TestFullAppStartup(unittest.TestCase):
    """真的启动一次 `WinMonApp`（含后台线程与定时器），验证初始化路径没有写错。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.settings = os.path.join(self.tmp.name, "winmon_settings.json")

    def tearDown(self):
        self.tmp.cleanup()

    def _make_app(self):
        import tkinter as tk
        import winmon
        root = tk.Tk()
        old = os.environ.get("WINMON_SETTINGS")
        os.environ["WINMON_SETTINGS"] = self.settings
        try:
            app = winmon.WinMonApp(root)
        finally:
            if old is None:
                os.environ.pop("WINMON_SETTINGS", None)
            else:
                os.environ["WINMON_SETTINGS"] = old
        self.addCleanup(h.shutdown_app, app)
        return root, app

    def test_creates_all_panes_and_widgets(self):
        root, app = self._make_app()
        for attr in ("tree", "preview", "gallery", "proc_tree", "catalog",
                     "program_prefs", "worker", "notebook", "workspace",
                     "ai_workspace", "workspaces", "idle_watch"):
            with self.subTest(attr=attr):
                self.assertTrue(hasattr(app, attr), f"初始化后缺少 {attr}")
        self.assertEqual(len(app.notebook.tabs()), 6)
        self.assertGreater(len(app.windows), 0, "初始化应已枚举出窗口")

    def test_settings_path_follows_env_override(self):
        root, app = self._make_app()
        self.assertEqual(os.path.normcase(app._settings_path),
                         os.path.normcase(self.settings))

    def test_window_opens_at_the_dpi_scaled_size(self):
        """
        开窗尺寸 = 设计稿 × DPI 缩放（2026-10-13 修的真 bug 的接线检查）。

        纯计算在 `test_a5` 里逐条核对；这里确认真 app 真的按它开了窗 ——
        本测试进程没开 DPI 感知，所以缩放是 1.0、尺寸就是设计稿 1160×730。
        """
        import winmon
        root, app = self._make_app()
        want = winmon.scaled_window_size(
            app.dpi_scale,
            screen=(root.winfo_screenwidth(), root.winfo_screenheight()))
        root.update_idletasks()
        self.assertAlmostEqual(root.winfo_width(), want[0], delta=6)
        self.assertAlmostEqual(root.winfo_height(), want[1], delta=40)
        self.assertGreaterEqual(app.dpi_scale, 1.0)

    def test_favorite_survives_restart(self):
        """收藏 + 最近使用写盘后，重启程序必须还在（真实往返）。"""
        root, app = self._make_app()
        target = r"X:\WinMon-Test\app.exe"
        items = [core.ProgramItem(name="测试程序", target=target)]
        # 这个目标路径并不存在，默认的「仅可启动」会把它过滤掉 ——
        # 本用例测的是收藏/最近使用的往返，所以先关掉那个过滤
        app.catalog.only_runnable.set(False)
        app.catalog._on_loaded(items)
        iid = next(iter(app.catalog.row_of_item))
        app.catalog.tree.selection_set(iid)
        app.catalog.toggle_favorite_selected()
        app.program_prefs.record_use(target, "测试程序")
        app.save_settings_now()
        self.assertTrue(app.program_prefs.is_favorite(target))

        with open(self.settings, encoding="utf-8") as fh:
            data = json.load(fh)
        self.assertIn("program_prefs", data)

        # 关掉，重新完整启动一次
        app.on_close()
        root2, app2 = self._make_app()
        self.assertTrue(app2.program_prefs.is_favorite(target),
                        "重启后收藏丢了")
        self.assertTrue(app2.program_prefs.is_recent(target),
                        "重启后最近使用丢了")

    def test_catalog_loads_and_renders_grouped(self):
        """完整启动下真的载入一次目录，确认分组渲染没报错。"""
        root, app = self._make_app()
        items = core.build_program_catalog(use_cache=True)
        app.notebook.select(3)
        app.catalog._on_loaded(items)
        root.update()
        self.assertEqual(len(app.catalog.filtered), len([it for it in items
                                                         if it.runnable]))
        self.assertTrue(app.catalog.category_row, "应当按类别分组")
        total = sum(len(app.catalog.tree.get_children(p))
                    for p in app.catalog.tree.get_children(""))
        self.assertEqual(total, len(app.catalog.filtered))


if __name__ == "__main__":
    unittest.main(verbosity=2)
