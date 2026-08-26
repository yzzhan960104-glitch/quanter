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
                    P0-2（2026-08-03）：默认 5.0（双边共 10bps）保守口径——
                    旧默认 0.0 使主回测零滑点，而真实止损/超时市价卖出有冲击
                    成本（限价买入/止盈可近似无滑点，止损不可）。任务级可显式
                    覆盖（如零费率研究传 0.0）。
    """
    capital: float = 1_000_000.0
    pos_cap: float = 0.05
    max_positions: int = 6
    risk_frac: float | None = None
    slippage_bps: float = 5.0
    # R6-11 有限资金模式（2026-08-26 用户指令「回测架构考虑有限资金」）：
    # lot_size > 0 时启用整手约束（A 股 100 股/手）+ 佣金最低收费（min_fee 元，
    # 买卖各一次）——预算 floor 到整手、买不起一手跳过、min5 侵蚀小单。
    # 默认 lot_size=0 = 原百分比世界（零回归，golden 钉死）。
    lot_size: int = 0
    min_fee: float = 0.0
    # R6-11c 挂单冻结（2026-08-26 用户指令）：True 时每笔的资金/并发占用从
    # signal_date+1（挂单日）起算而非 entry_date（成交日）——对资金与并发而言
    # 「挂单冻结中」与「持仓中」等效（都占 allocation 占槽），唯一区别是占用
    # 提前了等待期（signal→entry 的 max_wait 窗口）。Little's law 自动节流：
    # 在途冻结满 → 新信号丢弃 → 成交率被资金容量压下来（对齐实盘 A 股限价
    # 买单挂出即冻结资金的语义）。默认 False=原口径（零回归）。
    freeze_pending: bool = False
    # R7-P2 质量分层（2026-08-26 信号质量方案 Phase 2）：True 且流水 dict 带
    # "pos_cap" 键时，单笔仓位 = capital × 该笔 pos_cap（质量分→弹性仓位
    # 3%-10%），否则回落 model.pos_cap。默认 False=固定仓位（零回归，golden
    # 钉死）；与 max_positions 并发闸正交（4 并发不变，只变每笔的量）。
    quality_alloc: bool = False
    # R7c 排队纪律（2026-08-26 用户裁决「双口径并报」）：同占用日候选的进场序。
    #   "exit_date"（默认=历史先知口径）：最早出场先进场——确定性约定自 P0-1
    #     存在；容量约束下等价于用未来信息做最短作业优先调度（实测外层 +402%
    #     vs 可部署族 −5%~+20%），历史全部读数与 A/B 的连续性锚，仅内部用；
    #   "symbol"：symbol 字典序（升序）——确定性可部署序之一；
    #   "random"：同占用日内随机序（queue_seed 定种子）——**对外声明标准**的可
    #     部署口径取多种子中位数（portfolio_metrics_dual 实现）；单序方差大
    #     （升序 −4.7% vs 降序 +20% 外层实测），任意单序不可作标准；
    #   "priority"（研究用）：流水带 "priority" 键时按其降序（缺键=0 中性末位）。
    queue_order: str = "exit_date"
    queue_seed: int = 42   # queue_order="random" 的种子（确定性复现用）

    def to_dict(self) -> dict:
        return asdict(self)


def _iso(v) -> str:
    if v is None:
        return ""
    if hasattr(v, "isoformat"):
        return v.isoformat()
    return str(v)


def build_equity_curve(trades: list[dict], model: PositionModel,
                       return_taken: bool = False):
    """逐笔流水 → 组合净值曲线（按 exit_date 升序，equity_0=1.0）。

    参数：
        trades: _compute_stats 产出的流水 dict（symbol/entry_date/exit_date/rr/
                avg_pnl_pct），entry/exit 可为 pd.Timestamp 或 str。
        model:  资金模型（PositionModel）。
        return_taken: True 时返回 (curve, taken)——taken 为真正进净值的流水
                子集（R7-P2 受控 A/B 的拥挤日闸需要逐笔归属；默认 False 保持
                原返回形状，零回归）。

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
        return (curve, sorted_t) if return_taken else curve

    # pos_cap 模式（默认，对齐实盘 budget=capital×pos_cap）：加总不复利。
    # R6-11c：freeze_pending=True 时每笔占用起点提前到 signal_date+1（挂单日）
    # ——排序与释放判定统一走 occupy_from（占用起点），exit 释放口径不变。
    def _occupy_from(t) -> str:
        if not model.freeze_pending:
            return _iso(t.get("entry_date"))
        sd = t.get("signal_date")
        if sd is None:
            return _iso(t.get("entry_date"))
        try:
            import pandas as _pd
            return _iso(_pd.Timestamp(sd) + _pd.Timedelta(days=1))
        except Exception:
            return _iso(t.get("entry_date"))

    if model.queue_order == "symbol":
        # 确定性可部署序之一：同占用日按 symbol 字典序（无未来信息）
        by_entry = sorted(
            trades,
            key=lambda t: (_occupy_from(t), str(t.get("symbol") or ""),
                           _iso(t.get("exit_date"))),
        )
    elif model.queue_order == "random":
        # 可部署序（随机）：同占用日内种子随机——单序方差大（升/降序外层差
        # 25pp 实测），标准口径用多种子中位数（见 portfolio_metrics_dual）
        import random as _random
        _rng = _random.Random(model.queue_seed)
        _rkeys = [_rng.random() for _ in range(len(trades))]
        by_entry = sorted(
            ((_rk, t) for _rk, t in zip(_rkeys, trades)),
            key=lambda p: (_occupy_from(p[1]), p[0]),
        )
        by_entry = [t for _, t in by_entry]
    elif model.queue_order == "priority":
        # 研究用：同占用日内高 priority 先进场（缺键/None 记 0=中性末位）
        by_entry = sorted(
            trades,
            key=lambda t: (_occupy_from(t), -float(t.get("priority") or 0.0),
                           _iso(t.get("exit_date"))),
        )
    else:
        by_entry = sorted(
            trades,
            key=lambda t: (_occupy_from(t), _iso(t.get("exit_date"))),
        )
    curve: list[dict] = []
    taken: list[dict] = []     # R7-P2：真正进净值的流水子集（return_taken 用）
    active: list[tuple] = []      # (symbol, allocation, exit_key)
    cash = model.capital
    equity, run_rr = 1.0, 0.0
    for t in by_entry:
        entry_key = _occupy_from(t)
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
        # R7-P2 质量分层：quality_alloc=True 且流水带 pos_cap 时按笔取 frac
        # （质量分位→3%-10% 弹性仓位）；键缺失/模式关都回落固定 pos_cap。
        frac = model.pos_cap
        if model.quality_alloc:
            _pc = t.get("pos_cap")
            if _pc is not None:
                frac = float(_pc)
        allocation = model.capital * frac
        # R6-11 有限资金模式：整手约束（floor 到 lot_size 倍数；不足一手跳过）
        # + 佣金 min_fee（买卖各一次，从单笔收益扣——对小预算单笔是固定税）。
        fee_drag = 0.0
        if model.lot_size > 0:
            entry_price = float(t.get("entry_price") or 0.0)
            if entry_price <= 0:
                continue                       # 无入场价（老流水）→ 有限模式跳过
            n_lots = int(allocation // (entry_price * model.lot_size))
            if n_lots <= 0:
                continue                       # 买不起一手（高价股 vs 小预算）
            allocation = n_lots * entry_price * model.lot_size
            if model.min_fee > 0:
                # min5 固定费：买+卖各一次 min(min_fee, 费率佣金)——保守取 min_fee
                # 全额（费率佣金 < 5 元的小单正是 min5 生效场景）
                fee_drag = 2.0 * model.min_fee / allocation
        if allocation > cash:
            continue

        ret = float(t.get("avg_pnl_pct") or 0.0) / 100.0
        ret -= model.slippage_bps * 2.0 / 10_000.0
        ret -= fee_drag
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
        taken.append(t)
    return (curve, taken) if return_taken else curve
