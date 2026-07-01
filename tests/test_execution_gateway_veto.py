"""
tests/test_execution_gateway_veto.py
====================================
Task 14：宏观一票否决网关 MacroAwareGateway 的单测。

设计意图（Why）：
- 宏观信用 regime 是跨资产层级的主导信号：当 credit 处于收缩期（regime=-1，
  信用利差走阔、流动性溢价上行），任何「买入突破/动量追涨」类策略的胜率会
  系统性塌陷——历史上 2008Q4、2022全年等信用收紧区间，买入动量的回撤
  往往翻倍。故在执行网关层做一票否决式过滤，比让信号层各自判断更收敛、
  更不易遗漏。
- 本任务仅验证「网关对注入 regime 的处置逻辑」：减半、strict 否决、扩张
  期放行、卖出不受否决四条核心断言。regime 的计算本身由 CreditRegime
  （Task 11）负责，此处通过参数注入解耦，便于单测与不依赖外部数据。
"""
import pytest

from trading.execution_gateway import MacroAwareGateway, VetoedError
from trading.order_state import OrderState


class _Order:
    """最小订单桩对象：暴露 side / quantity / state 三个字段即可被网关处置。

    Why 自造桩而不复用 OrderRequest：OrderRequest 是 frozen dataclass，无法
    就地改 quantity；而本任务的网关语义正是「就地减半」。生产侧传入的会是
    可变订单对象（如策略层的 Order 信封），故桩对象按可变属性建模更贴近真实。
    """

    def __init__(self, side: str, qty: float) -> None:
        self.side = side
        self.quantity = qty
        self.state = OrderState.PENDING


def test_buy_in_contraction_halved():
    """收缩期买入 + strict_veto=False：数量减半，订单仍允许放行。"""
    gw = MacroAwareGateway(strict_veto=False)
    o = _Order("BUY", 1000)
    gw.submit_order(o, regime=-1)
    assert o.quantity == 500   # 强制减半


def test_buy_in_contraction_strict_veto():
    """收缩期买入 + strict_veto=True：直接否决抛 VetoedError，订单不下达。"""
    gw = MacroAwareGateway(strict_veto=True)
    with pytest.raises(VetoedError):
        gw.submit_order(_Order("BUY", 1000), regime=-1)


def test_buy_in_expansion_passes():
    """扩张期(regime=1)买入：风控信号偏多，订单原样放行。"""
    gw = MacroAwareGateway(strict_veto=False)
    o = _Order("BUY", 1000)
    gw.submit_order(o, regime=1)
    assert o.quantity == 1000   # 不变


def test_sell_not_vetoed_in_contraction():
    """收缩期卖出不受否决：减仓/止损是收缩期的正确动作，反而应被鼓励。"""
    gw = MacroAwareGateway(strict_veto=True)
    o = _Order("SELL", 1000)
    gw.submit_order(o, regime=-1)   # 即便 strict_veto=True，卖出仍放行
    assert o.quantity == 1000
