# -*- coding: utf-8 -*-
"""round_analysis：回测逐笔归因（T3.1 · 2026-08-16）——AI 分析的加厚输入。

物理意图（AI 回路缺口①）：训练环/提案环的分析输入此前只有 6 字段摘要（n/win_rate/
avg_rr/max_dd/annualized/round），无逐笔明细——GLM/Claude 看不到「亏在哪种出场、亏在
哪个月、亏在什么市场状态」，调参建议只能在总量指标上猜。本模块把 ReplayReport.trades
逐笔压成三张归因表（exit_reason / 月度 / regime），纯读纯函数零写入，供 digest 注入
与优化循环的每轮分析使用。

口径：trades 元素 = replay report 的逐笔 dict（symbol/exit_reason/avg_pnl_pct/
signal_date 或 entry_date）；pnl 用 avg_pnl_pct（含费率、分级止盈加权后的均笔收益%）。
regime 分类器可注入（默认 None 跳过该维度）——默认注入
``trading.compute.regime`` 的历史回放会在无指数数据时降级，故由调用方决定是否启用。
"""
from __future__ import annotations

from collections import defaultdict


def analyze_trades(trades: list[dict], regime_classify=None) -> dict:
    """逐笔 trades → 三维归因摘要（exit_reason / 月度 / regime）。

    Args:
        trades: ReplayReport.trades 列表（dict）；空列表返回空结构（调用方降级渲染）。
        regime_classify: 可选 (date_str) -> str 分类器（如 BULL/BEAR/UNKNOWN）；
            None 跳过 regime 维度（默认——历史逐日 regime 回放成本由调用方权衡）。

    Returns:
        {"n": int, "exit_reason": {...}, "monthly": {...}, "regime": {...} or None}
        各维度结构：{key: {"n", "win_rate", "avg_pnl", "total_pnl"}}
    """
    out = {"n": len(trades), "exit_reason": {}, "monthly": {}, "regime": None}
    if not trades:
        return out

    def _agg(bucket: dict, key: str, pnl: float) -> None:
        b = bucket.setdefault(key, {"n": 0, "wins": 0, "total_pnl": 0.0})
        b["n"] += 1
        b["wins"] += 1 if pnl > 0 else 0
        b["total_pnl"] += pnl

    def _finalize(bucket: dict) -> dict:
        return {k: {"n": v["n"],
                    "win_rate": round(v["wins"] / v["n"], 4),
                    "avg_pnl": round(v["total_pnl"] / v["n"], 4),
                    "total_pnl": round(v["total_pnl"], 2)}
                for k, v in sorted(bucket.items())}

    raw_reason, raw_month, raw_regime = {}, {}, {}
    for t in trades:
        pnl = float(t.get("avg_pnl_pct") or 0.0)
        _agg(raw_reason, str(t.get("exit_reason") or "unknown"), pnl)
        d = str(t.get("signal_date") or t.get("entry_date") or "")[:7]   # YYYY-MM
        _agg(raw_month, d or "unknown", pnl)
        if regime_classify is not None:
            full_d = str(t.get("signal_date") or t.get("entry_date") or "")
            _agg(raw_regime, regime_classify(full_d) if full_d else "unknown", pnl)

    out["exit_reason"] = _finalize(raw_reason)
    out["monthly"] = _finalize(raw_month)
    if regime_classify is not None:
        out["regime"] = _finalize(raw_regime)
    return out


def render_analysis_md(analysis: dict) -> str:
    """归因摘要 → markdown 三段（digest/训练报告注入用；空数据返回空串由调用方跳过）。"""
    if not analysis or analysis.get("n", 0) == 0:
        return ""
    lines = [f"### 逐笔归因（n={analysis['n']}）", ""]

    def _table(title: str, bucket: dict | None) -> None:
        if not bucket:
            return
        lines.append(f"**{title}**")
        lines.append("| 分组 | n | 胜率 | 均笔% | 累计% |")
        lines.append("|---|---|---|---|---|")
        for k, v in bucket.items():
            lines.append(f"| {k} | {v['n']} | {v['win_rate']:.0%} | "
                         f"{v['avg_pnl']:+.2f} | {v['total_pnl']:+.1f} |")
        lines.append("")

    _table("按出场原因", analysis.get("exit_reason"))
    _table("按月度", analysis.get("monthly"))
    _table("按市场状态", analysis.get("regime"))
    return "\n".join(lines)


def load_recent_trades(db_path: str | None = None, params_source: str = "ACTIVE") -> list[dict]:
    """读最近一条 SUCCESS 回测任务的逐笔 trades（与 digest.load_backtest_expectation
    同款 ACTIVE 优先选择逻辑——期望段与归因段必须来自同一任务，否则口径分裂）。

    无 SUCCESS / report 无 trades → 空列表（调用方降级）。
    """
    import json as _json
    try:
        from backtest import tasks_db as replay_tasks_db
        replay_tasks_db.init_db(db_path)
        tasks = replay_tasks_db.list_tasks(limit=50, path=db_path) or []
        try:
            from experiment.resolver import resolve_active
            active = resolve_active()
        except Exception:
            active = []
        target = None
        fallback = None
        for t in tasks:
            if t.get("status") != "SUCCESS" or not t.get("report_json"):
                continue
            if fallback is None:
                fallback = t
            if active and params_source == "ACTIVE":
                cfg = t.get("cfg_override") or {}
                if isinstance(cfg, str):
                    try:
                        cfg = _json.loads(cfg)
                    except (TypeError, ValueError):
                        cfg = None
                if cfg is not None and _json.dumps(cfg, sort_keys=True) == \
                        _json.dumps(dict(active[0].params), sort_keys=True):
                    target = t
                    break
        picked = target or fallback
        if picked is None:
            return []
        return _json.loads(picked["report_json"]).get("trades") or []
    except Exception:
        return []
