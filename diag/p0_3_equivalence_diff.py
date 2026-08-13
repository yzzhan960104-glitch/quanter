# -*- coding: utf-8 -*-
"""P0-3：P1 向量化验收的逐信号字段级 diff 基建（spec §1 P0-3 / §2.3 验收门）。

物理意图（Why）：P1 向量化（720s→≤40s）是 spec 最高风险项——行为等价红线要求逐信号
字段级零差异。本模块冻结一个确定性抽样 universe，把【当前 scan_symbol】每条信号的
CANONICAL 字段存成基线 JSON；P1 新实现跑同一 universe 后调 compare()，必须零 mismatch。

确定性（P1 可复现的基石）：
  - 抽样 = load_universe(lake_start) 的 symbol 排序后取前 N 只【有信号】的标的（固定 N）；
  - 基线 JSON 存 symbol 列表 + params + data_content_hash；
  - P1 对拍前先校验 data_content_hash 一致，否则湖已变（增量/复权）→ 告警须重建基线。

CANONICAL 字段（识别+出场结果的核心数值，剔 debug-only）：
  signal_date / entry / exit_reason / avg_pnl_pct / neckline / tp2 / exit_date / exit_price
"""
import json
import sys
import warnings
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from strategies.neckline.method_v0 import DEFAULTS  # noqa: E402
from strategies.neckline.backtest import scan_symbol, EXEC_DEFAULTS  # noqa: E402
from discovery.snapshot import load_universe, data_content_hash  # noqa: E402

LAKE = _ROOT / "data_lake" / "a_shares_daily.parquet"
BASELINE_PATH = Path(__file__).resolve().parent / "p0_3_baseline.json"

# P1 对拍的字段集（数值/枚举核心；剔 suppression 等 debug-only 元数据）
# rr 不入列：rr 由 strategy.scan_at 另算，非 scan_symbol/simulate_exit 的 filled 输出，
# 基线里恒为 null（s.get("rr") -> None），零区分力。
CANONICAL = ["signal_date", "entry", "exit_reason", "avg_pnl_pct",
             "neckline", "tp2", "exit_date", "exit_price"]

# 抽样规模：N 只【有信号】的创板科创标的（够覆盖典型形态，又控制基线体积/跑时）
SAMPLE_N = 10
DEFAULT_LAKE_START = "2025-01-01"


def _key(sig):
    """信号唯一键：(symbol, signal_date)。逐位对拍按此配对。"""
    return (sig.get("symbol"), str(sig.get("signal_date")))


def compare_signals(baseline: list, current: list) -> list:
    """两份信号列表 → mismatch 列表（P1 验收：须返空 list）。

    按 (symbol, signal_date) 配对，逐 CANONICAL 字段比对。
    缺失/多余信号分别记 __missing__ / __extra__。浮点用 round(…, 6) 容差对齐。

    类型对齐（Why）：基线 JSON 经 default=str 往返后日期为 str（"2025-07-21"），而 scan_symbol
    现场产出 datetime.date 对象——若直接 != 会假性 mismatch。非浮点分支对两侧取 str() 归一，
    使日期/枚举/数值的"同值异型"（JSON str ↔ Python date/int）判等相等，对拍只报真实差异。
    """
    bm = {_key(s): s for s in baseline}
    cm = {_key(s): s for s in current}
    mismatches = []
    # 缺失（baseline 有、current 无）
    for k, s in bm.items():
        if k not in cm:
            mismatches.append({"symbol": s.get("symbol"),
                               "signal_date": s.get("signal_date"),
                               "field": "__missing__", "baseline": "present",
                               "current": "absent"})
    # 多余 + 字段差异
    for k, c in cm.items():
        if k not in bm:
            mismatches.append({"symbol": c.get("symbol"),
                               "signal_date": c.get("signal_date"),
                               "field": "__extra__", "baseline": "absent",
                               "current": "present"})
            continue
        b = bm[k]
        for f in CANONICAL:
            bv = b.get(f)
            cv = c.get(f)
            if isinstance(bv, float) and isinstance(cv, float):
                if round(bv, 6) != round(cv, 6):
                    mismatches.append({"symbol": c.get("symbol"),
                                       "signal_date": c.get("signal_date"),
                                       "field": f, "baseline": bv, "current": cv})
            elif str(bv) != str(cv):
                mismatches.append({"symbol": c.get("symbol"),
                                   "signal_date": c.get("signal_date"),
                                   "field": f, "baseline": bv, "current": cv})
    return mismatches


def _params_default():
    """构造默认 params——DEFAULTS(11) ∪ EXEC_DEFAULTS(13) = 24 keys（evaluate 实取 ID_KEYS 11 + EXEC_KEYS 10 子集，commission/stamp/transfer 三个 cost rate 不进 evaluate）。"""
    return {**DEFAULTS, **EXEC_DEFAULTS}


def _run_sampling(universe, params):
    """对 universe 跑 scan_symbol 收集 filled 信号——委托生产 run_full_scan（生产强一致）。

    Why 委托：P0-3 是 P1 向量化验收基建，对拍口径必须与 discovery 生产 evaluate 完全一致
    （run_full_scan 用 ID_KEYS/EXEC_KEYS 具名 overlay 构 cfg），否则非默认 params 下 gate
    量非所跑。旧实现自构 id_cfg/exec_cfg（{**DEFAULTS, **{k in DEFAULTS}}）与生产分叉——默认
    params 下收敛（基线正确），但 P1 复用 compare() 跑非默认 params 时口径可能失真。
    注意签名换序：本 wrapper 是 (universe, params)，run_full_scan 是 (params, universe)。
    """
    from discovery.objective import run_full_scan
    try:
        return run_full_scan(params, universe)
    except Exception as e:
        warnings.warn(f"[P0-3] run_full_scan threw: {e!r}")
        return []


def build_sampling_universe(lake_start=DEFAULT_LAKE_START, n=SAMPLE_N):
    """确定性抽样：排序后取前 n 只【有信号】创板科创标的。

    Why 取有信号的：避免基线 universe 全空信号（无对拍价值）。symbol 排序保证确定性。
    """
    universe = load_universe(start=lake_start)
    chosen = {}
    for sym in sorted(universe):
        df = universe[sym]
        filled, _n, _skip = scan_symbol(df, DEFAULTS["window"])
        if filled:
            chosen[sym] = df
            if len(chosen) >= n:
                break
    return chosen


def record_baseline(path=BASELINE_PATH, lake_start=DEFAULT_LAKE_START):
    """冻结抽样 universe + 当前实现基线信号 → JSON（P1 对拍锚）。"""
    universe = build_sampling_universe(lake_start=lake_start)
    params = _params_default()
    signals = _run_sampling(universe, params)
    payload = {
        "lake_start": lake_start,
        "symbols": sorted(universe.keys()),
        "params": params,
        "data_content_hash": data_content_hash(universe, lake_start),
        "canonical_fields": CANONICAL,
        "n_signals": len(signals),
        "signals": [{f: s.get(f) for f in (["symbol"] + CANONICAL)} for s in signals],
    }
    Path(path).write_text(json.dumps(payload, ensure_ascii=False, indent=2,
                                     default=str), encoding="utf-8")
    return payload


def compare(baseline_path=BASELINE_PATH, lake_start=DEFAULT_LAKE_START) -> dict:
    """重跑当前实现，与基线 JSON 对拍 → {data_hash_ok, mismatches, n_baseline, n_current}。

    P1 验收用法：基线由 P0 本 Task 冻结（当前实现）；P1 向量化落地后重跑本函数，
    须 mismatches == [] 且 data_hash_ok == True。
    """
    base = json.loads(Path(baseline_path).read_text(encoding="utf-8"))
    # 校验湖内容指纹（湖变 → 基线失效，须重 record_baseline）
    universe = load_universe(start=base["lake_start"])
    sub = {s: universe[s] for s in base["symbols"] if s in universe}
    cur_hash = data_content_hash(sub, base["lake_start"])
    data_hash_ok = (cur_hash == base["data_content_hash"])
    current = _run_sampling(sub, base["params"])
    mismatches = compare_signals(base["signals"], current)
    return {"data_hash_ok": data_hash_ok,
            "n_baseline": base["n_signals"], "n_current": len(current),
            "n_mismatch": len(mismatches), "mismatches": mismatches[:50]}


if __name__ == "__main__":
    if not LAKE.exists():
        print("[P0-3] data_lake 缺失，仅单测覆盖（record/compare 需湖）", file=sys.stderr)
        sys.exit(2)
    payload = record_baseline()
    # 自洽校验：当前实现对自己 must 零 mismatch（证明对拍基建正确）
    self_check = compare()
    print(f"[P0-3] baseline: {payload['n_signals']} signals / {len(payload['symbols'])} symbols / "
          f"data_hash={payload['data_content_hash']}", flush=True)
    print(f"[P0-3] self-check: data_hash_ok={self_check['data_hash_ok']} "
          f"mismatches={self_check['n_mismatch']} (expect 0)", flush=True)
    print(f"-> {BASELINE_PATH}", flush=True)
