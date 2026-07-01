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


def test_contraction_buy_triggers_dingtalk_alert(monkeypatch):
    """宏观收缩期买入减半须触发钉钉告警（Epic 5：宏观 -1 风控动作可观测）。"""
    from unittest.mock import MagicMock
    # mock fire_and_forget 捕获告警派发；mock NotificationManager 避免"协程未 await"告警
    ff = MagicMock()
    notify = MagicMock(return_value="coro-sentinel")
    mgr = MagicMock()
    mgr.get_default.return_value.notify_risk_event = notify
    monkeypatch.setattr("core.notifier.fire_and_forget", ff)
    monkeypatch.setattr("core.notifier.NotificationManager", mgr)

    gw = MacroAwareGateway(strict_veto=False)
    gw.submit_order(_Order("BUY", 1000), regime=-1)
    assert ff.called              # ★ 收缩期减半 → 告警已派发

    # 扩张期买入不应派发告警
    ff.reset_mock()
    gw.submit_order(_Order("BUY", 1000), regime=1)
    assert not ff.called          # ★ 扩张期放行，无告警


# ============ I-2: 减半后必须保留 100 整手契约 ============


@pytest.mark.parametrize("inp,expected", [
    (300, 100),    # 300//2=150 → 向下取整到手 = 100（非 150 碎股）
    (100, 100),    # 100//2=50  → max(100, 0) = 100（最少保留 1 手）
    (1000, 500),   # 1000//2=500 → 已是整手，原样
    (500, 200),    # 500//2=250 → 向下取整到手 = 200（非 250 碎股）
    (2000, 1000),  # 2000//2=1000 → 已是整手
    (700, 300),    # 700//2=350 → 取整到手 = 300
])
def test_halve_preserves_100_lot_contract(inp, expected):
    """收缩期减半后数量必须是 100 的整数倍（A 股 Order 契约：shares%100==0）。

    修复前 ``max(1, qty//2)`` 会产生 50/150/250 等碎股，违反 A 股 Order 的
    ``shares%100==0`` 强契约（Order.__post_init__ 会 raise ValueError）。

    修复后 ``max(100, (qty//2//100)*100)``：先减半再向下取整到手，最少保留 1 手。
    """
    gw = MacroAwareGateway(strict_veto=False)
    o = _Order("BUY", inp)
    gw.submit_order(o, regime=-1)
    assert o.quantity == expected
    assert o.quantity % 100 == 0, f"减半后数量 {o.quantity} 破坏 100 整手契约"
