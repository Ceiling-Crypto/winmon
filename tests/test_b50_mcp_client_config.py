# -*- coding: utf-8 -*-
"""
B50 —— 2026-10-24：MCP 接入片段按客户端生成。

起因是用户的一次真实接入事故：把 Claude Desktop 的 `mcpServers` 片段
原样写进 DSH 的 `cordis.patch.yml`，DSH 只往 stderr 打了一行

    patch: entry "mcp-winmon" not found

界面不弹、会话照开，看起来像"配置没生效"。原因是 DSH 的 patch 文件
**不是清单而是操作列表**：顶层 `- id:` 表示"覆盖已有条目"，新增必须写
`- insert:`。所以这一组钉住：

  · `claude_config()` 仍然只给 `mcpServers`，且指向当前解释器（b34 的约定）；
  · `dsh_patch()` 第一行有效内容必须是 `- insert:`，且 `- id:` 必须是缩进的；
  · 路径按当前机器填绝对路径，不写死盘符；
  · 命令行 `--mcp-config` 三种用法（默认 / 单客户端 / 不认识的名字）；
  · GUI 的「复制配置」与命令行**同源**，不能各印一套。
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import winmon_mcp as mcp                 # noqa: E402


def _effective_lines(text: str) -> list[str]:
    """去掉注释与空行后的有效行（判断"第一行是什么"用）。"""
    return [ln for ln in text.splitlines()
            if ln.strip() and not ln.lstrip().startswith("#")]


class TestClaudeSnippet(unittest.TestCase):
    def test_uses_current_interpreter_and_script(self):
        data = json.loads(mcp.claude_config())
        server = data["mcpServers"]["winmon"]
        expected = sys.executable or "python"
        if expected.lower().endswith("pythonw.exe"):
            expected = expected[:-11] + "python.exe"
        self.assertEqual(server["command"], expected,
                         "MCP 配置必须指向当前解释器，不能假设 PATH 里有 python")
        self.assertTrue(os.path.isabs(server["args"][0]))
        self.assertEqual(server["args"][1], "--mcp-stdio")
        self.assertTrue(os.path.isfile(server["args"][0]))

    def test_pythonw_is_rewritten_to_python(self):
        text = mcp.claude_config(script=r"C:\x\winmon.py",
                                 interpreter=r"C:\x\pythonw.exe")
        self.assertEqual(json.loads(text)["mcpServers"]["winmon"]["command"],
                         r"C:\x\python.exe")


class TestDshSnippet(unittest.TestCase):
    def test_first_effective_line_is_insert(self):
        """顶层直接写 `- id:` 会被 DSH 当成覆盖，必须显式 insert。"""
        lines = _effective_lines(mcp.dsh_patch())
        self.assertEqual(lines[0], "- insert:",
                         "DSH 片段的第一行有效内容必须是 `- insert:`，"
                         "否则会被解析成'覆盖已有条目'并静默跳过")
        self.assertTrue(any(ln.strip() == "- id: mcp-winmon" for ln in lines))
        for line in lines:
            self.assertFalse(line.startswith("- id:"),
                             "顶层不能出现 `- id:`，那会被当成覆盖：%r" % line)

    def test_carries_entry_config(self):
        text = mcp.dsh_patch()
        for needle in ('name: "@deepseek-ai/dsh-mcp-client"',
                       "serverName: winmon",
                       "transport: stdio",
                       "'--mcp-stdio'",
                       "toolCallTimeoutMs: 120000",
                       "PYTHONUTF8: '1'",
                       "PYTHONIOENCODING: 'utf-8'"):
            self.assertIn(needle, text)

    def test_absolute_paths_and_cwd(self):
        text = mcp.dsh_patch()
        self.assertIn("cwd: '", text)
        script = os.path.join(os.path.dirname(os.path.abspath(mcp.__file__)), "winmon.py")
        self.assertIn("'%s'" % os.path.abspath(script), text)
        self.assertIn("cwd: '%s'" % os.path.dirname(os.path.abspath(script)), text)

    def test_is_yaml_not_json_and_has_no_cr(self):
        text = mcp.dsh_patch()
        self.assertNotIn("mcpServers", text,
                         "DSH 不认声明式 mcpServers，混进去只会误导用户")
        self.assertNotIn("\r", text)


class TestClientDispatch(unittest.TestCase):
    def test_client_config_matches_helpers(self):
        self.assertEqual(mcp.client_config("claude"), mcp.claude_config())
        self.assertEqual(mcp.client_config("dsh"), mcp.dsh_patch())
        self.assertEqual(mcp.client_config("DSH"), mcp.dsh_patch())

    def test_unknown_client_raises(self):
        with self.assertRaises(ValueError):
            mcp.client_config("cursor-pro")

    def test_client_configs_has_both(self):
        both = mcp.client_configs()
        self.assertEqual(sorted(both), ["claude", "dsh"])

    def test_gui_snippet_is_the_same_source(self):
        """界面「复制配置」必须与命令行同源，否则用户照着哪份配都可能对不上。"""
        import winmon
        self.assertEqual(winmon._AiConnectWindow._config_snippet(None),
                         mcp.claude_config())


class TestConfigCli(unittest.TestCase):
    def _run(self, kind):
        out, err = io.StringIO(), io.StringIO()
        import winmon
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = winmon.mcp_config_cli(kind)
        return code, out.getvalue(), err.getvalue()

    def test_single_client_prints_only_that_one(self):
        code, out, _err = self._run("dsh")
        self.assertEqual(code, 0)
        self.assertIn("- insert:", out)
        self.assertNotIn("mcpServers", out)

    def test_all_prints_both_with_headers(self):
        code, out, _err = self._run("all")
        self.assertEqual(code, 0)
        self.assertIn("Claude Desktop", out)
        self.assertIn("DeepSeek Harness", out)
        self.assertIn("mcpServers", out)
        self.assertIn("- insert:", out)
        self.assertIn("不能互换", out)

    def test_unknown_client_exits_2(self):
        code, out, err = self._run("cursor")
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn("不认识的客户端", err)

    def test_empty_kind_means_all(self):
        code, out, _err = self._run("")
        self.assertEqual(code, 0)
        self.assertIn("- insert:", out)

    def test_flag_forms_do_not_mistake_a_switch_for_a_client(self):
        import winmon
        self.assertEqual(winmon._mcp_config_kind(["--mcp-config"]), "all")
        self.assertEqual(winmon._mcp_config_kind(["--mcp-config", "--json"]), "all")
        self.assertEqual(winmon._mcp_config_kind(["--mcp-config=dsh"]), "dsh")
        self.assertEqual(winmon._mcp_config_kind(["--mcp-config", "DSH"]), "dsh")


if __name__ == "__main__":
    unittest.main()
