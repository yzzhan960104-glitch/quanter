# -*- coding: utf-8 -*-
"""交易机器人每日播报文案（一期 · 纯函数·注入式取数·可单测）。

内容：当日真实成交笔数与明细、拦截/拒单、期初→期末资金、当日盈亏、收盘持仓快照。
诚实边界（spec）：「止盈止损」字段第二期交易引擎上线后才有，本期如实占位标注，不造假。

成交口径（2026-08-02 修复，对齐 kind 语义）：
    - 只有 kind='fill' 的 BUY/SELL/DRY_RUN_* 才是「成交」——submit 审计行
      （下单/被拒）与 BLOCKED 行不是成交，混入会把 FakeGW 冒烟测试/拒单刷成
      「买 N 笔」假繁荣；
    - BLOCKED 单独计数并单列「拦截/拒单」段（真实风控拦截要看得见，但不冒充成交）；
    - 老 CSV 行（无 kind 列）经 query_trades 兜底为 kind='submit' → 不计成交
      （诚实：无成交回报证据的行不算成交）。

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

    # 成交口径：大小写不敏感 + DRY_RUN 归并 + **只认 kind='fill' 的成交回报**。
    # Why 加 kind 闸（2026-08-02）：CSV 里 submit 审计行（含 FakeGW 冒烟单、拒单）
    # 与真实成交同列 direction，早期只按 direction 统计会把 12 笔假单刷成「买 12 笔」。
    # query_trades 对老行（无 kind 列）兜底 'submit' → 无成交回报证据的行天然不算成交。
    _dir = lambda t: (t.get("direction") or "").lower()
    _kind = lambda t: (t.get("kind") or "").lower()
    fills = [
        t for t in trades
        if _dir(t) in ("buy", "sell", "dry_run_buy", "dry_run_sell") and _kind(t) == "fill"
    ]
    buys = [t for t in fills if _dir(t) in ("buy", "dry_run_buy")]
    sells = [t for t in fills if _dir(t) in ("sell", "dry_run_sell")]
    blocked = [t for t in trades if _dir(t) == "blocked"]

    # 成交明细：只列真实成交（fill），保持 CSV 原序（时间序）。
    trade_lines = []
    for t in fills[:20]:  # 明细最多列 20 笔防刷屏
        sym = t.get("symbol", "?")
        d = t.get("direction", "?")
        sh = _fmt_num(t.get("shares"))
        px = _fmt_num(t.get("price"))
        trade_lines.append(f"- {sym} {d} {sh}股 @ {px}")
    trade_block = "\n".join(trade_lines) if trade_lines else "- 今日无真实成交"

    # 拦截/拒单段：BLOCKED 行单列（最多 5 条 + 余量计数），不冒充成交。
    blocked_lines = []
    for t in blocked[:5]:
        sym = t.get("symbol", "?")
        sh = _fmt_num(t.get("shares"))
        px = _fmt_num(t.get("price"))
        reason = str(t.get("rationale") or "").split(":", 1)[-1].strip()
        blocked_lines.append(f"- {sym} {sh}股 @ {px}（{reason or '风控拦截'}）")
    if len(blocked) > 5:
        blocked_lines.append(f"- … 等共 {len(blocked)} 笔")
    blocked_block = "\n".join(blocked_lines) if blocked_lines else "- 今日无拦截/拒单"

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

    # 汇总行：成交笔数 + 拦截笔数（拦截>0 才附加，避免无拦截时刷噪声）
    summary = f"**成交汇总**：买 {len(buys)} 笔 / 卖 {len(sells)} 笔"
    if blocked:
        summary += f" / 拦截 {len(blocked)} 笔"

    sections = [
        f"### 🤖 交易机器人 · 每日跟踪\n> {date}（{weekday}）收盘{gw_note}\n",
        summary,
        trade_block,
        "",
        "**拦截/拒单**",
        blocked_block,
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
