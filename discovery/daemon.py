# -*- coding: utf-8 -*-
"""L4 守护 daemon 编排（spec §5.2 / §12#13，Plan 4 Task 2）。

物理意图：run_search（Plan 2/3）只管单夜跑批 + 单 run 收敛（判据②EI + 判据④覆盖度）；
跨夜"连续 K 夜前沿不扩张才算真收敛"（判据①）需要跨 run 状态累积——本模块是 run_search
的薄编排层：每夜 schtasks 触发 → 读上夜跨夜状态 → 调 run_search → 比对前沿 → 更新 k
→ 判据①自停 + 冠军 outer 去偏 + 告警（outer/告警注入式，T2 默认 noop，T3 接真实实现）。

两层收敛分工（化解 spec §5.2 K 轮歧义）：
  单夜内：run_search 的 ②EI<ε ∧ ④覆盖度达标（Plan 3 既有，零改）。
  跨夜：本模块的 ①连续 K 夜 frontier_size 不扩张（Plan 4 新增，状态落 search_run 表）。

纯函数可单测：run_search/notify/eval_outer 均可注入（测试 mock，不触达真实 schtasks/钉钉）。
信息隔离（spec §6.2）：eval_outer_fn 的结果只进返回 dict 供报告，严禁回写 run_search 排序。
"""
from discovery.store import init_db, connect, read_latest_search_run, write_daemon_state


def estimate_budget(budget_hours, n_proc=None):
    """时间预算（小时）→ 组数上限（算力账粗估，spec §3.6）。

    ~180s/组 × ProcessPool(n_proc) 并发。诚实标注：单组成本待 L1 replay 标定，
    偏高→次夜 trial_id 去重断点续跑接续（不无限跑）。n_proc 默认 CPU-2，保底线留给
    schtasks/日志/OS，避免 daemon 把机器吃满影响其他任务（spec §8 拷问②资源边界）。
    """
    import os
    n_proc = n_proc or max(1, (os.cpu_count() or 2) - 2)
    per_group_seconds = 180
    return max(1, int(budget_hours * 3600 / per_group_seconds) * n_proc)


def run_daemon_cycle(snapshot_meta, split, db_path, *, budget_hours=4, n_proc=None,
                     lake_start="2025-01-01", tpe_trials=0, rho_threshold=0.8, K=3,
                     run_search_fn=None, notify_fn=None, eval_outer_fn=None):
    """单夜 daemon 编排（读跨夜状态→跑批→比对前沿→更新 k→告警/outer）。

    Args:
      snapshot_meta: SnapshotMeta（冻结快照，跨夜可比性基石）。
      split: HoldoutSplit（inner/outer 切分，透传 run_search）。
      db_path: SQLite 路径（跨夜状态落 search_run 表）。
      budget_hours: 时间预算（小时），折算组数上限。
      n_proc: 并发进程数（None→CPU-2）。
      lake_start/tpe_trials/rho_threshold: 透传 run_search。
      K: 跨夜收敛阈值（连续 K 夜前沿不扩张→converged_cross）。
      run_search_fn: 注入 run_search（默认 discovery.runner.run_search）。测试 mock。
      notify_fn: 新冠军/收敛告警回调（默认 None=noop；T3 接 fire_and_forget+notify_risk_event）。
      eval_outer_fn: 冠军 outer 去偏回调（默认 None=noop；T3 接 evaluate）。签名 (trial_id)->dict。
    Returns:
      dict（run_id/summary/latest_k/converged_cross/early_exited/outer/status）。

    信息隔离（spec §6.2）：eval_outer_fn 的结果只进返回 dict 供报告，严禁回写 run_search 排序。
    """
    init_db(db_path)
    # 1. 读跨夜状态（首次=None）。read_latest_search_run 按 started_at DESC 取最新，
    #    daemon 每夜 write_search_run 落一行，最新即上夜 daemon 算完的最终状态。
    with connect(db_path) as conn:
        latest = read_latest_search_run(conn, snapshot_meta.snapshot_hash)

    # 早退：上夜已收敛 → 本夜跳过跑批（幂等，schtasks 多触发/人误触不重跑浪费算力）。
    if latest and latest.get("status") == "converged":
        return {"early_exited": True, "run_id": None, "summary": None,
                "latest_k": latest.get("k_rounds_no_expansion", 0),
                "converged_cross": True, "outer": None, "status": "converged"}

    # 2. 调 run_search（注入默认；测试 mock 替换以隔离真实跑批）。
    if run_search_fn is None:
        from discovery.runner import run_search as run_search_fn
    n_budget = estimate_budget(budget_hours, n_proc)
    summary = run_search_fn(
        snapshot_meta, split, budget=n_budget, n_sobol=min(5, n_budget),
        n_random=min(5, max(0, n_budget - 5)), seed=42, db_path=db_path,
        n_proc=n_proc, lake_start=lake_start,
        tpe_trials=tpe_trials, rho_threshold=rho_threshold)

    # 3. 跨夜判据①：比对本次 vs 上夜前沿。
    #    首次 latest=None → k=0（从 0 起算，不死板 -1 占位绕弯）。
    #    前沿扩张 → k 重置 0（找到了新 region，旧 stagnation 作废）。
    #    前沿不扩张 → k+=1（连续未扩张夜数累加，>=K 触收敛自停）。
    #    注：latest is None 与 frontier_size 扩张合并到 k=0 分支（语义等价，消除死代码）。
    if latest is None or summary.frontier_size > latest["frontier_size_prev"]:
        k = 0
    else:
        k = latest["k_rounds_no_expansion"] + 1
    converged_cross = (k >= K)

    # 4. 写回跨夜状态（UPDATE 本次 run_id 行：run_search 收尾已 write_search_run 落了初值 0，
    #    此处覆写 daemon 算完的最终 k/daemon_run_count/status，使次夜 read_latest 读到正确态）。
    status = "converged" if converged_cross else summary.status
    daemon_run_count = (latest["daemon_run_count"] + 1 if latest else 1)
    with connect(db_path) as conn:
        write_daemon_state(conn, run_id=summary.run_id, frontier_size=summary.frontier_size,
                           k_rounds_no_expansion=k, daemon_run_count=daemon_run_count,
                           status=status)

    # 5. 冠军 outer 去偏 + 告警（注入；默认 noop，T3 接真实实现）。
    #    outer 结果只进返回 dict，不回写 run_search（信息隔离，spec §6.2）。
    #    try/except 软降级：outer 数据缺失/钉钉失败不阻断 daemon 主流程（spec §7）。
    outer = None
    if eval_outer_fn is not None and summary.top_trial_id:
        try:
            outer = eval_outer_fn(summary.top_trial_id)
        except Exception:
            outer = None   # outer 软降级：数据缺失不阻断 daemon
    if notify_fn is not None:
        try:
            notify_fn(summary=summary, k=k, K=K, converged_cross=converged_cross, outer=outer)
        except Exception:
            pass           # 告警软降级：钉钉失败不阻断 daemon

    return {"early_exited": False, "run_id": summary.run_id, "summary": summary,
            "latest_k": k, "converged_cross": converged_cross, "outer": outer, "status": status}
