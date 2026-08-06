# -*- coding: utf-8 -*-
"""动态白名单单测（Task 5）。

覆盖两件事：
1. dynamic_whitelist 模块本身的 inject/clear/effective 三函数语义（A-3 清理前仍被
   engine pre_open 注入/清空调用，先锁住语义防误删）；
2. A-2/D3 后 trading_service 不再消费白名单（_whitelist helper 已删，check_order 无
   whitelist 关）——注入标的不再影响下单判定，防「旧白名单复活」回归。
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


def test_service_no_longer_consumes_whitelist():
    """A-2/D3：trading_service._whitelist 已删，白名单不再参与下单判定。

    防止「白名单挡板复活」：若未来有人恢复 _whitelist helper 并接回 check_order，
    本用例即红（配合 test_risk_shield 的 outside_whitelist_no_longer_blocks）。
    """
    from presentation.server.services import trading_service
    assert not hasattr(trading_service, "_whitelist")
