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
    """两阶段搜索跑批：Sobol 批量→TPE 序贯→落库→收敛自停（spec §5.1/§7.2，Plan 2+3）。

    串起 freeze→holdout_split→run_search（内含 sample_search→eval_batch→store）。
    主进程 freeze 一次拿 SnapshotMeta（snapshot_hash 依赖真实 universe_count/date_range，
    必须真 freeze 一次）；universe 由子进程 initializer 各自 freeze 复用（不随 params pickle）。
    Plan 3 新增：tpe_trials/rho_threshold 透传 run_search，收敛字段（status/rho/ei/dsr/
    frontier_size/convergence_reason）从 RunSummary 打印——给 T8 slow 集成测试一个稳定 cli 入口。
    """
    from discovery.runner import run_search
    universe, meta = freeze(args.lake_start)
    split = holdout_split(args.embargo)
    print(f"=== discovery run：两阶段搜索（snapshot={meta.snapshot_hash}）===")
    print(f"snapshot: {meta.universe_count} 只 | {meta.date_range} | hash {meta.snapshot_hash}")
    print(f"配置: budget={args.budget} sobol={args.n_sobol} random={args.n_random} "
          f"tpe={args.tpe_trials} proc={args.n_proc} seed={args.seed} "
          f"embargo={args.embargo} rho_threshold={args.rho_threshold}")
    summary = run_search(meta, split, budget=args.budget, n_sobol=args.n_sobol,
                         n_random=args.n_random, seed=args.seed, db_path=_db_path(),
                         n_proc=args.n_proc, lake_start=args.lake_start,
                         tpe_trials=args.tpe_trials, rho_threshold=args.rho_threshold)
    print(f"--- RunSummary ---")
    print(f"n_sampled={summary.n_sampled} n_evaluated={summary.n_evaluated} "
          f"n_new_trials={summary.n_new_trials} n_skipped_dup={summary.n_skipped_dup} "
          f"n_failed={summary.n_failed}")
    print(f"top_inner_calmar={summary.top_inner_calmar:.2f} top_trial_id={summary.top_trial_id} "
          f"dsr_top={summary.dsr_top:.3f}")
    print(f"收敛: status={summary.status} reason={summary.convergence_reason} "
          f"rho={summary.rho:.3f} ei={summary.ei:.4f} frontier_size={summary.frontier_size}")
    print(f"db={summary.db_path}")
    print(f"信息隔离: 汇总只用 inner（spec §6.2）；判据①连续K轮 + 跨 run EI 衰减留 Plan 4 daemon")


def cmd_daemon(args):
    """L4 守护 daemon 夜跑入口（spec §5.2/§12#13，Plan 4 Task 4）。

    串 freeze→holdout_split→run_daemon（跨夜编排：判据①连续K夜 + 钉钉告警 + outer去偏）。
    每夜 schtasks @02:00 触发本命令（discovery/schtasks.py 注册，run_daemon.bat 激活venv）。

    ⚠️ 开头必须 build_default_manager() 装钉钉通道：run_daemon 内部 _notify_champion
    走 NotificationManager.get_default() 单例，但首次 _channels=[]（get_default 懒构造
    不读 .env）→ 告警走"无通道"软降级（仅 debug 日志，钉钉收不到，夜跑告警静默丢失）。
    cmd_daemon 作为生产入口必须显式装通道（读 .env 的 DINGTALK_WEBHOOK/SECRET 等），
    这是 cmd_daemon 不可推卸的关键职责（T3 reviewer Cannot-verify 标记）。
    """
    # 先装钉钉通道（读 .env）——必须在 run_daemon 调用前完成，否则告警静默丢失
    from infra.notifier import build_default_manager
    build_default_manager()
    from discovery.daemon import run_daemon, estimate_budget
    universe, meta = freeze(args.lake_start)
    split = holdout_split(args.embargo)
    print(f"=== discovery daemon：跨夜守护（snapshot={meta.snapshot_hash}）===")
    print(f"配置: budget={args.budget}h(≈{estimate_budget(args.budget, args.n_proc)}组) "
          f"tpe={args.tpe_trials} proc={args.n_proc} K={args.k_rounds} rho={args.rho_threshold}")
    out = run_daemon(meta, split, _db_path(),
                     budget_hours=args.budget, n_proc=args.n_proc, lake_start=args.lake_start,
                     tpe_trials=args.tpe_trials, rho_threshold=args.rho_threshold,
                     K=args.k_rounds, budget_groups=args.budget_groups,
                     eval_replay_top=args.eval_replay_top,
                     auto_publish_fn=_auto_publish)
    # early_exited 短路（跨夜已收敛→不访问 out["summary"]，否则 None.attribute 炸）
    if out["early_exited"]:
        print(f"早退：跨夜已收敛（k={out['latest_k']}），不重复夜跑")
        return
    s = out["summary"]
    print(f"--- daemon 汇总 ---")
    print(f"run_id={out['run_id']} n_new={s.n_new_trials} frontier={s.frontier_size} "
          f"top_calmar={s.top_inner_calmar:.2f} rho={s.rho:.3f}")
    print(f"跨夜判据①: k={out['latest_k']}/{args.k_rounds} "
          f"{'已收敛（可 publish）' if out['converged_cross'] else '进行中'}")
    if out.get("outer"):
        o = out["outer"]
        print(f"冠军 outer 去偏: ann={o.get('ann',0)*100:.1f}% calmar={o.get('calmar',0):.2f} "
              f"max_dd={o.get('max_dd',0)*100:.1f}%")
        print(f"下一步: python -m discovery publish {s.top_trial_id}")


def _auto_publish(trial_id: str, outer: dict | None) -> str | None:
    """生产装配：新冠军 outer 优于 ACTIVE → 自动建 experiment DRAFT。

    延迟 import research.discovery_bridge（cli 是装配层，可依赖 research；
    discovery 包本体零 research 依赖，分层干净）。
    """
    from research.discovery_bridge import auto_publish_champion
    return auto_publish_champion(trial_id, outer)


def cmd_champions(args):
    """Pareto 前沿 + DSR 冠军报告（spec §3.5/§3.7，读 store 最新 snapshot 的 trial）。

    物理意图：run 跑完后，人审要从一堆 trial 里挑冠军——本命令读 store 最新 snapshot 的全部
    trial，算 Pareto 前沿（calmar×max_dd 多目标非支配）+ L1 calmar 排序 + DSR 标注（ deflate
    多重比较偏差）。信息隔离（spec §6.2）：报告只用 inner_metrics，outer 留 Plan 4 daemon 选型。
    """
    import json
    from discovery.store import connect
    from discovery.pareto import pareto_frontier
    from discovery.dsr import deflated_sharpe
    from discovery.judging import feasibility_gate
    db = _db_path()
    with connect(db) as conn:
        rows = conn.execute(
            "SELECT trial_id, snapshot_hash, inner_metrics FROM trial ORDER BY created_at DESC"
        ).fetchall()
    if not rows:
        print(f"无 trial 记录（db={db}）")
        return
    latest = rows[0]["snapshot_hash"]
    trials = [r for r in rows if r["snapshot_hash"] == latest]
    metrics = []
    for t in trials:
        try:
            metrics.append((json.loads(t["inner_metrics"]), t["trial_id"]))
        except (TypeError, ValueError):
            continue
    frontier_idxs = pareto_frontier([m for m, _ in metrics]) if metrics else []
    feasible = [(m, tid) for m, tid in metrics if feasibility_gate(m)]
    ranked = sorted(feasible, key=lambda x: x[0].get("calmar", 0.0), reverse=True)
    print(f"=== discovery champions（snapshot={latest}，{len(trials)} trial，前沿 {len(frontier_idxs)}）===")
    if not ranked:
        print("无可行域内 trial（L0 闸 max_dd≤0.4 ∧ n≥30 未过）")
        return
    for i, (m, tid) in enumerate(ranked[:args.top_n]):
        dsr = deflated_sharpe(m.get("sharpe", 0.0), n_trials=len(trials), n_obs=m.get("n", 30))
        print(f"#{i+1} {tid}: calmar={m.get('calmar', 0):.2f} ann={m.get('ann', 0)*100:.1f}% "
              f"max_dd={m.get('max_dd', 0)*100:.1f}% n={m.get('n', 0)} DSR={dsr:.3f}")


def cmd_report(args):
    """run 历史简报（snapshot/trial 计数 + 最近 snapshot，spec §12 验收）。

    物理意图：多次 run 后人审要快速看"跑了几个 snapshot、几个 trial、最近啥时候跑的"——
    本命令读 store 汇总，给一个不进排序的简报（排序留 champions）。spec §12 验收锚固化用。
    """
    from discovery.store import connect
    db = _db_path()
    with connect(db) as conn:
        n_trial = conn.execute("SELECT COUNT(*) c FROM trial").fetchone()["c"]
        n_snap = conn.execute("SELECT COUNT(*) c FROM snapshot").fetchone()["c"]
        snaps = conn.execute(
            "SELECT snapshot_hash, universe_count, date_range, created_at "
            "FROM snapshot ORDER BY created_at DESC LIMIT 5"
        ).fetchall()
    print(f"=== discovery report（db={db}）===")
    print(f"snapshot: {n_snap} | trial: {n_trial}")
    for s in snaps:
        print(f"  {s['snapshot_hash']}: {s['universe_count']}只 {s['date_range']} ({s['created_at']})")


def cmd_publish(args):
    """L5 publish：冠军 trial → experiment DRAFT + outer 去偏报告（spec §5.3，Plan 4 T5）。

    人审下一步：`experiment promote <id> --weight 0.1` → 走既有 _eod 链路。
    **不自动 promote**（spec §2.2 红线——防过拟合冠军参数直冲实盘）。
    """
    from discovery.publish import publish_champion
    out = publish_champion(args.trial_id, db_path=_db_path())
    print(f"=== discovery publish：冠军 → experiment DRAFT ===")
    print(f"experiment_id: {out['experiment_id']}（source=discovery:{out['snapshot_hash'][:8]}）")
    if out["outer"]:
        o = out["outer"]
        print(f"outer 去偏: ann={o.get('ann', 0)*100:.1f}% calmar={o.get('calmar', 0):.2f} "
              f"max_dd={o.get('max_dd', 0)*100:.1f}% n={o.get('n', 0)}")
    else:
        print("outer 去偏: 评估失败（数据缺失，已软降级）")
    print(f"下一步人审: python -m experiment promote {out['experiment_id']} --weight 0.1")


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
                     help="进程数（默认 min(核数-2,4)，DISCOVERY_N_PROC 可覆盖）")
    ap_r.add_argument("--seed", type=int, default=42, help="采样种子（可复现）")
    ap_r.add_argument("--lake-start", type=str, default="2025-01-01", dest="lake_start",
                     help="universe 加载起始日")
    ap_r.add_argument("--tpe-trials", type=int, default=0, dest="tpe_trials",
                     help="TPE 序贯 trial 数（Plan 3，0=仅 Sobol）")
    ap_r.add_argument("--rho-threshold", type=float, default=0.8, dest="rho_threshold",
                     help="覆盖度阈值 ρ（判据④前置否决，Plan 3）")
    ap_r.set_defaults(func=cmd_run)
    ap_c = sub.add_parser("champions", help="Pareto 前沿 + DSR 冠军报告（Plan 3）")
    ap_c.add_argument("--top-n", type=int, default=10, dest="top_n", help="报 top-N")
    ap_c.set_defaults(func=cmd_champions)
    ap_rp = sub.add_parser("report", help="run 历史简报（Plan 3）")
    ap_rp.set_defaults(func=cmd_report)
    # publish 子命令（Plan 4 Task 5）：daemon 收敛后冠军 → experiment DRAFT 桥
    ap_p = sub.add_parser("publish", help="L5 冠军→experiment DRAFT（Plan 4 T5）")
    ap_p.add_argument("trial_id", help="冠军 trial id（champions 报的 top / daemon top_trial_id）")
    ap_p.set_defaults(func=cmd_publish)
    # daemon 子命令（Plan 4 Task 4）：schtasks @02:00 触发 run_daemon.bat → 本命令
    ap_d = sub.add_parser("daemon", help="L4 守护 daemon 夜跑（Plan 4）")
    ap_d.add_argument("--budget", type=int, default=4, help="时间预算小时（默认 4h）")
    ap_d.add_argument("--budget-groups", type=int, default=None, dest="budget_groups",
                      help="直给本轮组数上限（24h 低功率模式每轮 1-2 组；None=按小时折算）")
    ap_d.add_argument("--no-eval-replay-top", action="store_false", dest="eval_replay_top",
                      help="关闭冠军 replay 口径复评（低功率模式每轮 1 组时复评会拖长一倍；"
                           "夜间集中跑默认开启）")
    ap_d.add_argument("--embargo", type=int, default=5, help="inner→outer embargo 天数")
    ap_d.add_argument("--n-proc", type=int, default=None, dest="n_proc",
                     help="进程数（默认 min(核数-2,4)，DISCOVERY_N_PROC 可覆盖）")
    ap_d.add_argument("--lake-start", type=str, default="2025-01-01", dest="lake_start",
                     help="universe 加载起始日")
    ap_d.add_argument("--tpe-trials", type=int, default=10, dest="tpe_trials",
                     help="TPE 序贯 trial 数（夜跑默认 10）")
    ap_d.add_argument("--rho-threshold", type=float, default=0.8, dest="rho_threshold",
                     help="覆盖度阈值 ρ（判据④前置否决）")
    ap_d.add_argument("--k-rounds", type=int, default=3, dest="k_rounds",
                     help="跨夜收敛 K（判据①连续K夜前沿不扩张）")
    ap_d.set_defaults(func=cmd_daemon)
    args = ap.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
