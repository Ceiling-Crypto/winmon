# -*- coding: utf-8 -*-
"""
代码复检（纯 AST，不引入第三方库）：
  · 未使用的 import
  · 定义了但从未被引用的模块级函数/类（疑似死代码）
  · 重复定义的函数名（后者会覆盖前者，通常是复制粘贴事故）
  · TODO/FIXME/XXX 遗留标记
  · 过长函数（超过 200 行，提示该拆）
  · 制表符 / 行尾空白

**跨文件引用**（本脚本最重要的约定）：一个模块级函数/类只有在
**整个项目（含 `tests/`）任何文件里都找不到引用**时才算疑似死代码。
早期版本只在这个文件内部找引用，于是 `winmon_core` 的公共 API（被 `winmon.py`
调用）与 `tests/_helpers.py` 的公共辅助（被各测试调用）**全部**被误报成死代码
（32 处全是噪音），"改完跑 lint 看看有没有新增问题"这条纪律因此形同虚设 ——
真出现死代码时，它和 32 条噪音混在一起，没人会注意到。

判定细节：
  · 引用形式 = `ast.Name`（直接调用）或 `ast.Attribute` 的属性名（`core.find_programs()`
    这种跨模块调用是属性，不是名字，漏掉它会再次误报公共 API）。
  · 字符串/注释里的提及**不算**引用（别把文档当成"用到了"）。
  · `__all__` 里列出的名字算"已导出"，不报。
  · `unittest.TestCase` 的子类由测试框架按发现规则实例化，名字不会出现在任何引用里，不报。
  · 函数只被自己递归调用不算被使用（排除定义体自身范围内的引用）。

**基线**：确实要保留的（例如"实测不可用但留作踩坑参照"的实验代码）写进
`tests/lint_baseline.txt`，每行 `相对路径::kind:名字   # 理由`。
基线里的条目会被抑制并单独列出；**只有新增问题**才让本脚本以非 0 退出。

用法：
    py tests\\lint_check.py            :: 有新增问题则退出码 1
"""
from __future__ import annotations

import ast
import os
import sys
from collections import Counter

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOP_FILES = ("winmon_core.py", "winmon.py", "winmon_ai.py", "winmon_mcp.py",
             "scan_windows.py", "close_others.py")
BASELINE_REL = os.path.join("tests", "lint_baseline.txt")
#: 别检查"检查器自己"：它里面的 TODO/FIXME 字面量与检查名会造成自指误报
SELF_NAME = "lint_check.py"
LONG_FUNC_LINES = 200
TESTCASE_BASE_SUFFIX = "TestCase"

#: 会做字符串搜索的遗留标记。注意这里写的是拼接结果，避免本文件自己被搜到。
LEGACY_TAGS = ("TO" + "DO", "FIX" + "ME", "X" + "XX", "HA" + "CK")


def project_files(root: str = ROOT) -> list[str]:
    """要检查的文件（相对 root 的路径），顺序稳定。"""
    files = [n for n in TOP_FILES if os.path.exists(os.path.join(root, n))]
    tests_dir = os.path.join(root, "tests")
    if os.path.isdir(tests_dir):
        files += [os.path.join("tests", n) for n in sorted(os.listdir(tests_dir))
                  if n.endswith(".py") and n != SELF_NAME]
    return files


def _read(path: str) -> str:
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def collect_refs(tree: ast.AST) -> list[tuple[str, int]]:
    """
    收集"用到某个名字"的位置：(名字, 行号)。

    包含 `ast.Name`、`ast.Attribute` 的属性名、以及 `__all__` 里的字符串。
    刻意**不**收字符串常量里的提及：注释和文档里出现函数名不算引用，
    否则任何被写进说明文字的函数都会被判成"有人在用"。
    """
    out: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            out.append((node.id, node.lineno))
        elif isinstance(node, ast.Attribute):
            out.append((node.attr, node.lineno))
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "__all__":
                    for sub in ast.walk(node):
                        if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
                            out.append((sub.value, node.lineno))
    return out


def _all_names(tree: ast.AST) -> set[str]:
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "__all__":
                    for sub in ast.walk(node):
                        if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
                            names.add(sub.value)
    return names


def _is_testcase(cls: ast.ClassDef) -> bool:
    """`unittest.TestCase` 的子类：由框架按发现规则实例化，不会被名字引用。"""
    for base in cls.bases:
        name = base.attr if isinstance(base, ast.Attribute) else getattr(base, "id", "")
        if name.endswith(TESTCASE_BASE_SUFFIX):
            return True
    return False


def _module_defs(tree: ast.AST) -> list[ast.AST]:
    return [n for n in tree.body
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))]


def _mentioned_in_strings(tree: ast.AST, name: str) -> bool:
    """名字出现在字符串常量里也算"用到了"（字符串形式的类型注解）。"""
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if name in node.value:
                return True
    return False


def analyze_file(rel: str, src: str, refs: dict[str, list[tuple[str, int]]],
                 tree: ast.AST | None = None) -> list[dict]:
    """检查单个文件，返回问题列表（未过滤基线）。"""
    if tree is None:
        tree = ast.parse(src, filename=rel)
    lines = src.splitlines()
    findings: list[dict] = []

    def add(lineno: int, kind: str, key: str, detail: str) -> None:
        findings.append({"rel": rel, "lineno": lineno, "kind": kind,
                         # 基线里统一用正斜杠，免得 Windows 的反斜杠对不上
                         "key": key.replace("\\", "/"), "detail": detail})

    # ---- 1) 未使用的 import ----
    imported: dict[str, tuple[int, str]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                imported[(a.asname or a.name).split(".")[0]] = (node.lineno, a.name)
        elif isinstance(node, ast.ImportFrom):
            # `from __future__ import annotations` 不是普通导入：
            # 它改变编译行为、名字本身永远"不被引用" —— 必须跳过，否则必然误报
            if node.module == "__future__":
                continue
            for a in node.names:
                if a.name == "*":
                    continue
                imported[a.asname or a.name] = (node.lineno, f"{node.module}.{a.name}")

    used = {name for name, _ in collect_refs(tree)}
    for name, (lineno, full) in sorted(imported.items()):
        if name in used or _mentioned_in_strings(tree, name):
            continue
        add(lineno, "未使用导入", f"{rel}::import:{name}", f"import {full}（名字 {name}）")

    # ---- 2) 重复定义（同名函数/类，后者覆盖前者）----
    counter: Counter = Counter()
    where: dict[str, list[int]] = {}
    for node in _module_defs(tree):
        counter[node.name] += 1
        where.setdefault(node.name, []).append(node.lineno)
    duplicated = {n for n, c in counter.items() if c > 1}
    for name in sorted(duplicated):
        add(where[name][0], "重复定义", f"{rel}::dup:{name}",
            f"{name} 定义了 {counter[name]} 次，行 {where[name]}")

    # ---- 3) 模块级函数/类在整个项目内都没被引用 ----
    exported = _all_names(tree)
    for node in _module_defs(tree):
        name = node.name
        if name in exported or name in duplicated:
            continue
        if isinstance(node, ast.ClassDef) and _is_testcase(node):
            continue
        start, end = node.lineno, (node.end_lineno or node.lineno)
        referenced = False
        for ref_rel, ref_line in refs.get(name, ()):
            # 别的文件里出现就算被引用；本文件里要排除"只有自己递归调用自己"
            if ref_rel != rel or ref_line < start or ref_line > end:
                referenced = True
                break
        if not referenced:
            what = "类" if isinstance(node, ast.ClassDef) else "函数"
            add(node.lineno, "可能死代码", f"{rel}::def:{name}",
                f"{name}()（{what}）在整个项目内都找不到引用")

    # ---- 4) 遗留标记 ----
    for i, line in enumerate(lines, 1):
        for tag in LEGACY_TAGS:
            if tag in line and not line.strip().startswith("#!"):
                add(i, "遗留标记", f"{rel}::tag:{tag}", f"{tag}: {line.strip()[:80]}")
                break

    # ---- 5) 过长函数 ----
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            n = (node.end_lineno or node.lineno) - node.lineno
            if n > LONG_FUNC_LINES:
                add(node.lineno, "函数过长", f"{rel}::long:{node.name}",
                    f"{node.name}() {n} 行，建议拆分")

    # ---- 6) 制表符 / 行尾空白 ----
    tabs = [i for i, line in enumerate(lines, 1) if "\t" in line]
    trail = [i for i, line in enumerate(lines, 1) if line != line.rstrip()]
    if tabs:
        add(tabs[0], "制表符", f"{rel}::ws:tab", f"{len(tabs)} 行含 tab（项目用 4 空格）")
    if trail:
        add(trail[0], "行尾空白", f"{rel}::ws:trail", f"{len(trail)} 行有行尾空白")
    return findings


def load_baseline(path: str) -> dict[str, str]:
    """读基线：每行 `相对路径::kind:名字  # 理由`，`#` 之后是理由。"""
    baseline: dict[str, str] = {}
    if not os.path.exists(path):
        return baseline
    for raw in _read(path).splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        key, _, reason = line.partition("#")
        key = key.strip().replace("\\", "/")
        if key:
            baseline[key] = reason.strip() or "（未写理由）"
    return baseline


def run(root: str = ROOT, baseline_path: str | None = None
        ) -> tuple[list[dict], list[dict], list[str]]:
    """
    检查整个项目。

    返回 (新增问题, 基线内已知问题, 基线里已失效的条目)。
    只看新增问题来决定成败 —— 这正是"改完跑 lint 看有没有**新**问题"的意思。
    """
    files = project_files(root)
    trees: dict[str, ast.AST] = {}
    sources: dict[str, str] = {}
    refs: dict[str, list[tuple[str, int]]] = {}
    for rel in files:
        src = _read(os.path.join(root, rel))
        tree = ast.parse(src, filename=rel)
        trees[rel] = tree
        sources[rel] = src
        for name, lineno in collect_refs(tree):
            refs.setdefault(name, []).append((rel, lineno))

    all_findings: list[dict] = []
    for rel in files:
        all_findings += analyze_file(rel, sources[rel], refs, trees[rel])

    baseline = load_baseline(baseline_path or os.path.join(root, BASELINE_REL))
    fresh = [f for f in all_findings if f["key"] not in baseline]
    known = [f for f in all_findings if f["key"] in baseline]
    stale = sorted(set(baseline) - {f["key"] for f in all_findings})
    return fresh, known, stale


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    fresh, known, stale = run()

    print(f"检查 {len(project_files())} 个文件"
          f"（跨文件统计引用，含 tests/）\n")
    by_file: dict[str, list[dict]] = {}
    for f in fresh:
        by_file.setdefault(f["rel"], []).append(f)
    for rel in project_files():
        src = _read(os.path.join(ROOT, rel))
        print(f"=== {rel}（{len(src.splitlines())} 行）===")
        items = by_file.get(rel, [])
        if not items:
            print("  未发现问题")
        for f in items:
            print(f"  [{f['kind']}] {f['rel']}:{f['lineno']}  {f['detail']}")

    print(f"\n{'=' * 60}")
    if known:
        print(f"基线内已知问题 {len(known)} 处（tests/lint_baseline.txt，已接受）：")
        for f in known:
            print(f"  · 【{f['kind']}】{f['rel']}:{f['lineno']} {f['detail']}")
    if stale:
        print(f"基线里有 {len(stale)} 条已失效（问题已消失，建议删掉）：")
        for key in stale:
            print(f"  · {key}")
    print(f"新增问题 {len(fresh)} 处")
    print("=" * 60)
    if fresh:
        print("有新增问题：请修掉，或确属有意保留就写进 tests/lint_baseline.txt（要写理由）。")
        return 1
    print("没有新增问题。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
