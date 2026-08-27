# -*- coding: utf-8 -*-
"""R6-1 识别/执行解耦缓存：提速实测 + tp1 渐近等价确认（2026-08-26）。

Phase A（提速命题）：diag/r6a_rescan.py 同款场景（freeze 2021 起贪心栈 base），
单进程依次评估 base + 3 个 exec-only 变体——首次全量识别（~2.5min 量级），后续
应秒级（段 1 三键缓存命中，识别占 >95% 被跳过）。cancel_thresh_mult 变体单独
入组：证 cancel 守卫移到段 2 重放后，该 exec 维同样命中缓存。

Phase B（R6-PhaseA 遗留假设）：tp1_portion=0 vs tp1_h_mult=5.0——两者语义皆
「关闭一档减仓、全仓吃 tp2」（前者 lot1 权重 0，后者 tp1 价位远于 tp2 永不
触发），读数应逐位一致（逐笔 avg_pnl/exit_reason 与 inner/outer/yearly 全指标
级；trades 的 tp1 价位字段不同属预期，不参与比较）。

产物：logs/r6_1_cache.json（gitignored）。
"""
import json
import os
import sys
import time
from copy import deepcopy

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main():
    from discovery.snapshot import freeze
    from discovery.split import holdout_split
    from discovery.objective import evaluate_portfolio, run_full_scan
    from strategies.neckline import backtest as bk

    base = json.loads(open("logs/r4_loop/state.json", encoding="utf-8").read())["base"]
    t0 = time.time()
    universe, meta = freeze("2021-01-01")
    split = holdout_split()
    print(f"[init] freeze {len(universe)} syms snapshot={meta.snapshot_hash} "
          f"data={meta.data_hash} in {time.time()-t0:.0f}s", flush=True)

    # —— Phase A：base + 3 个 exec-only 变体（同 id_cfg → 段 1 缓存复用）——
    variants = [
        ("BASE(冷)", deepcopy(base)),
        ("max_holding=15", {**deepcopy(base), "max_holding": 15}),
        ("tp1_portion=0.9+cooldown=8",
         {**deepcopy(base), "tp1_portion": 0.9, "cooldown": 8}),
        ("cancel=2.0+bl=2.5",
         {**deepcopy(base), "cancel_thresh_mult": 2.0, "buy_limit_atr_mult": 2.5}),
    ]
    timings = []
    for tag, p in variants:
        s0 = bk._scan_id_cache_state()
        t1 = time.time()
        r = evaluate_portfolio(p, universe, split)
        dt = time.time() - t1
        s1 = bk._scan_id_cache_state()
        row = {"tag": tag, "sec": round(dt, 1),
               "miss": s1["miss"] - s0["miss"], "hit": s1["hit"] - s0["hit"],
               "outer_ann": r["outer"]["ann"], "inner_ann": r["inner"]["ann"],
               "n_total": r["n_total"], "cache_events": s1["events"]}
        timings.append(row)
        print(f"[A] {tag:>24} {dt:7.1f}s  miss={row['miss']:>4} hit={row['hit']:>4} "
              f"outer_ann={row['outer_ann']:+.1%} n={row['n_total']}", flush=True)
    cold_sec = timings[0]["sec"]
    warm_desc = ", ".join("%s:%ss" % (t["tag"], t["sec"]) for t in timings[1:])
    print(f"[A] 冷跑 {cold_sec:.1f}s；exec-only 变体 {warm_desc}", flush=True)

    # —— Phase B：tp1 渐近等价（tp1_portion=0 vs tp1_h_mult=5.0，皆 exec 键 → 命中缓存）——
    pa = {**deepcopy(base), "tp1_portion": 0.0}
    pb = {**deepcopy(base), "tp1_h_mult": 5.0}
    fa = run_full_scan(pa, universe)
    fb = run_full_scan(pb, universe)

    def _key(r):
        return (r["symbol"], str(r["signal_date"]), r["exit_reason"], r["avg_pnl_pct"])

    ka, kb = [_key(r) for r in fa], [_key(r) for r in fb]
    trades_equal = ka == kb
    n_diff = sum(1 for x, y in zip(ka, kb) if x != y) + abs(len(ka) - len(kb))
    ra = evaluate_portfolio(pa, universe, split)
    rb = evaluate_portfolio(pb, universe, split)
    metrics_equal = ra == rb
    print(f"[B] 逐笔（symbol/date/reason/avg_pnl）逐位一致: {trades_equal}"
          f"（n={len(ka)}/{len(kb)}，diff={n_diff}）", flush=True)
    print(f"[B] inner/outer/yearly 全指标逐位一致: {metrics_equal}", flush=True)
    if not metrics_equal:
        for seg in ("inner", "outer"):
            for k in ra[seg]:
                if ra[seg][k] != rb[seg].get(k):
                    print(f"    [diff] {seg}.{k}: {ra[seg][k]!r} vs {rb[seg].get(k)!r}",
                          flush=True)
        print(f"    [diff] yearly: {ra['inner']['yearly_calmar']} vs "
              f"{rb['inner']['yearly_calmar']}", flush=True)
    if not trades_equal:
        for x, y in list(zip(ka, kb))[:5]:
            if x != y:
                print(f"    [trade-diff] {x} vs {y}", flush=True)

    out = {"base_snapshot": meta.snapshot_hash, "data_hash": meta.data_hash,
           "phase_a": timings, "cold_sec": cold_sec,
           "phase_b": {"trades_equal": trades_equal, "n_trades": [len(ka), len(kb)],
                        "n_diff": n_diff, "metrics_equal": metrics_equal,
                        "a_outer_ann": ra["outer"]["ann"], "b_outer_ann": rb["outer"]["ann"]}}
    json.dump(out, open("logs/r6_1_cache.json", "w", encoding="utf-8"),
              ensure_ascii=False, indent=1, default=str)
    print("[done] logs/r6_1_cache.json", flush=True)


if __name__ == "__main__":
    main()
