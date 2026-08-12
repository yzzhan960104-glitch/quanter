# -*- coding: utf-8 -*-
"""T-1 计划归因聚合（C2d 下沉 · 2026-08-12）。

物理意图：A/B 实验跑完后，研究员要回答「prod vs candidate 各自真下了多少单、占多少权重、
覆盖多少标的」。聚合维度是 experiment_id，数据源是 DB trade_event(SIGNAL).meta（DG-5 单一
真相源 = trade_event 表，非 plan_*.json）。

Why 从 experiment/cli.py 下沉到此：experiment 是纯配置叶子层（layer 铁律 5，
test_experiment_pure_leaf 禁任何 trading import），而 plan 数据在 trading.state_store，
归因天然是 trading concern。experiment CLI 的 report 子命令已退化为 deprecation 提示。

致命日期轴（红线）：底层 list_signals_with_meta_by_plan_date_range 按 plan_date
（trade_id 后缀 substr(-10)）区间查，**非 trade_event.timestamp**（= T 日盘后写入 ≠
计划日 T+1）——旧 _load_all_plans 扫 plan_*.json 按 mtime 恒错，本模块走 DB plan_date 才对。
"""
from __future__ import annotations

import argparse
import sys

from trading import state_store

# 未带 experiment_id 的老 order 归到这个桶，避免历史回测单子与新实验单子混淆归因。
_UNATTRIBUTED = "未归因"


def report_plan_attribution(since: str | None = None) -> list[dict]:
    """按 experiment_id 聚合 SIGNAL 计划：订单数 / 末次权重 / 去重标的。

    Args:
        since: 起始 plan_date（YYYY-MM-DD，含）；None = 全量。

    Returns:
        每项 {experiment_id, n, weight, symbols}，按 experiment_id 升序。weight 取该实验
        末次覆盖（单实验内 weight 恒定，跨 plan 随 promote/set-weight 更新）。
    """
    metas = state_store.list_signals_with_meta_by_plan_date_range(since=since)
    groups: dict[str, dict] = {}
    for m in metas:
        # 底层每项即一个 SIGNAL/order 行（含 experiment_id/experiment_weight/order）。
        eid = m.get("experiment_id") or _UNATTRIBUTED
        g = groups.setdefault(eid, {"n": 0, "weight": None, "symbols": set()})
        g["n"] += 1
        g["weight"] = m.get("experiment_weight")  # 末次覆盖
        sym = (m.get("order") or {}).get("symbol") or m.get("symbol")
        if sym:
            g["symbols"].add(sym)
    return [
        {"experiment_id": eid, "n": g["n"], "weight": g["weight"],
         "symbols": sorted(g["symbols"])}
        for eid, g in sorted(groups.items())
    ]


def _main(argv: list | None = None) -> int:
    """CLI 入口：python -m trading.plan_report [--since YYYY-MM-DD]。"""
    ap = argparse.ArgumentParser(prog="trading.plan_report", description="T-1 计划归因聚合（按 experiment_id）")
    ap.add_argument("--since", default=None, help="起始计划日 YYYY-MM-DD（含），留空全量")
    args = ap.parse_args(argv)
    rows = report_plan_attribution(since=args.since)
    # 表头定宽对齐，方便终端肉眼扫读（与旧 experiment report 同口径）
    print(f"{'experiment_id':30}{'订单数':>8}{'权重':>8}{'标的数':>8}")
    for r in rows:
        w = f"{r['weight']:.2f}" if r["weight"] is not None else "-"
        print(f"{r['experiment_id']:30}{r['n']:>8}{w:>8}{len(r['symbols']):>8}")
    return 0


if __name__ == "__main__":
    sys.exit(_main())
