# -*- coding: utf-8 -*-
"""``trading.__main__`` 入口契约锁（C-5 V1：uvicorn 薄壳改造后）。

测试边界（plan 未要求 __main__ 全量单测，本测试为契约锁）：
- 锁住「入口模块可 import」契约：import trading.__main__ 不崩、不阻塞（uvicorn.run
  在 if __name__ 守卫内，import 不起 server）。
- 锁住「run_server 是可调用对象」契约（V1：取代废弃的 _run_forever）。
- 锁住「run_server 起 uvicorn :8000，live 模式 reload=False」契约（spec §3.1/§3.3）。
- 锁住「_run_forever 已废弃」契约（V1：源码不再含 async def _run_forever 定义）。

物理意图（C-5 V1）：
    __main__ 从「独立起 engine 的 _run_forever」改造为「起 uvicorn 让 lifespan 装
    engine」，消除双进程抢 QMT_SESSION_ID（07-29 故障根因）。uvicorn bind 8000 天然
    单例（第二实例 WSAEADDRINUSE exit），无需文件锁。
"""
from __future__ import annotations

import trading.__main__ as main_mod


def test_module_importable():
    """入口模块可 import 不崩（锁住「入口可 import」契约）。"""
    assert main_mod is not None
    assert main_mod.logger is not None


def test_run_forever_removed():
    """V1：_run_forever 已废弃（源码不再定义 async def _run_forever）。

    物理意图：_run_forever 独立装配 engine 是双进程抢 session 的根因，V1 废弃后
    engine 只由 uvicorn lifespan 装配。若未来误恢复 _run_forever，本测试即红。
    """
    import pathlib
    src = pathlib.Path(main_mod.__file__).read_text(encoding="utf-8")
    assert "async def _run_forever" not in src, (
        "_run_forever 已在 C-5 V1 废弃（engine 装配收归 uvicorn lifespan），"
        "源码不应再定义 async def _run_forever")


def test_run_server_is_callable():
    """V1：run_server 是可调用对象（取代 _run_forever 的入口契约）。"""
    assert callable(main_mod.run_server)


def test_run_server_calls_uvicorn_port_8000(monkeypatch):
    """V1：run_server 起 uvicorn :8000（spec §3.1/§3.3 端口单例基线）。

    mock uvicorn.run 捕获调用参数，断言 app 指向 lifespan、port=8000、host 非空。
    不真起 server（uvicorn.run 被 lambda 替换）。
    """
    captured = {}

    def _fake_run(app, **kw):
        captured["app"] = app
        captured.update(kw)

    monkeypatch.setattr("uvicorn.run", _fake_run)
    monkeypatch.setenv("SERVER_PORT", "8000")
    monkeypatch.setenv("AUTO_TRADE_MODE", "dry_run")
    main_mod.run_server()
    assert captured["app"] == "presentation.server.main:app"
    assert captured["port"] == 8000
    assert captured["host"]  # 非空


def test_run_server_live_no_reload(monkeypatch):
    """V1 R6：live 模式 reload=False（reload 起 reloader 子进程抢 session，自扰）。

    spec §3.1 物理意图：reload 模式 uvicorn 会 fork reloader 子进程，子进程再次
    gw.connect() 抢同一 QMT_SESSION_ID → 自扰性断线。live 显式 reload=False。
    """
    captured = {}
    monkeypatch.setattr("uvicorn.run", lambda app, **kw: captured.update(kw))
    monkeypatch.setenv("AUTO_TRADE_MODE", "live")
    main_mod.run_server()
    assert captured.get("reload") is False, "live 模式必须 reload=False（防子进程抢 session）"


def test_run_server_dry_run_reload_true(monkeypatch):
    """V1：dry_run 模式 reload=True（开发热重载便利，无真网关无抢 session 风险）。"""
    captured = {}
    monkeypatch.setattr("uvicorn.run", lambda app, **kw: captured.update(kw))
    monkeypatch.setenv("AUTO_TRADE_MODE", "dry_run")
    main_mod.run_server()
    assert captured.get("reload") is True


def test_no_asyncio_run_at_top_level():
    """V1：模块顶层无裸 asyncio.run（import 不阻塞红线）。

    _run_forever 废弃后，__main__ 不再 asyncio.run。若未来误在模块顶层（if __name__
    守卫外）调 asyncio.run / uvicorn.run，import 会阻塞。本测试 AST 级断言：顶层
    非守卫位置不得出现 asyncio.run 或 uvicorn.run 调用。
    """
    import ast
    import pathlib

    src = pathlib.Path(main_mod.__file__).read_text(encoding="utf-8")
    tree = ast.parse(src)
    for node in tree.body:
        # 函数体内的调用不算（run_server 内部 import uvicorn 合法）
        if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)):
            continue
        # if __name__ == "__main__" 守卫块内的调用合法
        if isinstance(node, ast.If):
            continue
        # 其它顶层节点（import/赋值/裸表达式）不得是 asyncio.run / uvicorn.run 调用
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
            func = node.value.func
            if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
                if func.attr == "run" and func.value.id in ("asyncio", "uvicorn"):
                    raise AssertionError(
                        f"{func.value.id}.run() 不应在模块顶层调用（必须在 "
                        "run_server() 内或 if __name__ 守卫内，否则 import 阻塞）")


def test_log_startup_banner_includes_git_rev_and_started(monkeypatch, caplog):
    """A3: banner 必须含 git 版本与启动时间（代码更新后未重启可识别）。"""
    import logging

    monkeypatch.setattr(main_mod, "_git_rev", lambda: "abc1234")
    with caplog.at_level(logging.INFO, logger="trading.__main__"):
        main_mod.log_startup_banner()
    assert "git=abc1234" in caplog.text
    assert "started=" in caplog.text
