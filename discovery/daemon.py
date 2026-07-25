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
    """时间预算（小时）→ 组数上限（算力账，spec §3.6）。

    ~720s/组 × ProcessPool(n_proc) 并发。T7 slow E2E 实测：颈线法单组成本 ~12min
    （1334 标的 freeze + evaluate），原 180s 偏乐观 4 倍（已用 T7 真值标定）。
    偏高→次夜 trial_id 去重断点续跑接续（不无限跑）。n_proc 默认 CPU-2，保底线留给
    schtasks/日志/OS，避免 daemon 把机器吃满影响其他任务（spec §8 拷问②资源边界）。
    """
    import os
    n_proc = n_proc or max(1, (os.cpu_count() or 2) - 2)
    per_group_seconds = 720   # T7 slow E2E 实测单组~12min（原 180 偏乐观 4 倍）
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
    # daemon 轮次（seed 派生用）：每次夜跑换 seed，否则相同 seed+snapshot 产相同参数序列
    # → trial_id 全去重 → 无新探索（rho 永远 0，伪收敛）。run_count 上移到 run_search 前。
    run_count = (latest["daemon_run_count"] + 1 if latest else 1)
    summary = run_search_fn(
        snapshot_meta, split, budget=n_budget, n_sobol=min(5, n_budget),
        n_random=min(5, max(0, n_budget - 5)), seed=42 + run_count, db_path=db_path,
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
    daemon_run_count = run_count   # 上移到 run_search 前做 seed 派生（避免重跑去重）
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


def _eval_outer(trial_id, db_path, split, lake_start="2025-01-01"):
    """冠军 outer 去偏：读 trial params → evaluate 在 outer 段跑真实 OOS（spec §6.2）。

    物理意图：run_search 的 inner 排序天然乐观偏（holdout 调参对 inner 过拟合），
    daemon 每夜把冠军拿到 outer holdout 复核一次，给研究员一个未参与排序的诚实
    OOS 指标（用于 publish 决策 / 钉钉告警），是 inner/outer 隔离的第二道闸。

    信息隔离红线（spec §6.2）：结果只返回给调用方进返回 dict/告警，严禁回写
    run_search 排序——否则 inner 搜索会被 outer 变相污染，holdout 退化为训练集。

    软降级：trial 不存在（断点续跑残留 id）/ evaluate 抛异常（数据缺失）→ 返 None，
    daemon 主流程不阻断（spec §7——告警/outer 是旁路，不阻塞跑批编排）。

    Args:
      trial_id: 冠军 trial_id（来自 RunSummary.top_trial_id，RunSummary 语义而非
        params dict——T2 reviewer 发现 brief 早期 docstring 把它写成 (params) 是笔误，
        实际 run_daemon_cycle 传的就是 summary.top_trial_id str，与此签名对齐）。
      db_path: SQLite 路径（读 trial 表拿 params JSON）。
      split: HoldoutSplit（objective.evaluate 按 split.inner/outer 切段评估）。
      lake_start: 数据湖加载起始日（主进程 freeze 用，保证 outer 段含 2026 真实 OOS）。
    Returns:
      dict（evaluate 返回的 outer metrics，如 {"ann":..., "calmar":...}）或 None。
    """
    import json
    from discovery.store import connect
    from discovery.snapshot import freeze
    from discovery.objective import evaluate
    with connect(db_path) as conn:
        row = conn.execute("SELECT params FROM trial WHERE trial_id=?", (trial_id,)).fetchone()
    if row is None:
        return None
    params = json.loads(row["params"])
    # 主进程 freeze（outer evaluate 需要完整 universe 做 window/ATR 预热，
    # objective 再按 signal_date 切 inner/outer，不在 daemon 侧切——snapshot.freeze 范式）。
    universe, _ = freeze(lake_start)
    return evaluate(params, universe, split)["outer"]


def _notify_champion(summary, k, K, converged_cross, outer):
    """新冠军/收敛钉钉告警（fire_and_forget 不阻塞 daemon 主流程）。

    直指 infra.notifier 真身（infra.notifier 是 strangler 垫片，未来拆 core 时可能断链，
    daemon 作为 L4 生产入口必须绑死 infra 这一层防未来回归）。

    level=INFO：发现新冠军/进度属于业务流水（非风控红线），用 ℹ️ 前缀；投递失败由
    NotificationManager._broadcast 的 return_exceptions=True 软降级兜底（单通道失败仅
    记日志、不抛出），fire_and_forget 再加一层 daemon 线程隔离——双重软降级保证
    钉钉故障永不阻断 daemon 跑批编排（spec §7 拷问②接口边界）。

    Args:
      summary: RunSummary（含 snapshot_hash/run_id/top_trial_id/top_inner_calmar/rho/ei）。
      k/K: 跨夜判据①进度（连续未扩张夜数 / 阈值）。
      converged_cross: 跨夜是否已收敛（True→消息尾注"可 publish"）。
      outer: _eval_outer 返回的 outer metrics dict（可能 None，软降级场景）。
    """
    from infra.notifier import NotificationManager, fire_and_forget
    outer_ann = (outer or {}).get("ann", 0.0)
    msg = (
        f"discovery daemon: snapshot={summary.snapshot_hash} run={summary.run_id}\n"
        f"冠军 calmar={summary.top_inner_calmar:.2f} trial={summary.top_trial_id} "
        f"outer ann={outer_ann*100:.1f}% rho={summary.rho:.3f} ei={summary.ei:.4f}\n"
        f"跨夜判据①: k={k}/{K}（连续未扩张夜数）"
        f"{' → 已收敛，可 publish' if converged_cross else ' 进行中'}"
    )
    fire_and_forget(NotificationManager.get_default().notify_risk_event(msg, "INFO"))


def run_daemon(snapshot_meta, split, db_path, *, notify_fn=None, eval_outer_fn=None, **kwargs):
    """生产入口：run_daemon_cycle 预装真实 notify/outer（供 cli cmd_daemon 调）。

    ⚠️ **关键前置（生产入口必读）**：调用本函数前必须先 `build_default_manager()` 装钉钉通道
    （读 .env 的 DINGTALK_WEBHOOK/SECRET 等）。run_daemon 内部 _notify_champion 走
    NotificationManager.get_default() 单例，但首次 _channels=[]（get_default 懒构造不读 .env）
    → 告警走"无通道"软降级（仅 debug 日志，钉钉收不到，夜跑告警静默丢失）。cli cmd_daemon
    已在调本函数前显式 build_default_manager()——其他生产入口（如 cron 直调）须照此办理。

    与 run_daemon_cycle（测试用纯函数）分离的设计意图：
      - run_daemon_cycle 保持纯函数语义（notify/outer 默认 noop），测试直接注入 mock
        验注入语义，不触达真实钉钉/data_lake（spec §6.2 信息隔离可单测）。
      - 本函数绑死 _notify_champion（infra.notifier 真身）+ _eval_outer（evaluate 真身），
        cli 调本函数即得生产行为；两入口物理分离避免"测试要 mock、生产要真身"的
        全局开关坏味道（显式 > 隐式，Karpathy 极简）。

    显式签名（snapshot_meta/split/db_path 位置参数）而非 *args 透传：brief 早期
    `def run_daemon(*args, **kwargs)` 透传版有 bug——eval_outer_fn 闭包需要 split/
    db_path 来调 _eval_outer(tid, db_path, split)，但 *args 透传时 lambda 拿不到
    位置参数。显式签名把这俩提到形参，闭包自然捕获（T2 reviewer 标记的关键修正）。

    Args:
      snapshot_meta/split/db_path: 透传 run_daemon_cycle（见其 docstring）。
      notify_fn: None→绑 _notify_champion；测试/特殊场景可显式覆盖。
      eval_outer_fn: None→绑 lambda tid: _eval_outer(tid, db_path, split)；可覆盖。
      **kwargs: 其余 run_daemon_cycle 参数（budget_hours/n_proc/K/...）。
    Returns:
      run_daemon_cycle 的返回 dict。
    """
    if notify_fn is None:
        notify_fn = _notify_champion
    if eval_outer_fn is None:
        # 闭包捕获 db_path/split（显式形参，非 *args——这是本函数不用透传版的根本原因）。
        eval_outer_fn = lambda tid: _eval_outer(tid, db_path, split)
    return run_daemon_cycle(snapshot_meta, split, db_path,
                            notify_fn=notify_fn, eval_outer_fn=eval_outer_fn, **kwargs)
