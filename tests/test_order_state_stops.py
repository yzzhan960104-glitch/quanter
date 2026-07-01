"""订单状态机出场逻辑（止损/止盈/移动止损）纯函数单元测试

覆盖范围：
- 固定止损：价格跌穿 entry*(1-pct) 触发
- 固定止盈：价格涨破 entry*(1+pct) 触发
- ATR 移动止损：stop = high - atr*k，只上移不下移（锁浮盈）
"""
from trading.order_state import check_stop_loss, check_take_profit, update_trailing_stop


def test_stop_loss_triggers_below_threshold():
    """固定止损阈值：跌 6%>5% 触发；跌 4%<5% 不触发。"""
    assert check_stop_loss(entry=100.0, price=94.0, pct=0.05) is True   # 跌 6%>5%
    assert check_stop_loss(entry=100.0, price=96.0, pct=0.05) is False


def test_take_profit_triggers_above_threshold():
    """固定止盈阈值：涨 6%>5% 触发；涨 4%<5% 不触发。"""
    assert check_take_profit(entry=100.0, price=106.0, pct=0.05) is True
    assert check_take_profit(entry=100.0, price=104.0, pct=0.05) is False


def test_trailing_only_moves_up():
    """ATR 移动止损只上移不下移：新高随之上调，回撤时锁住既有浮盈。"""
    # high=110, atr=2, k=2 → stop=106；后续 high=108 → stop=104 < 106，不降
    s1 = update_trailing_stop(high=110.0, atr=2.0, k=2.0, prev_stop=0.0)
    assert s1 == 106.0
    s2 = update_trailing_stop(high=108.0, atr=2.0, k=2.0, prev_stop=s1)
    assert s2 == 106.0   # 不下移
    s3 = update_trailing_stop(high=112.0, atr=2.0, k=2.0, prev_stop=s1)
    assert s3 == 108.0   # 上移
