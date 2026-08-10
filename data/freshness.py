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

from data.integrity import existing_row_count

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
    ok: bool                       # True=数据够新；False=缺失/陈旧/行数骤降
    latest_date: str | None        # 数据湖内容最新日（YYYY-MM-DD）；缺失则 None
    expected_date: str             # 期望日（比对基准）
    message: str                   # 人类可读结论（含告警/排查信息）
    row_count: int | None = None   # 当前湖行数（骤降检测用；缺失/读失败则 None）


def check_freshness(
    key: str,
    expected_date: str,
    *,
    lake_dir: str = "data_lake",
) -> FreshnessResult:
    """检查某数据集最新日期是否 >= 期望交易日，并检测行数骤降（T13-A）。

    行数骤降（T12 防线）：即便 latest_date >= expected_date，若当前行数相对上次健康
    基线骤降（< 基线 × WRITE_GUARD_MIN_RATIO），判 FAIL + CRITICAL——封死「max-date 是
    今天但历史被抹除」的盲区。基线存 sidecar，仅健康时更新（防被掩盖）。

    Args:
        key:           registry 语义 key（如 "daily"），非 parquet 文件名。
        expected_date: 期望最新交易日（YYYY-MM-DD，来自 expected_latest_trade_day）。
        lake_dir:      数据湖目录（默认 data_lake；测试注入 tmp_path）。

    Returns:
        FreshnessResult：ok=True 当且仅当 latest_date >= expected_date **且** 行数未骤降。
    """
    fname = _KEY_TO_PARQUET.get(key, f"{key}.parquet")
    path = Path(lake_dir) / fname
    if not path.exists():
        msg = f"{key}({fname}) 缺失：{path} 不存在，期望 {expected_date} 数据未落湖"
        logger.warning(msg)
        return FreshnessResult(key, ok=False, latest_date=None,
                               expected_date=expected_date, message=msg, row_count=None)

    # 读最新日期 + 行数：date index max 取日期，pyarrow metadata 取行数（免全量 IO）
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
        row_count = existing_row_count(str(path)) or len(df)
    except Exception as exc:
        msg = f"{key} 读最新日期/行数异常：{exc}（parquet 损坏？）"
        logger.exception(msg)
        return FreshnessResult(key, ok=False, latest_date=None,
                               expected_date=expected_date, message=msg, row_count=None)

    # 行数骤降检测（sidecar 基线环比，复用 SSoT check_row_count_drop）
    crater_msg = _check_row_count_crater(key, row_count, lake_dir)

    if latest < expected_date:
        msg = (f"{key} 数据陈旧：最新 {latest} < 期望 {expected_date}，"
               f"T 日数据未落湖（检查 Tushare 增量采集是否成功）")
        logger.warning(msg)
        return FreshnessResult(key, ok=False, latest_date=latest,
                               expected_date=expected_date, message=msg,
                               row_count=row_count)
    if crater_msg:
        # max-date 合格但行数骤降：T12 式抹除，FAIL + CRITICAL
        logger.critical("%s %s", key, crater_msg)
        return FreshnessResult(key, ok=False, latest_date=latest,
                               expected_date=expected_date,
                               message=f"{key} 最新 {latest} 合格，但{crater_msg}",
                               row_count=row_count)
    # 健康：更新基线（仅健康时写，防骤降被基线掩盖）
    _update_baseline(key, row_count, lake_dir)
    return FreshnessResult(key, ok=True, latest_date=latest,
                           expected_date=expected_date,
                           message=f"{key} 最新 {latest} >= 期望 {expected_date}，PASS",
                           row_count=row_count)


def _baseline_path(lake_dir: str) -> Path:
    """sidecar 基线文件路径（与数据湖同目录，运行时状态不入库）。"""
    return Path(lake_dir) / ".freshness_baseline.json"


def _check_row_count_crater(key: str, row_count: int, lake_dir: str) -> str:
    """读 sidecar 基线，环比当前行数；骤降返中文结论，否则空串。

    物理意图：freshness 只看 max-date 会被「刚重写但内容是残片」骗过；本函数补行数维度。
    基线只在健康时更新（见 _update_baseline），骤降检查不拉低基线，防抹除被掩盖。
    无基线（首次）→ 不报骤降（返空），顺带由 _update_baseline 建基线。
    """
    import json
    from data.integrity import check_row_count_drop, WRITE_GUARD_MIN_RATIO
    bp = _baseline_path(lake_dir)
    if not bp.exists():
        return ""
    try:
        data = json.loads(bp.read_text(encoding="utf-8"))
    except Exception:
        logger.warning("freshness 基线读失败（%s），跳过骤降检测", bp, exc_info=True)
        return ""
    baseline = data.get(key, {}).get("row_count")
    if not baseline:
        return ""
    ok, reason = check_row_count_drop(baseline, row_count, WRITE_GUARD_MIN_RATIO)
    return "" if ok else reason


def _update_baseline(key: str, row_count: int, lake_dir: str) -> None:
    """健康检查后更新 sidecar 基线（仅健康调用方调用，故此处无条件写当前行数）。

    基线只在 check_freshness 判定健康（latest 合格 + 无骤降）时写入，骤降时不更新——
    否则残片覆盖会把基线拉低到残片行数，后续骤降检测失效（被掩盖）。
    """
    import json
    bp = _baseline_path(lake_dir)
    try:
        data = json.loads(bp.read_text(encoding="utf-8")) if bp.exists() else {}
    except Exception:
        data = {}
    data[key] = {"row_count": row_count}
    bp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
