# -*- coding: utf-8 -*-
"""湖 amount 历史断层定向修复（2026-08-29 发现 · amihud 过滤数据面前置）。

发现经过：NECK amihud 过滤部署时 writer 报「最新好行=2026-07-09」——排查实锤
湖 amount 列存在成片断崖（close ~94% 正常而 amount 仅 2-4%）：2024-Q1 全季、
2025 多段、2026-07-03~21（07-22 起自愈）。成因=上游 pro.daily 当时返回缺
amount，增量同步原样落湖。影响面：一切 amount 依赖（amihud/wic_amount/
moneyflow 归一/本过滤器）。

修复语义（保守三守卫）：
  ① 只填 NaN：该日期湖行的 amount 为 NaN 且 API 有值 → 填；已有值/其他列/
     行数/列集一律不动；
  ② 覆盖率闸：该日期 API 返回有效 amount 数 ≥ 湖该日 NaN 格子数的 80% 才施填
     （防 API 当日也缺 → 拿残缺快照填出半截截面）；
  ③ 行数不变断言：落盘前后行数/索引逐位一致（safe_overwrite 前置校验）。

用法（仓库根，盘后/周末）：
    ./.venv310/Scripts/python.exe data/tools/repair_amount_hole.py [--dry-run]
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

import pandas as pd

from data._tushare_compat import get_pro
from data.integrity import safe_overwrite
from data.resilience import tushare_rate_limiter_basic

LAKE = "data_lake/a_shares_daily.parquet"
PAGE = 500
MIN_VALID_FRAC = 0.5      # 坏行判据：amount 有效率 < 50%
MIN_API_COVER = 0.80      # 守卫②：API 覆盖率闸


def _fetch_day(pro, trade_date: str) -> dict:
    frames, offset = [], 0
    while True:
        tushare_rate_limiter_basic.acquire(1.0)
        df = pro.daily(trade_date=trade_date, limit=PAGE, offset=offset)
        if df is None or df.empty:
            break
        frames.append(df)
        if len(df) < PAGE:
            break
        offset += PAGE
    if not frames:
        return {}
    all_ = pd.concat(frames, ignore_index=True)
    all_ = all_[all_["amount"].notna()]
    return dict(zip(all_["ts_code"].astype(str), all_["amount"].astype(float)))


def main(argv=None):
    import argparse
    from datetime import datetime
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    lake = pd.read_parquet(LAKE)
    n0 = len(lake)
    dates = lake.index.get_level_values("date").unique().sort_values()
    amt_valid = lake["amount"].notna().groupby(lake.index.get_level_values("date")).mean()
    bad = amt_valid[amt_valid < MIN_VALID_FRAC]
    bad = bad[bad.index < pd.Timestamp(datetime.now().date())]   # 不碰当日
    print(f"坏行日（amount 有效率<{MIN_VALID_FRAC:.0%}，不含当日）：{len(bad)} 天")
    if bad.empty:
        print("无需修复")
        return 0

    pro = get_pro()
    total_filled, skipped = 0, []
    for i, d in enumerate(bad.index):
        ds = d.strftime("%Y%m%d")
        day_rows = lake.loc[d]
        need = day_rows.index[day_rows["amount"].isna()]
        api = _fetch_day(pro, ds)
        cover = len(set(need) & set(api)) / len(need) if len(need) else 1.0
        if cover < MIN_API_COVER:
            skipped.append((str(d.date()), round(cover, 3)))
            continue
        fill_idx = [(d, s) for s in need if s in api]
        lake.loc[fill_idx, "amount"] = [api[s] for _, s in fill_idx]
        total_filled += len(fill_idx)
        if (i + 1) % 20 == 0:
            print(f"  {i + 1}/{len(bad)} 天，已填 {total_filled} 格", flush=True)

    print(f"合计填充 {total_filled} 个 amount 格子；跳过 {len(skipped)} 天"
          f"（API 覆盖不足）：{skipped[:8]}")
    assert len(lake) == n0, f"行数漂移 {n0}→{len(lake)}——守卫③触发，拒绝落盘"
    # 复核：坏行消减
    amt_valid2 = lake["amount"].notna().groupby(lake.index.get_level_values("date")).mean()
    still_bad = amt_valid2[amt_valid2 < MIN_VALID_FRAC]
    print(f"修复后仍坏行：{len(still_bad)} 天"
          + (f"（跳过日：{[str(d.date()) for d in still_bad.index[:8]]}）"
             if len(still_bad) else ""))
    if args.dry_run:
        print("[dry-run] 不落盘")
        return 0
    safe_overwrite(LAKE, lake)
    print(f"[done] 湖已落盘（行数 {n0} 不变）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
