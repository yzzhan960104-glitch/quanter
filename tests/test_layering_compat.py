# -*- coding: utf-8 -*-
"""后端分层重构兼容性契约测试（Step 1/2/3 贯穿）。

物理意图（strangler 红线守护）：
    本文件是「只动结构不动逻辑」的安全网——每个 Task 移动文件/re-export 后，
    此处断言「旧 import 路径仍可用 + 新 import 路径已可用 + 关键符号可访问」。
    全量 pytest 绿 + 本文件绿 = 结构重构未破坏任何既有契约。

设计纪律：只做 import 与符号存在的断言，不做业务行为断言（行为由既有 86 个测试守护）。
"""
from __future__ import annotations


# ============================================================================
# Step 1 契约：config 包拆分后，所有旧顶层名仍可 from config import
# ============================================================================
def test_config_package_reexports_legacy_names():
    """config.py(857行) 拆为 config/ 包后，from config import X 零改动可用。"""
    from config import (  # noqa: F401
        DATA_SOURCE_CREDENTIALS, MARKET_HOURS, DATA_CONFIG, MACRO_CONFIG,
        VIZ_CONFIG, MOCK_TRADING_CONFIG, LAKE_CONFIG, MACRO_CLIENT_CONFIG,
        CELERY_CONFIG, JQDATA_CONFIG, AKSHARE_CONFIG,
        DATASET_REGISTRY, TUSHARE_DATASETS, SYNCING_DIR, get_credential,
    )
    # LAKE_CONFIG 跨段拼接正确性：base 键 + 追加键都在
    assert "default_path" in LAKE_CONFIG          # base（@113-117）
    assert "lakes" in LAKE_CONFIG and "default_lake" in LAKE_CONFIG  # 追加（@171-230）
    assert LAKE_CONFIG["default_lake"] == "daily"


def test_config_credentials_dotenv_loaded():
    """dotenv 副作用随包入口执行——DATA_SOURCE_CREDENTIALS 结构完整（值可为空但键在）。"""
    from config import DATA_SOURCE_CREDENTIALS
    assert "fred" in DATA_SOURCE_CREDENTIALS and "tushare" in DATA_SOURCE_CREDENTIALS


# ============================================================================
# Step 1 契约：core/indicator → factors/atr 后，新旧路径并存
# ============================================================================
def test_factor_atr_legacy_and_new_path():
    """core.indicator.atr 迁至 factors.atr，两条 import 路径都可用且同一对象。"""
    from core.indicator import atr as atr_legacy
    from factors.atr import atr as atr_new
    from factors import atr as atr_pkg  # 包级 re-export
    assert atr_legacy is atr_new is atr_pkg


# ============================================================================
# Step 1 契约：core/notifier → infra/notifier 后，新旧路径并存且符号同源
# ============================================================================
def test_notifier_legacy_and_new_path():
    """core.notifier 迁至 infra.notifier，关键符号新旧路径同源。"""
    from core.notifier import NotificationManager, fire_and_forget
    from infra.notifier import NotificationManager as NM2, fire_and_forget as ff2
    assert NotificationManager is NM2
    assert fire_and_forget is ff2


# ============================================================================
# Step 3a 契约：caisen 子包可 import（Task 1.3 后瘦身态）
# ============================================================================
# 注：原 test_caisen_subpackages_scaffold（断言 caisen.engines StrategyConfig/PatternScreener
# + caisen.config + caisen.patterns.screener 可 import）随 Task 1.3「caisen 形态退役」删——
# StrategyConfig/PatternScreener/caisen.config/caisen.patterns 已全删（真身 + 垫片）。
# Task 1.3 后 caisen 包仅余 optimize（参数训练）+ infra（replay_*/viz_* 垫片）+ advisor +
# 空壳 engines。此处的子包可 import 契约改由 test_shim_identity_tripwire 间接覆盖。
# 授权：caisen-retire-inventory §3.10 line182 + §8 裁决（#1/#2/#3/#10/#11 全删）。


# ============================================================================
# 垫片同源绊线（final whole-branch review Rec#5）
# ============================================================================
# 物理意图：所有迁移模块的「旧路径顶层垫片」与「新路径子包真身」必须为同一模块对象。
# 防 Step4（及后续）迁移漏 caisen/__init__.py 预加载行 —— 一旦漏掉，from caisen import X
# 会绑定到垫片壳子（而非真实模块），sys.modules 别名失效，monkeypatch 模块全局的测试
# 会静默假绿（Task3.3/3.4 已踩此坑两次）。此绊线在 CI 层兜底，未来迁移漏预加载立即红。
#
# Task 1.3（caisen 形态退役）评估：
#   全量 grep 显示 caisen 顶层垫片（plan/risk/config/patterns/）随真身删除一并删除
#   （无消费者）；存活的 optimize training_* / infra replay_*+viz_* / backtest_replay 垫片
#   仍有活跃消费者（training_loop/server/api/tests），strangler 红线「不强切消费者」→ 保留。
#   本绊线 cases 表移除 plan/risk/config/patterns/storage/execution（已删），保留存活垫片。
def test_shim_identity_tripwire():
    """迁移模块新旧路径必须同一对象（sys.modules 别名生效的前置断言）。

    Task 1.3：cases 表瘦身——caisen 形态相关垫片（plan/risk/config/patterns/storage/
    execution）已随真身删除，不再列入。仅保留 optimize training_* + infra replay_*/viz_*
    + backtest_replay 等存活垫片的同源断言。
    """
    import importlib

    # (旧路径顶层垫片, 新路径子包真身)
    cases = [
        # optimize 参数优化（Task 1.3：保留，颈线法经 training_loop 用）
        ("caisen.training_analyzer", "caisen.optimize.training_analyzer"),
        ("caisen.training_loops_db", "caisen.optimize.training_loops_db"),
        ("caisen.training_loop", "caisen.optimize.training_loop"),
        ("caisen.training_dingtalk", "caisen.optimize.training_dingtalk"),
        # infra 垫片（Task 1.3：颈线法异步回测基础设施保留）
        ("caisen.backtest_replay", "caisen.infra.backtest_replay"),
        ("caisen.replay_runs", "caisen.infra.replay_runs"),
        ("caisen.replay_tasks_db", "caisen.infra.replay_tasks_db"),
    ]
    # viz_static/viz_interactive/replay_scheduler/replay_worker 未列入（重型/反向依赖 import），
    # 其同源由专项测试间接覆盖；新增迁移须补入此表。
    # Task 1.3：plan/risk/config/patterns/storage/execution 垫片已删，不在表中。
    for old, new in cases:
        m_old = importlib.import_module(old)
        m_new = importlib.import_module(new)
        assert m_old is m_new, (
            f"垫片同源绊线失败: {old} is not {new} —— "
            f"检查 caisen/__init__.py 是否有为该模块的预加载行缺失或失效"
        )
