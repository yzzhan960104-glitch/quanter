# -*- coding: utf-8 -*-
"""L0 数据快照冻结（spec §6.1 / §1.4 漂移实证）。

物理意图：探查（discovery/tools/probe_champion_oos.py）发现两次跑 universe 1334→1332 只、
冠军 ann 漂 6%——data_lake 增量 + 流动性边界票浮动致"连复现自己都做不到"。本模块
冻结 universe + 日期范围 → sha256 指纹，同一指纹下所有 trial 可比，数据湖后续增量
不污染历史试验（spec ADR3）。

MVP（Plan 1）：universe = 创板科创 2025 截面（start 参数化），含 2025+2026 数据；
inner=2025 / outer=2026 holdout（非 walk-forward，4 折推后续 plan）。

与 scripts/param_iter.load_universe 同源（创板科创 + 近30日均成交额≥1e5 千元=1亿），
但 start 参数化（后续 plan 跑 2020-2024 时改 start）。
"""
import hashlib
import json
from dataclasses import dataclass

import pandas as pd

LAKE_PATH = "data_lake/a_shares_daily.parquet"
DEFAULT_UNIVERSE_DEF = "创板科创/2025截面/近30日均额≥1e5千元"


@dataclass
class SnapshotMeta:
    """快照元数据（落 SQLite snapshot 表）。"""
    snapshot_hash: str          # sha256[:16] 冻结指纹
    universe_def: str           # universe 定义描述
    universe_count: int         # 标的数
    date_range: str             # 实际数据日期范围 "start~end"
    lake_start: str             # 加载起始日（参数化用）


def is_target_board(sym):
    """创业板(300/301)+科创板(688/689)。与 param_iter.is_target_board 同源。"""
    code = sym.split(".")[0]
    return code.startswith(("300", "301", "688", "689"))


def snapshot_hash(universe_count, date_range, lake_start, universe_def=DEFAULT_UNIVERSE_DEF):
    """纯函数：快照指纹 sha256[:16]。同输入→同输出（可复现基石），不读文件故可快速单测。"""
    sig = json.dumps({
        "universe_def": universe_def,
        "universe_count": int(universe_count),
        "date_range": str(date_range),
        "lake_start": str(lake_start),
    }, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(sig.encode("utf-8")).hexdigest()[:16]


def load_universe(start="2025-01-01"):
    """加载创板科创 start 至今可交易标的 → {symbol: sym_df}。

    近30日均成交额≥1e5 千元（=1亿元）过滤流动性。sym_df 含 start 至今 OHLCV（含
    2026），供 objective 全历史跑 scan_symbol + 按 signal_date 分段（不硬切 df，
    避免 window/ATR 预热丢失——探查脚本验证过的范式）。
    """
    lake = pd.read_parquet(LAKE_PATH)
    lake = lake[lake.index.get_level_values("date") >= pd.Timestamp(start)]
    syms = lake.index.get_level_values("symbol").unique().tolist()
    # 近30日均成交额（千元）；按 symbol 取 tail(30) 均值，对齐 param_iter 口径
    amt = lake.groupby("symbol")["amount"].apply(lambda s: s.tail(30).mean() if len(s) > 0 else 0.0)
    tradable = [s for s in syms if is_target_board(s) and amt.get(s, 0.0) >= 1e5]
    universe = {}
    for s in tradable:
        try:
            universe[s] = lake.xs(s, level="symbol").sort_index()
        except Exception:
            continue
    return universe


def freeze(lake_start="2025-01-01"):
    """冻结一个快照：加载 universe + 算指纹 → (universe, SnapshotMeta)。

    universe 全量加载（start 至今），objective 再按 signal_date 切 inner/outer，
    而非此处切——保证 scan_symbol 有完整历史做 window/ATR 预热。
    """
    universe = load_universe(start=lake_start)
    dates = []
    for sym_df in universe.values():
        dates.extend(list(sym_df.index))
    if dates:
        dmin, dmax = min(dates), max(dates)
        date_range = f"{pd.Timestamp(dmin).date()}~{pd.Timestamp(dmax).date()}"
    else:
        date_range = "empty"
    meta = SnapshotMeta(
        snapshot_hash=snapshot_hash(len(universe), date_range, lake_start),
        universe_def=DEFAULT_UNIVERSE_DEF,
        universe_count=len(universe),
        date_range=date_range,
        lake_start=lake_start,
    )
    return universe, meta
