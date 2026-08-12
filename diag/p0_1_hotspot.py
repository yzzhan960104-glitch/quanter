# -*- coding: utf-8 -*-
"""P0-1：cProfile 热点确认（spec §1 P0-1 / §0.3 假设）。

物理意图（Why）：动 P1 向量化前，实测 scan_symbol 的时间去哪了，验证 spec §0.3 假设——
热点 = search_neckline O(tops²) 双循环 + local_minima O(W) 循环 + 逐日 iloc 双切片
（sym_df.iloc[:i+1] + atr_full.iloc[:i+1]，单标的 n≈2400 交易日 → O(n²) 拷贝）。

两段：
  ① 单标的（300750.SZ 全历史）单次 scan_symbol；
  ② 抽样 30 标的（discovery universe 前 30 只创板科创，lake_start=2025 对齐真实跑批口径）。

预期热点池 = strategies/neckline 命名函数 ∪ pandas 内部函数（iloc 切片/拷贝归此类）。
验收门（spec §1）：预期池 tottime 占比 ≥ 70%。

只读：不改任何策略/discovery 代码，只 import 调用。测量脚本不硬崩溃于意外结果——
占比偏离时打 WARN，由 Task 5 复核 P1 优先级（如 simulate_exit 反占大头则 P1 重排）。
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


def _expected_share(stats: pstats.Stats) -> float:
    """预期热点池 tottime 占比。

    pstats.Stats.stats 的 key = (文件路径, 行号, 函数名)，value = (cc, nc, tt, ct, callers)，
    其中 tt = tottime（不含子调用）。预期池 = 路径含 'strategies/neckline' 或 'pandas'。
    前者是颈部算法 Python 循环，后者是 iloc 切片/数组拷贝（spec 明列的两类热点）。
    """
    total = sum(v[2] for v in stats.stats.values())  # 全局 tottime
    if total <= 0:
        return 0.0
    hit = sum(
        v[2] for k, v in stats.stats.items()
        if ("strategies/neckline" in k[0]) or ("pandas" in k[0])
    )
    return hit / total


def _top_table(pr: cProfile.Profile, n: int = 20) -> str:
    """cProfile.Profile → tottime 降序 top-N 表（字符串）。"""
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
    share1 = _expected_share(pstats.Stats(pr1))
    print(f"\n=== ① 单标的 300750.SZ：预期热点池 tottime 占比 = {share1*100:.1f}% ===",
          flush=True)
    print(_top_table(pr1), flush=True)

    # ② 抽样 30 标的：discovery universe（lake_start=2025，对齐真实跑批每 worker 口径）
    universe = load_universe(start="2025-01-01")
    sample = dict(sorted(universe.items())[:30])
    pr30 = cProfile.Profile()
    pr30.enable()
    for _s, df in sample.items():
        scan_symbol(df, DEFAULTS["window"])
    pr30.disable()
    share30 = _expected_share(pstats.Stats(pr30))
    print(f"\n=== ② 抽样 30 标的（创板科创 2025+）：预期热点池 tottime 占比 = {share30*100:.1f}% ===",
          flush=True)
    print(_top_table(pr30), flush=True)

    # 验收门（spec §1）：min(单标的, 30标的) ≥ 70%
    gate = min(share1, share30)
    verdict = "✅ PASS" if gate >= 0.70 else "⚠️ 偏离 spec 假设 → Task 5 复核 P1 优先级"
    print(f"\n=== 验收门：min 预期占比 = {gate*100:.1f}%（≥70%）→ {verdict} ===", flush=True)
    print(f"\n[填入 §实测结果 P0-1] 单标的占比={share1*100:.1f}%  30标的占比={share30*100:.1f}%  "
          f"gate={gate*100:.1f}%  verdict={verdict}", flush=True)


if __name__ == "__main__":
    main()
