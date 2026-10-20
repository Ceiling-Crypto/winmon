# -*- coding: utf-8 -*-
"""
测试标记：只做“这条用例要不要在 --fast 下跳过”，不依赖项目本体。

为什么单独一个文件：`run_tests.py` 在发现用例之前就要知道哪些是慢用例，
如果标记工具放进 `_helpers.py`，会连带 import `winmon_core`，让 `--list`
也变慢。这里只使用标准库，零副作用。
"""

from __future__ import annotations

#: 自定义属性名；刻意不用 unittest 的 `skip`（那是“这条不跑”，不是“慢”）。
SLOW_ATTR = "_winmon_slow"


def slow(reason: str = "真窗口 / 抓屏端到端"):
    """
    标记一条用例或一个测试类为“慢”。

    用法：
        @slow
        def test_real_window(...): ...

        @slow("每次要采 2 秒画面")
        class TestRealCapture(...): ...
    """
    if callable(reason):                 # @slow 不带括号
        setattr(reason, SLOW_ATTR, "真窗口 / 抓屏端到端")
        return reason

    text = str(reason or "慢用例")

    def mark(target):
        setattr(target, SLOW_ATTR, text)
        return target

    return mark


def slow_reason(case) -> str:
    """返回慢标记的理由；不是慢用例则返回空串。"""
    # 类级标记：getattr(case, ...) 会顺着实例找到类属性。
    reason = getattr(case, SLOW_ATTR, "") or getattr(type(case), SLOW_ATTR, "")
    if reason:
        return str(reason)
    # 方法级标记：bound method 不总是把函数属性透出来，显式看 __func__。
    method = getattr(case, getattr(case, "_testMethodName", ""), None)
    for target in (method, getattr(method, "__func__", None)):
        if target is not None:
            reason = getattr(target, SLOW_ATTR, "")
            if reason:
                return str(reason)
    return ""


def is_slow(case) -> bool:
    return bool(slow_reason(case))
