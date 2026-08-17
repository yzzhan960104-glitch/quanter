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
    next_plan: dict | None = None,
) -> BriefResult:
    """生成交易每日播报 Markdown。数据由 __main__ 取数注入，本函数零 IO 副作用。

    next_plan（2026-08-17 补）：明日（T+1）交易计划，load_plan 契约
    ``{date, confirmed, orders}``（None=无计划/读库失败）。物理意图：用户核心诉求
    是「推送明日的交易计划」，旧模板只有当日回顾五段、计划数据在 DB 却不进播报
    ——补前瞻段让研究员盘后一眼看到明日挂单清单与确认态。渲染三态对齐持仓段
    纪律：None/空单诚实降级，confirmed=False 显式标注待确认，不冒充已确认。
    """
    trades = trades or []
    # positions=None 语义：取数失败/网关未连（持仓未知），不与「空仓」混用。
    # 空 list [] 才是 broker 权威空仓。这里不把 None 折叠成 []，留给三态渲染分支判定。
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

    # W3.2 去重（08-04 事故根因防线）：同一成交（traded_time/symbol/shares/price
    # 完全相同）重放 N 次时，消费端按 1 笔计数。traded_time 缺失时退化用 timestamp
    # 兜底（CSV 老行无 traded_time 字段，按落盘时间戳去重略宽于真相源，但仍优于不去重）。
    # Why 不用 order_id 去重：同一 order_id 的部分成交（不同 traded_time）是真多笔，
    # 不应合并；真相源的 UNIQUE(order_id, traded_time) 已在写入端保证，
    # 这里再按四元组去重是防「同一笔回报被消费链路重放多次」的最后一道闸。
    def _dedup_key(t: dict) -> tuple:
        tt = str(t.get("traded_time") or t.get("timestamp") or "")
        return (tt, str(t.get("symbol") or ""), t.get("shares"), t.get("price"))

    seen: dict[tuple, int] = {}
    deduped_fills: list[dict] = []
    for t in fills:
        k = _dedup_key(t)
        if k in seen:
            seen[k] += 1
        else:
            seen[k] = 1
            deduped_fills.append(t)
    # 去重后的买/卖分组（明细与计数都基于 deduped_fills，避免重放刷笔数）
    buys = [t for t in deduped_fills if _dir(t) in ("buy", "dry_run_buy")]
    sells = [t for t in deduped_fills if _dir(t) in ("sell", "dry_run_sell")]
    # 重放提示：任一四元组出现 >1 次时输出（N>1 才显示，避免无重放时刷噪声）
    replay_items = [(k, n) for k, n in seen.items() if n > 1]
    blocked = [t for t in trades if _dir(t) == "blocked"]

    # 成交明细：只列真实成交（fill，去重后），保持原序（时间序）。
    trade_lines = []
    for t in deduped_fills[:20]:  # 明细最多列 20 笔防刷屏
        sym = t.get("symbol", "?")
        d = t.get("direction", "?")
        sh = _fmt_num(t.get("shares"))
        px = _fmt_num(t.get("price"))
        trade_lines.append(f"- {sym} {d} {sh}股 @ {px}")
    trade_block = "\n".join(trade_lines) if trade_lines else "- 今日无真实成交"

    # 重放提示段（W3.2 新增）：N>1 时单列，让研究员看到「重放」而非「多笔」。
    # 物理意图：消费链路（主推 + 轮询 + 重建）可能重放同一笔成交回报，去重后
    # 计数是 1，但必须显式提示「曾重放 N 次」—— 否则研究员对照原始回报数会困惑。
    if replay_items:
        replay_lines = []
        for (_tt, sym, sh, px), n in replay_items[:5]:
            replay_lines.append(
                f"- {sym} {_fmt_num(sh)}股 @ {_fmt_num(px)} 同一成交重放 {n} 次")
        if len(replay_items) > 5:
            replay_lines.append(f"- … 等共 {len(replay_items)} 组重放")
        replay_block = "\n".join(replay_lines)
    else:
        replay_block = ""  # 无重放不输出该段

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

    # W3.2 持仓三态（spec §3.3.3）：
    # - None：取数失败/网关未连 → 「持仓未知（网关未连接）」（不渲染成「无持仓」，
    #   08-04 事故把未知当零敞口误导决策）；
    # - []：broker 权威空仓 → 「当前无持仓」；
    # - 非空：持仓明细。
    # Why 严格区分：broker 返 [] 是「确认无敞口」的强信号，返 None 是「不知道」的弱信号，
    # 后者绝不能渲染成前者 —— 研究员看到「无持仓」会以为真零敞口而放松风控警觉。
    if positions is None:
        pos_block = "- 持仓未知（网关未连接）"
    elif len(positions) == 0:
        pos_block = "- 当前无持仓"
    else:
        pos_lines = []
        for p in positions[:15]:
            sym = p.get("symbol", "?")
            qty = _fmt_num(p.get("qty"))
            pos_lines.append(f"- {sym} {qty}股")
        pos_block = "\n".join(pos_lines)

    # 汇总行：成交笔数 + 拦截笔数（拦截>0 才附加，避免无拦截时刷噪声）
    summary = f"**成交汇总**：买 {len(buys)} 笔 / 卖 {len(sells)} 笔"
    if blocked:
        summary += f" / 拦截 {len(blocked)} 笔"

    # —— 明日（T+1）交易计划段（2026-08-17 补）——
    # 三态：None/空单 → 诚实降级（eod 未产 / 读库失败）；
    # confirmed=False → 显式「待确认」（pre_open 不放行）；True → 「已确认」。
    # 明细最多列 20 条防刷屏（超量追加余量计数行）。
    if next_plan and next_plan.get("orders"):
        plan_date = next_plan.get("date", "?")
        n_orders = len(next_plan["orders"])
        conf_note = "已确认（人审/AUTO_CONFIRM 过闸）" if next_plan.get("confirmed") \
            else "待确认（pre_open 将不放行）"
        plan_lines = []
        for o in next_plan["orders"][:20]:
            od = o.get("order") or {}
            sym = od.get("symbol") or o.get("symbol") or "?"
            qty = _fmt_num(od.get("qty"))
            px = _fmt_num(od.get("price"))
            stop = _fmt_num(o.get("stop_price"))
            tp = _fmt_num(o.get("take_profit"))
            plan_lines.append(f"- {sym} 买 {qty}股 @ {px}（止损 {stop} / 止盈 {tp}）")
        if n_orders > 20:
            plan_lines.append(f"- … 等共 {n_orders} 单")
        plan_block = "\n".join(plan_lines)
        plan_section = [
            "",
            f"**明日（T+1）交易计划** · {plan_date} · {n_orders} 单 · {conf_note}",
            plan_block,
        ]
    else:
        plan_section = [
            "",
            "**明日（T+1）交易计划**",
            "- 明日无新计划（eod 未产 / 读库失败）",
        ]

    sections = [
        f"### 🤖 交易机器人 · 每日跟踪\n> {date}（{weekday}）收盘{gw_note}\n",
        summary,
        trade_block,
    ]
    # 重放段（仅 N>1 时插入，避免无重放时刷噪声）
    if replay_block:
        sections += ["", "**成交重放提示**", replay_block]
    sections += [
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
    # 计划段放末尾：回顾在前、前瞻收尾（与播报「每日跟踪」定位一致）
    sections += plan_section
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
