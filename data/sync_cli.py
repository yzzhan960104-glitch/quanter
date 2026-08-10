# -*- coding: utf-8 -*-
"""统一 Tushare 同步 CLI：python -m data.sync [选项]

设计意图（高内聚低耦合，2026-07-25 Plan Task 9 scripts/ 收敛）：
- 把散装 data/tools/sync_*.py（sync_all_tushare/sync_incremental/sync_data_lake/sync_daily_incremental）
  的能力收敛到一个 CLI，底层复用 data.tushare_sync.sync_dataset 统一引擎。
- data/tools/sync_*.py 转薄壳 + DeprecationWarning 转调本 CLI（server data_service 仍依赖
  data/tools/sync_tushare.py 薄壳，故该脚本保留作 key 单同步入口）。

用法：
  python -m data.sync --all --since 2021-01-01                  # 全量回填所有数据集
  python -m data.sync --keys daily,weekly,cyq_chips --since 2021-01-01
  python -m data.sync --keys moneyflow --incremental             # 增量（湖最新日→今天）
  python -m data.sync --keys daily --dry-run --since 2025-07-01  # 小样例（限2标的/1日）验证字段
  python -m data.sync --quota basic --since 2021-01-01           # 仅基础桶数据集（500/min）

退出码：0=全成功，1=部分失败（fail-soft，单 key 失败不中断后续），2=无数据集可选。
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
from datetime import datetime, timedelta
from typing import Optional

import pandas as pd

from config import TUSHARE_DATASETS
from data.tushare_sync import sync_dataset

logger = logging.getLogger(__name__)


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    """argparse 参数解析（抽出便于测试）。

    --keys 逗号分隔串在解析后 split 为列表（调用方拿到 args.keys 直接是 list[str]）。
    """
    ap = argparse.ArgumentParser(description="统一 Tushare 数据集同步 CLI")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--all", action="store_true", help="同步全部 TUSHARE_DATASETS")
    g.add_argument("--keys", help="逗号分隔的数据集 key 列表（如 daily,weekly,moneyflow）")
    ap.add_argument("--since", help="起始日 YYYY-MM-DD（缺省=近5年）", default=None)
    ap.add_argument("--end", help="结束日 YYYY-MM-DD（缺省=今天）", default=None)
    ap.add_argument("--quota", choices=["basic", "special"], default=None,
                    help="仅同步指定配额桶的数据集（基础500/特色300）")
    ap.add_argument("--incremental", action="store_true",
                    help="增量模式：读湖最新日 d0 → 拉 [d0+1, today]（仅时序湖）")
    ap.add_argument("--dry-run", action="store_true",
                    help="小样例：by=symbol 限 2 标的 / by=date 限 1 日，验证字段与落湖")
    ap.add_argument("--no-resume", action="store_true", help="不断点续传（重拉已存在 shard）")
    ap.add_argument("--limit", type=int, default=None, help="by=symbol 时仅前 N 只标的")
    args = ap.parse_args(argv)
    # --keys 逗号串 → 列表（便于调用方直接迭代）
    if args.keys:
        args.keys = [k.strip() for k in args.keys.split(",") if k.strip()]
    return args


def select_keys(*, all_keys: bool, keys: Optional[list[str]],
                quota: Optional[str]) -> list[str]:
    """选 key 列表：--all 取全集（可按 --quota 过滤），--keys 用传入列表。

    排除 _unavailable（代理坑残留或 dry-run 探测不可用），防 fail。
    """
    if all_keys:
        sel = list(TUSHARE_DATASETS.keys())
    else:
        sel = list(keys or [])
    if quota:
        sel = [k for k in sel if TUSHARE_DATASETS[k].get("quota_type", "basic") == quota]
    # 过滤 _unavailable + 已退役 key（T13-A：daily 退役后 --keys daily 不应 KeyError 崩，
    # 而是静默跳过该退役 key）。k in TUSHARE_DATASETS 兜底动态访问，防传无效/退役 key。
    sel = [k for k in sel if k in TUSHARE_DATASETS
           and not TUSHARE_DATASETS[k].get("_unavailable")]
    return sel


def _resolve_window(key: str, since: Optional[str], end: Optional[str],
                    incremental: bool) -> tuple[Optional[str], Optional[str]]:
    """解析 [start, end] 窗口：incremental 读湖最新日；否则 since..end（缺省近5年..今天）。

    返回 (None, None) 表示该 key 跳过（如增量模式湖已最新）。
    """
    if end is None:
        end = datetime.today().strftime("%Y-%m-%d")
    if incremental:
        lake = TUSHARE_DATASETS[key]["lake"]
        try:
            df = pd.read_parquet(lake)
            idx_dates = df.index.get_level_values("date") if isinstance(df.index, pd.MultiIndex) else df.index
            d0 = str(pd.Timestamp(idx_dates.max()).date())
            start = (pd.Timestamp(d0) + timedelta(days=1)).strftime("%Y-%m-%d")
            if start >= end:
                logger.info("[%s] 已最新 %s，跳过增量", key, d0)
                return (None, None)
            return (start, end)
        except Exception as e:
            logger.warning("[%s] 增量读湖失败 %s，回退 since", key, e)
    start = since or (datetime.today() - timedelta(days=365 * 5)).strftime("%Y-%m-%d")
    return (start, end)


def run(keys: list[str], since: Optional[str], end: Optional[str],
        incremental: bool = False, dry_run: bool = False,
        resume: bool = True, limit: Optional[int] = None) -> int:
    """执行同步：逐 key 调 sync_dataset，fail-soft 汇总。返回 exit code（0全成功/1部分失败）。

    fail-soft：单 key 异常不中断后续（全量回填某接口失败不应拖累其他数据集），
    结尾汇总失败列表，exit code 反映是否有失败。
    """
    failures: list[tuple[str, str]] = []
    for key in keys:
        start, end_w = _resolve_window(key, since, end, incremental)
        if start is None:
            continue
        symbols = None
        cfg_by = TUSHARE_DATASETS[key].get("by")
        # dry-run / limit：仅 by=symbol 类缩到小样例（保护配额，验证字段）。
        # Why 按 cfg_by 判断而非 symbols is None：resolve_symbols 对 by=date/single（无 universe）
        # 会走默认 stock 分支返股票列表（非 None），导致旧逻辑 `if dry_run and symbols is None`
        # 失效——by=date dry-run 不限日会跑全区间烧配额。按 cfg_by 精确分流。
        if (limit or dry_run) and cfg_by == "symbol":
            from data.tushare_sync import resolve_symbols
            try:
                symbols = resolve_symbols(key, limit=limit or (2 if dry_run else None))
            except Exception:
                symbols = None
        if dry_run and cfg_by == "date":
            # by=date dry-run：限 1 日（缩 end 到 since+1天），验证字段真实性
            try:
                d1 = (pd.Timestamp(start) + timedelta(days=1)).strftime("%Y-%m-%d")
                end_w = min(end_w, d1)
            except Exception:
                pass
        # by=single dry-run：不限（一次请求小数据，如 stock_basic/hs_const 全量列表）
        t0 = time.time()
        try:
            sync_dataset(key, start, end_w, symbols=symbols, resume=resume)
            logger.info("[%s] OK elapsed=%.0fs", key, time.time() - t0)
        except Exception as e:
            logger.exception("[%s] FAIL", key)
            failures.append((key, str(e)))
    logger.info("=" * 60)
    logger.info("同步完成：成功 %d，失败 %d", len(keys) - len(failures), len(failures))
    for k, err in failures:
        logger.info("  FAIL %s: %s", k, err[:120])
    return 1 if failures else 0


def main(argv: Optional[list[str]] = None) -> int:
    """CLI 入口（python -m data.sync 调此）。返回 exit code。"""
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s [%(name)s] %(message)s")
    args = parse_args(argv)
    keys = select_keys(all_keys=args.all, keys=args.keys, quota=args.quota)
    if not keys:
        logger.error("无数据集可选（检查 --keys/--quota 或 _unavailable）")
        return 2
    logger.info("待同步 %d 数据集：%s", len(keys), keys)
    return run(keys=keys, since=args.since, end=args.end,
               incremental=args.incremental, dry_run=args.dry_run,
               resume=not args.no_resume, limit=args.limit)


if __name__ == "__main__":
    sys.exit(main())
