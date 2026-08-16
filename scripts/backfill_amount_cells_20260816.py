# -*- coding: utf-8 -*-
"""一次性定向重采：I-2 amount 抹除格回填（2026-08-16 终审波 · 不留常驻入口）。

事故根因（终审 I-2，e8041041 波次实证）：
    data/tools/repair_gaps.py 旧版合并筛键是「跨符号 missing 日并集 ∩ gap_symbols」
    两维独立过滤 + OUT_COLS 漏 amount——_fetch_paged 按日拉全市场帧天然含
    (gap_symbol, 别的符号缺日) 的行（该标的该日在湖内本就有值），dedup keep=last
    让重建行顶掉湖内原行，而重建行不带 amount 列 → concat 并集后落 NaN。
    净损失：1,177 格 amount 被抹（2022-07-11~2026-08-12，39 标的）；另有 2,661 行
    OHLC 被缺口段窗口 adj 重定基（其中 473 行与 amount 抹除同格，见终审报告）。

本脚本只做 amount 定向回填（**不碰 OHLC/volume/任何价格列**——重定基残差是另案）：
    1. 受影响格 = 「湖内 amount 为 NaN」∩「git 3a476dc5（事故前一版）该格有值」的
       (trade_date, symbol) 对集——diff 结果固化为 JSON 证据件（旁路重算 git+lfs，
       保证脚本只消费可复算的静态清单）；
    2. 按标的走 data.tushare_sync._fetch_with_guard 正规限频链路重拉 daily
       （39 次调用，basic 桶 500/min，远低于 2k 调用预算）；
    3. 只把 amount 列写回这些格——湖的 amount 本就来自同一 pro.daily 响应列
       （千元口径），零换算、零前复权（amount 是成交额不是价格，不做 adj 缩放，
       见 repair_gaps OUT_COLS 语义）；
    4. safe_overwrite 原子落盘（写入前行数守卫 + tmp + fsync + os.replace，
       与 sync/repair 同一湖写纪律）。

失败面（诚实报数）：某标的拉取返空（持久态/退市）→ 该标的格子保持 NaN 如实计入
「未回填」清单；全量返空 → 零写入（湖不动，不产生无意义重写）。

用法（项目根目录）：
    .venv310/Scripts/python.exe -X utf8 scripts/backfill_amount_cells_20260816.py --dry-run
    .venv310/Scripts/python.exe -X utf8 scripts/backfill_amount_cells_20260816.py
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

# 项目根入 sys.path（脚本独立运行时 import data.* / trading.* 可达）
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd

# 默认证据件路径（diff 生成命令见 JSON 内 basis 字段说明）
_DEFAULT_PAIRS = os.path.join(".superpowers", "sdd", "2026-08-16-new-debt-wave",
                              "amount_wiped_cells.json")
LAKE = "data_lake/a_shares_daily.parquet"


def _load_pairs(path: str) -> dict[str, list[str]]:
    """读证据件 → {symbol: [trade_date(YYYYMMDD), ...]}（按标的分组便于整窗拉取）。"""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    by_sym: dict[str, list[str]] = {}
    for d, s in data["pairs"]:
        by_sym.setdefault(s, []).append(d)
    return by_sym


def main(argv: list[str] | None = None) -> int:
    import logging
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser(description="I-2 amount 抹除格定向回填（一次性）")
    ap.add_argument("--pairs", default=_DEFAULT_PAIRS, help="证据件 JSON 路径")
    ap.add_argument("--dry-run", action="store_true", help="只拉取比对，不写湖")
    args = ap.parse_args(argv)

    from data.integrity import safe_overwrite
    from data.tushare_sync import _fetch_with_guard

    by_sym = _load_pairs(args.pairs)
    total_cells = sum(len(v) for v in by_sym.values())
    print(f"证据件：{args.pairs} → {len(by_sym)} 标的 × {total_cells} 格")

    df = pd.read_parquet(LAKE)
    n_rows_before = len(df)
    print(f"湖现况：{n_rows_before} 行，amount NaN {int(df['amount'].isna().sum())} 格")

    # 逐标的拉窗重采（窗口 = 该标的受影响格的 min/max 日期，单标的 ~1000 行，
    # 单次调用可达；_fetch_with_guard 自带限频令牌 + 瞬时态退避 + 熔断冷却）
    fills: dict[tuple[str, str], float] = {}   # (trade_date, symbol) -> amount
    missing_syms: list[str] = []
    for i, (sym, days) in enumerate(sorted(by_sym.items()), 1):
        lo, hi = min(days), max(days)
        raw = _fetch_with_guard("daily", ts_code=sym, start_date=lo, end_date=hi)
        if raw is None or raw.empty:
            missing_syms.append(sym)
            print(f"[{i}/{len(by_sym)}] {sym} 拉取返空（{lo}~{hi}，{len(days)} 格）——保持 NaN 如实报数")
            continue
        m = {str(r.trade_date): float(r.amount) for r in raw.itertuples()
             if str(r.trade_date) in set(days) and r.amount == r.amount}  # r.amount==r.amount 剔 NaN
        hits = sum(1 for d in days if d in m)
        print(f"[{i}/{len(by_sym)}] {sym} {lo}~{hi}：命中 {hits}/{len(days)} 格")
        for d in days:
            if d in m:
                fills[(d, sym)] = m[d]

    print(f"\n重采命中 {len(fills)}/{total_cells} 格；返空标的 {len(missing_syms)} 个"
          f"{missing_syms if missing_syms else ''}")
    if not fills:
        print("零命中——零写入，湖不动（不产生无意义重写）")
        return 1 if total_cells else 0

    if args.dry_run:
        sample = sorted(fills.items())[:5]
        print(f"[dry-run] 不写湖。命中样例（千元口径）：{sample}")
        return 0

    # 定向赋值：只动 amount 列的目标格（目标格必在湖内——它们是「已有行的 NaN 格」，
    # 缺席即证据件与湖态漂移，硬断言拒写）
    tgt = pd.MultiIndex.from_tuples(
        [(pd.Timestamp(d), s) for (d, s) in fills], names=["date", "symbol"])
    assert tgt.isin(df.index).all(), "证据件存在湖内缺席的格子——湖态已漂移，拒写人工核"
    vals = pd.Series(list(fills.values()), index=tgt)
    df.loc[tgt, "amount"] = vals.values
    # 写入不变式：行数/索引零变化（只回填已有行的 NaN 格，绝不增删行）
    assert len(df) == n_rows_before, "行数漂移——拒写"

    safe_overwrite(LAKE, df)
    print(f"落盘完成（safe_overwrite）：{n_rows_before} 行原样，"
          f"amount NaN {int(pd.read_parquet(LAKE)['amount'].isna().sum())} 格（回填后）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
