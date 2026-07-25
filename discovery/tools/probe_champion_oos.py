# -*- coding: utf-8 -*-
"""L1 Go/No-Go 探查：当前 param_iter 冠军的 2026 近期 OOS 去偏水平。

对应 spec docs/superpowers/specs/2026-07-23-param-discovery-engine-design.md §12 验收第2条
（最小可信版，先于 discovery 包落地）。回答整个参数发现引擎项目的 Go/No-Go 锚点：

  当前 best_ann=115.8%（state.json v3，168 组）是 2025-01 至今【含 2026】全样本内搜出来的
  ——零样本外、高度疑似多重比较过拟合。本探查把"全段"切成 2025（样本内主体）/ 2026（近期
  OOS 代理），看冠军去偏后的真实水平：
    · 2026 ann 塌到个位数/为负 → 冠军高度依赖 2025 段，过拟合坐实，倾向 No-Go（不铺长期系统）；
    · 2026 ann 仍有相当水平 → 近期未失效，可继续铺 discovery 嵌套验证。

方法论（无前视 + 复用 param_iter 同源内核，保证与原 115.8% 可比）：
  ① 全历史跑 scan_symbol：scan_symbol 内部用 sym_df.iloc[:i+1]（截至该信号的历史）做识别，
     无未来函数。故正确切分=【传完整 sym_df 跑一次，收集全部 filled 后按 signal_date 分段】，
     而非硬切 df（硬切会让段开头的 window/ATR 丢失预热历史，信号失真）。
  ② 各段独立调 risk_metrics：ann=curve**(1/years)-1，years=该段 days/365.25——年化按段时长
     自洽，可直接复用于子段（2026 半年按 0.5 年年化，与全段 1.5 年年化同口径可比）。
  ③ full 段须复现 param_iter 的 history ann（≈115.8%）——同源内核+同 universe+同 risk_metrics，
     应精确吻合，作为脚本正确性的内建锚点（漂移>1% 说明有 bug）。

诚实边界（务必在解读结果时计入）：
  · 2026 非纯 OOS：冠军用 2025+2026 全段 score 选出，2026 参与了选择。真正纯 OOS 须等
    discovery 嵌套（inner 2025 选参 / outer 2026 纯评估，objective 不碰 outer）。本探查是
    discovery 落地前的最小近似——但 2026 塌仍是有力的过拟合证据。
  · universe=load_universe() 的 2025 截面（START_DATE=2025-01-01 创板科创近30日均成交额≥1亿），
    用于 2026 是合理短外推，无幸存者偏差（避开了用 2025 截面回填 2020-2024 的问题）。
  · 2026 仅半年，ann 估计方差大；须看 n/夏普/回撤交叉印证，不唯 ann 点估计。
"""
import os
import sys
import json
import time
import argparse

# 让脚本能 import 同目录 param_iter 与上级 strategies（与 param_iter.py 同款 path 注入）
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))                            # import param_iter
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))           # import strategies

# Windows 中文终端默认 GBK，print 的中文/✓/★ 会 UnicodeEncodeError → 强制 stdout/stderr 用 utf-8
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

import pandas as pd
from strategies.neckline.method_v0 import DEFAULTS
from strategies.neckline.backtest import scan_symbol, risk_metrics, EXEC_DEFAULTS
from param_iter import load_universe, PARAM_SPACE, STATE_FILE  # 复用：universe 加载 + 21 维参数分层 + state 路径


def run_full_then_split(params, universe):
    """全历史（2025+2026）跑一组参数，按 signal_date 分段返回 {段名: [(pnl, date), ...]}。

    一次遍历 scan_symbol 收集全部 filled → 按 signal_date 归入 2025/2026/full 三段。
    不硬切 df：scan_symbol 用 sym_df.iloc[:i+1] 截至信号的历史识别，传完整 sym_df 才能保留
    window/ATR 预热；分段只发生在收集后的 signal_date 维度，无前视。与 param_iter.run_one
    的 filled 收集逐字等价（同 id_cfg/exec_cfg/window/scan_symbol 调用）。
    """
    # 显式构造识别层/执行层 cfg（与 run_one 同款，去全局 mutation）
    id_params = {k: params[k] for k, lay, _ in PARAM_SPACE if lay == "id"}
    exec_params = {k: params[k] for k, lay, _ in PARAM_SPACE if lay == "exec"}
    id_cfg = {**DEFAULTS, **id_params}
    exec_cfg = {**EXEC_DEFAULTS, **exec_params}
    window = id_cfg["window"]

    segs = {"2025": [], "2026": [], "full": []}
    for sym, sym_df in universe.items():
        try:
            filled, _n_sig, _n_skip = scan_symbol(sym_df, window, exec=exec_cfg, id_cfg=id_cfg)
        except Exception:
            continue
        for r in filled:
            d = pd.to_datetime(r["signal_date"])
            rec = (r["avg_pnl_pct"], d)
            segs["full"].append(rec)              # full = 全部 filled（应复现 run_one 的 ann）
            if d.year == 2025:
                segs["2025"].append(rec)
            elif d.year == 2026:
                segs["2026"].append(rec)
    return segs


def metrics_of(pairs):
    """[(pnl, date), ...] → risk_metrics 五指标。空段返回零值（n=0）。"""
    if not pairs:
        return dict(n=0, ann=0.0, sharpe=0.0, max_dd=0.0, kelly=0.0, curve=1.0)
    pnls = [p for p, _ in pairs]
    dates = [d for _, d in pairs]
    kelly, curve, ann, sharpe, max_dd = risk_metrics(pnls, dates)
    return dict(n=len(pnls), ann=ann, sharpe=sharpe, max_dd=max_dd, kelly=kelly, curve=curve)


def main():
    ap = argparse.ArgumentParser(description="颈线法冠军 2026 近期 OOS 去偏探查（L1 Go/No-Go 最小版）")
    ap.add_argument("--top", type=int, default=1, help="跑全段 score top-N 冠军（默认 1=best；扩到 5 看是否孤峰）")
    args = ap.parse_args()

    state = json.load(open(STATE_FILE, encoding="utf-8"))
    # 按全段 score 降序取 top-N（best 即 top1）。history 存了每组的 params + 全段 ann/sharpe/max_dd/score。
    ranked = sorted(state["history"], key=lambda x: x.get("score", 0), reverse=True)[:args.top]

    print(f"=== L1 Go/No-Go 探查：param_iter 冠军 2026 近期 OOS 去偏 ===")
    print(f"state: 已试 {len(state['tried'])} 组 | best_ann(全段记录)={state.get('best_ann', 0) * 100:.1f}% | "
          f"best_score={state.get('best_score', 0):.3f}")
    print(f"加载 universe（创板科创 2025至今，load_universe 复用）...")
    t_load = time.time()
    universe = load_universe()   # 复用 param_iter 的 universe（2025 截面，含 2026 短外推）
    print(f"universe: {len(universe)} 只（加载 {time.time() - t_load:.0f}s）\n")

    for i, h in enumerate(ranked):
        params = h["params"]
        print(f"[top{i + 1}] 全段 score={h.get('score', 0):.3f}（history 记录 ann={h.get('ann', 0) * 100:.1f}%）")
        t0 = time.time()
        segs = run_full_then_split(params, universe)
        dt = time.time() - t0

        for seg in ("full", "2025", "2026"):
            m = metrics_of(segs[seg])
            tag = "★2026(OOS代理)" if seg == "2026" else ("◇full(复现锚)" if seg == "full" else "  2025(样本内)")
            print(f"  {tag:<18} ann{m['ann'] * 100:>7.1f}%  夏普{m['sharpe']:>5.2f}  "
                  f"回撤{m['max_dd'] * 100:>6.1f}%  {m['n']:>5}笔  kelly{m['kelly'] * 100:>5.1f}%")

        # 复现校验：full 段 ann 应与 param_iter history 记录吻合（同源内核），漂移>1% 报警
        m_full = metrics_of(segs["full"])
        hist_ann = h.get("ann", 0)
        drift = abs(m_full["ann"] - hist_ann)
        flag = "✓复现(脚本可信)" if drift < 0.01 else f"⚠漂移 {drift * 100:.1f}%（脚本/universe 有差异，结果存疑）"
        print(f"  复现校验: full ann {m_full['ann'] * 100:.1f}% vs history {hist_ann * 100:.1f}% → {flag}")
        print(f"  本组耗时 {dt:.0f}s\n")

    print(f"=== 解读：看每组 ★2026 行。ann 塌到个位数/为负 → 冠军依赖 2025 段、过拟合信号 ===")
    print(f"          top-N 多组都塌 → 2026 整体行情不利颈线法；仅 best 塌 → best 是过拟合尖峰 ===")


if __name__ == "__main__":
    main()
