# -*- coding: utf-8 -*-
"""P0-4：单 worker RSS 基线（spec §1 P0-4 / P2 n_proc 公式输入）。

物理意图（Why）：P2 放开 n_proc 须用"每 worker RSS"算上限，配 RSS 看门狗防 2026-08-03
MemoryError（12~18 worker 各 ~1.3GB 压垮 32GB 机）。一个 worker 的全部内存行为 =
freeze()(加载 universe) + evaluate()(跑一组 params)。故本脚本在单进程内直接调
freeze+evaluate/evaluate_replay，用 psutil 量自身 RSS——不侵入生产 eval_batch（只读红线）。

psutil 已在 requirements.txt:57（全仓首次使用，无新增依赖）。
lake_start=2025 对齐 load_universe 列裁剪口径（2025 起 ~0.3GB/worker，snapshot.py 实证）。

产出：discovery 路 / replay 路 各自 peak RSS（字节 + GB），与机器可用内存算 n_proc 上限。
"""
import gc
import os
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import psutil  # noqa: E402  （requirements.txt:57，全仓首次使用）
from strategies.neckline.method_v0 import DEFAULTS  # noqa: E402
from strategies.neckline.backtest import EXEC_DEFAULTS  # noqa: E402
from discovery.snapshot import freeze  # noqa: E402
from discovery.split import holdout_split  # noqa: E402
from discovery.objective import evaluate, evaluate_replay  # noqa: E402

LAKE_START = "2025-01-01"


def _gb(b: float) -> str:
    return f"{b / 1024**3:.2f}GB"


def _params_default():
    """21 维默认 params（discovery 跑批的默认档，evaluate 取 ID_KEYS/EXEC_KEYS 子集）。"""
    return {**DEFAULTS, **EXEC_DEFAULTS}


def _measure(label: str, fn) -> float:
    """gc 后量基线 RSS → 跑 fn → 量峰值 RSS，返回 peak。"""
    proc = psutil.Process(os.getpid())
    gc.collect()
    rss0 = proc.memory_info().rss
    fn()
    rss1 = proc.memory_info().rss
    peak = max(rss0, rss1)
    print(f"  [{label}] rss 前={_gb(rss0)}  后={_gb(rss1)}  peak={_gb(peak)}",
          flush=True)
    return peak


def main():
    lake = _ROOT / "data_lake" / "a_shares_daily.parquet"
    if not lake.exists():
        print("[P0-4] data_lake 缺失，跳过", file=sys.stderr)
        sys.exit(2)

    proc = psutil.Process(os.getpid())
    machine_mem = psutil.virtual_memory().total
    print(f"机器物理内存 = {_gb(machine_mem)}  lake_start={LAKE_START}", flush=True)

    # freeze 一次（discovery / replay 共用同一 universe）
    print("\n=== freeze（加载 universe）===", flush=True)
    universe, meta = freeze(lake_start=LAKE_START)
    split = holdout_split()
    rss_after_freeze = proc.memory_info().rss
    print(f"  universe {meta.universe_count} 只  range={meta.date_range}  "
          f"data_hash={meta.data_hash}", flush=True)
    print(f"  freeze 后 RSS = {_gb(rss_after_freeze)}", flush=True)

    params = _params_default()

    # ① discovery 路：evaluate（kelly/calmar 口径，run_full_scan→scan_symbol）
    print("\n=== ① discovery 路（evaluate）===", flush=True)
    peak_disc = _measure("discovery", lambda: evaluate(params, universe, split))

    # ② replay 路：evaluate_replay（backtest.replay 引擎，主回测同源）
    print("\n=== ② replay 路（evaluate_replay）===", flush=True)
    peak_replay = _measure("replay", lambda: evaluate_replay(params, universe, split))

    # n_proc 上限公式（P2 输入）：留 4GB 给系统/主进程，余量 / 单 worker peak
    per_worker = max(peak_disc, peak_replay)
    headroom = max(0.0, machine_mem - 4 * 1024**3)
    n_proc_cap = int(headroom // per_worker) if per_worker > 0 else 0
    print(f"\n=== 汇总（P2 n_proc 公式输入）===", flush=True)
    print(f"  discovery peak = {_gb(peak_disc)}", flush=True)
    print(f"  replay    peak = {_gb(peak_replay)}", flush=True)
    print(f"  每 worker 取 max = {_gb(per_worker)}", flush=True)
    print(f"  n_proc 上限（留 4GB 系统余量）= {n_proc_cap}", flush=True)
    print(f"\n[填入 §实测结果 P0-4] disc={_gb(peak_disc)} replay={_gb(peak_replay)} "
          f"per_worker={_gb(per_worker)} n_proc_cap={n_proc_cap}", flush=True)


if __name__ == "__main__":
    main()
