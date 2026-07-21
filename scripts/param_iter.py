# -*- coding: utf-8 -*-
"""颈线法参数迭代引擎 v2（创板科创 / 2025至今 / 22维概念 / 时间预算）。

重构（2026-07-20，详见 memory caisen-neckline-paramiter-baseline）：
  ① universe 收窄：创业板(300/301)+科创板(688/689)，2025-01-01 至今，可交易(≥1亿) ≈1334 只
     （原 param_iter 砍 top100=死区2.3%，代理目标错配；现用创板科创近年，胜率49%、空间大）
  ② 时间窗缩短：仅 2025 至今（~130 交易日），单组 ~167s，8h 可跑 ~173 组
  ③ 目标函数修复：kelly_metrics 用 pos=min(f*, 0.05) 实盘年化（旧版满仓复利爆炸至7257%）
  ④ 22 维概念全调：识别层 11（DEFAULTS）+ 执行层 10（EXEC_DEFAULTS 7 原硬编码 + trailing 3 移动止损）= 21 可调
                   + universe(创板科创2025至今) 固定 = 22 概念
  ⑤ 搜索策略：阶段1 随机采样 n_random 组（覆盖18维空间）→ 阶段2 top-K 邻域贪心 ±1 档细化
  ⑥ 运行模式：--time-budget 秒数跑到时间耗尽，state 持久化可续（kill/重启自动接续）

用法：
    PYTHONIOENCODING=utf-8 python -u scripts/param_iter.py --time-budget 28800   # 8h
"""
import os
import sys
import json
import time
import argparse
import random
import copy

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
from neckline_method_v0 import DEFAULTS
from neckline_backtest import scan_symbol, kelly_metrics, risk_metrics, EXEC_DEFAULTS


# ===== 21 维参数空间（识别层 id=DEFAULTS / 执行层 exec=EXEC_DEFAULTS）=====
# 每维给 2-3 档候选；window/min_suppression/max_h_atr/stop_atr_mult/tp_h_mult 沿用 v1 网格档
# 新纳入：min_touches/local_extrema_window/min_bottoms/breakout_vol_mult/min_rr/decay_tau（识别层原漏调）
#         + max_holding/max_wait/cooldown/buy_limit_atr_mult/tp1_h_mult/tp1_portion/cancel_thresh_mult（执行层原硬编码）
PARAM_SPACE = [
    # —— 识别层（DEFAULTS）：形态判定 ——
    ("window",              "id",   [40, 60, 80]),          # ① 识别窗口
    ("min_touches",         "id",   [2, 3]),                # ② 颈线聚集足够性
    ("min_suppression",     "id",   [0.5, 0.6, 0.7]),       # ③ 压制时长
    ("local_extrema_window","id",   [3, 5]),                # ④ 底部极值窗口
    ("min_bottoms",         "id",   [2, 3]),                # ⑤ 双底/三底门槛
    ("breakout_vol_mult",   "id",   [1.0, 1.5, 2.0]),       # ⑥ 突破带量
    ("min_rr",              "id",   [1.0, 1.5, 2.0]),       # ⑦ 盈亏比守卫
    ("max_h_atr",           "id",   [3.0, 4.0, 5.0]),       # ⑧ 形态深度上限
    ("stop_atr_mult",       "id",   [1.0, 1.5]),            # ⑨ 止损 ATR 倍数
    ("tp_h_mult",           "id",   [1.5, 2.0, 2.5]),       # ⑩ 止盈2 的 H 倍数
    ("decay_tau",           "id",   [None, 30, 60]),        # ⑪ 颈线时间衰减（None=等权）
    # —— 执行层（EXEC_DEFAULTS）：挂单/止盈/仓位/撤单 ——
    ("max_holding",         "exec", [10, 15, 20]),          # ⑫ 成交后超时持仓日
    ("max_wait",            "exec", [3, 5, 8]),             # ⑬ 挂单等回踩有效期
    ("cooldown",            "exec", [3, 5, 8]),             # ⑭ 信号去重冷却
    ("buy_limit_atr_mult",  "exec", [0.5, 1.0, 1.5]),       # ⑮ 挂单价 ATR 倍数
    ("tp1_h_mult",          "exec", [0.5, 1.0, 1.5]),       # ⑯ 止盈1 的 H 倍数
    ("tp1_portion",         "exec", [0.3, 0.5, 0.7]),       # ⑰ 止盈1 减仓比例
    ("cancel_thresh_mult",  "exec", [None, 1.0, 2.0]),      # ⑱ 撤单阈值（None=不撤放飞）
    # —— trailing 时间驱动移动止损（海龟风格 · simulate_exit 生效条件 grace>0 AND step>0）——
    # grace=0 退化为固定止损（=当前 EXEC_DEFAULTS 默认，作基线对照，验证 trailing vs 固定谁优）
    ("trailing_grace",  "exec", [0, 5, 10]),        # ⑲ 宽限期（0=关闭固定止损；5/10=前 N 天不收紧）
    ("trailing_step",   "exec", [0.05, 0.1, 0.15]), # ⑳ 收紧速度（ATR/日；grace 后每日 stop 上移）
    ("trailing_floor",  "exec", [0.0, 0.5]),        # ㉑ 收紧下限（0=到颈线；0.5=颈线−0.5ATR 卡住）
]
# universe（创板+科创 2025至今）固定不调 = 第 22 个"概念参数"（21 可调 + universe）

STATE_FILE = "logs/param_iter_state.json"
TARGET_ANN = 0.90          # 目标凯利年化 90%（用户 2026-07-21：≥90% 同时高夏普低回撤）
START_DATE = "2025-01-01"  # 回测起始日（缩短年限提速）


def score_of(ann, sharpe, max_dd):
    """复合软约束：ann × 夏普 / (1+回撤)，全程有梯度（ann<90% 不归零）。

    物理意图（用户 2026-07-21 复核改版：约束式 90% 硬门槛实证 5 组全 0、邻域退化 → 改软约束）：
    ann 在乘积里天然主导（高 ann 分高，邻域向高 ann 爬），夏普/回撤作风险调整。
    达标区(ann≥90%)与非达标区连续过渡——不强制 90% 但 ann 大本身让分高，自动接近
    「≥90% + 高夏普 + 低回撤」诉求。ann≤0（亏钱）或 max_dd≥100%（爆仓）归零。
    例：ann79%×夏普9/(1+3%)≈6.9；ann95%×夏普9/(1+3%)≈8.3（达标组分更高）。
    """
    if ann <= 0 or max_dd >= 1.0:
        return 0.0
    return ann * sharpe / (1.0 + max_dd)


def _breadth_at(breadth, date):
    """查 market_breadth 在 date 的值（date 非交易日 → ffill 最近≤date 的值，无前视）。

    breadth: pd.Series（DatetimeIndex 逐日，值=站上 MA60 比例）。date: date/Timestamp。
    返回 float 或 None（无数据）。P1-c 宽度顺势加权用。
    """
    try:
        ts = pd.Timestamp(date)
        if ts in breadth.index:
            return float(breadth.loc[ts])
        idx = breadth.index.get_indexer([ts], method="ffill")[0]
        return float(breadth.iloc[idx]) if idx >= 0 else None
    except Exception:
        return None


def is_target_board(sym):
    """创业板(300/301) + 科创板(688/689)。"""
    code = sym.split(".")[0]
    return code.startswith(("300", "301", "688", "689"))


def load_universe():
    """加载创板+科创 2025至今可交易(近30日均成交额≥1亿)标的 → {symbol: sym_df}。"""
    lake = pd.read_parquet("data_lake/a_shares_daily.parquet")
    lake = lake[lake.index.get_level_values("date") >= pd.Timestamp(START_DATE)]
    syms = lake.index.get_level_values("symbol").unique().tolist()
    amt = lake.groupby("symbol")["amount"].apply(lambda s: s.tail(30).mean() if len(s) > 0 else 0.0)
    tradable = [s for s in syms if is_target_board(s) and amt.get(s, 0.0) >= 1e5]
    universe = {}
    for s in tradable:
        try:
            universe[s] = lake.xs(s, level="symbol").sort_index()
        except Exception:
            continue
    return universe


def random_params(rng):
    """阶段1：每维独立随机选一个候选值（覆盖 18 维空间）。"""
    return {k: rng.choice(v) for k, _, v in PARAM_SPACE}


def neighbor_params(params, rng):
    """阶段2：贪心邻域——从基准参数随机选 1-2 维移到相邻档（±1）。"""
    nb = dict(params)
    keys = rng.sample(list(params.keys()), rng.choice([1, 1, 2]))  # 2/3 概率改1维，1/3 改2维
    for k in keys:
        vals = next(v for kk, _lay, v in PARAM_SPACE if kk == k)
        cur = params[k]
        idx = vals.index(cur) if cur in vals else len(vals) // 2
        new_idx = max(0, min(len(vals) - 1, idx + rng.choice([-1, 1])))
        nb[k] = vals[new_idx]
    return nb


def params_key(params):
    """参数组 → 去重用的稳定字符串键（None 也要能序列化）。"""
    return json.dumps(params, ensure_ascii=False, sort_keys=True, default=str)


def run_one(params, universe, breadth=None):
    """跑一组参数：识别层 set DEFAULTS、执行层 set EXEC_DEFAULTS，遍历 universe 算凯利年化。

    breadth: 可选 market_breadth Series（P1-c 宽度顺势加权）。非空时信号日 breadth≥0.4
        → avg_pnl×1.5（等效加仓 1.5×，研究验证 memory 2024 +1.95→+5.43%；research 用途，
        筹效突破 pos_cap，实盘需另调 cap 或加权方式）。
    返回 (年化, 凯利, 曲线, 笔数, 夏普, 回撤)。临时改全局，finally 恢复（单进程安全）。
    """
    id_params = {k: params[k] for k, lay, _ in PARAM_SPACE if lay == "id"}
    exec_params = {k: params[k] for k, lay, _ in PARAM_SPACE if lay == "exec"}

    orig_id = copy.deepcopy(DEFAULTS)
    orig_exec = copy.deepcopy(EXEC_DEFAULTS)
    DEFAULTS.update(id_params)
    EXEC_DEFAULTS.update(exec_params)
    try:
        all_filled = []
        window = DEFAULTS["window"]
        exec_cfg = EXEC_DEFAULTS   # scan_symbol 读此全局（已 update）
        for sym, sym_df in universe.items():
            try:
                filled, _n_sig, _n_skip = scan_symbol(sym_df, window, exec=exec_cfg)
                for r in filled:
                    r["symbol"] = sym
                all_filled.extend(filled)
            except Exception:
                continue
        if not all_filled:
            return -1.0, 0.0, 1.0, 0, 0.0, 0.0
        # P1-c 宽度顺势加权（可选）：信号日 breadth≥0.4 → avg_pnl×1.5（等效加仓）
        if breadth is not None:
            for r in all_filled:
                bd = _breadth_at(breadth, r["signal_date"])
                if bd is not None and bd >= 0.4:
                    r["avg_pnl_pct"] *= 1.5
        pnls = [r["avg_pnl_pct"] for r in all_filled]
        dates = [pd.to_datetime(r["signal_date"]) for r in all_filled]
        kelly, curve, ann, sharpe, max_dd = risk_metrics(pnls, dates)
        return ann, kelly, curve, len(all_filled), sharpe, max_dd
    finally:
        DEFAULTS.clear(); DEFAULTS.update(orig_id)        # 恢复识别层
        EXEC_DEFAULTS.clear(); EXEC_DEFAULTS.update(orig_exec)  # 恢复执行层


def load_state():
    if os.path.exists(STATE_FILE):
        try:
            return json.load(open(STATE_FILE, encoding="utf-8"))
        except Exception:
            pass
    return {"tried": {}, "best": None, "best_score": 0.0, "best_ann": -1.0,
            "best_sharpe": 0.0, "best_max_dd": 0.0, "history": []}


def save_state(state):
    os.makedirs("logs", exist_ok=True)
    json.dump(state, open(STATE_FILE, "w", encoding="utf-8"), indent=2, ensure_ascii=False)


def main():
    ap = argparse.ArgumentParser(description="颈线法参数迭代 v2（创板科创/2025至今/22维概念）")
    ap.add_argument("--time-budget", type=int, default=28800, help="时间预算秒数（默认 28800=8h）")
    ap.add_argument("--n-random", type=int, default=80, help="阶段1随机采样组数（默认80）")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--breadth-boost", action="store_true",
                    help="P1-c 宽度顺势加权（信号日 breadth≥0.4 → pnl×1.5，验证非熊市加仓）")
    args = ap.parse_args()

    rng = random.Random(args.seed)
    print(f"=== 颈线法参数迭代 v2 ===")
    print(f"universe: 创板+科创 {START_DATE} 至今 | 目标: 凯利年化≥{TARGET_ANN*100:.0f}% | 预算: {args.time_budget/3600:.1f}h")
    print(f"加载 universe ...")
    universe = load_universe()
    print(f"universe: {len(universe)} 只标的")

    # P1-c 宽度顺势加权（可选，--breadth-boost 开启）
    breadth = None
    if args.breadth_boost:
        bp = "data_lake/market_breadth.parquet"
        if os.path.exists(bp):
            breadth = pd.read_parquet(bp)["breadth"]
            n_boost = int((breadth >= 0.4).sum())
            print(f"宽度顺势加权开启：{bp} {len(breadth)} 日，≥0.4 占 {n_boost/len(breadth)*100:.0f}% → pnl×1.5")
        else:
            print(f"[warn] {bp} 不存在，--breadth-boost 失效（回退无加权）")

    state = load_state()
    # 兼容旧 state（v2 18维 ann 单目标 → v3 21维 score 多目标）：补 score 字段
    state.setdefault("best_score", 0.0)
    state.setdefault("best_sharpe", 0.0)
    state.setdefault("best_max_dd", 0.0)
    state.setdefault("best_ann", -1.0)
    print(f"state 续跑: 已试 {len(state['tried'])} 组 | v3.1 软约束(ann×夏普/(1+回撤)) best_score={state['best_score']:.3f}")

    t_start = time.time()
    n_eval_this_run = 0

    while time.time() - t_start < args.time_budget:
        n_tried = len(state["tried"])
        # 采样策略：阶段1 随机（前 n_random 组）→ 阶段2 贪心（top-K 邻域）
        if n_tried < args.n_random:
            params = random_params(rng)
            phase = "random"
        else:
            top = sorted(state["history"], key=lambda x: x.get("score", 0), reverse=True)[:10]
            if not top:
                params = random_params(rng); phase = "random"
            else:
                base = rng.choice(top)["params"]
                params = neighbor_params(base, rng)
                phase = "greedy"

        pk = params_key(params)
        if pk in state["tried"]:
            continue   # 去重（21维空间大，重复概率低）

        t0 = time.time()
        ann, kelly, curve, n, sharpe, max_dd = run_one(params, universe, breadth=breadth)
        dt = time.time() - t0
        n_eval_this_run += 1
        score = score_of(ann, sharpe, max_dd)

        state["tried"][pk] = {"ann": round(ann, 4), "kelly": round(kelly, 4),
                              "curve": round(curve, 3), "n": n,
                              "sharpe": round(sharpe, 3), "max_dd": round(max_dd, 4),
                              "score": round(score, 4), "phase": phase}
        state["history"].append({"idx": n_tried, "ann": round(ann, 4),
                                 "sharpe": round(sharpe, 3), "max_dd": round(max_dd, 4),
                                 "score": round(score, 4), "params": params, "phase": phase})
        improved = ""
        if score > state["best_score"]:
            state["best_score"] = score
            state["best"] = params
            state["best_ann"] = ann
            state["best_sharpe"] = sharpe
            state["best_max_dd"] = max_dd
            improved = " ★新最优"

        # 控制历史长度（防无限膨胀；tried 字典才是真·去重源），v3 按 score 截断
        if len(state["history"]) > 2000:
            state["history"] = sorted(state["history"], key=lambda x: x.get("score", 0), reverse=True)[:1000]

        save_state(state)

        elapsed = time.time() - t_start
        remain = max(0, args.time_budget - elapsed)
        print(f"[{n_tried+1}|{phase:6s}|{dt:5.0f}s|剩{remain/60:4.0f}min] "
              f"年化{ann*100:6.1f}% 夏普{sharpe:5.2f} 回撤{max_dd*100:5.1f}% 得分{score:6.3f} "
              f"{n:5d}笔{improved} | 最优得分{state['best_score']:.3f}"
              f"(年化{state['best_ann']*100:.1f}%/夏普{state['best_sharpe']:.2f}/回撤{state['best_max_dd']*100:.1f}%)")

        if ann >= TARGET_ANN and score == state["best_score"] and score > 0:
            print(f"  🎯 达标 ann≥90% 且 score 新高！参数: {params}")

        if n_eval_this_run % 10 == 0:
            print(f"  --- 进度 {len(state['tried'])} 组 | best_score={state['best_score']:.3f} "
                  f"(年化{state['best_ann']*100:.1f}%/夏普{state['best_sharpe']:.2f}/回撤{state['best_max_dd']*100:.1f}%) ---")

    print(f"\n=== 时间预算耗尽（本轮 {n_eval_this_run} 组，累计 {len(state['tried'])} 组）===")
    print(f"最优 score: {state['best_score']:.3f}（约束式 ann≥90% 内 夏普/(1+回撤)）")
    print(f"  年化 {state['best_ann']*100:.1f}% | 夏普 {state['best_sharpe']:.2f} | 回撤 {state['best_max_dd']*100:.1f}%")
    print(f"最优参数: {state['best']}")


if __name__ == "__main__":
    main()
