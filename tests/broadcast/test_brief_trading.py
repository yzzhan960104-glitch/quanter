# -*- coding: utf-8 -*-
"""交易机器人 brief 单测（Task 3）。"""
from broadcast.brief_trading import build_trading_brief


def test_trading_brief_basic():
    """有成交 + 资产 + 持仓 → 含关键字段。

    样本 direction 采用**生产大写口径**（BUY/SELL），与
    server/services/trading_service.py:432 落 CSV 的真实大小写一致；
    断言「买 1 笔」锁定成交汇总统计大小写不敏感、不再假绿。
    kind='fill' 是成交口径的必要证据（2026-08-02 起只认成交回报行）。
    """
    r = build_trading_brief(
        "2026-07-21",
        trades=[
            {"timestamp": "2026-07-21 09:35:00", "symbol": "510300.SH", "direction": "BUY",
             "shares": 100, "price": 4.0, "strategy": "neckline", "rationale": "",
             "kind": "fill"},
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
    assert "无真实成交" in r.markdown or "无成交" in r.markdown or "未成交" in r.markdown
    assert "断线" in r.markdown or "disconnected" in r.markdown


def test_trading_brief_fake_submit_and_blocked_not_counted_as_fills():
    """2026-07-31 复现：FakeGW 冒烟 submit 行 + BLOCKED 行不得计入买/卖笔数。

    回归用例：12 笔 _FakeGW:SUBMITTED 冒烟单 + 12 笔 BLOCKED（同时间戳成对）曾被
    刷成「买 12 笔」；修复后只认 kind='fill' 的成交回报 → 买 0 笔，BLOCKED 单列
    「拦截 12 笔」。
    """
    trades = []
    for i in range(12):
        trades.append({
            "timestamp": f"2026-07-31 12:0{i}:00", "symbol": "510300.SH",
            "direction": "BUY", "shares": 100, "price": 5.0,
            "strategy": "", "rationale": "_FakeGW:SUBMITTED:ok", "kind": "submit",
        })
        trades.append({
            "timestamp": f"2026-07-31 12:0{i}:00", "symbol": "510300.SH",
            "direction": "BLOCKED", "shares": 100, "price": 5.0,
            "strategy": "", "rationale": "connection:网关未连接或已锁定（断线保护）",
            "kind": "submit",
        })
    r = build_trading_brief(
        "2026-07-31", trades=trades, asset=None, positions=[],
        status={"connected": False, "locked": False, "mode": "disconnected"},
    )
    md = r.markdown
    assert "买 0 笔 / 卖 0 笔 / 拦截 12 笔" in md
    assert "今日无真实成交" in md
    assert "510300.SH 100股 @ 5（网关未连接或已锁定（断线保护））" in md
    # 冒烟 submit 行不得出现在成交明细
    assert "SUBMITTED" not in md


def test_trading_brief_real_fill_plus_blocked():
    """真实成交（kind=fill）与 BLOCKED 并存：成交计数只含 fill，拦截单列。"""
    r = build_trading_brief(
        "2026-07-31",
        trades=[
            {"timestamp": "2026-07-31 09:35:00", "symbol": "688538.SH",
             "direction": "BUY", "shares": 20300, "price": 2.46,
             "strategy": "neckline", "rationale": "成交回报@20260731101000", "kind": "fill"},
            {"timestamp": "2026-07-31 09:22:02", "symbol": "688538.SH",
             "direction": "BLOCKED", "shares": 20300, "price": 2.46,
             "strategy": "", "rationale": "connection:网关未连接或已锁定（断线保护）",
             "kind": "submit"},
        ],
        asset=None, positions=[],
        status={"connected": False, "locked": False, "mode": "disconnected"},
    )
    md = r.markdown
    assert "买 1 笔 / 卖 0 笔 / 拦截 1 笔" in md
    assert "688538.SH BUY 20300股 @ 2.46" in md
    assert "**拦截/拒单**" in md
