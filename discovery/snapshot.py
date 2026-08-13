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
    data_hash: str = ""         # P1-3（2026-08-03）：universe 价格内容指纹（close 序列
                                # sha256 聚合）。数据增量/复权重算历史 → 指纹变，供
                                # daemon 审计"跨夜收敛因数据版本变化而重置"。


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


def data_content_hash(universe, lake_start="2025-01-01"):
    """universe 价格内容指纹（P1-3 · 2026-08-03 Phase A）。

    物理意图：snapshot_hash 只含 universe_count/date_range/lake_start/universe_def，
    不含价格内容——若数据湖增量同步重算 qfq 基准（除权标的，sync_daily_incremental
    已知 follow-up），同一 snapshot_hash 下历史价格变化，新旧 trial "假可比"。
    本函数对每 symbol 的 close 序列（date>=lake_start）做 sha256 聚合：
        - 数据没变 → 指纹稳定（同内容同 hash）；
        - 新增交易日（增量落湖）→ close 序列变长 → 指纹变；
        - 历史价被重算（除权）→ 指纹变。
    daemon 用它标注"数据版本变化导致跨夜收敛重置"（k=0 不再不可见）。
    """
    h = hashlib.sha256()
    for sym in sorted(universe):
        df = universe[sym]
        closes = df["close"].astype("float64").values
        h.update(sym.encode("utf-8"))
        h.update(closes.tobytes())
    return h.hexdigest()[:16]


def load_universe(start="2025-01-01"):
    """加载创板科创 start 至今可交易标的 → {symbol: sym_df}。

    近30日均成交额≥1e5 千元（=1亿元）过滤流动性。sym_df 含 start 至今 OHLCV（含
    2026），供 objective 全历史跑 scan_symbol + 按 signal_date 分段（不硬切 df，
    避免 window/ATR 预热丢失——探查脚本验证过的范式）。

    2026-08-03 资源优化：read_parquet 直接带 pyarrow filters（date>=start），
    只读 start 之后的行——旧实现先全量读 1019 万行再筛，每 worker ~1.3GB；
    过滤后（2025 起 ~200 万行）每 worker 降到 ~0.3GB。12 个 worker 同时加载
    是内存峰值来源，这是最重要的单项削减。
    """
    try:
        lake = pd.read_parquet(
            LAKE_PATH,
            filters=[("date", ">=", pd.Timestamp(start))],
        )
    except Exception:
        # filters 推送失败（异常湖/旧引擎）→ 回退全量读再筛（语义不变，仅内存优化）
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
        data_hash=data_content_hash(universe, lake_start),
    )
    return universe, meta


# ============================================================================
# P5（2026-08-13 · spec §6.1）：分折 universe（防幸存者偏差）
# ============================================================================
def filter_universe_from_lake(lake_df, min_amt=1e5):
    """湖 DataFrame → 可交易 universe dict（纯函数，分折/全量共用）。

    lake_df: 已按日期窗截好的湖（MultiIndex date/symbol，含 amount 列）。
    流动性口径：近 30 日均成交额 ≥ min_amt（1e5 千元 = 1 亿元，对齐 param_iter）；
    创板科创过滤（is_target_board 口径）。返回 {symbol: sym_df}（sort_index 后）。
    """
    from discovery.tools.param_iter import is_target_board
    syms = lake_df.index.get_level_values("symbol").unique().tolist()
    amt = lake_df.groupby("symbol")["amount"].apply(
        lambda s: s.tail(30).mean() if len(s) > 0 else 0.0)
    tradable = [s for s in syms if is_target_board(s) and amt.get(s, 0.0) >= min_amt]
    universe = {}
    for s in tradable:
        try:
            universe[s] = lake_df.xs(s, level="symbol").sort_index()
        except Exception:
            continue
    return universe


def load_universe_window(start, end, warmup_days=180):
    """P5 分折 universe：标的池按【折末 30 日】流动性重算，数据自 start-warmup 起。

    与 load_universe(start) 的关键差异（幸存者偏差防线，spec §6.1）：
      - 流动性 tail(30) 取自 [start-warmup, end] 窗（折末口径）——历史折不用未来
        流动性选股（load_universe 的 tail(30) 是「今天」口径，会把 2025 之后才
        活跃的标的选进 2020 折 = 幸存者偏差）；
      - 数据含 start-warmup 预热段（窗口 window≤80 + ATR 余量，颈线形态在折首
        也能正确成形）；信号日期由调用方 segment 过滤，预热段不产计入信号；
      - 退市标的：湖内保留挂牌期数据（P0-2 实测 38 只退市/休眠创板科创），折内
        挂牌且满足流动性则纳入，折后自然消失。
    """
    from datetime import timedelta
    data_start = pd.Timestamp(start) - timedelta(days=warmup_days)
    try:
        lake = pd.read_parquet(
            LAKE_PATH,
            filters=[("date", ">=", data_start), ("date", "<=", pd.Timestamp(end))],
        )
    except Exception:
        lake = pd.read_parquet(LAKE_PATH)   # filters 推送失败 → 全量读再筛（语义不变）
        lake = lake[(lake.index.get_level_values("date") >= data_start)
                    & (lake.index.get_level_values("date") <= pd.Timestamp(end))]
    return filter_universe_from_lake(lake)
