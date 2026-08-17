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


# ============================= W3.2 简报去重 + 持仓三态 =============================


def test_trading_brief_dedup_replayed_fill():
    """W3.2: 同一成交（traded_time/symbol/shares/price 完全相同）重放 N 次 →
    简报成交计数仍为 1 笔（去重），且 N>1 时输出「同一成交重放 N 次」提示段。

    08-04 事故根因：消费端读 CSV 镜像且不去重，把 24 行重复当成「买 24 笔」
    误导决策。T6 已让写入端幂等（DB fill 表真相源），简报消费段再补一道
    (traded_time, symbol, shares, price) 去重防线。
    """
    # 同一笔成交回报被上层重放 3 次（典型场景：成交回报主推 + on_traded 兜底轮询 + 重建）
    trades = [
        {"timestamp": "2026-08-04 09:35:00", "symbol": "300001.SZ", "direction": "BUY",
         "shares": 100, "price": 10.5, "strategy": "neckline", "rationale": "",
         "kind": "fill", "traded_time": "20260804093500"},
    ] * 3
    r = build_trading_brief(
        "2026-08-04", trades=trades, asset=None, positions=[],
        status={"connected": True, "locked": False, "mode": "live"},
    )
    md = r.markdown
    # 去重后成交笔数是 1（不是 3）
    assert "买 1 笔" in md
    # N>1 时显式提示重放次数（让研究员看到「重放」而非「多笔」）
    assert "重放" in md and "3" in md


def test_trading_brief_dedup_no_replay_no_hint():
    """W3.2: 无重放（每笔 traded_time/shares/price 唯一）→ 不输出「重放」段。"""
    trades = [
        {"timestamp": "2026-08-04 09:35:00", "symbol": "300001.SZ", "direction": "BUY",
         "shares": 100, "price": 10.5, "kind": "fill", "traded_time": "20260804093500"},
        {"timestamp": "2026-08-04 09:36:00", "symbol": "300002.SZ", "direction": "BUY",
         "shares": 200, "price": 20.0, "kind": "fill", "traded_time": "20260804093600"},
    ]
    r = build_trading_brief(
        "2026-08-04", trades=trades, asset=None, positions=[],
        status={"connected": True, "locked": False, "mode": "live"},
    )
    md = r.markdown
    assert "买 2 笔" in md
    # 无重放时不刷「重放」噪声
    assert "重放" not in md


def test_trading_brief_positions_three_states_unknown():
    """W3.2 持仓三态（spec §3.3.3）：取数失败/网关未连 → 「持仓未知（网关未连接）」。

    08-04 事故：消费端把「未知」（网关断/取数失败）渲染成「当前无持仓」，
    误导研究员以为真零敞口。修复：positions=None 且网关未连 → 明确「持仓未知」。
    与「broker 权威空仓（positions=[]）」严格区分。
    """
    # 状态 1：未知 —— 取数失败/网关未连，positions=None
    r_unknown = build_trading_brief(
        "2026-08-04", trades=[], asset=None, positions=None,
        status={"connected": False, "locked": False, "mode": "disconnected"},
    )
    md = r_unknown.markdown
    assert "持仓未知" in md
    # 不渲染成「当前无持仓」（08-04 事故把未知当零误导决策）
    assert "当前无持仓" not in md


def test_trading_brief_positions_three_states_empty():
    """W3.2 持仓三态：broker 权威空仓（positions=[]）→ 「当前无持仓」。"""
    r_empty = build_trading_brief(
        "2026-08-04", trades=[], asset=None, positions=[],
        status={"connected": True, "locked": False, "mode": "live"},
    )
    md = r_empty.markdown
    assert "当前无持仓" in md
    assert "持仓未知" not in md


def test_trading_brief_positions_three_states_detail():
    """W3.2 持仓三态：有持仓 → 明细行。"""
    r_detail = build_trading_brief(
        "2026-08-04", trades=[], asset=None,
        positions=[{"symbol": "510300.SH", "qty": 100}],
        status={"connected": True, "locked": False, "mode": "live"},
    )
    md = r_detail.markdown
    assert "510300.SH" in md and "100" in md
    assert "持仓未知" not in md
    assert "当前无持仓" not in md


# ===========================================================================
# 明日（T+1）交易计划段（2026-08-17 补）：用户核心诉求「推送明日的交易计划」
# 在旧模板中从未落地——brief 只有当日回顾五段，计划数据在 DB 却不进播报。
# 新增 next_plan 注入参数（load_plan 契约：{date, confirmed, orders} / None）。
# ===========================================================================

def _plan_order(sym: str, qty: int, px: float, stop: float, tp: float) -> dict:
    """构造 load_plan.orders 元素（meta shape：{symbol, order{...}, stop_price, ...}）。"""
    return {"symbol": sym,
            "order": {"symbol": sym, "qty": float(qty), "side": "buy", "price": px},
            "stop_price": stop, "take_profit": tp, "neckline": px - 0.5, "atr": 0.4}


def test_brief_next_day_plan_renders_orders_and_confirm_state():
    """有明日计划 → 渲染计划段：日期、标的、量、挂单价、止损/止盈、确认态。"""
    r = build_trading_brief(
        "2026-08-14", trades=[], asset=None, positions=[],
        status={"connected": True, "locked": False, "mode": "live"},
        next_plan={"date": "2026-08-17", "confirmed": True,
                   "orders": [_plan_order("300017.SZ", 3000, 16.56, 15.04, 24.29),
                              _plan_order("688111.SH", 100, 260.08, 243.29, 365.75)]},
    )
    md = r.markdown
    assert "2026-08-17" in md                    # 计划日（T+1）可见
    assert "300017.SZ" in md and "688111.SH" in md
    assert "16.56" in md and "260.08" in md      # 挂单价
    assert "15.04" in md and "24.29" in md       # 止损/止盈
    assert "已确认" in md                          # 整体确认态


def test_brief_next_day_plan_unconfirmed_flagged():
    """计划存在但未过确认闸 → 显式标注待确认（pre_open 不放行），不冒充已确认。"""
    r = build_trading_brief(
        "2026-08-14", trades=[], asset=None, positions=[],
        status={"connected": True, "locked": False, "mode": "live"},
        next_plan={"date": "2026-08-17", "confirmed": False,
                   "orders": [_plan_order("300017.SZ", 3000, 16.56, 15.04, 24.29)]},
    )
    assert "待确认" in r.markdown
    assert "已确认" not in r.markdown


def test_brief_next_day_plan_none_degrades_honestly():
    """无明日计划（eod 未产/regime 停手/读库失败）→ 诚实降级文案，不抛不造假。"""
    r = build_trading_brief(
        "2026-08-14", trades=[], asset=None, positions=[],
        status={"connected": True, "locked": False, "mode": "live"},
        next_plan=None,
    )
    assert "明日无新计划" in r.markdown


def test_brief_next_day_plan_default_omittable():
    """旧调用方不传 next_plan（向后兼容）→ 计划段降级文案而非 TypeError。"""
    r = build_trading_brief(
        "2026-08-14", trades=[], asset=None, positions=[],
        status={"connected": True, "locked": False, "mode": "live"},
    )
    assert "明日无新计划" in r.markdown
