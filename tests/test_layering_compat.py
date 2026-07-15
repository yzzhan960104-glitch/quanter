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
