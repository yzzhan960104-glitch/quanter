# -*- coding: utf-8 -*-
"""A4 实证：Kelly 收敛性——分年/分折 f* 稳定性 + 分数 Kelly 0.25× 仓位对照。

物理意图（audit spec A 波 A4 / DG-G5 定稿）：
    审计标定颈线法"4% Kelly 薄边缘"——单一整段 kelly 点估计无法回答「这个下注比例
    在不同时期是否稳定为正」。Kelly 公式 f*=(bp−q)/b 对胜率/盈亏比的时期漂移极敏
    感：若分年 kelly 符号摇摆，满仓 f* 的复利期望就是幻觉。本脚本把 f* 按
    ①自然年（2021-2026）②walk-forward 四折（train/oos 分离）分解，量化收敛性，
    并给出 DG-G5 定稿的分数 Kelly（0.25×，上限 0.5×）对应仓位区间 vs 当前固定 5%。

判据（写进报告 docs/research/2026-08-16-a4-kelly-convergence.md）：
    正 kelly 年占比 ≥ 4/6（n≥30 的年；样本不足年按"非正"计，逃考惩罚口径与
    yearly_metrics 一致）且 wf 四折 oos 段 kelly 符号一致 → 分数 Kelly 可上；
    某段 kelly 塌到 0 → 0.25× 退化到最小仓位（保守方向，可上但 hat 取保守分位）。

口径对齐：
    · kelly 单源 = kelly_metrics（strategies/neckline/backtest.py:371，f* 约束 [0,0.5]）；
    · 逐笔 pnl 来自 run_full_scan（与 discovery evaluate 同源，avg_pnl_pct 已含费率）；
    · wf 四折 = evaluate_wf（每折独立 universe 时点重建，防幸存者偏差）。

用法：
    PYTHONIOENCODING=utf-8 .venv310/Scripts/python.exe -u diag/a4_kelly_convergence.py
"""
import sys, os, time, json, sqlite3
from collections import defaultdict
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dotenv import load_dotenv
load_dotenv()

import pandas as pd
from discovery.snapshot import freeze
from discovery.objective import run_full_scan, evaluate_wf
from discovery.split import walk_forward_split
from strategies.neckline.backtest import kelly_metrics

MIN_TRADES = 30   # 年样本下限（与 yearly_metrics min_trades=30 同口径；不足年 kelly 视为不可信）

# ── 参数集：ACTIVE 基线 + 最强 DRAFT（与 A3 同一证据链）──
conn = sqlite3.connect("experiment/experiments.db")
conn.row_factory = sqlite3.Row
PARAM_SETS = {}
row = conn.execute(
    "SELECT experiment_id, params FROM experiment_version WHERE status='ACTIVE' "
    "ORDER BY weight DESC LIMIT 1").fetchone()
PARAM_SETS["ACTIVE_" + row["experiment_id"].split("_")[-1]] = json.loads(row["params"])
row = conn.execute(
    "SELECT experiment_id, params FROM experiment_version "
    "WHERE experiment_id='neckline_prop_20260816_47d350'").fetchone()
if row is not None:
    PARAM_SETS["DRAFT_47d350"] = json.loads(row["params"])

t0 = time.time()
universe, meta = freeze("2021-01-01")
print(f"[freeze] universe={meta.universe_count} hash={meta.snapshot_hash} "
      f"用 {time.time()-t0:.0f}s\n", flush=True)

out = {"per_year": {}, "wf": {}, "summary": {}}

# ── ① 分年 kelly（一次全历史 scan → 按信号自然年分块，无前视）──
for label, params in PARAM_SETS.items():
    t1 = time.time()
    all_filled = run_full_scan(params, universe)
    by_year = defaultdict(list)
    for r in all_filled:
        d = pd.to_datetime(r["signal_date"])
        by_year[d.year].append((r["avg_pnl_pct"], d))
    rows = {}
    for y in sorted(by_year):
        pnls = [p for p, _ in by_year[y]]
        dates = [d for _, d in by_year[y]]
        kelly, curve, ann = kelly_metrics(pnls, dates)
        rows[y] = {"n": len(pnls), "kelly": round(kelly, 4),
                   "eligible": len(pnls) >= MIN_TRADES}
    out["per_year"][label] = rows
    eligible = [v for v in rows.values() if v["eligible"]]
    pos_years = [v for v in eligible if v["kelly"] > 0]
    ks = [v["kelly"] for v in eligible]
    mean_k = sum(ks) / len(ks) if ks else 0.0
    std_k = (sum((k - mean_k) ** 2 for k in ks) / len(ks)) ** 0.5 if len(ks) > 1 else 0.0
    out["summary"][label] = {
        "years_total": len(rows),
        "years_eligible": len(eligible),
        "years_pos_kelly": len(pos_years),
        "pos_ratio_eligible": round(len(pos_years) / len(eligible), 3) if eligible else None,
        "kelly_mean": round(mean_k, 4), "kelly_std": round(std_k, 4),
        "kelly_cv": round(std_k / mean_k, 3) if mean_k > 0 else None,
    }
    print(f"[{label}] 分年 kelly（scan 用 {time.time()-t1:.0f}s）：", flush=True)
    for y, v in rows.items():
        mark = "" if v["eligible"] else "  ← n<30 不可信"
        print(f"    {y}: n={v['n']:>4} kelly={v['kelly']:+.3f}{mark}", flush=True)
    s = out["summary"][label]
    print(f"    → 正 kelly 年 {s['years_pos_kelly']}/{s['years_eligible']}（可信年）"
          f" 均值 {s['kelly_mean']:+.3f} std {s['kelly_std']:.3f} "
          f"cv {s['kelly_cv'] if s['kelly_cv'] is not None else '—'}\n", flush=True)

# ── ② wf 四折（仅 ACTIVE——G7 门槛对象是冠军口径；每折独立 universe）──
active_label, active_params = list(PARAM_SETS.items())[0]
t2 = time.time()
wf = evaluate_wf(active_params, walk_forward_split())
out["wf"][active_label] = wf
oos_kellys = []
print(f"[{active_label}] wf 四折（用 {time.time()-t2:.0f}s）：", flush=True)
for f in wf:
    tr, oo = f["train"], f["oos"]
    print(f"    {f['fold']}: n_sym={f['n_symbols']} "
          f"train kelly={tr['kelly']:+.3f} (n={tr['n']}) | "
          f"oos kelly={oo['kelly']:+.3f} (n={oo['n']}) ann={oo['ann']:+.1%}", flush=True)
    oos_kellys.append(oo["kelly"])
sign_consistent = all(k > 0 for k in oos_kellys) or all(k <= 0 for k in oos_kellys)
out["summary"][active_label]["wf_oos_kelly_sign_consistent"] = sign_consistent
print(f"    → oos kelly 符号一致={sign_consistent} "
      f"({'全正' if all(k > 0 for k in oos_kellys) else oos_kellys})\n", flush=True)

# ── ③ 分数 Kelly 仓位对照（DG-G5：0.25× 起步，上限 0.5× 需样本外验证）──
print("=== A4 判定 ===", flush=True)
try:
    from discovery.fingerprint import engine_hash
    print(f"engine_hash={engine_hash()}", flush=True)
except Exception:
    pass
for label, s in out["summary"].items():
    ratio = s.get("pos_ratio_eligible")
    verdict_k = (ratio is not None and ratio >= 4 / 6)
    if label == active_label:
        verdict_k = verdict_k and s.get("wf_oos_kelly_sign_consistent", False)
    ks = [v["kelly"] for v in out["per_year"][label].values() if v["eligible"]]
    if ks:
        hat_conservative = sorted(ks)[max(0, (len(ks) - 1) // 3)]   # 下三分位（保守 hat）
        frac_pos = [round(min(0.25 * k, 0.05), 4) for k in ks if k > 0]
        print(f"[{label}] 正kelly年占比={ratio} → 分数Kelly {'可上' if verdict_k else '缓上'}；"
              f"hat 区间 {min(ks):+.3f}~{max(ks):+.3f}，保守 hat(下三分位)={hat_conservative:+.3f}"
              f" → 0.25×hat 仓位 {sorted(frac_pos) if frac_pos else '—'} vs 当前固定 0.05",
              flush=True)
    else:
        print(f"[{label}] 无可信年（全部 n<{MIN_TRADES}）", flush=True)

out_path = os.path.join("logs", "a4_kelly_convergence_results.json")
with open(out_path, "w", encoding="utf-8") as f:
    json.dump(out, f, ensure_ascii=False, indent=1, default=str)
print(f"\n[done] 原始数字 → {out_path}", flush=True)
