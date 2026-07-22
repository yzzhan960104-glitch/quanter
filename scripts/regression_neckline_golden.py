# -*- coding: utf-8 -*-
"""颈线法数值回归 T1 golden 基线（Layer 2 解耦 spec §8.3 / plan 阶段 1 Task 1.0 Step 1b）。

物理定位（为什么需要它）：
    本重构是**纯结构迁移（零逻辑改动）**的 strangler 六阶段工程。pytest 全绿只证明
    「零件没坏」，不证明「决策内核未动」。本脚本捕获颈线法 `scan_symbol` 全链路（识别→
    执行→凯利）在**固定输入**（3 标的 + DEFAULTS + EXEC_DEFAULTS）下的黑盒数值，作为
    阶段 1/2/4 每一步迁移后的**逐位对比锚**——纯重构下 `--verify` 必须 `==` 一致，
    任何漂移 = 某处偷改逻辑，立即 revert（spec §8.1 T1 层定义）。

    不变量守护（spec §8.4）：
        · 阶段 2 `check_exit` is 同源 → 本脚本数值不变
        · 阶段 4 `param_iter`/`identify_param_scan` 收口走 driver → 本脚本数值不变
        · 阶段 1 颈线法收口进 strategies/neckline/ → import 路径变，**数值不变**

固定输入（确定性三要素，零随机性）：
    ① 标的：3 只创板科创代表（300750.SZ 宁德时代 / 688981.SH 中芯国际 / 301269.SZ 华大智造），
       均经 data_lake 核验为可交易（近30日均成交额≥1亿），且历史长度充足（≥900 根）。
    ② 识别层：neckline_method_v0.DEFAULTS（11 维，未 update，原样默认）。
    ③ 执行层：neckline_backtest.EXEC_DEFAULTS（10 维，未 update，原样默认）。

镜像 param_iter.py 的 import 模式（sibling import）：
    `scan_symbol` 读全局 DEFAULTS/EXEC_DEFAULTS，与 param_iter.run_one 同口径。

用法：
    # 捕获 golden 基线（阶段 1 起步执行一次，commit 进仓）
    .venv310/Scripts/python.exe scripts/regression_neckline_golden.py --capture

    # 验证（阶段 1/2/4 每步迁移后跑，逐位对比）
    .venv310/Scripts/python.exe scripts/regression_neckline_golden.py --verify
"""
import os
import sys
import json
import argparse
import hashlib
from datetime import datetime, timezone

# 项目根 + scripts/ 加入 sys.path（镜像 param_iter.py 的 sibling import 模式）
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
from neckline_method_v0 import DEFAULTS  # noqa: E402
from neckline_backtest import scan_symbol, kelly_metrics, EXEC_DEFAULTS  # noqa: E402


# ============================================================================
# 固定输入常量（确定性锚点 · 严禁改动，改动 = 基线漂移）
# ============================================================================
GOLDEN_SYMBOLS = ["300750.SZ", "688981.SH", "301269.SZ"]
LAKE_PATH = "data_lake/a_shares_daily.parquet"
GOLDEN_PATH = "tests/_golden/neckline_baseline.json"

# scan_symbol 来源标注（import 路径会随阶段 1 收口变，数值不变——这是要守的不变量）
SCAN_SYMBOL_SOURCE = (
    "scripts/neckline_backtest.py::scan_symbol "
    "(阶段1 收口进 strategies/neckline/ 后改 from strategies.neckline.backtest import scan_symbol)"
)


def _stable_hash(obj: dict) -> str:
    """dict → 稳定 sha256（sort_keys + default=str，None/数值统一序列化）。

    用于 DEFAULTS / EXEC_DEFAULTS 内容指纹：参数 dict 任意键值变化 → 哈希变，
    迁移中误 update 全局默认即报警。
    """
    canon = json.dumps(obj, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()


def _run_pipeline():
    """跑固定 3 标的 + DEFAULTS + EXEC_DEFAULTS → scan_symbol 全链路 → 汇总结果。

    与 param_iter.run_one 同口径（不 update 全局，直接用默认 DEFAULTS/EXEC_DEFAULTS，
    scan_symbol 内部读全局）。

    返回:
        per_symbol: dict[symbol → {n_signals, n_filled, n_skip, kelly, curve, ann,
                                   win_rate, avg_pnl, total_pnl}]
        all_filled: list[dict]（每笔成交记录，含 symbol）
        pnls/dates: 用于汇总 kelly
    """
    lake = pd.read_parquet(LAKE_PATH)
    window = DEFAULTS["window"]
    per_symbol = {}
    all_filled = []
    for sym in GOLDEN_SYMBOLS:
        sym_df = lake.xs(sym, level="symbol").sort_index()
        filled, n_sig, n_skip = scan_symbol(sym_df, window)  # exec/id_cfg 默认读全局
        for r in filled:
            r["symbol"] = sym
        all_filled.extend(filled)
        # 单标的级凯利（样本可能为 0/1 → kelly_metrics 返回 0.0,1.0,0.0）
        sym_pnls = [r["avg_pnl_pct"] for r in filled]
        sym_dates = [pd.to_datetime(r["signal_date"]) for r in filled]
        sym_kelly, sym_curve, sym_ann = kelly_metrics(sym_pnls, sym_dates)
        sym_wins = sum(1 for p in sym_pnls if p > 0)
        per_symbol[sym] = {
            "n_signals": int(n_sig),
            "n_filled": len(filled),
            "n_skip": int(n_skip),
            "kelly": sym_kelly,
            "curve": sym_curve,
            "ann": sym_ann,
            "win_rate": (sym_wins / len(filled)) if filled else 0.0,
            "avg_pnl_pct": (sum(sym_pnls) / len(sym_pnls)) if sym_pnls else 0.0,
            "total_pnl_pct": sum(sym_pnls),
        }
    return per_symbol, all_filled


def _build_payload(per_symbol, all_filled):
    """组装 golden json payload（含标的清单 + 哈希 + 数值 + 元信息）。"""
    pnls = [r["avg_pnl_pct"] for r in all_filled]
    dates = [pd.to_datetime(r["signal_date"]) for r in all_filled]
    kelly, curve, ann = kelly_metrics(pnls, dates)
    wins = sum(1 for p in pnls if p > 0)
    return {
        "schema_version": 1,
        "captured_at_utc": datetime.now(timezone.utc).isoformat(),
        "scan_symbol_source": SCAN_SYMBOL_SOURCE,
        "symbols": list(GOLDEN_SYMBOLS),
        "defaults_hash": _stable_hash(DEFAULTS),
        "exec_defaults_hash": _stable_hash(EXEC_DEFAULTS),
        "defaults_snapshot": dict(DEFAULTS),
        "exec_defaults_snapshot": dict(EXEC_DEFAULTS),
        "per_symbol": per_symbol,
        "summary": {
            "n_filled_total": len(all_filled),
            "n_wins_total": wins,
            "win_rate": (wins / len(all_filled)) if all_filled else 0.0,
            "kelly": kelly,
            "curve": curve,
            "ann": ann,
            "avg_pnl_pct": (sum(pnls) / len(pnls)) if pnls else 0.0,
            "total_pnl_pct": sum(pnls),
        },
        # 逐笔明细（日期统一 isoformat；供深度 diff 调试，迁移不变量只看汇总数值）
        "trades": [
            {
                "symbol": r["symbol"],
                "signal_date": r["signal_date"].isoformat(),
                "buy_date": r.get("buy_date").isoformat() if r.get("buy_date") else None,
                "exit_date": r.get("exit_date").isoformat() if r.get("exit_date") else None,
                "exit_reason": r.get("exit_reason"),
                "neckline": r.get("neckline"),
                "entry": r.get("entry"),
                "exit_price": r.get("exit_price"),
                "tp1": r.get("tp1"),
                "tp2": r.get("tp2"),
                "risk_pct": r.get("risk_pct"),
                "lot1_pnl_pct": r.get("lot1_pnl_pct"),
                "lot2_pnl_pct": r.get("lot2_pnl_pct"),
                "avg_pnl_pct": r.get("avg_pnl_pct"),
                "holding_bars": r.get("holding_bars"),
            }
            for r in all_filled
        ],
    }


def _round_floats(obj, ndigits=12):
    """递归把 payload 里所有 float 圆整到 ndigits（消除浮点末位抖动，逐位对比更稳）。

    物理意图：scan_symbol 内部 round(..., 2/3) 已限定位数，但 kelly/curve/ann 是
    连续浮点运算结果，跨 Python minor 版本/平台可能有 1e-15 级抖动。圆整到 12 位
    （远高于业务打印精度）消除噪声，保留真实漂移检测能力。
    """
    if isinstance(obj, float):
        return round(obj, ndigits)
    if isinstance(obj, dict):
        return {k: _round_floats(v, ndigits) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_round_floats(v, ndigits) for v in obj]
    return obj


def cmd_capture():
    """捕获 golden 基线 → 写 tests/_golden/neckline_baseline.json。"""
    per_symbol, all_filled = _run_pipeline()
    payload = _round_floats(_build_payload(per_symbol, all_filled))
    os.makedirs(os.path.dirname(GOLDEN_PATH), exist_ok=True)
    with open(GOLDEN_PATH, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)
    s = payload["summary"]
    print(f"[capture] golden 已落 {GOLDEN_PATH}")
    print(f"  标的={payload['symbols']}")
    print(f"  DEFAULTS hash={payload['defaults_hash'][:16]}  EXEC_DEFAULTS hash={payload['exec_defaults_hash'][:16]}")
    print(f"  汇总: n_filled={s['n_filled_total']} kelly={s['kelly']:.6f} "
          f"curve={s['curve']:.6f} ann={s['ann']:.6f}")
    for sym, m in payload["per_symbol"].items():
        print(f"    {sym}: n_sig={m['n_signals']} n_filled={m['n_filled']} "
              f"n_skip={m['n_skip']} kelly={m['kelly']:.6f} ann={m['ann']:.6f}")


def _diff(path, expected, actual):
    """递归对比两 dict/list/标量，返回差异路径列表（用于 --verify 报告）。"""
    diffs = []
    if isinstance(expected, dict) and isinstance(actual, dict):
        for k in expected:
            if k not in actual:
                diffs.append(f"{path}.{k}: MISSING in actual")
            else:
                diffs.extend(_diff(f"{path}.{k}", expected[k], actual[k]))
        # actual 多出的键报为 EXTRA：条件须为 "k 不在 expected"
        # （原写 "k not in actual" 是恒 False 死分支，EXTRA 永远抓不到）
        for k in actual:
            if k not in expected:
                diffs.append(f"{path}.{k}: EXTRA in actual")
    elif isinstance(expected, list) and isinstance(actual, list):
        if len(expected) != len(actual):
            diffs.append(f"{path}: len mismatch expected={len(expected)} actual={len(actual)}")
        for i, (e, a) in enumerate(zip(expected, actual)):
            diffs.extend(_diff(f"{path}[{i}]", e, a))
    elif isinstance(expected, float) or isinstance(actual, float):
        # 浮点用 abs 容差 1e-9（对齐 brief：pytest.approx(abs=1e-9) 口径）
        if abs(float(expected) - float(actual)) > 1e-9:
            diffs.append(f"{path}: float drift expected={expected} actual={actual}")
    else:
        if expected != actual:
            diffs.append(f"{path}: expected={expected!r} actual={actual!r}")
    return diffs


def cmd_verify():
    """重跑 → 与 golden json 逐位对比 → 全一致 exit 0，任何漂移 exit 1。"""
    if not os.path.exists(GOLDEN_PATH):
        print(f"[verify][FAIL] golden 文件不存在: {GOLDEN_PATH}（先 --capture）", file=sys.stderr)
        return 2
    with open(GOLDEN_PATH, encoding="utf-8") as f:
        golden = json.load(f)

    per_symbol, all_filled = _run_pipeline()
    actual = _round_floats(_build_payload(per_symbol, all_filled))

    # 守护指纹：DEFAULTS/EXEC_DEFAULTS 哈希必须一致（误 update 全局默认即报警）
    if actual["defaults_hash"] != golden["defaults_hash"]:
        print(f"[verify][FAIL] DEFAULTS hash 漂移：golden={golden['defaults_hash'][:16]} "
              f"actual={actual['defaults_hash'][:16]}", file=sys.stderr)
        return 1
    if actual["exec_defaults_hash"] != golden["exec_defaults_hash"]:
        print(f"[verify][FAIL] EXEC_DEFAULTS hash 漂移：golden={golden['exec_defaults_hash'][:16]} "
              f"actual={actual['exec_defaults_hash'][:16]}", file=sys.stderr)
        return 1

    # captured_at_utc 是运行时时间戳，比较前剔除（不是数值不变量）
    golden_cmp = {k: v for k, v in golden.items() if k != "captured_at_utc"}
    actual_cmp = {k: v for k, v in actual.items() if k != "captured_at_utc"}

    diffs = _diff("$", golden_cmp, actual_cmp)
    if diffs:
        print(f"[verify][FAIL] 检出 {len(diffs)} 处漂移（纯重构下应零漂移，立即 revert 排查）：",
              file=sys.stderr)
        for d in diffs[:50]:
            print(f"  {d}", file=sys.stderr)
        if len(diffs) > 50:
            print(f"  ...（共 {len(diffs)} 处，仅显示前 50）", file=sys.stderr)
        return 1

    s = actual["summary"]
    print(f"[verify][PASS] golden 逐位一致（{len(diffs)} 漂移）")
    print(f"  标的={actual['symbols']}  DEFAULTS hash={actual['defaults_hash'][:16]}")
    print(f"  汇总: n_filled={s['n_filled_total']} kelly={s['kelly']:.6f} "
          f"curve={s['curve']:.6f} ann={s['ann']:.6f}")
    return 0


def main():
    ap = argparse.ArgumentParser(description="颈线法数值回归 T1 golden 基线（Layer 2 解耦数值锚）")
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--capture", action="store_true",
                      help="捕获 golden 基线 → 写 tests/_golden/neckline_baseline.json")
    mode.add_argument("--verify", action="store_true",
                      help="重跑并与 golden 逐位对比（纯重构下必须零漂移）")
    args = ap.parse_args()
    if args.capture:
        cmd_capture()
    else:
        sys.exit(cmd_verify())


if __name__ == "__main__":
    main()
