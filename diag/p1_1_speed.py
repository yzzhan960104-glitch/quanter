# -*- coding: utf-8 -*-
"""P1 测速对拍（spec §2.3 验收门：单组全宇宙评估 720s -> <=40s）。

物理意图（Why）：P1 向量化是性能改造，需要 before/after 同口径硬数字——本脚本在改造
前（旧实现）与改造后（新实现）各跑一次，同一抽样、同一度量，产出可对拍的耗时。

抽样口径（与 P0-1/P0-3 对齐，确定性可复现）：
  - 10 标的：p0_3_baseline.json 的抽样 universe（15 信号锚，P1 等价验收同口径）；
  - 100 标的：load_universe 排序后前 100 只（批量放大样本，摊薄单标的噪声）；
  - 全宇宙：load_universe 全部标的 run_full_scan（生产口径 720s 锚；旧实现下耗时长，
    仅 after 跑，before 用 discovery daemon 实测 720s/组 锚定）。

度量：scan_symbol 逐标的 wall-clock 总耗时（识别+出场全链路，P0-1 确认识别路径
占 ~80% 主导）。每段先跑 1 次预热（数据/代码缓存），再计时正式跑。

验收门（spec §2.3）：after 全宇宙 run_full_scan <= 40s -> [PASS]，否则 [WARN]（如实
记录，不回退——速度目标可依实测再定）。

只读：不改任何策略/discovery 代码。输出全 ASCII（无 emoji/箭头），规避 Windows
GBK stdout UnicodeEncodeError。
"""
import os
import sys
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import pandas as pd  # noqa: E402

from strategies.neckline.backtest import scan_symbol, EXEC_DEFAULTS  # noqa: E402
from strategies.neckline.method_v0 import DEFAULTS  # noqa: E402
from discovery.snapshot import load_universe  # noqa: E402
from discovery.objective import run_full_scan  # noqa: E402

LAKE_START = "2025-01-01"


def _time_scan(universe: dict) -> tuple:
    """逐标的 scan_symbol 总耗时（预热 1 次 + 正式 1 次）。返回 (warm_sec, real_sec, n_symbols)。"""
    n = len(universe)
    for sym in universe:
        scan_symbol(universe[sym], DEFAULTS["window"])   # 预热（不含计时）
    t0 = time.perf_counter()
    for sym in universe:
        scan_symbol(universe[sym], DEFAULTS["window"])
    return time.perf_counter() - t0, n


def main():
    print(f"[P1-1] 加载 universe（lake_start={LAKE_START}）...", flush=True)
    universe = load_universe(start=LAKE_START)
    syms = sorted(universe)
    print(f"[P1-1] universe {len(syms)} 只\n", flush=True)

    # 段一：10 标的抽样（p0_3 锚）
    base_syms = [
        "300004.SZ", "300012.SZ", "300014.SZ", "300015.SZ", "300016.SZ",
        "300017.SZ", "300018.SZ", "300019.SZ", "300024.SZ", "300031.SZ",
    ]
    sub10 = {s: universe[s] for s in base_syms if s in universe}
    real, n = _time_scan(sub10)
    print(f"[P1-1] 段1 10标的: {real:.2f}s / {n} symbols "
          f"(per-symbol {real / n * 1000:.1f}ms)", flush=True)

    # 段二：100 标的抽样
    sub100 = {s: universe[s] for s in syms[:100]}
    real, n = _time_scan(sub100)
    print(f"[P1-1] 段2 100标的: {real:.2f}s / {n} symbols "
          f"(per-symbol {real / n * 1000:.1f}ms)", flush=True)

    # 段三：全宇宙 run_full_scan（生产口径；仅 after 建议跑——旧实现 ~720s）
    if os.environ.get("P1_FULL_UNIVERSE"):
        params = {**DEFAULTS, **EXEC_DEFAULTS}   # 全 21 键（run_full_scan 具名 overlay 读全键）
        t0 = time.perf_counter()
        filled = run_full_scan(params, universe)
        real = time.perf_counter() - t0
        verdict = "[PASS]" if real <= 40 else "[WARN]"
        print(f"[P1-1] 段3 全宇宙 run_full_scan: {real:.2f}s / {len(filled)} signals "
              f"-> 验收门 <=40s {verdict}", flush=True)
    else:
        print("[P1-1] 段3 全宇宙：跳过（设 P1_FULL_UNIVERSE=1 开启；旧实现约 720s）",
              flush=True)

    print("\n[P1-1] done. 与 before 记录对拍：per-symbol ms 应有 ~10-30x 下降。", flush=True)


if __name__ == "__main__":
    main()
