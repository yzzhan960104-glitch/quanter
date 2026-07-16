# -*- coding: utf-8 -*-
"""每日行情播报 CLI 入口（spec §5.4 · `python -m broadcast`）。

职责：load 数据湖 → 定应播日(index_daily 最新交易日) → 幂等去重(logs/.last_broadcast)
→ build_daily_brief → push_brief → 成功才写 last_broadcast。

幂等（spec 决策 6）：同日不重发（除非 --force）；周末/节假日 index_daily 最新日不变
→ 天然跳过，零废报。比维护一张 A 股交易日历表极简得多。

降级：dws 推送失败不写 last_broadcast（下次触发重试）；dry_run 不读/写 last_broadcast。
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

import pandas as pd

from broadcast.brief import build_daily_brief
from broadcast.push import push_brief
from config import LAKE_CONFIG
from data.lake_reader import DataLakeReader

logger = logging.getLogger(__name__)

# 上次成功播报日期记录（幂等去重）；放 logs/ 与其他日志同源。
LAST_BC_FILE = Path("logs") / ".last_broadcast"


# 播报只用这 4 个湖（不全量 load：a_shares_daily 9M 行/408MB load 慢且浪费，brief 用不到）。
_BRIEF_LAKES = ("index_daily", "ths_daily", "moneyflow", "dragon_list")


def _load_reader() -> DataLakeReader:
    """仅 load 播报用到的 4 湖（复用 server/main.py:78 load 模式，但收窄到 _BRIEF_LAKES）。

    Why 不全量：LAKE_CONFIG['lakes'] 含 a_shares_daily（9M 行/408MB），播报用不到，
    load 它纯浪费内存+启动时间。parquet 缺失则 lake_reader 内部离线降级（不阻断）。
    """
    reader = DataLakeReader.get_instance()
    lakes = LAKE_CONFIG.get("lakes", {})
    for key in _BRIEF_LAKES:
        path = lakes.get(key)
        if path:
            reader.load(path, key=key)
    return reader


def _latest_trade_date(reader: DataLakeReader) -> str | None:
    """index_daily 最新交易日（YYYY-MM-DD）；湖空/无 date 层级 → None。"""
    df = reader.get_lake("index_daily")
    if df is None or getattr(df, "empty", True):
        return None
    try:
        dates = df.index.get_level_values("date")
    except Exception:
        return None
    try:
        return pd.Timestamp(dates.max()).strftime("%Y-%m-%d")
    except Exception:
        return None


def _read_last_broadcast() -> str:
    """读上次播报日期；文件不存在/损坏 → 空串（首次播报或容错）。"""
    try:
        return LAST_BC_FILE.read_text(encoding="utf-8").strip()
    except Exception:
        return ""


def _write_last_broadcast(date: str) -> None:
    """记录本次播报日期（幂等依据）。写失败仅 warning：不影响本次推送，但下次可能重复发。"""
    try:
        LAST_BC_FILE.parent.mkdir(parents=True, exist_ok=True)
        LAST_BC_FILE.write_text(date, encoding="utf-8")
    except Exception:
        logger.warning("写 last_broadcast 失败（不影响本次推送，但下次可能重复）", exc_info=True)


def main(argv: list[str] | None = None) -> int:
    """CLI 主入口。返回 0=成功/跳过，1=无法定播报日，2=推送失败。"""
    p = argparse.ArgumentParser(prog="python -m broadcast", description="每日行情播报")
    p.add_argument("--date", help="播报日 YYYY-MM-DD（缺省=index_daily 最新交易日）")
    p.add_argument("--dry-run", action="store_true", help="只打印文案不发钉钉")
    p.add_argument("--force", action="store_true", help="忽略幂等去重强制重发")
    args = p.parse_args(argv)

    reader = _load_reader()
    date = args.date or _latest_trade_date(reader)
    if date is None:
        logger.error("无法确定播报日（index_daily 未加载/为空）；用 --date 显式指定")
        return 1

    # 幂等去重：dry_run 不参与去重（随时可预览文案）；非 force 且今日已播 → 跳过。
    if not args.dry_run and not args.force and _read_last_broadcast() == date:
        print(f"今日({date})已播报，跳过（--force 可重发）")
        return 0

    brief = build_daily_brief(date, reader=reader)
    title = f"📈 每日行情播报 {date}"
    robot_code = os.getenv("DINGTALK_CHAT_ROBOT_CODE", "")
    group_id = os.getenv("BROADCAST_GROUP_ID", "")
    ok = push_brief(
        title, brief.markdown,
        robot_code=robot_code, group_id=group_id, dry_run=args.dry_run,
    )

    if args.dry_run:
        return 0
    if ok:
        _write_last_broadcast(date)
        print(f"播报已推送({date})")
        return 0
    logger.error("推送失败，未写 last_broadcast（下次触发重试）")
    return 2


if __name__ == "__main__":
    sys.exit(main())
