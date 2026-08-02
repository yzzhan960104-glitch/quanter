# -*- coding: utf-8 -*-
"""组合资金模型（P0-1 · 回测/实盘资金口径统一单源）。

物理定位（2026-08-02 回测评审 P0-1）：
    旧 replay 净值模型是写死的 RISK_FRAC=0.01（每笔冒 AUM 1% 风险、rr 复利），与
    实盘下单口径（trading/compute/plan.py：capital × pos_cap=5% × experiment_weight、
    最多并发 N 仓）完全脱钩，也跟 discovery 的 kelly 封顶 5% 是两套资金曲线。
    本模块把"逐笔交易 → 组合净值曲线"抽成策略中立的单源模型：

        - pos_cap 模式（默认，对齐实盘）：每笔 allocation = capital × pos_cap，
          收益 = allocation × avg_pnl_pct/100，净值加总（不复利——资金基准固定，
          与实盘 budget = capital × pos_cap 同语义）；并发持仓超 max_positions
          或现金不足的笔不进净值（模拟实盘持仓/资金约束）。
        - risk_frac 模式（向后兼容）：旧口径 equity = Π(1 + rr × risk_frac)，
          供需要复现历史报告的调用方显式选择，不再作为默认。

依赖铁律（backtest 单向依赖）：本模块只依赖 stdlib + dataclass，零 I/O、零 pandas，
回测/计算单元/前端报告共用同一净值算法。
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field


@dataclass(frozen=True)
class PositionModel:
    """组合资金模型参数（不可变快照，随回测任务冻结）。

    capital:        总资金基准（AUM，元；净值归一化 equity_0=1.0 后仅用比例）。
    pos_cap:        单笔仓位上限（对齐实盘 TRADE_POS_CAP=0.05；0.05=每笔 5% 资金）。
    max_positions:  最大并发持仓（0=不限制；默认 6 ≈ 6×5%=30% 组合仓位）。
    risk_frac:      旧模型开关：非 None 时退化为 Π(1+rr×risk_frac) 复利（默认 None）。
    slippage_bps:   双边滑点（bps，买卖各一次，从每笔收益扣除）。
    """
    capital: float = 1_000_000.0
    pos_cap: float = 0.05
    max_positions: int = 6
    risk_frac: float | None = None
    slippage_bps: float = 0.0

    def to_dict(self) -> dict:
        return asdict(self)


def _iso(v) -> str:
    if v is None:
        return ""
    if hasattr(v, "isoformat"):
        return v.isoformat()
    return str(v)


def build_equity_curve(trades: list[dict], model: PositionModel) -> list[dict]:
    """逐笔流水 → 组合净值曲线（按 exit_date 升序，equity_0=1.0）。

    参数：
        trades: _compute_stats 产出的流水 dict（symbol/entry_date/exit_date/rr/
                avg_pnl_pct），entry/exit 可为 pd.Timestamp 或 str。
        model:  资金模型（PositionModel）。

    返回：
        [{date, cumulative_rr, equity, pnl_pct}, ...]；空流水返 []。
        equity 是归一化净值（1 + 累计收益比例）；pnl_pct 是本笔对净值的贡献百分比
        （pos_cap 模式下 = 仓位加权收益，risk_frac 模式下 = rr×risk_frac）。

    并发/资金约束（P2-7）：
        pos_cap 模式按 entry_date 升序滚动；区间重叠的持仓占用并发额度
        （前一笔 exit_date ≤ 新笔 entry_date 视为已释放），达到 max_positions
        或现金（capital − 在仓 allocation 之和）不足的笔跳过——只影响净值与
        cumulative_rr，不改变 signal 级统计（n_hits/win_rate 仍是策略质量口径）。
    """
    if not trades:
        return []

    if model.risk_frac is not None:
        # 旧口径（向后兼容）：rr 复利，按 exit_date 排序。
        sorted_t = sorted(trades, key=lambda t: _iso(t.get("exit_date")))
        curve, eq, run_rr = [], 1.0, 0.0
        for t in sorted_t:
            rr = float(t.get("rr") or 0.0)
            run_rr += rr
            eq *= 1.0 + rr * model.risk_frac
            curve.append({
                "date": _iso(t.get("exit_date")),
                "cumulative_rr": run_rr,
                "equity": eq,
                "pnl_pct": rr * model.risk_frac * 100.0,
            })
        return curve

    # pos_cap 模式（默认，对齐实盘 budget=capital×pos_cap）：加总不复利。
    by_entry = sorted(
        trades,
        key=lambda t: (_iso(t.get("entry_date")), _iso(t.get("exit_date"))),
    )
    curve: list[dict] = []
    active: list[tuple] = []      # (symbol, allocation, exit_key)
    cash = model.capital
    equity, run_rr = 1.0, 0.0
    for t in by_entry:
        entry_key = _iso(t.get("entry_date"))
        # 释放已到期持仓（exit ≤ 当前 entry）的占用资金。
        still, freed = [], 0.0
        for sym, alloc, ex_key in active:
            if ex_key <= entry_key:
                freed += alloc
            else:
                still.append((sym, alloc, ex_key))
        active = still
        cash += freed

        # 并发上限 / 现金不足 → 跳过（净值与累计 rr 均不计）。
        if model.max_positions and len(active) >= model.max_positions:
            continue
        allocation = model.capital * model.pos_cap
        if allocation > cash:
            continue

        ret = float(t.get("avg_pnl_pct") or 0.0) / 100.0
        ret -= model.slippage_bps * 2.0 / 10_000.0
        cash -= allocation
        active.append((t.get("symbol"), allocation, _iso(t.get("exit_date"))))
        run_rr += float(t.get("rr") or 0.0)
        equity += allocation * ret / model.capital
        curve.append({
            "date": _iso(t.get("exit_date")),
            "cumulative_rr": run_rr,
            "equity": equity,
            "pnl_pct": ret * 100.0,
        })
    return curve
