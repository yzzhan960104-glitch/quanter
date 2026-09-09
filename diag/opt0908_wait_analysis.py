# -*- coding: utf-8 -*-
"""wait_days(等回踩天数)判别力分析 · 固化版(2026-09-09,补 R8c 可复现性)。

背景:R8c 会话中一次性内联脚本得出「wait>3 天成交胜率 80.4% vs ≤3 天
54.8%」但未落盘——本脚本固化推导并补分界敏感性。口径注记:
  wait_days = entry_date - formed_at 的自然日数(非交易日);
  原分界 3 天来自 pd.qcut(duplicates='drop')的数据驱动切分,非预登记;
  语料 = diag/opt0908_pt_bl3p10_replay.parquet(bl3.0+portion1.0 候选,
  replay 路径六年全成交笔)。
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import pandas as pd

OUT = Path("logs/committee_20260908/r8c_wait_discrimination.json")


def main() -> None:
    tr = pd.read_parquet("diag/opt0908_pt_bl3p10_replay.parquet")
    tr["formed_at"] = pd.to_datetime(tr["formed_at"])
    tr["entry_d"] = pd.to_datetime(tr["entry_date"])
    tr["wait_days"] = (tr["entry_d"] - tr["formed_at"]).dt.days

    res = {"corpus": "diag/opt0908_pt_bl3p10_replay.parquet",
           "n": int(len(tr)), "wait_days": "自然日(entry_date - formed_at)"}

    # ① 原始发现复现:qcut 五分位(duplicates=drop 自动并档)
    q = pd.qcut(tr["wait_days"], 5, duplicates="drop")
    g = tr.groupby(q, observed=True)["avg_pnl_pct"]
    res["qcut_bins"] = {str(k): {"n": int(len(g.get_group(k))),
                                 "win": round(float((g.get_group(k) > 0).mean()) * 100, 1),
                                 "mean": round(float(g.get_group(k).mean()), 3)}
                        for k in g.groups}

    # ② 分界敏感性(1/2/3/5/8 自然日)+双窗
    inner = tr[tr.formed_at <= pd.Timestamp("2024-12-31")]
    outer = tr[tr.formed_at > pd.Timestamp("2025-01-01")]
    sens = {}
    for th in (1, 2, 3, 5, 8):
        row = {}
        for tag, d in (("全期", tr), ("inner", inner), ("outer", outer)):
            hot = d[d.wait_days > th]["avg_pnl_pct"]
            cold = d[d.wait_days <= th]["avg_pnl_pct"]
            row[tag] = {"hot_n": int(len(hot)),
                        "hot_win": round(float((hot > 0).mean()) * 100, 1),
                        "hot_mean": round(float(hot.mean()), 3),
                        "cold_win": round(float((cold > 0).mean()) * 100, 1),
                        "cold_mean": round(float(cold.mean()), 3)}
        sens[str(th)] = row
    res["threshold_sensitivity"] = sens

    # ③ wait 分布(理解两档并档的原因)
    res["wait_distribution"] = {
        "p50": float(tr.wait_days.quantile(.5)),
        "p84": float(tr.wait_days.quantile(.84)),
        "p90": float(tr.wait_days.quantile(.9)),
        "share_le3": round(float((tr.wait_days <= 3).mean()), 3)}

    OUT.write_text(json.dumps(res, ensure_ascii=False, indent=1),
                   encoding="utf-8")
    print(json.dumps(res["qcut_bins"], ensure_ascii=False))
    print(json.dumps(res["wait_distribution"], ensure_ascii=False))
    for th, row in sens.items():
        f, i, o = row["全期"], row["inner"], row["outer"]
        print(f"分界>{th}天: 全期 {f['hot_win']}%/{f['hot_mean']}%(n={f['hot_n']}) "
              f"vs 冷 {f['cold_win']}%/{f['cold_mean']} | inner {i['hot_win']} "
              f"| outer {o['hot_win']}")
    print(f"→ {OUT}")


if __name__ == "__main__":
    main()
