# -*- coding: utf-8 -*-
"""
A1 —— 语法与编码安全。

这组测试对应 HANDOFF.md 里"最容易毁掉成果"的编码坑：
当时曾用 `Get-Content -Raw | Set-Content -Encoding UTF8` 回写 `winmon.py`，
中文按系统代码页重新解码再写回，**96% 的汉字被静默替换成 `?`**，
且不可逆（GBK 双字节解码是多对一，丢失的字节无法还原），核心文件只能重写。

只靠"能编译通过"是发现不了这种损坏的：ASCII 问号是合法 Python。
所以这里额外做两件事：
  1. 统计非 ASCII 字符占比 —— 中文字符全部变成 `?` 后该比例会塌到 0；
  2. 检测"被错误解码"的汉字特征区（U+0080~U+00FF 占比过高）。
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _helpers import DOC_FILES, PROJECT_DIR, SOURCE_FILES  # noqa: E402


def _read_bytes(name: str) -> bytes:
    with open(os.path.join(PROJECT_DIR, name), "rb") as fh:
        return fh.read()


class TestSyntax(unittest.TestCase):
    """四个源文件都必须能通过 `compile()`。"""

    def test_source_files_exist(self):
        for name in SOURCE_FILES:
            with self.subTest(file=name):
                self.assertTrue(os.path.isfile(os.path.join(PROJECT_DIR, name)),
                                f"{name} 不存在")

    def test_all_sources_compile(self):
        for name in SOURCE_FILES:
            with self.subTest(file=name):
                src = _read_bytes(name).decode("utf-8")
                try:
                    compile(src, name, "exec")
                except SyntaxError as exc:
                    self.fail(f"{name} 语法错误：第 {exc.lineno} 行 {exc.msg}")

    def test_no_source_is_empty(self):
        """文件被误清空/截断时立刻报错，而不是让后续 import 报奇怪的错。"""
        for name in SOURCE_FILES:
            with self.subTest(file=name):
                self.assertGreater(len(_read_bytes(name)), 200, f"{name} 内容异常地少")

    def test_import_winmon_core_is_tkinter_free(self):
        """
        核心层必须能在没有 tkinter 的环境里用（HANDOFF 承诺"可当库用"）。

        做法：先确认 tkinter 没被导入过，再 import 核心层，检查 sys.modules。
        （本测试进程里可能已经因为别的测试导入过 tkinter，所以只做"核心层不主动导入"
        的静态检查 + import 成功检查。）
        """
        src = _read_bytes("winmon_core.py").decode("utf-8")
        self.assertNotIn("import tkinter", src,
                         "winmon_core.py 不应依赖 tkinter（它要能当纯库用）")
        import winmon_core                                        # noqa: F401


class TestEncodingSafety(unittest.TestCase):
    """源文件必须是干净的 UTF-8，且中文没有被损坏。"""

    def test_utf8_decodable(self):
        for name in SOURCE_FILES:
            with self.subTest(file=name):
                raw = _read_bytes(name)
                try:
                    raw.decode("utf-8")
                except UnicodeDecodeError as exc:
                    self.fail(f"{name} 不是合法 UTF-8：{exc}")

    def test_no_utf8_bom(self):
        """带 BOM 的 UTF-8 会让某些工具误判编码，项目约定不带 BOM。"""
        for name in SOURCE_FILES:
            with self.subTest(file=name):
                self.assertFalse(_read_bytes(name).startswith(b"\xef\xbb\xbf"),
                                 f"{name} 含 UTF-8 BOM")

    def test_no_utf16_encoding(self):
        """UTF-16 的典型特征是大量 NUL 字节 —— 回写成 UTF-16 也是常见事故。"""
        for name in SOURCE_FILES:
            with self.subTest(file=name):
                raw = _read_bytes(name)
                self.assertNotIn(b"\x00", raw, f"{name} 含 NUL 字节（疑似 UTF-16 写出）")

    def test_declares_utf8_coding(self):
        for name in SOURCE_FILES:
            with self.subTest(file=name):
                head = _read_bytes(name).decode("utf-8").splitlines()[:2]
                joined = "\n".join(head).lower().replace(" ", "")
                self.assertIn("coding:utf-8", joined,
                              f"{name} 头两行缺少 `# -*- coding: utf-8 -*-` 声明")

    def test_chinese_text_still_present(self):
        """
        中文损坏的"静默杀手"：Set-Content 回写会把汉字变成 `?`。

        判据：界面/文档里有大量中文的源文件，其**非 ASCII 字符比例**不应塌陷。
        实测损坏前约 6~8%，损坏后约 0%，阈值 1% 有充分余量。
        """
        for name in SOURCE_FILES:
            with self.subTest(file=name):
                text = _read_bytes(name).decode("utf-8")
                total = len(text)
                han = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
                ratio = han / max(1, total)
                self.assertGreater(
                    ratio, 0.01,
                    f"{name} 的汉字占比只有 {ratio:.4%}，中文疑似已被替换成 '?'"
                    f"（非 ASCII 码点共 {sum(1 for c in text if ord(c) > 127)} 个）")

    def test_no_mojibake_range(self):
        """
        另一种损坏形态：UTF-8 字节被按 GBK 解码后重写。

        这时文本里会充满 U+0080~U+00FF 区间（GBK 双字节码位）的"乱码"字符，
        而正常中文源码的非 ASCII 字符几乎全在 U+4E00 以上。
        判据：非 ASCII 字符中落在 U+0080~U+00FF 的比例不应超过 20%
        （正常源码里该区间只有 "×" "°" 之类极少数符号，实测 < 2%）。
        """
        for name in SOURCE_FILES:
            with self.subTest(file=name):
                text = _read_bytes(name).decode("utf-8")
                nonascii = [ch for ch in text if ord(ch) > 127]
                if not nonascii:
                    continue                      # 纯 ASCII 文件不适用
                latin1 = sum(1 for ch in nonascii if 0x80 <= ord(ch) <= 0xFF)
                ratio = latin1 / len(nonascii)
                self.assertLess(
                    ratio, 0.20,
                    f"{name} 的非常规字符中有 {ratio:.1%} 落在 U+0080~U+00FF，"
                    f"疑似被按错误代码页解码（乱码）")

    def test_no_question_mark_runs(self):
        """
        找"中文被替换成 `?`"的损坏痕迹。

        不能用"出现连续 `?`"当判据 —— 源码里本来就有合法的连续问号
        （例如注释里写 `0xFFFFFFFF????????` 举例 64 位句柄的高位、URL 查询串）。
        真正的损坏特征是**问号与汉字相邻**：编码事故只会把汉字逐个变成 `?`，
        于是必然出现「汉字紧挨着 ???」。判据三条，任一命中即判为损坏：
          · 汉字 + 3 个以上连续 `?`（或反向）
          · 10 个以上连续 `?`（远超任何正常写法的需要）
          · 剥掉字符串字面量与注释后，单行仍有 4 个以上孤立 `?`

        最后一条必须**先在 AST 层面剥掉字符串**：正则表达式、
        SQL、URL 模板里本来就会有不少 `?`（本项目就有一条
        `VIDEO_CARD_RE`，里面 5 个 `?` 都是合法的非贪婪/命名组语法）。
        """
        import ast
        import re

        def masked_source(text: str) -> list[str]:
            """把字符串字面量替换成占位符，返回逐行文本（用于数孤立问号）。"""
            lines = text.splitlines()
            try:
                tree = ast.parse(text)
            except SyntaxError:
                return lines
            spots = []
            for node in ast.walk(tree):
                if isinstance(node, ast.Constant) and isinstance(node.value, str):
                    if node.lineno and node.end_lineno:
                        spots.append((node.lineno, node.col_offset,
                                      node.end_lineno, node.end_col_offset))
            for (ln0, c0, ln1, c1) in spots:
                if ln0 == ln1:
                    row = lines[ln0 - 1]
                    lines[ln0 - 1] = row[:c0] + (" " * (c1 - c0)) + row[c1:]
                else:
                    lines[ln0 - 1] = lines[ln0 - 1][:c0]
                    for mid in range(ln0, ln1 - 1):
                        lines[mid] = ""
                    lines[ln1 - 1] = lines[ln1 - 1][c1:]
            return lines

        for name in SOURCE_FILES:
            with self.subTest(file=name):
                text = _read_bytes(name).decode("utf-8")
                failures = []

                m = re.search(r"[\u4e00-\u9fff]\?{3,}|\?{3,}[\u4e00-\u9fff]", text)
                if m:
                    failures.append(f"汉字与连续问号相邻：{m.group(0)!r}")

                m = re.search(r"\?{10,}", text)
                if m:
                    failures.append(f"{len(m.group(0))} 个连续问号")

                for lineno, line in enumerate(masked_source(text), 1):
                    code = line.split("#", 1)[0]        # 顺带去掉行尾注释
                    if len(re.findall(r"(?<!\?)\?(?!\?)", code)) >= 4:
                        failures.append(f"第 {lineno} 行有多个孤立问号")
                        break

                self.assertEqual(failures, [],
                                 f"{name} 出现疑似中文损坏痕迹：{'；'.join(failures)}")


class TestDocsEncoding(unittest.TestCase):
    """
    文档（README / HANDOFF / CHANGELOG）也必须是不带 BOM 的合法 UTF-8，
    且中文没被损坏。

    这一组是**踩坑后补的**：用 PowerShell 的 `Set-Content -Encoding UTF8`
    批量替换文档里的数字时，内容没坏但被加了 BOM，而项目约定是不带 BOM。
    """

    def _files(self):
        return [os.path.join(PROJECT_DIR, n) for n in DOC_FILES]

    def test_docs_exist_and_are_utf8(self):
        for path in self._files():
            with self.subTest(file=os.path.basename(path)):
                self.assertTrue(os.path.isfile(path), f"{path} 不存在")
                raw = open(path, "rb").read()
                try:
                    raw.decode("utf-8")
                except UnicodeDecodeError as exc:
                    self.fail(f"{os.path.basename(path)} 不是合法 UTF-8：{exc}")

    def test_docs_have_no_bom(self):
        for path in self._files():
            with self.subTest(file=os.path.basename(path)):
                raw = open(path, "rb").read()
                self.assertFalse(raw.startswith(b"\xef\xbb\xbf"),
                                 f"{os.path.basename(path)} 含 UTF-8 BOM"
                                 f"（多半是被 Set-Content 重写过）")

    def test_docs_chinese_is_intact(self):
        for path in self._files():
            with self.subTest(file=os.path.basename(path)):
                text = open(path, encoding="utf-8").read()
                han = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
                self.assertGreater(han, 500,
                                   f"{os.path.basename(path)} 汉字只有 {han} 个，"
                                   f"疑似被错误编码重写")
                nonascii = [ch for ch in text if ord(ch) > 127]
                latin1 = sum(1 for ch in nonascii if 0x80 <= ord(ch) <= 0xFF)
                ratio = latin1 / max(1, len(nonascii))
                self.assertLess(ratio, 0.20,
                                f"{os.path.basename(path)} 有 {ratio:.1%} 的字符落在"
                                f" U+0080~U+00FF，疑似乱码")


class TestHandoffClaims(unittest.TestCase):
    """HANDOFF 里"已验证通过"的硬指标，代码层面必须留有对应实现。"""

    def test_icon_size_invariant_documented(self):
        """图标清晰度依赖"Treeview 行高 > 图标高度"，这条约定不能悄悄丢。"""
        src = _read_bytes("winmon.py").decode("utf-8")
        self.assertIn("ICON_SIZE", src)
        self.assertRegex(src, r"rowheight\s*=\s*ICON_SIZE\s*\+\s*\d+")
