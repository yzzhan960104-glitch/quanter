# -*- coding: utf-8 -*-
"""颈线法多标的回测：流动性 top30 标的 × 颈线法识别 × 挂单回踩持有期模拟。

用户交易规则（v2 · 2026-07-18）：
    进场 = 突破后在 颈线+1·ATR 挂买单，有效期 max_wait 天，回踩触发即按挂单价成交；
           不回踩则放弃（防追高，但可能错过直接飞的信号）。
    止损 = 颈线−1·ATR（破位 1ATR 确认，防假摔洗盘）。
    止盈 = 分级 50%@颈线+H，50%@颈线+2H。
    超时 = 成交后 max_holding 日未达任一止盈，收盘卖剩余。

盈亏结构：risk=entry−stop=2·ATR（固定对称），收益=H−ATR / 2H−ATR，
盈亏比由形态深度 H/ATR 决定。

用法：
    PYTHONIOENCODING=utf-8 python -u scripts/neckline_backtest.py
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import math

import pandas as pd
from neckline_method_v0 import detect_neckline_method, DEFAULTS, compute_atr


# 持有期参数（用户规则，可调）—— 保留为向后兼容常量
MAX_HOLDING = 15           # 成交后超时持仓周期（交易日）
MAX_WAIT = 5               # 挂单等待回踩成交有效期（≈一周）
COOLDOWN = 5               # 信号去重冷却期（相邻信号合并为一次，同形态多日触发）
TOP_N = 30                 # 扫描流动性 top N 标的

# 执行层参数（param_iter 19维调参的执行层 7 旋钮；simulate_exit/scan_symbol 读此）
# 与识别层 DEFAULTS 分离：识别管"形态判定"，执行管"挂单/止盈/仓位/撤单"
EXEC_DEFAULTS = {
    "max_holding": 15,           # 成交后超时持仓日（=MAX_HOLDING）
    "max_wait": 5,               # 挂单等待回踩成交有效期（=MAX_WAIT）
    "cooldown": 5,               # 信号去重冷却（=COOLDOWN）
    "buy_limit_atr_mult": 1.0,   # 挂单价 = 颈线 + N×ATR（N=1.0=颈线上方1ATR挂买单等回踩）
    "tp1_h_mult": 1.0,           # 止盈1 = 颈线 + N×H（第一波减仓位，N=1.0=1H）
    "tp1_portion": 0.5,          # 止盈1 减仓比例（lot1 占比，剩余 lot2 持到 tp2；0.5=各半）
    "cancel_thresh_mult": 1.0,   # 等待期撤单阈值 = 颈线 + N×H（high≥此价即撤单防追高；
                                 # N=1.0=涨到tp1即撤(旧默认)；None=不撤单放飞所有信号）
    # 时间驱动移动止损（海龟风格，用户 2026-07-20）：前 grace 天宽限（止损=颈线-stop_mult×ATR
    # 固定不动，给趋势确认空间），grace 天后每日收紧 step×ATR（趋势不确认逐步退出），
    # 到 floor×ATR 卡住。grace=0/step=0 退化为固定止损（兼容旧行为）。详见 memory
    # neckline-trailing-stop.md。
    "trailing_grace": 0,         # 宽限天数 b（前 b 天不收紧；候选 5/10）
    "trailing_step": 0.0,        # 收紧速度 a（ATR/日；候选 0.05/0.1/0.15）
    "trailing_floor": 0.5,       # 最低 ATR 倍数（收紧上限；0=到颈线，0.5=颈线-0.5ATR）
}


def simulate_exit(sym_df: pd.DataFrame, signal_idx: int, c_star: float,
                  bottom: float, atr_val: float, exec: dict = None, id_cfg: dict = None):
    """挂单回踩进场 + 颈线−ATR 止损 + 分级止盈（执行层参数化版）。

    exec: 执行层参数 dict（见 EXEC_DEFAULTS），None 用默认。
    id_cfg: 识别层参数 dict（读 stop_atr_mult/tp_h_mult），None 用全局 DEFAULTS。
        阶段B：NecklineMethodStrategy 传 self.id_cfg 解耦全局依赖；scripts 调用不传（用默认）。
    执行：① 挂买单@颈线+buy_limit_atr_mult×ATR，max_wait 天内首个 low≤挂单价 成交，
    等待期 high≥撤单阈值(cancel_thresh_mult×H)则撤单；
    ② 成交后止损@颈线−stop_atr_mult×ATR + 止盈@tp1_h_mult×H / tp_h_mult×H，tp1_portion 减仓。
    """
    if exec is None:
        exec = EXEC_DEFAULTS
    if id_cfg is None:
        id_cfg = DEFAULTS
    max_holding = exec["max_holding"]
    max_wait = exec["max_wait"]
    H = c_star - bottom
    if H <= 0:
        return None
    buy_limit = c_star + exec["buy_limit_atr_mult"] * atr_val   # 挂单价（颈线+N×ATR）
    base_stop = c_star - id_cfg["stop_atr_mult"] * atr_val      # 止损基准（颈线−N×ATR，固定；
    # risk_pct 用此基准预告初始风险；持有期 trailing 动态调整见 loop）
    tp1 = c_star + exec["tp1_h_mult"] * H                         # 第一止盈（颈线+N×H）
    tp2 = c_star + id_cfg["tp_h_mult"] * H                      # 第二止盈（颈线+N×H，识别层参数）
    # 撤单阈值（None=不撤单放飞所有信号；否则等待期 high≥此价即撤单防追高）
    cancel_on = (c_star + exec["cancel_thresh_mult"] * H
                 if exec.get("cancel_thresh_mult") is not None else None)

    # ① 等回踩成交（用户逻辑修正：等待期 high≥tp1 → 涨幅已兑现，回踩是退潮，撤单）
    wait_end = min(signal_idx + max_wait, len(sym_df) - 1)
    buy_idx = None
    for i in range(signal_idx + 1, wait_end + 1):
        # 等待期价格已达撤单阈值 → 涨幅已兑现，回踩是退潮，撤单不买（过滤"猛突破后回踩"陷阱）
        if cancel_on is not None and float(sym_df["high"].iloc[i]) >= cancel_on:
            return {"signal_date": sym_df.index[signal_idx].date(),
                    "exit_reason": "skip_target_met",
                    "avg_pnl_pct": 0.0, "lot1_pnl_pct": 0.0, "lot2_pnl_pct": 0.0,
                    "neckline": round(c_star, 3), "entry": None,
                    "risk_pct": None, "tp1": round(tp1, 3), "tp2": round(tp2, 3)}
        if float(sym_df["low"].iloc[i]) <= buy_limit:
            buy_idx = i
            break
    if buy_idx is None:
        return {"signal_date": sym_df.index[signal_idx].date(),
                "exit_reason": "skip_no_pullback",
                "avg_pnl_pct": 0.0, "lot1_pnl_pct": 0.0, "lot2_pnl_pct": 0.0,
                "neckline": round(c_star, 3), "entry": None,
                "risk_pct": None, "tp1": round(tp1, 3), "tp2": round(tp2, 3)}

    entry = min(buy_limit, float(sym_df["open"].iloc[buy_idx]))
    # 限价买单成交价：open>buy_limit（盘中回踩）→ 成交 buy_limit；open<=buy_limit（跳空低开）
    # → 成交 open（市价<挂单价，更优）。旧版 entry=buy_limit 高估了跳空低开的买入价。
    end_idx = min(buy_idx + max_holding, len(sym_df) - 1)

    # ② 持有期逐根判 exit
    lot1_open, lot2_open = True, True
    lot1_pnl, lot2_pnl = None, None
    exit_reason = "timeout"
    exit_pos = end_idx   # 默认超时（is_last 或循环自然结束）；stop_loss/tp2 break 时覆盖
    for i in range(buy_idx, end_idx + 1):
        row = sym_df.iloc[i]
        high, low, close = float(row["high"]), float(row["low"]), float(row["close"])
        is_last = (i == end_idx)
        # 时间驱动移动止损（海龟风格）：前 trailing_grace 天用 base_stop（宽限，给趋势确认空间），
        # grace 天后每日收紧 trailing_step×ATR（趋势不确认逐步退出），到 trailing_floor×ATR 卡住。
        # grace=0/step=0 退化为固定止损（=base_stop，兼容旧行为）。
        holding_days = i - buy_idx
        grace = exec.get("trailing_grace", 0) or 0
        step = exec.get("trailing_step", 0) or 0
        if grace and step and holding_days > grace:
            eff_mult = id_cfg["stop_atr_mult"] - (holding_days - grace) * step
            floor = exec.get("trailing_floor")
            if floor is not None:
                eff_mult = max(eff_mult, floor)
            stop = c_star - eff_mult * atr_val
        else:
            stop = base_stop
        if low <= stop:                                  # 优先级1：止损（动态 trailing）
            lot1_pnl = lot2_pnl = (stop - entry) / entry
            lot1_open = lot2_open = False
            exit_reason = "stop_loss"
            exit_pos = i
            break
        if lot2_open and high >= tp2:                    # 优先级2：tp2（lot1 同日一并卖）
            lot2_pnl = (tp2 - entry) / entry
            lot2_open = False
            if lot1_open:
                lot1_pnl = (tp1 - entry) / entry
                lot1_open = False
            exit_reason = "tp2"
            exit_pos = i
            break
        if lot1_open and high >= tp1:                    # 优先级3：tp1（卖 lot1）
            lot1_pnl = (tp1 - entry) / entry
            lot1_open = False
            continue
        if is_last:                                       # 超时
            if lot1_open:
                lot1_pnl = (close - entry) / entry
            if lot2_open:
                lot2_pnl = (close - entry) / entry
            exit_reason = "timeout"
            exit_pos = i   # = end_idx
    if lot1_pnl is None or lot2_pnl is None:
        return None

    avg_pnl = exec["tp1_portion"] * lot1_pnl + (1 - exec["tp1_portion"]) * lot2_pnl
    # exit_price：分级止盈两批不同价，用 avg_pnl 反推加权平均离场价（供适配器 trade dict）
    exit_price_avg = entry * (1 + avg_pnl)
    return {
        "signal_date": sym_df.index[signal_idx].date(),
        "buy_date": sym_df.index[buy_idx].date(),
        "neckline": round(c_star, 3),
        "entry": round(entry, 3),
        "risk_pct": round((entry - base_stop) / entry * 100, 2),  # 初始风险（基准止损 base_stop，trailing 动态前）
        "tp1": round(tp1, 3), "tp2": round(tp2, 3),
        "H_over_ATR": round(H / atr_val, 2) if atr_val > 0 else None,
        "lot1_pnl_pct": round(lot1_pnl * 100, 2),
        "lot2_pnl_pct": round(lot2_pnl * 100, 2),
        "avg_pnl_pct": round(avg_pnl * 100, 2),
        "exit_reason": exit_reason,
        # 阶段B 新增（供 NecklineMethodStrategy trade dict；纯加字段，不改任何出场行为）
        "exit_date": sym_df.index[exit_pos].date(),
        "exit_price": round(exit_price_avg, 3),
        "holding_bars": exit_pos - buy_idx,
    }


def dedup_signals(signals, cooldown=COOLDOWN):
    """信号去重：相邻信号（idx 差 < cooldown）只保留第一个。

    物理意图：同一颈线被连续多日触发（滚动 replay 每个突破日都信号），实盘只应
    交易一次。冷却期内重复信号合并，避免同形态多计。
    """
    if not signals:
        return []
    deduped = [signals[0]]
    for sig_idx, res in signals[1:]:
        if sig_idx - deduped[-1][0] >= cooldown:
            deduped.append((sig_idx, res))
    return deduped


def kelly_metrics(pnls, dates, pos_cap=0.05, freq_cap=150):
    """凯利仓位 + 实盘可实现资金曲线年化（param_iter 目标函数）。

    f* = (bp − q) / b（b=盈亏比=平均盈利/平均亏损，p=胜率，q=1−p），约束 [0, 0.5]。

    实盘年化（2026-07-20 修复短窗爆炸 bug）：
        旧版 curve=Π(1+f*×r/100) 在近年高频短窗（创板科创 1250笔/1.5年、f*=0.2）下
        爆炸至 7257%~16495%——根源：凯利满仓复利假设所有信号独立可同时下注，无视
        持仓并发约束与资金容量。改 pos=min(f*, pos_cap) 封顶单笔仓位：
          · 凯利低（<pos_cap）→ 用凯利（信号差少下注，目标函数受惩罚）
          · 凯利高（≥pos_cap）→ 封顶 pos_cap（信号好最多下注这么多，实盘风控上限）
        区分度保留：curve 由每笔 r 分布（胜率×盈亏比）决定，非 f* 单值；f* 仅作信号
        质量参考返回。pos_cap=0.05（5%单笔）对应创板科创 ±20% 涨跌下 ~0.5% 组合单笔
        风险。年化60% = 每笔 5% 仓位平均贡献 +0.56%，实盘可达的真实目标。
    """
    n = len(pnls)
    if n == 0:
        return 0.0, 1.0, 0.0
    wins = [p for p in pnls if p > 0]
    losses = [-p for p in pnls if p < 0]   # 转正值
    if not wins or not losses:
        return 0.0, 1.0, 0.0
    p_win = len(wins) / n
    avg_win = sum(wins) / len(wins) / 100      # 比例
    avg_loss = sum(losses) / len(losses) / 100
    b = avg_win / avg_loss if avg_loss > 0 else 0
    q = 1 - p_win
    kelly = (b * p_win - q) / b if b > 0 else 0
    kelly = max(0.0, min(kelly, 0.5))           # 约束 [0, 0.5]
    pos = min(kelly, pos_cap)                   # 实盘仓位：凯利封顶 pos_cap（防爆+风控）
    # 频率封顶（2026-07-20 二次修复）：复利 curve 在高频（2905笔/1.5年）下即使 pos=5%
    # 仍爆炸至 784%——根源是复利假设所有信号可同时下注，无视持仓并发。按年封顶
    # freq_cap 笔（模拟实盘最大持仓约束下"先到先得"，每年最多复利 freq_cap 笔），
    # curve 受控。freq_cap=150 ≈ 同时持6只×持有10天×年250交易日（实盘持仓约束）。
    pnl_df = pd.DataFrame({"pnl": pnls, "date": pd.to_datetime(dates)}).sort_values("date")
    pnl_df["year"] = pnl_df["date"].dt.year
    sampled = pnl_df.groupby("year").head(freq_cap)
    curve = 1.0
    for r in sampled["pnl"]:
        curve *= (1 + pos * r / 100)
    days = (max(dates) - min(dates)).days
    years = days / 365.25 if days > 0 else 1.0
    ann = curve ** (1 / years) - 1 if curve > 0 else -1.0
    return kelly, curve, ann


def risk_metrics(pnls, dates, pos_cap=0.05, freq_cap=150):
    """凯利仓位 + 年化 + 信号夏普 + 资金曲线最大回撤（param_iter 多目标 score 用）。

    在 kelly_metrics 基础上补两个风险维度（sampling 与 kelly_metrics 完全一致：
    pos=min(kelly,pos_cap) 封顶 + 按年 head(freq_cap) 封顶），供 param_iter 约束式
    目标 score = 夏普/(1+回撤)（ann≥90% 硬门槛内）使用：
      - 信号夏普 = mean(仓位加权逐笔) / std × √(年交易数)
        物理意图：每笔信号的风险调整收益（与 ann 解耦，反映信号质量）。
        √年交易数 把 per-trade 夏普折到年口径（高频信号天然放大）。
      - 最大回撤 = 仓位加权逐笔 cumulative curve 的 max(peak−trough)/peak
        物理意图：实盘资金曲线最坏一段跌幅，压回撤（针对 top1 跨2月 −72% 痛点）。
    返回 (kelly, curve, ann, sharpe, max_dd)；前三个与 kelly_metrics 同源。
    """
    kelly, curve, ann = kelly_metrics(pnls, dates, pos_cap, freq_cap)
    if kelly <= 0 or len(pnls) < 2:
        return kelly, curve, ann, 0.0, 0.0
    pos = min(kelly, pos_cap)
    pnl_df = pd.DataFrame({"pnl": pnls, "date": pd.to_datetime(dates)}).sort_values("date")
    pnl_df["year"] = pnl_df["date"].dt.year
    sampled = pnl_df.groupby("year").head(freq_cap)
    returns = pos * sampled["pnl"] / 100   # 仓位加权逐笔收益（与 curve 同源 sampling）
    # 信号夏普（per-trade × √年交易数）
    std_r = float(returns.std())
    if std_r > 0:
        days = (max(dates) - min(dates)).days
        years = days / 365.25 if days > 0 else 1.0
        trades_per_year = len(returns) / years
        sharpe = float(returns.mean() / std_r * math.sqrt(trades_per_year))
    else:
        sharpe = 0.0
    # 最大回撤（逐笔 cumulative curve，peak→trough）
    cum = (1.0 + returns).cumprod()
    peak = cum.cummax()
    dd = (cum - peak) / peak
    max_dd = float(abs(dd.min())) if len(dd) > 0 else 0.0
    return kelly, curve, ann, sharpe, max_dd


def scan_symbol(sym_df, window, exec=None, id_cfg=None):
    """对单标的滚动识别 + 去重 + 模拟，返回成交结果列表与统计。

    exec: 执行层参数（见 EXEC_DEFAULTS），None 用默认。
    id_cfg: 识别层参数（见 DEFAULTS），None 用 {**DEFAULTS, window:window}。
        P1-b（2026-07-21）：参数化识别层，消除与 strategies/NecklineMethodStrategy.scan_at
        的双轨分叉（scan_symbol 此前硬编码全局 DEFAULTS、scan_at 参数化；现两侧都参数化，
        由 test_scan_symbol_matches_strategy 守护一致）。param_iter 不传 id_cfg（用默认
        读全局 DEFAULTS，run_one update 全局，行为不变）。
    """
    if exec is None:
        exec = EXEC_DEFAULTS
    if id_cfg is None:
        id_cfg = {**DEFAULTS, "window": window}
    # 预算全序列 ATR 一次（窗口对齐 id_cfg["window"]，与 scan_at 同源），detect 复用
    atr_full = compute_atr(sym_df["high"], sym_df["low"], sym_df["close"], window=id_cfg["window"])
    signals = []
    for i in range(id_cfg["window"], len(sym_df)):
        res = detect_neckline_method(sym_df.iloc[: i + 1], id_cfg,
                                     atr_series=atr_full.iloc[: i + 1])
        if res is not None:
            signals.append((i, res))
    signals = dedup_signals(signals, cooldown=exec["cooldown"])
    filled = []
    n_skip = 0
    for sig_idx, res in signals:
        sim = simulate_exit(sym_df, sig_idx, res["neckline"], res["bottom"], res["atr"], exec=exec)
        if sim is None:
            continue
        if sim["exit_reason"] in ("skip_no_pullback", "skip_target_met"):
            n_skip += 1
        else:
            # 附识别特征供深挖（突破质量/颈线压制/形态深度）
            vol_T = float(sym_df["volume"].iloc[sig_idx])
            vol5 = float(sym_df["volume"].iloc[max(0, sig_idx - 5):sig_idx].mean()) if sig_idx >= 5 else vol_T
            sim["breakout_vol_ratio"] = round(vol_T / vol5, 2) if vol5 > 0 else 0.0
            sim["suppression"] = res.get("suppression")
            sim["H_over_ATR"] = res.get("H_over_ATR")
            filled.append(sim)
    return filled, len(signals), n_skip


def main():
    lake_path = "data_lake/a_shares_daily.parquet"
    print(f"加载 {lake_path} ...")
    lake = pd.read_parquet(lake_path)
    window = DEFAULTS["window"]

    # 1. 全市场流动性过滤（amount 千元；近30日均 ≥ 1e5 千元 = 1 亿元，对齐 caisen）
    print("计算流动性（近30日均成交额 ≥ 1 亿元）...")
    syms = lake.index.get_level_values("symbol").unique().tolist()
    tradable = []
    for s in syms:
        try:
            amt_v = float(lake.xs(s, level="symbol")["amount"].tail(30).mean())
        except Exception:
            continue
        if amt_v >= 1e5:   # 1 亿元（千元单位）
            tradable.append((s, amt_v))
    tradable.sort(key=lambda x: x[1], reverse=True)
    print(f"可交易标的: {len(tradable)} 只 / 全市场 {len(syms)}")
    print(f"参数（窗口={window}, 挂单等待={MAX_WAIT}, 超时={MAX_HOLDING}, 冷却={COOLDOWN}）\n")

    # 2. 逐标的扫描（全市场，带进度）
    per_sym = []
    all_filled = []
    for idx, (sym, _) in enumerate(tradable):
        if idx % 50 == 0:
            print(f"  进度 {idx}/{len(tradable)} ({idx/len(tradable)*100:.0f}%) ...", flush=True)
        try:
            sym_df = lake.xs(sym, level="symbol").sort_index()
            filled, n_sig, n_skip = scan_symbol(sym_df, window)
        except Exception:
            continue
        pnls = [r["avg_pnl_pct"] for r in filled]
        wins = sum(1 for p in pnls if p > 0)
        n = len(filled)
        per_sym.append({
            "symbol": sym, "n_signals": n_sig, "n_filled": n, "n_skip": n_skip,
            "win_rate": wins / n if n else 0.0,
            "avg_pnl": sum(pnls) / n if n else 0.0,
            "total_pnl": sum(pnls),
        })
        for r in filled:
            r["symbol"] = sym
        all_filled.extend(filled)

    # 存 csv 供后续分析（标的筛选 / 参数调优）
    os.makedirs("logs", exist_ok=True)
    if all_filled:
        pd.DataFrame(all_filled).to_csv("logs/neckline_fullscan_trades.csv", index=False, encoding="utf-8-sig")
    pd.DataFrame(per_sym).to_csv("logs/neckline_fullscan_summary.csv", index=False, encoding="utf-8-sig")
    print(f"\n逐笔明细 → logs/neckline_fullscan_trades.csv ({len(all_filled)} 笔)")
    print(f"各标的汇总 → logs/neckline_fullscan_summary.csv ({len(per_sym)} 只)\n")

    # 3. 汇总
    from collections import Counter
    print(f"=== 颈线法全市场汇总（可交易 {len(tradable)} 只）===")
    total = len(all_filled)
    if total == 0:
        print("无成交信号")
        return
    total_wins = sum(1 for r in all_filled if r["avg_pnl_pct"] > 0)
    all_pnls = [r["avg_pnl_pct"] for r in all_filled]
    print(f"总成交笔数 = {total}")
    print(f"整体胜率   = {total_wins/total*100:.1f}%  ({total_wins}/{total})")
    print(f"平均收益   = {sum(all_pnls)/total:.2f}%")
    # 凯利仓位 + 年化（参数迭代目标函数）
    kelly_f, curve, ann = kelly_metrics(all_pnls, [pd.to_datetime(r["signal_date"]) for r in all_filled])
    print(f"凯利仓位   = {kelly_f*100:.1f}%   资金曲线 = {curve:.2f}   年化 = {ann*100:.1f}%")
    print(f"中位收益   = {sorted(all_pnls)[total//2]:.2f}%")
    print(f"总收益求和 = {sum(all_pnls):.1f}%")

    print(f"\nexit 分布:")
    rc = Counter(r["exit_reason"] for r in all_filled)
    for reason, cnt in rc.most_common():
        sub = [r["avg_pnl_pct"] for r in all_filled if r["exit_reason"] == reason]
        print(f"  {reason:10s}: {cnt:>4}笔 ({cnt/total*100:>4.0f}%)  avg={sum(sub)/len(sub):+.2f}%")

    print(f"\n=== 各标的（按 avg 收益降序）===")
    print(f"{'symbol':<12}{'信号':>5}{'成交':>5}{'放弃':>5}{'胜率':>7}{'均收益':>8}{'总收益':>9}")
    for s in sorted(per_sym, key=lambda x: x["avg_pnl"], reverse=True):
        print(f"{s['symbol']:<12}{s['n_signals']:>5}{s['n_filled']:>5}{s['n_skip']:>5}"
              f"{s['win_rate']*100:>6.0f}%{s['avg_pnl']:>+7.2f}%{s['total_pnl']:>+8.1f}%")

    # 标的级胜率分布（样本≥3 才有意义）
    valid = [s for s in per_sym if s["n_filled"] >= 3]
    if valid:
        wrs = [s["win_rate"] for s in valid]
        profitable = sum(1 for s in valid if s["avg_pnl"] > 0)
        print(f"\n=== 标的级分布（成交≥3笔的 {len(valid)} 只）===")
        print(f"胜率: max={max(wrs)*100:.0f}%  中位={sorted(wrs)[len(wrs)//2]*100:.0f}%  min={min(wrs)*100:.0f}%")
        print(f"avg收益为正: {profitable}/{len(valid)} 只")


if __name__ == "__main__":
    main()
