# -*- coding: utf-8 -*-
"""P0-1：cProfile 热点确认（spec §1 P0-1 / §0.3 假设）。

物理意图（Why）：动 P1 向量化前，实测 scan_symbol 的时间去哪了，验证 spec §0.3 假设——
热点 = search_neckline O(tops²) 双循环 + local_minima O(W) 循环 + 逐日 iloc 双切片
（sym_df.iloc[:i+1] + atr_full.iloc[:i+1]，单标的 n≈2400 交易日 → O(n²) 拷贝）。

两段：
  ① 单标的（300750.SZ 全历史）单次 scan_symbol；
  ② 抽样 30 标的（discovery universe 前 30 只创板科创，lake_start=2025 对齐真实跑批口径）。

度量（cumtime 路径占比，修正首版 tottime 归因误读）：
  cProfile 经典归因问题——search_neckline/local_minima 等 Python 循环里反复调
  numpy .max()/.min()/abs()，这些 numpy 叶子开销（reduce 1.561s）被 cProfile 归到
  numpy ufunc，不计入 Python 调用方的 tottime。故 tottime+预期池会漏算识别路径
  真实成本。改用 cumtime（含子调用）做诚实归因：
    识别路径占比 = cumtime(detect_signal) / cumtime(scan_symbol)
    出场路径占比 = cumtime(simulate_exit) / cumtime(scan_symbol)
  detect_signal 的 cumtime 已含其调用的 search_neckline/local_minima/numpy 叶子，
  不再漏算。

验收门（spec §1，修正版）：识别路径 cumtime ≥ 50% scan_symbol → [PASS]
（识别路径主导 → P1 向量化识别路径对症）；否则 [WARN]。

只读：不改任何策略/discovery 代码，只 import 调用。测量脚本不硬崩溃于意外结果——
占比偏离时打 WARN，由 Task 5 复核 P1 优先级（如 simulate_exit 反占大头则 P1 重排）。

输出全 ASCII（无 emoji/箭头），规避 Windows GBK stdout UnicodeEncodeError。
"""
import cProfile
import io
import pstats
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import pandas as pd  # noqa: E402
from strategies.neckline.method_v0 import DEFAULTS  # noqa: E402
from strategies.neckline.backtest import scan_symbol  # noqa: E402
from discovery.snapshot import load_universe  # noqa: E402

LAKE = _ROOT / "data_lake" / "a_shares_daily.parquet"


def _cumtime_of(stats: pstats.Stats, funcname: str) -> float:
    """取指定函数名的 cumtime（含子调用总时间，pstats value 索引 3）。

    pstats.Stats.stats 的 key = (文件路径, 行号, 函数名)，
    value = (cc, nc, tottime, cumtime, callers)，其中 cumtime = v[3]（自身+全部子调用）。
    同名函数可能因多处定义而有多个条目（不同 path/lineno），取其 cumtime 峰值；通常唯一。
    """
    cums = [v[3] for k, v in stats.stats.items() if k[2] == funcname]
    return max(cums) if cums else 0.0


def _path_shares(stats: pstats.Stats) -> tuple[float, float]:
    """识别/出场路径 cumtime 占 scan_symbol cumtime 比例。

    Why cumtime 而非 tottime：cProfile 把 Python 循环里调用的 numpy 叶子
    （.max()/.min()/abs()/reduce）开销归到 numpy ufunc，不计入 Python 调用方
    的 tottime。故 detect_signal 的 tottime 漏算其调用的 search_neckline/
    local_minima 内部的 numpy 叶子成本，严重低估识别路径。cumtime 含全部子调用，
    能诚实归因——detect_signal 的 cumtime 已含 search_neckline/local_minima/
    numpy 叶子，不再漏算。

    返回 (识别路径 cumtime / scan_symbol cumtime, 出场路径 cumtime / scan_symbol cumtime)。
    """
    cum_scan = _cumtime_of(stats, "scan_symbol")
    if cum_scan <= 0:
        return 0.0, 0.0
    id_share = _cumtime_of(stats, "detect_signal") / cum_scan
    exit_share = _cumtime_of(stats, "simulate_exit") / cum_scan
    return id_share, exit_share


def _top_table(pr: cProfile.Profile, n: int = 20) -> str:
    """cProfile.Profile -> tottime 降序 top-N 表（字符串，原始证据保留）。

    tottime 表作为原始证据保留——它揭示具体叶子开销分布（numpy reduce / ndarray.max /
    pandas __finalize__ 等），但路径占比裁定用 cumtime（见 _path_shares）。
    """
    s = pstats.Stats(pr)
    buf = io.StringIO()
    s.stream = buf
    s.sort_stats("tottime").print_stats(n)
    return buf.getvalue()


def main():
    if not LAKE.exists():
        print("[P0-1] data_lake 缺失，跳过", file=sys.stderr)
        sys.exit(2)

    # ① 单标的：300750.SZ 全历史（test_neckline_core 已知 detect 能识别，避免空跑）
    lake = pd.read_parquet(LAKE)
    try:
        sym_df = lake.xs("300750.SZ", level="symbol").sort_index()
    except KeyError:
        print("[P0-1] 300750.SZ 不在湖中，换首只创板科创", flush=True)
        sym_df = next(iter(lake.xs(slice(None), level="symbol", drop_level=False)
                           .groupby(level="symbol")))[1].sort_index()

    pr1 = cProfile.Profile()
    pr1.enable()
    scan_symbol(sym_df, DEFAULTS["window"])
    pr1.disable()
    id_share1, exit_share1 = _path_shares(pstats.Stats(pr1))
    print(f"\n=== ① 单标的 300750.SZ：识别路径 cumtime = {id_share1*100:.1f}%  "
          f"出场路径 cumtime = {exit_share1*100:.1f}% ===", flush=True)
    print(_top_table(pr1), flush=True)

    # ② 抽样 30 标的：discovery universe（lake_start=2025，对齐真实跑批每 worker 口径）
    universe = load_universe(start="2025-01-01")
    sample = dict(sorted(universe.items())[:30])
    pr30 = cProfile.Profile()
    pr30.enable()
    for _s, df in sample.items():
        scan_symbol(df, DEFAULTS["window"])
    pr30.disable()
    id_share30, exit_share30 = _path_shares(pstats.Stats(pr30))
    print(f"\n=== ② 抽样 30 标的（创板科创 2025+）：识别路径 cumtime = {id_share30*100:.1f}%  "
          f"出场路径 cumtime = {exit_share30*100:.1f}% ===", flush=True)
    print(_top_table(pr30), flush=True)

    # 验收门（spec §1，修正版）：识别路径 cumtime min(单标的, 30标的) >= 50% scan_symbol
    # 识别路径主导 -> P1 向量化识别路径（search_neckline O(tops²) -> 布尔矩阵、
    # local_minima O(W) -> 滑窗掩码、消逐日 iloc）对症。
    gate = min(id_share1, id_share30)
    verdict = ("[PASS] 识别路径主导 -> P1 向量化识别路径对症"
               if gate >= 0.50
               else "[WARN] 出场/其他路径主导 -> Task 5 复核 P1 优先级")
    print(f"\n=== 验收门：识别路径 cumtime min(单标的, 30标的) = {gate*100:.1f}% "
          f"(>=50%) -> {verdict} ===", flush=True)
    print(f"\n[填入 §实测结果 P0-1] 单标的 识别={id_share1*100:.1f}% 出场={exit_share1*100:.1f}% | "
          f"30标的 识别={id_share30*100:.1f}% 出场={exit_share30*100:.1f}% | "
          f"gate={gate*100:.1f}% verdict={verdict}", flush=True)


if __name__ == "__main__":
    main()
