# -*- coding: utf-8 -*-
"""R6-7 supp04+tp1=2.0 晋级七门评估（2026-08-26，用户指令「跑一下」）。

流程（autopromote 同源，**只评估不 promote**）：
  1. 物化 DRAFT 锚（research.proposals._create_experiment_draft，幂等，
     NecklineConfig 全键物化——R1 partial 入库地雷防线）；
  2. research.autopromote.evaluate_gates（基线=ACTIVE 默认解析）：
     G1-G3 replay 口径×模拟线（G1 样本量 / G2 outer 改善 / G3 风险不劣化）
     + G4-G6 scan 口径（G4 wf 四折 oos calmar 全 ≥0 / G5 邻域高原 /
     G6 DSR≥0.8）+ G7 前置常量；kelly_hat=保守下三分位。

产物：logs/r6_7_promote_gates.json。
"""
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))


def main():
    from research.proposals import _create_experiment_draft
    from research.autopromote import evaluate_gates

    base = json.loads(open("logs/r4_loop/state.json", encoding="utf-8").read())["base"]
    params = {**base, "min_suppression": 0.4, "tp1_h_mult": 2.0}
    exp_id = _create_experiment_draft(params, source="r6_6_supp04_tp12")
    print(f"[draft] {exp_id} 已物化（幂等）", flush=True)

    t0 = time.time()
    res = evaluate_gates(exp_id)
    print(f"\n=== 七门评估（vs 基线 {res['baseline_id']}，用 {(time.time()-t0)/60:.0f}min）===",
          flush=True)
    for k, g in res["gates"].items():
        if k == "_meta":
            continue
        mark = "✓" if g["pass"] else "✗"
        detail = {kk: vv for kk, vv in g.items() if kk != "pass"}
        print(f"  [{mark}] {k}: {detail}", flush=True)
    print(f"\nall_pass = {res['all_pass']} | kelly_hat(保守下三分位) = {res['kelly_hat']:.4f}",
          flush=True)
    if not res["all_pass"]:
        print("（不过全闸——按 fail-closed 语义不 promote；单门读数留档供排程）", flush=True)

    json.dump(res, open("logs/r6_7_promote_gates.json", "w", encoding="utf-8"),
              ensure_ascii=False, indent=1, default=str)
    print("[done] logs/r6_7_promote_gates.json", flush=True)


if __name__ == "__main__":
    main()
