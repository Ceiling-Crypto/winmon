# -*- coding: utf-8 -*-
"""
WinMon 测试总入口 —— 只依赖 Python 标准库。

用法（在 winmon 目录下、任意 Python 3.9+）
------------------------------------------
    py tests\\run_tests.py              :: 跑全部用例
    py tests\\run_tests.py -v           :: 详细输出
    py tests\\run_tests.py -k capture   :: 只跑名字里含 capture 的用例
    py tests\\run_tests.py --list       :: 只列出会跑哪些用例，不执行

也可以用标准库的方式跑（结果等价）：
    py -m unittest discover -s tests -t tests -v

退出码：0 = 全绿（允许有 skip），1 = 有失败/错误。
"""

from __future__ import annotations

import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)


def _force_utf8_output() -> None:
    """
    把标准输出/错误切到 UTF-8 —— **必须放在最前面**，否则你会"看不到结果"。

    为什么（本机实测）：这一版的 Python 在中文 Windows 上默认把 stdout 编成
    **GBK(cp936)**，而 PowerShell（7 / 5.1 都算）拿 **UTF-8** 去解这些字节 ——
    于是中文全部变成乱码，严重时整行被吞掉，看起来就是"跑完什么都没输出"。
    表现对比（同一条命令，只是加不加 `-X utf8`）：

        py -c "print('结果：521 个用例，失败 0')"
        →  �����521 ��������ʧ�� 0

        py -X utf8 -c "print('结果：521 个用例，失败 0')"
        →  结果：521 个用例，失败 0

    `errors="replace"` 是兜底：窗口标题里可能有 GBK 编不出的字符
    （HANDOFF 坑 59：某个播放器标题里的 `▶` 就让 `--selftest` 崩过一次），
    宁可显示成问号，也不能让一行中文把整个测试跑废掉。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


_force_utf8_output()


def _iter_suite(suite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from _iter_suite(item)
        else:
            yield item


def _say(text: str = "") -> None:
    """
    打印一行并**立刻 flush**。

    为什么必须 flush：全量跑要 75 秒左右，而 stdout 连管道/重定向时是**块缓冲**的 ——
    不 flush 的话开头这句"发现 N 个用例"要等 75 秒跑完才一起出现。
    用户那边看到的就是"敲下去没反应/没有结果产出"（真被这么反馈过一次）。
    第一行必须**马上**出现，让人知道它在跑、跑了多少个。
    """
    print(text, flush=True)


def main(argv: list[str]) -> int:
    verbose = "-v" in argv or "--verbose" in argv
    argv = [a for a in argv if a not in ("-v", "--verbose")]

    keyword = ""
    if "-k" in argv:
        i = argv.index("-k")
        if i + 1 >= len(argv):
            print("-k 后面要跟关键词")
            return 2
        keyword = argv[i + 1]
        del argv[i:i + 2]

    loader = unittest.TestLoader()
    suite = loader.discover(HERE, pattern="test_*.py", top_level_dir=HERE)

    if keyword:
        keep = unittest.TestSuite()
        for case in _iter_suite(suite):
            if keyword.lower() in case.id().lower():
                keep.addTest(case)
        suite = keep

    if "--list" in argv:
        cases = list(_iter_suite(suite))
        _say(f"共 {len(cases)} 个用例：")
        for case in cases:
            print("  ", case.id())
        return 0

    cases = list(_iter_suite(suite))
    _say(f"发现 {len(cases)} 个用例（Python {sys.version.split()[0]}）")
    if keyword:
        _say(f"（筛选关键词 -k {keyword}：只跑名字里含它的用例）")
    if not cases:
        _say("没有匹配的用例 —— 关键词写错了吗？可用 --list 看不筛时有哪些用例。")
        return 2
    _say("=" * 72)
    _say(f"开始跑……（约 {max(1, len(cases) * 145 // 1000)} 秒，"
         f"结束时会有「结果：」那一行）")
    runner = unittest.TextTestRunner(verbosity=2 if verbose else 1, stream=sys.stdout)
    result = runner.run(suite)
    _say("=" * 72)
    skipped = len(result.skipped)
    _say(f"结果：{result.testsRun} 个用例，失败 {len(result.failures)}，"
         f"错误 {len(result.errors)}，跳过 {skipped}"
         + ("　→ 全绿 ✅" if result.wasSuccessful() else "　→ 有红灯 ❌"))
    if skipped and not verbose:
        _say("跳过的原因（用 -v 看每个用例）：")
        for case, reason in result.skipped:
            _say(f"  · {case.id().split('.')[-1]}：{reason}")
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
