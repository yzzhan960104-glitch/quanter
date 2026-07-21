# -*- coding: utf-8 -*-
"""``trading.__main__`` 入口 import 冒烟测试（Task 10）。

测试边界（plan 未要求 __main__ 单测，本测试为契约锁）：
- 锁住「入口模块可 import」契约：``import trading.__main__`` 不崩。
- 锁住「``_run_forever`` 是可调用对象」契约（async 函数）。
- 锁住「``asyncio.run`` 必须在 ``if __name__ == "__main__"`` 守卫内」红线：
  import 时不阻塞（不会进 event loop）——若未来误把 asyncio.run 提到模块顶层，
  本测试会在 import 阶段阻塞/报错，即时暴露。
"""
from __future__ import annotations

import inspect

import trading.__main__ as main_mod


def test_module_importable():
    """入口模块可 import 不崩（锁住「入口可 import」契约）。"""
    assert main_mod is not None
    # 模块级 logger 与 _run_forever 是入口的两个关键对象
    assert main_mod.logger is not None
    assert hasattr(main_mod, "logger")


def test_run_forever_is_callable():
    """``_run_forever`` 是 async 可调用对象（锁住「入口守护函数存在」契约）。"""
    assert callable(main_mod._run_forever)
    # 必须是协程函数（async def），否则 asyncio.run 无法驱动
    assert inspect.iscoroutinefunction(main_mod._run_forever)


def test_asyncio_run_is_main_guarded():
    """``asyncio.run(_run_forever())`` 必须在 ``if __name__ == "__main__"`` 守卫内。

    红线：若 import 本模块时 asyncio.run 被误提到模块顶层，import 会阻塞。
    本测试通过「import 已成功」即隐含验证（若不在守卫内，本测试根本无法执行）。
    这里再做一次源码级断言，双重保险：
    """
    import ast
    import pathlib

    src = pathlib.Path(main_mod.__file__).read_text(encoding="utf-8")
    tree = ast.parse(src)
    # 收集模块顶层所有 asyncio.run 调用所在的「是否在 if __name__ == "__main__"」
    top_level = tree.body
    for node in top_level:
        if isinstance(node, ast.AsyncFunctionDef):
            continue  # 函数体内的 asyncio.run 不算（_run_forever 内部无 asyncio.run）
        if isinstance(node, ast.If):
            # __main__ 守卫块：检查其内是否有 asyncio.run 调用（允许）
            for child in ast.walk(node):
                if isinstance(child, ast.Call) and isinstance(child.func, ast.Attribute):
                    if (isinstance(child.func.value, ast.Name)
                            and child.func.value.id == "asyncio"
                            and child.func.attr == "run"):
                        return  # 找到了，在守卫内，契约达成
        elif isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
            func = node.value.func
            # 顶层裸 asyncio.run（无 if 守卫）= 违约
            if (isinstance(func, ast.Attribute)
                    and isinstance(func.value, ast.Name)
                    and func.value.id == "asyncio"
                    and func.attr == "run"):
                raise AssertionError(
                    "asyncio.run(_run_forever()) 不应在模块顶层调用（必须 "
                    "在 `if __name__ == '__main__'` 守卫内，否则 import 阻塞）"
                )
