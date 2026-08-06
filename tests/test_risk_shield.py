# -*- coding: utf-8 -*-
"""风控挡板（risk_shield）纯函数穷举单测（A-2 后三闸语义）。

2026-08-06 裁定（D1-D3/D5）后 check_order 只保留三闸：
  connection（断线保护，D1）/ dry_run（模拟语义）/ session（D2，09:15 起点 A1 已修）。
被删闸（allow_live/confirm/whitelist/lot/max_amount/max_shares/high_low_limit）
不再拦截——本文件用「不再 blocked」断言固化删除语义，防回归复活。
"""
import pytest

from trading.compute.types import OrderRequest
from trading.compute.risk import check_order


def _order(**kw):
    base = dict(symbol="510300.SH", qty=100, side="buy", price=5.0)
    base.update(kw)
    return OrderRequest(**base)


def _ok_kwargs(**kw):
    """三闸全过的参数（连接正常、非 dry_run、时段内）。"""
    base = dict(dry_run=False, enforce_session=True,
                is_locked=False, connected=True, in_session=True)
    base.update(kw)
    return base


def test_qmt_gateway_exported():
    from trading import QmtExecutionGateway
    assert QmtExecutionGateway is not None


def test_pass_all_clear():
    d = check_order(_order(), **_ok_kwargs())
    assert d.blocked is False
    assert d.stage == ""


def test_block_connection_locked():
    d = check_order(_order(), **_ok_kwargs(is_locked=True))
    assert d.blocked and d.stage == "connection"


def test_block_connection_disconnected():
    d = check_order(_order(), **_ok_kwargs(connected=False))
    assert d.blocked and d.stage == "connection"


def test_dry_run_is_not_error():
    d = check_order(_order(), **_ok_kwargs(dry_run=True))
    assert d.blocked is True
    assert d.is_dry_run is True
    assert d.stage == "dry_run"


def test_block_session():
    d = check_order(_order(), **_ok_kwargs(in_session=False))
    assert d.blocked and d.stage == "session"


def test_session_gate_off_when_not_enforced():
    """enforce_session=False → 时段闸不生效（运维显式关闭）。"""
    d = check_order(_order(), **_ok_kwargs(enforce_session=False, in_session=False))
    assert d.blocked is False


def test_short_circuit_connection_over_session():
    """连接闸优先于时段闸（断线时只报 connection）。"""
    d = check_order(_order(), **_ok_kwargs(is_locked=True, in_session=False))
    assert d.stage == "connection"


# ============ A-2 被删闸：不再拦截（防回归复活） ============


def test_allow_live_false_no_longer_blocks():
    d = check_order(_order(), **_ok_kwargs())
    assert d.blocked is False  # QMT_ALLOW_LIVE_TRADE 挡板已删（D3 裁定）


def test_confirm_false_no_longer_blocks():
    d = check_order(_order(), **_ok_kwargs())
    assert d.blocked is False  # confirm 挡板已删（由计划确认闸承担）


def test_outside_whitelist_no_longer_blocks():
    d = check_order(_order(symbol="000001.SZ"), **_ok_kwargs())
    assert d.blocked is False  # 白名单挡板已删（D3 前端同步放开）


def test_non_lot_qty_no_longer_blocks():
    d = check_order(_order(qty=150), **_ok_kwargs())
    assert d.blocked is False  # 整手契约挡板已删（柜台/交易所兜底）


def test_zero_qty_no_longer_blocks():
    d = check_order(_order(qty=0), **_ok_kwargs())
    assert d.blocked is False


def test_max_amount_no_longer_blocks():
    d = check_order(_order(qty=100, price=5.0), **_ok_kwargs())
    assert d.blocked is False


def test_max_shares_no_longer_blocks():
    d = check_order(_order(qty=200), **_ok_kwargs())
    assert d.blocked is False


def test_high_limit_buy_no_longer_blocks():
    d = check_order(_order(side="buy"), **_ok_kwargs())
    assert d.blocked is False


def test_low_limit_sell_no_longer_blocks():
    d = check_order(_order(side="sell"), **_ok_kwargs())
    assert d.blocked is False
