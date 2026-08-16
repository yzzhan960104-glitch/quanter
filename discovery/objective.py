# -*- coding: utf-8 -*-
"""L1 评估函数（spec §6.2，Plan 1 简化：inner/outer 二段，无 walk-forward）。

v2（P0-2 · 2026-08-02）：新增 evaluate_replay——用 backtest.replay 引擎（与 Win 主回测
同源）评估同一 params，产出 ReplayReport 口径指标（胜率/均rr/回撤/年化），供 compute_unit
replay 模式与"基于最新策略回测"使用；老 evaluate 保持 kelly/calmar 搜索口径不动。

核心范式（探查脚本 probe_champion_oos.py 验证过）：全历史跑 scan_symbol 一次 → 收集
all_filled → 按 signal_date 分段。不硬切 df（scan_symbol 用 sym_df.iloc[:i+1] 截至信号
历史识别，传完整 sym_df 才保 window/ATR 预热）；分段只发生在 signal_date 维度，无前视。

信息隔离（spec §6.2）：inner/outer 从同一客观 all_filled 派生（同 params 同 universe 的
全部信号），但 outer 的 metrics 不反馈任何选择——由调用方（judging/cli）保证只用 inner
排序、outer 只进报告。Plan 1 无搜索，"隔离"主要约束 outer 不进冠军排序。
"""
import logging

import pandas as pd

from strategies.neckline.method_v0 import DEFAULTS
from strategies.neckline.backtest import scan_symbol, risk_metrics, EXEC_DEFAULTS

# G7 告警可观测：discovery 跑批的降级点统一记日志，消「监控监控器」盲区
# （原 except:pass/continue 零日志 → 反复失败的标的/params 无人知晓）。
logger = logging.getLogger(__name__)

# 21 维参数分层键名（与 scripts/param_iter.PARAM_SPACE 同源；Plan 1 只需键名分层，不需候选值）
ID_KEYS = ["window", "min_touches", "min_suppression", "local_extrema_window",
           "min_bottoms", "breakout_vol_mult", "min_rr", "max_h_atr",
           "stop_atr_mult", "tp_h_mult", "decay_tau"]
EXEC_KEYS = ["max_holding", "max_wait", "cooldown", "buy_limit_atr_mult",
             "tp1_h_mult", "tp1_portion", "cancel_thresh_mult",
             "trailing_grace", "trailing_step", "trailing_floor"]


def run_full_scan(params, universe):
    """全历史跑一组参数 → all_filled（每条带 avg_pnl_pct/signal_date/symbol）。

    显式构造 id_cfg/exec_cfg 传入 scan_symbol（与 param_iter.run_one 同款，去全局 mutation）。
    遍历 universe 调 scan_symbol——单标的用 sym_df 全历史，保证 window/ATR 预热完整。
    """
    id_cfg = {**DEFAULTS, **{k: params[k] for k in ID_KEYS}}
    exec_cfg = {**EXEC_DEFAULTS, **{k: params[k] for k in EXEC_KEYS}}
    window = id_cfg["window"]
    all_filled = []
    for sym, sym_df in universe.items():
        try:
            filled, _n_sig, _n_skip = scan_symbol(sym_df, window, exec=exec_cfg, id_cfg=id_cfg)
            for r in filled:
                r["symbol"] = sym
            all_filled.extend(filled)
        except Exception:
            # G7 告警可观测：单标的 scan 异常不炸整批（spec §8 单 trial 失败不影响 run），
            # 但原零日志 continue → 反复失败的标的（数据残缺/内核崩溃）被静默吞掉，运维
            # 无法定位「哪只标的在吞异常」。加 warning 定位标的（控制流不变，仍 continue）。
            logger.warning("run_full_scan 标的 scan 异常已跳过 symbol=%s", sym, exc_info=True)
            continue
    return all_filled


def metrics_of(pairs):
    """[(pnl, date), ...] → 风险指标 dict（含 calmar=ann/max_dd，分层裁判 L1 主目标）。"""
    if not pairs:
        return dict(n=0, ann=0.0, sharpe=0.0, max_dd=0.0, kelly=0.0, calmar=0.0, curve=1.0)
    pnls = [p for p, _ in pairs]
    dates = [d for _, d in pairs]
    kelly, curve, ann, sharpe, max_dd = risk_metrics(pnls, dates)
    # calmar = ann/max_dd；max_dd 极小时 ann>0 给 inf（极佳），ann≤0 给 0
    if max_dd > 1e-9:
        calmar = ann / max_dd
    else:
        calmar = float("inf") if ann > 0 else 0.0
    return dict(n=len(pnls), ann=ann, sharpe=sharpe, max_dd=max_dd, kelly=kelly,
                calmar=calmar, curve=curve)


def _iter_pairs(all_filled, segment):
    """all_filled → (pnl, date) 生成器（segment 过滤单源——评审 2026-08-15 去重：
    segment_metrics 与 yearly_metrics 共用 to_datetime+covers 形状，防双份漂移）。"""
    for r in all_filled:
        d = pd.to_datetime(r["signal_date"])
        if segment.covers(d):
            yield r["avg_pnl_pct"], d


def segment_metrics(all_filled, segment, embargo_days=0):
    """从 all_filled 按 signal_date 过滤到 segment（embargo 偏移）→ metrics dict。

    embargo：segment.start + embargo_days 天内的 signal_date 不计入（吸收前段持仓
    跨越——颈线法 trailing grace/max_holding 可达数日~20 日，inner 末信号持仓可能跨到
    outer 初；embargo 让 outer 评估跳过这段，防 inner 持仓污染 outer 信号统计）。
    """
    from datetime import timedelta
    embargo_cutoff = segment.start + timedelta(days=embargo_days)
    pairs = [(pnl, d) for pnl, d in _iter_pairs(all_filled, segment)
             if not (embargo_days > 0 and d.date() < embargo_cutoff)]
    return metrics_of(pairs)


def yearly_metrics(all_filled, segment, min_trades=30):
    """A2（DG-G4）：segment 内 all_filled 按信号自然年分块 → {年: calmar}。

    Why 按年 min：整段 calmar 被单一大年淹没（2025 特化教训——wf 四折实证冠军
    参数 2022 折外 -0.62 而整段 inner 44.87），按年分块让参数必须在【每一个】
    年份站得住才得高分。
    Why n<min_trades 记 0.0（不剔除）：剔除=逃考——信号稀疏年恰是参数在该年
    不适配的证据，记 0 让它拖累 min（保守，不奖励缺席）。
    无信号年份不入 dict（年份由实际信号决定，非日历枚举）。
    """
    from collections import defaultdict
    by_year = defaultdict(list)
    for pnl, d in _iter_pairs(all_filled, segment):
        by_year[d.year].append((pnl, d))
    out = {}
    for y in sorted(by_year):
        m = metrics_of(by_year[y])
        out[y] = m["calmar"] if m["n"] >= min_trades else 0.0
    return out


def evaluate(params, universe, split):
    """评估给定 params 的 inner/outer 两段。

    跑一次 run_full_scan（同 params 同 universe 的客观信号），按 split 分段。
    A2（DG-G4 · 2026-08-14）：inner dict 注入 yearly_calmar（各自然年 calmar，
    n<30 年记 0）与 min_yearly_calmar（搜索排序新目标）——整段 calmar 等字段
    保留（feasibility_gate 等既有消费者兼容，纯增量）。
    信息隔离：返回的 outer metrics 仅供报告，调用方不得用于冠军排序/选择（spec §6.2）。
    """
    all_filled = run_full_scan(params, universe)
    inner_m = segment_metrics(all_filled, split.inner, embargo_days=0)
    yearly = yearly_metrics(all_filled, split.inner)
    inner_m["yearly_calmar"] = yearly
    inner_m["min_yearly_calmar"] = min(yearly.values()) if yearly else 0.0
    return {
        "inner": inner_m,
        "outer": segment_metrics(all_filled, split.outer, embargo_days=split.embargo_days),
        "n_total": len(all_filled),
    }


def report_metrics(report) -> dict:
    """ReplayReport → 指标 dict（compute_unit result 用，无 trades 轻量）。"""
    return {
        "n_hits": report.n_hits,
        "win_rate": report.win_rate,
        "avg_rr": report.avg_rr,
        "max_drawdown": report.max_drawdown,
        "annualized_return": report.annualized_return,
        "n_trading_days": report.n_trading_days,
    }


def _compact_report(report) -> dict:
    """ReplayReport → 压缩快照（去 trades/equity_curve/metadata，防 result.json 膨胀）。"""
    return {k: v for k, v in report.__dict__.items()
            if k not in ("trades", "equity_curve", "metadata")}


def evaluate_replay(params, universe, split, start=None, end=None,
                    position_model: dict | None = None) -> dict:
    """replay 模式评估（P0-2）：给定 params 跑 backtest.replay → ReplayReport 口径指标。

    与 discovery evaluate 的差异：
        - 引擎：backtest.replay + NecklineMethodStrategy(cfg_override=params)（主回测同源）；
        - 指标：ReplayReport 聚合（n_hits/win_rate/avg_rr/max_drawdown/annualized_return），
          非 kelly/calmar；
        - 分段：默认按 split.inner/outer 各跑一段；显式 start/end → 只评单段（inner）。
        - 资金：position_model（PositionModel.to_dict()）透传，None=默认 pos_cap 口径。
    """
    from backtest.models import PositionModel
    from backtest.replay import replay
    from strategies.neckline.strategy import NecklineMethodStrategy

    strategy = NecklineMethodStrategy(cfg_override=params)
    pm = None
    if position_model:
        valid = {k: v for k, v in position_model.items()
                 if k in PositionModel.__dataclass_fields__}
        pm = PositionModel(**valid) if valid else None

    if start is not None and end is not None:
        segments = [("inner", start, end)]
    else:
        segments = [
            ("inner", split.inner.start, split.inner.end),
            ("outer", split.outer.start, split.outer.end),
        ]
    out, inner_report = {}, None
    for name, seg_start, seg_end in segments:
        # P1-5（2026-08-03）：每段新建策略实例——颈线策略带跨 T cooldown 状态，
        # inner/outer 复用同一实例会让 outer 被 inner 的锚点污染（A8 fail-fast
        # 已把这种复用升级为 ValueError，此处是真实路径修复）。
        strategy = NecklineMethodStrategy(cfg_override=params)
        rep = replay(universe, strategy, str(seg_start), str(seg_end), position_model=pm)
        out[name] = report_metrics(rep)
        if name == "inner":
            inner_report = rep
    outer = out.get("outer", {})
    return {
        "inner": out["inner"],
        "outer": outer,
        "n_total": out["inner"]["n_hits"] + (outer.get("n_hits", 0) if outer else 0),
        "report": _compact_report(inner_report) if inner_report is not None else {},
    }


# ============================================================================
# R1-1（2026-08-16）：组合约束口径评估——搜索目标与实盘口径裂缝的修复
# ============================================================================
def portfolio_metrics(filled, segment, universe_dates, embargo_days=0,
                      position_model=None) -> dict:
    """run_full_scan 的 filled → 组合约束口径指标（build_equity_curve 单源后处理）。

    R0 实证（口径裂缝）：scan 口径（evaluate/metrics_of 假设信号独立可下注）与 replay
    组合口径（max_positions=6/资金约束）对同一参数 outer ann 符号反转（+18.4% vs
    -23.9%）。本函数把 replay 引擎的净值算法（backtest.models.build_equity_curve 单源）
    直接后处理在 scan 产物上——共享昂贵步（run_full_scan），只加 O(n log n) 组合模拟，
    让搜索能以实盘口径目标驱动（快 60-100 倍于跑 replay 引擎）。

    与 replay 引擎的已知边界差异（标定脚本 diag/r1_portfolio_calibration.py 量化）：
      - 分段按 signal_date（scan 口径惯例）；replay 按引擎逐日回放窗口；
      - 段末未平仓持仓不截断（replay 引擎同款——exit_date 可越出段末）；
      - 完整性 gate（_apply_continuity_filter）不在本路径——universe 由 freeze 已筛。
    """
    from datetime import timedelta
    from backtest.models import PositionModel, build_equity_curve

    pm = position_model or PositionModel()   # 默认 pos_cap=0.05/max_positions=6/slip 5bps=实盘同源
    embargo_cutoff = segment.start + timedelta(days=embargo_days)
    trades = []
    for r in filled:
        # 日期守卫：缺 signal_date 的记录（合成数据/异常产物）跳过——pd.to_datetime(None)
        # 返 None 会穿透下游 risk_metrics 的 max-min（实弹 TypeError 教训）。
        if r.get("signal_date") is None:
            continue
        d = pd.to_datetime(r["signal_date"])
        if d is None or pd.isna(d) or not segment.covers(d):
            continue
        if embargo_days > 0 and d.date() < embargo_cutoff:
            continue
        trades.append({"symbol": r.get("symbol"),
                       "signal_date": d,                  # 保留（sharpe extras 消费）
                       "entry_date": r.get("buy_date"),   # scan 产物成交日键名（replay 侧叫 entry_date）
                       "exit_date": r.get("exit_date"),
                       "avg_pnl_pct": r["avg_pnl_pct"], "rr": 0.0})
    curve = build_equity_curve(trades, pm)
    # n_trading_days：段内交易日数（镜像 replay.py:222-227——各 symbol 同区间取一计数）
    n_days = int(((universe_dates >= pd.Timestamp(segment.start)) &
                  (universe_dates <= pd.Timestamp(segment.end))).sum())
    equity_end = curve[-1]["equity"] if curve else 1.0
    ann = equity_end ** (252.0 / n_days) - 1.0 if (n_days > 0 and equity_end > 0) else 0.0
    # 净值曲线峰谷最大回撤（负值口径，同 ReplayReport.max_drawdown）
    peak, max_dd = 1.0, 0.0
    for pt in curve:
        peak = max(peak, pt["equity"])
        if pt["equity"] - peak < max_dd:
            max_dd = pt["equity"] - peak
    n_taken = len(curve)                       # 并发/资金约束后真正进净值的笔数
    wins = sum(1 for pt in curve if pt["pnl_pct"] > 0)
    if abs(max_dd) > 1e-9:
        calmar = ann / abs(max_dd)
    else:
        calmar = float("inf") if ann > 0 else 0.0
    out = {"n": len(trades), "n_taken": n_taken,
           "win_rate": wins / n_taken if n_taken else 0.0,
           "ann": ann, "max_dd": -abs(max_dd), "calmar": calmar,
           "equity_end": equity_end, "n_days": n_days}
    # 信号口径补充键（sharpe/kelly）：DSR 门控（runner）与 trial 展示消费 sharpe——组合
    # 模拟本身不产 sharpe，用段内信号序列的 metrics_of 补（选择统计量 min_yearly_calmar
    # 仍是组合口径；DSR 的多重比较修正由 n_trials 主导，sharpe 用信号口径代理可接受，
    # 此处显式注释声明混口径边界）。metrics_of 的 n/ann/max_dd/calmar 被组合值覆盖。
    sig = metrics_of([(float(t["avg_pnl_pct"]), t["signal_date"]) for t in trades])
    out["sharpe"] = sig["sharpe"]
    out["kelly"] = sig["kelly"]
    return out


def evaluate_portfolio(params, universe, split, position_model=None) -> dict:
    """R1-1 组合口径评估：run_full_scan 一次 → inner/outer 分段 + 分年组合指标。

    返回形状与 evaluate() 对齐（inner 含 yearly_calmar/min_yearly_calmar），下游
    feasibility_gate / 排序可无缝消费。信息隔离语义同 evaluate：outer 只进报告。
    yearly 口径：按信号自然年构造 Segment 复用 portfolio_metrics（A2 的 min_yearly_calmar
    在组合口径下的同款「每一年都站得住」判别——n<30 年记 0.0 逃考惩罚同源）。
    """
    from discovery.split import Segment
    all_filled = run_full_scan(params, universe)
    universe_dates = next(iter(universe.values())).index   # 交易日历（同区间取一）
    inner_m = portfolio_metrics(all_filled, split.inner, universe_dates,
                                position_model=position_model)
    by_year = {}
    for r in all_filled:
        d = pd.to_datetime(r["signal_date"])
        if split.inner.covers(d):
            by_year.setdefault(d.year, []).append(r)
    yearly = {}
    for y in sorted(by_year):
        seg_y = Segment(f"y{y}", pd.Timestamp(f"{y}-01-01").date(),
                        pd.Timestamp(f"{y}-12-31").date())
        m = portfolio_metrics(by_year[y], seg_y, universe_dates,
                              position_model=position_model)
        yearly[y] = m["calmar"] if m["n"] >= 30 else 0.0
    inner_m["yearly_calmar"] = yearly
    inner_m["min_yearly_calmar"] = min(yearly.values()) if yearly else 0.0
    return {
        "inner": inner_m,
        "outer": portfolio_metrics(all_filled, split.outer, universe_dates,
                                   embargo_days=split.embargo_days,
                                   position_model=position_model),
        "n_total": len(all_filled),
    }


# ============================================================================
# P5（2026-08-13 · spec §6.1）：walk-forward 交叉验证评估
# ============================================================================
def evaluate_wf(params, wf_split, warmup_days=180):
    """P5 多折 walk-forward 评估：每折独立 universe（防幸存者偏差）→ 折内 train/oos 指标。

    与 evaluate（二段 holdout）的差异：
      - 每折用 load_universe_window(train.start, train.end) 独立重建标的池（折末 30 日
        流动性口径），不复用「今天」标的池——历史折不用未来信息选股（spec §6.1）；
      - 每折跑一次 run_full_scan（该折 universe），按 signal_date 分 train/oos 两段
        （embargo 吸收边界持仓跨越，与 segment_metrics 同源）；
      - 不落库、不参与选择——分析口径（cmd_wf 交叉验证一致性用）。
    返回 list[dict]：每折 {fold, n_symbols, train, oos, n_total}。
    """
    from discovery.snapshot import load_universe_window
    out = []
    for name, train, oos in wf_split.folds:
        # 选股截止 train.end（信息不泄漏），数据延伸至 oos.end（评估 OOS 年需要数据）——
        # load_universe_window 内已做选股/数据双窗口分离（wf smoke 曾抓到数据止于
        # sel_end → oos 段零信号的 bug）
        universe = load_universe_window(train.start, train.end, data_end=oos.end,
                                        warmup_days=warmup_days)
        all_filled = run_full_scan(params, universe)
        out.append({
            "fold": name,
            "n_symbols": len(universe),
            "train": segment_metrics(all_filled, train, embargo_days=0),
            "oos": segment_metrics(all_filled, oos, embargo_days=wf_split.embargo_days),
            "n_total": len(all_filled),
        })
    return out
