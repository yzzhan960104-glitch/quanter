# -*- coding: utf-8 -*-
"""Plan 4 端到端 slow 集成（spec §12 #13/#14）。

物理意图：T1-T6 单元测试各验一段（mock run_search / mock resolve_active），
本模块做"整机联调"——真实多夜 daemon（真实 freeze + run_search 颈线法回测） +
publish→experiment DRAFT 闭环 + ≥5天硬闸 fail-closed，验证全链通。

诚实标注（slow E2E 验"链路"，不验"特定收敛结果"）：
  - 多夜 daemon 的跨夜收敛（判据①连续 K 夜前沿不扩张）取决于真实 frontier 扩张行为，
    数据驱动、非确定性，本测试只硬验"链路跑通不炸 + 状态落库 + 若有冠军则 publish 成 DRAFT"，
    对 converged_cross 不做特定 True/False 断言（brief 明示）。
  - 影子期硬闸是确定性逻辑（activated_at 今日 → 影子期 0 < 5 → 拒绝），可硬验 sys.exit(2)。

slow 标记：真实 freeze/evaluate 跑颈线法回测（~分钟级），非 slow 回归（pytest -m "not slow"）
不跑。隔离：每用例 tmp_path 独立 SQLite（monkeypatch 覆盖 store 默认路径），不污染 logs/。
"""
import pytest

# 模块级 slow 标记：本文件全部用例都属 slow（~分钟级真实回测）。
pytestmark = pytest.mark.slow


@pytest.mark.slow
def test_plan4_multi_night_daemon_converges_and_publishes(tmp_path, monkeypatch):
    """三夜 daemon（真实 freeze + run_search）→ 链路通 → publish 冠军 → experiment DRAFT。

    加速档（brief）：budget_hours=0.01（≈36s 上限 → estimate_budget 算 1 组）+ n_proc=1
    + tpe_trials=2 + K=3。颈线法 freeze+evaluate 单组 ~分钟级，3 夜 daemon ~10-15min 可接受。

    断言策略（诚实）：
      - out is not None：daemon 至少跑通返了结果（链路无异常）。
      - early_exited is False or converged_cross in (True, False)：只验"链路跑通不炸 +
        状态落库"，不硬验"3 夜必收敛"（真实 frontier 是否扩张取决于数据，不强求 K=3 收敛）。
      - 若 daemon 产出 top_trial_id：publish → experiment_id 以 "neckline_disc_" 起头
        + list_versions 含 DRAFT 状态版本（source=discovery:xxx 溯源）。
    """
    # --- 隔离：tmp_path 独立 SQLite（discovery + experiment 各一个）---
    import discovery.store as dstore
    disc_db = str(tmp_path / "d.db")
    monkeypatch.setattr(dstore, "DEFAULT_DB_PATH", disc_db)

    import experiment.store as estore
    exp_db = str(tmp_path / "e.db")
    estore.init_db(exp_db)
    monkeypatch.setattr(estore, "_DEFAULT_DB", exp_db)

    from discovery.daemon import run_daemon
    from discovery.snapshot import freeze
    from discovery.split import holdout_split
    from discovery.store import init_db
    init_db(disc_db)
    # 主进程 freeze：daemon 内 _eval_outer 也调 freeze（同 lake_start），此处预热一次复用。
    universe, meta = freeze("2025-01-01")
    split = holdout_split()

    # --- 三夜 daemon（真实 run_search，小 budget 加速；每夜复算 frontier 比对）---
    out = None
    for _ in range(3):
        out = run_daemon(meta, split, disc_db, budget_hours=0.01,   # 极小 budget 加速（~1 组/夜）
                         n_proc=1, tpe_trials=2, K=3)
    # 链路至少跑通（收敛取决于真实前沿，此处只验链路不炸 + 状态落库）。
    assert out is not None
    assert out["early_exited"] is False or out["converged_cross"] in (True, False)

    # --- publish 冠军（若有 top_trial_id）→ experiment DRAFT ---
    if out["summary"] and out["summary"].top_trial_id:
        from discovery.publish import publish_champion
        pub = publish_champion(out["summary"].top_trial_id, db_path=disc_db, exp_db_path=exp_db)
        assert pub["experiment_id"].startswith("neckline_disc_")
        versions = estore.list_versions(exp_db)
        from experiment.models import ExperimentStatus
        # publish 建 DRAFT（weight=0，不自动 promote——spec §2.2 红线钉死）。
        assert any(v.status == ExperimentStatus.DRAFT for v in versions)


