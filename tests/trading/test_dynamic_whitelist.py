# -*- coding: utf-8 -*-
"""动态白名单单测（Task 5）。

覆盖两件事：
1. dynamic_whitelist 模块本身的 inject/clear/effective 三函数语义（brief 原文用例）；
2. 端到端连线证据：trading_service._whitelist() 真的消费了动态注入——
   否则动态白名单是空转的（信号标的永远过不了 risk_shield 关5）。

Why 端到端用例不能省：模块单测只证明 dw.get_effective_whitelist() 正确，
但 engine 自动下单走的是 trading_service.submit_order → check_order(whitelist=_whitelist())，
若 _whitelist() 没改成调用 dw.get_effective_whitelist()，注入的标的仍被关5拒。
"""
from trading import dynamic_whitelist as dw


def test_inject_then_clear(monkeypatch):
    """模块层语义：静态 env ∪ 动态注入；clear 后回到纯 env。"""
    monkeypatch.setenv("QMT_SYMBOL_WHITELIST", "510300.SH,159915.SZ")
    dw.clear_dynamic_whitelist()
    assert dw.get_effective_whitelist() == {"510300.SH", "159915.SZ"}
    dw.inject_dynamic_whitelist({"600000.SH", "000001.SZ"})
    assert dw.get_effective_whitelist() == {"510300.SH", "159915.SZ", "600000.SH", "000001.SZ"}
    dw.clear_dynamic_whitelist()
    assert dw.get_effective_whitelist() == {"510300.SH", "159915.SZ"}


def test_service_whitelist_consumes_dynamic(monkeypatch):
    """端到端连线：trading_service._whitelist 必须消费 dw.get_effective_whitelist。

    场景：env 静态只配 1 只 ETF（510300.SH），engine 注入个股 600000.SH。
    断言：inject 后 _whitelist() 返回 {ETF, 个股}；clear 后回到 {ETF}。

    Why monkeypatch dw 模块级 _DYNAMIC：上条用例已 mutate 过模块全局，
    每个用例开头 clear 保证隔离；env 也用 monkeypatch 保证不污染其他用例。
    """
    # 延迟 import：避免在 collection 期触发 server 包初始化副作用（若 server 模块
    # import 路径重，可在 conftest 层 fixture 化；此处直接 import 已足够）。
    from server.services import trading_service

    monkeypatch.setenv("QMT_SYMBOL_WHITELIST", "510300.SH")
    dw.clear_dynamic_whitelist()

    # 基线：无动态注入时 = 纯 env，行为等价于旧 _whitelist()（向后兼容红线）
    assert trading_service._whitelist() == {"510300.SH"}

    # 注入个股后：server 路径的白名单必须放行该个股
    dw.inject_dynamic_whitelist({"600000.SH"})
    assert trading_service._whitelist() == {"510300.SH", "600000.SH"}

    # 清空后回到纯 env
    dw.clear_dynamic_whitelist()
    assert trading_service._whitelist() == {"510300.SH"}
