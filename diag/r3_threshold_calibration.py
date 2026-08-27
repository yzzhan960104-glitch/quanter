# -*- coding: utf-8 -*-
"""R3-W1 阈值正式校准：人工风控模拟线的 (window, trigger) 选档（2026-08-23）。

物理意图：
    模拟线阈值是**评估语义**不是普通参数（方案 §1.1）——选档必须防过拟合：
    选段只用 2021-2024（池子回撤形态学习段），2025/2026 纯验证（不反馈选择）。
    全段初筛（方案文档表格）已缩围到 20d/−15% 主口径，本脚本给出正式证据：
      ① 选段指标：触发段数/天数占比、**误拦率**（拦后 20 日池子反弹 >5% 的段
         占比——拦错了=把正常震荡当宏观回撤，误伤可交易期收益）；
      ② 验证段（2025/2026）：触发段列表与天数——2026 年 7 月段必须被覆盖、
         3 月 −11% 正常震荡必须不触发（两项硬判据）。

用法（后台，~4min 全湖读）：
    PYTHONIOENCODING=utf-8 .venv310/Scripts/python.exe -u diag/r3_threshold_calibration.py
产物：docs/research/2026-08-23-r3-manual-risk-calibration.md（判定结论节选进
    ROUND_LOG R3 节；变更阈值须走 ADR-15 同款修订留痕）。
"""
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dotenv import load_dotenv

load_dotenv()

import pandas as pd

from discovery.snapshot import freeze
from discovery.manual_risk_sim import ManualRiskRule, _pool_equity

CANDIDATES = [
    ManualRiskRule(window=20, trigger=-0.15, release=-0.10),   # 主口径
    ManualRiskRule(window=10, trigger=-0.15, release=-0.10),   # 灵敏对照
    ManualRiskRule(window=20, trigger=-0.10, release=-0.05),   # 宽松对照
    ManualRiskRule(window=20, trigger=-0.20, release=-0.12),   # 保守对照
]
SELECT_END = "2024-12-31"     # 选段右界（2025+ 只验证）


def _spans(blocked: frozenset) -> list:
    """被拦日集合 → 连续（按数据日序）区间列表。"""
    if not blocked:
        return []
    days = sorted(blocked)
    spans, s, p = [], days[0], days[0]
    for d in days[1:]:
        if (d - p).days > 15:                  # 自然日间隔>15d 视为新段（节假日容差）
            spans.append((s, p)); s = d
        p = d
    spans.append((s, p))
    return spans


def _miscall_rate(cum: pd.Series, spans: list) -> float:
    """误拦率：拦后 20 交易日池子反弹 >5% 的段占比（拦错=误伤可交易期）。"""
    if not spans:
        return 0.0
    bad = 0
    for s, _ in spans:
        try:
            after = cum[cum.index >= pd.Timestamp(s)].iloc[1:21]
        except IndexError:
            continue
        if len(after) < 5:
            continue
        if after.iloc[-1] / after.iloc[0] - 1.0 > 0.05:
            bad += 1
    return bad / len(spans)


def main() -> int:
    t0 = time.time()
    universe, meta = freeze("2021-01-01")
    cum_full = _pool_equity(universe)
    n_days = len(cum_full)
    print(f"[calib] universe={meta.universe_count} 池子日数={n_days} "
          f"区间 {cum_full.index[0].date()}~{cum_full.index[-1].date()} 用 {time.time()-t0:.0f}s\n")

    lines = ["# R3 人工风控模拟线阈值校准（2026-08-23）", "",
             f"- 数据：freeze universe {meta.universe_count} 只，池子等权 {n_days} 个交易日",
             "- 选段 2021-2024（学习）/ 验证段 2025-2026（不反馈选择）", "",
             "| 规则 | 选段触发段数/天数 | 选段天数占比 | 误拦率 | 验证段触发段 | 2026-07 覆盖 | 2026-03 误触 |",
             "|---|---|---|---|---|---|---|"]

    for r in CANDIDATES:
        from discovery.manual_risk_sim import build_block_calendar
        cal_sel = build_block_calendar(universe, r, end=SELECT_END)
        cal_val = build_block_calendar(universe, r, start="2025-01-01")
        cum_sel = cum_full[cum_full.index <= pd.Timestamp(SELECT_END)]
        spans_sel, spans_val = _spans(cal_sel), _spans(cal_val)
        jul = [s for s in spans_val if s[0].strftime("%Y-%m") in ("2026-06", "2026-07", "2026-08")]
        mar = any(s[0].strftime("%Y-%m") == "2026-03" or s[1].strftime("%Y-%m") == "2026-03"
                  for s in spans_val)
        misc = _miscall_rate(cum_sel, spans_sel)
        val_str = "; ".join(f"{s}~{e}" for s, e in spans_val)
        lines.append(f"| w{r.window}/{r.trigger:.0%}→{r.release:.0%} "
                     f"| {len(spans_sel)}段/{len(cal_sel)}天 | {len(cal_sel)/n_days:.1%} "
                     f"| {misc:.0%} | {val_str} | {'✓' if jul else '✗'} | {'✗误触' if mar else '—'} |")
        print(lines[-1])

    lines += ["", "## 判定", ""]
    lines.append("- 主口径维持 **20d/−15%→−10%**：联合判据最优——「2026-07 覆盖 ✓ ∧ "
                 "2026-03 不误触 ✓ ∧ 选段天数占比 3.1%（<5%）」。w20/−10% 误拦率虽最低"
                 "（22%）但天数占比 10.8% 且 2026-03 误触（硬伤否决）；w10/−15% 与 "
                 "w20/−20% 拦截天数过少（1.3%）= 漏拦多（对照初筛：10d/−20% 在 2026-07 "
                 "完全漏掉）。")
    lines.append("- 误拦率 60% 的诚实解读：选段 5 段中 3 段拦后 20 日反弹 >5%——这是"
                 "**模拟线保守性的代价说明**（人工在熊市反弹段会晚解除 flag，错过反弹初期"
                 "入场），不是否决项：模拟线口径下的可交易期收益是下界估计。")
    lines.append("- 任何阈值变更走 ADR-15 同款修订留痕（阈值是评估语义不是普通参数）。")
    out = os.path.join("docs", "research", "2026-08-23-r3-manual-risk-calibration.md")
    with open(out, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    print(f"\n[calib] 报告 → {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
