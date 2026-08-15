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

    N1 停牌真值（2026-08-16）升级：① 从湖自身算日级在场计数喂 find_gaps（长洞
    市场共识启发式的分母，不新增数据源）；② unjustified_gaps 语义 = 日级判定 +
    启发式 + unfillable sidecar 排除后「真需补的子段计数」（pipeline/daemon 消费
    同 key 自动对齐）；③ suspend_suspected_segments 单列计数（启发式推定停牌，
    与 suspend_d 铁证区分，可审计）。

    Args:
        lake_dir: 数据湖目录（默认 data_lake；测试注入 tmp_path）。
        symbol: 仅扫描该标的（None=全市场）。
        since/end: 扫描区间（YYYY-MM-DD）；缺省 = lake 的 [date_min, date_max] 全期。

    Returns:
        {scan_range, total_symbols, total_gaps, unjustified_gaps, unjustified_symbols,
         suspend_suspected_segments, unfillable_skipped_segments,
         gaps: [GapRange as dict]}。
    """
    lake = Path(lake_dir)
    daily_path = lake / "a_shares_daily.parquet"
    suspend_path = lake / "suspend_d.parquet"

    df = pd.read_parquet(daily_path)
    # 市场共识启发式喂入：湖日级在场计数（每交易日湖内标的数，10.3M 行 ~1.5s）。
    # Why 从湖自身算：市场是否健康的最直接证据就是湖当天有多少标的在场——
    # 不引入第二数据源（suspend_d 已被实证 2019 后长停牌稀疏），分母与被检对象
    # 同源同口径（都是「湖里有什么」）。
    _pres = df.groupby(level="date").size()
    market_presence = {pd.Timestamp(d).strftime("%Y-%m-%d"): int(c)
                       for d, c in _pres.items()}
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
    gaps = find_gaps(df, trade_days, susp_intervals, market_presence=market_presence)
    if symbol:
        gaps = [g for g in gaps if g.symbol == symbol]

    # unfillable sidecar 排除：repair 拉过「源零行」已标记的日不算漏采（盲区年代/
    # 停牌残留）。Why：不排除则 pipeline 每夜照旧 spawn repair 空转、daemon
    # fail-closed 闸永久拒跑——标记是「已勘定不可补」的持久结论，不是漏采。
    # 惰性 import：sidecar 归 repair_gaps 所有（写入侧在那里），此处只读；放函数内
    # 防任何未来的模块级循环 import。
    from data.tools.repair_gaps import unfillable_coverage, load_unfillable_entries
    cov = unfillable_coverage(load_unfillable_entries(lake_dir))

    def _covered(sym: str, day: str) -> bool:
        return any(s <= day <= e for s, e in cov.get(sym, ()))

    suspected = [g for g in gaps if g.suspend_suspected]
    unfillable_skipped = 0
    unjustified_subseg_count = 0
    unjustified_syms: set[str] = set()
    for g in gaps:
        if g.suspend_justified:
            continue
        # 日级真缺日 → sidecar 排除 → 剩余日按连续性拆子段（repair 原子单位）
        eff_days = [d for d in g.unjustified_days if not _covered(g.symbol, d)]
        if not eff_days:
            unfillable_skipped += 1  # 整段已标记不可补：不算漏采，单列计数可审计
            continue
        unjustified_syms.add(g.symbol)
        unjustified_subseg_count += len(_count_runs(g, set(eff_days)))
    return {
        "scan_range": [td_min, td_max],
        "total_symbols": int(df.index.get_level_values("symbol").nunique()),
        "total_gaps": len(gaps),
        # 消费方兼容红线：key 不变（pipeline._n_gaps / daemon.integrity_gate /
        # engine 周扫），语义升级为「真需补的子段计数」（旧语义=段级误报数）
        "unjustified_gaps": unjustified_subseg_count,
        "unjustified_symbols": sorted(unjustified_syms),
        "suspend_suspected_segments": len(suspected),
        "unfillable_skipped_segments": unfillable_skipped,
        "gaps": [_gap_to_dict(g) for g in gaps],
    }


def _count_runs(g: GapRange, eff_days: set[str]) -> list[tuple[str, ...]]:
    """段内有效真缺日的连续子段拆分（与 integrity.unjustified_subsegments 同骨架，
    但作用在 sidecar 排除后的日集上——scan 计数口径，不回写 GapRange）。"""
    from data.integrity import unjustified_subsegments
    return unjustified_subsegments(
        GapRange(symbol=g.symbol, start=g.start, end=g.end,
                 missing_dates=g.missing_dates, suspend_justified=False,
                 unjustified_days=tuple(d for d in g.unjustified_days if d in eff_days))
    )


def _gap_to_dict(g: GapRange) -> dict:
    """GapRange → 可 JSON 序列化的 dict（含 N1 日级/启发式新字段）。"""
    return {
        "symbol": g.symbol,
        "start": g.start,
        "end": g.end,
        "missing_dates": list(g.missing_dates),
        "suspend_justified": g.suspend_justified,
        "unjustified_days": list(g.unjustified_days),
        "suspend_suspected": g.suspend_suspected,
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
          f"{report['total_gaps'] - report['unjustified_gaps']}，"
          f"真需补子段 {report['unjustified_gaps']}）")
    # N1 新增可见性：启发式推定停牌（长洞）与 sidecar 已标记不可补——两者都不是
    # 漏采但性质不同（推定=概率结论需可审计；标记=已实证拉空需逃生口），单列展示
    print(f"长洞市场共识推定停牌 {report['suspend_suspected_segments']} 段；"
          f"unfillable 已标记跳过 {report['unfillable_skipped_segments']} 段"
          f"（--clear-unfillable 可重置）")
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
