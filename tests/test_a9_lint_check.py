# -*- coding: utf-8 -*-
"""
A9 —— 代码复检器（`tests/lint_check.py`）自己的测试。

为什么要给 linter 写测试：它原本的"疑似死代码"只看**单个文件内**的引用，
于是 `winmon_core` 的公共 API（被 `winmon.py` 用）和 `tests/_helpers.py` 的公共辅助
（被各测试用）被判成死代码，一次报了 32 处 —— 全是噪音。噪音的代价不是"难看"，
而是**真的出现死代码时没人会注意到**（"改完跑 lint 看有没有新增问题"这条纪律失效）。
所以这里把三种情形都钉住：

  1. 跨文件被引用（含 `core.foo()` 这种属性形式的调用）→ **不许报**
  2. 整个项目都没人用 → **必须报**
  3. 只被自己递归调用 → **必须报**（"在自己身体里出现"不等于被使用）

另外还有一条元检查：**真实项目当前没有新增问题**。
也就是说，以后往项目里加一段没人用的函数，`run_tests.py` 会直接变红。

注意：本文件里刻意不出现完整的遗留标记单词（用 `"TO" + "DO"` 拼出来），
否则 linter 会把本文件自己当成"遗留标记"报一处 —— 这也是它的一种自指陷阱。
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import lint_check                              # noqa: E402

#: 目标文件名只能是 linter 认识的那几个（它按项目实际的文件名清单扫描）
MAIN = "winmon_core.py"
USER = "winmon.py"


class _TempProject:
    """临时项目：在临时目录里放几个 .py，然后交给 `lint_check.run()`。"""

    def __init__(self, case: unittest.TestCase, files: dict[str, str]) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        case.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name
        for name, body in files.items():
            path = os.path.join(self.root, name)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(body)

    def findings(self, baseline: str | None = None):
        """返回 (新增问题, 基线内已知问题, 失效基线条目)。"""
        path = None
        if baseline is not None:
            path = os.path.join(self.root, "lint_baseline.txt")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(baseline)
        return lint_check.run(root=self.root, baseline_path=path)

    def keys(self, baseline: str | None = None) -> set[str]:
        fresh, _known, _stale = self.findings(baseline)
        return {f["key"] for f in fresh}

    def dead_names(self, baseline: str | None = None) -> set[str]:
        """被报成死代码的名字集合，形如 `{'nobody()'}`。"""
        fresh, _known, _stale = self.findings(baseline)
        return {f["detail"].split("（")[0].strip() for f in fresh
                if f["kind"] == "可能死代码"}


class TestDeadCodeIsProjectWide(unittest.TestCase):
    """死代码判定必须跨文件，且认得属性形式的调用。"""

    def test_cross_file_call_is_not_dead_code(self):
        """`winmon.py` 里以 `core.lonely()` 形式调用 → 不算死代码。"""
        proj = _TempProject(self, {
            MAIN: "def lonely():\n    return 1\n",
            USER: "import winmon_core as core\n\nprint(core.lonely())\n",
        })
        self.assertNotIn("lonely()", proj.dead_names(),
                         "跨文件的属性调用被误报成死代码了（这正是修掉的那 32 处噪音）")

    def test_cross_file_name_call_is_not_dead_code(self):
        """`from winmon_core import lonely` 之后直接调用 → 不算死代码。"""
        proj = _TempProject(self, {
            MAIN: "def lonely():\n    return 1\n",
            USER: "from winmon_core import lonely\n\nprint(lonely())\n",
        })
        self.assertNotIn("lonely()", proj.dead_names())

    def test_nobody_calls_it_anywhere_is_dead_code(self):
        proj = _TempProject(self, {
            MAIN: "def nobody():\n    return 1\n\n\ndef somebody():\n    return 2\n",
            USER: "import winmon_core as core\n\nprint(core.somebody())\n",
        })
        self.assertIn("nobody()", proj.dead_names())
        self.assertNotIn("somebody()", proj.dead_names())

    def test_recursive_only_function_is_dead_code(self):
        """只被自己递归调用 → 仍然算死代码（"出现在自己身体里"不算被使用）。"""
        proj = _TempProject(self, {
            MAIN: "def loopy(n):\n    if n <= 0:\n        return 0\n    return loopy(n - 1)\n",
            USER: "import winmon_core\n",
        })
        self.assertIn("loopy()", proj.dead_names())

    def test_string_mention_is_not_a_reference(self):
        """名字只出现在字符串/注释里 → 不算被引用（别把文档当成"用到了"）。"""
        proj = _TempProject(self, {
            MAIN: "def helper():\n    return 1\n",
            USER: 'DOC = "调用 helper 可以拿到 1"\n# helper 用起来很方便\n',
        })
        self.assertIn("helper()", proj.dead_names())

    def test_testcase_subclass_is_not_dead_code(self):
        """`unittest.TestCase` 子类由框架按发现规则实例化，不能报死代码。"""
        proj = _TempProject(self, {
            MAIN: "import unittest\n\n\n"
                  "class TestSomething(unittest.TestCase):\n"
                  "    def test_it(self):\n        self.assertTrue(True)\n",
        })
        self.assertEqual(proj.dead_names(), set())

    def test_exported_name_in_all_is_not_dead_code(self):
        """`__all__` 里声明导出的名字不报（对外提供的公共 API）。"""
        proj = _TempProject(self, {
            MAIN: '__all__ = ["public_api"]\n\n\ndef public_api():\n    return 1\n',
        })
        self.assertEqual(proj.dead_names(), set())


class TestOtherChecks(unittest.TestCase):
    """其它几类检查（未用导入 / 重复定义 / 遗留标记）仍然有效。"""

    def test_unused_import_flagged_and_used_import_kept(self):
        proj = _TempProject(self, {
            MAIN: "import json\nimport ctypes\n\nprint(ctypes.sizeof(ctypes.c_int))\n",
        })
        kinds = {f["key"]: f["kind"] for f in proj.findings()[0]}
        self.assertEqual(kinds.get("winmon_core.py::import:json"), "未使用导入")
        self.assertNotIn("winmon_core.py::import:ctypes", kinds)

    def test_duplicate_definition_flagged(self):
        proj = _TempProject(self, {
            MAIN: "def twice():\n    return 1\n\n\ndef twice():\n    return 2\n",
        })
        keys = proj.keys()
        self.assertIn("winmon_core.py::dup:twice", keys)

    def test_legacy_tag_flagged(self):
        # 用拼接避免本文件自己含完整标记字面量（否则 linter 会报本文件）
        tag = "TO" + "DO"
        proj = _TempProject(self, {MAIN: f"x = 1  # {tag}: 以后再说\n"})
        self.assertIn(f"winmon_core.py::tag:{tag}", proj.keys())


class TestBaseline(unittest.TestCase):
    """基线：接受的问题被抑制，修掉之后会被提醒删除。"""

    FILES = {MAIN: "def accepted():\n    return 1\n"}

    def test_baseline_suppresses_the_finding(self):
        proj = _TempProject(self, self.FILES)
        self.assertIn("winmon_core.py::def:accepted", proj.keys())
        fresh, known, stale = proj.findings("winmon_core.py::def:accepted  # 有意保留\n")
        self.assertEqual(fresh, [])
        self.assertEqual([f["key"] for f in known], ["winmon_core.py::def:accepted"])
        self.assertEqual(stale, [])

    def test_stale_baseline_entry_is_reported(self):
        """基线里记着、但问题已经不存在了 → 提醒删掉（别让基线烂掉）。"""
        proj = _TempProject(self, {MAIN: "def actually_used():\n    return 1\n",
                                   USER: "import winmon_core as core\nprint(core.actually_used())\n"})
        _fresh, _known, stale = proj.findings("winmon_core.py::def:actually_used  # 早就修好了\n")
        self.assertEqual(stale, ["winmon_core.py::def:actually_used"])

    def test_baseline_matches_across_slashes(self):
        """基线里写正斜杠也能匹配（Windows 上生成的 key 用正斜杠归一化）。"""
        proj = _TempProject(self, {
            os.path.join("tests", "helper.py"): "def spare():\n    return 1\n",
        })
        fresh, known, _stale = proj.findings("tests/helper.py::def:spare  # 备用\n")
        self.assertEqual(fresh, [])
        self.assertEqual(len(known), 1)


class TestRealProjectIsClean(unittest.TestCase):
    """元检查：真实项目当前没有**新增**问题（有的话连同测试一起变红）。"""

    def test_no_new_lint_findings(self):
        fresh, _known, stale = lint_check.run()
        detail = "\n".join(f"  [{f['kind']}] {f['rel']}:{f['lineno']}  {f['detail']}"
                           for f in fresh)
        self.assertEqual(
            fresh, [],
            "lint_check 发现新增问题（修掉，或写进 tests/lint_baseline.txt 并写明理由）：\n"
            + detail)
        self.assertEqual(
            stale, [],
            "tests/lint_baseline.txt 里有已失效的条目，请删掉：\n  " + "\n  ".join(stale))


if __name__ == "__main__":
    unittest.main(verbosity=2)
