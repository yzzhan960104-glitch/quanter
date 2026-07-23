# -*- coding: utf-8 -*-
"""数据实时性检查核心。

物理意图：现状 data bot 只看 parquet mtime 新不新鲜（被动），会被「刚重写但内容是旧数据」
骗过。本模块改为「比对交易日历期望日 vs 数据湖内容最新日」——真正回答「T/T-1 数据到没到」。

边界（Grill Me）：
- 绝不猜价/猜日：parquet 缺失或读失败 → FAIL + 告警，不静默返 PASS。
- 大文件 read_parquet 开销：每日检查点只跑 1-2 次，单次 ~1.75s（455MB）可接受；
  不复用内存湖（DataLakeReader 可能未载入该 key）。
"""
from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path
import logging

logger = logging.getLogger(__name__)

# registry key → parquet 文件名映射（与 config/registry.py 的 lake_key 口径一致）
# 物理意图：registry 用语义 key（daily），落湖用文件名（a_shares_daily），此处对齐。
_KEY_TO_PARQUET = {
    "daily": "a_shares_daily.parquet",
    "moneyflow": "moneyflow.parquet",
    "margin": "margin.parquet",
    # 按需扩展：颈线法核心依赖以 daily 为主，其余检查点②按需追加
}


@dataclass(frozen=True)
class FreshnessResult:
    """实时性检查结果（不可变，便于聚合与断言）。"""
    key: str
    ok: bool                       # True=数据够新；False=缺失/陈旧
    latest_date: str | None        # 数据湖内容最新日（YYYY-MM-DD）；缺失则 None
    expected_date: str             # 期望日（比对基准）
    message: str                   # 人类可读结论（含告警/排查信息）


def check_freshness(
    key: str,
    expected_date: str,
    *,
    lake_dir: str = "data_lake",
) -> FreshnessResult:
    """检查某数据集最新日期是否 >= 期望交易日。

    Args:
        key:           registry 语义 key（如 "daily"），非 parquet 文件名。
        expected_date: 期望最新交易日（YYYY-MM-DD，来自 expected_latest_trade_day）。
        lake_dir:      数据湖目录（默认 data_lake；测试注入 tmp_path）。

    Returns:
        FreshnessResult：ok=True 当且仅当 latest_date >= expected_date。
    """
    fname = _KEY_TO_PARQUET.get(key, f"{key}.parquet")
    path = Path(lake_dir) / fname
    if not path.exists():
        msg = f"{key}({fname}) 缺失：{path} 不存在，期望 {expected_date} 数据未落湖"
        logger.warning(msg)
        return FreshnessResult(key, ok=False, latest_date=None,
                               expected_date=expected_date, message=msg)

    # 读最新日期：直接 read_parquet 取 date index max（检查点低频，开销可接受）
    try:
        import pandas as pd
        df = pd.read_parquet(path)
        idx = df.index
        # MultiIndex(date, symbol) 或 DatetimeIndex
        if isinstance(idx, pd.MultiIndex) and "date" in idx.names:
            dates = idx.get_level_values("date")
        else:
            dates = idx
        latest = str(pd.Timestamp(dates.max()).date())
    except Exception as exc:
        msg = f"{key} 读最新日期异常：{exc}（parquet 损坏？）"
        logger.exception(msg)
        return FreshnessResult(key, ok=False, latest_date=None,
                               expected_date=expected_date, message=msg)

    if latest >= expected_date:
        return FreshnessResult(key, ok=True, latest_date=latest,
                               expected_date=expected_date,
                               message=f"{key} 最新 {latest} >= 期望 {expected_date}，PASS")
    msg = (f"{key} 数据陈旧：最新 {latest} < 期望 {expected_date}，"
           f"T 日数据未落湖（检查 Tushare 增量采集是否成功）")
    logger.warning(msg)
    return FreshnessResult(key, ok=False, latest_date=latest,
                           expected_date=expected_date, message=msg)
