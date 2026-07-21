# -*- coding: utf-8 -*-
"""交易机器人 brief 单测（Task 3）。"""
from broadcast.brief_trading import build_trading_brief


def test_trading_brief_basic():
    """有成交 + 资产 + 持仓 → 含关键字段。

    样本 direction 采用**生产大写口径**（BUY/SELL），与
    server/services/trading_service.py:432 落 CSV 的真实大小写一致；
    断言「买 1 笔」锁定成交汇总统计大小写不敏感、不再假绿。
    """
    r = build_trading_brief(
        "2026-07-21",
        trades=[
            {"timestamp": "2026-07-21 09:35:00", "symbol": "510300.SH", "direction": "BUY",
             "shares": 100, "price": 4.0, "strategy": "neckline", "rationale": ""},
        ],
        asset={"cash": 999600.0, "total_asset": 1000000.0, "market_value": 400.0},
        positions=[{"symbol": "510300.SH", "qty": 100, "market_value": 400.0, "pnl": 0.0}],
        status={"connected": True, "locked": False, "mode": "live"},
    )
    md = r.markdown
    assert "510300.SH" in md
    assert "1000000" in md or "1,000,000" in md  # 期末资金
    assert "止盈止损" in md  # 占位字段存在（诚实标注第二期）
    # 成交笔数断言：锁定大小写不敏感统计（生产 CSV 大写 BUY 不再被漏成 0 笔）
    assert ("买 1 笔" in md) or ("买1笔" in md)


def test_trading_brief_empty_and_disconnected():
    """无成交 + 网关断线 → 中性降级文案，不抛、不造假。"""
    r = build_trading_brief("2026-07-21", trades=[], asset=None, positions=[], status={"connected": False, "locked": False, "mode": "disconnected"})
    assert "无成交" in r.markdown or "未成交" in r.markdown
    assert "断线" in r.markdown or "disconnected" in r.markdown
