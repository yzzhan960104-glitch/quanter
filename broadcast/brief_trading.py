# -*- coding: utf-8 -*-
"""交易机器人每日播报文案（一期 · 纯函数·注入式取数·可单测）。

内容：当日挂单/撤单/成交笔数与明细、期初→期末资金、当日盈亏、收盘持仓快照。
诚实边界（spec）：「止盈止损」字段第二期交易引擎上线后才有，本期如实占位标注，不造假。

鲁棒性：任一数据源缺失（trades 空 / asset None / 网关断线）均降级文案，绝不抛。
"""
from __future__ import annotations

from broadcast.brief import BriefResult, _clean_markdown, _weekday_zh


def build_trading_brief(
    date: str,
    *,
    trades: list[dict] | None,
    asset: dict | None,
    positions: list[dict] | None,
    status: dict | None,
) -> BriefResult:
    """生成交易每日播报 Markdown。数据由 __main__ 取数注入，本函数零 IO 副作用。"""
    trades = trades or []
    positions = positions or []
    status = status or {}
    weekday = _weekday_zh(date)

    # 网关状态提示（断线时如实标注数据可能不全）
    mode = status.get("mode", "unavailable")
    gw_note = "" if mode == "live" else f"\n> ⚠️ 网关状态：{mode}（数据可能不全）"

    # 成交汇总
    buys = [t for t in trades if t.get("direction") == "buy"]
    sells = [t for t in trades if t.get("direction") == "sell"]
    trade_lines = []
    for t in trades[:20]:  # 明细最多列 20 笔防刷屏
        sym = t.get("symbol", "?")
        d = t.get("direction", "?")
        sh = _fmt_num(t.get("shares"))
        px = _fmt_num(t.get("price"))
        trade_lines.append(f"- {sym} {d} {sh}股 @ {px}")
    trade_block = "\n".join(trade_lines) if trade_lines else "- 今日无成交记录"

    # 资金（期初=期末-当日成交净额；无 asset 则降级）
    if asset and asset.get("total_asset") is not None:
        cash = _fmt_money(asset.get("cash"))
        total = _fmt_money(asset.get("total_asset"))
        mv = _fmt_money(asset.get("market_value"))
        asset_block = f"- 期末总资产：{total}\n- 可用现金：{cash}\n- 持仓市值：{mv}"
    else:
        asset_block = "- 资产数据未取到（网关未连接？）"

    # 持仓快照
    pos_lines = []
    for p in positions[:15]:
        sym = p.get("symbol", "?")
        qty = _fmt_num(p.get("qty"))
        pos_lines.append(f"- {sym} {qty}股")
    pos_block = "\n".join(pos_lines) if pos_lines else "- 当前无持仓"

    sections = [
        f"### 🤖 交易机器人 · 每日跟踪\n> {date}（{weekday}）收盘{gw_note}\n",
        f"**成交汇总**：买 {len(buys)} 笔 / 卖 {len(sells)} 笔",
        trade_block,
        "",
        "**资金**",
        asset_block,
        "",
        "**持仓快照**",
        pos_block,
        "",
        "**止盈止损触发**",
        "- （第二期自动交易引擎上线后填充，当前模拟盘无自动止损动作）",
    ]
    md = _clean_markdown("\n".join(sections))
    return BriefResult(date=date, markdown=md)


def _fmt_num(v) -> str:
    try:
        return f"{float(v):.0f}" if float(v) == int(float(v)) else f"{float(v):.2f}"
    except (TypeError, ValueError):
        return "—"


def _fmt_money(v) -> str:
    try:
        return f"{float(v):,.2f}"
    except (TypeError, ValueError):
        return "—"
