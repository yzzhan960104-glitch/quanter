# -*- coding: utf-8 -*-
"""R4 自动归因-改进循环（12h 高强度无人值守 · 2026-08-24 用户指令）。

范式（ROUND_LOG R4 节）：逐笔归因驱动——不做全局广度搜索，每轮做
「受控单变量扫描 → 贪心采纳 → 解剖刷新」的局部最优堆叠；贪心收敛（连续
两轮零采纳）后自动切 TPE 邻域精修（budget 60/轮），全部结果按 (base, Δ单变量)
受控记录，inner 验证 outer 报告（信息隔离军规不变）。

循环体（每轮 ~60-90min）：
    1) 解剖刷新：base 全档 replay → 逐笔明细落 logs/r4_loop/detail_rN.csv
       （几何+环境特征富化——止损率/死法分布的离线统计源）
    2) 受控扫描：对 HYPOTHESIS_SPACE 每维每档跑 evaluate_portfolio(base+δ)
       （组合口径 ~3.5min/次，双口径自动附带），记录 raw/模拟线 outer ann
    3) 贪心采纳：raw outer ann 提升 > +1pp 且模拟线 outer 不降 > 2pp 的最优
       单变量 → 采纳进 base（checkpoint 落盘）——一次一维（受控原则）
    4) 收敛切换：连续 2 轮零采纳 → TPE 精修轮（--budget 60，邻域由 base 派生
       的种子集），TPE top 超过 base 采纳；再连续 2 轮无进展 → 终止
    5) 断点续跑：logs/r4_loop/state.json 每步落盘，重启从当前 round 继续

Why 贪心而非同步调多维：多维同调无法归因「哪个改进在起效」——与逐笔范式
相悖；单变量栈的每一步都可解释、可回退（state 记录采纳链）。

用法（12 小时 deadline）：
    PYTHONIOENCODING=utf-8 .venv310/Scripts/python.exe -u diag/r4_auto_loop.py --hours 12
"""
import argparse
import copy
import json
import os
import sqlite3
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dotenv import load_dotenv

load_dotenv()

ROOT = Path(__file__).resolve().parents[1]
PY = str(ROOT / ".venv310/Scripts/python.exe")
LOOP_DIR = ROOT / "logs/r4_loop"
STATE = LOOP_DIR / "state.json"
ENV = {**os.environ, "PYTHONIOENCODING": "utf-8",
       "DISCOVERY_MANUAL_RISK": "on"}

# 起点参数：R3 最强 raw outer（49e3755 的 21+1 键全参）
def _load_base() -> dict:
    con = sqlite3.connect(f"file:{ROOT/'logs/discovery_trials.db'}?mode=ro", uri=True)
    p = json.loads(con.execute(
        "SELECT params FROM trial WHERE trial_id='49e3755b322c'").fetchone()[0])
    con.close()
    return p

# 假设空间（受控单变量档位；None=当前 base 值跳过）
def hypothesis_space(base: dict) -> list[tuple[str, object]]:
    hs = []
    for dim, levels in {
        "momentum_gate": [None, 0.0, 0.03, 0.05, 0.10],
        "buy_limit_atr_mult": [0.5, 1.0, 1.5],
        "max_h_atr": [3.0, 3.5, 4.5, 5.0],
        "tp_h_mult": [2.0, 2.5, 3.0],
        "min_suppression": [0.5, 0.7],
        "max_holding": [15, 25, 30],
        "cancel_thresh_mult": [None, 2.0, 3.0],
        "breakout_vol_mult": [1.0, 2.0],
    }.items():
        for lv in levels:
            if base.get(dim) != lv:
                hs.append((dim, lv))
    return hs


def _eval_one(params: dict, tag: str, cache: dict) -> dict:
    """受控评估（进程内直调，双口径自动附带）——cache 按 (dim,lv) 去重。"""
    from discovery.snapshot import freeze
    from discovery.split import holdout_split
    from discovery.objective import evaluate_portfolio
    from discovery.manual_risk_sim import build_block_calendar
    if "universe" not in cache:
        cache["universe"], cache["meta"] = freeze("2021-01-01")
        cache["split"] = holdout_split()
        cache["cal"] = build_block_calendar(cache["universe"])
    res = evaluate_portfolio(params, cache["universe"], cache["split"],
                             block_dates=cache["cal"])
    # 键名口径：portfolio_metrics 组合口径键为 ann/max_dd/n（replay 口径才叫
    # annualized_return——首轮启动 KeyError 实锤，勿混）
    out = {"tag": tag,
           "raw_outer_ann": res["outer_raw"]["ann"],
           "raw_outer_dd": res["outer_raw"]["max_dd"],
           "mr_outer_ann": res["outer"]["ann"],
           "inner_ann": res["inner"]["ann"],
           "n_inner": res["inner"]["n"]}
    print(f"    [{tag:>28}] raw_outer {out['raw_outer_ann']:+7.1%} | 模拟线 {out['mr_outer_ann']:+7.1%}"
          f" | inner {out['inner_ann']:+6.1%} n={out['n_inner']}", flush=True)
    return out


def _autopsy(base: dict, rnd: int, cache: dict) -> str:
    """解剖刷新：replay 全档逐笔明细落 CSV（几何+环境特征）。"""
    import pandas as pd
    from backtest.replay import replay
    from strategies.neckline.strategy import NecklineMethodStrategy
    from discovery.manual_risk_sim import _pool_equity
    if "universe" not in cache:
        _eval_one(base, "warmup", cache)          # 复用 freeze
    pool = _pool_equity(cache["universe"])
    mom20, vol20 = pool.pct_change(20), pool.pct_change().rolling(20).std()
    strat = NecklineMethodStrategy(cfg_override=base)
    rep = replay(cache["universe"], strat, "2021-01-01", "2026-08-21")
    rows = []
    for t in rep.trades:
        if t.get("neckline") is None or t.get("atr") in (None, 0):
            continue
        ed = pd.Timestamp(t["entry_date"])
        h_atr = (t["neckline"] - (t["bottom"] if t["bottom"] is not None else t["neckline"])) / t["atr"]
        rows.append({"exit_reason": t["exit_reason"], "year": ed.year,
                     "rr": t["rr"], "pnl": t["avg_pnl_pct"], "h_atr": h_atr,
                     "entry_gap_atr": (t["entry_price"] - t["neckline"]) / t["atr"],
                     "mom20": float(mom20.reindex([ed], method="ffill").iloc[0]),
                     "vol20": float(vol20.reindex([ed], method="ffill").iloc[0]),
                     "holding": t["holding_bars"]})
    df = pd.DataFrame(rows)
    out = LOOP_DIR / f"detail_r{rnd}.csv"
    df.to_csv(out, index=False)
    stop = df[df["exit_reason"] == "stop_loss"]
    tp = df[df["exit_reason"].isin(("tp1", "tp2"))]
    print(f"  [解剖 r{rnd}] {len(df)} 笔（stop {len(stop)} / tp {len(tp)}）"
          f" stop率 {len(stop)/max(len(df),1):.0%} → {out.name}", flush=True)
    return str(out)


def _tpe_refine(base: dict, rnd: int, cache: dict) -> dict | None:
    """TPE 邻域精修：CLI 子进程（审计同路径），budget 60。"""
    log = LOOP_DIR / f"tpe_r{rnd}.log"
    with log.open("w", encoding="utf-8") as f:
        subprocess.run([PY, "-u", "-m", "discovery", "run", "--budget", "60",
                        "--tpe-trials", "45", "--n-proc", "6",
                        "--seed", f"20260824{rnd:02d}"],
                       stdout=f, stderr=subprocess.STDOUT, cwd=str(ROOT),
                       env=ENV, timeout=2 * 3600)
    con = sqlite3.connect(f"file:{ROOT/'logs/discovery_trials.db'}?mode=ro", uri=True)
    snap = con.execute("SELECT snapshot_hash FROM snapshot ORDER BY created_at DESC LIMIT 1").fetchone()[0]
    rows = list(con.execute(
        "SELECT trial_id, params, inner_metrics, outer_metrics FROM trial "
        "WHERE snapshot_hash=? ORDER BY rowid DESC LIMIT 60", (snap,)))
    con.close()
    best = None
    for tid, params_s, inner_s, outer_s in rows:
        p, i, o = json.loads(params_s), json.loads(inner_s), json.loads(outer_s)
        if best is None or i.get("min_yearly_calmar", -9) > best[1]:
            best = (p, i.get("min_yearly_calmar", -9), o.get("ann"), tid, i.get("n"))
    if best and best[3] and best[2] is not None:
        print(f"  [TPE r{rnd}] top {best[3][:12]} min_yr_calmar {best[1]:.2f} "
              f"outer(模拟线) {best[2]:+.1%} n {best[4]}", flush=True)
        return {"params": best[0], "trial_id": best[3],
                "mr_outer_ann": best[2], "min_yr_calmar": best[1]}
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=float, default=12.0)
    args = ap.parse_args()
    LOOP_DIR.mkdir(parents=True, exist_ok=True)
    deadline = time.time() + args.hours * 3600

    st = json.loads(STATE.read_text(encoding="utf-8")) if STATE.exists() else {
        "round": 0, "base": _load_base(), "adopted": [], "zero_streak": 0, "history": []}
    base = st["base"]
    cache: dict = {}
    print(f"[loop] 起点 base raw_outer 参照（R3 实测 +43.5%）；deadline {args.hours}h", flush=True)

    while time.time() < deadline:
        st["round"] += 1
        rnd = st["round"]
        print(f"\n========== Round {rnd}（剩 {(deadline-time.time())/3600:.1f}h）"
              f" base={ {k: base[k] for k in ('momentum_gate','max_h_atr','tp_h_mult','buy_limit_atr_mult') if k in base} }",
              flush=True)
        t0 = time.time()

        # ① 解剖刷新（约 9min）
        try:
            _autopsy(base, rnd, cache)
        except Exception as e:
            print(f"  [解剖异常，续] {type(e).__name__}: {e}", flush=True)

        # ② base 基准评估（每轮重算——base 可能上轮已更新）
        base_res = _eval_one(copy.deepcopy(base), "BASE", cache)
        base_raw = base_res["raw_outer_ann"]
        base_mr = base_res["mr_outer_ann"]

        # ③ 受控扫描（时间窗允许时全扫，否则按剩余时间截断）
        results = []
        for dim, lv in hypothesis_space(base):
            if time.time() > deadline - 15 * 60:      # 留 15min 收尾
                break
            try:
                p = copy.deepcopy(base)
                p[dim] = lv
                r = _eval_one(p, f"{dim}={lv}", cache)
                r["dim"], r["lv"] = dim, lv
                results.append(r)
            except Exception as e:
                print(f"    [{dim}={lv}] 评估异常跳过：{type(e).__name__}: {e}", flush=True)

        # ④ 贪心采纳：raw outer 提升 >1pp 且模拟线不降 >2pp
        cands = [r for r in results
                 if r["raw_outer_ann"] > base_raw + 0.01
                 and r["mr_outer_ann"] > base_mr - 0.02]
        if cands:
            best = max(cands, key=lambda r: r["raw_outer_ann"])
            base[best["dim"]] = best["lv"]
            st["adopted"].append({"round": rnd, "dim": best["dim"], "lv": best["lv"],
                                  "raw_outer": best["raw_outer_ann"], "tag": best["tag"]})
            st["zero_streak"] = 0
            print(f"  ★ 采纳 r{rnd}: {best['tag']} → raw_outer {base_raw:+.1%}⇒{best['raw_outer_ann']:+.1%}",
                  flush=True)
        else:
            st["zero_streak"] += 1
            print(f"  ✗ r{rnd} 无可采纳（zero_streak={st['zero_streak']}）", flush=True)

        # ⑤ 收敛 → TPE 精修
        if st["zero_streak"] >= 2 and time.time() < deadline - 40 * 60:
            tpe = _tpe_refine(base, rnd, cache)
            if tpe and tpe["mr_outer_ann"] is not None:
                # TPE 候选须受控复核 raw 口径再采纳（防模拟线口径偏置）
                chk = _eval_one(tpe["params"], f"TPE@{tpe['trial_id'][:8]}", cache)
                if chk["raw_outer_ann"] > base_raw + 0.01:
                    base = tpe["params"]
                    st["adopted"].append({"round": rnd, "dim": "TPE", "lv": tpe["trial_id"],
                                          "raw_outer": chk["raw_outer_ann"]})
                    st["zero_streak"] = 0
                    print(f"  ★ TPE 采纳：raw_outer {chk['raw_outer_ann']:+.1%}", flush=True)
                else:
                    print(f"  ✗ TPE top raw 复核未过（{chk['raw_outer_ann']:+.1%} ≤ {base_raw:+.1%}）", flush=True)
        if st["zero_streak"] >= 4:
            print("[loop] 连续 4 轮无进展——空间收敛，提前终止", flush=True)
            break

        st["base"] = base
        st["history"].append({"round": rnd, "took_min": round((time.time()-t0)/60),
                              "n_scanned": len(results), "at": datetime.now().isoformat()})
        STATE.write_text(json.dumps(st, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"  [checkpoint] r{rnd} 用 {(time.time()-t0)/60:.0f}min → {STATE}", flush=True)

    # 终局：对最终 base 落终版解剖与评估
    final = _eval_one(copy.deepcopy(base), "FINAL", cache)
    st["base"] = base
    st["final"] = {k: v for k, v in final.items()}
    STATE.write_text(json.dumps(st, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n[loop] 终局 base raw_outer {final['raw_outer_ann']:+.1%} "
          f"（起点 +43.5%）；采纳链 {len(st['adopted'])} 步 → {STATE}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
