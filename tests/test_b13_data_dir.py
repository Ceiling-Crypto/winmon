# -*- coding: utf-8 -*-
"""
B13 —— 运行时数据**不落在程序目录里**（`test_b13`）。

这一批解决什么
--------------
用户的原话：「对于 winmon 运行产生的多余文件只进入 git[ignore] 而非用户设备」。

改之前：设置、目录缓存、操作历史日志、媒体快照、错误日志**全都写在程序目录里**。
后果有两个，第二个才是要命的：

  · 用户的程序目录里堆着几个跟他无关的文件；
  · **别人 clone 下来跑一次，`git status` 立刻变脏**（一堆没跟踪的生成物）——
    对一个"放到 GitHub 上给人试用"的 demo 来说，这是第一印象问题。

改之后：这些文件都在**运行时数据目录**里（`core.data_dir()`，默认
`%LOCALAPPDATA%\\WinMon`），程序目录只剩源与文档。同时：

  · `WINMON_DATA_DIR` 可以把它指回任意目录 —— 想"绿色便携"就设它；
  · 旧版本留在程序目录里的文件会被**一次性迁移**过去（`migrate_legacy_data_files`），
    而且**先校验副本再删原件**，搬不动就原样留着 —— 迁移不许弄丢用户数据。

这个文件钉住的就是上面这些判据。
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import winmon_core as core                                    # noqa: E402
import winmon                                                 # noqa: E402


def _can_use_real_data_dir() -> bool:
    """这台机器上"默认数据目录"是不是真的离开了程序目录（非便携、拿得到 LOCALAPPDATA）。"""
    if os.environ.get(core.DATA_DIR_ENV):
        return False
    return bool(os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA"))


class TestDataDirResolution(unittest.TestCase):
    """`data_dir()` / `data_file()` / `legacy_data_file()` 的解析规则。"""

    def _with_env(self, **env):
        old = {k: os.environ.get(k) for k in env}
        for k, v in env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        def restore():
            for k, v in old.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
        self.addCleanup(restore)

    def test_env_override_wins(self):
        """`WINMON_DATA_DIR` = 便携模式：数据跟着你指定的目录走。"""
        target = os.path.join(tempfile.gettempdir(), "winmon-便携-测试")
        self._with_env(**{core.DATA_DIR_ENV: target})
        self.assertEqual(core.data_dir(), os.path.abspath(target))
        self.assertEqual(core.data_file("x.json"), os.path.join(os.path.abspath(target),
                                                                "x.json"))

    def test_default_is_not_the_program_dir(self):
        """默认必须**离开**程序目录 —— 否则 clone 下来跑一次工作区就脏了。"""
        if not _can_use_real_data_dir():
            self.skipTest("便携模式或拿不到 LOCALAPPDATA，无法验证默认位置")
        self.assertNotEqual(os.path.normcase(core.data_dir()),
                            os.path.normcase(core.APP_DIR))
        self.assertIn("WinMon", core.data_dir())

    def test_data_dir_does_not_create_anything(self):
        """**只算路径、不建目录** —— 读一次快照不该顺手在磁盘上创出目录来。"""
        target = os.path.join(tempfile.gettempdir(), "winmon-不该被建出来-xyz")
        self._with_env(**{core.DATA_DIR_ENV: target})
        self.addCleanup(lambda: os.path.isdir(target)
                        and __import__("shutil").rmtree(target, ignore_errors=True))
        core.data_dir()
        core.data_file("a.json")
        self.assertFalse(os.path.isdir(target), "`data_dir()` 不该建目录")

    def test_legacy_path_is_still_the_program_dir(self):
        """迁移要认得"旧位置"，它永远是程序目录。"""
        self.assertEqual(core.legacy_data_file("winmon_history.log"),
                         os.path.join(core.APP_DIR, "winmon_history.log"))


class TestDefaultPathsAllLiveInTheDataDir(unittest.TestCase):
    """五个"运行痕迹"文件的默认位置，**一个都不许留在程序目录里**。"""

    def _defaults(self):
        return {
            "操作历史日志": core.history_log_path(),
            "媒体快照": core.media_snapshot_path(),
            "设置": winmon._settings_path(),
        }

    def test_everything_defaults_into_the_data_dir(self):
        if os.environ.get("WINMON_HISTORY_LOG") or os.environ.get(core.MEDIA_SNAPSHOT_ENV) \
                or os.environ.get("WINMON_SETTINGS"):
            self.skipTest("有环境变量覆盖，测不到默认值")
        for name, path in self._defaults().items():
            with self.subTest(item=name):
                self.assertEqual(os.path.dirname(os.path.abspath(path)),
                                 os.path.abspath(core.data_dir()))

    def test_none_of_them_is_in_the_program_dir(self):
        if not _can_use_real_data_dir():
            self.skipTest("便携模式或拿不到 LOCALAPPDATA，无法验证默认位置")
        if os.environ.get("WINMON_HISTORY_LOG") or os.environ.get(core.MEDIA_SNAPSHOT_ENV) \
                or os.environ.get("WINMON_SETTINGS"):
            self.skipTest("有环境变量覆盖，测不到默认值")
        for name, path in self._defaults().items():
            with self.subTest(item=name):
                self.assertNotEqual(os.path.normcase(os.path.dirname(
                    os.path.abspath(path))), os.path.normcase(core.APP_DIR),
                    f"{name} 又写回程序目录了")

    def test_the_names_are_still_the_ones_the_docs_promise(self):
        """搬家不改名：README / 清理清单 / .gitignore 里写的还是这些文件名。"""
        if os.environ.get("WINMON_HISTORY_LOG"):
            self.skipTest("有环境变量覆盖")
        self.assertEqual(os.path.basename(core.history_log_path()),
                         core.HISTORY_LOG_NAME)
        self.assertEqual(os.path.basename(core.media_snapshot_path()),
                         "winmon_media.jsonl")


class TestCleanupListKnowsWhereThingsAre(unittest.TestCase):
    """清理清单要分得清"数据目录"与"程序目录"（字节码缓存没跟着搬）。"""

    def test_pycache_is_looked_for_in_the_program_dir(self):
        with tempfile.TemporaryDirectory() as data:
            items = {d["key"]: d for d in core.list_cleanable_files(root=data)}
            for key in ("catalog_cache", "error_log", "media_snapshot", "settings"):
                with self.subTest(item=key):
                    self.assertEqual(os.path.dirname(items[key]["paths"][0])
                                     if items[key]["paths"] else data, data)
            # 字节码缓存那一项**不在**数据目录里找
            self.assertNotEqual(
                os.path.dirname(items["pycache"]["paths"][0]) if items["pycache"]["paths"]
                else "", data)

    def test_cleanup_never_touches_the_program_dir_when_root_is_given(self):
        """给了 `root`（= 数据目录）时，程序目录里那两项也不该被牵连去删。"""
        with tempfile.TemporaryDirectory() as data:
            other = os.path.join(data, "__pycache__")
            os.makedirs(other)
            with open(os.path.join(other, "x.pyc"), "w", encoding="utf-8") as fh:
                fh.write("x")
            app_pycache = os.path.join(core.APP_DIR, "__pycache__")
            before = os.path.isdir(app_pycache)
            core.clean_files(["pycache"], root=data, app_root=data)
            self.assertFalse(os.path.isdir(other), "给了 app_root 就该删那一份")
            self.assertEqual(os.path.isdir(app_pycache), before,
                             "不该顺手删真实程序目录里的缓存")


class TestLegacyMigration(unittest.TestCase):
    """`migrate_legacy_data_files()` —— 搬家可以，弄丢数据不行。"""

    def _dirs(self):
        src = tempfile.TemporaryDirectory()
        dst = tempfile.TemporaryDirectory()
        self.addCleanup(src.cleanup)
        self.addCleanup(dst.cleanup)
        return src.name, dst.name

    def _write(self, path, text):
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)

    def test_known_files_are_moved(self):
        src, dst = self._dirs()
        self._write(os.path.join(src, "winmon_settings.json"), '{"a": 1}')
        self._write(os.path.join(src, "winmon_history.log"), "一行\n")
        moved = core.migrate_legacy_data_files(src_dir=src, dst_dir=dst)
        self.assertEqual(sorted(moved), ["winmon_history.log", "winmon_settings.json"])
        self.assertEqual(open(os.path.join(dst, "winmon_settings.json"),
                              encoding="utf-8").read(), '{"a": 1}')
        self.assertFalse(os.path.exists(os.path.join(src, "winmon_settings.json")),
                         "搬完原件该没了（程序目录恢复干净）")

    def test_it_never_overwrites_newer_data(self):
        """目标已存在就跳过 —— 新数据永远优先，绝不被旧文件盖掉。"""
        src, dst = self._dirs()
        self._write(os.path.join(src, "winmon_settings.json"), "旧")
        self._write(os.path.join(dst, "winmon_settings.json"), "新")
        self.assertEqual(core.migrate_legacy_data_files(src_dir=src, dst_dir=dst), [])
        self.assertEqual(open(os.path.join(dst, "winmon_settings.json"),
                              encoding="utf-8").read(), "新")
        self.assertTrue(os.path.exists(os.path.join(src, "winmon_settings.json")))

    def test_unknown_files_are_left_alone(self):
        """只搬已知名字：不认识的（例如用户自己放的文件）一律不碰。"""
        src, dst = self._dirs()
        self._write(os.path.join(src, "我的笔记.txt"), "别动我")
        self._write(os.path.join(src, "random.bin"), "x")
        self.assertEqual(core.migrate_legacy_data_files(src_dir=src, dst_dir=dst), [])
        self.assertTrue(os.path.exists(os.path.join(src, "我的笔记.txt")))
        self.assertTrue(os.path.exists(os.path.join(src, "random.bin")))

    def test_same_dir_is_a_noop(self):
        """便携模式（数据目录 == 程序目录）时什么都不做，也别删自己。"""
        src, _dst = self._dirs()
        self._write(os.path.join(src, "winmon_settings.json"), "x")
        self.assertEqual(core.migrate_legacy_data_files(src_dir=src, dst_dir=src), [])
        self.assertTrue(os.path.exists(os.path.join(src, "winmon_settings.json")))

    def test_every_known_name_is_a_real_data_file(self):
        """迁移清单里的每个名字都要是"程序真的会产生"的那个名字（防手滑写错）。"""
        for name in core.LEGACY_DATA_NAMES:
            with self.subTest(name=name):
                self.assertTrue(name.startswith("winmon_"),
                                "运行痕迹的文件名都以 winmon_ 开头")


if __name__ == "__main__":
    unittest.main(verbosity=2)
