# -*- coding: utf-8 -*-
"""trading.review_report — T 日交易复盘报告（最小版）。

物理定位：e2e 交易链路第 4 步的「可观测产物」。聚合 position_book.fill 表（当日成交
流水）+ trading_plan（计划）+ position 表（收盘持仓）+ post_close drift，输出 markdown
报告。数据源全部已就绪，零新 I/O 通道。

为什么最小版：本 task 核心是 gap4 对账链路；复盘报告是链路终点的可观测产物，最小集
（计划/成交/持仓/对账四段）够验证链路通。全功能复盘（按 experiment_id 聚合 PnL/胜率/
Sharpe + 推钉钉）属 Layer 6 LLM 复盘范畴，留 follow-up。
"""
from __future__ import annotations

import logging
import sqlite3
from pathlib import Path
from typing import Optional

from trading import position_book, trading_plan

logger = logging.getLogger(__name__)

_DEFAULT_REVIEW_DIR = "logs/trading_reviews"


def _fetch_fills_on(date: str, *, db_path: str) -> list[sqlite3.Row]:
    """读 fill 表中 applied_at 当日（LIKE 'date%'）的成交流水，按时间升序。

    Why LIKE date%：applied_at 是 datetime.now().isoformat()（含时分秒），date 是
    YYYY-MM-DD，前缀匹配取当日全部成交。ISO 格式字典序 = 时间序，ORDER BY 自然升序。
    """
    with position_book._connect(db_path) as con:
        return con.execute(
            "SELECT order_id, symbol, direction, qty, price, applied_at FROM fill"
            " WHERE applied_at LIKE ? ORDER BY applied_at",
            (f"{date}%",),
        ).fetchall()


def generate_review(
    date: str,
    *,
    db_path: Optional[str] = None,
    plan: Optional[dict] = None,
    drift: Optional[bool] = None,
) -> str:
    """生成 T 日交易复盘 markdown（计划/成交/持仓/对账四段）。

    Args:
        date:    交易日（YYYY-MM-DD）。
        db_path: 持仓账本 db 路径（默认 position_book._DEFAULT_DB）。
        plan:    透传计划 dict（避免重复 load）；None 则内部 trading_plan.load_plan(date)。
        drift:   post_close 对账结果（True=有偏差/False=ok/None=未对账）。

    Returns:
        markdown 字符串（四段：计划/成交/持仓/对账）。
    """
    db_path = db_path or position_book._DEFAULT_DB

    # ---- 计划段 ----
    # SSoT Phase C · C2c：plan=None 时直接读 DB trade_event(SIGNAL).meta（真相源），
    # 不再 fallback trading_plan.load_plan(date)。SIGNAL meta 形态与原 plan orders dict
    # 同构（meta = order_dict + plan_date/strategy_name/rationale，C1 落盘），渲染逻辑零改动。
    # 致命日期轴：按 substr(trade_id,-10)=date 查（非 timestamp）。
    # 透传 plan 参数仍保留（避免重复查 DB；post_close 等已持有 plan 的调用方复用）。
    if plan is None:
        from trading import state_store as _ss
        try:
            sigs = _ss.list_signals_with_meta_by_plan_date(date)
        except Exception:
            # DB 读失败软降级：渲染「无计划」（不阻断复盘报告其余段落）。
            sigs = []
        # **ssot-review P2 fix**：confirmed 不能只看「有 SIGNAL 行」（bool(sigs)）——
        #研究员复盘看到未审核计划被标「已确认」会误导决策。改 per-trade 查 latest action：
        # 全部 ∈ 已确认集合才 confirmed=True（与 load_plan/pre_open/_stoploss 语义对齐，
        # is_trade_confirmed 单点）。无 sigs（None/[]）→ plan=None（落「无计划」分支）。
        # account_id 解析与 trading_plan.load_plan:84 同口径（env > 默认）。
        if sigs:
            import os as _os
            _aid = _os.getenv("QMT_ACCOUNT_ID", _ss._DEFAULT_ACCOUNT_ID)
            confirmed_all = all(
                _ss.is_trade_confirmed(
                    _ss.build_trade_id(_aid, (s.get("order") or {}).get("symbol", s.get("symbol")), date))
                for s in sigs
            )
            plan = {"orders": sigs, "confirmed": confirmed_all}
        else:
            plan = None
    plan_lines: list[str] = []
    if plan and plan.get("orders"):
        confirmed = "已确认" if plan.get("confirmed") else "待确认"
        plan_lines.append(f"> 计划 {confirmed}（{len(plan['orders'])} 单）")
        for o in plan["orders"]:
            od = o.get("order") or {}
            plan_lines.append(
                f"- {od.get('symbol')} {od.get('side')} {od.get('qty')}股@{od.get('price')}"
                f"（止损{o.get('stop_price')}/止盈{o.get('take_profit')}）"
            )
    else:
        plan_lines.append("- 无计划")

    # ---- 成交段（fill 表当日聚合）----
    fills = _fetch_fills_on(date, db_path=db_path)
    buy_n = sum(1 for f in fills if f["direction"] == "BUY")
    sell_n = sum(1 for f in fills if f["direction"] == "SELL")
    trade_lines: list[str] = [f"- 买入 {buy_n} 笔，卖出 {sell_n} 笔"]
    for f in fills:
        trade_lines.append(
            f"  - {f['symbol']} {f['direction']} {f['qty']:g}股@{f['price']:g}"
            f"（order={f['order_id']}）"
        )

    # ---- 持仓段（position 表）----
    positions = position_book.get_local_positions(db_path=db_path)
    pos_lines: list[str] = []
    if positions:
        for sym, qty in positions.items():
            pos_lines.append(f"- {sym} {qty:g}股")
    else:
        pos_lines.append("- 空仓")

    # ---- 对账段 ----
    if drift is True:
        drift_line = "- ⚠️ 有偏差（drift=True，请排查 only_local/only_broker/drifted）"
    elif drift is False:
        drift_line = "- ✅ 无偏差（drift=False）"
    else:
        drift_line = "- 未对账（drift=None）"

    return (
        f"### T 日交易复盘 {date}\n\n"
        f"**计划**\n" + "\n".join(plan_lines) + "\n\n"
        f"**成交**\n" + "\n".join(trade_lines) + "\n\n"
        f"**收盘持仓**\n" + "\n".join(pos_lines) + "\n\n"
        f"**对账**\n" + drift_line + "\n"
    )


def save_review(date: str, md: str, *, review_dir: str = _DEFAULT_REVIEW_DIR) -> Path:
    """落盘 logs/trading_reviews/review_<date>.md（幂等覆盖，父目录自动建）。"""
    p = Path(review_dir)
    p.mkdir(parents=True, exist_ok=True)
    out = p / f"review_{date}.md"
    out.write_text(md, encoding="utf-8")
    logger.info("复盘报告已落盘 %s", out)
    return out
