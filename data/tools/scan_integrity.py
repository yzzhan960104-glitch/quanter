# -*- coding: utf-8 -*-
"""数据完整性全市场扫描 CLI（规则 1）。

物理意图：扫 a_shares_daily 全市场各标的的 date 序列，找出「应有却缺失」的交易日段，
区分合法跳空（停牌）vs 漏采（数据缺口），输出漏采标的清单 + JSON 报告。

数据流：读 daily + suspend_d → fetch_trade_days（trade_cal 基准）→ load_suspend_intervals
（停牌区间）→ find_gaps（连续性扫描）→ 报告 dict（unjustified = 漏采标的）。

用法：
    python -m data.tools.scan_integrity                       # 全市场全历史扫描
    python -m data.tools.scan_integrity --symbol 300214.SZ    # 单标的诊断
    python -m data.tools.scan_integrity --since 2026-07-01 --end 2026-07-24
    python -m data.tools.scan_integrity --report logs/integrity.json

退出码：0=无漏采（PASS）；1=有漏采（FAIL，可作 CI gate / 触发补采）。

依赖：data.integrity 三件套（load_suspend_intervals / fetch_trade_days / find_gaps），
本脚本只做「读文件 + 组装 + 输出」接线，核心算法在 integrity.py（已单测）。
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path

# 加项目根到 sys.path：脚本可从任意 cwd 直接 `python -m data.tools.scan_integrity` 运行
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import pandas as pd  # noqa: E402

from data.integrity import (  # noqa: E402
    fetch_trade_days, find_gaps, load_suspend_intervals, GapRange,
)

logger = logging.getLogger(__name__)


def scan(
    lake_dir: str = "data_lake",
    symbol: str | None = None,
    since: str | None = None,
    end: str | None = None,
) -> dict:
    """扫描数据完整性，返回报告 dict。

    Args:
        lake_dir: 数据湖目录（默认 data_lake；测试注入 tmp_path）。
        symbol: 仅扫描该标的（None=全市场）。
        since/end: 扫描区间（YYYY-MM-DD）；缺省 = lake 的 [date_min, date_max] 全期。

    Returns:
        {scan_range, total_symbols, total_gaps, unjustified_gaps,
         unjustified_symbols, gaps: [GapRange as dict]}。
    """
    lake = Path(lake_dir)
    daily_path = lake / "a_shares_daily.parquet"
    suspend_path = lake / "suspend_d.parquet"

    df = pd.read_parquet(daily_path)
    # suspend 容错：缺失/空 → 空 df（无停牌记录，所有缺口都是漏采）
    if suspend_path.exists():
        susp_df = pd.read_parquet(suspend_path)
    else:
        logger.warning("suspend_d.parquet 缺失，所有缺口将判为漏采（无停牌 ground-truth）")
        susp_df = pd.DataFrame(
            {"suspend_type": []},
            index=pd.MultiIndex.from_arrays([[], []], names=["date", "symbol"]),
        )

    # 扫描区间：显式 since/end 优先，否则用 lake 全期 [date_min, date_max]
    if since and end:
        td_min, td_max = since, end
    else:
        td_min = str(pd.Timestamp(df.index.get_level_values("date").min()).date())
        td_max = str(pd.Timestamp(df.index.get_level_values("date").max()).date())

    trade_days = fetch_trade_days(td_min, td_max)
    susp_intervals = load_suspend_intervals(susp_df, trade_days)
    gaps = find_gaps(df, trade_days, susp_intervals)
    if symbol:
        gaps = [g for g in gaps if g.symbol == symbol]

    unjustified = [g for g in gaps if not g.suspend_justified]
    return {
        "scan_range": [td_min, td_max],
        "total_symbols": int(df.index.get_level_values("symbol").nunique()),
        "total_gaps": len(gaps),
        "unjustified_gaps": len(unjustified),
        "unjustified_symbols": sorted({g.symbol for g in unjustified}),
        "gaps": [_gap_to_dict(g) for g in gaps],
    }


def _gap_to_dict(g: GapRange) -> dict:
    """GapRange → 可 JSON 序列化的 dict。"""
    return {
        "symbol": g.symbol,
        "start": g.start,
        "end": g.end,
        "missing_dates": list(g.missing_dates),
        "suspend_justified": g.suspend_justified,
    }


def main(argv: list[str] | None = None) -> int:
    """CLI 入口。返回 0=无漏采 / 1=有漏采（可作 CI gate / 触发补采）。"""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser(description="数据完整性全市场扫描（规则 1）")
    ap.add_argument("--symbol", default=None, help="仅扫描该标的（单标的诊断）")
    ap.add_argument("--since", default=None, help="扫描起 YYYY-MM-DD（缺省=lake 首日）")
    ap.add_argument("--end", default=None, help="扫描止 YYYY-MM-DD（缺省=lake 末日）")
    ap.add_argument("--lake-dir", default="data_lake", help="数据湖目录")
    ap.add_argument("--report", default=None, help="报告 JSON 输出路径（如 logs/integrity.json）")
    ap.add_argument("--top", type=int, default=10, help="终端摘要显示 top N 漏采标的")
    args = ap.parse_args(argv)

    report = scan(args.lake_dir, symbol=args.symbol, since=args.since, end=args.end)

    # 终端摘要
    print(f"扫描区间 {report['scan_range']}，{report['total_symbols']} 标的")
    print(f"缺口段 {report['total_gaps']}（其中停牌合法跳空 "
          f"{report['total_gaps'] - report['unjustified_gaps']}，漏采 {report['unjustified_gaps']}）")
    unj_syms = report["unjustified_symbols"]
    if unj_syms:
        print(f"漏采标的 {len(unj_syms)} 只，top {args.top}：{unj_syms[:args.top]}")
    else:
        print("✅ 无漏采（所有缺口均已由停牌解释）")

    if args.report:
        out = Path(args.report)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"报告已写入 {args.report}")

    return 0 if report["unjustified_gaps"] == 0 else 1


if __name__ == "__main__":
    # stdout UTF-8 治理:防 GBK 管道崩 emoji(详见 infra/pyio.py)
    from infra.pyio import force_utf8_stdout
    force_utf8_stdout()
    sys.exit(main())
