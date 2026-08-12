# -*- coding: utf-8 -*-
"""P0-2：湖深细节核（spec §1 P0-2 / P5 walk-forward 数据可行性）。

物理意图（Why）：P5 walk-forward 分折（2020-21/22-23/24/25 + 2026 OOS）需要逐年足够的
创板科创标的覆盖；且每折重建 universe 必须保留【退市标的的挂牌期数据】防幸存者偏差。
本脚本逐年统计覆盖，并标出退市/休眠标的。

硬约束（实证）：科创（688/689）2019-07 开板，故 2020 前【无科创数据】——2020-21 折的
创板科创 universe 实际=创板（全）+ 科创（2020 起）。本脚本量化每折可用规模。

只用 data_lake（groupby 实证交易日），不依赖 Tushare token。
验收门（spec §1）：结论可支撑 P5 分折定义（2020+ 每年创板科创 ≥ 阈值，见 §实测结果）。
"""
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import pandas as pd  # noqa: E402

LAKE = _ROOT / "data_lake" / "a_shares_daily.parquet"


def _is_kbkg(sym: str) -> bool:
    """创板(300/301)+科创(688/689)，与 discovery.snapshot.is_target_board 同源。"""
    return sym.split(".")[0].startswith(("300", "301", "688", "689"))


def main():
    if not LAKE.exists():
        print("[P0-2] data_lake 缺失，跳过", file=sys.stderr)
        sys.exit(2)

    lake = pd.read_parquet(LAKE).reset_index()
    lake["date"] = pd.to_datetime(lake["date"])
    lake["year"] = lake["date"].dt.year
    lake["kbkg"] = lake["symbol"].apply(_is_kbkg)
    latest = lake["date"].max()
    print(f"湖日期范围：{lake['date'].min().date()} ~ {latest.date()}  "
          f"总行数 {len(lake):,}", flush=True)

    # ① 逐年覆盖（全市场 / 创板科创 / 科创单独，量化 2019-07 开板约束）
    print("\n=== 逐年标的覆盖 ===", flush=True)
    print(f"{'year':>6}{'全市场':>10}{'创板科创':>12}{'科创(688/689)':>16}", flush=True)
    per_year_kbkg = {}
    for year in range(2016, latest.year + 1):
        sub = lake[lake["year"] == year]
        total = sub["symbol"].nunique()
        kbkg = sub[sub["kbkg"]]["symbol"].nunique()
        kechuang = sub[sub["symbol"].apply(
            lambda s: s.split(".")[0].startswith(("688", "689")))]["symbol"].nunique()
        per_year_kbkg[year] = kbkg
        print(f"{year:>6}{total:>10}{kbkg:>12}{kechuang:>16}", flush=True)

    # ② 退市/休眠痕迹：last_date < 2026-01-01 的标的（分折 universe 须保留其挂牌期数据）
    last = lake.groupby("symbol")["date"].max()
    delisted_mask = last < pd.Timestamp("2026-01-01")
    delisted = last[delisted_mask]
    kbkg_delisted = [s for s in delisted.index if _is_kbkg(s)]
    print(f"\n=== 退市/休眠痕迹（last_date < 2026-01-01）===", flush=True)
    print(f"  全市场：{len(delisted)} 只   创板科创：{len(kbkg_delisted)} 只", flush=True)
    if kbkg_delisted:
        # 抽样 10 只看最后交易日分布（判断是真退市还是长期停牌）
        sample = sorted(kbkg_delisted, key=lambda s: last[s])[:10]
        print("  最早收尾 10 只（创板科创）：", flush=True)
        for s in sample:
            print(f"    {s}  last_date={last[s].date()}", flush=True)

    # 验收门：P5 折 2020-21/22-23/24/25 每年创板科创覆盖
    folds = {"2020-21": (2020, 2021), "2022-23": (2022, 2023),
             "2024": (2024,), "2025": (2025,), "2026 OOS": (2026,)}
    print(f"\n=== P5 walk-forward 分折可行性（创板科创 min 年覆盖）===", flush=True)
    feasible = True
    for fold, years in folds.items():
        cov = min((per_year_kbkg.get(y, 0) for y in years), default=0)
        ok = cov >= 200  # 阈值：单年创板科创 ≥200 只视为分折可用（spec §0.3 湖深实证 10M 行）
        feasible = feasible and ok
        print(f"  折 {fold:>8}：min 创板科创 = {cov:>5}  {'[OK]' if ok else '[不足]'}", flush=True)
    verdict = "[OK] 支撑 P5 分折定义" if feasible else "[WARN] 部分折覆盖不足 -> Task 5 调整 P5 折定义/阈值"
    print(f"\n=== 验收门 -> {verdict} ===", flush=True)


if __name__ == "__main__":
    main()
