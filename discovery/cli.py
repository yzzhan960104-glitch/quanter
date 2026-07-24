# -*- coding: utf-8 -*-
"""命令行：python -m discovery {oos,verify}。

oos：对当前 param_iter 冠军（logs/param_iter_state.json 的 best）跑 2025/2026 holdout
嵌套评估，固化其 2026 去偏水平（L1 验收锚），落 SQLite。解决探查实证的快照漂移。

物理意图：探查脚本 probe_champion_oos.py 因 data_lake 增量 + 流动性边界票浮动，两次跑
universe 漂 6%——"连复现自己都做不到"。本 cli 把"冠军 2026 去偏水平"固化成一个带快照
指纹 + 引擎指纹的 trial 记录，后续内核/数据变了老 trial 自动标 stale（engine_hash/
snapshot_hash 双指纹），给 L1 验收一个不漂移的锚。
"""
import argparse
import hashlib
import json
import os

from discovery.snapshot import freeze
from discovery.split import holdout_split
from discovery.objective import evaluate
from discovery.store import (init_db, connect, write_snapshot, write_trial,
                             trial_id_of, DEFAULT_DB_PATH)
from discovery.judging import feasibility_gate

STATE_FILE = "logs/param_iter_state.json"


def _db_path():
    """环境变量 DISCOVERY_DB 覆盖 DEFAULT_DB_PATH（测试隔离用，避免污染 logs 库）。"""
    return os.environ.get("DISCOVERY_DB", DEFAULT_DB_PATH)


def _engine_hash():
    """回测内核代码 hash（backtest.py+method_v0.py 内容 sha256[:12]）。

    物理意图：内核改了老 trial 的指标就 stale 了——engine_hash 作内核指纹，与
    snapshot_hash 双指纹共同标识"可复现试验"。内核（scan_symbol/risk_metrics）一动，
    engine_hash 变，老 trial 自然与新跑不可比（spec §3.2 engine_hash）。
    """
    from strategies.neckline import backtest, method_v0
    h = hashlib.sha256()
    for f in (backtest.__file__, method_v0.__file__):
        with open(f, "rb") as fh:
            h.update(fh.read())
    return h.hexdigest()[:12]


def cmd_oos(args):
    """当前冠军 2026 去偏评估 + 落库。

    串起 freeze→holdout_split→evaluate→judging→store 全链。冠军从 param_iter_state.json
    的 best 读（与探查脚本同源），跑一次全历史 scan，分 inner(2025)/outer(2026) 两段报指标。
    outer 不反馈任何选择（冠军已由 param_iter 用 2025+2026 全段 score 选出——诚实标注见下）。
    """
    universe, meta = freeze()
    split = holdout_split(args.embargo)
    with open(STATE_FILE, encoding="utf-8") as f:
        state = json.load(f)
    params = state["best"]
    print(f"=== discovery oos：当前冠军 2026 去偏（snapshot={meta.snapshot_hash}）===")
    print(f"snapshot: {meta.universe_count} 只 | {meta.date_range} | hash {meta.snapshot_hash}")
    res = evaluate(params, universe, split)
    _print_segment("inner 2025", res["inner"])
    _print_segment("★outer 2026", res["outer"])
    print(f"L0 可行域闸(inner): {'通过' if feasibility_gate(res['inner']) else '不通过'} "
          f"(熊市 ann≥0 在 Plan 1 N/A——2025-2026 无熊市数据)")
    print(f"诚实标注: 2026 非纯 OOS（冠军用 2025+2026 全段 score 选出，2026 参与了选择）；"
          f"夏普/ann 是 risk_metrics 复利放大产物，绝对值非实盘预期")
    db = _db_path()
    init_db(db)
    eng = _engine_hash()
    tid = trial_id_of(params, meta.snapshot_hash)
    with connect(db) as conn:
        write_snapshot(conn, meta)
        write_trial(conn, tid, params, meta.snapshot_hash, eng, "holdout_2025_2026",
                    res["inner"], res["outer"], "manual_champion")
    print(f"落库: db={db} trial_id={tid} engine_hash={eng}")


def _print_segment(name, m):
    """打印一段指标（ann/calmar/夏普/回撤/笔数对齐，供人眼快速判读）。"""
    print(f"{name:>12}: ann {m['ann']*100:>6.1f}%  calmar {m['calmar']:>5.2f}  "
          f"夏普{m['sharpe']:>5.2f}  回撤{m['max_dd']*100:>5.1f}%  {m['n']:>5}笔")


def cmd_verify(args):
    """邻域稳定性 + 基线对照验收闸（spec §12 ⑤⑥）。

    物理意图：oos 固化了冠军的 2026 去偏水平（L1 验收锚），但"一组参数出高 calmar"
    可能是过拟合尖峰——本命令在冠军 21 维邻域 ±perturb 扰动采样，看 outer calmar 是否
    稳健（高原放行）还是塌掉（孤峰否决）。叠加基线对照（state best_ann 全段 vs outer 2026）
    给人审一个"去偏幅度"的直觉（spec §1.4 漂移实证）。
    Plan 1 手动验收：判定结果打印不进排序（排序留 Plan 3 搜索）。
    """
    from discovery.neighborhood import neighborhood_stability
    universe, meta = freeze()
    split = holdout_split(args.embargo)
    with open(STATE_FILE, encoding="utf-8") as f:
        state = json.load(f)
    params = state["best"]
    print(f"=== discovery verify：邻域稳定性 + 基线对照（snapshot={meta.snapshot_hash}）===")
    stab = neighborhood_stability(params, universe, split,
                                  perturb=args.perturb, n_samples=args.n_samples)
    print(f"冠军 outer: calmar={stab['base_calmar']:.2f} ann={stab['base_outer']['ann']*100:.1f}% "
          f"夏普{stab['base_outer']['sharpe']:.2f} 回撤{stab['base_outer']['max_dd']*100:.1f}%")
    print(f"邻域 {args.n_samples}×±{args.perturb:.0%} 扰动 calmar: "
          f"mean={stab['neighbor_mean']:.2f} std={stab['std']:.2f}")
    verdict = "是（高原稳健，放行）" if stab["is_plateau"] else "否（孤峰，spec §12⑥ 否决——冠军是过拟合尖峰）"
    print(f"邻域稳定性判定: {verdict}")
    print(f"基线对照: state best_ann(全段)={state.get('best_ann', 0)*100:.1f}% "
          f"vs outer 2026 ann={stab['base_outer']['ann']*100:.1f}% "
          f"（去偏幅度见 spec §1.4）")


def cmd_run(args):
    """并发搜索跑批：采样→Pool→落库（spec §5.1，Plan 2 L2+L3 基础）。

    串起 freeze→holdout_split→run_search（内含 sample_search→eval_batch→store）。
    主进程 freeze 一次拿 SnapshotMeta（snapshot_hash 依赖真实 universe_count/date_range，
    必须真 freeze 一次）；universe 由子进程 initializer 各自 freeze 复用（不随 params pickle）。
    """
    from discovery.runner import run_search
    universe, meta = freeze(args.lake_start)
    split = holdout_split(args.embargo)
    print(f"=== discovery run：并发搜索（snapshot={meta.snapshot_hash}）===")
    print(f"snapshot: {meta.universe_count} 只 | {meta.date_range} | hash {meta.snapshot_hash}")
    print(f"配置: budget={args.budget} sobol={args.n_sobol} random={args.n_random} "
          f"proc={args.n_proc} seed={args.seed} embargo={args.embargo}")
    summary = run_search(meta, split, budget=args.budget, n_sobol=args.n_sobol,
                         n_random=args.n_random, seed=args.seed, db_path=_db_path(),
                         n_proc=args.n_proc, lake_start=args.lake_start)
    print(f"--- RunSummary ---")
    print(f"n_sampled={summary.n_sampled} n_evaluated={summary.n_evaluated} "
          f"n_new_trials={summary.n_new_trials} n_skipped_dup={summary.n_skipped_dup} "
          f"n_failed={summary.n_failed}")
    print(f"top_inner_calmar={summary.top_inner_calmar:.2f} "
          f"top_trial_id={summary.top_trial_id} status={summary.status}")
    print(f"db={summary.db_path}")
    print(f"信息隔离: 汇总只用 inner calmar（搜索不反馈 outer，spec §6.2）；"
          f"Plan 2 无收敛判据（Pareto/覆盖度④留 Plan 3）")


def main(argv=None):
    """cli 入口：子命令派发。argv=None 走 sys.argv（python -m discovery {oos,verify}）。"""
    ap = argparse.ArgumentParser(prog="discovery")
    sub = ap.add_subparsers(dest="cmd", required=True)
    ap_oos = sub.add_parser("oos", help="当前冠军 2026 去偏评估（L1 验收锚）")
    ap_oos.add_argument("--embargo", type=int, default=5, help="inner→outer embargo 天数")
    ap_oos.set_defaults(func=cmd_oos)
    ap_v = sub.add_parser("verify", help="邻域稳定性 + 基线对照验收闸")
    ap_v.add_argument("--embargo", type=int, default=5)
    ap_v.add_argument("--perturb", type=float, default=0.15, help="邻域扰动幅度（默认 15%%）")
    ap_v.add_argument("--n-samples", type=int, default=5, dest="n_samples")
    ap_v.set_defaults(func=cmd_verify)
    ap_r = sub.add_parser("run", help="并发搜索跑批（L2+L3 基础，Plan 2）")
    ap_r.add_argument("--budget", type=int, default=10, help="采样目标上限（最多跑 N 组新 trial）")
    ap_r.add_argument("--n-sobol", type=int, default=5, dest="n_sobol", help="Sobol 初始覆盖组数")
    ap_r.add_argument("--n-random", type=int, default=5, dest="n_random", help="random 补充组数")
    ap_r.add_argument("--embargo", type=int, default=5, help="inner→outer embargo 天数")
    ap_r.add_argument("--n-proc", type=int, default=None, dest="n_proc",
                     help="进程数（默认核数-2）")
    ap_r.add_argument("--seed", type=int, default=42, help="采样种子（可复现）")
    ap_r.add_argument("--lake-start", type=str, default="2025-01-01", dest="lake_start",
                     help="universe 加载起始日")
    ap_r.set_defaults(func=cmd_run)
    args = ap.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
