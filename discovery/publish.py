# -*- coding: utf-8 -*-
"""L5 publish：discovery 冠军 → experiment DRAFT 桥（spec §5.3/§12#14，Plan 4 T5）。

物理意图：daemon 收敛后，冠军 trial 的 params 须沉淀为 experiment 系统的 DRAFT 候选
（带 source 溯源），供人审 promote 走既有 _eod 链路。本模块是 discovery→experiment 的
薄桥：零改 experiment 系统（create_version/create_experiment_id 既有）。

关键红线（spec §2.2 非目标）：**不自动 promote**——过拟合参数若借 publish 直冲 ACTIVE，
会绕开人审直接进 _eod scan 下单。publish 只建 DRAFT(weight=0)，promote 留人审
`experiment promote <id> --weight 0.1`。这条红线在 test_publish_no_auto_promote 钉死。

outer 信息隔离（spec §6.2）：publish 内部 evaluate 一次拿 outer metrics，**只进 note + 报告**，
不反馈任何搜索/选择（与 daemon 的 _eval_outer 同源：evaluate(params, universe, split)["outer"]）。
"""
import json
from datetime import datetime

from discovery.store import DEFAULT_DB_PATH, connect


def publish_champion(trial_id, db_path=DEFAULT_DB_PATH, exp_db_path=None, *, lake_start="2025-01-01"):
    """冠军 trial → experiment DRAFT + outer 去偏报告。

    Args:
      trial_id: 冠军 trial id（daemon RunSummary.top_trial_id / champions 报的 top）。
      db_path: discovery trial 库（默认 logs/discovery_trials.db）。
      exp_db_path: experiment 库（默认 experiment.store._DEFAULT_DB）。
      lake_start: universe 加载起始日（outer 评估用，默认 2025-01-01 与 daemon 同源）。
    Returns: {"experiment_id","outer","trial_id","snapshot_hash"}。
      outer=None 表示评估软降级（数据缺失不阻断 publish 桥）。

    幂等性：experiment_id 含 trial_id[:6] + 日期，同 trial 同日重复 publish 会撞
    UNIQUE(strategy_name, version) → create_version 抛 ValueError（调用方感知重复）。
    """
    from experiment.store import create_version, _DEFAULT_DB, init_db as init_exp_db
    from experiment.models import ExperimentVersion, ExperimentStatus
    from discovery.snapshot import freeze
    from discovery.split import holdout_split
    from discovery.objective import evaluate

    exp_db = exp_db_path or _DEFAULT_DB

    # 1. 读 trial（params + snapshot_hash；source 溯源用 snapshot_hash 指纹）
    with connect(db_path) as conn:
        row = conn.execute(
            "SELECT params, snapshot_hash FROM trial WHERE trial_id=?", (trial_id,)).fetchone()
    if row is None:
        # fail-fast：未知 trial 不静默建空 DRAFT（防调用方传错 id 把 garbage 写进 experiment）
        raise ValueError(f"trial 不存在: {trial_id}")
    params = json.loads(row["params"])
    snapshot_hash = row["snapshot_hash"]

    # 2. outer 去偏报告（信息隔离：只读不回写搜索，spec §6.2）
    # 软降级：freeze/evaluate 可能因 data_lake 缺失/读取异常抛——publish 桥不依赖 outer 数值
    # 建桥（DRAFT 落库 + source 溯源才是核心），outer=None 时 note 写"评估失败"，不阻断。
    outer = None
    try:
        universe, _ = freeze(lake_start)
        split = holdout_split()
        outer = evaluate(params, universe, split)["outer"]
    except Exception:
        outer = None

    # 3. experiment create DRAFT（source 溯源 discovery:<snapshot_hash[:8]>）
    # experiment_id 含日期+trial 短码：人审 promote 时一眼可追溯来源 trial；
    # 同 trial 同日重复 publish → UNIQUE(strategy_name, version) 冲突 → 抛 ValueError（幂等保护）。
    today = datetime.now().strftime("%Y%m%d")
    experiment_id = f"neckline_disc_{today}_{trial_id[:6]}"
    if outer:
        note = (f"outer ann={outer.get('ann', 0)*100:.1f}% "
                f"calmar={outer.get('calmar', 0):.2f} "
                f"max_dd={outer.get('max_dd', 0)*100:.1f}%")
    else:
        note = "outer 评估失败（软降级）"
    version = ExperimentVersion(
        experiment_id=experiment_id, strategy_name="neckline", params=params,
        weight=0.0, status=ExperimentStatus.DRAFT, version=1,
        source=f"discovery:{snapshot_hash[:8]}", note=note,
        created_at=datetime.now().isoformat(timespec="seconds"))
    # 确保 experiment.db 建表（幂等；首次 publish 前可能从未 init，create_version 会报 no such table）
    init_exp_db(exp_db)
    create_version(exp_db, version, operator="discovery:publish")

    return {"experiment_id": experiment_id, "outer": outer,
            "trial_id": trial_id, "snapshot_hash": snapshot_hash}
