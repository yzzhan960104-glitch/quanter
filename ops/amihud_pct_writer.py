# -*- coding: utf-8 -*-
"""amihud60 全市场截面分位写入器（NECK 信号过滤的数据面 · 2026-08-29 用户采纳）。

物理定位：
    emquant NECK 主腿的 amihud 非流动性信号过滤（规格=logs/quality/factor_zoo/
    signal_cutdown_final.md）需要「当日全湖 amihud60 分位表」，但策略进程在
    15:36 调度窗内现算 5800 只×61 根不现实——本写入器挂 data_pipeline 尾部
    （18:00 后湖已含当日 K 线），每晚算好截面分位写进主腿策略目录 state/，
    策略次日 15:36 直接读文件。

语义（与回测验证形态逐位一致，勿改——改了要重过四闸）：
    amihud60 = mean(|日收益|/成交额, 60d, min 48 有效)；rank(axis=1, pct=True)
    取**最新行**；行有效率 <50% 判坏行（2024-Q1 amount 断层实锤）→ 回退上一
    好行（ffill）；交付滞后一个交易日（T 日信号用 T-1 收盘截面——60 日慢变量
    日间秩相关 ~0.995，lag-1 形态已单独过四闸：外层 Δ+16.2pp/全期 +2.3pp/
    逐年全过/taken 49→70）。

失败契约：湖缺失/算不出 → rc=1（pipeline 记 ⚠️ 不阻断）；策略侧文件缺失/
    过期（>max_stale_days=3 自然日）→ 过滤旁路 fail-open + audit WARN（维持
    现任行为，绝不因数据面故障停摆）。

用法：
    ./.venv310/Scripts/python.exe -m ops.amihud_pct_writer [--dir <策略目录>]
产物：
    <主腿策略目录>/state/amihud_pct_latest.json   （策略消费面）
    logs/amihud_pct/amihud_pct_<date>.json        （仓内审计留档）
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from diag.quality_momentum_batch2 import _matrix

LAKE = ROOT / "data_lake" / "a_shares_daily.parquet"
LOAD_FROM = "2019-09-01"
WINDOW = 60
MIN_DAYS = 48
BAD_ROW_VALID = 0.5          # 行有效率低于此 → 坏行（回退上一好行）
ARCHIVE_DIR = ROOT / "logs" / "amihud_pct"


def compute_pct_row() -> dict:
    """湖 → 最新好行的 {symbol: pct} + 元数据（坏行 ffill 语义）。"""
    import numpy as np
    import pandas as pd

    w = _matrix(str(LAKE), ["close", "amount"], pd.Timestamp(LOAD_FROM))
    close = w["close"].astype("float32")
    amt = w["amount"].astype("float32")
    ret = close.pct_change(fill_method=None).astype("float32")
    amihud = (ret.abs() / (amt + 1.0)).rolling(WINDOW, min_periods=MIN_DAYS) \
        .mean().astype("float32")
    mkt = amihud.rank(axis=1, pct=True).astype("float32")
    valid = mkt.notna().mean(axis=1)
    latest = mkt.index[-1]
    # 坏行回退：从最新行往前找第一个有效率 ≥ 阈值的好行（等价 ffill 尾值）
    good = valid[valid >= BAD_ROW_VALID]
    if good.empty:
        raise RuntimeError("全湖无有效率达标行——湖 amount 面疑似整体故障")
    use_date = good.index[-1]
    row = mkt.loc[use_date]
    pct = {s: round(float(v), 4) for s, v in row.dropna().items()}
    return {"date": str(use_date.date()), "lake_latest": str(latest.date()),
            "row_valid_frac": round(float(valid.loc[use_date]), 4),
            "ffilled": bool(use_date != latest), "n_stocks": len(pct),
            "window": WINDOW, "min_days": MIN_DAYS, "pct": pct}


def main(argv: list[str] | None = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(description="amihud60 全市场分位写入器")
    ap.add_argument("--dir", help="主腿策略目录（缺省走 ops.gm_ops_common 解析）")
    args = ap.parse_args(argv)

    from infra.pyio import force_utf8_stdout
    force_utf8_stdout()
    t0 = time.time()
    payload = compute_pct_row()
    payload["computed_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    body = json.dumps(payload, ensure_ascii=False)

    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    arch = ARCHIVE_DIR / f"amihud_pct_{payload['date']}.json"
    arch.write_text(body, encoding="utf-8")

    strat_dir = Path(args.dir) if args.dir else None
    if strat_dir is None:
        try:
            from ops.gm_ops_common import LEGS, leg_strategy_dir
            main_leg = next(l for l in LEGS if l.key == "main")
            strat_dir = Path(leg_strategy_dir(main_leg))
        except Exception as e:
            print(f"[amihud_pct] ⚠️ 主腿目录解析失败（仓内留档已写 {arch}）：{e!r}")
            return 1
    state_dir = strat_dir / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    out = state_dir / "amihud_pct_latest.json"
    tmp = out.with_suffix(".tmp")
    tmp.write_text(body, encoding="utf-8")
    tmp.replace(out)
    print(f"[amihud_pct] date={payload['date']} n={payload['n_stocks']} "
          f"ffilled={payload['ffilled']} -> {out}（{time.time() - t0:.0f}s）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
