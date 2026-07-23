# -*- coding: utf-8 -*-
"""风控挡板（risk_shield）纯函数穷举单测。

覆盖 10 关短路 + dry_run 模拟语义（is_dry_run=True 不算错误）+ 全过放行。
挡板是纯函数：所有外部数据（quote/连接状态/dry_run）由参数注入，确定性可测。
"""
import pytest

from trading.compute.types import OrderRequest  # Layer2 阶段6 follow-up #4b：execution_gateway 垫片已删，直指 compute.types 真身
from trading.compute.risk import RiskDecision, check_order  # Layer2 阶段6：直指 functional core 真身（risk_shield 垫片已删）


def _order(**kw):
    """造一个默认合法订单（白名单内、100 整手、限价、金额内）。"""
    base = dict(symbol="510300.SH", qty=100, side="buy", price=5.0)
    base.update(kw)
    return OrderRequest(**base)


def _ok_kwargs(**kw):
    """造一组全过的挡板参数（连接正常、实盘放行、确认、白名单、quote 正常、时段内）。"""
    base = dict(
        dry_run=False, allow_live=True,
        whitelist={"510300.SH"}, max_amount=1000.0, max_shares=100,
        quote={"last_price": 5.0, "high_limit": 5.5, "low_limit": 4.5},
        enforce_session=True, is_locked=False, connected=True,
        confirm=True, in_session=True,
    )
    base.update(kw)
    return base


def test_qmt_gateway_exported():
    """trading 包应导出 QmtExecutionGateway（Task 1 配置层契约）。"""
    from trading import QmtExecutionGateway
    assert QmtExecutionGateway is not None


def test_pass_all_clear():
    """全过 → blocked=False。"""
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
    """dry_run=True → blocked=True 但 is_dry_run=True（模拟语义，非拒单错误）。"""
    d = check_order(_order(), **_ok_kwargs(dry_run=True))
    assert d.blocked is True
    assert d.is_dry_run is True
    assert d.stage == "dry_run"


def test_block_allow_live_gate():
    """dry_run=False 但 allow_live=False → 拒单（强制模拟）。"""
    d = check_order(_order(), **_ok_kwargs(dry_run=False, allow_live=False))
    assert d.blocked and d.stage == "allow_live" and d.is_dry_run is False


def test_block_no_confirm():
    d = check_order(_order(), **_ok_kwargs(confirm=False))
    assert d.blocked and d.stage == "confirm"


def test_block_whitelist():
    d = check_order(_order(symbol="000001.SZ"),
                    **_ok_kwargs(whitelist={"510300.SH"}))
    assert d.blocked and d.stage == "whitelist"


def test_block_lot_size():
    d = check_order(_order(qty=150), **_ok_kwargs(max_shares=1000))
    assert d.blocked and d.stage == "lot"


def test_block_lot_zero():
    d = check_order(_order(qty=0), **_ok_kwargs(max_shares=1000))
    assert d.blocked and d.stage == "lot"


def test_block_max_amount():
    # 100 股 * 5.0 = 500，上限调到 400 → 触发
    d = check_order(_order(qty=100, price=5.0), **_ok_kwargs(max_amount=400.0))
    assert d.blocked and d.stage == "max_amount"


def test_block_max_shares():
    d = check_order(_order(qty=200), **_ok_kwargs(max_shares=100, max_amount=100000))
    assert d.blocked and d.stage == "max_shares"


def test_block_high_limit_buy():
    """BUY + 涨停 → 拦截（#6：涨停买盘封死，买不进）。"""
    q = {"last_price": 5.6, "high_limit": 5.5, "low_limit": 4.5}
    d = check_order(_order(side="buy"), **_ok_kwargs(quote=q))
    assert d.blocked and d.stage == "high_limit"


def test_block_low_limit_sell():
    """SELL + 跌停 → 拦截（#6：跌停卖盘封死，卖不出，止损 SELL 发也是废单）。"""
    q = {"last_price": 4.4, "high_limit": 5.5, "low_limit": 4.5}
    d = check_order(_order(side="sell"), **_ok_kwargs(quote=q))
    assert d.blocked and d.stage == "low_limit"


def test_buy_low_limit_not_blocked():
    """BUY + 跌停 → 放行（#6：跌停能买，原实现不分 side 误拦建仓单）。

    物理意图：跌停是卖盘封死、买盘可成交，BUY 挂单对卖盘成交。蔡森回踩建仓
    若恰逢跌停应允许买入。
    """
    q = {"last_price": 4.4, "high_limit": 5.5, "low_limit": 4.5}
    d = check_order(_order(side="buy"), **_ok_kwargs(quote=q))
    assert d.blocked is False


def test_sell_high_limit_not_blocked():
    """SELL + 涨停 → 放行（#6：涨停能卖——本关最致命的实盘风险修复）。

    物理意图：涨停是买盘封死、卖盘可成交，SELL 挂单对买盘成交（含一字板）。
    蔡森 tick_exit 止损/止盈/时间止损全走 SELL，涨停被拦=错过离场=敞口失控。
    原 plan 提「SELL 仅拦一字涨停不可卖」经核实不成立（涨停卖单总能成交），
    故对称实现：SELL 仅拦跌停。
    """
    q = {"last_price": 5.6, "high_limit": 5.5, "low_limit": 4.5}
    d = check_order(_order(side="sell"), **_ok_kwargs(quote=q))
    assert d.blocked is False


def test_block_session():
    d = check_order(_order(), **_ok_kwargs(in_session=False))
    assert d.blocked and d.stage == "session"


def test_no_quote_skips_limit_check():
    """quote=None → 跳过涨跌停关（xtdata 不可用时的降级）。"""
    d = check_order(_order(), **_ok_kwargs(quote=None))
    assert d.blocked is False


def test_short_circuit_order():
    """关 1（连接）优先于关 4（confirm）：断线时即便 confirm=False 也只报 connection。"""
    d = check_order(_order(), **_ok_kwargs(is_locked=True, confirm=False))
    assert d.stage == "connection"
